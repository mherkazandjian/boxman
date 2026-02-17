from __future__ import annotations

from pathlib import Path

from boxman.config_cache import BoxmanCache
from boxman.images.resolver import ResolvedBaseImage, resolve_base_image


def format_image_inspect(resolved: ResolvedBaseImage, cache_dir: str | None) -> str:
    """Format the `boxman image inspect` output as plain text.

    Args:
        resolved: Output of `resolve_base_image()`.
        cache_dir: Cache directory for the image if known.

    Returns:
        Human-readable, plain text output.
    """

    lines: list[str] = []

    if resolved.kind == "libvirt-vm":
        lines.append("kind: libvirt-vm")
        lines.append(f"src_vm_name: {resolved.src_vm_name}")
        lines.append("note: base_image is treated as a libvirt source VM name")
        return "\n".join(lines) + "\n"

    if resolved.kind == "local-qcow2":
        lines.append("kind: local-qcow2")
        if resolved.image_ref:
            lines.append(f"image_ref: {resolved.image_ref}")
        if cache_dir:
            lines.append(f"cache_dir: {cache_dir}")
        if resolved.qcow2_path:
            lines.append(f"qcow2_path: {resolved.qcow2_path}")
        if resolved.metadata_path:
            lines.append(f"metadata_path: {resolved.metadata_path}")

        md = resolved.metadata
        if md is None:
            lines.append("metadata: <none>")
        else:
            lines.append("metadata:")
            lines.append(f"  firmware: {md.firmware}")
            lines.append(f"  machine: {md.machine}")
            lines.append(f"  disk_bus: {md.disk_bus}")
            lines.append(f"  net_model: {md.net_model}")

        return "\n".join(lines) + "\n"

    lines.append(f"kind: {resolved.kind}")
    if cache_dir:
        lines.append(f"cache_dir: {cache_dir}")
    return "\n".join(lines) + "\n"


def image_inspect(manager, cli_args) -> None:
    """Entry point for `boxman image inspect` CLI."""

    cache = getattr(manager, "cache", None)

    # Keep behavior CLI-only; do not modify provisioning/providers.
    resolved = resolve_base_image(cli_args.base_image_ref, cache=cache)

    cache_dir: str | None = None
    if resolved.kind == "local-qcow2":
        # Derive cache dir from the qcow2 path (preferred) or metadata path.
        p: Path | None = None
        if resolved.qcow2_path:
            p = Path(resolved.qcow2_path)
        elif resolved.metadata_path:
            p = Path(resolved.metadata_path)
        if p is not None:
            cache_dir = str(p.parent)

    print(format_image_inspect(resolved=resolved, cache_dir=cache_dir), end="")
