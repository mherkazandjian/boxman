"""
Unit tests for boxman.providers.libvirt.net (Network + NetworkInterface).

Focuses on: construction + defaults, XML generation via the bundled Jinja2
template, static helpers (list_networks, get_bridge_from_network), and
bridge-allocation logic in find_available_bridge_name.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.net import Network, NetworkInterface

pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True, stderr: str = "", return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


class TestConstruction:

    def test_defaults_when_minimal_info(self):
        n = Network(name="net1", info={}, assign_new_bridge=True,
                    provider_config={"use_sudo": False})
        # Default forward mode is 'nat'
        assert n.forward_mode == "nat"
        # Default IP / netmask
        assert n.ip_address == "192.168.254.1"
        assert n.netmask == "255.255.255.0"
        # Default MAC pattern (52:54:00:XX:XX:XX)
        assert n.mac_address.startswith("52:54:00:")
        # Default enable
        assert n.enable is True
        # Assigned a bridge name
        assert n.bridge_name is not None

    def test_custom_ip_and_dhcp(self):
        info = {
            "ip": {
                "address": "10.0.0.1",
                "netmask": "255.255.255.0",
                "dhcp": {"range": {"start": "10.0.0.100", "end": "10.0.0.200"}},
            }
        }
        with patch.object(Network, "find_available_bridge_name", return_value="virbr5"):
            n = Network("net1", info=info, assign_new_bridge=True,
                        provider_config={"use_sudo": False})
        assert n.ip_address == "10.0.0.1"
        assert n.dhcp_range_start == "10.0.0.100"
        assert n.dhcp_range_end == "10.0.0.200"
        assert n.bridge_name == "virbr5"

    def test_enable_false_propagates(self):
        with patch.object(Network, "find_available_bridge_name", return_value="virbr0"):
            n = Network("net1", info={"enable": False}, assign_new_bridge=True,
                        provider_config={"use_sudo": False})
        assert n.enable is False


class TestGenerateXml:

    @pytest.fixture
    def net(self) -> Network:
        with patch.object(Network, "find_available_bridge_name", return_value="virbr9"):
            return Network(
                name="demo-net",
                info={
                    "mode": "nat",
                    "ip": {
                        "address": "192.168.150.1",
                        "netmask": "255.255.255.0",
                        "dhcp": {"range": {"start": "192.168.150.10",
                                           "end": "192.168.150.100"}},
                    },
                },
                assign_new_bridge=True,
                provider_config={"use_sudo": False},
            )

    def test_xml_is_well_formed_and_has_expected_fields(self, net: Network):
        xml = net.generate_xml()
        root = ET.fromstring(xml)
        # Top-level <network>
        assert root.tag == "network"
        assert root.findtext("name") == "demo-net"
        # bridge name
        bridge = root.find("bridge")
        assert bridge is not None
        assert bridge.get("name") == "virbr9"
        # forward mode
        forward = root.find("forward")
        assert forward is not None
        assert forward.get("mode") == "nat"
        # ip address + dhcp range
        ip = root.find("ip")
        assert ip is not None
        assert ip.get("address") == "192.168.150.1"
        dhcp_range = ip.find("dhcp/range")
        assert dhcp_range is not None
        assert dhcp_range.get("start") == "192.168.150.10"
        assert dhcp_range.get("end") == "192.168.150.100"


class TestDhcpReservations:
    """Static ``ip.dhcp.hosts`` entries -> ``<host>`` elements under <dhcp>."""

    @staticmethod
    def _net(hosts, dhcp_range=True) -> Network:
        dhcp = {}
        if dhcp_range:
            dhcp["range"] = {"start": "10.5.3.50", "end": "10.5.3.100"}
        if hosts is not None:
            dhcp["hosts"] = hosts
        info = {
            "mode": "nat",
            "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0",
                   "dhcp": dhcp},
        }
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            return Network(name="demo-net", info=info, assign_new_bridge=True,
                           provider_config={"use_sudo": False})

    def test_no_hosts_key_leaves_reservations_empty(self):
        assert self._net(None).dhcp_hosts == []

    def test_reservation_renders_as_host_element(self):
        net = self._net([{"mac": "52:54:00:0c:01:01", "ip": "10.5.3.10",
                          "name": "ctrl-1-01"}])
        ip = ET.fromstring(net.generate_xml()).find("ip")
        hosts = ip.findall("dhcp/host")
        assert len(hosts) == 1
        assert hosts[0].get("mac") == "52:54:00:0c:01:01"
        assert hosts[0].get("ip") == "10.5.3.10"
        assert hosts[0].get("name") == "ctrl-1-01"
        # the dynamic range must survive alongside the reservation
        assert ip.find("dhcp/range").get("start") == "10.5.3.50"

    def test_name_is_optional(self):
        net = self._net([{"mac": "52:54:00:0c:01:02", "ip": "10.5.3.11"}])
        host = ET.fromstring(net.generate_xml()).find("ip/dhcp/host")
        assert host.get("name") is None
        assert host.get("ip") == "10.5.3.11"

    def test_reservations_without_a_range_still_emit_dhcp(self):
        # a reservations-only network is valid libvirt: no dynamic pool, every
        # guest pinned. it must not fall through to an empty <ip>
        net = self._net([{"mac": "52:54:00:0c:01:03", "ip": "10.5.3.12"}],
                        dhcp_range=False)
        ip = ET.fromstring(net.generate_xml()).find("ip")
        assert ip.find("dhcp") is not None
        assert ip.find("dhcp/range") is None
        assert ip.find("dhcp/host").get("ip") == "10.5.3.12"

    def test_mac_is_normalised_to_lowercase(self):
        net = self._net([{"mac": "52:54:00:AB:CD:EF", "ip": "10.5.3.13"}])
        assert net.dhcp_hosts[0]["mac"] == "52:54:00:ab:cd:ef"

    def test_ordering_puts_range_before_hosts(self):
        # libvirt accepts either order, but it emits <range> before <host>
        # itself, so matching it keeps a dumpxml diff readable
        net = self._net([{"mac": "52:54:00:0c:01:04", "ip": "10.5.3.14"}])
        children = [el.tag for el in ET.fromstring(net.generate_xml()).find("ip/dhcp")]
        assert children == ["range", "host"]

    def test_short_hex_groups_in_a_mac_are_accepted(self):
        # libvirt parses 52:54:0:c:1:1, so the format check must not be stricter
        net = self._net([{"mac": "52:54:0:c:1:1", "ip": "10.5.3.15"}])
        assert net.dhcp_hosts[0]["mac"] == "52:54:0:c:1:1"

    @pytest.mark.parametrize("name", ["a&b", "a<b", "a>b", "a'b", 'a"b'])
    def test_names_with_xml_metacharacters_are_rejected(self, name):
        # escaping is not enough: libvirt accepts the escaped form at
        # net-define and then fails at net-start, because it writes the name
        # back out unescaped ("EntityRef: expecting ';'"). Verified against
        # libvirt 11.2.0, so the only safe answer is to refuse the name
        with pytest.raises(ValueError, match="libvirt"):
            self._net([{"mac": "52:54:00:0c:01:05", "ip": "10.5.3.16",
                        "name": name}])

    def test_an_ordinary_name_still_renders(self):
        net = self._net([{"mac": "52:54:00:0c:01:05", "ip": "10.5.3.16",
                          "name": "node-01.lab"}])
        host = ET.fromstring(net.generate_xml()).find("ip/dhcp/host")
        assert host.get("name") == "node-01.lab"

    @pytest.mark.parametrize("dhcp", [None, {}, {"range": None, "hosts": None}])
    def test_null_dhcp_blocks_are_not_a_crash(self, dhcp):
        # yaml turns `dhcp:` with nothing under it into None, not into {}
        info = {"mode": "nat",
                "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0",
                       "dhcp": dhcp}}
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            net = Network(name="demo-net", info=info, assign_new_bridge=True,
                          provider_config={"use_sudo": False})
        assert net.dhcp_hosts == []
        assert net.dhcp_range_start is None
        assert ET.fromstring(net.generate_xml()).find("ip/dhcp") is None

    def test_null_ip_block_is_not_a_crash(self):
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            net = Network(name="demo-net", info={"mode": "nat", "ip": None},
                          assign_new_bridge=True,
                          provider_config={"use_sudo": False})
        assert net.dhcp_hosts == []
        assert net.ip_address == "192.168.254.1"

    @pytest.mark.parametrize("hosts, expected", [
        ([{"mac": "52:54:00:0c:01:01"}], "needs both"),
        ([{"ip": "10.5.3.10"}], "needs both"),
        ([{"mac": "52:54:00:0c:01:01", "ip": "not-an-ip"}], "invalid ip"),
        ([{"mac": "52:54:00:0c:01:01", "ip": "10.9.9.9"}], "outside the network"),
        ([{"mac": "52:54:00:0c:01:01", "ip": "10.5.3.10"},
          {"mac": "52:54:00:0C:01:01", "ip": "10.5.3.11"}], "reserved twice"),
        ([{"mac": "52:54:00:0c:01:01", "ip": "10.5.3.10"},
          {"mac": "52:54:00:0c:01:02", "ip": "10.5.3.10"}], "reserved twice"),
        # the address the bridge itself answers on
        ([{"mac": "52:54:00:0c:01:01", "ip": "10.5.3.1"}], "gateway address"),
        ([{"mac": "52:54:00:0c:01:01", "ip": "10.5.3.0"}],
         "network or broadcast"),
        ([{"mac": "52:54:00:0c:01:01", "ip": "10.5.3.255"}],
         "network or broadcast"),
        # hostnames are case-insensitive, so these two collide
        ([{"mac": "52:54:00:0c:01:01", "ip": "10.5.3.10", "name": "ctrl"},
          {"mac": "52:54:00:0c:01:02", "ip": "10.5.3.11", "name": "CTRL"}],
         "reserved twice"),
        ([{"mac": "52-54-00-0c-01-01", "ip": "10.5.3.10"}], "malformed mac"),
        ([{"mac": "5254000c0101", "ip": "10.5.3.10"}], "malformed mac"),
        # a mapping instead of a list of mappings: the leading '-' was forgotten
        ({"mac": "52:54:00:0c:01:01", "ip": "10.5.3.10"}, "must be a list"),
        (["52:54:00:0c:01:01"], "must be a mapping"),
    ])
    def test_invalid_reservations_are_rejected(self, hosts, expected):
        with pytest.raises(ValueError, match=expected):
            self._net(hosts)


class TestWriteXml:

    def test_writes_file_and_returns_absolute_path(self, tmp_path: Path):
        with patch.object(Network, "find_available_bridge_name", return_value="virbr0"):
            n = Network("demo", {}, provider_config={"use_sudo": False})
        target = tmp_path / "net.xml"
        written = n.write_xml(str(target))
        assert written == str(target)
        assert target.exists()
        ET.fromstring(target.read_text())  # parses cleanly


class TestFindAvailableBridgeName:

    @pytest.fixture
    def net(self) -> Network:
        with patch.object(Network, "find_available_bridge_name", return_value="virbr0"):
            return Network("x", {}, provider_config={"use_sudo": False})

    def test_empty_returns_virbr0(self, net: Network):
        with patch.object(net, "_get_libvirt_bridges", return_value=set()), \
             patch.object(net, "_get_cached_bridges", return_value=set()):
            assert net.find_available_bridge_name() == "virbr0"

    def test_skips_contiguous_used_indices(self, net: Network):
        with patch.object(net, "_get_libvirt_bridges",
                          return_value={"virbr0", "virbr1"}), \
             patch.object(net, "_get_cached_bridges", return_value=set()):
            assert net.find_available_bridge_name() == "virbr2"

    def test_fills_gaps(self, net: Network):
        with patch.object(net, "_get_libvirt_bridges",
                          return_value={"virbr0", "virbr2"}), \
             patch.object(net, "_get_cached_bridges", return_value=set()):
            # 0 and 2 used → first free is 1
            assert net.find_available_bridge_name() == "virbr1"

    def test_merges_cached_and_libvirt_sets(self, net: Network):
        with patch.object(net, "_get_libvirt_bridges",
                          return_value={"virbr0"}), \
             patch.object(net, "_get_cached_bridges",
                          return_value={"virbr1", "virbr2"}):
            assert net.find_available_bridge_name() == "virbr3"

    def test_exception_falls_back_to_virbr0(self, net: Network):
        with patch.object(net, "_get_libvirt_bridges",
                          side_effect=RuntimeError("boom")):
            assert net.find_available_bridge_name() == "virbr0"

    def test_ignores_non_virbr_named_bridges(self, net: Network):
        with patch.object(net, "_get_libvirt_bridges",
                          return_value={"virbr0", "br0", "docker0"}), \
             patch.object(net, "_get_cached_bridges", return_value=set()):
            assert net.find_available_bridge_name() == "virbr1"


class TestStaticListNetworks:

    def test_parses_names_from_virsh_output(self):
        output = "default\ndemo-net\n\n"
        with patch(
            "boxman.providers.libvirt.net.VirshCommand"
        ) as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(stdout=output)
            assert Network.list_networks() == ["default", "demo-net"]

    def test_empty_on_failure(self):
        with patch(
            "boxman.providers.libvirt.net.VirshCommand"
        ) as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(ok=False)
            assert Network.list_networks() == []

    def test_active_only_drops_all_flag(self):
        with patch(
            "boxman.providers.libvirt.net.VirshCommand"
        ) as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(stdout="")
            Network.list_networks(active_only=True)
        args, _kwargs = virsh_cls.return_value.execute.call_args
        assert args[0] == "net-list"
        assert "--all" not in args
        assert "--name" in args


class TestStaticGetBridgeFromNetwork:

    NET_XML = """\
