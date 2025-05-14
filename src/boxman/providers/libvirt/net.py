import os
import uuid
import re
import pkg_resources
from typing import Optional, Dict, Any, Union, List

import tempfile
from jinja2 import Template, Environment, FileSystemLoader
import xml.etree.ElementTree as ET

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

        if bridge_name := self.get_bridge_from_network(name):
            pass
        else:
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
        except RuntimeError as exc:
            self.logger.error(f"Error defining network: {exc}")
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
                self.logger.info(f"network {self.name} does not exist, nothing to destroy")
                return True

            # check if network is active
            result = self.execute("net-list", "| grep -q " + self.name, warn=True)
            if result.return_code == 0:
                # the network is active, stop it
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

        # remove iptables rules if any
        if self.forward_mode == 'route':
            self.remove_route_iptables_rule()
        elif self.forward_mode == 'nat':
            self.remove_nat_config()
        else:
            raise RuntimeError(f"Unsupported forward mode: {self.forward_mode}")

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
            cmd_executor = LibVirtCommandBase(
                provider_config=self.provider_config,
                override_config_use_sudo=False)
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

    @staticmethod
    def _ensure_rule(cls,
                     check_cmd: str,
                     action_cmd: str,
                     present: bool = True) -> bool:
        """
        Make sure a rule is either present (present=True) or absent (present=False).

        Args:
            instance   : object exposing execute_shell & logger
            check_cmd  : iptables -C ... command used to probe rule existence
            action_cmd : command that adds the rule (present) or deletes the rule (absent)
            present    : True -> ensure rule exists, False -> ensure rule is removed
        """
        chk_res = cls.execute_shell(check_cmd, warn=True)

        # desired state already reached
        if (present and chk_res.return_code == 0) or (not present and chk_res.return_code != 0):
            cls.logger.debug(f"rule already in desired state: {check_cmd}")
            return True

        # need an action to reach desired state
        apply_res = cls.execute_shell(action_cmd, warn=True)
        if not apply_res.ok:
            cls.logger.error(f"failed to execute '{action_cmd}': {apply_res.stderr}")
            return False
        return True

    def remove_route_iptables_rule(self) -> bool:
        """
        Remove the isolation rules inserted by apply_route_iptables_rule.
        Executed during `remove_network`.  Follows the same check-then-execute
        pattern used in apply_route_iptables_rule.
        """
        if self.forward_mode != 'route':
            return True

        try:
            br_name = self.bridge_name
            self.logger.info(f"removing route isolation rules for bridge {br_name}")

            if not self._ensure_rule(
                    self,
                    f"sudo iptables -C FORWARD -i {br_name} -o {br_name} -j ACCEPT",
                    f"sudo iptables -D FORWARD -i {br_name} -o {br_name} -j ACCEPT",
                    present=False):
                return False
            if not self._ensure_rule(
                    self,
                    f"sudo iptables -C INPUT  -i {br_name} -j DROP",
                    f"sudo iptables -D INPUT  -i {br_name} -j DROP",
                    present=False):
                return False
            if not self._ensure_rule(
                    self,
                    f"sudo iptables -C OUTPUT -o {br_name} -j DROP",
                    f"sudo iptables -D OUTPUT -o {br_name} -j DROP",
                    present=False):
                return False

            self.logger.info(f"successfully removed isolation rules for routed network {self.name}")
            return True
        except Exception as exc:
            self.logger.error(f"error removing route isolation rules: {exc}")
            return False

    def remove_nat_config(self) -> bool:
        """
        Remove the forwarding and masquerade rules inserted by apply_nat_config.
        """
        if self.forward_mode != 'nat':
            return True

        try:
            # discover outgoing iface (same logic as insertion)
            cmd_exec = LibVirtCommandBase(provider_config=self.provider_config,
                                          override_config_use_sudo=False)
            res = cmd_exec.execute_shell("ip route get 8.8.8.8 | awk '{print $5}'",
                                         hide=True, warn=True)
            out_iface = res.stdout.strip() if res.ok else ""
            bridge_name = self.bridge_name

            if out_iface:
                self._ensure_rule(
                    self,
                    f"sudo iptables -C FORWARD -i {out_iface} -o {bridge_name} -j ACCEPT",
                    f"sudo iptables -D FORWARD -i {out_iface} -o {bridge_name} -j ACCEPT",
                    present=False)
                self._ensure_rule(
                    self,
                    f"sudo iptables -C FORWARD -i {bridge_name} -o {out_iface} -j ACCEPT",
                    f"sudo iptables -D FORWARD -i {bridge_name} -o {out_iface} -j ACCEPT",
                    present=False)
            else:
                self.logger.warning("could not determine outgoing iface while cleaning nat rules")

            # remove masquerade
            import ipaddress
            try:
                net_cidr = str(ipaddress.IPv4Interface(f"{self.ip_address}/{self.netmask}").network)
                self._ensure_rule(
                    self,
                    f"sudo iptables -t nat -C POSTROUTING -s {net_cidr} -j MASQUERADE",
                    f"sudo iptables -t nat -D POSTROUTING -s {net_cidr} -j MASQUERADE",
                    present=False)
            except ValueError:
                self.logger.warning("could not compute network cidr while cleaning masquerade rule")

            return True
        except Exception as exc:
            self.logger.error(f"error removing NAT configuration for {self.name}: {exc}")
            return False

    def apply_route_iptables_rule(self) -> bool:
        """
        Apply iptables rules for truly isolated routed networks.

        This method configures iptables to:
        1. Allow vm-to-vm communication on the same bridge
        2. Block all traffic between host and guests in both directions

        Returns:
            True if successful, False otherwise
        """
        if self.forward_mode != 'route':
            return True  # Nothing to do for non-route networks

        try:
            bridge_name = self.bridge_name
            self.logger.info(
                f"configuring complete isolation for routed network with bridge {bridge_name}")

            # 1. allow vm-to-vm communication on the same bridge
            vm2vm_check = f"sudo iptables -C FORWARD -i {bridge_name} -o {bridge_name} -j ACCEPT"
            vm2vm_cmd   = f"sudo iptables -I FORWARD -i {bridge_name} -o {bridge_name} -j ACCEPT"
            if not self._ensure_rule(self, vm2vm_check, vm2vm_cmd):
                return False

            # 2. block all traffic from the VMs to the host
            host2vm_check = f"sudo iptables -C INPUT -i {bridge_name} -j DROP"
            host2vm_cmd   = f"sudo iptables -I INPUT -i {bridge_name} -j DROP"
            if not self._ensure_rule(self, host2vm_check, host2vm_cmd):
                return False

            # 3. block all traffic from host to the VMs
            vm2host_check = f"sudo iptables -C OUTPUT -o {bridge_name} -j DROP"
            vm2host_cmd   = f"sudo iptables -I OUTPUT -o {bridge_name} -j DROP"
            if not self._ensure_rule(self, vm2host_check, vm2host_cmd):
                return False

            self.logger.info(f"successfully applied complete isolation for routed network {self.name}")
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
            cmd_executor = LibVirtCommandBase(
                provider_config=self.provider_config,
                override_config_use_sudo=False)

            # get the outgoing interface using 'ip route'
            # .. todo:: figure out how to get the default route interface in case the host
            #           is not connected to the internet. This should work even if the host is not
            #           connected to the internet.
            cmd = "ip route get 8.8.8.8 | awk '{print $5}'"
            result = cmd_executor.execute_shell(cmd, hide=True)
            if not result.ok:
                self.logger.error(f"failed to find outgoing interface: {result.stderr}")
                return False

            bridge = self.bridge_name

            out_iface = result.stdout.strip()
            if not out_iface:
                self.logger.error("could not determine outgoing interface")
                return False

            self.logger.info(f"found outgoing interface: {out_iface}")

            # step 2: bridge interface is already known (self.bridge_name)
            self.logger.info(f"using bridge interface: {bridge}")

            # 3. allow forwarding between interfaces
            fwd1_check = f"sudo iptables -C FORWARD -i {out_iface} -o {bridge} -j ACCEPT"
            fwd1_cmd   = f"sudo iptables -I FORWARD -i {out_iface} -o {bridge} -j ACCEPT"
            if not self._ensure_rule(self, fwd1_check, fwd1_cmd):
                return False

            fwd2_check = f"sudo iptables -C FORWARD -i {bridge} -o {out_iface} -j ACCEPT"
            fwd2_cmd   = f"sudo iptables -I FORWARD -i {bridge} -o {out_iface} -j ACCEPT"
            if not self._ensure_rule(self, fwd2_check, fwd2_cmd):
                return False

            # 4. enable nat for the virtual network
            import ipaddress
            try:
                ip_interface = ipaddress.IPv4Interface(f"{self.ip_address}/{self.netmask}")
                network_cidr = str(ip_interface.network)

                masq_check = f"sudo iptables -t nat -C POSTROUTING -s {network_cidr} -j MASQUERADE"
                masq_cmd   = f"sudo iptables -t nat -A POSTROUTING -s {network_cidr} -j MASQUERADE"
                if not self._ensure_rule(self, masq_check, masq_cmd):
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

    @staticmethod
    def get_bridge_from_network(network_name: str,
                                provider_config: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """
        Fetch the bridge name of a network

        An xml dump of the network is obtained using virsh, and the bridge name
        is extracted from the xml.

        Args:
            network_name    : the libvirt network to inspect
            provider_config : optional provider config (sudo, uri, ...)

        Returns:
            The bridge interface name (e.g. 'virbr0') or None on failure.
        """
        try:
            virsh = VirshCommand(provider_config=provider_config)

            # obtain the xml definition
            result = virsh.execute("net-dumpxml", network_name)
            if not result.ok:
                log.error(f"failed to dump XML for network {network_name}: {result.stderr}")
                return None

            # write to a temporary file so that we exactly follow the 'dump -> read-back' wording
            with tempfile.NamedTemporaryFile(mode="w+", suffix=".xml", delete=False) as tmp:
                tmp.write(result.stdout)
                tmp_path = tmp.name

            # parse the xml and extract the bridge name
            bridge_name = None
            try:
                tree = ET.parse(tmp_path)
                bridge_elem = tree.find("./bridge")
                if bridge_elem is not None:
                    bridge_name = bridge_elem.attrib.get("name")
            finally:
                os.unlink(tmp_path)  # clean up temp file

            return bridge_name
        except Exception as exc:
            log.error(f"error getting bridge for network {network_name}: {exc}")
            return None

    @staticmethod
    def list_networks(provider_config: Optional[Dict[str, Any]] = None,
                      active_only: bool = False) -> List[str]:
        """
        Return the names of libvirt networks.

        Args:
            provider_config: Optionally forward the provider configuration
            active_only    : If True, list only active networks; otherwise '--all' is used.

        Returns:
            A list with network names. Returns an empty list on failure.
        """
        try:
            virsh = VirshCommand(provider_config=provider_config)
            cmd_parts = ["net-list"]
            if not active_only:
                cmd_parts.append("--all")
            cmd_parts.append("--name")          # names only for easy parsing

            result = virsh.execute(*cmd_parts, warn=True)
            if not result.ok:
                log.error(f"failed to list networks: {result.stderr}")
                return []

            # filter out empty lines that may appear in the output
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except Exception as exc:
            log.error(f"error listing networks: {exc}")
            return []


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

    def configure_from_config(self,
                              adapter_config: Dict[str, Any]) -> bool:
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
