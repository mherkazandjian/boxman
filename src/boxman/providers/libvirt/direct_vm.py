"""Shared base for VMs created directly via ``virt-install`` (no template clone).

Both PXE network-boot VMs (:class:`~boxman.providers.libvirt.bare_vm.BareVM`)
and ISO-install VMs (:class:`~boxman.providers.libvirt.iso_boot_vm.IsoBootVM`)
create an empty boot disk and run ``virt-install`` to define the domain; they
differ only in their install media and firmware boot order. The common logic
lives here so a fix (path expansion, network namespacing, disk-size units, …)
applies to both.
"""

import os
import shlex
from typing import Any

from boxman import log
from boxman.utils.shell import run as _shell_run

from .commands import VirshCommand, VirtInstallCommand


def normalize_disk_size(value: Any, default: str = "20G") -> str:
    """Normalize a boot-disk size config value to a ``qemu-img`` size string.

    Accepts an int/float (interpreted as GiB) or a string with an optional unit
    suffix (``'50G'``, ``'51200M'``); a bare numeric string is treated as GiB.
    Empty / ``None`` / non-numeric falls back to *default*.
    """
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return f"{int(value)}G"
    s = str(value).strip()
    if not s:
        return default
    # already carries a unit suffix (G/M/K/T/P, possibly with 'iB') -> use as-is
    return s if s[-1].isalpha() else f"{s}G"


class DirectInstallVM:
    """Create a libvirt VM with an empty boot disk via ``virt-install``.

    Subclasses set :attr:`boot_order` (the ``--boot`` value) and may inject
    install media through :meth:`_media_args` (e.g. ``--cdrom``).
    """

    #: virt-install ``--boot`` value (firmware boot order)
    boot_order = "hd"

    def __init__(
        self,
        vm_name: str,
        info: dict[str, Any],
        provider_config: dict[str, Any],
        workdir: str,
    ):
        self.vm_name = vm_name
        self.info = info
        self.provider_config = provider_config
        self.workdir = workdir
        self.logger = log
        self.virsh = VirshCommand(provider_config)
        self.virt_install = VirtInstallCommand(provider_config=provider_config)

    # ── subclass hooks ────────────────────────────────────────────────────
    def _media_args(self) -> list[str]:
        """Extra ``virt-install`` args for install media (overridden)."""
        return []

    def _describe(self) -> str:
        return f"VM '{self.vm_name}'"

    # ── config helpers ────────────────────────────────────────────────────
    def _boot_disk_size(self) -> str:
        """Boot-disk size as a ``qemu-img`` size string.

        Uses the ``disk_size`` field (template convention, unit-suffixed) for
        the primary disk; any ``disks:`` entries are *additional* disks created
        later by the configure step (in MiB), exactly as for cloned VMs.
        """
        return normalize_disk_size(self.info.get("disk_size"))

    def _networks(self) -> list[str]:
        """Fully-qualified libvirt network names to attach.

        Prefers ``_resolved_networks`` (namespaced by the manager); falls back
        to the raw ``networks[].name`` (or ``default``) when unresolved.
        """
        resolved = self.info.get("_resolved_networks")
        if resolved:
            return list(resolved)
        networks = self.info.get("networks") or []
        names = [
            n["name"] for n in networks
            if isinstance(n, dict) and n.get("name")
        ]
        return names or ["default"]

    # ── creation ──────────────────────────────────────────────────────────
    def create(self) -> bool:
        """Create the VM (empty boot disk + ``virt-install`` define)."""
        disk_path = os.path.expanduser(
            os.path.join(self.workdir, f"{self.vm_name}.qcow2"))
        disk_size = self._boot_disk_size()
        # `or` (not .get default) so an explicit null in YAML still falls back
        memory = self.info.get("memory") or 2048
        vcpus = self.info.get("vcpus") or 2

        qemu_img_cmd = f'qemu-img create -f qcow2 {shlex.quote(disk_path)} {disk_size}'
        qemu_img_cmd = self.virsh._wrap_for_runtime(qemu_img_cmd)
        result = _shell_run(qemu_img_cmd, hide=True, warn=True)
        if not result.ok:
            self.logger.error(f"qemu-img create failed: {result.stderr}")
            return False

        parts = []
        if self.virt_install.use_sudo:
            parts.append("sudo")
        parts.append(self.virt_install.command_path)
        parts.append(f"--connect={self.virt_install.uri}")
        parts.append(f"--name={self.vm_name}")
        parts.append(f"--memory={memory}")
        parts.append(f"--vcpus={vcpus}")
        parts.append(
            f"--disk=path={shlex.quote(disk_path)},format=qcow2,bus=virtio,discard=unmap")
        for net in self._networks():
            parts.append(f"--network=network={net},model=virtio")
        parts.extend(self._media_args())
        parts.append(f"--boot={self.boot_order}")
        parts.append("--os-variant=detect=on,require=off")
        parts.append("--graphics=vnc")
        parts.append("--noautoconsole")
        parts.append("--wait=0")

        cmd = " ".join(parts)
        cmd = self.virt_install._wrap_for_runtime(cmd)
        self.logger.info(f"creating {self._describe()}: {cmd}")
        result = _shell_run(cmd, hide=True, warn=True)
        if not result.ok:
            self.logger.error(f"virt-install failed: {result.stderr}")
            return False

        self.logger.info(f"{self._describe()} created")
        return True
