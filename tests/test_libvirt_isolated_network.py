"""
Unit tests for the address-free ``mode: isolated`` libvirt network.

An isolated network renders no ``<forward>`` and no ``<ip>``: the host holds no
address on the bridge, libvirt has nothing to run DHCP on, and boxman installs
no firewall rules. The mode is opt-in, so the other half of this module pins
the rendering of the existing modes byte for byte, as they rendered before the
mode was added.
"""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import ConfigError
from boxman.providers.libvirt import net_reconcile as nr
from boxman.providers.libvirt.net import Network
from boxman.providers.libvirt.session import LibVirtSession

pytestmark = pytest.mark.unit

ZERO_UUID = str(uuid.UUID(int=0))


def _result(stdout: str = "", ok: bool = True, stderr: str = "") -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = 0 if ok else 1
    return r


def _network(info: dict, name: str = "lab-test", manager=None) -> Network:
    with patch.object(Network, "find_available_bridge_name",
                      return_value="virbr9"):
        net = Network(name=name, info=info, assign_new_bridge=True,
                      provider_config={"use_sudo": False}, manager=manager)
    net.uuid_val = ZERO_UUID
    return net


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

ISOLATED_DEFAULT_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><network>\n'
    '  <name>lab-test</name>\n'
    f'  <uuid>{ZERO_UUID}</uuid>\n'
    "  <bridge name='virbr9' stp='on' delay='0'/>\n"
    "  <mac address='52:54:00:0a:0b:0c'/>\n"
    '</network>'
)

ISOLATED_PINNED_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><network>\n'
    '  <name>lab-test</name>\n'
    f'  <uuid>{ZERO_UUID}</uuid>\n'
    "  <bridge name='l2test0' stp='off' delay='0'/>\n"
    "  <mac address='52:54:00:00:00:01'/>\n"
    '</network>'
)

PINNED_INFO = {
    "mode": "isolated",
    "bridge": {"name": "l2test0", "stp": "off", "delay": "0"},
    "mac": "52:54:0:0:0:1",
    "enable": True,
}


#: an injection payload: closes the delay attribute, adds a NAT forward
#: and a host address, and reopens an element so the rest still parses
MARKUP_PAYLOAD = ("0'/><forward mode='nat'/><ip address='192.0.2.1' "
                  "netmask='255.255.255.0'/><dns enable='no")

#: every shape of accepted isolated configuration the validator lets through
ACCEPTED_ISOLATED_INFOS = [
    {"mode": "isolated"},
    PINNED_INFO,
    {"mode": "isolated", "enable": False},
    {"mode": "isolated", "bridge": {"stp": True}},
    {"mode": "isolated", "bridge": {"stp": "off", "delay": 0}},
    {"mode": "isolated", "bridge": {"delay": 15}},
    {"mode": "isolated", "bridge": {"name": "br-lab.1_x"}},
    {"mode": "isolated", "mac": "52:54:00:00:00:02"},
    {"mode": "isolated", "mac": "52:54:0:A:B:C"},
]


