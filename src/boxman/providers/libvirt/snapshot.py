#!/usr/bin/env python3
"""
Snapshot module for libvirt provider.
This module provides functionality to manage VM snapshots using command-line tools.
"""

import os
import time
from typing import Any
from xml.etree import ElementTree as ET

from boxman import log

from .commands import VirshCommand


class SnapshotManager:
    """
    Class to manage snapshots of VMs in libvirt using virsh commands.
    """

    def __init__(self, provider_config: dict[str, Any] | None = None):
        """
        Initialize the snapshot manager.

        Args:
            provider_config: Configuration for the libvirt provider
        """
        #: VirshCommand: Command executor for virsh
        self.virsh = VirshCommand(provider_config=provider_config)

        #: str: URI for libvirt connection
        self.uri = provider_config.get('uri', 'qemu:///system') if provider_config else 'qemu:///system'

        #: bool: Whether to use sudo
        self.use_sudo = provider_config.get('use_sudo', False) if provider_config else False

        #: logging.Logger: Logger instance
        self.logger = log

    def _flatten_cdrom_overlays(self, vm_name: str) -> None:
        """
        Switch any cdrom that is using a qcow2 overlay of a raw ISO back to
        pointing at the raw ISO directly, using ``virsh change-media --live``.

        Background
        ----------
        ``snapshot-create-as --memspec`` saves live VM memory state.  That
        memory state records the exact disk paths that QEMU has open at that
        instant.  If a cdrom is currently using a qcow2 overlay whose backing
        store is a raw ISO (e.g. seed.1772465824 → seed.iso), the memory state
        will reference the qcow2 overlay.  On ``snapshot-revert`` libvirt
        restores the memory and QEMU calls ``cont``; at that point it tries to
        validate the qcow2 backing chain, finds a raw file where it expects
        qcow2, and aborts with "Image is not in qcow2 format".

        By switching cdroms back to the raw ISO *before* the snapshot, the
        saved memory state references the raw file directly.  Combined with
        ``--diskspec target,snapshot=no`` (which prevents any new overlay from
        being created), every future revert will open the raw ISO cleanly.
        """
        result = self.virsh.execute("dumpxml", vm_name, warn=True)
        if not result.ok:
            return

        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError:
            return

        for disk in root.findall(".//disk[@device='cdrom']"):
            target_elem = disk.find("target")
            source_elem = disk.find("source")
            if target_elem is None or source_elem is None:
                continue

            target = target_elem.get("dev")
            source_file = source_elem.get("file")
            if not target or not source_file:
                continue

            # Check whether this cdrom is a qcow2 overlay on a raw backing store.
            # The backingStore element directly inside the disk element is the
            # immediate backing layer of the active source.
            backing_store = disk.find("backingStore")
            if backing_store is None:
                continue

            backing_format = backing_store.find("format")
            backing_source = backing_store.find("source")
            if backing_format is None or backing_source is None:
                continue

            if backing_format.get("type") != "raw":
                continue

            raw_iso = backing_source.get("file")
            if not raw_iso:
                continue

            # The cdrom is currently: source_file (qcow2) → raw_iso (raw).
            # Switch it to point at raw_iso directly so the memory snapshot
            # will reference the raw ISO, not the qcow2 overlay.
            self.logger.info(
                f"flattening cdrom {target} on {vm_name}: "
                f"'{source_file}' -> '{raw_iso}'")
            change_result = self.virsh.execute(
                "change-media", vm_name, target, raw_iso,
                "--live", "--config", "--force",
                warn=True)
            if not change_result.ok:
                self.logger.warning(
                    f"failed to switch cdrom {target} on {vm_name} "
                    f"back to raw ISO '{raw_iso}': {change_result.stderr}")

    def _cdrom_diskspec_args(self, vm_name: str) -> list[str]:
        """
        Return --diskspec args that exclude cdrom devices from a snapshot.

        Even after _flatten_cdrom_overlays switches the cdrom to the raw ISO,
        snapshot-create-as would still create a new qcow2 overlay for it,
        reintroducing the problem for the next revert.  Passing
        --diskspec <target>,snapshot=no prevents that overlay from being
        created so the cdrom stays at the raw ISO permanently.
        """
        result = self.virsh.execute("domblklist", vm_name, "--details", warn=True)
        if not result.ok:
            return []
        args = []
        for line in result.stdout.splitlines():
            parts = line.split()
            # domblklist --details columns: Type  Device  Target  Source
            if len(parts) >= 3 and parts[1] == 'cdrom':
                args.append(f"--diskspec {parts[2]},snapshot=no")
        return args

    def create_snapshot(self,
                        vm_name: str,
                        vm_dir: str,
                        snapshot_name: str,
                        description: str) -> bool:
        """
        Create a snapshot of a VM.

        Args:
            vm_name: Name of the VM
            vm_dir: Directory where the VM is located
            snapshot_name: Name for the snapshot
            description: Description for the snapshot

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Before saving memory state, ensure cdroms point directly at
            # their raw ISO backing files (not qcow2 overlays of them).
            # This prevents snapshot-revert from failing with
            # "Image is not in qcow2 format" when QEMU validates the chain.
            self._flatten_cdrom_overlays(vm_name)

            snap_fname = f"{vm_name}_snapshot_{snapshot_name}.raw"
            # Exclude cdroms from the snapshot so no new qcow2 overlay is
            # created for them (they stay at the raw ISO after this call).
            cdrom_args = self._cdrom_diskspec_args(vm_name)
            result = self.virsh.execute(
                "snapshot-create-as",
                f"--domain {vm_name}",
                f"--name {snapshot_name}",
                f"--description '{description}'",
                "--atomic",
                f"--memspec={os.path.join(vm_dir, snap_fname)}",
                *cdrom_args)

            if result.ok:
                self.logger.info(f"snapshot '{snapshot_name}' created for vm {vm_name}")
                return True
            else:
                self.logger.error(f"failed to create snapshot for vm {vm_name}: {result.stderr}")
                return False
        except Exception as exc:
            self.logger.error(f"error creating snapshot for vm {vm_name}: {exc}")
            return False

    def get_latest_snapshot(self, vm_name: str) -> str | None:
        """
        Get the name of the current (latest) snapshot for a VM.

        Args:
            vm_name: Name of the VM

        Returns:
            str: Name of the current snapshot, or None if none exists
        """
        result = self.virsh.execute("snapshot-current", vm_name, "--name")
        if result.ok:
            name = result.stdout.strip()
            return name if name else None
        return None

    def validate_snapshot(self, vm_name: str, snapshot_name: str):
        """
        Validate that a snapshot is intact and can be safely restored.

        Checks:
        1. ``virsh snapshot-info`` succeeds (snapshot exists in libvirt).
        2. The memory ``.raw`` file referenced in the snapshot XML exists on disk.
        3. All external disk overlay files referenced in the snapshot XML exist on disk.

        Args:
            vm_name: Name of the VM
            snapshot_name: Name of the snapshot to validate

        Returns:
            tuple[bool, List[str]]: (valid, errors) where errors is empty on success.
        """
        errors = []

        # 1. Check snapshot exists in libvirt
        info_result = self.virsh.execute("snapshot-info", vm_name, snapshot_name, warn=True)
        if not info_result.ok:
            errors.append(f"snapshot-info failed: {info_result.stderr.strip()}")
            return False, errors

        # 2. Parse snapshot XML to check files
        dumpxml_result = self.virsh.execute(
            "snapshot-dumpxml", vm_name, snapshot_name, warn=True)
        if not dumpxml_result.ok:
            errors.append(f"snapshot-dumpxml failed: {dumpxml_result.stderr.strip()}")
            return False, errors

        try:
            root = ET.fromstring(dumpxml_result.stdout)

            # Check memory file
            memory_elem = root.find("memory")
            if memory_elem is not None:
                mem_file = memory_elem.get("file")
                if mem_file and not os.path.isfile(mem_file):
                    errors.append(f"memory file missing: {mem_file}")

            # Check external disk overlay files
            for disk in root.findall(".//disks/disk"):
                if disk.get("snapshot") != "external":
                    continue
                source = disk.find("source")
                if source is not None:
                    src_file = source.get("file")
                    if src_file and not os.path.isfile(src_file):
                        errors.append(f"disk overlay missing: {src_file}")

        except ET.ParseError as exc:
            errors.append(f"failed to parse snapshot XML: {exc}")
            return False, errors

        return len(errors) == 0, errors

    def list_snapshots(self, vm_name: str) -> list[dict[str, str]]:
        """
        List all snapshots for a vm.

        Args:
            vm_name: Name of the vm

        Returns:
            list: List of snapshot info dictionaries
        """
        try:
            # fetch the available snapshots
            result = self.virsh.execute("snapshot-list", vm_name, "--name")
            if not result.ok:
                self.logger.error(f"failed to list snapshots for vm {vm_name}: {result.stderr}")
                return []

            snapshot_names = result.stdout.strip().split('\n')
            snapshot_names = [name for name in snapshot_names if name]

            # get the snapshot details
            snapshots = []
            for snapshot_name in snapshot_names:
                dumpxml_result = self.virsh.execute("snapshot-dumpxml", vm_name, snapshot_name)
                if dumpxml_result.ok:
                    snap_info = {'name': snapshot_name}
                    xml_content = dumpxml_result.stdout

                    root = ET.fromstring(xml_content)
                    snap_info['description'] = root.findtext('description', default='')

                    snapshots.append(snap_info)
            return snapshots
        except Exception as exc:
            self.logger.error(f"error listing snapshots for vm {vm_name}: {exc}")
            return []

    def _get_snapshot_overlay_files(self, vm_name: str) -> dict[str, str]:
        """
        Return a mapping of snapshot_name -> set of overlay file paths
        for every external snapshot on *vm_name*.
        """
        result = self.virsh.execute(
            "snapshot-list", vm_name, "--name", warn=True)
        if not result.ok:
            return {}

        overlays: dict[str, list[str]] = {}
        for snap in result.stdout.strip().splitlines():
            snap = snap.strip()
            if not snap:
                continue
            xml_result = self.virsh.execute(
                "snapshot-dumpxml", vm_name, snap, warn=True)
            if not xml_result.ok:
                continue
            try:
                root = ET.fromstring(xml_result.stdout)
                files = []
                for disk in root.findall(".//disks/disk"):
                    if disk.get("snapshot") != "external":
                        continue
                    source = disk.find("source")
                    if source is not None and source.get("file"):
                        files.append(source.get("file"))
                if files:
                    overlays[snap] = files
            except ET.ParseError:
                continue
        return overlays

    def _preserve_snapshot_overlays(self, vm_name: str) -> list[tuple]:
        """
        Back up overlay files that belong to snapshots and may be deleted
        by ``snapshot-revert``.

        libvirt deletes the overlay of the *current* snapshot when
        reverting to an earlier one.  This method copies those files so
        they can be restored afterwards, keeping every snapshot reachable.

        Returns a list of (original_path, backup_path) tuples.
        """
        all_overlays = self._get_snapshot_overlay_files(vm_name)

        # collect every overlay file referenced by any snapshot
        files_to_preserve: set = set()
        for files in all_overlays.values():
            files_to_preserve.update(files)

        pairs = [(f, f + '.preserve') for f in sorted(files_to_preserve)
                 if os.path.isfile(f)]
        if not pairs:
            return []

        # batch all copies into one command → one sudo prompt
        sudo = "sudo " if self.use_sudo else ""
        cmd = " && ".join(
            f"{sudo}rsync -aW --sparse '{src}' '{dst}'"
            for src, dst in pairs)
        result = self.virsh.execute_shell(cmd, warn=True)
        if result.ok:
            for src, _ in pairs:
                self.logger.debug(f"preserved snapshot overlay: {src}")
            return pairs

        self.logger.warning(
            f"failed to preserve overlays for {vm_name}: {result.stderr}")
        return []

    def _restore_preserved_overlays(self, preserved: list[tuple]) -> None:
        """
        Restore overlay files that were deleted during revert and clean
        up backup files that are no longer needed.
        """
        if not preserved:
            return

        sudo = "sudo " if self.use_sudo else ""

        to_restore = [(o, b) for o, b in preserved
                      if not os.path.isfile(o) and os.path.isfile(b)]
        to_cleanup = [b for o, b in preserved
                      if os.path.isfile(o) and os.path.isfile(b)]

        if to_restore:
            cmd = " && ".join(
                f"{sudo}rsync -aW --sparse --remove-source-files '{b}' '{o}'"
                for o, b in to_restore)
            result = self.virsh.execute_shell(cmd, warn=True)
            if result.ok:
                for o, _ in to_restore:
                    self.logger.info(
                        f"restored overlay deleted by revert: {o}")
            else:
                self.logger.warning(
                    f"failed to restore overlays: {result.stderr}")

        if to_cleanup:
            cmd = " && ".join(
                f"{sudo}rm -f '{b}'" for b in to_cleanup)
            self.virsh.execute_shell(cmd, warn=True)

    def snapshot_restore(self, vm_name: str, snapshot_name: str) -> bool:
        """
        Revert a VM to a specific snapshot.

        Before reverting, overlay files referenced by all snapshots are
        backed up.  After the revert any overlays that libvirt deleted
        are restored so that every snapshot remains reachable.

        Retries up to 3 times on write-lock contention errors, which can
        occur transiently when multiple snapshot-reverts run in parallel and
        QEMU races to acquire an exclusive lock while creating the new disk
        overlay for the post-revert state.

        Args:
            vm_name: Name of the vm
            snapshot_name: Name of the snapshot to revert to

        Returns:
            bool: True if successful, False otherwise
        """
        preserved = self._preserve_snapshot_overlays(vm_name)

        max_retries = 3
        last_stderr = ''

        for attempt in range(1, max_retries + 1):
            try:
                result = self.virsh.execute(
                    "snapshot-revert", vm_name, snapshot_name, warn=True)

                if result.ok:
                    self.logger.info(f"vm {vm_name} reverted to snapshot '{snapshot_name}'")
                    self._restore_preserved_overlays(preserved)
                    return True

                last_stderr = result.stderr or ''
                if 'write' in last_stderr and 'lock' in last_stderr and attempt < max_retries:
                    self.logger.warning(
                        f"write lock contention reverting {vm_name}, "
                        f"retrying in 2s (attempt {attempt}/{max_retries})")
                    time.sleep(2)
                    continue

                break

            except Exception as exc:
                self.logger.error(
                    f"error reverting vm {vm_name} to snapshot '{snapshot_name}': {exc}")
                self._restore_preserved_overlays(preserved)
                return False

        self._restore_preserved_overlays(preserved)
        self.logger.error(
            f"failed to revert vm {vm_name} to snapshot '{snapshot_name}': {last_stderr}")
        return False

    def delete_snapshot(self, vm_name: str, snapshot_name: str) -> bool:
        """
        Delete a specific snapshot.

        Args:
            vm_name: Name of the VM
            snapshot_name: Name of the snapshot to delete

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # use virsh snapshot-delete to delete the snapshot
            result = self.virsh.execute("snapshot-delete", vm_name, snapshot_name)

            if result.ok:
                self.logger.info(f"snapshot '{snapshot_name}' deleted from vm {vm_name}")
                return True
            else:
                self.logger.error(
                    f"failed to delete snapshot '{snapshot_name}' from vm {vm_name}: {result.stderr}")
                return False
        except Exception as exc:
            self.logger.error(f"error deleting snapshot '{snapshot_name}' from vm {vm_name}: {exc}")
            return False
