import os
import uuid
import pkg_resources
from typing import Optional, Dict, Any, Union

from jinja2 import Template, Environment, FileSystemLoader
import invoke


class Network:
    """
    Class to define libvirt networks by creating XML definitions and using virsh commands.
    """

    def __init__(self, name: str, info: Dict[str, Any]):
        """
        Initialize the network definition with a dictionary-based configuration.

        Args:
            name: Name of the network
            info: Dictionary containing network configuration with keys like:
                 mode, bridge, mac, ip, network, enable, etc.
        """
        #: dict: The provider configuration
        self.provider_config = {}

        #: str: Name of the network
        self.name = name

        #: str: UUID of the network, generated if not provided
        self.uuid_val = str(uuid.uuid4())

        #: str: Forward mode (nat, route, bridge, etc.)
        self.forward_mode = info.get('mode', 'nat')

        # Extract bridge configuration
        bridge_info = info.get('bridge', {})
        #: str: Bridge name
        self.bridge_name = bridge_info.get('name', f"virbr{self.name[-1] if name[-1].isdigit() else '0'}")
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

        # run the following commands to define and start the networks
        cmds = [
            f"sudo virsh net-define {file_path}",
            f"sudo virsh net-start {self.name}",
            f"sudo virsh net-autostart {self.name}"
        ]

        for cmd in cmds:
            try:
                result = invoke.run(cmd, hide=True)
                if result.ok:
                    print(f"Network {self.name} defined successfully")
                else:
                    print(f"Failed to define network: {result.stderr}")
            except invoke.exceptions.UnexpectedExit as exc:
                msg = (
                    f"Error defining network: {exc}\n"
                    f"Command: {cmd}\n"
                    f"Exit code: {exc.result.return_code}\n"
                    f"Stdout: {exc.result.stdout}\n"
                    f"Stderr: {exc.result.stderr}"
                )
                raise RuntimeError(msg)

    def start_network(self) -> bool:
        """
        Start the defined network.

        Returns:
            True if successful, False otherwise
        """
        try:
            result = invoke.run(f"sudo virsh net-start {self.name}", hide=True)
            if result.ok:
                print(f"Network {self.name} started successfully")
                return True
            else:
                print(f"Failed to start network: {result.stderr}")
                return False
        except invoke.exceptions.UnexpectedExit as e:
            print(f"Error starting network: {e}")
            return False

    def autostart_network(self) -> bool:
        """
        Set the network to autostart.

        Returns:
            True if successful, False otherwise
        """
        try:
            result = invoke.run(f"sudo virsh net-autostart {self.name}", hide=True)
            if result.ok:
                print(f"Network {self.name} set to autostart")
                return True
            else:
                print(f"Failed to set network to autostart: {result.stderr}")
                return False
        except invoke.exceptions.UnexpectedExit as e:
            print(f"Error setting network to autostart: {e}")
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
            result = invoke.run(f"sudo virsh net-list --all | grep -q {self.name}", hide=True, warn=True)
            if result.return_code != 0:
                print(f"Network {self.name} does not exist, nothing to destroy")
                return True

            # Check if network is active
            result = invoke.run(f"sudo virsh net-list | grep -q {self.name}", hide=True, warn=True)
            if result.return_code == 0:
                # Network is active, stop it
                result = invoke.run(f"sudo virsh net-destroy {self.name}", hide=True)
                if not result.ok:
                    print(f"Failed to destroy network {self.name}: {result.stderr}")
                    return False
                print(f"Network {self.name} destroyed successfully")

            return True
        except invoke.exceptions.UnexpectedExit as e:
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
            result = invoke.run(f"sudo virsh net-list --all | grep -q {self.name}", hide=True, warn=True)
            if result.return_code != 0:
                print(f"Network {self.name} does not exist, nothing to undefine")
                return True

            # Disable autostart first if it's enabled
            result = invoke.run(f"sudo virsh net-autostart {self.name} --disable", hide=True, warn=True)

            # Undefine the network
            result = invoke.run(f"sudo virsh net-undefine {self.name}", hide=True)
            if result.ok:
                print(f"Network {self.name} undefined successfully")
                return True
            else:
                print(f"Failed to undefine network {self.name}: {result.stderr}")
                return False
        except invoke.exceptions.UnexpectedExit as e:
            print(f"Error undefining network: {e}")
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