class TestIsolatedRendering:

    def test_default_golden(self):
        assert _network({"mode": "isolated"}).generate_xml() == ISOLATED_DEFAULT_XML

    def test_pinned_bridge_golden(self):
        assert _network(PINNED_INFO).generate_xml() == ISOLATED_PINNED_XML

    @pytest.mark.parametrize("info", [{"mode": "isolated"}, PINNED_INFO])
    def test_no_forward_no_ip_no_dnsmasq(self, info):
        xml = _network(info).generate_xml()
        root = ET.fromstring(xml)
        assert root.tag == "network"
        assert root.find("forward") is None
        assert root.find("ip") is None
        assert root.findall(".//ip") == []
        assert root.findall(".//dhcp") == []
        assert "dnsmasq" not in xml
        # libvirt still needs a bridge to create; the mac pins its address
        assert root.find("bridge").get("name") in {"virbr9", "l2test0"}
        assert root.find("mac") is not None
        # no ipv6='yes' or any other attribute on <network>
        assert root.attrib == {}

    @pytest.mark.parametrize("attr,payload", [
        ("bridge_delay", MARKUP_PAYLOAD),
        ("bridge_stp", MARKUP_PAYLOAD),
        ("mac_address", MARKUP_PAYLOAD),
        ("bridge_name", MARKUP_PAYLOAD),
    ])
    def test_template_escapes_attribute_markup(self, attr, payload):
        # validation refuses these values; the template must not depend on
        # it. Set past validation, the payload stays inert attribute text
        net = _network({"mode": "isolated"})
        setattr(net, attr, payload)
        root = ET.fromstring(net.generate_xml())
        assert root.find("forward") is None
        assert root.findall(".//ip") == []
        assert root.find("dns") is None
        element, key = {"bridge_delay": ("bridge", "delay"),
                        "bridge_stp": ("bridge", "stp"),
                        "mac_address": ("mac", "address"),
                        "bridge_name": ("bridge", "name")}[attr]
        assert root.find(element).get(key) == payload

    @pytest.mark.parametrize("info", ACCEPTED_ISOLATED_INFOS)
    def test_no_accepted_config_renders_forward_or_ip(self, info):
        net = _network(info)
        net.validate_definition()
        root = ET.fromstring(net.generate_xml())
        assert [child.tag for child in root] == ["name", "uuid", "bridge", "mac"]
        assert root.attrib == {}

    def test_no_default_address_is_assumed(self):
        # a nat network with no ip block gets 192.168.254.1; an isolated one
        # must not carry that address in memory, where the cache and the
        # address-conflict check would treat it as held by the host
        net = _network({"mode": "isolated"})
        assert net.ip_address is None
        assert net.netmask is None
        assert net.dhcp_range_start is None
        assert net.dhcp_range_end is None
        assert net.dhcp_hosts == []


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

