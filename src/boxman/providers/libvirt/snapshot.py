#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Snapshot module for libvirt provider.
This module provides functionality to manage VM snapshots using command-line tools.
"""

import os
from datetime import datetime
from typing import Dict, Any, List, Optional
from .commands import VirshCommand
from xml.etree import ElementTree as ET

from boxman import log

class SnapshotManager:
    """
    Class to manage snapshots of VMs in libvirt using virsh commands.
    """

    def __init__(self, provider_config: Optional[Dict[str, Any]] = None):
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
            # use virsh snapshot-create to create the snapshot
            snap_fname = f"{vm_name}_snapshot_{snapshot_name}.raw"
            result = self.virsh.execute(
                "snapshot-create-as",
                f"--domain {vm_name}",
                f"--name {snapshot_name}",
                f"--description '{description}'",
                "--atomic",
                f"--memspec={os.path.join(vm_dir, snap_fname)}")

            if result.ok:
                self.logger.info(f"snapshot '{snapshot_name}' created for vm {vm_name}")
                return True
            else:
                self.logger.error(f"failed to create snapshot for vm {vm_name}: {result.stderr}")
                return False
        except Exception as exc:
            self.logger.error(f"error creating snapshot for vm {vm_name}: {exc}")
            return False

    def list_snapshots(self, vm_name: str) -> List[Dict[str, str]]:
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

    def snapshot_restore(self, vm_name: str, snapshot_name: str) -> bool:
        """
        Revert a VM to a specific snapshot.

        Args:
            vm_name: Name of the vm
            snapshot_name: Name of the snapshot to revert to

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Use virsh snapshot-revert to revert to snapshot
            result = self.virsh.execute("snapshot-revert", vm_name, snapshot_name)

            if result.ok:
                self.logger.info(f"vm {vm_name} reverted to snapshot '{snapshot_name}'")
                return True
            else:
                self.logger.error(
                    f"failed to revert vm {vm_name} to snapshot '{snapshot_name}': {result.stderr}")
                return False
        except Exception as exc:
            self.logger.error(f"error reverting vm {vm_name} to snapshot '{snapshot_name}': {exc}")
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
