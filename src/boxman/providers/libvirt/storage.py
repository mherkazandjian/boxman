"""
Storage management for the libvirt provider.

Wraps ``qemu-img info / measure / convert``, ``virt-sparsify``, and the
``virsh`` verbs needed to inspect and reclaim qcow2 disk space:

- :meth:`StorageManager.disk_info` / :meth:`disk_chain` / :meth:`disk_measure`
  for read-only inspection.
- :meth:`fstrim_guest` (via ``virsh domfstrim`` over the qemu guest agent)
  for OS-level reclaim.
- :meth:`compact_disk` for host-level reclaim — picks ``virt-sparsify``
  (preserves snapshot chain) or ``qemu-img convert`` (flattens chain).

Used by :class:`boxman.manager.BoxmanManager` via the ``boxman storage``
CLI subcommands (``df``, ``trim``, ``compact``, ``optimize``).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from xml.etree import ElementTree as ET

from boxman import log

from .commands import LibVirtCommandBase, VirshCommand


def vm_disk_paths(
    workdir: str,
    vm_name: str,
    vm_info: dict[str, Any] | None = None,
) -> list[str]:
    """
    Return every qcow2 path that belongs to *vm_name* under *workdir*.

    Mirrors the file naming used by
    :func:`boxman.providers.libvirt.disk_cleanup.remove_vm_disks`:
    boot disk at ``<workdir>/<vm_name>.qcow2`` and one extra per entry in
    ``vm_info['disks']`` named ``<workdir>/<vm_name>_<disk['name']>.qcow2``.
    """
    workdir = os.path.expanduser(workdir)
    paths = [os.path.join(workdir, f'{vm_name}.qcow2')]
    if vm_info:
        for disk in vm_info.get('disks', []) or []:
            name = disk.get('name') if isinstance(disk, dict) else None
            if name:
                paths.append(os.path.join(workdir, f'{vm_name}_{name}.qcow2'))
    return paths


class StorageManager:
    """Inspect and reclaim qcow2 disk space for libvirt-backed VMs."""

    def __init__(self, provider_config: dict[str, Any] | None = None):
        self.provider_config = provider_config or {}
        self.virsh = VirshCommand(provider_config=provider_config)
        self.cmd = LibVirtCommandBase(provider_config=provider_config)
        self.uri = self.provider_config.get('uri', 'qemu:///system')
        self.use_sudo = self.provider_config.get('use_sudo', False)
        self.logger = log

    # ── inspection ──────────────────────────────────────────────────────

    def disk_info(self, disk_path: str) -> dict[str, Any]:
        """Parsed ``qemu-img info --output=json`` for *disk_path* (``{}`` on error)."""
        result = self.cmd.execute_shell(
            f"qemu-img info --output=json {disk_path}", warn=True)
        if not result.ok:
            self.logger.warning(
                f"qemu-img info failed for {disk_path}: {result.stderr.strip()}")
            return {}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.logger.warning(
                f"qemu-img info json parse failed for {disk_path}: {exc}")
            return {}

    def disk_chain(self, disk_path: str) -> list[dict[str, Any]]:
        """Parsed ``qemu-img info --backing-chain --output=json`` (head first)."""
        result = self.cmd.execute_shell(
            f"qemu-img info --backing-chain --output=json {disk_path}", warn=True)
        if not result.ok:
            return []
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return data
        return [data]

    def disk_measure(self, disk_path: str) -> dict[str, Any]:
        """``qemu-img measure --output=json`` → ``{required, fully-allocated}`` bytes."""
        result = self.cmd.execute_shell(
            f"qemu-img measure --output=json -O qcow2 {disk_path}", warn=True)
        if not result.ok:
            return {}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}

    def count_snapshots(self, vm_name: str) -> int:
        result = self.virsh.execute("snapshot-list", vm_name, "--name", warn=True)
        if not result.ok:
            return 0
        return len([line for line in result.stdout.splitlines() if line.strip()])

    def has_discard_unmap(self, vm_name: str) -> bool:
        """True if at least one ``<disk device='disk'>`` has ``discard='unmap'``."""
        result = self.virsh.execute("dumpxml", vm_name, warn=True)
        if not result.ok:
            return False
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError:
            return False
        for driver in root.findall(".//disk[@device='disk']/driver"):
            if driver.get('discard') == 'unmap':
                return True
        return False

    def is_running(self, vm_name: str) -> bool:
        result = self.virsh.execute("domstate", vm_name, warn=True)
        if not result.ok:
            return False
        return "running" in result.stdout

    def is_libguestfs_available(self) -> bool:
        """Whether ``virt-sparsify`` is reachable in the configured runtime."""
        result = self.cmd.execute_shell(
            "command -v virt-sparsify", warn=True, hide=True)
        return result.ok

    def snapshot_memory_files(self, workdir: str, vm_name: str) -> list[str]:
        """
        Return paths of memory files for *vm_name* under *workdir*.

        Includes both ``<vm>_snapshot_*.raw`` (uncompressed) and
        ``<vm>_snapshot_*.raw.zst`` (compressed via ``snapshot take
        --compress-memory`` or ``storage compress-snapshots``).
        """
        import glob
        base = os.path.join(os.path.expanduser(workdir),
                            f'{vm_name}_snapshot_*.raw')
        return [p for p in (glob.glob(base) + glob.glob(f"{base}.zst"))
                if os.path.isfile(p)]

    # ── guest-side reclaim ──────────────────────────────────────────────

    def fstrim_guest(self, vm_name: str) -> bool:
        """Run ``fstrim`` inside the guest via ``virsh domfstrim`` (qemu guest agent)."""
        result = self.virsh.execute("domfstrim", vm_name, warn=True)
        if result.ok:
            self.logger.info(f"fstrim ok: {vm_name}")
            return True
        err = (result.stderr or "").strip().lower()
        if "agent" in err or "not responding" in err or "not supported" in err:
            self.logger.error(
                f"fstrim failed: {vm_name} — qemu-guest-agent not responsive. "
                f"install it in the guest (`apt/dnf install qemu-guest-agent`) and reboot.")
        else:
            self.logger.error(
                f"fstrim failed: {vm_name} — {result.stderr.strip()}")
        return False

    # ── vm state transitions ────────────────────────────────────────────

    def shutdown_and_wait(self, vm_name: str, timeout_s: int = 120) -> bool:
        if not self.is_running(vm_name):
            return True
        self.logger.info(f"shutting down vm {vm_name} (timeout {timeout_s}s)")
        result = self.virsh.execute("shutdown", vm_name, warn=True)
        if not result.ok:
            self.logger.error(
                f"virsh shutdown failed for {vm_name}: {result.stderr.strip()}")
            return False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if not self.is_running(vm_name):
                return True
            time.sleep(2)
        self.logger.warning(
            f"vm {vm_name} did not shutdown within {timeout_s}s — issuing destroy")
        destroy = self.virsh.execute("destroy", vm_name, warn=True)
        return destroy.ok

    def start(self, vm_name: str) -> bool:
        result = self.virsh.execute("start", vm_name, warn=True)
        if not result.ok:
            self.logger.error(
                f"virsh start failed for {vm_name}: {result.stderr.strip()}")
            return False
        return True

    # ── host-side reclaim ───────────────────────────────────────────────

    def sparsify_in_place(self, disk_path: str) -> bool:
        cmd = f"virt-sparsify --in-place {disk_path}"
        self.logger.info(f"sparsifying {disk_path}")
        result = self.cmd.execute_shell(cmd, warn=True, hide=False)
        if not result.ok:
            self.logger.error(
                f"virt-sparsify failed for {disk_path}: {result.stderr.strip()}")
            return False
        return True

    def convert(self, disk_path: str, compress: bool = False) -> bool:
        """Rewrite *disk_path* via ``qemu-img convert`` — flattens snapshot chain."""
        tmp_path = f"{disk_path}.compact-tmp"
        compress_flag = "-c " if compress else ""
        cmd = (f"qemu-img convert {compress_flag}-O qcow2 {disk_path} {tmp_path} "
               f"&& mv {tmp_path} {disk_path}")
        self.logger.info(f"converting {disk_path} (compress={compress})")
        result = self.cmd.execute_shell(cmd, warn=True, hide=False)
        if not result.ok:
            self.logger.error(
                f"qemu-img convert failed for {disk_path}: {result.stderr.strip()}")
            self.cmd.execute_shell(f"rm -f {tmp_path}", warn=True, hide=True)
            return False
        return True

    def compact_disk(
        self,
        disk_path: str,
        method: str = 'auto',
        has_snapshots: bool = False,
        drop_snapshots: bool = False,
    ) -> bool:
        """
        Compact a single qcow2 file.

        Method resolution:
          - ``auto`` → ``sparsify`` if *has_snapshots* else ``convert``.
          - ``convert`` / ``convert-compressed`` flatten the snapshot chain
            and are refused unless *drop_snapshots* is True when snapshots
            exist.
        """
        resolved = method
        if resolved == 'auto':
            resolved = 'sparsify' if has_snapshots else 'convert'

        flattens_chain = resolved in ('convert', 'convert-compressed')
        if flattens_chain and has_snapshots and not drop_snapshots:
            self.logger.error(
                f"refusing {resolved} on {disk_path}: snapshots present and "
                f"--drop-snapshots not given")
            return False

        if resolved == 'sparsify':
            if not self.is_libguestfs_available():
                self.logger.error(
                    "virt-sparsify not found in the runtime — install "
                    "guestfs-tools / libguestfs-tools, or pass --method convert")
                return False
            return self.sparsify_in_place(disk_path)
        if resolved == 'convert':
            return self.convert(disk_path, compress=False)
        if resolved == 'convert-compressed':
            return self.convert(disk_path, compress=True)
        self.logger.error(f"unknown compact method: {method}")
        return False