class TestIsolatedValidation:

    @staticmethod
    def _assert_rejected_before_define(tmp_path, info, message):
        net = _network(info)
        net.virsh.execute = MagicMock()
        net.virsh.execute_shell = MagicMock()
        xml_path = tmp_path / "invalid.xml"
        with pytest.raises(ConfigError, match=message):
            net.define_network(str(xml_path))
        net.virsh.execute.assert_not_called()
        net.virsh.execute_shell.assert_not_called()
        assert not xml_path.exists()

    @pytest.mark.parametrize("extra,key", [
        ({"ip": {"address": "192.0.2.1", "netmask": "255.255.255.0"}}, "ip"),
        ({"ip": {"dhcp": {"range": {"start": "192.0.2.10",
                                    "end": "192.0.2.20"}}}}, "ip"),
        ({"ip": {"dhcp": {"hosts": [{"mac": "52:54:00:00:00:03",
                                     "ip": "192.0.2.11"}]}}}, "ip"),
        # an empty `ip:` is None to yaml; still a request for an address
        ({"ip": None}, "ip"),
        ({"ip": {}}, "ip"),
        ({"dhcp": {"range": {"start": "192.0.2.10", "end": "192.0.2.20"}}},
         "dhcp"),
        ({"ipv6": True}, "ipv6"),
        ({"ipv6": False}, "ipv6"),
    ])
    def test_contradictory_keys_are_rejected(self, tmp_path, extra, key):
        info = {"mode": "isolated", **extra}
        self._assert_rejected_before_define(
            tmp_path, info, rf"mode 'isolated' is address-free.*remove {key} \(")

    def test_every_offending_key_is_reported_at_once(self):
        net = _network({"mode": "isolated", "ip": {"address": "192.0.2.1"},
                        "dhcp": {}, "ipv6": True})
        with pytest.raises(ConfigError) as excinfo:
            net.validate_definition()
        message = str(excinfo.value)
        assert "network lab-test:" in message
        for key in ("ip (", "dhcp (", "ipv6 ("):
            assert key in message
        assert "use mode nat or route" in message

    @pytest.mark.parametrize("info,fragment", [
        ({"bridge": {"delay": MARKUP_PAYLOAD}}, "bridge.delay"),
        ({"mac": MARKUP_PAYLOAD}, "mac"),
        ({"bridge": {"name": "br0'/><forward mode='nat'/><x a='"}},
         "bridge.name"),
        ({"bridge": {"delay": -1}}, "bridge.delay"),
        ({"bridge": {"delay": "-1"}}, "bridge.delay"),
        ({"bridge": {"delay": "1.5"}}, "bridge.delay"),
        ({"bridge": {"delay": True}}, "bridge.delay"),
        ({"bridge": {"delay": None}}, "bridge.delay"),
        ({"mac": "52-54-00-00-00-02"}, "mac"),
        ({"mac": "52:54:00:00:00"}, "mac"),
        ({"mac": None}, "mac"),
        ({"bridge": {"name": "br/lab"}}, "bridge.name"),
        ({"bridge": {"name": "a-bridge-name-too-long"}}, "bridge.name"),
        ({"bridge": {"name": 7}}, "bridge.name"),
    ])
    def test_invalid_accepted_values_are_rejected(self, tmp_path, info,
                                                  fragment):
        self._assert_rejected_before_define(
            tmp_path, {"mode": "isolated", **info},
            rf"mode 'isolated' has invalid values: {fragment} ")

    def test_every_invalid_value_is_reported_at_once(self):
        net = _network({"mode": "isolated", "mac": "nope",
                        "bridge": {"name": "br/x", "delay": "x"}})
        with pytest.raises(ConfigError) as excinfo:
            net.validate_definition()
        message = str(excinfo.value)
        for key in ("bridge.name 'br/x'", "bridge.delay 'x'", "mac 'nope'"):
            assert key in message

    def test_bridge_block_must_still_be_a_mapping(self, tmp_path):
        self._assert_rejected_before_define(
            tmp_path, {"mode": "isolated", "bridge": "l2test0"},
            "bridge must be a mapping")

    @pytest.mark.parametrize("info", ACCEPTED_ISOLATED_INFOS)
    def test_supported_keys_validate(self, info):
        _network(info).validate_definition()

    def test_other_modes_keep_their_ip_block(self):
        # the rejection is scoped to isolated: nat and route still take ip
        for mode in ("nat", "route"):
            _network({"mode": mode,
                      "ip": {"address": "192.0.2.1"}}).validate_definition()

    def test_unknown_modes_are_still_rejected(self):
        with pytest.raises(ConfigError, match="unsupported forward mode"):
            _network({"mode": "open"}).validate_definition()

    def test_supported_modes_message_lists_isolated(self):
        with pytest.raises(ConfigError,
                           match="supported modes: bridge, isolated, nat, route"):
            _network({"mode": "hostdev"}).validate_definition()


# --------------------------------------------------------------------------
# lifecycle: define, cache, firewall, removal
# --------------------------------------------------------------------------

