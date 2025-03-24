import os
from typing import Dict, Any, Optional, List
from .net import Network, NetworkInterface
from .clone_vm import CloneVM
from .destroy_vm import DestroyVM
from .disk import DiskManager

class LibVirtSession:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the LibVirtSession.

        Args:
            config: Optional configuration dictionary
        """
        #: Optional[Dict[str, Any]]: The configuration for this session
        self.config = config

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
        from .commands import VirshCommand

        try:
            virsh = VirshCommand(self.config.get('provider', {}))

            # Check if VM is already running
            result = virsh.execute("domstate", vm_name, warn=True)
            if result.ok and "running" in result.stdout:
                print(f"VM {vm_name} is already running")
                return True

            # Try to start the VM
            result = virsh.execute("start", vm_name)

            if not result.ok:
                print(f"Failed to start VM {vm_name}: {result.stderr}")
                return False

            # Verify that VM is running
            verify_result = virsh.execute("domstate", vm_name)

            if "running" in verify_result.stdout:
                print(f"VM {vm_name} started successfully")
                return True
            else:
                print(f"VM {vm_name} did not start properly. Current state: {verify_result.stdout}")
                return False

        except Exception as e:
            import traceback
            print(f"Error starting VM {vm_name}: {e}")
            print(traceback.format_exc())
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
            print(f"Configuring network interface {i+1} for VM {vm_name}")

            if not network_interface.configure_from_config(adapter_config):
                print(f"Failed to configure network interface {i+1} for VM {vm_name}")
                success = False
            else:
                print(f"Successfully configured network interface {i+1} for VM {vm_name}")

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
            print(f"Configuring disk {i+1} for VM {vm_name}")

            if not disk_manager.configure_from_disk_config(
                disk_config=disk_config,
                workdir=workdir,
                disk_prefix=disk_prefix
            ):
                print(f"Failed to configure disk {i+1} for VM {vm_name}")
                success = False
            else:
                print(f"Successfully configured disk {i+1} for VM {vm_name}")

        return success
