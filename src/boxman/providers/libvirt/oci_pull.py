"""Pull and inspect VM disk images from an OCI registry via the ``oras`` CLI.

Counterpart to :mod:`boxman.providers.libvirt.oci_push`. A VM image is stored as
a plain OCI artifact: a qcow2 blob, optionally accompanied by a ``vmimage.json``
metadata sidecar.

Authentication is delegated to oras and follows oras-supported methods:

- Environment variables: ``ORAS_USERNAME`` / ``ORAS_PASSWORD``
- Config file: ``~/.oras/config.json``
- The ``oras login`` credential store
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from boxman import log


# ── metadata sidecar (vmimage.json) ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VmImageMetadata:
    """VM image metadata shipped alongside the qcow2 blob as ``vmimage.json``.

    Fields are intentionally small and provider-agnostic. For now boxman only
    *surfaces* this metadata (via ``boxman image inspect``); it does not yet
    apply it to VM/template creation.
    """

    firmware: str = "uefi"  # "uefi" or "bios"
    machine: Optional[str] = None
    disk_bus: str = "virtio"
    net_model: str = "virtio"

    # display / informational
    name: Optional[str] = None
    version: Optional[str] = None
    arch: Optional[str] = None


def _metadata_from_dict(raw: dict) -> VmImageMetadata:
    """Build a :class:`VmImageMetadata` from a raw dict, honoring known keys only.

    Unknown keys are ignored for forward-compatibility.
    """
    return VmImageMetadata(
        firmware=str(raw.get("firmware", "uefi")),
        machine=raw.get("machine"),
        disk_bus=str(raw.get("disk_bus", "virtio")),
        net_model=str(raw.get("net_model", "virtio")),
        name=raw.get("name"),
        version=raw.get("version"),
        arch=raw.get("arch"),
    )


# ── helpers ──────────────────────────────────────────────────────────────────


def _strip_scheme(image_ref: str) -> str:
    """Drop a leading ``oci://`` scheme if present."""
    if image_ref.startswith("oci://"):
        return image_ref[len("oci://"):]
    return image_ref


def _repo_without_tag(ref: str) -> str:
    """Return the repository part of an OCI ref (drop a trailing ``:tag`` or
    ``@digest``).

    Handles registries with a port (e.g. ``localhost:5000/repo:tag`` ->
    ``localhost:5000/repo``) by only treating a colon that follows the last
    ``/`` as the tag separator, and digest-pinned refs
    (``repo@sha256:<hex>``) whose digest ``:`` must not be mistaken for a tag.
    """
    # Digest-pinned ref: the repository is everything before '@'.
    if "@" in ref:
        return ref.split("@", 1)[0]
    slash = ref.rfind("/")
    tail = ref[slash + 1:]
    if ":" in tail:
        name = tail.rsplit(":", 1)[0]
        return ref[: slash + 1] + name
    return ref


def _find_qcow2(out_dir: Path) -> Optional[Path]:
    """Locate the qcow2 in a pulled oras output directory.

    Prefers ``disk.qcow2``; otherwise the first ``*.qcow2`` (sorted).
    """
    preferred = out_dir / "disk.qcow2"
    if preferred.is_file():
        return preferred
    candidates = sorted(out_dir.glob("*.qcow2"))
    if len(candidates) > 1:
        log.warning(
            f"OCI artifact in '{out_dir}' has multiple qcow2 files and no "
            f"'disk.qcow2'; using '{candidates[0].name}'. Name the base disk "
            f"'disk.qcow2' to make this unambiguous.")
    return candidates[0] if candidates else None


def _run_oras(cmd: list, action: str, ref: str) -> subprocess.CompletedProcess:
    """Run an oras command, normalizing missing-CLI and non-zero-exit errors.

    Raises:
        RuntimeError: If the oras CLI is not on PATH or the command fails.
    """
    try:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "oras CLI not found. Please install 'oras' and ensure it is on PATH."
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            "oras {action} failed for '{ref}'.\n"
            "command: {cmd}\n"
            "exit code: {code}\n"
            "stdout:\n{stdout}\n"
            "stderr:\n{stderr}\n".format(
                action=action,
                ref=ref,
                cmd=" ".join(cmd),
                code=result.returncode,
                stdout=(result.stdout or "").strip(),
                stderr=(result.stderr or "").strip(),
            )
        )
    return result


# ── container image / KubeVirt containerDisk support ─────────────────────────