class TestIsolatedLifecycle:

    def test_define_runs_no_firewall_commands(self, tmp_path):
        net = _network({"mode": "isolated"})
        with patch.object(net, "check_network_exists"), \
             patch.object(net, "_get_libvirt_bridges", return_value=set()), \
             patch.object(net, "update_network_cache"), \
             patch.object(net.virsh, "execute", return_value=_result()) as virsh, \
             patch.object(net.virsh, "execute_shell") as shell:
            assert net.define_network(str(tmp_path / "net.xml")) is True
        shell.assert_not_called()
        commands = [call.args[0] for call in virsh.call_args_list]
        assert commands == ["net-define", "net-start", "net-autostart"]
        written = ET.fromstring((tmp_path / "net.xml").read_text())
        assert written.find("forward") is None
        assert written.find("ip") is None

    def test_bridge_in_use_is_still_refused(self, tmp_path):
        # libvirt creates this bridge, so it must not collide with a live one
        net = _network({"mode": "isolated"})
        with patch.object(net, "check_network_exists"), \
             patch.object(net, "_get_libvirt_bridges", return_value={"virbr9"}), \
             patch.object(net, "_log_bridge_usage"), \
             patch.object(net.virsh, "execute") as virsh:
            with pytest.raises(RuntimeError, match="already in use"):
                net.define_network(str(tmp_path / "net.xml"))
        virsh.assert_not_called()

    def test_cache_records_the_bridge_but_no_address(self):
        manager = MagicMock()
        manager.config = {"project": "demo"}
        manager._runtime_name = "local"
        manager.cache.projects = {"demo": {"networks": {}}}
        _network({"mode": "isolated"}, manager=manager).update_network_cache()
        entry = manager.cache.projects["demo"]["networks"]["lab-test"]
        assert entry == {"bridge_name": "virbr9"}

    def test_no_address_conflict_with_another_network(self):
        # two address-free networks, or one beside a nat network, share no
        # address -- only a bridge or name clash may conflict
        manager = MagicMock()
        manager.config = {"project": "demo"}
        manager._runtime_name = "local"
        manager.cache.projects = {
            "other": {"networks": {
                "lab-outside": {"bridge_name": "virbr3"},
                "nat-net": {"bridge_name": "virbr4",
                            "ip_address": "192.168.254.1"}}}}
        net = _network({"mode": "isolated"}, manager=manager)
        net.check_network_exists()  # does not raise

    def test_isolation_reconcile_is_skipped(self):
        net = _network({"mode": "isolated"})
        net.virsh.execute_shell = MagicMock()
        assert net.reconcile_isolation() == "skipped"
        assert net.apply_route_iptables_rule() is True
        net.virsh.execute_shell.assert_not_called()

    def test_removal_runs_no_firewall_cleanup(self):
        net = _network({"mode": "isolated"})
        with patch.object(net, "destroy_network", return_value=True), \
             patch.object(net, "undefine_network", return_value=True), \
             patch.object(net.virsh, "execute_shell") as shell:
            assert net.remove_network() is True
        shell.assert_not_called()


# --------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------

#: what libvirt reports back for the default isolated network: upper-case mac
#: and a uuid, but still no <forward> and no <ip>
LIVE_ISOLATED_XML = """
<network>
  <name>lab-test</name>
  <uuid>9f8e7d6c-0000-0000-0000-000000000000</uuid>
  <bridge name='virbr9' stp='on' delay='0'/>
  <mac address='52:54:00:0A:0B:0C'/>
</network>
"""

#: a forward-less network that holds a host address: libvirt's general
#: "isolated" network, which boxman's mode deliberately is not
LIVE_HOST_ONLY_XML = """
<network>
  <name>lab-test</name>
  <bridge name='virbr9' stp='on' delay='0'/>
  <mac address='52:54:00:0a:0b:0c'/>
  <ip address='192.0.2.1' netmask='255.255.255.0'/>
</network>
"""

LIVE_NAT_XML = """
<network>
  <name>lab-test</name>
  <forward mode='nat'/>
  <bridge name='virbr9' stp='on' delay='0'/>
  <mac address='52:54:00:0a:0b:0c'/>
  <ip address='192.168.254.1' netmask='255.255.255.0'/>
</network>
"""


#: the default isolated network as libvirt would report it after someone
#: turned on guest-to-guest IPv6 by hand
LIVE_ISOLATED_IPV6_XML = LIVE_ISOLATED_XML.replace(
    "<network>", "<network ipv6='yes'>")


