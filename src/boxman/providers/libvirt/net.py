import os
import uuid
import re
import pkg_resources
from typing import Optional, Dict, Any, Union
import logging
import tempfile
from jinja2 import Template, Environment, FileSystemLoader
from .commands import VirshCommand, LibVirtCommandBase


class Network(VirshCommand):
    """
    Class to define libvirt networks by creating XML definitions and using virsh commands.
    """

    def __init__(self,
                name: str,
                info: Dict[str, Any],
                provider_config: Optional[Dict[str, Any]] = None):
        """
        Initialize the network definition with a dictionary-based configuration.

        Args:
            name: Name of the network
            info: Dictionary containing network configuration with keys like:
                 mode, bridge, mac, ip, network, enable, etc.
            provider_config: Configuration for the libvirt provider
        """
        super().__init__(provider_config)

        #: str: Name of the network
        self.name = name

        #: str: UUID of the network, generated if not provided
        self.uuid_val = str(uuid.uuid4())

        #: str: Forward mode (nat, route, bridge, etc.)
        self.forward_mode = info.get('mode', 'nat')

        # Extract bridge configuration
        bridge_info = info.get('bridge', {})

        # Handle the bridge name - if not specified, find the first available virbrX
        bridge_name = bridge_info.get('name')
        if not bridge_name:
            bridge_name = self.find_available_bridge_name()

        #: str: Bridge name
        self.bridge_name = bridge_name

        #: str: STP on/off for the bridge
        self.bridge_stp = bridge_info.get('stp', 'on')
        #: str: Delay for STP
        self.bridge_delay = bridge_info.get('delay', '0')

        #: str: MAC address for the bridge
        self.mac_address = info.get('mac', f"52:54:00:{':'.join(['%02x' % (i + 10) for i in range(3)])}")

        # Extract IP configuration
        ip_info = info.get('ip', {})
        #: str: IP address for the network
        self.ip_address = ip_info.get('address', '192.168.122.1')
        #: str: Netmask for the network
        self.netmask = ip_info.get('netmask', '255.255.255.0')

        # Extract DHCP configuration
        dhcp_info = ip_info.get('dhcp', {}).get('range', {})
        #: str: Start of DHCP range
        self.dhcp_range_start = dhcp_info.get('start', '192.168.122.2')
        #: str: End of DHCP range
        self.dhcp_range_end = dhcp_info.get('end', '192.168.122.254')

        #: bool: Whether the network should be enabled
        self.enable = info.get('enable', True)

        #: str: Network CIDR notation
        self.network = info.get('network', '')

    def generate_xml(self) -> str:
        """
        Generate the XML for the network definition using a Jinja2 template.

        Returns:
            XML string for the network definition
        """
        # Get the path to the assets directory
        assets_path = pkg_resources.resource_filename('boxman', 'assets')

        # Create a Jinja environment
        env = Environment(
            loader=FileSystemLoader(assets_path),
            trim_blocks=True,
            lstrip_blocks=True
        )

        # Load the template
        template = env.get_template('network.xml.j2')

        # Render the template with the network configuration
        context = {
            'name': self.name,
            'uuid_val': self.uuid_val,
            'forward_mode': self.forward_mode,
            'bridge_name': self.bridge_name,
            'bridge_stp': self.bridge_stp,
            'bridge_delay': self.bridge_delay,
            'mac_address': self.mac_address,
            'ip_address': self.ip_address,
            'netmask': self.netmask,
            'dhcp_range_start': self.dhcp_range_start,
            'dhcp_range_end': self.dhcp_range_end
        }

        return template.render(**context)

    def write_xml(self, file_path: str) -> str:
        """
        Write the XML to a file.

        Args:
            file_path: Path where the XML file should be written

        Returns:
            The path to the written file
        """
        xml_content = self.generate_xml()
        with open(os.path.expanduser(file_path), 'w') as f:
            f.write(xml_content)
        return file_path

    def define_network(self, file_path: Optional[str] = None):
        """
        Define the network using virsh.

        Args:
            file_path: Path to write the XML file, if None a temporary path will be used

        Returns:
            True if successful, False otherwise
        """
        if not file_path:
            file_path = f"/tmp/{self.name}-network.xml"

        self.write_xml(file_path)

        # Define the network
        try:
            self.execute("net-define", file_path)
            self.execute("net-start", self.name)
            self.execute("net-autostart", self.name)

            # If this is a routed network, apply the iptables rule
            if self.forward_mode == 'route':
                self.apply_route_iptables_rule()

            return True
        except RuntimeError as e:
            print(f"Error defining network: {e}")
            return False

    def define_and_start(self,
                        file_path: Optional[str] = None,
                        autostart: bool = True) -> bool:
        """
        Define and start the network in one operation.

        Args:
            file_path: Path to write the XML file
            autostart: Whether to set the network to autostart

        Returns:
            True if all operations were successful, False otherwise
        """
        if not self.define_network(file_path):
            return False

        if not self.start_network():
            return False

        if autostart and not self.autostart_network():
            return False

        return True

    def destroy_network(self) -> bool:
        """
        Destroy (stop) the network.

        Returns:
            True if successful, False otherwise
        """
        try:
            # Check if network exists first
            result = self.execute("net-list", "--all", "| grep -q " + self.name, warn=True)
            if result.return_code != 0:
                print(f"Network {self.name} does not exist, nothing to destroy")
                return True

            # Check if network is active
            result = self.execute("net-list", "| grep -q " + self.name, warn=True)
            if result.return_code == 0:
                # Network is active, stop it
                self.execute("net-destroy", self.name)
                print(f"Network {self.name} destroyed successfully")

            return True
        except RuntimeError as e:
            print(f"Error destroying network: {e}")
            return False

    def undefine_network(self) -> bool:
        """
        Undefine (remove definition of) the network.

        Returns:
            True if successful, False otherwise
        """
        try:
            # Check if network exists
            result = self.execute("net-list", "--all", "| grep -q " + self.name, warn=True)
            if result.return_code != 0:
                print(f"Network {self.name} does not exist, nothing to undefine")
                return True

            # Disable autostart first if it's enabled
            self.execute("net-autostart", self.name, "--disable", warn=True)

            # Undefine the network
            self.execute("net-undefine", self.name)
            print(f"Network {self.name} undefined successfully")
            return True
        except RuntimeError as e:
            print(f"Error un-defining network: {e}")
            return False

    def remove_network(self) -> bool:
        """
        Complete removal of a network: destroy and undefine.

        Returns:
            True if all operations were successful, False otherwise
        """
        if not self.destroy_network():
            return False

        if not self.undefine_network():
            return False

        return True

    def find_available_bridge_name(self) -> str:
        """
        Find the first available virbrX name that is not in use.

        Returns:
            The first available virbrX name
        """
        # Get a list of existing bridge interfaces using brctl
        try:
            # Create a shell command to execute brctl
            cmd_executor = LibVirtCommandBase()
            result = cmd_executor.execute("brctl", "show", hide=True, warn=True)

            if not result.ok:
                self.logger.warning("Failed to run brctl show, defaulting to virbr0")
                return "virbr0"

            # Parse brctl output to find existing virbr bridges
            existing_bridges = []
            lines = result.stdout.splitlines()
            if len(lines) > 1:  # Skip header line
                for line in lines[1:]:
                    parts = line.split()
                    if parts and parts[0].startswith('virbr'):
                        existing_bridges.append(parts[0])

            # Find the first unused virbr index
            used_indices = set()
            for bridge in existing_bridges:
                match = re.match(r'virbr(\d+)', bridge)
                if match:
                    used_indices.add(int(match.group(1)))

            # Find the first available index
            index = 0
            while index in used_indices:
                index += 1

            return f"virbr{index}"

        except Exception as e:
            self.logger.error(f"Error finding available bridge name: {e}")
            # Return a default if all else fails
            return "virbr0"

    def apply_route_iptables_rule(self) -> bool:
        """
        Apply iptables rule to allow communication between hosts on a routed network.

        This adds a rule to the FORWARD chain to allow traffic between hosts
        on the same bridge interface.

        Returns:
            True if successful, False otherwise
        """
        if self.forward_mode != 'route':
            return True  # Nothing to do for non-route networks

        try:
            # Build the iptables command
            iptables_cmd = f"iptables -I FORWARD -i {self.bridge_name} -o {self.bridge_name} -j ACCEPT"
            self.logger.info(f"Applying iptables rule for routed network: {iptables_cmd}")

            # Execute the command using shell execution
            result = self.execute_shell(iptables_cmd)

            if result.ok:
                self.logger.info(f"Applied iptables rule for routed network {self.name}")
                return True
            else:
                self.logger.error(f"Failed to apply iptables rule: {result.stderr}")
                return False
        except Exception as e:
            self.logger.error(f"Error applying iptables rule: {e}")
            return False