# OCI / docker container-image config media types. A manifest whose config is
# one of these is a runnable container image (and therefore a candidate
# KubeVirt containerDisk) rather than a boxman oras artifact.
_CONTAINER_CONFIG_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}

# Map the host machine to an OCI platform architecture for multi-arch indexes.
_ARCH_MAP = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "ppc64le": "ppc64le",
    "s390x": "s390x",
}


def _host_arch() -> str:
    """Return the host's OCI architecture (e.g. ``amd64``).

    An unmapped machine falls back to the raw ``platform.machine()`` value rather
    than masquerading as amd64, so an index that lacks the host's architecture
    fails to match and raises a clear error instead of silently selecting a
    wrong-architecture disk.
    """
    machine = platform.machine().lower()
    return _ARCH_MAP.get(machine, machine)


def _fetch_manifest(ref: str) -> dict:
    """Fetch and parse an OCI manifest via ``oras manifest fetch``.

    Raises:
        ValueError: If the manifest is empty or not valid JSON.
        RuntimeError: If oras is missing or the fetch fails.
    """
    result = _run_oras(
        ["oras", "manifest", "fetch", ref], action="manifest fetch", ref=ref)
    stdout = (result.stdout or "").strip()
    if not stdout:
        # An empty response (vs a parse/transport error) would otherwise become
        # {} and resurface later as a misleading "no qcow2" failure.
        raise ValueError(f"empty manifest response from oras for '{ref}'")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"could not parse manifest for '{ref}': {exc}") from exc


def _manifest_has_qcow2_title(manifest: dict) -> bool:
    """True if any layer is a titled ``*.qcow2`` (boxman/oras artifact convention)."""
    for layer in manifest.get("layers", []) or []:
        title = (layer.get("annotations", {}) or {}).get(
            "org.opencontainers.image.title", "")
        if title.endswith(".qcow2"):
            return True
    return False


def _manifest_kind(manifest: dict) -> str:
    """Classify a manifest: ``image-index`` | ``artifact`` | ``image`` | ``unknown``.

    A container-image config takes precedence over a ``*.qcow2`` layer title, so
    a containerDisk that happens to annotate a layer title is still treated as an
    image (its embedded qcow2 is extracted) rather than mis-pulled as a raw
    artifact (which would yield a compressed layer tarball, not a real qcow2).
    """
    if manifest.get("manifests"):
        return "image-index"
    if (manifest.get("config") or {}).get("mediaType", "") in _CONTAINER_CONFIG_TYPES:
        return "image"
    if _manifest_has_qcow2_title(manifest):
        return "artifact"
    return "unknown"


def _select_index_digest(manifest: dict) -> Optional[str]:
    """Pick the ``linux/<host-arch>`` manifest digest from an image index.

    Matches strictly on the host OS/architecture — selecting a different
    architecture would yield a disk that cannot boot here. Attestation/SBOM
    entries (``architecture == 'unknown'`` or no platform) simply do not match.
    Returns ``None`` when the index carries no manifest for this host.
    """
    host = _host_arch()
    for entry in manifest.get("manifests", []) or []:
        plat = entry.get("platform") or {}
        if plat.get("os") == "linux" and plat.get("architecture") == host:
            return entry.get("digest")
    return None


def _resolve_image_manifest(ref: str, manifest: dict, _max_depth: int = 5) -> dict:
    """Resolve a possibly-multi-arch manifest to a concrete image manifest.

    Follows image indexes to the host-platform manifest, including nested
    indexes (index -> index) up to *_max_depth* hops; a non-index manifest is
    returned unchanged.
    """
    repo = _repo_without_tag(ref)
    for _ in range(_max_depth):
        if not manifest.get("manifests"):
            return manifest
        digest = _select_index_digest(manifest)
        if not digest:
            raise RuntimeError(
                f"image index for '{ref}' has no manifest for "
                f"linux/{_host_arch()}")
        manifest = _fetch_manifest(f"{repo}@{digest}")
    raise RuntimeError(
        f"image index for '{ref}' nests deeper than {_max_depth} levels")


# A KubeVirt containerDisk carries the VM disk under a *root-level* ``disk/``
# directory, named freely (commonly ``disk/<name>.img`` — often a qcow2 with a
# ``.img`` name — sometimes with no extension at all). Root-level ``disk/``
# membership is therefore the only reliable signal: matching on a disk-image
# extension anywhere in the rootfs would false-positive on ordinary files such
# as ``/boot/initrd.img``. The extracted disk's *content* is validated as qcow2
# separately (see ``_is_qcow2``).
_QCOW2_MAGIC = b"QFI\xfb"


