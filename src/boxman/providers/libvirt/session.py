import os
from typing import Dict, Any, Optional
from .netdefine import NetDefine

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

        net_define = NetDefine(network_name, network_info)

        status = net_define.define_network(
            file_path=os.path.join(workdir, f'{network_name}_net_define.xml')
        )
