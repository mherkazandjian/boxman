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
                        description: str,
                        compress_memory: bool = False,
                        compress_level: int = 3) -> bool:
        """
        Create a snapshot of a VM.

        Args:
            vm_name: Name of the VM
            vm_dir: Directory where the VM is located
            snapshot_name: Name for the snapshot
            description: Description for the snapshot
            compress_memory: If True, zstd-compress the .raw memory file
                after successful snapshot creation. The libvirt snapshot
                metadata still references the original .raw path; the
                ``snapshot_restore`` path detects the .raw.zst sibling and
                decompresses transparently.
            compress_level: zstd compression level (default 3 — sweet spot
                of ~71% reduction at sub-second/GB on commodity CPUs).

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
            mem_path = os.path.join(vm_dir, snap_fname)
            # Exclude cdroms from the snapshot so no new qcow2 overlay is
            # created for them (they stay at the raw ISO after this call).
            cdrom_args = self._cdrom_diskspec_args(vm_name)
            result = self.virsh.execute(
                "snapshot-create-as",
                f"--domain {vm_name}",
                f"--name {snapshot_name}",
                f"--description '{description}'",
                "--atomic",
                f"--memspec={mem_path}",
                *cdrom_args)

            if result.ok:
                self.logger.info(f"snapshot '{snapshot_name}' created for vm {vm_name}")
                if compress_memory:
                    if not self.compress_memory_file(mem_path, level=compress_level):
                        self.logger.warning(
                            f"snapshot '{snapshot_name}' for vm {vm_name}: memory "
                            f"compression failed; .raw kept on disk")
                return True
            else:
                self.logger.error(f"failed to create snapshot for vm {vm_name}: {result.stderr}")
                return False
        except Exception as exc:
            self.logger.error(f"error creating snapshot for vm {vm_name}: {exc}")
            return False

    # ── memory file compression (zstd) ──────────────────────────────────
    #
    # libvirt's --memspec writes a raw memory dump that's typically the
    # full RAM size of the VM (multi-GB).  zstd -3 reduces this by ~71%
    # at sub-second/GB.  We keep the libvirt snapshot metadata pointing
    # at the original .raw path; the .raw.zst sits next to it.  At
    # restore time, snapshot_restore checks for the sibling and
    # decompresses just-in-time before issuing snapshot-revert.

    def _is_zstd_available(self) -> bool:
        """Whether ``zstd`` is reachable in the configured runtime."""
        result = self.virsh.execute_shell("command -v zstd", warn=True, hide=True)
        return result.ok

    def _memory_path_from_xml(self, vm_name: str, snapshot_name: str) -> str | None:
        """Return the memory file path libvirt has recorded for a snapshot."""
        result = self.virsh.execute(
            "snapshot-dumpxml", vm_name, snapshot_name, warn=True)
        if not result.ok:
            return None
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError:
            return None
        memory_elem = root.find("memory")
        if memory_elem is None:
            return None
        path = memory_elem.get("file")
        return path or None

    def compress_memory_file(self,
                             raw_path: str,
                             level: int = 3,
                             threads: int = 0) -> bool:
        """
        zstd-compress *raw_path* in place to ``<raw_path>.zst`` (and remove
        the .raw on success — ``--rm``).

        Idempotent: if .raw is already gone and .raw.zst exists, returns True.
        """
        zst_path = f"{raw_path}.zst"
        if not os.path.isfile(raw_path):
            if os.path.isfile(zst_path):
                return True
            self.logger.warning(f"memory file not found: {raw_path}")
            return False
        if not self._is_zstd_available():
            self.logger.error(
                "zstd not found in runtime — install zstd in the docker "
                "image / host before using --compress-memory")
            return False
        sudo = "sudo " if self.use_sudo else ""
        cmd = (f"{sudo}zstd -{level} -T{threads} --rm -q -f "
               f"-o '{zst_path}' '{raw_path}'")
        self.logger.info(f"compressing memory file {raw_path} (zstd -{level})")
        result = self.virsh.execute_shell(cmd, warn=True)
        if not result.ok:
            self.logger.error(
                f"zstd compress failed for {raw_path}: {result.stderr.strip()}")
            return False
        return True

    def compress_all_memory(self, vm_name: str, level: int = 3) -> tuple[int, int]:
        """
        Compress every snapshot's memory ``.raw`` file for *vm_name*.

        Returns ``(compressed, total)`` — total counts snapshots that *had*
        an uncompressed memory file at start; compressed counts ones we
        successfully shrunk. Snapshots whose memory was already compressed
        (or absent) are not counted.
        """
        compressed = 0
        candidates = 0
        for snap in self.list_snapshots(vm_name):
            mem_path = self._memory_path_from_xml(vm_name, snap['name'])
            if not mem_path:
                continue
            if not os.path.isfile(mem_path):
                continue  # already compressed or missing
            candidates += 1
            if self.compress_memory_file(mem_path, level=level):
                compressed += 1
        return compressed, candidates

    def decompress_all_memory(self, vm_name: str) -> tuple[int, int]:
        """Inverse of :meth:`compress_all_memory`."""
        decompressed = 0
        candidates = 0
        for snap in self.list_snapshots(vm_name):
            mem_path = self._memory_path_from_xml(vm_name, snap['name'])
            if not mem_path:
                continue
            if not os.path.isfile(f"{mem_path}.zst"):
                continue
            if os.path.isfile(mem_path):
                continue  # already decompressed
            candidates += 1
            if self.decompress_memory_file(mem_path, keep_zst=False):
                decompressed += 1
        return decompressed, candidates

    def decompress_memory_file(self,
                               raw_path: str,
                               keep_zst: bool = True) -> bool:
        """
        Decompress ``<raw_path>.zst`` → ``<raw_path>``.

        ``keep_zst`` defaults to True because callers (snapshot_restore)
        typically want to re-compress after revert; storing both during
        the revert is a small price for cheap recompression.
        """
        zst_path = f"{raw_path}.zst"
        if not os.path.isfile(zst_path):
            return False
        if os.path.isfile(raw_path):
            return True  # already decompressed
        if not self._is_zstd_available():
            self.logger.error(
                "zstd not found in runtime — cannot decompress memory file")
            return False
        sudo = "sudo " if self.use_sudo else ""
        keep_flag = "-k " if keep_zst else ""
        cmd = (f"{sudo}zstd -d {keep_flag}-q -f "
               f"-o '{raw_path}' '{zst_path}'")
        self.logger.info(f"decompressing memory file {zst_path}")
        result = self.virsh.execute_shell(cmd, warn=True)
        if not result.ok:
            self.logger.error(
                f"zstd decompress failed for {zst_path}: {result.stderr.strip()}")
            return False
        return True

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

        Compressed memory: if the snapshot's ``.raw`` memory file is missing
        but a ``.raw.zst`` sibling exists (created by ``snapshot take
        --compress-memory`` or ``storage compress-snapshots``), it is
        decompressed in-place before the revert and re-compressed afterwards
        so the next revert remains compressed.

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

        # Just-in-time decompress if the memory file was zstd'd.
        mem_path = self._memory_path_from_xml(vm_name, snapshot_name)
        decompressed_for_revert = False
        if mem_path and not os.path.isfile(mem_path) and os.path.isfile(f"{mem_path}.zst"):
            self.logger.info(
                f"memory file is compressed; decompressing before revert: "
                f"{mem_path}.zst")
            if self.decompress_memory_file(mem_path, keep_zst=True):
                decompressed_for_revert = True
            else:
                self.logger.error(
                    f"could not decompress memory for {vm_name}/{snapshot_name}; "
                    f"revert will likely fail")

        max_retries = 3
        last_stderr = ''
        success = False

        for attempt in range(1, max_retries + 1):
            try:
                result = self.virsh.execute(
                    "snapshot-revert", vm_name, snapshot_name, warn=True)

                if result.ok:
                    self.logger.info(f"vm {vm_name} reverted to snapshot '{snapshot_name}'")
                    success = True
                    break

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
                self._recompress_after_revert(mem_path, decompressed_for_revert)
                return False

        self._restore_preserved_overlays(preserved)
        self._recompress_after_revert(mem_path, decompressed_for_revert)

        if success:
            return True

        self.logger.error(
            f"failed to revert vm {vm_name} to snapshot '{snapshot_name}': {last_stderr}")
        return False

    def _recompress_after_revert(self,
                                 mem_path: str | None,
                                 decompressed_for_revert: bool) -> None:
        """
        Re-compress the memory ``.raw`` file (and remove it) if we
        decompressed it for this revert. Best-effort — failures are
        warnings, not errors.
        """
        if not (decompressed_for_revert and mem_path and os.path.isfile(mem_path)):
            return
        zst_path = f"{mem_path}.zst"
        if os.path.isfile(zst_path):
            # We kept the .zst around during revert, so delete the
            # decompressed copy rather than re-running zstd.
            sudo = "sudo " if self.use_sudo else ""
            self.virsh.execute_shell(f"{sudo}rm -f '{mem_path}'", warn=True)
            return
        # No .zst on disk (unusual) — re-compress from scratch.
        if not self.compress_memory_file(mem_path):
            self.logger.warning(
                f"could not re-compress memory file after revert: {mem_path}")

    def delete_snapshot(self, vm_name: str, snapshot_name: str) -> bool:
        """
        Delete a snapshot.

        Internal (libvirt-native) snapshots are removed by ``virsh
        snapshot-delete`` directly. External snapshots — which is what
        boxman creates via ``snapshot-create-as --memspec`` — are not
        deletable that way; libvirt errors with "deletion of external
        snapshots is not supported". For external snapshots we dispatch:

        - **Only external snapshot**: ``virsh blockcommit --active --pivot``
          per disk merges the overlay back into base while online; then
          we remove the overlay/memory files and clear the libvirt
          metadata with ``--metadata``.
        - **Most-recent external snapshot with older snapshots above**:
          equivalent to :meth:`collapse_to` against the parent — the VM
          is shut down, ``qemu-img rebase`` rewires the chain, and the
          dropped snapshot's files + metadata are cleaned up.
        - **Non-current external snapshot**: refused with a clear
          pointer to ``boxman snapshot collapse --to <name>``; rebasing
          a middle layer while preserving both ends is out of v1 scope.
        """
        info = self.virsh.execute("snapshot-info", vm_name, snapshot_name, warn=True)
        if not info.ok:
            self.logger.error(
                f"snapshot '{snapshot_name}' not found on vm {vm_name}: "
                f"{info.stderr.strip()}")
            return False

        # Try the native delete first — works for internal snapshots.
        simple = self.virsh.execute(
            "snapshot-delete", vm_name, snapshot_name, warn=True)
        if simple.ok:
            self.logger.info(f"snapshot '{snapshot_name}' deleted from vm {vm_name}")
            return True

        err_lower = (simple.stderr or "").lower()
        if "external" not in err_lower and "not supported" not in err_lower:
            self.logger.error(
                f"snapshot-delete failed: {simple.stderr.strip()}")
            return False

        # External snapshot — needs the rebase/blockcommit dance.
        return self._delete_external_snapshot(vm_name, snapshot_name)

    def _delete_external_snapshot(self,
                                  vm_name: str,
                                  snapshot_name: str) -> bool:
        chain = self._chain_order(vm_name)
        if snapshot_name not in chain:
            self.logger.error(
                f"snapshot {snapshot_name} not in chain for vm {vm_name}")
            return False

        idx = chain.index(snapshot_name)

        # v1: only the most-recent external snapshot is directly deletable.
        if idx != len(chain) - 1:
            newer = chain[idx + 1:]
            self.logger.error(
                f"cannot delete external snapshot '{snapshot_name}' from "
                f"{vm_name}: it has newer snapshots above it ({newer}). "
                f"use `boxman snapshot collapse --to "
                f"{chain[idx - 1] if idx > 0 else snapshot_name}` to drop "
                f"it (and the snapshots above it) in one step.")
            return False

        if idx == 0:
            # Only snapshot in the chain — collapse it back to base online.
            return self._collapse_only_external_snapshot_online(
                vm_name, snapshot_name)

        # Most-recent with older snapshots above base — collapse_to(parent)
        # drops just this one snapshot while keeping older ones revertable.
        parent = chain[idx - 1]
        return self.collapse_to(vm_name, parent, dry_run=False)

    def _collapse_only_external_snapshot_online(self,
                                                vm_name: str,
                                                snapshot_name: str) -> bool:
        """
        Online deletion of the only external snapshot via
        ``virsh blockcommit --active --pivot``. After this, the chain is
        a single base qcow2 again.
        """
        disks = self._data_disk_targets(vm_name)
        if not disks:
            self.logger.error(f"no data disks found for vm {vm_name}")
            return False

        for disk_target, _src in disks:
            commit = self.virsh.execute(
                "blockcommit", vm_name, disk_target,
                "--active", "--pivot", "--wait", "--verbose",
                warn=True)
            if not commit.ok:
                self.logger.error(
                    f"blockcommit failed for {vm_name}/{disk_target}: "
                    f"{commit.stderr.strip()}")
                return False

        sudo = "sudo " if self.use_sudo else ""
        for disk_target, _src in disks:
            overlay = self._overlay_path_for_snapshot(
                vm_name, snapshot_name, disk_target)
            if overlay and os.path.isfile(overlay):
                self.virsh.execute_shell(f"{sudo}rm -f '{overlay}'", warn=True)

        mem_path = self._memory_path_from_xml(vm_name, snapshot_name)
        if mem_path:
            for path in (mem_path, f"{mem_path}.zst"):
                if os.path.isfile(path):
                    self.virsh.execute_shell(f"{sudo}rm -f '{path}'", warn=True)

        meta = self.virsh.execute(
            "snapshot-delete", vm_name, snapshot_name, "--metadata", warn=True)
        if not meta.ok:
            self.logger.warning(
                f"files cleaned for {snapshot_name} but metadata delete "
                f"failed: {meta.stderr.strip()}")
        self.logger.info(
            f"external snapshot '{snapshot_name}' deleted from vm {vm_name}")
        return True

    # ── snapshot collapse (qemu-img rebase) ─────────────────────────────
    #
    # collapse_to(target) merges every snapshot strictly newer than
    # *target* into the live head, dropping their overlays and metadata.
    # *target* and older snapshots remain revertable. The VM must be off
    # because qemu-img rebase is offline-only — the BoxmanManager
    # orchestrator handles shutdown/start.

    def _chain_order(self, vm_name: str) -> list[str]:
        """
        Return all snapshot names for *vm_name* in chain order, oldest
        first. Built from each snapshot's ``<parent>`` element so it
        works for any tree topology, but boxman only takes linear chains.
        """
        snaps = self.list_snapshots(vm_name)
        if not snaps:
            return []

        parents: dict[str, str | None] = {}
        for snap in snaps:
            xml_result = self.virsh.execute(
                "snapshot-dumpxml", vm_name, snap['name'], warn=True)
            if not xml_result.ok:
                continue
            try:
                root = ET.fromstring(xml_result.stdout)
            except ET.ParseError:
                continue
            parent_name_elem = root.find("parent/name")
            parents[snap['name']] = (
                parent_name_elem.text if parent_name_elem is not None else None)

        children: dict[str | None, list[str]] = {}
        for name, parent in parents.items():
            children.setdefault(parent, []).append(name)

        ordered: list[str] = []
        queue: list[str | None] = [None]
        while queue:
            current = queue.pop(0)
            for child in children.get(current, []):
                ordered.append(child)
                queue.append(child)
        return ordered

    def _data_disk_targets(self, vm_name: str) -> list[tuple[str, str]]:
        """
        Return ``[(target_name, source_path), ...]`` for each
        ``<disk device='disk'>`` of *vm_name* (excludes cdroms).
        """
        result = self.virsh.execute(
            "domblklist", vm_name, "--details", warn=True)
        if not result.ok:
            return []
        disks: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            # columns: Type Device Target Source
            if parts[1] != 'disk':
                continue
            disks.append((parts[2], parts[3]))
        return disks

    def _overlay_path_for_snapshot(self,
                                   vm_name: str,
                                   snapshot_name: str,
                                   disk_target: str) -> str | None:
        """Source file recorded for *disk_target* in the snapshot XML."""
        result = self.virsh.execute(
            "snapshot-dumpxml", vm_name, snapshot_name, warn=True)
        if not result.ok:
            return None
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError:
            return None
        for disk in root.findall(".//disks/disk"):
            if disk.get("name") == disk_target:
                source = disk.find("source")
                if source is not None:
                    return source.get("file")
        return None

    def _qemu_img_rebase(self,
                         head_path: str,
                         new_base_path: str) -> bool:
        """
        ``qemu-img rebase -p -b <new_base> -F qcow2 <head>`` — VM must
        be offline. ``-p`` shows progress. The rebase merges intermediate
        overlays' content into *head* so reading *head* alone produces
        the same view as the original chain did.
        """
        sudo = "sudo " if self.use_sudo else ""
        cmd = (f"{sudo}qemu-img rebase -p "
               f"-b '{new_base_path}' -F qcow2 '{head_path}'")
        self.logger.info(f"rebasing {head_path} onto {new_base_path}")
        result = self.virsh.execute_shell(cmd, warn=True, hide=False)
        if not result.ok:
            self.logger.error(
                f"qemu-img rebase failed for {head_path}: "
                f"{result.stderr.strip()}")
            return False
        return True

    def _strip_backing_store_cache(self, vm_name: str) -> bool:
        """
        Remove cached ``<backingStore>`` children from each ``<disk
        device='disk'>`` in *vm_name*'s domain XML and ``virsh define``
        the cleaned XML.

        After modifying the qcow2 chain externally (rebase / blockcommit),
        libvirt's cached backingStore points at files that no longer exist
        and ``virsh start`` fails until the cache is cleared. Use a real
        XML parser — nested elements break naive regex.
        """
        import tempfile

        result = self.virsh.execute("dumpxml", vm_name, warn=True)
        if not result.ok:
            self.logger.error(
                f"dumpxml failed for {vm_name}: {result.stderr.strip()}")
            return False
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError as exc:
            self.logger.error(f"could not parse domain XML for {vm_name}: {exc}")
            return False

        modified = False
        for disk in root.findall(".//disk"):
            if disk.get("device") not in ("disk", None):
                continue
            for backing in list(disk.findall("backingStore")):
                disk.remove(backing)
                modified = True

        if not modified:
            return True

        new_xml = ET.tostring(root, encoding="unicode")
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".xml", delete=False) as tmp:
            tmp.write(new_xml)
            tmp_path = tmp.name
        try:
            define_result = self.virsh.execute("define", tmp_path, warn=True)
            if not define_result.ok:
                self.logger.error(
                    f"virsh define failed: {define_result.stderr.strip()}")
                return False
            self.logger.info(
                f"stripped cached <backingStore> from {vm_name} domain XML")
            return True
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def collapse_to(self,
                    vm_name: str,
                    target_name: str,
                    dry_run: bool = False) -> bool:
        """
        Collapse all snapshots strictly newer than *target_name* into the
        live head. *target_name* and older snapshots remain revertable.

        Workflow per disk: ``qemu-img rebase`` the head onto the target
        snapshot's overlay so the rebased head's read-alone view equals
        what the full chain produced. Then strip ``<backingStore>``
        cache, ``virsh define``, delete the dropped snapshots' overlay
        and memory files, and clear their libvirt metadata.

        VM must be offline. Returns True on success.
        """
        info = self.virsh.execute(
            "snapshot-info", vm_name, target_name, warn=True)
        if not info.ok:
            self.logger.error(
                f"target snapshot '{target_name}' not found on vm "
                f"{vm_name}: {info.stderr.strip()}")
            return False

        chain = self._chain_order(vm_name)
        if target_name not in chain:
            self.logger.error(
                f"snapshot '{target_name}' not in chain for vm {vm_name}")
            return False

        target_idx = chain.index(target_name)
        drop_set = chain[target_idx + 1:]

        if not drop_set:
            self.logger.info(
                f"snapshot '{target_name}' is already the most-recent on "
                f"vm {vm_name}; nothing to collapse")
            return True

        disks = self._data_disk_targets(vm_name)
        if not disks:
            self.logger.error(f"no data disks found for vm {vm_name}")
            return False

        if dry_run:
            self.logger.info(
                f"[dry-run] {vm_name}: would drop {len(drop_set)} snapshot(s) "
                f"({drop_set}); kept '{target_name}' and older")
            for disk_target, source in disks:
                new_base = self._overlay_path_for_snapshot(
                    vm_name, target_name, disk_target)
                self.logger.info(
                    f"[dry-run]   would rebase {source} onto {new_base}")
            return True

        # Per-disk rebase — must succeed for every disk before we touch metadata.
        for disk_target, head_path in disks:
            new_base = self._overlay_path_for_snapshot(
                vm_name, target_name, disk_target)
            if not new_base:
                self.logger.error(
                    f"cannot find overlay for disk '{disk_target}' in "
                    f"snapshot '{target_name}' of vm {vm_name}")
                return False
            if not self._qemu_img_rebase(head_path, new_base):
                return False

        if not self._strip_backing_store_cache(vm_name):
            return False

        sudo = "sudo " if self.use_sudo else ""
        for snap in drop_set:
            for disk_target, _ in disks:
                overlay = self._overlay_path_for_snapshot(
                    vm_name, snap, disk_target)
                if overlay and os.path.isfile(overlay):
                    self.virsh.execute_shell(
                        f"{sudo}rm -f '{overlay}'", warn=True)
            mem_path = self._memory_path_from_xml(vm_name, snap)
            if mem_path:
                for path in (mem_path, f"{mem_path}.zst"):
                    if os.path.isfile(path):
                        self.virsh.execute_shell(
                            f"{sudo}rm -f '{path}'", warn=True)
            meta = self.virsh.execute(
                "snapshot-delete", vm_name, snap, "--metadata", warn=True)
            if not meta.ok:
                self.logger.warning(
                    f"could not clear metadata for snapshot '{snap}' on "
                    f"{vm_name}: {meta.stderr.strip()}")

        self.logger.info(
            f"collapsed {len(drop_set)} snapshot(s) on vm {vm_name}; "
            f"'{target_name}' preserved as new oldest revertable point")
        return True