def _is_disk_candidate(name: str) -> bool:
    """True if a tar member could be the containerDisk's VM disk.

    A candidate is any path under a *root-level* ``disk/`` directory, excluding
    overlay ``.wh.`` whiteout markers (deletion tombstones, never real content).
    The leading ``./`` some tars prefix is stripped first; note ``str.lstrip``
    must NOT be used for that — it strips a character set and would eat the dot
    of a root-level ``.wh.`` name.
    """
    norm = name.replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    if not norm.startswith("disk/"):
        return False
    base = os.path.basename(norm)
    return bool(base) and not base.startswith(".wh.")


def _is_qcow2(path: Path) -> bool:
    """True if *path* begins with the qcow2 magic (``QFI\\xfb``)."""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == _QCOW2_MAGIC
    except OSError:
        return False


def _blob_fetch_to_file(repo: str, digest: str, dest: Path) -> None:
    """Fetch a layer blob by digest to *dest* via ``oras blob fetch``."""
    _run_oras(
        ["oras", "blob", "fetch", f"{repo}@{digest}", "--output", str(dest)],
        action="blob fetch", ref=f"{repo}@{digest}")


def _extract_disk_from_layer(blob_path: Path, out_dir: Path) -> Optional[Path]:
    """Extract the embedded VM disk image from a single (compressed) layer tarball.

    Streams the tar (``r|*`` auto-detects gzip/bzip2/xz; zstd is unsupported and
    surfaces a clear error). Considers only real files under a root-level
    ``disk/`` directory (the KubeVirt containerDisk convention) and keeps the
    *largest* of them — the VM disk dwarfs any sidecar (README/checksum/metadata)
    that may share the directory. Smaller candidates are discarded from the tar
    header's size alone, so only the running best is ever written. Overlay
    ``.wh.`` whiteout markers are ignored. Only the basename is used for the
    output file, so a malicious member name cannot traverse outside *out_dir*.

    Returns the path to the extracted disk image, or ``None`` if the layer has none.
    """
    chosen: Optional[Path] = None
    chosen_size = -1
    writing: Optional[Path] = None
    try:
        with open(blob_path, "rb") as raw, tarfile.open(fileobj=raw, mode="r|*") as tar:
            for member in tar:
                # isfile() is False for symlinks/hardlinks/dirs/devices, so only
                # real file content is ever read; basename() neutralises any
                # absolute / '..' member name — no member can write outside out_dir.
                if not (member.isfile() and _is_disk_candidate(member.name)):
                    continue
                # The disk is the largest file under disk/; a smaller candidate is
                # rejected from its header without extracting it.
                if member.size <= chosen_size:
                    continue
                src = tar.extractfile(member)
                if src is None:
                    continue
                dest = out_dir / os.path.basename(member.name)
                writing = dest
                with src, open(dest, "wb") as fh:
                    shutil.copyfileobj(src, fh, 1024 * 1024)
                writing = None
                if chosen is not None and chosen != dest:
                    chosen.unlink(missing_ok=True)  # drop the smaller earlier disk
                chosen, chosen_size = dest, member.size
    except tarfile.ReadError as exc:
        # a partially written disk from a truncated stream must not be left
        # behind where it could later be mistaken for a complete disk
        if writing is not None:
            writing.unlink(missing_ok=True)
        raise RuntimeError(
            f"could not read OCI layer tarball '{blob_path.name}': {exc} "
            "(only gzip/bzip2/xz-compressed layers are supported; zstd is "
            "not)") from exc
    return chosen


def _extract_embedded_disk(ref: str, manifest: dict, out_dir: Path) -> Optional[Path]:
    """Extract the VM disk image embedded in a container image's layers.

    Layers are scanned from topmost to bottom (so an upper layer's disk wins);
    the first layer that yields a disk image is used. Overlay whiteouts are
    honoured only within a layer, not across layers — containerDisks are built
    ``FROM scratch`` as a single layer, so a disk deleted by an upper-layer
    whiteout is not expected.
    """
    repo = _repo_without_tag(ref)
    layers = manifest.get("layers", []) or []
    blob_path = out_dir / ".boxman-layer.blob"
    for layer in reversed(layers):
        digest = layer.get("digest")
        if not digest:
            continue
        try:
            _blob_fetch_to_file(repo, digest, blob_path)
            disk = _extract_disk_from_layer(blob_path, out_dir)
        finally:
            blob_path.unlink(missing_ok=True)
        if disk is not None:
            return disk
    return None


# ── public interface ─────────────────────────────────────────────────────────


