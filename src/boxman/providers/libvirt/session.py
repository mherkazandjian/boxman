import os
from typing import Dict, Any, Optional
from .net import Network

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
                       cluster_name,
                       network_name):
        """

        Args:
            name: Name of the network
            network_info: Dictionary containing network configuration

        Returns:
            True if successful, False otherwise
        """
        network_info = self.config['clusters'][cluster_name]['networks'][network_name]
        workdir = self.config['clusters'][cluster_name]['workdir']
        network_name = f'{cluster_name}_{network_name}'

        network = Network(network_name, network_info)

        status = network.define_network(
            file_path=os.path.join(workdir, f'{network_name}_net_define.xml')
        )

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

    def clone_vm

    def destroy_vm