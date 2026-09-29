"""Shared base for VMs created directly via ``virt-install`` (no template clone).

Both PXE network-boot VMs (:class:`~boxman.providers.libvirt.bare_vm.BareVM`)
and ISO-install VMs (:class:`~boxman.providers.libvirt.iso_boot_vm.IsoBootVM`)
create an empty boot disk and run ``virt-install`` to define the domain; they
differ only in their install media and firmware boot order. The common logic
lives here so a fix (path expansion, network namespacing, MAC pinning,
disk-size units, …) applies to both.
"""

import os
import shlex
from typing import Any

from boxman import log
from boxman.exceptions import ProvisionError
from boxman.utils.shell import run as _shell_run

from .commands import VirshCommand, VirtInstallCommand
from .disk import create_image_exclusive, libvirt_disk_source

#: What a refused boot disk tells the operator to do.
BOOT_DISK_REMEDY = ("If it is stale, remove or rename it and run the "
                    "command again.")

#: ``{path: descriptor}`` of the boot disks this process made, each held
#: open. A clone is retried in the same process
#: (``_clone_with_retry``), and an attempt whose virt-install failed leaves
#: its image behind: the next attempt uses that image rather than refusing
#: it as someone else's. The open descriptor is what makes that safe: it
#: keeps the image's inode from being freed, so its number cannot be reused
#: by a file that replaced it (ext4 reuses a freed inode number at once).
_boot_disks_made: dict[str, int] = {}


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

    @staticmethod
    def _network_spec(entry: Any) -> dict[str, Any] | None:
        """Normalise one ``networks:`` / ``_resolved_networks`` entry.

        Accepts a bare network name or a ``{name, mac}`` mapping and returns
        ``{'name': str, 'mac': str | None}``; ``None`` for an unusable entry.
        """
        if isinstance(entry, str):
            return {"name": entry, "mac": None} if entry else None
        if isinstance(entry, dict) and entry.get("name"):
            mac = entry.get("mac")
            return {"name": entry["name"], "mac": str(mac) if mac else None}
        return None

    def _network_specs(self) -> list[dict[str, Any]]:
        """Networks to attach at ``virt-install`` time, as ``{name, mac}`` dicts.

        Prefers ``_resolved_networks`` (namespaced by the manager, optionally
        carrying a pinned ``mac``); falls back to the raw ``networks[]`` entries,
        and to libvirt's ``default`` network when nothing is declared.
        """
        for source in (self.info.get("_resolved_networks"),
                       self.info.get("networks")):
            if not source:
                # Nothing declared here. `_resolve_iso_config` writes
                # `_resolved_networks: []` even when no `networks:` was given,
                # so an empty list has to mean "ask the next source", not
                # "declared and unresolvable".
                continue
            specs = [s for s in map(self._network_spec, source or []) if s]
            if not specs:
                # Declared, and nothing survived. Falling through to `default`
                # here put a VM whose only entry was blank onto libvirt's
                # shared default network with nothing reported -- the libvirt
                # twin of #164 NET-C1. Refuse what can be proven wrong; never
                # silently drop a reference (#171 A4).
                raise ProvisionError(
                    f"vm {self.vm_name}: 'networks:' was declared but no entry "
                    f"names a network ({source!r}). Remove the key to use "
                    f"libvirt's default network, or name one.")
            return specs
        return [{"name": "default", "mac": None}]

    def _networks(self) -> list[str]:
        """Fully-qualified libvirt network names to attach
        (see :meth:`_network_specs`)."""
        return [spec["name"] for spec in self._network_specs()]

    # ── creation ──────────────────────────────────────────────────────────
    def _make_boot_disk(self, disk_path: str, disk_size: str) -> bool:
        """
        Create the empty boot disk, never over anything already at its path
        (:func:`~boxman.providers.libvirt.disk.create_image_exclusive`,
        #215) -- except the image an earlier attempt of this same process
        made there (:data:`_boot_disks_made`).

        Returns:
            True when the disk is in place, False when ``qemu-img`` failed.

        Raises:
            DiskPathOccupiedError: something else is at *disk_path*.
            ProvisionError: whether it is cannot be told, or the image could
                not be put in place.
        """
        path = libvirt_disk_source(disk_path)
        pinned = _boot_disks_made.get(path)
        if pinned is not None:
            try:
                ours, there = os.fstat(pinned), os.lstat(path)
            except OSError:
                pass
            else:
                if (there.st_dev, there.st_ino) == (ours.st_dev, ours.st_ino):
                    self.logger.info(
                        f"using the boot disk {path} an earlier attempt made")
                    return True

        def run(cmd: str):
            return _shell_run(self.virsh._wrap_for_runtime(cmd),
                              hide=True, warn=True)

        made = create_image_exclusive(disk_path, disk_size, "qcow2", run=run,
                                      remedy=BOOT_DISK_REMEDY)
        if made is None:
            return False
        self._pin(path, made)
        return True

    @staticmethod
    def _pin(path: str, made: tuple[int, int]) -> None:
        """Hold the image just made at *path* open (:data:`_boot_disks_made`),
        if *path* still names it. Failing to only costs a retry the image."""
        try:
            # read-only: this process made the file, so it may read it (some
            # Python builds lack O_PATH)
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != made:
            os.close(fd)
            return
        earlier = _boot_disks_made.pop(path, None)
        if earlier is not None:
            os.close(earlier)
        _boot_disks_made[path] = fd

    def create(self) -> bool:
        """Create the VM (empty boot disk + ``virt-install`` define)."""
        # Resolved first, and deliberately before qemu-img: an unusable
        # `networks:` used to be discovered after the boot disk had already
        # been written, leaving a stray image behind on a refusal (#171 A4).
        network_specs = self._network_specs()
        # absolute, so qemu-img and virt-install name the same file wherever
        # they run (the docker-compose runtime's container has another cwd)
        disk_path = libvirt_disk_source(
            os.path.join(self.workdir, f"{self.vm_name}.qcow2"))
        disk_size = self._boot_disk_size()
        # `or` (not .get default) so an explicit null in YAML still falls back
        memory = self.info.get("memory") or 2048
        vcpus = self.info.get("vcpus") or 2

        if not self._make_boot_disk(disk_path, disk_size):
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
            f"--disk=path={shlex.quote(disk_path)},format=qcow2,driver.type=qcow2,bus=virtio,discard=unmap")
        for spec in network_specs:
            net_arg = f"--network=network={spec['name']},model=virtio"
            if spec["mac"]:
                net_arg += f",mac={spec['mac']}"
            parts.append(net_arg)
        parts.extend(self._media_args())
        parts.append(f"--boot={self.boot_order}")
        parts.append("--os-variant=detect=on,require=off")
        parts.append("--graphics=vnc")
        parts.append("--noautoconsole")
        parts.append("--wait=0")
        for extra in self.info.get("virt_install_extra_args", []):
            parts.append(extra)

        cmd = " ".join(parts)
        cmd = self.virt_install._wrap_for_runtime(cmd)
        self.logger.info(f"creating {self._describe()}: {cmd}")
        result = _shell_run(cmd, hide=True, warn=True)
        if not result.ok:
            self.logger.error(f"virt-install failed: {result.stderr}")
            return False

        self.logger.info(f"{self._describe()} created")
        return True
