from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Optional

from boxman.config_cache import BoxmanCache
from boxman.images.metadata import VmImageMetadata, load_vmimage_metadata


@dataclass(frozen=True, slots=True)
class ResolvedBaseImage:
    """Resolved representation of a cluster's `base_image`.

    For now Boxman only supports legacy libvirt cloning, where `base_image` is the
    name of an existing libvirt VM.

    In the future `base_image` may also be an OCI reference (e.g. ``oci://...``)
    that resolves to a locally cached qcow2 + metadata.
    """

    kind: str

    # legacy (libvirt clone) mode
    src_vm_name: Optional[str] = None

    # local qcow2 mode (future/OCI)
    qcow2_path: Optional[str] = None
    metadata_path: Optional[str] = None
    image_ref: Optional[str] = None

    # parsed metadata (vmimage.json)
    metadata: VmImageMetadata | None = None


def resolve_base_image(base_image: str, cache: BoxmanCache | None = None) -> ResolvedBaseImage:
    """Resolve a `base_image` string into a structured representation.

    Args:
        base_image: Value from config (cluster.base_image)
        cache: Boxman cache instance (reserved for future OCI cache usage)

    Returns:
        ResolvedBaseImage describing how the base image should be consumed.

    Raises:
        NotImplementedError: If `base_image` is an OCI reference.
        ValueError: If `base_image` is empty.
    """

    if not base_image or not str(base_image).strip():
        raise ValueError("base_image must be a non-empty string")

    base_image = str(base_image).strip()

    if base_image.startswith("oci://"):
        if cache is None:
            cache = BoxmanCache()

        image_ref = base_image[len("oci://"):]
        if not image_ref.strip():
            raise ValueError("OCI base_image must be of the form oci://<registry>/<repo>:<tag>")

        cache_dir = _oci_cache_dir(cache=cache, image_ref=image_ref)

        # idempotency: if we already have a qcow2 in cache_dir, skip pulling.
        qcow2_path, metadata_path = _find_pulled_oci_files(cache_dir)
        if qcow2_path is None:
            _oras_pull(image_ref=image_ref, out_dir=cache_dir)
            qcow2_path, metadata_path = _find_pulled_oci_files(cache_dir)

        if qcow2_path is None:
            raise RuntimeError(
                f"OCI image '{image_ref}' pulled to '{cache_dir}', but no qcow2 was found. "
                "Expected 'disk.qcow2' or any '*.qcow2'."
            )

        return ResolvedBaseImage(
            kind="local-qcow2",
            qcow2_path=str(qcow2_path),
            metadata_path=str(metadata_path) if metadata_path is not None else None,
            image_ref=image_ref,
            metadata=load_vmimage_metadata(str(metadata_path) if metadata_path is not None else None),
        )

    # Legacy behavior: treat as libvirt source VM name for `virt-clone`.
    _ = cache  # reserved for future use
    return ResolvedBaseImage(kind="libvirt-vm", src_vm_name=base_image)


def _oci_cache_dir(cache: BoxmanCache, image_ref: str) -> Path:
    """Return a stable cache directory for an OCI image reference."""

    # Use a short deterministic hash to avoid filesystem/path issues.
    digest = hashlib.sha256(image_ref.encode("utf-8")).hexdigest()[:20]
    # Preserve a mildly readable prefix for humans.
    safe_prefix = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in image_ref)[:60]
    path = Path(cache.images_cache_dir) / "oci" / f"{safe_prefix}__{digest}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _oras_pull(image_ref: str, out_dir: Path) -> None:
    """Pull an OCI artifact to a local directory using oras (no auth)."""

    cmd = ["oras", "pull", image_ref, "-o", str(out_dir)]
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
            "oras pull failed for '{ref}'.\n"
            "command: {cmd}\n"
            "exit code: {code}\n"
            "stdout:\n{stdout}\n"
            "stderr:\n{stderr}\n".format(
                ref=image_ref,
                cmd=" ".join(cmd),
                code=result.returncode,
                stdout=(result.stdout or "").strip(),
                stderr=(result.stderr or "").strip(),
            )
        )


def _find_pulled_oci_files(out_dir: Path) -> tuple[Optional[Path], Optional[Path]]:
    """Locate qcow2 + optional vmimage.json in a pulled oras output directory."""

    if not out_dir.exists():
        return None, None

    # Prefer disk.qcow2 if present.
    preferred = out_dir / "disk.qcow2"
    if preferred.is_file():
        qcow2 = preferred
    else:
        qcow2_candidates = sorted(out_dir.glob("*.qcow2"))
        qcow2 = qcow2_candidates[0] if qcow2_candidates else None

    metadata = out_dir / "vmimage.json"
    metadata_path = metadata if metadata.is_file() else None

    return qcow2, metadata_path
