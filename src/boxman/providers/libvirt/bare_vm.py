"""Create a bare VM (empty disk) for PXE network boot."""

from .direct_vm import DirectInstallVM


class BareVM(DirectInstallVM):
    """Create a libvirt VM with an empty disk and boot order ``network,hd``.

    The VM has no OS; it is intended to boot via PXE and have an OS installed
    by a provisioning server (e.g. Cobbler).
    """

    boot_order = "network,hd"

    def _describe(self) -> str:
        return f"bare PXE VM '{self.vm_name}'"
