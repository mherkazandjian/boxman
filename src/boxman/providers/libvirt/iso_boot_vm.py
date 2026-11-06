"""Create a VM that boots from an install ISO (e.g. Talos Linux)."""

import os
import shlex
import stat
from typing import Any

from boxman.exceptions import ProvisionError

from .cdrom import CDROMManager
from .commands import virt_install_path
from .direct_vm import DirectInstallVM
from .install_media import media_path


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

    There is no install phase (``--install no_install=yes``): that order is
    the one the guest boots with from the start, and the installer's reboot
    is a reboot. The ISO stays in the definition.

    Every further ``cdroms:`` entry is given to virt-install too, so the
    installer sees it on its first boot (a driver ISO, an answer file).
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
        # spelt as the manager resolves every cdroms: source, so that the
        # boot ISO is told from the other entries by the one path
        self.iso_path = media_path(iso_path)

    def _extra_cdroms(self) -> list[dict[str, Any]]:
        """The resolved ``cdroms:`` entries other than the boot ISO."""
        extra = []
        for entry in self.info.get("cdroms") or []:
            source = entry.get("source") if isinstance(entry, dict) else None
            if source and media_path(source) == self.iso_path:
                continue
            if not source:
                raise ProvisionError(
                    f"vm {self.vm_name}: cdroms entry {entry!r} has no "
                    f"resolved source to give virt-install")
            extra.append(entry)
        return extra

    def _media_args(self) -> list[str]:
        # Every cdrom goes to virt-install, not only the boot ISO. Attached
        # by the configure step instead, once virt-install had started the
        # installer, a SATA/IDE drive reached the persistent definition only
        # (libvirt cannot hot-plug one), and an installer that needs a second
        # medium -- drivers, an answer file -- never saw it.
        #
        # `--cdrom` alone makes virt-install run an install phase: a first
        # definition that boots the CD-ROM only and has on_reboot=destroy.
        # boxman's --wait=0 left nothing to restart it, so the guest stayed
        # off after an installer's first reboot (G2). `no_install=yes` gives
        # the one definition, with a normal reboot. `--cdrom` stays because
        # it is the medium virt-install detects the OS from; a CD-ROM
        # `--disk` would not be read.
        #
        # Each path goes through virt_install_path: shell quoting alone let
        # a comma in a `cdroms:` source end virt-install's `path=` value
        # there, and a quote in any of them reach the domain as another
        # file. `--cdrom` is a whole value, which virt-install does not
        # split.
        iso = virt_install_path(self._medium(self.iso_path, "the boot ISO"),
                                self.vm_name, sub_option=False)
        args = [f"--cdrom={shlex.quote(iso)}",
                "--install=no_install=yes"]
        for entry in self._extra_cdroms():
            source = self._medium(
                entry["source"],
                f"the ISO of cdrom '{entry.get('name') or entry['source']}'")
            disk = (f"path={virt_install_path(source, self.vm_name)},"
                    f"device=cdrom,readonly=on")
            target = entry.get("target")
            if target:
                disk += (f",target.dev={target},"
                         f"target.bus={CDROMManager._bus_for_target(target)}")
            args.append(f"--disk={shlex.quote(disk)}")
        return args

    def _medium(self, source: str, what: str) -> str:
        """
        *source* as virt-install is given it -- absolute, with ``~``
        expanded (:func:`media_path`) -- once something a CD-ROM can read
        is there.

        Asked before the boot disk is made, of the boot ISO as of the
        others. The boot ISO went to virt-install unasked, and one that was
        not there failed it only once the disk was written; the disk
        outlived the failure (#294).

        A path that cannot be looked up for another reason -- a directory
        this user may not search -- is left for virt-install to open: it
        may run as root.

        Raises:
            ProvisionError: nothing is there, or a directory is; naming the
                VM, *what*, and the path.
        """
        path = media_path(source)
        try:
            st = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            raise ProvisionError(
                f"vm {self.vm_name}: {what} does not exist: {path}. Correct "
                f"its source under the VM's cdroms: and run the command "
                f"again; nothing was made for the VM.") from None
        except OSError:
            return path
        if stat.S_ISDIR(st.st_mode):
            raise ProvisionError(
                f"vm {self.vm_name}: {what} is a directory, not an ISO file: "
                f"{path}. Correct its source under the VM's cdroms: and run "
                f"the command again; nothing was made for the VM.")
        return path

    def _describe(self) -> str:
        return f"ISO-boot VM '{self.vm_name}'"
