import os
import uuid
import re
import pkg_resources
from typing import Optional, Dict, Any, Union

import tempfile
from jinja2 import Template, Environment, FileSystemLoader

from .commands import VirshCommand, LibVirtCommandBase

from boxman import log

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
        super().__init__(provider_config=provider_config)

        #: str: the name of the network
        self.name = name

        #: str: the uuid of the network, generated if not provided
        self.uuid_val = str(uuid.uuid4())

        #: str: the forward mode (nat, route, bridge, etc.)
        self.forward_mode = info.get('mode', 'nat')

        # extract bridge configuration
        bridge_info = info.get('bridge', {})

        # handle the bridge name - if not specified, find the first available virbrX
        bridge_name = bridge_info.get('name')
        if not bridge_name:
            bridge_name = self.find_available_bridge_name()

        #: str: the name of the bridge interface
        self.bridge_name = bridge_name

        #: str: use stp on/off for the bridge
        self.bridge_stp = bridge_info.get('stp', 'on')
        #: str: they delay for the stp
        self.bridge_delay = bridge_info.get('delay', '0')

        #: str: set the mac address for the bridge
        self.mac_address = info.get(
            'mac', f"52:54:00:{':'.join(['%02x' % (i + 10) for i in range(3)])}")

        # extract the ip configuration
        ip_info = info.get('ip', {})
        #: str: the ip address for the network
        self.ip_address = ip_info.get('address', '192.168.122.1')
        #: str: the netmask for the network
        self.netmask = ip_info.get('netmask', '255.255.255.0')

        # extract the dhcp configuration
        dhcp_info = ip_info.get('dhcp', {}).get('range', {})
        #: str: the start of DHCP range
        self.dhcp_range_start = dhcp_info.get('start', '192.168.122.2')
        #: str: the end of DHCP range
        self.dhcp_range_end = dhcp_info.get('end', '192.168.122.254')

        #: bool: whether the network should be enabled
        self.enable = info.get('enable', True)

        #: str: the network cidr notation
        self.network = info.get('network', '')

    def generate_xml(self) -> str:
        """
        Generate the XML for the network definition using a Jinja2 template.

        Returns:
            XML string for the network definition
        """
        # get the path to the assets directory
        assets_path = pkg_resources.resource_filename('boxman', 'assets')

        # create a jinja environment
        env = Environment(
            loader=FileSystemLoader(assets_path),
            trim_blocks=True,
            lstrip_blocks=True
        )

        # load the template
        template = env.get_template('network.xml.j2')

        # render the template with the network configuration
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

        # define the network
        try:
            self.execute("net-define", file_path)
            self.execute("net-start", self.name)
            self.execute("net-autostart", self.name)

            # apply appropriate network configuration based on type
            if self.forward_mode == 'route':
                self.apply_route_iptables_rule()
            elif self.forward_mode == 'nat':
                self.apply_nat_config()
            else:
                raise RuntimeError(f"Unsupported forward mode: {self.forward_mode}")

            return True
        except RuntimeError as e:
            self.logger.error(f"Error defining network: {e}")
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
            # check if network exists first
            result = self.execute("net-list", "--all", "| grep -q " + self.name, warn=True)
            if result.return_code != 0:
                self.logger.info(f"Network {self.name} does not exist, nothing to destroy")
                return True

            # check if network is active
            result = self.execute("net-list", "| grep -q " + self.name, warn=True)
            if result.return_code == 0:
                # Network is active, stop it
                self.execute("net-destroy", self.name)
                self.logger.info(f"network {self.name} destroyed successfully")

            return True
        except RuntimeError as exc:
            self.logger.error(f"Error destroying network: {exc}")
            return False

    def undefine_network(self) -> bool:
        """
        Undefine (remove definition of) the network.

        Returns:
            True if successful, False otherwise
        """
        try:
            # check if network exists
            result = self.execute("net-list", "--all", "| grep -q " + self.name, warn=True)
            if result.return_code != 0:
                self.logger.info(f"Network {self.name} does not exist, nothing to undefine")
                return True

            # disable autostart first if it's enabled
            self.execute("net-autostart", self.name, "--disable", warn=True)

            # undefine the network
            self.execute("net-undefine", self.name)
            self.logger.info(f"network {self.name} undefined successfully")
            return True
        except RuntimeError as e:
            self.logger.error(f"Error un-defining network: {e}")
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
        # get a list of existing bridge interfaces using brctl
        try:
            # create a shell command to execute brctl
            cmd_executor = LibVirtCommandBase()
            result = cmd_executor.execute("brctl", "show", hide=True, warn=True)

            if not result.ok:
                self.logger.warning("Failed to run brctl show, defaulting to virbr0")
                return "virbr0"

            # parse brctl output to find existing virbr bridges
            existing_bridges = []
            lines = result.stdout.splitlines()
            if len(lines) > 1:  # Skip header line
                for line in lines[1:]:
                    parts = line.split()
                    if parts and parts[0].startswith('virbr'):
                        existing_bridges.append(parts[0])

            # find the first unused virbr index
            used_indices = set()
            for bridge in existing_bridges:
                match = re.match(r'virbr(\d+)', bridge)
                if match:
                    used_indices.add(int(match.group(1)))

            # find the first available index
            index = 0
            while index in used_indices:
                index += 1

            return f"virbr{index}"

        except Exception as exc:
            self.logger.error(f"Error finding available bridge name: {exc}")
            # return a default if all else fails
            return "virbr0"

    def apply_route_iptables_rule(self) -> bool:
        """
        Apply iptables rules for truly isolated routed networks.

        This method configures iptables to:
        1. Allow VM-to-VM communication on the same bridge
        2. Block ALL traffic between host and guests in both directions

        Returns:
            True if successful, False otherwise
        """
        if self.forward_mode != 'route':
            return True  # Nothing to do for non-route networks

        try:
            # get the bridge interface name
            bridge_name = self.bridge_name
            self.logger.info(
                f"configuring complete isolation for routed network with bridge {bridge_name}")

            # 1. allow vm-to-vm communication on the same bridge
            vm_to_vm_cmd = f"sudo iptables -I FORWARD -i {bridge_name} -o {bridge_name} -j ACCEPT"
            self.logger.info(f"setting up vm-to-vm communication: {vm_to_vm_cmd}")
            result = self.execute_shell(vm_to_vm_cmd)
            if not result.ok:
                self.logger.error(f"failed to allow VM-to-VM communication: {result.stderr}")
                return False

            # 2. block all traffic from the vms to the host
            host_to_vm_block = f"sudo iptables -I INPUT -i {bridge_name} -j DROP"
            self.logger.info(f"blocking all traffic from the vms to the host: {host_to_vm_block}")
            result = self.execute_shell(host_to_vm_block)
            if not result.ok:
                self.logger.error(f"failed to block vm to host traffic: {result.stderr}")
                return False

            # 3. block ALL traffic from host to the vms
            vm_to_host_block = f"sudo iptables -I OUTPUT -o {bridge_name} -j DROP"
            self.logger.info(f"blocking all traffic from host to the vms: {vm_to_host_block}")
            result = self.execute_shell(vm_to_host_block)
            if not result.ok:
                self.logger.error(f"failed to block host to vm traffic: {result.stderr}")
                return False

            self.logger.info(
                f"successfully applied complete isolation for routed network {self.name}")
            return True

        except Exception as exc:
            self.logger.error(f"error applying route isolation rules: {exc}")
            return False

    def apply_nat_config(self) -> bool:
        """
        Apply NAT configuration for networks with forward mode 'nat'.

        This method:
        1. Finds the outgoing interface (eth0, wlan0, etc.)
        2. Gets the bridge name for this network
        3. Allows forwarding between the bridge and outgoing interface
        4. Enables IP masquerading for the network

        Returns:
            True if successful, False otherwise
        """
        try:
            # step 1: find the outgoing interface
            cmd_executor = LibVirtCommandBase(self.provider_config)

            # get the outgoing interface using 'ip route'
            # .. todo:: figure out how to get the default route interface in case the host
            #           is not connected to the internet. This should work even if the host is not
            #           connected to the internet.
            result = cmd_executor.execute_shell("ip route get 8.8.8.8 | awk '{print $5}'", hide=True)
            if not result.ok:
                self.logger.error(f"failed to find outgoing interface: {result.stderr}")
                return False

            outgoing_iface = result.stdout.strip()
            if not outgoing_iface:
                self.logger.error("could not determine outgoing interface")
                return False

            self.logger.info(f"found outgoing interface: {outgoing_iface}")

            # step 2: bridge interface is already known (self.bridge_name)
            self.logger.info(f"using bridge interface: {self.bridge_name}")

            # step 3: allow forwarding between interfaces
            # forward outgoing -> bridge
            cmd = f"sudo iptables -I FORWARD -i {outgoing_iface} -o {self.bridge_name} -j ACCEPT"
            result = cmd_executor.execute_shell(cmd)
            if not result.ok:
                self.logger.error(f"failed to set up forwarding from {outgoing_iface} to {self.bridge_name}: {result.stderr}")
                return False

            # forward bridge -> outgoing
            cmd = f"sudo iptables -I FORWARD -i {self.bridge_name} -o {outgoing_iface} -j ACCEPT"
            result = cmd_executor.execute_shell(cmd)
            if not result.ok:
                self.logger.error(f"failed to set up forwarding from {self.bridge_name} to {outgoing_iface}: {result.stderr}")
                return False

            # step 4: enable NAT for the virtual network
            # extract network address from IP and netmask
            import ipaddress
            try:
                ip_interface = ipaddress.IPv4Interface(f"{self.ip_address}/{self.netmask}")
                network_cidr = str(ip_interface.network)

                # Add masquerade rule
                cmd = f"sudo iptables -t nat -A POSTROUTING -s {network_cidr} -j MASQUERADE"
                result = cmd_executor.execute_shell(cmd)
                if not result.ok:
                    self.logger.error(
                        f"failed to set up masquerading for {network_cidr}: {result.stderr}")
                    return False

                self.logger.info(
                    f"successfully configured nat for network {self.name} ({network_cidr})")
                return True

            except ValueError as exc:
                self.logger.error(f"error calculating network cidr: {exc}")
                return False

        except Exception as exc:
            self.logger.error(f"error configuring NAT for network {self.name}: {exc}")
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
        super().__init__(provider_config=provider_config)

        #: str: Name of the VM
        self.vm_name = vm_name

        #: logging.Logger: Logger instance
        self.logger = log

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
            # get the path to the assets directory
            assets_path = pkg_resources.resource_filename('boxman', 'assets')

            # create a jinja environment
            env = Environment(
                loader=FileSystemLoader(assets_path),
                trim_blocks=True,
                lstrip_blocks=True
            )

            # load the template
            template = env.get_template('network_interface.xml.j2')

            # render the template with the interface configuration
            context = {
                'network_source': network_source,
                'link_state': link_state,
                'mac_address': mac_address,
                'model': model
            }

            xml_content = template.render(**context)

            # create a temporary file to store the XML
            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            # use virsh to attach the interface
            self.execute("attach-device", self.vm_name, temp_path, "--persistent")

            # remove temporary file
            os.unlink(temp_path)
            self.logger.info(
                f"added network interface to vm "
                f"{self.vm_name}: network={network_source}, model={model}")
            return True
        except Exception as exc:
            import traceback
            self.logger.error(f"error adding network interface to vm {self.vm_name}: {exc}")
            self.logger.debug(traceback.format_exc())

            # clean up temp file if it exists
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