class NetworkInterface(VirshCommand):
    """
    Class to manage network interfaces for libvirt VMs.
    """

    def __init__(self,
                 vm_name: str,
                 provider_config: Optional[Dict[str, Any]] = None):
        """
        Initialize the network interface manager.

        Args:
            vm_name: Name of the VM to manage interfaces for
            provider_config: Configuration for the libvirt provider
        """
        super().__init__(provider_config)

        #: str: Name of the VM
        self.vm_name = vm_name

        #: logging.Logger: Logger instance
        self.logger = logging.getLogger(__name__)

    def add_interface(self,
                      network_source: str,
                      link_state: str = 'active',
                      mac_address: Optional[str] = None,
                      model: str = 'virtio') -> bool:
        """
        Add a network interface to the VM.

        Args:
            network_source: Name of the network to attach to
            link_state: State of the link ('active' or 'inactive')
            mac_address: Optional MAC address for the interface
            model: NIC model (default: virtio)

        Returns:
            True if successful, False otherwise
        """

        try:
            # Get the path to the assets directory
            assets_path = pkg_resources.resource_filename('boxman', 'assets')

            # Create a Jinja environment
            env = Environment(
                loader=FileSystemLoader(assets_path),
                trim_blocks=True,
                lstrip_blocks=True
            )

            # Load the template
            template = env.get_template('network_interface.xml.j2')

            # Render the template with the interface configuration
            context = {
                'network_source': network_source,
                'link_state': link_state,
                'mac_address': mac_address,
                'model': model
            }

            xml_content = template.render(**context)

            # Create a temporary file to store the XML
            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            # Use virsh to attach the interface
            self.execute("attach-device", self.vm_name, temp_path, "--persistent")

            # Remove temporary file
            os.unlink(temp_path)
            self.logger.info(f"Added network interface to VM {self.vm_name}: network={network_source}, model={model}")
            return True
        except Exception as e:
            import traceback
            self.logger.error(f"Error adding network interface to VM {self.vm_name}: {e}")
            self.logger.debug(traceback.format_exc())

            # Clean up temp file if it exists
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
            return False

    def configure_from_config(self, adapter_config: Dict[str, Any]) -> bool:
        """
        Configure a network interface from configuration.

        Args:
            adapter_config: Dictionary with network adapter configuration

        Returns:
            True if successful, False otherwise
        """
        network_source = adapter_config['network_source']
        link_state = adapter_config['link_state']
        mac_address = adapter_config.get('mac', None)
        model = adapter_config.get('model', 'virtio')

        return self.add_interface(
            network_source=network_source,
            link_state=link_state,
            mac_address=mac_address,
            model=model
        )