class TestIsolatedReconcile:

    def test_parse_reports_a_forward_less_network_as_isolated(self):
        state = nr.parse_network_xml(LIVE_ISOLATED_XML)
        assert state["mode"] == "isolated"
        assert state["has_ip"] is False
        assert state["ip_address"] is None

    def test_parse_sees_the_address_of_a_host_only_network(self):
        state = nr.parse_network_xml(LIVE_HOST_ONLY_XML)
        assert state["mode"] == "isolated"
        assert state["has_ip"] is True

    def test_desired_state(self):
        assert nr.desired_state(_network({"mode": "isolated"})) == {
            "mode": "isolated", "bridge_name": "virbr9",
            "bridge_stp": "on", "bridge_delay": "0",
            "mac": "52:54:00:0a:0b:0c",
            "ip_address": None, "netmask": None, "dhcp_range": None,
            "dhcp_hosts": [], "dhcp_options_trimmed": False, "has_ip": False,
            "ipv6": "no",
        }

    @pytest.mark.parametrize("info", [{"mode": "isolated"}, PINNED_INFO])
    def test_rendered_xml_round_trips_without_drift(self, info):
        net = _network(info)
        plan = nr.diff_network(nr.desired_state(net),
                               nr.parse_network_xml(net.generate_xml()))
        assert plan == {"action": "none", "structural": [],
                        "host_ops": [], "range_ops": []}

    def test_libvirts_echo_round_trips_without_drift(self):
        plan = nr.diff_network(nr.desired_state(_network({"mode": "isolated"})),
                               nr.parse_network_xml(LIVE_ISOLATED_XML))
        assert plan["action"] == "none"

    @pytest.mark.parametrize("root,expected", [
        ("<network>", "no"),
        ("<network ipv6='no'>", "no"),
        ("<network ipv6='yes'>", "yes"),
        ("<network ipv6='YES'>", "yes"),
    ])
    def test_parse_normalises_the_root_ipv6_attribute(self, root, expected):
        xml = LIVE_ISOLATED_XML.replace("<network>", root)
        assert nr.parse_network_xml(xml)["ipv6"] == expected

    def test_live_ipv6_yes_is_drift(self):
        plan = nr.diff_network(nr.desired_state(_network({"mode": "isolated"})),
                               nr.parse_network_xml(LIVE_ISOLATED_IPV6_XML))
        assert plan["action"] == "recreate"
        assert plan["structural"] == [
            "guest IPv6 (<network ipv6> attribute) 'yes' -> 'no'"]

    @pytest.mark.parametrize("root", ["<network>", "<network ipv6='no'>"])
    def test_missing_and_no_ipv6_are_both_converged(self, root):
        xml = LIVE_ISOLATED_XML.replace("<network>", root)
        plan = nr.diff_network(nr.desired_state(_network({"mode": "isolated"})),
                               nr.parse_network_xml(xml))
        assert plan["action"] == "none"
        assert plan["structural"] == []

    @pytest.mark.parametrize("mode", ["nat", "route"])
    def test_existing_modes_do_not_compare_ipv6(self, mode):
        # scoped to isolated: a nat or route network that has ipv6='yes'
        # plans exactly as it did before the field existed
        net = _network({"mode": mode})
        assert "ipv6" not in nr.desired_state(net)
        live = net.generate_xml().replace("<network", "<network ipv6='yes'", 1)
        assert nr.diff_network(nr.desired_state(net),
                               nr.parse_network_xml(live))["action"] == "none"
        bridge = _network({"mode": "bridge", "bridge": {"name": "br-ext"}})
        assert "ipv6" not in nr.desired_state(bridge)

    def test_nat_to_isolated_needs_a_recreate(self):
        plan = nr.diff_network(nr.desired_state(_network({"mode": "isolated"})),
                               nr.parse_network_xml(LIVE_NAT_XML))
        assert plan["action"] == "recreate"
        assert "forward mode 'nat' -> 'isolated'" in plan["structural"]
        assert any(change.startswith("host address")
                   for change in plan["structural"])

    def test_a_host_address_on_a_forward_less_network_is_drift(self):
        # mode matches, so only has_ip can see it
        plan = nr.diff_network(nr.desired_state(_network({"mode": "isolated"})),
                               nr.parse_network_xml(LIVE_HOST_ONLY_XML))
        assert plan["action"] == "recreate"
        assert plan["structural"] == [
            "host address (<ip> element) True -> False"]

    def test_isolated_to_nat_needs_a_recreate(self):
        plan = nr.diff_network(nr.desired_state(_network({"mode": "nat"})),
                               nr.parse_network_xml(LIVE_ISOLATED_XML))
        assert plan["action"] == "recreate"
        assert "forward mode 'isolated' -> 'nat'" in plan["structural"]
        assert "ip address None -> '192.168.254.1'" in plan["structural"]

    @pytest.mark.parametrize("mode", ["nat", "route"])
    def test_existing_modes_do_not_compare_has_ip(self, mode):
        # the field is opt-in on the desired side; nat/route/bridge plans are
        # exactly what they were
        assert "has_ip" not in nr.desired_state(_network({"mode": mode}))
        bridge = _network({"mode": "bridge", "bridge": {"name": "br-ext"}})
        assert "has_ip" not in nr.desired_state(bridge)

    def test_teardown_of_an_isolated_network_describes_it(self):
        from boxman.manager import BoxmanManager
        actual = nr.parse_network_xml(LIVE_ISOLATED_XML)
        info = BoxmanManager._teardown_info(
            {"actual": actual}, {"mode": "route", "ip": {"address": "10.9.9.1"}})
        # no route-firewall cleanup for rules an isolated network never had
        assert info == {"mode": "isolated", "bridge": {"name": "virbr9"}}


