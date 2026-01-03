from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Optional


@dataclass(frozen=True, slots=True)
class VmImageMetadata:
    """VM image metadata v1.

    This metadata is shipped alongside the qcow2 blob as `vmimage.json`.

    Fields are intentionally small and provider-agnostic; Boxman will later use
    them to configure VM defaults (firmware, disk bus, NIC model, etc.).
    """

    firmware: str = "uefi"  # "uefi" or "bios"
    machine: Optional[str] = None
    disk_bus: str = "virtio"
    net_model: str = "virtio"

    # Display / informational
    name: Optional[str] = None
    version: Optional[str] = None
    arch: Optional[str] = None


def load_vmimage_metadata(path: str | None) -> VmImageMetadata:
    """Load `vmimage.json` metadata.

    If `path` is None or the file doesn't exist, defaults are returned.

    Raises:
        ValueError: JSON invalid (includes filename/context).
    """

    if path is None:
        return VmImageMetadata()

    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return VmImageMetadata()

    try:
        with open(path, "r") as f:
            raw = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid vmimage.json in '{path}': {exc}") from exc

    if raw is None:
        return VmImageMetadata()

    if not isinstance(raw, dict):
        raise ValueError(f"Invalid vmimage.json in '{path}': expected JSON object")

    # Only accept known keys; unknown keys are ignored for forward-compat.
    return VmImageMetadata(
        # NOTE: don't use VmImageMetadata.firmware here.
        # With dataclasses, the class attribute is a Field descriptor and not
        # the default value. Use the literal defaults instead.
        firmware=str(raw.get("firmware", "uefi")),
        machine=raw.get("machine"),
        disk_bus=str(raw.get("disk_bus", "virtio")),
        net_model=str(raw.get("net_model", "virtio")),
        name=raw.get("name"),
        version=raw.get("version"),
        arch=raw.get("arch"),
    )
