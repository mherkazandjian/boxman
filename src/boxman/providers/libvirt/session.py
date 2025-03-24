import os
from typing import Dict, Any, Optional, List
from .net import Network
from .clone_vm import CloneVM
from .destroy_vm import DestroyVM

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