class TestIsolatedBridgeOwnership:
    """libvirt creates and deletes an isolated network's bridge."""

    def test_isolated_to_bridge_rejects_the_managed_bridge_name(self):
        message = nr.bridge_ownership_conflict(
            {"mode": "bridge", "bridge_name": "virbr9"},
            {"mode": "isolated", "bridge_name": "virbr9"})
        assert "would delete that managed bridge" in message

    def test_bridge_to_isolated_rejects_the_host_bridge_name(self):
        message = nr.bridge_ownership_conflict(
            {"mode": "isolated", "bridge_name": "br-lab"},
            {"mode": "bridge", "bridge_name": "br-lab"})
        assert "cannot claim its name" in message

    def test_nat_to_isolated_same_bridge_is_not_an_ownership_change(self):
        assert nr.bridge_ownership_conflict(
            {"mode": "isolated", "bridge_name": "virbr9"},
            {"mode": "nat", "bridge_name": "virbr9"}) is None

    def test_bridge_to_auto_isolated_reserves_name_before_removal(self):
        # an authority-bearing uri, so no client-side bridge probe runs
        session = LibVirtSession(config={"provider": {"libvirt": {
            "uri": "qemu+ssh://hypervisor.example/system",
            "use_sudo": False}}})
        session.manager = MagicMock()
        bridge_xml = ("<network><name>demo</name><forward mode='bridge'/>"
                      "<bridge name='virbr0'/></network>")
        with (
            patch.object(Network, "exists", return_value=True),
            patch.object(Network, "dump_xml", return_value=bridge_xml),
            patch.object(Network, "attached_domains", return_value=[]),
            patch.object(Network, "is_active", return_value=True),
            patch.object(Network, "get_bridge_from_network",
                         return_value="virbr0"),
            patch.object(Network, "find_available_bridge_name",
                         return_value="virbr1"),
        ):
            plan = session.plan_network(name="demo", info={"mode": "isolated"})
        assert plan["action"] == "recreate"
        assert plan["replacement_bridge_name"] == "virbr1"


# --------------------------------------------------------------------------
# existing modes render exactly as before
# --------------------------------------------------------------------------