def pull_oci_image(image_ref: str, out_dir: str) -> str:
    """Pull an OCI image and return the local path to its VM disk (a qcow2).

    Two source layouts are supported:

    * **boxman / oras artifact** — a qcow2 stored as a titled OCI layer
      (``oras push disk.qcow2``); fetched with ``oras pull``.
    * **container image / KubeVirt containerDisk** — a VM disk embedded under a
      root-level ``disk/`` directory inside a container image (named freely,
      commonly ``disk/<name>.img``, e.g. ``quay.io/containerdisks/...``); the
      carrying layer is fetched and the disk extracted from it. The extracted
      disk's content must be qcow2 — raw and other formats are rejected with a
      clear error rather than silently producing an unbootable VM.

    Args:
        image_ref: OCI reference, with or without an ``oci://`` scheme
            (e.g. ``oci://registry.example.com/repo:tag``).
        out_dir: Directory to pull the artifact into (created if needed).

    Returns:
        Absolute path to the local VM disk (qcow2 content; the file name may end
        in ``.img`` or have no extension when taken from a containerDisk).

    Raises:
        ValueError: If *image_ref* is empty, or the registry returns an empty
            or unparseable manifest.
        RuntimeError: If oras is missing, a registry call fails, no usable disk
            can be obtained, or the embedded disk is not in qcow2 format.
    """
    if not image_ref or not str(image_ref).strip():
        raise ValueError("image_ref must be a non-empty string")

    ref = _strip_scheme(str(image_ref).strip())
    out = Path(os.path.expanduser(out_dir)).resolve()
    out.mkdir(parents=True, exist_ok=True)

    # Fetch the manifest first: this is the initial registry call, so a bad ref
    # / auth / network failure surfaces clearly here instead of being masked by
    # a fallback. Resolve a multi-arch index to the host-platform manifest.
    manifest = _resolve_image_manifest(ref, _fetch_manifest(ref))

    if _manifest_kind(manifest) == "artifact":
        # boxman/oras artifact: a titled qcow2 layer that `oras pull` extracts.
        _run_oras(["oras", "pull", ref, "-o", str(out)], action="pull", ref=ref)
        qcow2 = _find_qcow2(out)
        if qcow2 is None:
            raise RuntimeError(
                f"OCI image '{ref}' pulled to '{out}', but no qcow2 was found "
                "(expected 'disk.qcow2' or any '*.qcow2').")
        return str(qcow2)

    # Otherwise treat it as a container image (e.g. KubeVirt containerDisk) and
    # extract the VM disk image embedded in its layers.
    disk = _extract_embedded_disk(ref, manifest, out)
    if disk is None:
        raise RuntimeError(
            f"OCI image '{ref}' has no VM disk: it is neither a boxman artifact "
            "(a titled '*.qcow2' layer) nor a container image with an embedded "
            "disk (expected a KubeVirt-style containerDisk carrying a file under "
            "a root-level '/disk/' directory, e.g. '/disk/<name>.img').")
    if not _is_qcow2(disk):
        disk.unlink(missing_ok=True)  # don't cache a disk we can't boot
        raise RuntimeError(
            f"OCI image '{ref}' embeds a VM disk that is not in qcow2 format. "
            "boxman imports containerDisk disks as qcow2; raw and other formats "
            "are not yet supported. Convert it to qcow2 (e.g. `qemu-img convert "
            "-O qcow2`) and publish it as a boxman OCI artifact with "
            "`boxman image push --qcow2 ...`.")
    return str(disk)


