import copy
import ipaddress
import logging
import os
import re
import shlex
import tempfile
import uuid
import xml.etree.ElementTree as ET
from importlib import resources as importlib_resources
from typing import Any
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader

from boxman import log
from boxman.exceptions import ConfigError
from boxman.netlab.shared_bridges import BRIDGE_NAME_RE

from . import net_reconcile
from .commands import VirshCommand
from .virsh_parse import parse_domiflist


class Network:
    """
    Class to define libvirt networks by creating XML definitions and using virsh commands.
    """
    SUPPORTED_FORWARD_MODES = frozenset({'nat', 'route', 'bridge'})

    def __init__(self,
                name: str,
                info: dict[str, Any],
                assign_new_bridge: bool = True,
                provider_config: dict[str, Any] | None = None,
                manager = None):
        """
        Initialize the network definition with a dictionary-based configuration.

        Args:
            name: Name of the network
            info: Dictionary containing network configuration with keys like:
                 mode, bridge, mac, ip, network, enable, etc.
            assign_new_bridge: Whether to assign a bridge name automatically
            provider_config: Configuration for the libvirt provider
            cache: Optional cache object for storing network definitions
        """
        #: VirshCommand: Command executor for virsh
        self.virsh = VirshCommand(provider_config=provider_config)

        #: Dict[str, Any]: Configuration for the libvirt provider
        self.provider_config = provider_config or {}

        #: logging.Logger: Logger instance
        self.logger = log

        #: str: the name of the network
        self.name = name

        #: dict: original network block, retained for presence validation
        self.info = info

        #: str: the uuid of the network, generated if not provided
        self.uuid_val = str(uuid.uuid4())

        #: str: the forward mode (nat, route, bridge, etc.)
        self.forward_mode = info.get('mode', 'nat')

        # extract bridge configuration
        self._raw_bridge_info = info.get('bridge')
        bridge_info = (self._raw_bridge_info
                       if isinstance(self._raw_bridge_info, dict) else {})

        #: the cache object to be used to store network information
        self.manager = manager

        #: str | None: the bridge name the configuration pinned, if any. Kept
        #: separate from the one actually in use so that reconciliation can
        #: tell "the config asked for virbr42" apart from "boxman picked one"
        self.pinned_bridge_name = bridge_info.get('name')

        if self.forward_mode == 'bridge':
            # A bridge-mode libvirt network is only a named indirection to an
            # existing host bridge. Libvirt neither creates nor configures it.
            bridge_name = self.pinned_bridge_name
        elif self.pinned_bridge_name:
            # A configured name is the effective desired name even while
            # inspecting an existing definition. The live name is parsed from
            # net-dumpxml separately during reconciliation.
            bridge_name = self.pinned_bridge_name
        elif assign_new_bridge:
            # No configured name: allocate the first available virbrX.
            bridge_name = self.find_available_bridge_name()
        else:
            if bridge_name := Network.get_bridge_from_network(name, provider_config=self.provider_config):
                self.logger.debug(f"found existing bridge {bridge_name} for network {name}")
            else:
                # not an error: an undefined network has no bridge yet, which
                # is exactly what reconciliation asks about before creating one
                bridge_name = self.pinned_bridge_name
                self.logger.debug(
                    f"no bridge in libvirt for network {name} "
                    f"(it is not defined yet)")

        #: str: the name of the bridge interface
        self.bridge_name = bridge_name

        #: str: use stp on/off for the bridge
        self.bridge_stp = (None if self.forward_mode == 'bridge' else
                           self._normalise_stp(bridge_info.get('stp', 'on')))
        #: str: the delay for the stp
        self.bridge_delay = (None if self.forward_mode == 'bridge' else
                             str(bridge_info.get('delay', '0')))

        #: str: set the mac address for the bridge
        #: canonicalised because libvirt zero-pads this one when it stores it
        #: (52:54:0:a:b:c comes back as 52:54:00:0a:0b:0c). Left short, the
        #: configuration and the live network never match, and reconciliation
        #: would rebuild the network -- rebooting its guests -- on every run.
        #: Reservation macs are deliberately *not* padded: libvirt stores those
        #: verbatim, so padding them would create the mismatch instead
        self.mac_address = None if self.forward_mode == 'bridge' else \
            self._canonical_mac(info.get(
                'mac', f"52:54:00:{':'.join(['%02x' % (i + 10) for i in range(3)])}"))

        #: bool: whether the ip has been provided or dummy values are injected
        self.ip_provided = 'ip' in info

        #: str: the ip address for the network
        self.ip_address = None

        #: str: the netmask for the network
        self.netmask = None

        #: str: the start of dhcp range
        self.dhcp_range_start = None

        #: str: the end of dhcp range
        self.dhcp_range_end = None

        #: list: static dhcp reservations, each a dict of mac and ip, plus an
        #: optional name; absent from the dict when it was not configured
        self.dhcp_hosts = []

        # extract the ip configuration. If the 'ip' key is not present then the
        # otherwise mandatory keys will be set to default values. These are:
        #  - ip_address
        #  - netmask
        #  - dhcp_range_start
        #  - dhcp_range_end
        # `or {}` throughout: yaml turns an empty or explicitly null block
        # (`ip:`, `dhcp: null`) into None rather than into a missing key, and a
        # bare .get() default does not cover that
        # Every ``ip`` field is forbidden in bridge mode, so do not descend
        # into a malformed nested value before validation can report the
        # useful bridge-mode error.
        ip_info = ({} if self.forward_mode == 'bridge'
                   else info.get('ip') or {})

        self.ip_address = (None if self.forward_mode == 'bridge' else
                           ip_info.get('address', '192.168.254.1'))
        self.netmask = (None if self.forward_mode == 'bridge' else
                        ip_info.get('netmask', '255.255.255.0'))

        dhcp_conf = ip_info.get('dhcp') or {}
        dhcp_info = dhcp_conf.get('range') or {}
        self.dhcp_range_start = dhcp_info.get('start', None)
        self.dhcp_range_end = dhcp_info.get('end', None)

        # a reservation may sit inside or outside the dynamic range: dnsmasq
        # excludes reserved addresses from the pool either way. keeping them
        # outside is still the clearer convention
        self.dhcp_hosts = ([] if self.forward_mode == 'bridge' else
                           self._parse_dhcp_hosts(dhcp_conf.get('hosts') or []))

        #: bool: whether the network should be enabled
        self.enable = info.get('enable', True)

    def validate_definition(self) -> None:
        """Validate fields needed to define or reconcile this network.

        Kept out of ``__init__`` deliberately: removal needs only the libvirt
        network name and its old forward mode. Older Boxman versions could
        leave an otherwise unsupported ``open``/``isolated`` network defined
        before reporting failure; ``boxman destroy`` must still be able to
        construct that network and clean it up.
        """
        if self.forward_mode not in self.SUPPORTED_FORWARD_MODES:
            supported = ', '.join(sorted(self.SUPPORTED_FORWARD_MODES))
            raise ConfigError(
                f"network {self.name}: unsupported forward mode "
                f"{self.forward_mode!r}; supported modes: {supported}")

        if (self._raw_bridge_info is not None
                and not isinstance(self._raw_bridge_info, dict)):
            raise ConfigError(
                f"network {self.name}: bridge must be a mapping")

        if self.forward_mode != 'bridge':
            return

        bridge_info = self._raw_bridge_info or {}
        bridge_name = bridge_info.get('name')
        if not bridge_name:
            raise ConfigError(
                f"network {self.name}: mode 'bridge' requires an existing "
                "Linux bridge name at bridge.name")
        if (not isinstance(bridge_name, str)
                or not BRIDGE_NAME_RE.fullmatch(bridge_name)):
            raise ConfigError(
                f"network {self.name}: invalid bridge name {bridge_name!r}: "
                f"must match {BRIDGE_NAME_RE.pattern}")

        forbidden = []
        if 'ip' in self.info:
            forbidden.append('ip')
        if 'mac' in self.info:
            forbidden.append('mac')
        forbidden.extend(
            f"bridge.{key}" for key in ('stp', 'delay')
            if key in bridge_info)
        if forbidden:
            raise ConfigError(
                f"network {self.name}: mode 'bridge' uses addressing and "
                "link settings from the existing host bridge; remove: "
                + ', '.join(forbidden))

    def validate_runtime_prerequisites(self) -> None:
        """Verify runtime-owned resources needed by this definition exist.

        A raw sysfs probe is authoritative only when the libvirt URI has no
        network authority (``qemu:///system``, ``test:///default``, or a local
        unix socket). Any authority-bearing URI -- even
        ``qemu://localhost/system`` -- may resolve through a transport or
        daemon namespace different from the Boxman process. In that case skip
        client-side sysfs and let the endpoint's libvirt daemon validate the
        bridge at ``net-start`` instead.

        For a directly-addressed daemon, validate the administrative ``IFF_UP``
        flag. ``operstate`` is deliberately not used: a perfectly usable bridge
        with no attached ports commonly reports ``unknown``.
        """
        if self.forward_mode != 'bridge':
            return

        if urlsplit(self.virsh.uri).netloc:
            self.logger.debug(
                f"network {self.name}: deferring bridge {self.bridge_name!r} "
                "validation to the authority-bearing libvirt endpoint")
            return

        bridge_path = shlex.quote(
            f"/sys/class/net/{self.bridge_name}/bridge")
        result = self.virsh.execute_shell(
            f"test -d {bridge_path}", hide=True, warn=True)
        if not result.ok:
            raise ConfigError(
                f"network {self.name}: Linux bridge "
                f"{self.bridge_name!r} does not exist in the active "
                "runtime; create it and bring it up before running Boxman")

        flags_path = shlex.quote(f"/sys/class/net/{self.bridge_name}/flags")
        flags_result = self.virsh.execute_shell(
            f"cat {flags_path}", hide=True, warn=True)
        try:
            flags = int(flags_result.stdout.strip(), 0) if flags_result.ok else None
        except (AttributeError, ValueError):
            flags = None
        if flags is None:
            raise ConfigError(
                f"network {self.name}: could not read administrative state "
                f"for Linux bridge {self.bridge_name!r} in the active runtime")
        if not flags & 0x1:  # Linux IFF_UP
            raise ConfigError(
                f"network {self.name}: Linux bridge {self.bridge_name!r} "
                "exists but is administratively down in the active runtime; "
                f"run: sudo ip link set dev {self.bridge_name} up")

    @staticmethod
    def _canonical_mac(value: str) -> str:
        """
        Zero-pad and lowercase a mac, the way libvirt stores a network's own.

        Left alone if it does not look like six hex groups, so that anything
        unexpected still reaches libvirt and gets libvirt's own error rather
        than being silently mangled here.
        """
        groups = str(value).split(':')
        if len(groups) != 6:
            return str(value).lower()
        try:
            return ':'.join(f"{int(group, 16):02x}" for group in groups)
        except ValueError:
            return str(value).lower()

    @staticmethod
    def _normalise_stp(value: Any) -> str:
        """
        Coerce a configured ``stp`` value to the ``on``/``off`` libvirt echoes.

        yaml reads an unquoted ``stp: on`` as the boolean True, which would be
        rendered as ``stp='True'``. libvirt accepts that and stores it as
        ``stp='on'`` -- so without this the configuration and the live network
        never agree, and every reconcile reports drift that a recreate cannot
        fix.

        Raises:
            ValueError: on a value that is neither, rather than quietly
                turning a typo like ``stp: enabled`` into ``off``
        """
        if isinstance(value, bool):
            return 'on' if value else 'off'

        text = str(value).strip().lower()
        if text in ('on', 'true', 'yes', '1'):
            return 'on'
        if text in ('off', 'false', 'no', '0'):
            return 'off'
        raise ValueError(
            f"bridge stp must be on or off, got {value!r}")

    def _parse_dhcp_hosts(self, hosts_info: list) -> list:
        """
        Validate and normalise the static dhcp reservations.

        Each entry becomes a ``<host>`` element under ``<dhcp>``, which libvirt
        turns into a dnsmasq ``dhcp-host`` line pinning an address to a mac for
        the life of the network.

        The checks are the ones whose absence produces either a network libvirt
        refuses to define -- with an error that does not say which entry is at
        fault -- or one that defines cleanly and then hands out the wrong
        address.

        Args:
            hosts_info: the raw ``ip.dhcp.hosts`` list from the configuration

        Returns:
            A list of dicts with the keys ``mac``, ``ip`` and optionally
            ``name``, or an empty list when nothing is reserved.

        Raises:
            ValueError: if the list or an entry is malformed, if a reservation
                is incomplete, outside the network, on an address the network
                cannot hand out, or collides with another entry
        """
        if not hosts_info:
            return []

        if not isinstance(hosts_info, list):
            raise ValueError(
                f"network {self.name}: 'ip.dhcp.hosts' must be a list of "
                f"reservations, got {type(hosts_info).__name__}. A single "
                f"reservation still needs its leading '-'")

        try:
            interface = ipaddress.IPv4Interface(
                f"{self.ip_address}/{self.netmask}")
        except ValueError as exc:
            raise ValueError(
                f"network {self.name}: cannot validate the dhcp reservations "
                f"because {self.ip_address}/{self.netmask} is not a valid "
                f"ipv4 network: {exc}") from exc

        network = interface.network

        hosts = []
        seen_macs = {}
        seen_ips = {}
        seen_names = {}

        for entry in hosts_info:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"network {self.name}: a dhcp reservation must be a "
                    f"mapping with 'mac' and 'ip' keys, got {entry!r}")

            mac = entry.get('mac')
            ip = entry.get('ip')

            if not mac or not ip:
                raise ValueError(
                    f"network {self.name}: a dhcp reservation needs both a "
                    f"'mac' and an 'ip', got {entry!r}")

            # libvirt compares these case-insensitively, so normalise before
            # looking for duplicates or the same mac twice in two cases slips
            # through and the last one silently wins
            mac = str(mac).lower()

            # six colon-separated hex groups. libvirt tolerates a group written
            # with a single digit (52:54:0:c:1:1 parses), so this is deliberately
            # looser than the canonical form; it is only here to reject the
            # dash-separated and run-together spellings early
            if not re.fullmatch(r'[0-9a-f]{1,2}(:[0-9a-f]{1,2}){5}', mac):
                raise ValueError(
                    f"network {self.name}: the dhcp reservation for {ip} has "
                    f"a malformed mac {entry.get('mac')!r}, expected six "
                    f"colon-separated hex groups")

            try:
                address = ipaddress.IPv4Address(ip)
            except ValueError as exc:
                raise ValueError(
                    f"network {self.name}: the dhcp reservation for {mac} has "
                    f"an invalid ip {ip!r}: {exc}") from exc

            if address not in network:
                raise ValueError(
                    f"network {self.name}: the dhcp reservation {ip} for {mac} "
                    f"is outside the network {network}")

            # libvirt accepts all three of these and dnsmasq then hands out an
            # address that cannot work: the gateway is the bridge's own address,
            # and the other two are not host addresses at all
            if address == interface.ip:
                raise ValueError(
                    f"network {self.name}: the dhcp reservation {ip} for {mac} "
                    f"is the gateway address of the network itself")

            if address in (network.network_address, network.broadcast_address):
                raise ValueError(
                    f"network {self.name}: the dhcp reservation {ip} for {mac} "
                    f"is the network or broadcast address of {network}")

            # compare and render the parsed form so that the dedup below cannot
            # be fooled by a non-canonical spelling
            ip = str(address)

            if mac in seen_macs:
                raise ValueError(
                    f"network {self.name}: mac {mac} is reserved twice, for "
                    f"{seen_macs[mac]} and {ip}")

            if ip in seen_ips:
                raise ValueError(
                    f"network {self.name}: ip {ip} is reserved twice, for "
                    f"{seen_ips[ip]} and {mac}")

            seen_macs[mac] = ip
            seen_ips[ip] = mac

            host = {'mac': mac, 'ip': ip}

            # optional, and worth setting: it becomes the dnsmasq hostname, so
            # the guest also resolves by name for everything on this network
            name = entry.get('name')
            if isinstance(name, bool):
                # `name: false` would otherwise become the hostname 'False'
                raise ValueError(
                    f"network {self.name}: the dhcp reservation name for "
                    f"{mac} must be a hostname, got the boolean {name!r}")

            if name is not None and str(name) != '':
                # stored as a string: yaml reads `name: 101` as an int, which
                # would never compare equal to the '101' read back out of the
                # network XML, and every reconcile would re-apply it
                name = str(name)

                # libvirt round-trips this name through its own files
                # unescaped, so an xml metacharacter defines cleanly and then
                # breaks `net-start` with a parse error ("EntityRef: expecting
                # ';'", "Unescaped '<' not allowed in attributes"). Escaping on
                # the way out does not help; the name simply cannot hold one
                if bad := set(name) & set('&<>"\''):
                    raise ValueError(
                        f"network {self.name}: the dhcp reservation name "
                        f"{name!r} contains {''.join(sorted(bad))!r}. libvirt "
                        f"defines a network holding & < or ' and then fails to "
                        f"start it; \" and > are refused with them because "
                        f"none of the five belong in a hostname")

                # hostnames are case-insensitive, and libvirt accepts the same
                # one twice: dnsmasq then resolves it to whichever it likes
                if (key := name.lower()) in seen_names:
                    raise ValueError(
                        f"network {self.name}: name {name!r} is reserved "
                        f"twice, for {seen_names[key]} and {ip}")
                seen_names[key] = ip
                host['name'] = name

            hosts.append(host)

        # debug rather than info: reconciliation builds a Network per plan and
        # per apply, so this would print several times per network per `up`
        self.logger.debug(
            f"network {self.name}: {len(hosts)} static dhcp reservation(s): " +
            ', '.join(f"{host['mac']} -> {host['ip']}" for host in hosts))

        return hosts

    def generate_xml(self) -> str:
        """
        Generate the XML for the network definition using a Jinja2 template.

        Returns:
            XML string for the network definition
        """
        # get the path to the assets directory
        assets_path = str(importlib_resources.files('boxman').joinpath('assets'))

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
            'dhcp_range_end': self.dhcp_range_end,
            'dhcp_hosts': self.dhcp_hosts
        }

        conf_xml = template.render(**context)

        return conf_xml

    def write_xml(self, file_path: str) -> str:
        """
        Write the xml configuration to a file.

        Args:
            file_path: The path where the xml file should be written

        Returns:
            The path to the written file
        """
        xml_content = self.generate_xml()
        abs_path = os.path.abspath(os.path.expanduser(file_path))
        dir_path = os.path.dirname(abs_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        with open(abs_path, 'w') as fobj:
            fobj.write(xml_content)

        log.info(f"wrote network XML to {abs_path} ({os.path.getsize(abs_path)} bytes)")
        return abs_path

    def _listed_networks(self, active_only: bool = False) -> list[str] | None:
        """Names of the networks libvirt knows, or None if it could not be asked."""
        args = ["net-list", "--name"] if active_only else ["net-list", "--all", "--name"]
        result = self.virsh.execute(*args, hide=True, warn=True)
        if not result.ok:
            return None
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def exists(self) -> bool | None:
        """
        Whether the network is defined.

        Returns:
            True/False, or None when libvirt could not be reached at all --
            which is not the same as "not defined" and must not be treated as
            an invitation to create it.
        """
        listed = self._listed_networks()
        return None if listed is None else self.name in listed

    def dump_xml(self) -> str | None:
        """
        Return the network XML as libvirt currently has it.

        Returns:
            The XML text, or None when it could not be read (undefined, or
            libvirt unreachable -- use :meth:`exists` to tell those apart).
        """
        result = self.virsh.execute("net-dumpxml", self.name, hide=True, warn=True)
        if not result.ok:
            return None
        return result.stdout

    def is_active(self) -> bool:
        """Return True when the network is defined *and* running."""
        listed = self._listed_networks(active_only=True)
        return bool(listed) and self.name in listed

    def start(self) -> bool:
        """Start a defined network, and make it come back after a host reboot."""
        result = self.virsh.execute("net-start", self.name, hide=True, warn=True)
        if not result.ok:
            self.logger.error(
                f"network {self.name}: could not be started: "
                f"{result.stderr.strip()}")
            return False

        autostart = self.virsh.execute(
            "net-autostart", self.name, hide=True, warn=True)
        if not autostart.ok:
            # not fatal, but it means the network is gone again after a reboot
            self.logger.warning(
                f"network {self.name}: started, but could not be set to "
                f"autostart: {autostart.stderr.strip()}")

        self.logger.info(f"network {self.name}: started")
        return True

    def apply_net_update(self,
                         command: str,
                         section: str,
                         element: str,
                         live: bool | None = None) -> bool:
        """
        Run a single ``virsh net-update`` against this network.

        The element is handed over in a temporary file rather than inline.
        ``net-update`` accepts both, but the inline form is a shell argument
        full of spaces and quotes, and under a container runtime the command is
        re-wrapped as a string -- a quoting bug waiting to happen.

        ``--live`` is only added when the network is running; applying it to a
        defined-but-stopped network is an error, while ``--config`` alone is
        exactly right.

        Args:
            command: ``add-last``, ``modify`` or ``delete``
            section: the section to update, e.g. ``ip-dhcp-host``
            element: the XML element to add, match or modify
            live: whether the network is running. Left to be looked up when
                not given, but the caller should pass it so that a plan of a
                dozen operations does not run a dozen ``net-list`` calls.

        Returns:
            True on success.
        """
        if live is None:
            live = self.is_active()

        temp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.xml', prefix='boxman-netupdate-', delete=False)
        try:
            temp.write(element)
            temp.close()

            args = ["net-update", self.name, command, section, temp.name,
                    "--config"]
            if live:
                args.append("--live")

            result = self.virsh.execute(*args, hide=True, warn=True)
            if not result.ok:
                self.logger.error(
                    f"network {self.name}: {command} {section} failed: "
                    f"{result.stderr.strip()}")
                return False

            self.logger.info(
                f"network {self.name}: {command} {section} {element}")
            return True
        finally:
            if os.path.exists(temp.name):
                os.unlink(temp.name)

    def apply_live_plan(self, plan: dict[str, Any]) -> bool:
        """
        Apply the non-disruptive half of a reconciliation plan.

        Ranges are applied before reservations so that a reservation moving
        into freshly-widened space does not transiently look out of range.

        Args:
            plan: as produced by :func:`net_reconcile.diff_network`

        Returns:
            True when every operation succeeded.
        """
        ok = True
        live = self.is_active()

        # a range change is a delete followed by an add. If the delete fails,
        # adding anyway would leave the network with two ranges -- and the diff
        # only ever reads the first, so every later run would try to add again
        for command, entry in plan.get('range_ops', []):
            if not self.apply_net_update(
                    command, 'ip-dhcp-range',
                    net_reconcile.range_element(entry), live=live):
                self.logger.error(
                    f"network {self.name}: stopping after a failed range "
                    f"{command} rather than leaving the network with a "
                    f"half-applied range; the reservations are left for the "
                    f"next run")
                return False

        for command, entry in plan.get('host_ops', []):
            # a delete matches on the mac alone; sending the whole element
            # makes libvirt match every attribute, so a stale ip would miss
            match = {'mac': entry['mac']} if command == 'delete' else entry
            ok &= self.apply_net_update(
                command, 'ip-dhcp-host',
                net_reconcile.host_element(match), live=live)

        return bool(ok)

    def attached_domains(self) -> list[str]:
        """
        Return the names of domains whose interfaces use this network.

        Used to tell the user which guests a recreate would disconnect.
        """
        result = self.virsh.execute("list", "--all", "--name", hide=True, warn=True)
        if not result.ok:
            return []

        attached = []
        for domain in [line.strip() for line in result.stdout.splitlines() if line.strip()]:
            iflist = self.virsh.execute(
                "domiflist", domain, hide=True, warn=True)
            if not iflist.ok:
                continue
            for row in parse_domiflist(iflist.stdout):
                if row.type == 'network' and row.source == self.name:
                    attached.append(domain)
                    break
        return attached

    def update_network_cache(self) -> bool:
        """
        Write the network information to the cache.
        """
        self.manager.cache.read_projects_cache()
        previous_projects = copy.deepcopy(self.manager.cache.projects)
        projects_in_cache = self.manager.cache.projects
        project_name = self.manager.config['project']
        project_cache = projects_in_cache[project_name]

        if 'networks' not in project_cache:
            project_cache['networks'] = {}

        project_cache['networks'][self.name] = {}

        if self.ip_address:
            project_cache['networks'][self.name]['ip_address'] = self.ip_address
        project_cache['networks'][self.name]['bridge_name'] = self.bridge_name

        try:
            self.manager.cache.write_projects_cache()
        except Exception:
            # Cache writes are atomic on disk. Restore the in-memory view too,
            # while preserving any previous record for this network.
            self.manager.cache.projects = previous_projects
            raise

    def check_network_exists(self) -> None:
        """
        Check if there are any issues or conflicts with existing networks in the cache.

        Only projects registered under the **same** runtime are checked:
        each runtime has its own isolated libvirt instance, so a network
        living in a docker-compose runtime can never conflict with one on
        the local host's libvirt (and vice versa).

        Returns:
            True if the network exists, False otherwise
        """
        self.manager.cache.read_projects_cache()
        projects_in_cache = self.manager.cache.projects

        # Determine the current runtime scope for filtering
        current_runtime = getattr(self.manager, '_runtime_name', 'local')

        conflicts = {}
        n_conflicts = 0
        for project_name, project_data in projects_in_cache.items():
            # Skip projects from a different runtime scope -- including when
            # the current runtime is 'local': a docker-compose runtime is a
            # different libvirt instance, not the same one
            project_runtime = project_data.get('runtime', 'local')
            if project_runtime != current_runtime:
                self.logger.debug(
                    f"skipping project {project_name} (runtime={project_runtime}) "
                    f"— current runtime is {current_runtime}")
                continue

            conflicts[project_name] = {}
            if 'networks' in project_data:
                conflicts[project_name]['networks'] = {}
                for net_name, net_info in project_data['networks'].items():
                    # a network is not in conflict with its own cache entry.
                    # Redefining one -- after a recreate, or after a define that
                    # failed and left the entry behind -- would otherwise be
                    # refused forever, by name and by address, against itself
                    if (project_name == self.manager.config['project']
                            and net_name == self.name):
                        continue

                    conflicts[project_name]['networks'][net_name] = {}
                    if net_name == self.name:
                        # network already exists in the cache
                        msg = f"network {self.name} already exists in project {project_name}"
                        conflicts[project_name]['networks'][net_name]['name'] = msg
                        n_conflicts += 1
                    if (self.forward_mode != 'bridge'
                            and net_info.get('bridge_name') == self.bridge_name):
                        # bridge name conflict
                        msg = f"bridge {self.bridge_name} is already used by network {net_name} in project {project_name}"
                        conflicts[project_name]['networks'][net_name]['bridge'] = msg
                        n_conflicts += 1
                    if (self.ip_address is not None
                            and net_info.get('ip_address') == self.ip_address):
                        # ip address conflict
                        msg = f"ip address {self.ip_address} is already used by network {net_name} in project {project_name}"
                        conflicts[project_name]['networks'][net_name]['ip'] = msg
                        n_conflicts += 1

        if n_conflicts > 0:
            for project, project_conflicts in conflicts.items():
                if 'networks' in project_conflicts:
                    for net_name, net_conflicts in project_conflicts['networks'].items():
                        for _conflict_type, conflict_msg in net_conflicts.items():
                            self.logger.error(f"{conflict_msg} (project: {project}, network: {net_name})")

            msg = f"found {n_conflicts} conflicts for network {self.name} in project {self.manager.config['project']}"
            self.logger.info(msg)
            raise RuntimeError(msg)
        else:
            self.logger.info(f"no conflicts found for network {self.name} in project {self.manager.config['project']}")

    def define_network(self, file_path: str | None = None):
        """
        Define the network using virsh.

        Args:
            file_path: Path to write the XML file, if None a temporary path will be used

        Returns:
            True if successful, False otherwise
        """
        # Reject unsupported modes and invalid bridge-mode fields before an
        # XML file, cache entry or libvirt network can be created.
        self.validate_definition()
        self.validate_runtime_prerequisites()

        if not file_path:
            file_path = f"/tmp/{self.name}-network.xml"

        # Resolve to an absolute path so the file is accessible both on the
        # host and inside a bind-mounted docker-compose container.
        file_path = os.path.abspath(os.path.expanduser(file_path))

        written_path = self.write_xml(file_path)

        # Verify the file was actually written before proceeding
        if not os.path.isfile(written_path):
            raise RuntimeError(
                f"network XML file was not created at {written_path}")

        self.check_network_exists()

        # Verify the bridge is not already in use before defining
        active_bridges = self._get_libvirt_bridges()
        if (self.forward_mode != 'bridge'
                and self.bridge_name in active_bridges):
            self.logger.error(
                f"bridge '{self.bridge_name}' is already in use by another "
                f"active network. Cannot define network '{self.name}'.")
            self._log_bridge_usage(self.bridge_name)
            # RuntimeError rather than sys.exit: reconciliation calls this per
            # network and turns a failure into a result, and a bare exit would
            # take the process down mid-run, after a recreate has already
            # destroyed the old network
            raise RuntimeError(
                f"bridge '{self.bridge_name}' is already in use by another "
                f"active network, cannot define network '{self.name}'")
        if (self.forward_mode == 'bridge'
                and self.bridge_name in active_bridges):
            self._log_bridge_mode_managed_owner()

        # Define transactionally. In particular, a remote bridge cannot be
        # checked through the client's /sys, so ``net-start`` is the
        # authoritative prerequisite check. If it fails, do not leave the
        # definition/autostart/cache debris the caller was told had failed.
        defined = False
        started = False
        route_rules_attempted = False
        try:
            self.virsh.execute("net-define", written_path)
            defined = True

            self.virsh.execute("net-start", self.name)
            started = True
            self.virsh.execute("net-autostart", self.name)

            # apply appropriate network configuration based on type. NAT
            # needs nothing from boxman: libvirtd installs the masquerade
            # and FORWARD rules for <forward mode='nat'/> itself when the
            # network starts (and removes them when it stops)
            if self.forward_mode == 'route':
                route_rules_attempted = True
                if not self.apply_route_iptables_rule():
                    raise RuntimeError(
                        f"network {self.name}: could not apply route "
                        "isolation rules")
            # NAT is configured by libvirt. Bridge mode only points at the
            # pre-existing host bridge, so it needs no firewall setup here.

            # Cache only a definition that completed its whole bring-up. A
            # failed start (notably a missing bridge on a remote endpoint) is
            # rolled back below and must never be advertised as provisioned.
            self.update_network_cache()

            return True
        except Exception as exc:
            # Catch operational/programming exceptions, including cache
            # OSError, but deliberately leave BaseException subclasses such as
            # KeyboardInterrupt and SystemExit alone.
            if route_rules_attempted:
                try:
                    rules_removed = self.remove_route_iptables_rule()
                except Exception as rollback_exc:
                    self.logger.warning(
                        f"network {self.name}: rollback could not remove "
                        f"route isolation rules: {rollback_exc}")
                else:
                    if not rules_removed:
                        self.logger.warning(
                            f"network {self.name}: rollback could not remove "
                            "route isolation rules")
            if started:
                try:
                    stopped = self.virsh.execute(
                        "net-destroy", self.name, hide=True, warn=True)
                except Exception as rollback_exc:
                    self.logger.warning(
                        f"network {self.name}: rollback could not stop the "
                        f"partially defined network: {rollback_exc}")
                else:
                    if not stopped.ok:
                        self.logger.warning(
                            f"network {self.name}: rollback could not stop the "
                            "partially defined network")
            if defined:
                try:
                    undefined = self.virsh.execute(
                        "net-undefine", self.name, hide=True, warn=True)
                except Exception as rollback_exc:
                    self.logger.warning(
                        f"network {self.name}: rollback could not undefine the "
                        f"partially defined network: {rollback_exc}")
                else:
                    if not undefined.ok:
                        self.logger.warning(
                            f"network {self.name}: rollback could not undefine "
                            "the partially defined network")
            self.logger.error(f"Error defining network: {exc}")
            return False

    def destroy_network(self) -> bool:
        """
        Destroy (stop) the network.

        Returns:
            True if successful, False otherwise
        """
        try:
            # check if network exists first -- by exact name, not a grep
            # substring ('prod' must not match 'prod-backup')
            listed = self._listed_networks()
            if listed is None:
                self.logger.error(
                    f"could not ask libvirt whether network {self.name} exists")
                return False
            if self.name not in listed:
                self.logger.info(f"network {self.name} does not exist, nothing to destroy")
                return True

            # destroy only when the network itself is active
            if self.is_active():
                self.virsh.execute("net-destroy", self.name)
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
            # check if network exists (exact name match, as in destroy_network)
            listed = self._listed_networks()
            if listed is None:
                self.logger.error(
                    f"could not ask libvirt whether network {self.name} exists")
                return False
            if self.name not in listed:
                self.logger.info(f"Network {self.name} does not exist, nothing to undefine")
                return True

            # disable autostart first if it's enabled
            self.virsh.execute("net-autostart", self.name, "--disable", warn=True)

            # undefine the network
            self.virsh.execute("net-undefine", self.name)
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

        # Remove Boxman-owned iptables rules, if any.
        if self.forward_mode == 'route':
            return self.remove_route_iptables_rule()

        # NAT and bridge mode need no Boxman-owned host-rule cleanup. The same
        # is true for an unsupported network orphaned by an older release:
        # libvirt owned any rules, and destroy/undefine above already removed
        # it. Do not make legacy cleanup impossible just because new
        # definitions reject its mode.
        return True

    def find_available_bridge_name(self) -> str:
        """
        Find the first available virbrX name that is not in use.

        Uses ``virsh net-list`` + ``virsh net-dumpxml`` to discover bridges
        managed by libvirt in the **current runtime**, rather than ``brctl show``
        which may leak host bridges into a container environment.

        Returns:
            The first available virbrX name
        """
        try:
            # Discover bridges via libvirt networks (runtime-aware)
            existing_bridges = self._get_libvirt_bridges()

            # Also check the boxman cache for bridges registered under the
            # same runtime scope
            cached_bridges = self._get_cached_bridges()
            existing_bridges.update(cached_bridges)

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

    def _get_libvirt_bridges(self) -> set:
        """
        Discover bridge names from libvirt networks using virsh commands.

        This is runtime-aware: inside a docker-compose container it sees only
        the container's libvirt networks, not host bridges.

        Returns:
            A set of bridge name strings (e.g. {'virbr0', 'virbr1'}).
        """
        bridges = set()
        try:
            network_names = Network.list_networks(provider_config=self.provider_config)
            for net_name in network_names:
                bridge = Network.get_bridge_from_network(
                    net_name, provider_config=self.provider_config)
                if bridge:
                    bridges.add(bridge)
        except Exception as exc:
            self.logger.warning(f"failed to discover libvirt bridges: {exc}")
        return bridges

    def _get_cached_bridges(self) -> set:
        """
        Collect bridge names from the boxman cache, filtered by runtime scope.

        Returns:
            A set of bridge name strings from cached projects in the same runtime.
        """
        bridges = set()
        if not self.manager:
            return bridges

        try:
            self.manager.cache.read_projects_cache()
            projects = self.manager.cache.projects or {}
            current_runtime = getattr(self.manager, '_runtime_name', 'local')

            for _project_name, project_data in projects.items():
                # Only consider projects from the same runtime scope
                project_runtime = project_data.get('runtime', 'local')
                if current_runtime != 'local' and project_runtime != current_runtime:
                    continue

                for _net_name, net_info in project_data.get('networks', {}).items():
                    bridge = net_info.get('bridge_name')
                    if bridge:
                        bridges.add(bridge)
        except Exception as exc:
            self.logger.warning(f"failed to read cached bridges: {exc}")
        return bridges

    def _log_bridge_usage(self, bridge_name: str) -> None:
        """Log which libvirt networks are using the given bridge."""
        try:
            network_names = Network.list_networks(provider_config=self.provider_config)
            for net_name in network_names:
                bridge = Network.get_bridge_from_network(
                    net_name, provider_config=self.provider_config)
                if bridge == bridge_name:
                    self.logger.error(
                        f"  bridge '{bridge_name}' is used by network '{net_name}'")
        except Exception as exc:
            self.logger.warning(f"failed to enumerate bridge usage: {exc}")

    def _log_bridge_mode_managed_owner(self) -> None:
        """Diagnose bridge-mode reuse of a libvirt-managed Linux bridge.

        Sharing is legal and can be intentional, so this is not a conflict.
        It does couple lifecycles, though: destroying a nat/route owner removes
        its Linux bridge underneath this bridge-mode network. Keep the probe
        debug-gated because it needs one ``net-dumpxml`` per network.
        """
        if not self.logger.isEnabledFor(logging.DEBUG):
            return

        network_names = self._listed_networks()
        if network_names is None:
            self.logger.debug(
                f"network {self.name}: could not inspect ownership of bridge "
                f"{self.bridge_name!r}")
            return

        owners = []
        for network_name in network_names:
            result = self.virsh.execute(
                "net-dumpxml", network_name, hide=True, warn=True)
            if not result.ok:
                continue
            try:
                state = net_reconcile.parse_network_xml(result.stdout)
            except (ET.ParseError, ValueError):
                continue
            if (state.get('bridge_name') == self.bridge_name
                    and state.get('mode') in {'nat', 'route'}):
                owners.append(f"{network_name} ({state['mode']})")

        if owners:
            self.logger.debug(
                f"network {self.name}: bridge mode references "
                f"{self.bridge_name!r}, which is managed by libvirt network(s) "
                f"{', '.join(owners)}; destroying an owner removes the Linux "
                "bridge underneath this network")

    @staticmethod
    def _ensure_rule(cls,
                     check_cmd: str,
                     action_cmd: str,
                     present: bool = True) -> bool:
        """
        Make sure a rule is either present (present=True) or absent (present=False).

        Args:
            cls        : object with a ``virsh`` executor and a ``logger``
            check_cmd  : iptables -C ... command used to probe rule existence
            action_cmd : command that adds the rule (present) or deletes the rule (absent)
            present    : True -> ensure rule exists, False -> ensure rule is removed
        """
        chk_res = cls.virsh.execute_shell(check_cmd, warn=True)

        # desired state already reached
        if (present and chk_res.return_code == 0) or (not present and chk_res.return_code != 0):
            cls.logger.debug(f"rule already in desired state: {check_cmd}")
            return True

        # need an action to reach desired state
        apply_res = cls.virsh.execute_shell(action_cmd, warn=True)
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

        if not self.bridge_name:
            self.logger.warning("no bridge name found, cannot remove isolation rules")
            return True

        try:
            # shlex.quote: the bridge name comes from the config and is
            # interpolated into a shell string below
            br_name = shlex.quote(self.bridge_name)
            self.logger.info(f"removing route isolation rules for bridge {self.bridge_name}")

            # no embedded 'sudo': execute_shell routes that decision through
            # _should_use_sudo_for_command, so use_sudo: false is honoured
            if not self._ensure_rule(
                    self,
                    f"iptables -C FORWARD -i {br_name} -o {br_name} -j ACCEPT",
                    f"iptables -D FORWARD -i {br_name} -o {br_name} -j ACCEPT",
                    present=False):
                return False
            if not self._ensure_rule(
                    self,
                    f"iptables -C INPUT  -i {br_name} -j DROP",
                    f"iptables -D INPUT  -i {br_name} -j DROP",
                    present=False):
                return False
            if not self._ensure_rule(
                    self,
                    f"iptables -C OUTPUT -o {br_name} -j DROP",
                    f"iptables -D OUTPUT -o {br_name} -j DROP",
                    present=False):
                return False

            self.logger.info(f"successfully removed isolation rules for routed network {self.name}")
            return True
        except Exception as exc:
            self.logger.error(f"error removing route isolation rules: {exc}")
            return False

    def apply_route_iptables_rule(self) -> bool:
        """
        Apply iptables rules for truly isolated routed networks.

        This method configures iptables to:

            - allow vm-to-vm communication on the same bridge
            - block all traffic between host and guests in both directions

        Returns:
            True if successful, False otherwise
        """
        if self.forward_mode != 'route':
            return True  # Nothing to do for non-route networks

        try:
            # shlex.quote: the bridge name comes from the config and is
            # interpolated into a shell string below
            bridge_name = shlex.quote(self.bridge_name)
            self.logger.info(
                f"configuring complete isolation for routed network with bridge {self.bridge_name}")

            # 1. allow vm-to-vm communication on the same bridge. No embedded
            # 'sudo' here or below: execute_shell routes that decision through
            # _should_use_sudo_for_command, so use_sudo: false is honoured
            vm2vm_check = f"iptables -C FORWARD -i {bridge_name} -o {bridge_name} -j ACCEPT"
            vm2vm_cmd   = f"iptables -I FORWARD -i {bridge_name} -o {bridge_name} -j ACCEPT"
            if not self._ensure_rule(self, vm2vm_check, vm2vm_cmd):
                return False

            # 2. block all traffic from the VMs to the host
            host2vm_check = f"iptables -C INPUT -i {bridge_name} -j DROP"
            host2vm_cmd   = f"iptables -I INPUT -i {bridge_name} -j DROP"
            if not self._ensure_rule(self, host2vm_check, host2vm_cmd):
                return False

            # 3. block all traffic from host to the VMs
            vm2host_check = f"iptables -C OUTPUT -o {bridge_name} -j DROP"
            vm2host_cmd   = f"iptables -I OUTPUT -o {bridge_name} -j DROP"
            if not self._ensure_rule(self, vm2host_check, vm2host_cmd):
                return False

            self.logger.info(f"successfully applied complete isolation for routed network {self.name}")
            return True

        except Exception as exc:
            self.logger.error(f"error applying route isolation rules: {exc}")
            return False

    @staticmethod
    def get_bridge_from_network(network_name: str,
                                provider_config: dict[str, Any] | None = None) -> str | None:
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

        # check if network name exists
        existing_networks = Network.list_networks(provider_config=provider_config)
        if network_name not in existing_networks:
            log.warning(f"network {network_name} does not exist")
            return None

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
    def list_networks(provider_config: dict[str, Any] | None = None,
                      active_only: bool = False) -> list[str]:
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


class NetworkInterface:
    """
    Class to manage network interfaces for libvirt VMs.
    """
    def __init__(self,
                 vm_name: str,
                 provider_config: dict[str, Any] | None = None):
        """
        Initialize the network interface manager.

        Args:
            vm_name: Name of the VM to manage interfaces for
            provider_config: Configuration for the libvirt provider
        """
        #: VirshCommand: Command executor for virsh
        self.virsh = VirshCommand(provider_config=provider_config)

        #: str: Name of the VM
        self.vm_name = vm_name

        #: logging.Logger: Logger instance
        self.logger = log

    def add_interface(self,
                      network_source: str,
                      link_state: str = 'active',
                      mac_address: str | None = None,
                      model: str = 'virtio',
                      source_type: str = 'network') -> bool:
        """
        Add a network interface to the VM.

        Args:
            network_source: Name of the network to attach to
            link_state: State of the link ('active' or 'inactive')
            mac_address: Optional MAC address for the interface
            model: NIC model (default: virtio)
            source_type: ``'network'`` (libvirt-managed network, default) or
                ``'bridge'`` (attach to an existing host Linux bridge, used
                for shared-bridge L2 glue with external lab tools).

        Returns:
            True if successful, False otherwise
        """

        try:
            # get the path to the assets directory
            assets_path = str(importlib_resources.files('boxman').joinpath('assets'))

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
                'model': model,
                'source_type': source_type,
            }

            xml_content = template.render(**context)

            # create a temporary file to store the XML
            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            # use virsh to attach the interface
            self.virsh.execute("attach-device", self.vm_name, temp_path, "--persistent")

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
                              adapter_config: dict[str, Any]) -> bool:
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
        source_type = adapter_config.get('source_type', 'network')

        return self.add_interface(
            network_source=network_source,
            link_state=link_state,
            mac_address=mac_address,
            model=model,
            source_type=source_type,
        )
