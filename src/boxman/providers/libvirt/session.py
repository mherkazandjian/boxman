import os
import time
from typing import Dict, Any, Optional, List
from .net import Network, NetworkInterface
from .clone_vm import CloneVM
from .destroy_vm import DestroyVM
from .disk import DiskManager
from datetime import datetime

from boxman import log
from .snapshot import SnapshotManager
from .commands import VirshCommand


class LibVirtSession:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the LibVirtSession.

        Args:
            config: Optional configuration dictionary
        """
        #: Optional[Dict[str, Any]]: The configuration for this session
        self.config = config

        #: logging.Logger: Logger instance
        self.logger = log

        # Get provider config
        self.provider_config = config.get('provider', {}).get('libvirt', {}) if config else {}

        # Extract commonly used provider settings
        self.uri = self.provider_config.get('uri', 'qemu:///system')
        self.use_sudo = self.provider_config.get('use_sudo', False)

    def define_network(self,
                       name: str = None,
                       info: Optional[Dict[str, Any]] = None,
                       workdir: Optional[str] = None) -> bool:
        """

        Args:
            name: Name of the network
            info: Dictionary containing network configuration

        Returns:
            True if successful, False otherwise
        """
        network = Network(name=name, info=info)

        status = network.define_network(
            file_path=os.path.join(workdir, f'{name}_net_define.xml')
        )
        return status

    def destroy_network(self, cluster_name: str, network_name: str) -> bool:
        """
        Destroy a network.

        Args:
            cluster_name: Name of the cluster
            network_name: Name of the network

        Returns:
            True if successful, False otherwise
        """
        network_info = self.config['clusters'][cluster_name]['networks'][network_name]
        full_network_name = f'{cluster_name}_{network_name}'

        network = Network(full_network_name, network_info)
        return network.destroy_network()

    def undefine_network(self, cluster_name: str, network_name: str) -> bool:
        """
        Undefine a network.

        Args:
            cluster_name: Name of the cluster
            network_name: Name of the network

        Returns:
            True if successful, False otherwise
        """
        network_info = self.config['clusters'][cluster_name]['networks'][network_name]
        full_network_name = f'{cluster_name}_{network_name}'

        network = Network(full_network_name, network_info)
        return network.undefine_network()

    def remove_network(self, cluster_name: str, network_name: str) -> bool:
        """
        Complete removal of a network: destroy and undefine.

        Args:
            cluster_name: Name of the cluster
            network_name: Name of the network

        Returns:
            True if successful, False otherwise
        """
        network_info = self.config['clusters'][cluster_name]['networks'][network_name]
        full_network_name = f'{cluster_name}_{network_name}'

        network = Network(name=full_network_name,
                          info=network_info,
                          provider_config=self.config['provider'])

        status = network.remove_network()

        return status

    def clone_vm(self,
                 new_vm_name: str,
                 src_vm_name: str,
                 info: Dict[str, Any],
                 workdir: str,
                 ) -> bool:
        """
        Clone a VM.

        Args:
            new_vm_name: Name of the new VM
            src_vm_name: Name of the source VM
            info: VM configuration information

        Returns:
            True if successful, False otherwise
        """
        cloner = CloneVM(
            src_vm_name=src_vm_name,
            new_vm_name=new_vm_name,
            info=info,
            provider_config=self.config.get('provider', {}),
            workdir=workdir,
        )

        status = cloner.clone()
        if not status:
            raise RuntimeError(
                f"Failed to clone VM {src_vm_name} to {new_vm_name}"
            )

    def destroy_disks(self,
                      workdir : str,
                      vm_name: str,
                      disks: List[Dict[str, str]],
                      ) -> bool:
        """
        Destroy disks associated with the VM.

        Args:
            vm_name: Name of the VM
            vm_info: VM configuration information

        Returns:
            True if successful, False otherwise
        """
        boot_disk = os.path.expanduser(
            os.path.join(workdir, f'{vm_name}.qcow2'))

        if os.path.isfile(boot_disk):
            os.remove(boot_disk)

        for disk in disks:
            disk_path = os.path.expanduser(
                os.path.join(
                    workdir,
                    f'{vm_name}_{disk["name"]}.qcow2')
                )
            if os.path.isfile(disk_path):
                os.remove(disk_path)

        return True

    def destroy_vm(self, name: str) -> bool:
        """
        Destroy (remove) a VM.

        Args:
            name: Name of the VM to destroy

        Returns:
            True if successful, False otherwise
        """
        destroyer = DestroyVM(name=name, provider_config=self.config.get('provider', {}))
        status = destroyer.remove()
        return status

    def start_vm(self, vm_name: str) -> bool:
        """
        Start a VM.

        Args:
            vm_name: Name of the VM to start

        Returns:
            True if successful, False otherwise
        """

        try:
            virsh = VirshCommand(self.config.get('provider', {}))

            # Check if VM is already running
            result = virsh.execute("domstate", vm_name, warn=True)
            if result.ok and "running" in result.stdout:
                self.logger.info(f"VM {vm_name} is already running")
                return True

            # Try to start the VM
            result = virsh.execute("start", vm_name)

            if not result.ok:
                self.logger.error(f"Failed to start VM {vm_name}: {result.stderr}")
                return False

            # Verify that VM is running
            verify_result = virsh.execute("domstate", vm_name)

            if "running" in verify_result.stdout:
                self.logger.info(f"VM {vm_name} started successfully")
                return True
            else:
                self.logger.error(f"VM {vm_name} did not start properly. Current state: {verify_result.stdout}")
                return False

        except Exception as e:
            import traceback
            self.logger.error(f"Error starting VM {vm_name}: {e}")
            self.logger.error(traceback.format_exc())
            return False

    def add_network_interface(self,
                              vm_name: str,
                              network_source: str,
                              link_state: str = 'active',
                              mac_address: Optional[str] = None,
                              model: str = 'virtio') -> bool:
        """
        Add a network interface to a VM.

        Args:
            vm_name: Name of the VM
            network_source: Name of the network to attach to
            link_state: State of the link ('active' or 'inactive')
            mac_address: Optional MAC address for the interface
            model: NIC model (default: virtio)

        Returns:
            True if successful, False otherwise
        """
        network_interface = NetworkInterface(
            vm_name=vm_name,
            provider_config=self.config.get('provider', {})
        )

        return network_interface.add_interface(
            network_source=network_source,
            link_state=link_state,
            mac_address=mac_address,
            model=model
        )

    def configure_vm_network_interfaces(self,
                                       vm_name: str,
                                       network_adapters: List[Dict[str, Any]]) -> bool:
        """
        Configure all network interfaces for a VM.

        Args:
            vm_name: Name of the VM
            network_adapters: List of network adapter configurations

        Returns:
            True if all adapters were configured successfully, False otherwise
        """
        network_interface = NetworkInterface(
            vm_name=vm_name,
            provider_config=self.config.get('provider', {})
        )

        success = True
        for i, adapter_config in enumerate(network_adapters):
            self.logger.info(f"Configuring network interface {i+1} for VM {vm_name}")

            if not network_interface.configure_from_config(adapter_config):
                self.logger.error(f"Failed to configure network interface {i+1} for VM {vm_name}")
                success = False
            else:
                self.logger.info(f"Successfully configured network interface {i+1} for VM {vm_name}")

        return success

    def configure_vm_disks(self,
                          vm_name: str,
                          disks: List[Dict[str, Any]],
                          workdir: str,
                          disk_prefix: str = "") -> bool:
        """
        Configure all disks for a VM.

        Args:
            vm_name: Name of the VM
            disks: List of disk configurations
            workdir: Working directory for disk images
            disk_prefix: Prefix to add to disk image filenames

        Returns:
            True if all disks were configured successfully, False otherwise
        """
        disk_manager = DiskManager(
            vm_name=vm_name,
            provider_config=self.config.get('provider', {})
        )

        success = True
        for i, disk_config in enumerate(disks):
            self.logger.info(f"Configuring disk {i+1} for VM {vm_name}")

            if not disk_manager.configure_from_disk_config(
                disk_config=disk_config,
                workdir=workdir,
                disk_prefix=disk_prefix
            ):
                self.logger.error(f"Failed to configure disk {i+1} for VM {vm_name}")
                success = False
            else:
                self.logger.info(f"Successfully configured disk {i+1} for VM {vm_name}")

        return success

    def get_vm_ip_addresses(self, vm_name: str) -> Dict[str, str]:
        """
        Get all IP addresses for a VM.

        Args:
            vm_name: Name of the VM

        Returns:
            Dictionary mapping interface names to IP addresses
        """
        try:
            # Use virsh commands to get domain info
            from .commands import VirshCommand
            virsh = VirshCommand(self.config.get('provider', {}))

            # First check if VM is running
            result = virsh.execute("domstate", vm_name, warn=True)
            if not result.ok or "running" not in result.stdout:
                self.logger.warn(f"VM {vm_name} is not running, cannot get IP addresses")
                return {}

            # Try domifaddr to get all interfaces and their IPs
            result = virsh.execute("domifaddr", vm_name, warn=True)

            if not result.ok:
                self.logger.error(f"Failed to get interface addresses for VM {vm_name}")
                return {}

            # Parse the output to extract interface information
            # Output format is like:
            # Name       MAC address          Protocol     Address
            # ---------------------------------------------------------
            # vnet0      52:54:00:xx:xx:xx    ipv4         192.168.122.x/24

            ip_addresses = {}
            lines = result.stdout.strip().split('\n')

            if len(lines) > 2:  # Skip header and separator lines
                for line in lines[2:]:
                    parts = line.split()
                    if len(parts) >= 4:  # Name MAC Protocol Address
                        iface_name = parts[0]
                        ip_address = parts[3].split('/')[0]  # Remove CIDR notation

                        if iface_name and ip_address and not ip_address.startswith('N/A'):
                            ip_addresses[iface_name] = ip_address

            return ip_addresses

        except Exception as e:
            import traceback
            self.logger.error(f"Error getting IP addresses for VM {vm_name}: {e}")
            self.logger.debug(traceback.format_exc())
            return {}

    ### snapshots
    def snapshot_take(self, vm_name=None, snapshot_name=None, description=None):
        """
        Create a snapshot of a specific VM

        Args:
            vm_name (str, optional): Full name of the VM to snapshot
            snapshot_name (str): Name for the snapshot
            description (str, optional): Description for the snapshot

        Returns:
            bool: True if successful, False otherwise
        """
        snapshot_mgr = SnapshotManager(self.provider_config)
        self.logger.info(f"Processing vm: {vm_name}")
        return snapshot_mgr.create_snapshot(
            vm_name=vm_name,
            snapshot_name=snapshot_name,
            description=description)

    def snapshot_list(self, vm_name=None):
        """
        List snapshots for VMs in the specified cluster or all clusters.

        Args:
            cluster_name (str, optional): Name of the cluster. If None, all clusters.
            vm_name (str, optional): Name of the VM. If None, all VMs in cluster(s).

        Returns:
            dict: Dictionary of snapshots per VM
        """
        self.logger.info(f"Listing snapshots for the VM: {vm_name}")
        snapshot_mgr = SnapshotManager(self.provider_config)
        snapshots = snapshot_mgr.list_snapshots(vm_name)
        for snapshot in snapshots:
            self.logger.info(
                f"  Snapshot: {snapshot['name']} - Description: {snapshot['description']}")

        return snapshots

    def snapshot_restore(self, vm_name, snapshot_name):
        """
        Restore a VM to a specific snapshot.

        Args:
            vm_name (str): Name of the VM to revert
            snapshot_name (str): Name of the snapshot to revert to

        Returns:
            bool: True if successful, False otherwise
        """
        self.logger.info(f"Reverting VM {vm_name} to snapshot {snapshot_name}")
        snapshot_mgr = SnapshotManager(self.provider_config)
        return snapshot_mgr.snapshot_restore(vm_name, snapshot_name)

    def snapshot_delete(self, vm_name, snapshot_name):
        """
        Delete a specific snapshot from a VM.

        Args:
            vm_name (str): Name of the VM
            snapshot_name (str): Name of the snapshot to delete

        Returns:
            bool: True if successful, False otherwise
        """
        self.logger.info(f"Deleting snapshot {snapshot_name} from VM {vm_name}")
        snapshot_mgr = SnapshotManager(self.provider_config)
        return snapshot_mgr.delete_snapshot(vm_name, snapshot_name)
    # end snapshots

    ### control vm
    def suspend_vm(self, vm_name: str) -> bool:
        """
        Suspend (pause) a VM.

        Args:
            vm_name: Name of the VM to suspend

        Returns:
            True if successful, False otherwise
        """
        try:
            virsh = VirshCommand(self.provider_config)

            # Check if VM is running
            result = virsh.execute("domstate", vm_name, warn=True)
            if not result.ok or "running" not in result.stdout:
                self.logger.warning(f"VM {vm_name} is not running, cannot suspend")
                return False

            # Try to suspend the VM
            self.logger.info(f"Suspending VM {vm_name}")
            result = virsh.execute("suspend", vm_name)

            if not result.ok:
                self.logger.error(f"Failed to suspend VM {vm_name}: {result.stderr}")
                return False

            # Verify VM is suspended
            verify_result = virsh.execute("domstate", vm_name)
            if "paused" in verify_result.stdout:
                self.logger.info(f"VM {vm_name} suspended successfully")
                return True
            else:
                self.logger.error(f"VM {vm_name} not suspended. Current state: {verify_result.stdout}")
                return False

        except Exception as e:
            self.logger.error(f"Error suspending VM {vm_name}: {e}")
            return False

    def resume_vm(self, vm_name: str) -> bool:
        """
        Resume a suspended VM.

        Args:
            vm_name: Name of the VM to resume

        Returns:
            True if successful, False otherwise
        """
        try:
            virsh = VirshCommand(self.provider_config)

            # Check if VM is suspended
            result = virsh.execute("domstate", vm_name, warn=True)
            if not result.ok:
                self.logger.warning(f"VM {vm_name} does not exist")
                return False
            if "paused" not in result.stdout:
                self.logger.warning(f"VM {vm_name} is not suspended (current state: {result.stdout.strip()})")
                return False

            # Try to resume the VM
            self.logger.info(f"Resuming VM {vm_name}")
            result = virsh.execute("resume", vm_name)

            if not result.ok:
                self.logger.error(f"Failed to resume VM {vm_name}: {result.stderr}")
                return False

            # Verify VM is running
            verify_result = virsh.execute("domstate", vm_name)
            if "running" in verify_result.stdout:
                self.logger.info(f"VM {vm_name} resumed successfully")
                return True
            else:
                self.logger.error(f"VM {vm_name} not resumed. Current state: {verify_result.stdout}")
                return False

        except Exception as e:
            self.logger.error(f"Error resuming VM {vm_name}: {e}")
            return False

    def save_vm(self, vm_name: str, workdir: str) -> bool:
        """
        Save VM state to a file in the specified workdir.

        Args:
            vm_name: Name of the VM to save
            workdir: Directory where the VM state will be saved

        Returns:
            True if successful, False otherwise
        """
        try:
            virsh = VirshCommand(self.provider_config)

            # Check if VM is running
            result = virsh.execute("domstate", vm_name, warn=True)
            if not result.ok or "running" not in result.stdout:
                self.logger.warning(f"VM {vm_name} is not running, cannot save state")
                return False

            # Expand the workdir path and ensure it exists
            workdir = os.path.expanduser(workdir)
            if not os.path.exists(workdir):
                os.makedirs(workdir, exist_ok=True)

            save_path = os.path.join(workdir, f"{vm_name}.save")

            # Try to save the VM state
            self.logger.info(f"Saving VM {vm_name} state to {save_path}")
            result = virsh.execute("save", vm_name, save_path)

            if not result.ok:
                self.logger.error(f"Failed to save VM {vm_name} state: {result.stderr}")
                return False

            # Verify the save file exists
            if os.path.exists(save_path):
                self.logger.info(f"VM {vm_name} state saved successfully to {save_path}")
                return True
            else:
                self.logger.error(f"Save file {save_path} not created for VM {vm_name}")
                return False

        except Exception as e:
            self.logger.error(f"Error saving VM {vm_name} state: {e}")
            return False

    def restore_vm(self, vm_name: str, workdir: str) -> bool:
        """
        Restore VM from a saved state file in the specified workdir.

        Args:
            vm_name: Name of the VM to restore
            workdir: Directory where the VM state was saved

        Returns:
            True if successful, False otherwise
        """
        try:
            virsh = VirshCommand(self.provider_config)

            # Expand the workdir path
            workdir = os.path.expanduser(workdir)
            save_path = os.path.join(workdir, f"{vm_name}.save")

            # Check if save file exists
            if not os.path.exists(save_path):
                self.logger.error(f"Save file {save_path} does not exist")
                return False

            # Check if the VM is defined but not running
            exists_result = virsh.execute("domstate", vm_name, warn=True)
            if exists_result.ok and "running" in exists_result.stdout:
                self.logger.warning(f"VM {vm_name} is already running, shutting down first")
                shutdown_result = virsh.execute("shutdown", vm_name)
                if not shutdown_result.ok:
                    self.logger.error(f"Failed to shutdown VM {vm_name} before restore: {shutdown_result.stderr}")
                    return False

                # Wait for VM to shut down
                for i in range(30):
                    state_result = virsh.execute("domstate", vm_name, warn=True)
                    if state_result.ok and "shut off" in state_result.stdout:
                        break
                    time.sleep(1)
                else:
                    self.logger.error(f"VM {vm_name} did not shut down within timeout")
                    return False

            # Try to restore the VM
            self.logger.info(f"Restoring VM {vm_name} from {save_path}")
            result = virsh.execute("restore", save_path)

            if not result.ok:
                self.logger.error(f"Failed to restore VM {vm_name}: {result.stderr}")
                return False

            # Verify VM is running
            verify_result = virsh.execute("domstate", vm_name)
            if "running" in verify_result.stdout:
                self.logger.info(f"VM {vm_name} restored successfully from {save_path}")
                # Optionally remove the save file after successful restore
                # os.remove(save_path)
                return True
            else:
                self.logger.error(f"VM {vm_name} not restored. Current state: {verify_result.stdout}")
                return False

        except Exception as e:
            self.logger.error(f"Error restoring VM {vm_name}: {e}")
            return False
    ### end control vm