def _fetch_vmimage_metadata(ref: str, layers: list) -> Optional[VmImageMetadata]:
    """Best-effort fetch of the ``vmimage.json`` blob for inspection.

    Returns ``None`` if there is no such layer or the small blob cannot be
    fetched/parsed — inspection must not fail just because metadata is absent.
    """
    digest = None
    for layer in layers:
        if layer.get("title") == "vmimage.json":
            digest = layer.get("digest")
            break
    if not digest:
        return None

    repo = _repo_without_tag(ref)
    try:
        result = subprocess.run(
            ["oras", "blob", "fetch", f"{repo}@{digest}", "--output", "-"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return None

    if result.returncode != 0 or not result.stdout:
        # Distinguish in the logs: a non-zero blob fetch (e.g. auth/network)
        # is not the same as "image has no metadata", but inspect must not fail.
        log.debug(
            f"could not fetch vmimage.json blob for '{ref}' "
            f"(oras blob fetch rc={result.returncode}); "
            f"metadata will be reported as <none>")
        return None

    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    return _metadata_from_dict(raw)


def inspect_oci_image(image_ref: str) -> dict:
    """Inspect an OCI image reference without downloading the full qcow2 blob.

    Fetches the manifest via ``oras manifest fetch`` and summarizes its layers,
    media type and annotations. When a ``vmimage.json`` layer is present, that
    small blob is fetched and parsed so its metadata can be surfaced.

    Args:
        image_ref: OCI reference, with or without an ``oci://`` scheme.

    Returns:
        A summary dict with keys ``image_ref``, ``kind`` (``artifact`` |
        ``image`` | ``image-index`` | ``unknown``), ``media_type``, ``layers``
        (each ``{title, media_type, size, digest}``), ``manifests``,
        ``annotations`` and ``metadata`` (a :class:`VmImageMetadata` or
        ``None``). For a container image / image index, the embedded qcow2 is
        not downloaded here; it is extracted on ``pull``.

    Raises:
        ValueError: If *image_ref* is empty or the manifest is not valid JSON.
        RuntimeError: If oras is missing or the manifest fetch fails.
    """
    if not image_ref or not str(image_ref).strip():
        raise ValueError("image_ref must be a non-empty string")

    ref = _strip_scheme(str(image_ref).strip())
    manifest = _fetch_manifest(ref)

    layers = []
    for layer in manifest.get("layers", []) or []:
        ann = layer.get("annotations", {}) or {}
        layers.append({
            "title": ann.get("org.opencontainers.image.title", ""),
            "media_type": layer.get("mediaType", ""),
            "size": layer.get("size", 0),
            "digest": layer.get("digest", ""),
        })

    # An image index (multi-arch manifest list) carries `manifests` instead of
    # `layers`; surface those so inspect doesn't report an empty artifact.
    index_manifests = []
    for entry in manifest.get("manifests", []) or []:
        plat_info = entry.get("platform", {}) or {}
        plat = "/".join(
            part for part in (
                plat_info.get("os"),
                plat_info.get("architecture"),
                plat_info.get("variant"),
            ) if part)
        index_manifests.append({
            "media_type": entry.get("mediaType", ""),
            "size": entry.get("size", 0),
            "digest": entry.get("digest", ""),
            "platform": plat,
        })

    return {
        "image_ref": ref,
        "kind": _manifest_kind(manifest),
        "media_type": manifest.get("mediaType", ""),
        "layers": layers,
        "manifests": index_manifests,
        "annotations": manifest.get("annotations", {}) or {},
        "metadata": _fetch_vmimage_metadata(ref, layers),
    }


def format_inspect(summary: dict) -> str:
    """Render an :func:`inspect_oci_image` summary as human-readable text."""
    lines = [f"image_ref: {summary.get('image_ref', '')}"]

    kind = summary.get("kind")
    if kind:
        lines.append(f"kind: {kind}")
        if kind in ("image", "image-index"):
            lines.append(
                "  (container image — if it is a KubeVirt-style containerDisk, "
                "boxman extracts the VM disk under /disk/ on pull)")

    media_type = summary.get("media_type")
    if media_type:
        lines.append(f"media_type: {media_type}")

    layers = summary.get("layers") or []
    lines.append(f"layers: {len(layers)}")
    for layer in layers:
        title = layer.get("title") or "<untitled>"
        lines.append(
            f"  - {title} "
            f"({layer.get('media_type', '')}, {layer.get('size', 0)} bytes)")

    manifests = summary.get("manifests") or []
    if manifests:
        lines.append(f"manifests (image index): {len(manifests)}")
        for entry in manifests:
            plat = entry.get("platform") or "unknown"
            lines.append(
                f"  - {plat} "
                f"({entry.get('media_type', '')}, {entry.get('size', 0)} bytes)")

    annotations = summary.get("annotations") or {}
    if annotations:
        lines.append("annotations:")
        for key, value in annotations.items():
            lines.append(f"  {key}: {value}")

    md = summary.get("metadata")
    if md is None:
        lines.append("metadata: <none>")
    else:
        lines.append("metadata:")
        lines.append(f"  firmware: {md.firmware}")
        lines.append(f"  machine: {md.machine}")
        lines.append(f"  disk_bus: {md.disk_bus}")
        lines.append(f"  net_model: {md.net_model}")
        if md.name:
            lines.append(f"  name: {md.name}")
        if md.version:
            lines.append(f"  version: {md.version}")
        if md.arch:
            lines.append(f"  arch: {md.arch}")

    return "\n".join(lines) + "\n"