#: Inputs covering every branch of network.xml.j2 for nat, route and bridge.
EXISTING_MODE_INFOS = {
    'nat-default': {},
    'nat-ip-range': {'mode': 'nat', 'ip': {
        'address': '10.1.0.1', 'netmask': '255.255.255.0',
        'dhcp': {'range': {'start': '10.1.0.10', 'end': '10.1.0.20'}}}},
    'nat-hosts': {'mode': 'nat', 'ip': {
        'address': '10.1.1.1', 'netmask': '255.255.255.0',
        'dhcp': {'hosts': [{'mac': '52:54:00:aa:bb:01', 'ip': '10.1.1.5',
                            'name': 'h1'}]}}},
    'route-noip-dhcp': {'mode': 'route', 'ip': {
        'address': '10.2.0.1', 'netmask': '255.255.255.0'}},
    'route-dhcp': {
        'mode': 'route', 'bridge': {'name': 'virbr77', 'stp': 'off', 'delay': 3},
        'mac': '52:54:0:a:b:c',
        'ip': {'address': '10.2.1.1', 'netmask': '255.255.255.0',
               'dhcp': {'range': {'start': '10.2.1.10', 'end': '10.2.1.20'},
                        'hosts': [{'mac': '52:54:00:aa:bb:02',
                                   'ip': '10.2.1.5'}]}}},
    'bridge': {'mode': 'bridge', 'bridge': {'name': 'br-ext'}},
    'nat-null-ip': {'ip': None},
    'nat-escaped': {'ip': {
        'address': '10.3.0.1', 'netmask': '255.255.255.0',
        'dhcp': {'hosts': [{'mac': '52:54:00:aa:bb:03', 'ip': '10.3.0.5',
                            'name': 'a-b'}]}}},
}

