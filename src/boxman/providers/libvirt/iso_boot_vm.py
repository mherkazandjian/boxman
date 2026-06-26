"""Create a VM that boots from an install ISO (e.g. Talos Linux)."""

import shlex
from typing import Any

from .direct_vm import DirectInstallVM


class IsoBootVM(DirectInstallVM):
    """Create a libvirt VM with an empty disk and an install ISO attached.

    Intended for OSes that live-boot and self-install from an ISO (e.g. Talos
    Linux via Omni). The ``iso_path`` must already be resolved to a local file
    by the caller.

    The firmware boot order is ``hd,cdrom`` (disk first, falling through to the
    CDROM): on the first boot the empty disk is not bootable so the VM boots the
    ISO and installs to disk; after the OS reboots, the now-bootable disk is
    used instead of re-running the installer. (Using ``cdrom,hd`` would loop
    back into the installer on every reboot.)
    """

    boot_order = "hd,cdrom"

    def __init__(
        self,
        vm_name: str,
        info: dict[str, Any],
        provider_config: dict[str, Any],
        workdir: str,
        iso_path: str,
    ):
        super().__init__(vm_name, info, provider_config, workdir)
        self.iso_path = iso_path

    def _media_args(self) -> list[str]:
        return [f"--cdrom={shlex.quote(self.iso_path)}"]

    def _describe(self) -> str:
        return f"ISO-boot VM '{self.vm_name}'"
