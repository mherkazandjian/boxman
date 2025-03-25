#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Snapshot module for libvirt provider.
This module provides functionality to manage VM snapshots using command-line tools.
"""

import os
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from .commands import VirshCommand

log = logging.getLogger(__name__)

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
        self.virsh = VirshCommand(provider_config)

        #: str: URI for libvirt connection
        self.uri = provider_config.get('uri', 'qemu:///system') if provider_config else 'qemu:///system'

        #: bool: Whether to use sudo
        self.use_sudo = provider_config.get('use_sudo', False) if provider_config else False

    def create_snapshot(self,
                        vm_name: str,
                        snapshot_name: str,
                        description: str) -> bool:
        """
        Create a snapshot of a VM.

        Args:
            vm_name: Name of the VM
            snapshot_name: Name for the snapshot
            description: Description for the snapshot

        Returns:
            bool: True if successful, False otherwise
        """
        xml_content = f"""
        <domainsnapshot>
            <name>{snapshot_name}</name>
            <description>{description}</description>
        </domainsnapshot>
        """
        tmp_xml_path = os.path.expanduser(f"/tmp/{vm_name}_snapshot_{snapshot_name}.xml")
        try:
            with open(tmp_xml_path, 'w') as f:
                f.write(xml_content)

            # Use virsh snapshot-create to create the snapshot
            result = self.virsh.execute("snapshot-create", vm_name, f"--xmlfile={tmp_xml_path}")

            if result.ok:
                log.info(f"Snapshot '{snapshot_name}' created for VM {vm_name}")
                return True
            else:
                log.error(f"Failed to create snapshot for VM {vm_name}: {result.stderr}")
                return False
        except Exception as e:
            log.error(f"Error creating snapshot for VM {vm_name}: {e}")
            return False
        finally:
            # Clean up temporary file
            if os.path.exists(tmp_xml_path):
                os.remove(tmp_xml_path)

    def list_snapshots(self, vm_name: str) -> List[Dict[str, str]]:
        """
        List all snapshots for a VM.

        Args:
            vm_name: Name of the VM

        Returns:
            list: List of snapshot info dictionaries
        """
        try:
            # Use virsh snapshot-list to get all snapshots
            result = self.virsh.execute("snapshot-list", vm_name, "--name")

            if not result.ok:
                log.error(f"Failed to list snapshots for VM {vm_name}: {result.stderr}")
                return []

            snapshot_names = result.stdout.strip().split('\n')
            # Filter out empty strings
            snapshot_names = [name for name in snapshot_names if name]

            snapshots = []
            for name in snapshot_names:
                # Get detailed information about each snapshot
                info_result = self.virsh.execute("snapshot-info", vm_name, name)

                if info_result.ok:
                    info_lines = info_result.stdout.strip().split('\n')
                    snap_info = {'name': name}

                    # Parse snapshot info output
                    for line in info_lines:
                        if ':' in line:
                            key, value = line.split(':', 1)
                            key = key.strip().lower().replace(' ', '_')
                            value = value.strip()

                            if key == 'description':
                                snap_info['description'] = value
                            elif key == 'creation_time':
                                snap_info['creation_time'] = value

                    # Set default values if not found
                    if 'description' not in snap_info:
                        snap_info['description'] = "No description"
                    if 'creation_time' not in snap_info:
                        snap_info['creation_time'] = "Unknown"

                    snapshots.append(snap_info)

            return snapshots
        except Exception as e:
            log.error(f"Error listing snapshots for VM {vm_name}: {e}")
            return []

    def snapshot_restore(self, vm_name: str, snapshot_name: str) -> bool:
        """
        Revert a VM to a specific snapshot.

        Args:
            vm_name: Name of the VM
            snapshot_name: Name of the snapshot to revert to

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Use virsh snapshot-revert to revert to snapshot
            result = self.virsh.execute("snapshot-revert", vm_name, snapshot_name)

            if result.ok:
                log.info(f"VM {vm_name} reverted to snapshot '{snapshot_name}'")
                return True
            else:
                log.error(f"Failed to revert VM {vm_name} to snapshot '{snapshot_name}': {result.stderr}")
                return False
        except Exception as e:
            log.error(f"Error reverting VM {vm_name} to snapshot '{snapshot_name}': {e}")
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
            # Use virsh snapshot-delete to delete the snapshot
            result = self.virsh.execute("snapshot-delete", vm_name, snapshot_name)

            if result.ok:
                log.info(f"Snapshot '{snapshot_name}' deleted from VM {vm_name}")
                return True
            else:
                log.error(f"Failed to delete snapshot '{snapshot_name}' from VM {vm_name}: {result.stderr}")
                return False
        except Exception as e:
            log.error(f"Error deleting snapshot '{snapshot_name}' from VM {vm_name}: {e}")
            return False