#: Rendered by the template as it was before ``mode: isolated`` existed.
#: A change here is a change to every existing network's definition.
EXISTING_MODE_GOLDENS = {
    'bridge': (
        '<?xml version="1.0" encoding="UTF-8"?><network>\n'
        '  <name>bridge</name>\n'
        '  <uuid>00000000-0000-0000-0000-000000000000</uuid>\n'
        "  <forward mode='bridge'/>\n"
        "  <bridge name='br-ext'/>\n"
        '</network>'
    ),
    'nat-default': (
        '<?xml version="1.0" encoding="UTF-8"?><network>\n'
        '  <name>nat-default</name>\n'
        '  <uuid>00000000-0000-0000-0000-000000000000</uuid>\n'
        "  <forward mode='nat'/>\n"
        "  <bridge name='virbr9' stp='on' delay='0'/>\n"
        "  <mac address='52:54:00:0a:0b:0c'/>\n"
        "  <ip address='192.168.254.1' netmask='255.255.255.0'>\n"
        '  </ip>\n'
        '</network>'
    ),
    'nat-escaped': (
        '<?xml version="1.0" encoding="UTF-8"?><network>\n'
        '  <name>nat-escaped</name>\n'
        '  <uuid>00000000-0000-0000-0000-000000000000</uuid>\n'
        "  <forward mode='nat'/>\n"
        "  <bridge name='virbr9' stp='on' delay='0'/>\n"
        "  <mac address='52:54:00:0a:0b:0c'/>\n"
        "  <ip address='10.3.0.1' netmask='255.255.255.0'>\n"
        '    <dhcp>\n'
        "      <host mac='52:54:00:aa:bb:03' name='a-b' ip='10.3.0.5'/>\n"
        '    </dhcp>\n'
        '  </ip>\n'
        '</network>'
    ),
    'nat-hosts': (
        '<?xml version="1.0" encoding="UTF-8"?><network>\n'
        '  <name>nat-hosts</name>\n'
        '  <uuid>00000000-0000-0000-0000-000000000000</uuid>\n'
        "  <forward mode='nat'/>\n"
        "  <bridge name='virbr9' stp='on' delay='0'/>\n"
        "  <mac address='52:54:00:0a:0b:0c'/>\n"
        "  <ip address='10.1.1.1' netmask='255.255.255.0'>\n"
        '    <dhcp>\n'
        "      <host mac='52:54:00:aa:bb:01' name='h1' ip='10.1.1.5'/>\n"
        '    </dhcp>\n'
        '  </ip>\n'
        '</network>'
    ),
    'nat-ip-range': (
        '<?xml version="1.0" encoding="UTF-8"?><network>\n'
        '  <name>nat-ip-range</name>\n'
        '  <uuid>00000000-0000-0000-0000-000000000000</uuid>\n'
        "  <forward mode='nat'/>\n"
        "  <bridge name='virbr9' stp='on' delay='0'/>\n"
        "  <mac address='52:54:00:0a:0b:0c'/>\n"
        "  <ip address='10.1.0.1' netmask='255.255.255.0'>\n"
        '    <dhcp>\n'
        "      <range start='10.1.0.10' end='10.1.0.20'/>\n"
        '    </dhcp>\n'
        '  </ip>\n'
        '</network>'
    ),
    'nat-null-ip': (
        '<?xml version="1.0" encoding="UTF-8"?><network>\n'
        '  <name>nat-null-ip</name>\n'
        '  <uuid>00000000-0000-0000-0000-000000000000</uuid>\n'
        "  <forward mode='nat'/>\n"
        "  <bridge name='virbr9' stp='on' delay='0'/>\n"
        "  <mac address='52:54:00:0a:0b:0c'/>\n"
        "  <ip address='192.168.254.1' netmask='255.255.255.0'>\n"
        '  </ip>\n'
        '</network>'
    ),
    'route-dhcp': (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<network xmlns:dnsmasq='http://libvirt.org/schemas/network/dnsmasq/1.0'>\n"
        '  <name>route-dhcp</name>\n'
        '  <uuid>00000000-0000-0000-0000-000000000000</uuid>\n'
        "  <forward mode='route'/>\n"
        "  <bridge name='virbr77' stp='off' delay='3'/>\n"
        "  <mac address='52:54:00:0a:0b:0c'/>\n"
        "  <ip address='10.2.1.1' netmask='255.255.255.0'>\n"
        '    <dhcp>\n'
        "      <range start='10.2.1.10' end='10.2.1.20'/>\n"
        "      <host mac='52:54:00:aa:bb:02' ip='10.2.1.5'/>\n"
        '    </dhcp>\n'
        '  </ip>\n'
        '  <dnsmasq:options>\n'
        "    <dnsmasq:option value='dhcp-option=3'/>\n"
        "    <dnsmasq:option value='dhcp-option=6'/>\n"
        '  </dnsmasq:options></network>'
    ),
    'route-noip-dhcp': (
        '<?xml version="1.0" encoding="UTF-8"?><network>\n'
        '  <name>route-noip-dhcp</name>\n'
        '  <uuid>00000000-0000-0000-0000-000000000000</uuid>\n'
        "  <forward mode='route'/>\n"
        "  <bridge name='virbr9' stp='on' delay='0'/>\n"
        "  <mac address='52:54:00:0a:0b:0c'/>\n"
        "  <ip address='10.2.0.1' netmask='255.255.255.0'>\n"
        '  </ip>\n'
        '</network>'
    ),
}


@pytest.mark.regression
class TestExistingModesRenderUnchanged:
    """Adding ``mode: isolated`` must not move a byte of the other modes."""

    def test_every_golden_has_an_input(self):
        assert set(EXISTING_MODE_GOLDENS) == set(EXISTING_MODE_INFOS)

    @pytest.mark.parametrize("key", sorted(EXISTING_MODE_INFOS))
    def test_byte_identical(self, key):
        net = _network(EXISTING_MODE_INFOS[key], name=key)
        assert net.generate_xml() == EXISTING_MODE_GOLDENS[key]

    @pytest.mark.parametrize("key", sorted(EXISTING_MODE_INFOS))
    def test_validation_still_accepts_them(self, key):
        _network(EXISTING_MODE_INFOS[key], name=key).validate_definition()
