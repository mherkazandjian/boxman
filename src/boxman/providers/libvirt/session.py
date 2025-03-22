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
                 cluster_name: str,
                 vm_name: str,
                 vm_info: Dict[str, Any]) -> bool:
        """
        Clone a VM.

        Args:
            cluster_name: Name of the cluster
            vm_name: Name of the VM
            vm_info: VM configuration information

        Returns:
            True if successful, False otherwise
        """
        workdir = self.config['clusters'][cluster_name]['workdir']
        full_vm_name = f"{cluster_name}_{vm_name}"

        # Prepare configuration for CloneVM
        clone_config = vm_info.copy()
        clone_config['xml_path'] = os.path.join(workdir, f"{full_vm_name}.xml")

        # Ensure the disk path is set
        if 'disk_path' not in clone_config:
            clone_config['disk_path'] = os.path.join(workdir, f"{full_vm_name}.qcow2")

        # Create CloneVM instance
        cloner = CloneVM(
            name=full_vm_name,
            config=clone_config,
            provider_config=self.config.get('provider', {})
        )

        # Clone VM and start it
        return cloner.clone_and_start()

    def destroy_vm(self, name: str, remove_storage: bool = True) -> bool:
        """
        Destroy (remove) a VM.

        Args:
            name: Name of the VM to destroy
            remove_storage: Whether to remove associated storage

        Returns:
            True if successful, False otherwise
        """
        destroyer = DestroyVM(name=name, provider_config=self.config.get('provider', {}))
        status = destroyer.remove(remove_storage=remove_storage)
        return status