<network>
  <name>default</name>
  <bridge name='virbr7'/>
</network>
"""

    def test_returns_bridge_name_from_dumpxml(self):
        with patch.object(Network, "list_networks", return_value=["default"]), \
             patch("boxman.providers.libvirt.net.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(stdout=self.NET_XML)
            assert Network.get_bridge_from_network("default") == "virbr7"

    def test_returns_none_for_unknown_network(self):
        with patch.object(Network, "list_networks", return_value=["other"]):
            assert Network.get_bridge_from_network("default") is None

    def test_returns_none_when_dumpxml_fails(self):
        with patch.object(Network, "list_networks", return_value=["default"]), \
             patch("boxman.providers.libvirt.net.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(ok=False)
            assert Network.get_bridge_from_network("default") is None


class TestNetworkInterfaceAddInterface:

    @pytest.fixture
    def ni(self) -> NetworkInterface:
        return NetworkInterface("vm01", provider_config={"use_sudo": False})

    def test_success_calls_attach_device(self, ni: NetworkInterface):
        with patch.object(ni, "execute", return_value=_result()) as execute:
            ok = ni.add_interface(
                network_source="default", mac_address="52:54:00:aa:bb:cc",
            )
        assert ok is True
        args, _kwargs = execute.call_args
        assert args[0] == "attach-device"
        assert args[1] == "vm01"
        assert "--persistent" in args

    def test_exception_returns_false(self, ni: NetworkInterface):
        with patch.object(ni, "execute", side_effect=RuntimeError("boom")):
            assert ni.add_interface(network_source="default") is False


class TestNetworkInterfaceConfigureFromConfig:

    def test_delegates_with_all_fields(self):
        ni = NetworkInterface("vm01", provider_config=None)
        with patch.object(ni, "add_interface", return_value=True) as add:
            ni.configure_from_config({
                "network_source": "default",
                "link_state": "active",
                "mac": "52:54:00:ff:ff:ff",
                "model": "e1000",
            })
        add.assert_called_once_with(
            network_source="default",
            link_state="active",
            mac_address="52:54:00:ff:ff:ff",
            model="e1000",
            source_type="network",
        )

    def test_model_defaults_to_virtio(self):
        ni = NetworkInterface("vm01", provider_config=None)
        with patch.object(ni, "add_interface", return_value=True) as add:
            ni.configure_from_config({
                "network_source": "default",
                "link_state": "active",
            })
        _args, kwargs = add.call_args
        assert kwargs["model"] == "virtio"


class TestDestroyUndefineExactName:
    """Existence checks must match the network name exactly.

    Regression tests for issue #85 item 13: the checks used to be
    ``net-list | grep -q <name>``, so destroying ``prod`` while
    ``prod-backup`` existed matched the grep and spuriously ran
    ``net-destroy prod`` against an undefined network, failing the remove.
    """

    @pytest.fixture
    def net(self) -> Network:
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            return Network(name="prod", info={}, assign_new_bridge=True,
                           provider_config={"use_sudo": False})

    def test_destroy_skips_net_destroy_when_only_superstring_exists(self, net):
        calls = []

        def listed(active_only: bool = False):
            return ["prod-backup"]

        with patch.object(net, "_listed_networks", side_effect=listed), \
             patch.object(net, "execute",
                          side_effect=lambda *a, **k: calls.append(a) or _result()):
            assert net.destroy_network() is True
        assert not any(args[0] == "net-destroy" for args in calls)

    def test_destroy_runs_net_destroy_when_name_matches_exactly(self, net):
        calls = []

        def listed(active_only: bool = False):
            return ["prod", "prod-backup"]

        with patch.object(net, "_listed_networks", side_effect=listed), \
             patch.object(net, "execute",
                          side_effect=lambda *a, **k: calls.append(a) or _result()):
            assert net.destroy_network() is True
        assert ("net-destroy", "prod") in calls

    def test_destroy_defined_but_inactive_is_left_alone(self, net):
        calls = []

        def listed(active_only: bool = False):
            return [] if active_only else ["prod"]

        with patch.object(net, "_listed_networks", side_effect=listed), \
             patch.object(net, "execute",
                          side_effect=lambda *a, **k: calls.append(a) or _result()):
            assert net.destroy_network() is True
        assert not any(args[0] == "net-destroy" for args in calls)

    def test_destroy_fails_when_libvirt_cannot_be_asked(self, net):
        with patch.object(net, "_listed_networks", return_value=None):
            assert net.destroy_network() is False

    def test_undefine_skips_net_undefine_when_only_superstring_exists(self, net):
        calls = []
        with patch.object(net, "_listed_networks", return_value=["prod-backup"]), \
             patch.object(net, "execute",
                          side_effect=lambda *a, **k: calls.append(a) or _result()):
            assert net.undefine_network() is True
        assert not any(args[0] == "net-undefine" for args in calls)

    def test_undefine_runs_net_undefine_when_name_matches_exactly(self, net):
        calls = []
        with patch.object(net, "_listed_networks", return_value=["prod"]), \
             patch.object(net, "execute",
                          side_effect=lambda *a, **k: calls.append(a) or _result()):
            assert net.undefine_network() is True
        assert ("net-undefine", "prod") in calls

    def test_undefine_fails_when_libvirt_cannot_be_asked(self, net):
        with patch.object(net, "_listed_networks", return_value=None):
            assert net.undefine_network() is False


class TestNatIsLibvirtdsJob:
    """Boxman must not install its own NAT iptables rules.

    Regression tests for issue #85 item 14: a ``<forward mode='nat'/>``
    network already makes libvirtd install the masquerade and FORWARD
    rules. Boxman's hand-rolled duplicates (keyed off
    ``ip route get 8.8.8.8``) fought libvirtd's and leaked stale rules on
    removal, so apply/remove of a nat network must not touch iptables.
    """

    @staticmethod
    def _net(mode: str = "nat", use_sudo: bool = False) -> Network:
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            return Network(name="nat-net", info={"mode": mode},
                           assign_new_bridge=True,
                           provider_config={"use_sudo": use_sudo})

    def test_define_nat_network_runs_no_iptables(self, tmp_path):
        net = self._net("nat")
        with patch.object(net, "check_network_exists"), \
             patch.object(net, "_get_libvirt_bridges", return_value=set()), \
             patch.object(net, "update_network_cache"), \
             patch.object(net, "execute", return_value=_result()), \
             patch.object(net, "execute_shell") as shell:
            assert net.define_network(str(tmp_path / "net.xml")) is True
        for call in shell.call_args_list:
            assert "iptables" not in call.args[0]
            assert "ip route get" not in call.args[0]

    def test_remove_nat_network_runs_no_iptables(self):
        net = self._net("nat")
        with patch.object(net, "destroy_network", return_value=True), \
             patch.object(net, "undefine_network", return_value=True), \
             patch.object(net, "execute_shell") as shell:
            assert net.remove_network() is True
        shell.assert_not_called()


class TestRouteIsolationRules:
    """The routed-network isolation rules stay; sudo is execute_shell's call."""

    @staticmethod
    def _net(use_sudo: bool = False) -> Network:
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            return Network(name="routed-net", info={"mode": "route"},
                           assign_new_bridge=True,
                           provider_config={"use_sudo": use_sudo})

    def test_apply_commands_embed_no_sudo(self):
        net = self._net()
        seen = []
        with patch.object(
                net, "_ensure_rule",
                side_effect=lambda cls, chk, act, present=True:
                    seen.append((chk, act)) or True):
            assert net.apply_route_iptables_rule() is True
        # the three isolation rules (vm-to-vm, vm->host, host->vm) survive
        assert len(seen) == 3
        for check_cmd, action_cmd in seen:
            assert not check_cmd.startswith("sudo")
            assert not action_cmd.startswith("sudo")
            assert "virbr9" in check_cmd

    def test_remove_commands_embed_no_sudo(self):
        net = self._net()
        seen = []
        with patch.object(
                net, "_ensure_rule",
                side_effect=lambda cls, chk, act, present=True:
                    seen.append((chk, act, present)) or True):
            assert net.remove_route_iptables_rule() is True
        assert len(seen) == 3
        for check_cmd, action_cmd, present in seen:
            assert present is False
            assert not check_cmd.startswith("sudo")
            assert not action_cmd.startswith("sudo")

    @pytest.mark.parametrize("use_sudo, expected_prefix", [
        (False, "iptables"),
        (True, "sudo iptables"),
    ])
    def test_sudo_comes_from_use_sudo_via_execute_shell(
            self, use_sudo, expected_prefix):
        net = self._net(use_sudo=use_sudo)
        with patch("boxman.providers.libvirt.commands._shell_run",
                   return_value=_result()) as run:
            net.execute_shell("iptables -C INPUT -i virbr9 -j DROP", warn=True)
        assert run.call_args.args[0].startswith(expected_prefix)


