"""Pull and inspect qcow2 VM images from an OCI registry via the ``oras`` CLI.

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
    """Return the host's OCI architecture (e.g. ``amd64``), defaulting to amd64."""
    return _ARCH_MAP.get(platform.machine().lower(), "amd64")


def _fetch_manifest(ref: str) -> dict:
    """Fetch and parse an OCI manifest via ``oras manifest fetch``.

    Raises:
        ValueError: If the manifest is not valid JSON.
        RuntimeError: If oras is missing or the fetch fails.
    """
    result = _run_oras(
        ["oras", "manifest", "fetch", ref], action="manifest fetch", ref=ref)
    try:
        return json.loads(result.stdout or "{}")
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
    """Classify a manifest: ``image-index`` | ``artifact`` | ``image`` | ``unknown``."""
    if manifest.get("manifests"):
        return "image-index"
    if _manifest_has_qcow2_title(manifest):
        return "artifact"
    if (manifest.get("config") or {}).get("mediaType", "") in _CONTAINER_CONFIG_TYPES:
        return "image"
    return "unknown"


def _select_index_digest(manifest: dict) -> Optional[str]:
    """Pick the platform manifest digest from an image index for the host arch.

    Prefers ``linux/<host-arch>``; falls back to the first ``linux`` entry, then
    the first non-attestation entry. Entries with ``architecture == 'unknown'``
    (attestation/SBOM manifests) are skipped.
    """
    entries = [
        e for e in (manifest.get("manifests", []) or [])
        if (e.get("platform") or {}).get("architecture") not in (None, "unknown")
    ]
    host = _host_arch()
    for entry in entries:
        plat = entry.get("platform") or {}
        if plat.get("os") == "linux" and plat.get("architecture") == host:
            return entry.get("digest")
    for entry in entries:
        if (entry.get("platform") or {}).get("os") == "linux":
            return entry.get("digest")
    return entries[0].get("digest") if entries else None


def _resolve_image_manifest(ref: str, manifest: dict) -> dict:
    """Resolve a possibly-multi-arch manifest to a concrete image manifest.

    If *manifest* is an image index, fetch the host-platform manifest it points
    at; otherwise return it unchanged.
    """
    if not manifest.get("manifests"):
        return manifest
    digest = _select_index_digest(manifest)
    if not digest:
        raise RuntimeError(
            f"image index for '{ref}' has no usable linux manifest entry")
    repo = _repo_without_tag(ref)
    return _fetch_manifest(f"{repo}@{digest}")


def _is_disk_qcow2(name: str) -> bool:
    """True if *name* is a qcow2 under a ``disk/`` directory (containerDisk layout)."""
    norm = name.replace("\\", "/").lstrip("./")
    return norm.endswith(".qcow2") and ("/disk/" in "/" + norm)


def _blob_fetch_to_file(repo: str, digest: str, dest: Path) -> None:
    """Fetch a layer blob by digest to *dest* via ``oras blob fetch``."""
    _run_oras(
        ["oras", "blob", "fetch", f"{repo}@{digest}", "--output", str(dest)],
        action="blob fetch", ref=f"{repo}@{digest}")


def _extract_qcow2_from_layer(blob_path: Path, out_dir: Path) -> Optional[Path]:
    """Extract an embedded qcow2 from a single (compressed) layer tarball.

    Streams the tar (``r|*`` auto-detects gzip/bzip2/xz; zstd is unsupported and
    surfaces a clear error). Prefers a ``disk/*.qcow2`` (KubeVirt containerDisk
    convention) over any other ``*.qcow2``, and stops as soon as a ``disk/`` one
    is found. Only the basename is used for the output file, so a malicious
    member name cannot traverse outside *out_dir*.

    Returns the path to the extracted qcow2, or ``None`` if the layer has none.
    """
    chosen: Optional[Path] = None
    chosen_is_disk = False
    try:
        with open(blob_path, "rb") as raw, tarfile.open(fileobj=raw, mode="r|*") as tar:
            for member in tar:
                if not (member.isfile() and member.name.endswith(".qcow2")):
                    continue
                src = tar.extractfile(member)
                if src is None:
                    continue
                dest = out_dir / os.path.basename(member.name)
                with open(dest, "wb") as fh:
                    shutil.copyfileobj(src, fh, 1024 * 1024)
                is_disk = _is_disk_qcow2(member.name)
                if chosen is None or (is_disk and not chosen_is_disk):
                    if chosen is not None and chosen != dest:
                        chosen.unlink(missing_ok=True)  # drop the less-preferred one
                    chosen, chosen_is_disk = dest, is_disk
                elif dest != chosen:
                    dest.unlink(missing_ok=True)
                if chosen_is_disk:
                    break  # a disk/*.qcow2 is the best match; stop scanning
    except tarfile.ReadError as exc:
        raise RuntimeError(
            f"could not read OCI layer tarball '{blob_path.name}': {exc}. "
            "Layers compressed with zstd are not currently supported.") from exc
    return chosen


def _extract_embedded_qcow2(ref: str, manifest: dict, out_dir: Path) -> Optional[Path]:
    """Extract the qcow2 embedded in a container image's layers.

    Layers are scanned from topmost to bottom (so an upper layer's disk wins);
    the first layer that yields a qcow2 is used.
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
            qcow2 = _extract_qcow2_from_layer(blob_path, out_dir)
        finally:
            blob_path.unlink(missing_ok=True)
        if qcow2 is not None:
            return qcow2
    return None


# ── public interface ─────────────────────────────────────────────────────────


def pull_oci_image(image_ref: str, out_dir: str) -> str:
    """Pull an OCI image and return the local qcow2 path.

    Two source layouts are supported:

    * **boxman / oras artifact** — a qcow2 stored as a titled OCI layer
      (``oras push disk.qcow2``); fetched with ``oras pull``.
    * **container image / KubeVirt containerDisk** — a qcow2 embedded inside a
      container image's filesystem (conventionally ``/disk/*.qcow2``, e.g.
      ``quay.io/containerdisks/...``); the carrying layer is fetched and the
      qcow2 extracted from it.

    Args:
        image_ref: OCI reference, with or without an ``oci://`` scheme
            (e.g. ``oci://registry.example.com/repo:tag``).
        out_dir: Directory to pull the artifact into (created if needed).

    Returns:
        Absolute path to the qcow2 file.

    Raises:
        ValueError: If *image_ref* is empty.
        RuntimeError: If oras is missing, a registry call fails, or no qcow2
            can be obtained from the image.
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

    if _manifest_has_qcow2_title(manifest):
        # boxman/oras artifact: a titled qcow2 layer that `oras pull` extracts.
        _run_oras(["oras", "pull", ref, "-o", str(out)], action="pull", ref=ref)
        qcow2 = _find_qcow2(out)
        if qcow2 is None:
            raise RuntimeError(
                f"OCI image '{ref}' pulled to '{out}', but no qcow2 was found "
                "(expected 'disk.qcow2' or any '*.qcow2').")
        return str(qcow2)

    # Otherwise treat it as a container image (e.g. KubeVirt containerDisk) and
    # extract the qcow2 embedded in its layers.
    qcow2 = _extract_embedded_qcow2(ref, manifest, out)
    if qcow2 is None:
        raise RuntimeError(
            f"OCI image '{ref}' has no qcow2: it is neither a boxman artifact "
            "(a titled '*.qcow2' layer) nor a container image with an embedded "
            "'*.qcow2' (expected a KubeVirt-style containerDisk carrying "
            "'/disk/*.qcow2').")
    return str(qcow2)


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
                "boxman extracts the embedded /disk/*.qcow2 on pull)")

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