class TestShellQuoting:
    """Config-derived values interpolated into shell strings are quoted.

    Regression tests for issue #85 item 6 (net.py part): the bridge name
    comes from the configuration and used to land in the iptables command
    strings verbatim.
    """

    @staticmethod
    def _net(bridge_name: str) -> Network:
        return Network(name="routed-net",
                       info={"mode": "route", "bridge": {"name": bridge_name}},
                       assign_new_bridge=True,
                       provider_config={"use_sudo": False})

    def test_apply_quotes_a_bridge_name_with_metacharacters(self):
        net = self._net("virbr 9;touch /tmp/x")
        seen = []
        with patch.object(
                net, "_ensure_rule",
                side_effect=lambda cls, chk, act, present=True:
                    seen.append((chk, act)) or True):
            assert net.apply_route_iptables_rule() is True
        assert seen
        for check_cmd, action_cmd in seen:
            assert "'virbr 9;touch /tmp/x'" in check_cmd
            assert "'virbr 9;touch /tmp/x'" in action_cmd

    def test_remove_quotes_a_bridge_name_with_metacharacters(self):
        net = self._net("virbr 9;touch /tmp/x")
        seen = []
        with patch.object(
                net, "_ensure_rule",
                side_effect=lambda cls, chk, act, present=True:
                    seen.append((chk, act)) or True):
            assert net.remove_route_iptables_rule() is True
        assert seen
        for check_cmd, action_cmd in seen:
            assert "'virbr 9;touch /tmp/x'" in check_cmd
            assert "'virbr 9;touch /tmp/x'" in action_cmd

    def test_a_plain_bridge_name_is_left_unquoted(self):
        net = self._net("virbr9")
        seen = []
        with patch.object(
                net, "_ensure_rule",
                side_effect=lambda cls, chk, act, present=True:
                    seen.append(chk) or True):
            assert net.apply_route_iptables_rule() is True
        assert seen
        for check_cmd in seen:
            # shlex.quote leaves a shell-safe name untouched
            assert "virbr9" in check_cmd
            assert "'" not in check_cmd


class TestXmlEscaping:
    """The network template has no autoescape; config values need |e.

    Regression tests for issue #85 item 6 (network.xml.j2 part).
    """

    @staticmethod
    def _net(name: str, bridge_name: str = "virbr9") -> Network:
        return Network(name=name,
                       info={"mode": "nat", "bridge": {"name": bridge_name},
                             "ip": {"address": "10.5.3.1",
                                    "netmask": "255.255.255.0"}},
                       assign_new_bridge=True,
                       provider_config={"use_sudo": False})

    def test_name_with_xml_metacharacters_stays_well_formed(self):
        net = self._net("net&<x>")
        root = ET.fromstring(net.generate_xml())   # must parse
        assert root.findtext("name") == "net&<x>"

    def test_bridge_name_with_a_quote_cannot_break_out_of_the_attribute(self):
        net = self._net("demo-net", bridge_name="virbr0' onload='x")
        root = ET.fromstring(net.generate_xml())   # must parse
        assert root.find("bridge").get("name") == "virbr0' onload='x"
        assert "onload" not in root.find("bridge").attrib
