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

from boxman.exceptions import ConfigError
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

    def test_bridge_mode_renders_only_forward_and_existing_bridge(self):
        net = Network(
            name="migration-net",
            info={"mode": "bridge", "bridge": {"name": "br-migrate"}},
            assign_new_bridge=True,
            provider_config={"use_sudo": False},
        )
        root = ET.fromstring(net.generate_xml())
        assert root.find("forward").get("mode") == "bridge"
        assert root.find("bridge").attrib == {"name": "br-migrate"}
        assert root.find("mac") is None
        assert root.find("ip") is None


class TestBridgeModeValidation:

    @staticmethod
    def _assert_rejected_before_define(tmp_path, info, message):
        net = Network(
            "migration-net", info, assign_new_bridge=True,
            provider_config={"use_sudo": False})
        net.virsh.execute = MagicMock()
        xml_path = tmp_path / "invalid.xml"
        with pytest.raises(ConfigError, match=message):
            net.define_network(str(xml_path))
        net.virsh.execute.assert_not_called()
        assert not xml_path.exists()

    def test_requires_an_explicit_bridge_name(self, tmp_path):
        self._assert_rejected_before_define(
            tmp_path, {"mode": "bridge"}, "requires.*bridge.name")

    def test_bridge_block_must_be_a_mapping(self, tmp_path):
        self._assert_rejected_before_define(
            tmp_path, {"mode": "bridge", "bridge": ["br0"]},
            "bridge must be a mapping")

    def test_rejects_unsafe_or_overlong_bridge_name(self, tmp_path):
        self._assert_rejected_before_define(
            tmp_path,
            {"mode": "bridge", "bridge": {"name": "br0;touch /tmp/x"}},
            "invalid bridge name")

    def test_rejects_non_string_bridge_name(self, tmp_path):
        self._assert_rejected_before_define(
            tmp_path,
            {"mode": "bridge", "bridge": {"name": 123}},
            "invalid bridge name")

    @pytest.mark.parametrize("extra,field", [
        ({"ip": {"address": "10.0.0.1"}}, "ip"),
        ({"mac": "52:54:00:00:00:01"}, "mac"),
        ({"bridge": {"name": "br0", "stp": "on"}}, "bridge.stp"),
        ({"bridge": {"name": "br0", "delay": 0}}, "bridge.delay"),
    ])
    def test_rejects_fields_owned_by_the_host_bridge(
        self, tmp_path, extra, field
    ):
        info = {"mode": "bridge", "bridge": {"name": "br0"}}
        info.update(extra)
        self._assert_rejected_before_define(
            tmp_path, info, field.replace('.', r'\.'))

    def test_rejects_unknown_modes_before_libvirt(self, tmp_path):
        self._assert_rejected_before_define(
            tmp_path,
            {"mode": "hostdev", "bridge": {"name": "br-old"}},
            "unsupported forward mode")

    def test_legacy_unsupported_network_can_still_be_removed(self):
        net = Network(
            "old-open-net",
            {"mode": "open", "bridge": {"name": "br-old"}},
            assign_new_bridge=True,
            provider_config={"use_sudo": False})
        net.destroy_network = MagicMock(return_value=True)
        net.undefine_network = MagicMock(return_value=True)
        net.remove_route_iptables_rule = MagicMock()

        assert net.remove_network() is True
        net.destroy_network.assert_called_once_with()
        net.undefine_network.assert_called_once_with()
        net.remove_route_iptables_rule.assert_not_called()

    def test_bridge_mode_may_reuse_an_existing_bridge_from_cache(self):
        net = Network(
            "migration-net",
            {"mode": "bridge", "bridge": {"name": "br-migrate"}},
            provider_config={"use_sudo": False},
            manager=MagicMock(),
        )
        net.manager.config = {"project": "current"}
        net.manager._runtime_name = "local"
        net.manager.cache.projects = {
            "other": {
                "runtime": "local",
                "networks": {
                    "other-name": {"bridge_name": "br-migrate"}
                },
            }
        }

        net.check_network_exists()


class TestBridgeModeDefinition:

    @staticmethod
    def _net(tmp_path):
        manager = MagicMock()
        manager.config = {"project": "p1"}
        manager._runtime_name = "local"
        manager.cache.projects = {"p1": {"runtime": "local"}}
        return Network(
            "migration-net",
            {"mode": "bridge", "bridge": {"name": "br-migrate"}},
            provider_config={"use_sudo": False}, manager=manager,
        )

    def test_missing_host_bridge_fails_before_net_define(self, tmp_path):
        net = self._net(tmp_path)
        net.virsh.execute_shell = MagicMock(return_value=_result(ok=False))
        net.virsh.execute = MagicMock()

        with pytest.raises(ConfigError, match="does not exist"):
            net.define_network(str(tmp_path / "bridge.xml"))

        net.virsh.execute.assert_not_called()
        assert not (tmp_path / "bridge.xml").exists()

    def test_existing_bridge_defines_without_firewall_setup(self, tmp_path):
        net = self._net(tmp_path)
        net.virsh.execute_shell = MagicMock(side_effect=[
            _result(),
            _result(stdout="0x1003\n"),  # IFF_UP; operstate may still be unknown
        ])
        net.virsh.execute = MagicMock(return_value=_result())
        net._get_libvirt_bridges = MagicMock(return_value={"br-migrate"})
        net._log_bridge_mode_managed_owner = MagicMock()
        net.check_network_exists = MagicMock()
        net.update_network_cache = MagicMock()
        net.apply_route_iptables_rule = MagicMock()

        assert net.define_network(str(tmp_path / "bridge.xml")) is True
        commands = [call.args[0] for call in net.virsh.execute.call_args_list]
        assert commands == ["net-define", "net-start", "net-autostart"]
        net._log_bridge_mode_managed_owner.assert_called_once_with()
        net.apply_route_iptables_rule.assert_not_called()

    def test_administratively_down_bridge_is_rejected(self, tmp_path):
        net = self._net(tmp_path)
        net.virsh.execute_shell = MagicMock(side_effect=[
            _result(),
            _result(stdout="0x1002\n"),  # IFF_UP is clear
        ])
        net.virsh.execute = MagicMock()

        with pytest.raises(ConfigError, match="administratively down"):
            net.define_network(str(tmp_path / "down-bridge.xml"))

        net.virsh.execute.assert_not_called()
        commands = [call.args[0] for call in net.virsh.execute_shell.call_args_list]
        assert commands == [
            "test -d /sys/class/net/br-migrate/bridge",
            "cat /sys/class/net/br-migrate/flags",
        ]

    def test_unreadable_bridge_admin_state_is_rejected(self, tmp_path):
        net = self._net(tmp_path)
        net.virsh.execute_shell = MagicMock(side_effect=[
            _result(),
            _result(ok=False, stderr="permission denied"),
        ])

        with pytest.raises(ConfigError, match="could not read administrative state"):
            net.define_network(str(tmp_path / "unknown-bridge.xml"))

    @pytest.mark.parametrize("stdout", [
        "not-a-number",   # a runtime that answers, but not with the flags
        "",               # an empty read
        None,             # no stdout at all on the result object
    ])
    def test_unparseable_bridge_flags_are_rejected(self, tmp_path, stdout):
        # a successful `cat` whose payload is not an integer must fail the
        # same way an unreadable one does. Guessing "probably up" here would
        # hand libvirt a bridge that cannot carry traffic
        net = self._net(tmp_path)
        net.virsh.execute_shell = MagicMock(side_effect=[
            _result(),
            _result(stdout=stdout),
        ])
        net.virsh.execute = MagicMock()

        with pytest.raises(ConfigError,
                           match="could not read administrative state"):
            net.define_network(str(tmp_path / "bad-flags.xml"))

        net.virsh.execute.assert_not_called()

    @pytest.mark.parametrize("uri", [
        "qemu+ssh://hypervisor.example/system",
        "qemu://localhost/system",
    ])
    def test_authority_uri_does_not_probe_the_client_namespace(
        self, tmp_path, uri
    ):
        manager = MagicMock()
        manager.config = {"project": "p1"}
        manager._runtime_name = "local"
        manager.cache.projects = {"p1": {"runtime": "local"}}
        net = Network(
            "migration-net",
            {"mode": "bridge", "bridge": {"name": "br-migrate"}},
            provider_config={
                "use_sudo": False,
                "uri": uri,
            },
            manager=manager,
        )
        net.virsh.execute_shell = MagicMock()
        net.virsh.execute = MagicMock(return_value=_result())
        net._get_libvirt_bridges = MagicMock(return_value=set())
        net.check_network_exists = MagicMock()
        net.update_network_cache = MagicMock()

        assert net.define_network(str(tmp_path / "remote-bridge.xml")) is True
        net.virsh.execute_shell.assert_not_called()
        assert [call.args[0] for call in net.virsh.execute.call_args_list] == [
            "net-define", "net-start", "net-autostart",
        ]

    def test_remote_start_failure_rolls_back_without_cache_entry(self, tmp_path):
        manager = MagicMock()
        manager.config = {"project": "p1"}
        manager._runtime_name = "local"
        manager.cache.projects = {"p1": {"runtime": "local"}}
        net = Network(
            "migration-net",
            {"mode": "bridge", "bridge": {"name": "br-migrate"}},
            provider_config={
                "use_sudo": False,
                "uri": "qemu+ssh://hypervisor.example/system",
            },
            manager=manager,
        )
        net.virsh.execute_shell = MagicMock()
        calls = []

        def execute(command, *args, **kwargs):
            calls.append(command)
            if command == "net-start":
                raise RuntimeError("remote bridge is missing")
            return _result()

        net.virsh.execute = MagicMock(side_effect=execute)
        net._get_libvirt_bridges = MagicMock(return_value=set())
        net.check_network_exists = MagicMock()
        net.update_network_cache = MagicMock()

        assert net.define_network(str(tmp_path / "remote-bridge.xml")) is False
        assert calls == ["net-define", "net-start", "net-undefine"]
        net.virsh.execute_shell.assert_not_called()
        net.update_network_cache.assert_not_called()

    def test_cache_oserror_rolls_back_network_and_cache(self, tmp_path):
        manager = MagicMock()
        manager.config = {"project": "p1"}
        manager._runtime_name = "local"
        manager.cache.projects = {"p1": {"runtime": "local"}}
        manager.cache.write_projects_cache.side_effect = OSError(
            "projects.json is read-only")
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            net = Network(
                "managed-net", {"mode": "route"},
                provider_config={"use_sudo": False}, manager=manager)
        calls = []
        net.virsh.execute = MagicMock(
            side_effect=lambda command, *a, **k: calls.append(command) or _result())
        net._get_libvirt_bridges = MagicMock(return_value=set())
        net.check_network_exists = MagicMock()
        net.apply_route_iptables_rule = MagicMock(return_value=True)
        net.remove_route_iptables_rule = MagicMock(return_value=True)

        assert net.define_network(str(tmp_path / "managed.xml")) is False
        assert calls == [
            "net-define", "net-start", "net-autostart",
            "net-destroy", "net-undefine",
        ]
        net.apply_route_iptables_rule.assert_called_once_with()
        net.remove_route_iptables_rule.assert_called_once_with()
        assert manager.cache.projects == {"p1": {"runtime": "local"}}

    def test_route_rule_failure_rolls_back_without_cache_entry(self, tmp_path):
        manager = MagicMock()
        manager.config = {"project": "p1"}
        manager._runtime_name = "local"
        manager.cache.projects = {"p1": {"runtime": "local"}}
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            net = Network(
                "managed-net", {"mode": "route"},
                provider_config={"use_sudo": False}, manager=manager)
        calls = []
        net.virsh.execute = MagicMock(
            side_effect=lambda command, *a, **k: calls.append(command) or _result())
        net._get_libvirt_bridges = MagicMock(return_value=set())
        net.check_network_exists = MagicMock()
        net.apply_route_iptables_rule = MagicMock(return_value=False)
        net.remove_route_iptables_rule = MagicMock(return_value=True)
        net.update_network_cache = MagicMock()

        assert net.define_network(str(tmp_path / "managed.xml")) is False
        assert calls == [
            "net-define", "net-start", "net-autostart",
            "net-destroy", "net-undefine",
        ]
        net.apply_route_iptables_rule.assert_called_once_with()
        net.remove_route_iptables_rule.assert_called_once_with()
        net.update_network_cache.assert_not_called()
        assert manager.cache.projects == {"p1": {"runtime": "local"}}

    def test_baseexception_is_not_swallowed(self, tmp_path):
        manager = MagicMock()
        manager.config = {"project": "p1"}
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            net = Network(
                "managed-net", {"mode": "nat"},
                provider_config={"use_sudo": False}, manager=manager)
        net.virsh.execute = MagicMock(return_value=_result())
        net._get_libvirt_bridges = MagicMock(return_value=set())
        net.check_network_exists = MagicMock()
        net.update_network_cache = MagicMock(side_effect=KeyboardInterrupt)

        with pytest.raises(KeyboardInterrupt):
            net.define_network(str(tmp_path / "managed.xml"))

    def test_removal_needs_no_firewall_cleanup(self, tmp_path):
        net = self._net(tmp_path)
        net.destroy_network = MagicMock(return_value=True)
        net.undefine_network = MagicMock(return_value=True)
        net.remove_route_iptables_rule = MagicMock()
        assert net.remove_network() is True
        net.remove_route_iptables_rule.assert_not_called()


class TestDefineNetworkRollbackDiagnostics:
    """A rollback that cannot finish must say so, step by step.

    ``define_network`` unwinds route rules, ``net-destroy`` and
    ``net-undefine`` when bring-up fails. Each of those can fail in turn,
    and the warnings are the only place an operator learns which pieces of
    the half-built network were left on the host — the return value is a
    bare ``False`` either way.
    """

    @staticmethod
    def _routed_net():
        manager = MagicMock()
        manager.config = {"project": "p1"}
        manager._runtime_name = "local"
        manager.cache.projects = {"p1": {"runtime": "local"}}
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            net = Network("managed-net", {"mode": "route"},
                          provider_config={"use_sudo": False}, manager=manager)
        net._get_libvirt_bridges = MagicMock(return_value=set())
        net.check_network_exists = MagicMock()
        net.apply_route_iptables_rule = MagicMock(return_value=True)
        # bring-up itself succeeds; the cache write is what fails, so the
        # rollback runs with all three steps armed
        net.update_network_cache = MagicMock(
            side_effect=OSError("projects.json is read-only"))
        net.logger = MagicMock()
        return net

    def test_failed_rollback_steps_are_each_warned_about(self, tmp_path):
        net = self._routed_net()
        net.remove_route_iptables_rule = MagicMock(return_value=False)
        net.virsh.execute = MagicMock(
            side_effect=lambda command, *a, **k: _result(
                ok=command not in ("net-destroy", "net-undefine")))

        assert net.define_network(str(tmp_path / "managed.xml")) is False

        warnings = [c.args[0] for c in net.logger.warning.call_args_list]
        assert any("could not remove route isolation rules" in w
                   for w in warnings)
        assert any("could not stop the partially defined network" in w
                   for w in warnings)
        assert any("could not undefine the partially defined network" in w
                   for w in warnings)

    def test_raising_rollback_steps_report_their_cause(self, tmp_path):
        # a rollback step that raises must not abort the remaining steps:
        # failing to drop the iptables rules is no reason to leave the
        # network defined as well
        net = self._routed_net()
        net.remove_route_iptables_rule = MagicMock(
            side_effect=RuntimeError("iptables lock held"))

        def execute(command, *_args, **_kwargs):
            if command in ("net-destroy", "net-undefine"):
                raise RuntimeError(f"{command} refused")
            return _result()

        net.virsh.execute = MagicMock(side_effect=execute)

        assert net.define_network(str(tmp_path / "managed.xml")) is False

        warnings = [c.args[0] for c in net.logger.warning.call_args_list]
        assert any("iptables lock held" in w for w in warnings)
        assert any("net-destroy refused" in w for w in warnings)
        assert any("net-undefine refused" in w for w in warnings)
        # every step was still attempted
        attempted = [c.args[0] for c in net.virsh.execute.call_args_list]
        assert attempted == [
            "net-define", "net-start", "net-autostart",
            "net-destroy", "net-undefine",
        ]


class TestBridgeModeManagedOwnerDiagnostic:

    @staticmethod
    def _net() -> Network:
        return Network(
            "migration-net",
            {"mode": "bridge", "bridge": {"name": "br-migrate"}},
            provider_config={"use_sudo": False},
        )

    def test_logs_managed_owner_and_lifecycle_risk_at_debug(self):
        net = self._net()
        net.logger = MagicMock()
        net.logger.isEnabledFor.return_value = True
        net._listed_networks = MagicMock(return_value=["nat-owner", "peer"])
        xml = {
            "nat-owner": (
                "<network><forward mode='nat'/>"
                "<bridge name='br-migrate'/></network>"),
            "peer": (
                "<network><forward mode='bridge'/>"
                "<bridge name='br-migrate'/></network>"),
        }
        net.virsh.execute = MagicMock(
            side_effect=lambda _cmd, name, **_kwargs: _result(stdout=xml[name]))

        net._log_bridge_mode_managed_owner()

        message = net.logger.debug.call_args.args[0]
        assert "nat-owner (nat)" in message
        assert "destroying an owner removes" in message
        assert "peer" not in message

    def test_diagnostic_does_no_virsh_work_when_debug_is_disabled(self):
        net = self._net()
        net.logger = MagicMock()
        net.logger.isEnabledFor.return_value = False
        net._listed_networks = MagicMock()

        net._log_bridge_mode_managed_owner()

        net._listed_networks.assert_not_called()

    def test_unlistable_networks_are_reported_instead_of_dumped(self):
        # this is a diagnostic, so libvirt being unreachable is a note, not
        # a failure — but it must not silently read as "no owners found"
        net = self._net()
        net.logger = MagicMock()
        net.logger.isEnabledFor.return_value = True
        net._listed_networks = MagicMock(return_value=None)
        net.virsh.execute = MagicMock()

        net._log_bridge_mode_managed_owner()

        net.virsh.execute.assert_not_called()
        assert "could not inspect ownership" in net.logger.debug.call_args.args[0]

    def test_unreadable_and_malformed_definitions_are_skipped(self):
        # one bad definition among many must not hide the owner that the
        # operator actually needs to know about
        net = self._net()
        net.logger = MagicMock()
        net.logger.isEnabledFor.return_value = True
        net._listed_networks = MagicMock(
            return_value=["vanished", "truncated", "nat-owner"])
        responses = {
            # disappeared between net-list and net-dumpxml
            "vanished": _result(ok=False, stderr="no such network"),
            # well-formed enough to reach the parser, not enough to parse
            "truncated": _result(stdout="<network><forward mode='nat'"),
            "nat-owner": _result(stdout=(
                "<network><forward mode='nat'/>"
                "<bridge name='br-migrate'/></network>")),
        }
        net.virsh.execute = MagicMock(
            side_effect=lambda _cmd, name, **_kwargs: responses[name])

        net._log_bridge_mode_managed_owner()

        message = net.logger.debug.call_args.args[0]
        assert "nat-owner (nat)" in message
        assert "vanished" not in message
        assert "truncated" not in message


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
        with patch.object(ni.virsh, "execute", return_value=_result()) as execute:
            ok = ni.add_interface(
                network_source="default", mac_address="52:54:00:aa:bb:cc",
            )
        assert ok is True
        args, _kwargs = execute.call_args
        assert args[0] == "attach-device"
        assert args[1] == "vm01"
        assert "--persistent" in args

    def test_exception_returns_false(self, ni: NetworkInterface):
        with patch.object(ni.virsh, "execute", side_effect=RuntimeError("boom")):
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
             patch.object(net.virsh, "execute",
                          side_effect=lambda *a, **k: calls.append(a) or _result()):
            assert net.destroy_network() is True
        assert not any(args[0] == "net-destroy" for args in calls)

    def test_destroy_runs_net_destroy_when_name_matches_exactly(self, net):
        calls = []

        def listed(active_only: bool = False):
            return ["prod", "prod-backup"]

        with patch.object(net, "_listed_networks", side_effect=listed), \
             patch.object(net.virsh, "execute",
                          side_effect=lambda *a, **k: calls.append(a) or _result()):
            assert net.destroy_network() is True
        assert ("net-destroy", "prod") in calls

    def test_destroy_defined_but_inactive_is_left_alone(self, net):
        calls = []

        def listed(active_only: bool = False):
            return [] if active_only else ["prod"]

        with patch.object(net, "_listed_networks", side_effect=listed), \
             patch.object(net.virsh, "execute",
                          side_effect=lambda *a, **k: calls.append(a) or _result()):
            assert net.destroy_network() is True
        assert not any(args[0] == "net-destroy" for args in calls)

    def test_destroy_fails_when_libvirt_cannot_be_asked(self, net):
        with patch.object(net, "_listed_networks", return_value=None):
            assert net.destroy_network() is False

    def test_undefine_skips_net_undefine_when_only_superstring_exists(self, net):
        calls = []
        with patch.object(net, "_listed_networks", return_value=["prod-backup"]), \
             patch.object(net.virsh, "execute",
                          side_effect=lambda *a, **k: calls.append(a) or _result()):
            assert net.undefine_network() is True
        assert not any(args[0] == "net-undefine" for args in calls)

    def test_undefine_runs_net_undefine_when_name_matches_exactly(self, net):
        calls = []
        with patch.object(net, "_listed_networks", return_value=["prod"]), \
             patch.object(net.virsh, "execute",
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
             patch.object(net.virsh, "execute", return_value=_result()), \
             patch.object(net.virsh, "execute_shell") as shell:
            assert net.define_network(str(tmp_path / "net.xml")) is True
        for call in shell.call_args_list:
            assert "iptables" not in call.args[0]
            assert "ip route get" not in call.args[0]

    def test_remove_nat_network_runs_no_iptables(self):
        net = self._net("nat")
        with patch.object(net, "destroy_network", return_value=True), \
             patch.object(net, "undefine_network", return_value=True), \
             patch.object(net.virsh, "execute_shell") as shell:
            assert net.remove_network() is True
        shell.assert_not_called()


class TestCacheSurvivesADestroyedProject:
    """`destroy` unregisters the project; a later define must still work.

    Redefining through the reconcile path used to raise a bare KeyError on
    the project name. It surfaced as the opaque "Error defining network:
    '<project>'", the network was never created, and the VMs then failed to
    start with "Network not found".
    """

    @staticmethod
    def _net(projects: dict) -> Network:
        manager = MagicMock()
        manager.config = {"project": "gone"}
        manager.config_path = "/tmp/some/conf.yml"
        manager._runtime_name = "local"
        manager.cache.projects = projects
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            return Network("net1", {"mode": "nat"},
                           provider_config={"use_sudo": False},
                           manager=manager)

    def test_missing_project_is_re_registered_instead_of_raising(self):
        projects: dict = {}
        net = self._net(projects)
        net.update_network_cache()
        assert "gone" in projects
        assert projects["gone"]["networks"]["net1"]["bridge_name"] == "virbr9"

    def test_re_registered_entry_is_well_formed(self):
        # other code reads conf/runtime off the entry, so a bare {} would be
        # a different kind of landmine
        projects: dict = {}
        self._net(projects).update_network_cache()
        assert projects["gone"]["conf"] == "/tmp/some/conf.yml"
        assert projects["gone"]["runtime"] == "local"

    def test_an_existing_project_entry_is_preserved(self):
        projects = {"gone": {"conf": "/orig.yml", "runtime": "local",
                             "networks": {"other": {"bridge_name": "virbr1"}}}}
        self._net(projects).update_network_cache()
        assert projects["gone"]["conf"] == "/orig.yml"
        assert "other" in projects["gone"]["networks"]
        assert "net1" in projects["gone"]["networks"]


class TestIsolationOfAnUndefinedNetwork:
    """A network that does not exist is not an isolation failure.

    Reporting one adds a misleading second error on top of whatever actually
    failed to define the network.
    """

    @staticmethod
    def _net() -> Network:
        with patch.object(Network, "get_bridge_from_network", return_value=None):
            return Network("ghost-net", {"mode": "route"},
                           assign_new_bridge=False,
                           provider_config={"use_sudo": False})

    def test_undefined_network_reports_absent(self):
        net = self._net()
        with patch.object(Network, "list_networks", return_value=["other-net"]):
            assert net.reconcile_isolation() == 'absent'

    def test_defined_but_bridgeless_network_is_still_a_failure(self):
        net = self._net()
        with patch.object(Network, "list_networks", return_value=["ghost-net"]):
            assert net.reconcile_isolation() == 'failed'


class TestRoutedDhcpOptionTrimming:
    """A routed network must not advertise what it then blocks.

    dnsmasq offers the bridge address as both gateway (option 3) and resolver
    (option 6), but the isolation rules drop traffic to it. The router option
    is the damaging one: it installs a default route at metric 0, outranking
    the guest's real NIC and black-holing all of its traffic. An empty
    ``dhcp-option=N`` makes dnsmasq omit the option entirely.
    """

    DNSMASQ_NS = {'dnsmasq': 'http://libvirt.org/schemas/network/dnsmasq/1.0'}

    @staticmethod
    def _xml(mode: str, with_dhcp: bool = True) -> str:
        info: dict = {"mode": mode, "bridge": {"name": "virbr9"}}
        if mode != 'bridge':
            info["ip"] = {"address": "10.0.14.1", "netmask": "255.255.255.0"}
            if with_dhcp:
                info["ip"]["dhcp"] = {"range": {"start": "10.0.14.2",
                                                "end": "10.0.14.254"}}
        return Network("t", info, assign_new_bridge=True,
                       provider_config={"use_sudo": False}).generate_xml()

    def _options(self, xml: str) -> list[str]:
        root = ET.fromstring(xml)  # must stay well-formed with the namespace
        block = root.find('dnsmasq:options', self.DNSMASQ_NS)
        if block is None:
            return []
        return [o.get('value')
                for o in block.findall('dnsmasq:option', self.DNSMASQ_NS)]

    def test_routed_dhcp_network_suppresses_router_and_dns(self):
        assert sorted(self._options(self._xml('route'))) == [
            'dhcp-option=3', 'dhcp-option=6']

    def test_routed_network_without_dhcp_declares_nothing(self):
        # nothing is handed out, so there is no offer to trim
        assert self._options(self._xml('route', with_dhcp=False)) == []

    @pytest.mark.parametrize("mode", ["nat", "bridge"])
    def test_other_modes_are_untouched(self, mode):
        # only route mode installs the host<->guest block, so only route mode
        # advertises a gateway the guest cannot reach
        xml = self._xml(mode)
        assert self._options(xml) == []
        assert 'dnsmasq' not in xml

    def test_namespace_is_only_declared_when_used(self):
        assert 'xmlns:dnsmasq' in self._xml('route')
        assert 'xmlns:dnsmasq' not in self._xml('route', with_dhcp=False)

    def test_reservations_alone_also_trim_the_offer(self):
        info = {"mode": "route", "bridge": {"name": "virbr9"},
                "ip": {"address": "10.0.14.1", "netmask": "255.255.255.0",
                       "dhcp": {"hosts": [{"mac": "52:54:00:0c:01:01",
                                           "ip": "10.0.14.10"}]}}}
        xml = Network("t", info, assign_new_bridge=True,
                      provider_config={"use_sudo": False}).generate_xml()
        assert sorted(self._options(xml)) == ['dhcp-option=3', 'dhcp-option=6']


TRIMMED_XML = (
    "<network><dnsmasq:options>"
    "<dnsmasq:option value='dhcp-option=3'/>"
    "<dnsmasq:option value='dhcp-option=6'/>"
    "</dnsmasq:options></network>"
)
UNTRIMMED_XML = "<network><forward mode='route'/></network>"


class TestRouteIsolationChains:
    """Routed-network isolation lives in a chain per direction.

    Loose rules in INPUT/OUTPUT could not be reconciled safely: ``iptables
    -I`` pushes to the top, so a DROP re-inserted after an ACCEPT ends up
    above it and silently re-breaks DHCP. A chain is declarative -- flush and
    refill -- so ordering is owned rather than inherited from insertion
    history.
    """

    @staticmethod
    def _net(with_dhcp: bool = True, bridge_name: str = "virbr9",
             use_sudo: bool = False) -> Network:
        info: dict = {"mode": "route", "bridge": {"name": bridge_name}}
        if with_dhcp:
            info["ip"] = {
                "address": "10.0.14.1", "netmask": "255.255.255.0",
                "dhcp": {"range": {"start": "10.0.14.2",
                                   "end": "10.0.14.254"}},
            }
        return Network(name="routed-net", info=info, assign_new_bridge=True,
                       provider_config={"use_sudo": use_sudo})

    @staticmethod
    def _record(net: Network, rules_present: bool = False,
                trimmed: bool = True, chain_rules: list | None = None):
        """Stub both executors and record every iptables command."""
        calls: list[str] = []

        def shell(command, *_args, **_kwargs):
            calls.append(command)
            if command.startswith("iptables -S "):
                if chain_rules is None:
                    return _result(ok=False, return_code=1)
                return _result(stdout="\n".join(chain_rules))
            if " -C " in f" {command} ":
                return _result(ok=rules_present,
                               return_code=0 if rules_present else 1)
            return _result(ok=True, return_code=0)

        def execute(cmd, *_args, **_kwargs):
            if cmd == "net-dumpxml":
                return _result(
                    stdout=TRIMMED_XML if trimmed else UNTRIMMED_XML)
            return _result()

        net.virsh.execute_shell = MagicMock(side_effect=shell)
        net.virsh.execute = MagicMock(side_effect=execute)
        return calls

    def _apply(self, net: Network, **kwargs) -> list[str]:
        calls = self._record(net, **kwargs)
        assert net.apply_route_iptables_rule() is True
        return calls

    def test_chain_is_created_flushed_and_hooked(self):
        calls = self._apply(self._net())
        for chain, hook, flag in (("BXM_ISO_I_virbr9", "INPUT", "-i"),
                                  ("BXM_ISO_O_virbr9", "OUTPUT", "-o")):
            assert f"iptables -N {chain}" in calls
            assert f"iptables -F {chain}" in calls
            assert f"iptables -I {hook} {flag} virbr9 -j {chain}" in calls

    def test_accept_is_filled_before_drop(self):
        calls = self._apply(self._net())
        accept = calls.index("iptables -A BXM_ISO_I_virbr9 -p udp --dport 67 -j ACCEPT")
        drop = calls.index("iptables -A BXM_ISO_I_virbr9 -j DROP")
        assert accept < drop

    def test_dhcp_ports_are_direction_specific(self):
        calls = self._apply(self._net())
        assert "iptables -A BXM_ISO_I_virbr9 -p udp --dport 67 -j ACCEPT" in calls
        assert "iptables -A BXM_ISO_O_virbr9 -p udp --dport 68 -j ACCEPT" in calls

    def test_no_dhcp_hole_without_a_dhcp_block(self):
        calls = self._apply(self._net(with_dhcp=False))
        assert not any(c.startswith("iptables -A") and "--dport" in c
                       for c in calls)
        assert "iptables -A BXM_ISO_I_virbr9 -j DROP" in calls

    def test_hole_stays_shut_until_the_live_offer_is_trimmed(self):
        # an upgrade must not open DHCP on a network whose dnsmasq still
        # advertises the bridge as router and DNS: the resulting lease
        # installs a metric-0 default route to an unreachable gateway and
        # black-holes the guest. Such a network plans as `action: none`, so
        # nothing else would stop this.
        calls = self._apply(self._net(), trimmed=False)
        assert not any(c.startswith("iptables -A") and "--dport" in c
                       for c in calls)
        assert "iptables -A BXM_ISO_I_virbr9 -j DROP" in calls

    def test_reservations_alone_also_open_the_hole(self):
        info = {
            "mode": "route", "bridge": {"name": "virbr9"},
            "ip": {"address": "10.0.14.1", "netmask": "255.255.255.0",
                   "dhcp": {"hosts": [{"mac": "52:54:00:0c:01:01",
                                       "ip": "10.0.14.10"}]}},
        }
        net = Network("routed-net", info, assign_new_bridge=True,
                      provider_config={"use_sudo": False})
        assert any("--dport 67" in c for c in self._apply(net))

    def test_vm_to_vm_forward_rule_survives(self):
        calls = self._apply(self._net())
        assert ("iptables -I FORWARD -i virbr9 -o virbr9 -j ACCEPT") in calls

    def test_legacy_loose_rules_are_migrated_away(self):
        calls = self._apply(self._net(), rules_present=True)
        assert "iptables -D INPUT -i virbr9 -j DROP" in calls
        assert "iptables -D OUTPUT -o virbr9 -j DROP" in calls

    def test_removal_unhooks_flushes_and_deletes(self):
        net = self._net()
        calls = self._record(net, rules_present=False,
                             chain_rules=["-A BXM_ISO_I_virbr9 -j DROP"])
        assert net.remove_route_iptables_rule() is True
        for chain in ("BXM_ISO_I_virbr9", "BXM_ISO_O_virbr9"):
            assert f"iptables -F {chain}" in calls
            assert f"iptables -X {chain}" in calls

    def test_removal_deletes_every_duplicate_hook(self):
        # -D removes ONE match; a duplicated jump would keep the chain
        # referenced and make -X fail, while teardown still reported success
        net = self._net()
        seen: list[str] = []
        remaining = {"INPUT": 3, "OUTPUT": 1}

        def shell(command, *_a, **_k):
            seen.append(command)
            if command.startswith("iptables -S "):
                return _result(stdout="-A chain -j DROP")
            for hook in remaining:
                if f" -C {hook} " in command and "BXM_ISO" in command:
                    return _result(ok=remaining[hook] > 0,
                                   return_code=0 if remaining[hook] > 0 else 1)
                if f" -D {hook} " in command and "BXM_ISO" in command:
                    remaining[hook] -= 1
                    return _result(ok=True, return_code=0)
            if " -C " in f" {command} ":
                return _result(ok=False, return_code=1)
            return _result(ok=True, return_code=0)

        net.virsh.execute_shell = MagicMock(side_effect=shell)
        net.virsh.execute = MagicMock(return_value=_result(stdout=TRIMMED_XML))
        assert net.remove_route_iptables_rule() is True
        assert remaining == {"INPUT": 0, "OUTPUT": 0}

    def test_removal_fails_loudly_when_the_chain_cannot_be_deleted(self):
        net = self._net()

        def shell(command, *_a, **_k):
            if command.startswith("iptables -S "):
                return _result(stdout="-A BXM_ISO_I_virbr9 -j DROP")
            if command.startswith("iptables -X "):
                return _result(ok=False, return_code=1,
                               stderr="chain is still referenced")
            if " -C " in f" {command} ":
                return _result(ok=False, return_code=1)
            return _result(ok=True, return_code=0)

        net.virsh.execute_shell = MagicMock(side_effect=shell)
        net.virsh.execute = MagicMock(return_value=_result(stdout=TRIMMED_XML))
        assert net.remove_route_iptables_rule() is False

    def test_chain_name_is_sanitised_not_quoted(self):
        net = self._net(bridge_name="br-a.b")
        calls = self._apply(net)
        assert any("BXM_ISO_I_br_a_b" in c for c in calls)
        assert not any("BXM_ISO_I_br-a.b" in c for c in calls)

    def test_bridge_name_with_metacharacters_is_quoted(self):
        net = self._net(bridge_name="virbr 9;touch /tmp/x")
        calls = self._apply(net)
        matches = [c for c in calls if "-i " in c or "-o " in c]
        assert matches
        for cmd in matches:
            assert "'virbr 9;touch /tmp/x'" in cmd

    def test_a_plain_bridge_name_is_left_unquoted(self):
        calls = self._apply(self._net())
        assert not any("'" in c for c in calls)

    @pytest.mark.parametrize("use_sudo, expected_prefix", [
        (False, "iptables"),
        (True, "sudo iptables"),
    ])
    def test_sudo_comes_from_use_sudo_via_execute_shell(
            self, use_sudo, expected_prefix):
        net = self._net(use_sudo=use_sudo)
        with patch("boxman.providers.libvirt.commands._shell_run",
                   return_value=_result()) as run:
            net.virsh.execute_shell("iptables -C INPUT -i virbr9 -j DROP", warn=True)
        assert run.call_args.args[0].startswith(expected_prefix)


class TestIsolationContentsAreChecked:
    """A hooked chain is not necessarily an isolating one.

    A chain that is still jumped to but empty, or that lost its terminal
    DROP, returns from the user chain and stops isolating anything -- while a
    jump-only probe reports everything fine and a reconcile returns 'ok'
    instead of 'repaired', hiding the exposure.
    """

    @staticmethod
    def _net(with_dhcp: bool = True) -> Network:
        info: dict = {"mode": "route", "bridge": {"name": "virbr9"}}
        if with_dhcp:
            info["ip"] = {"address": "10.0.14.1", "netmask": "255.255.255.0",
                          "dhcp": {"range": {"start": "10.0.14.2",
                                             "end": "10.0.14.254"}}}
        return Network("routed-net", info, assign_new_bridge=True,
                       provider_config={"use_sudo": False})

    @staticmethod
    def _probe(net: Network, chain_rules: list, trimmed: bool = True):
        def shell(command, *_a, **_k):
            if command.startswith("iptables -S "):
                chain = command.split()[-1]
                port = 67 if chain.startswith("BXM_ISO_I_") else 68
                rules = [r.replace("CHAIN", chain).replace("PORT", str(port))
                         for r in chain_rules]
                return _result(stdout="\n".join(rules))
            if " -C " in f" {command} ":
                return _result(ok=True, return_code=0)   # jump present
            return _result(ok=True, return_code=0)
        net.virsh.execute_shell = MagicMock(side_effect=shell)
        net.virsh.execute = MagicMock(return_value=_result(
            stdout=TRIMMED_XML if trimmed else UNTRIMMED_XML))
        return net._isolation_is_intact()

    def test_correct_contents_are_intact(self):
        assert self._probe(self._net(), [
            "-A CHAIN -p udp -m udp --dport PORT -j ACCEPT",
            "-A CHAIN -j DROP"]) is True

    def test_empty_chain_is_not_intact(self):
        # hooked but empty: traffic returns from the user chain unfiltered
        assert self._probe(self._net(), []) is False

    def test_missing_terminal_drop_is_not_intact(self):
        assert self._probe(self._net(), [
            "-A CHAIN -p udp -m udp --dport PORT -j ACCEPT"]) is False

    def test_unexpected_dhcp_hole_is_not_intact(self):
        # no dhcp declared, so an ACCEPT here is drift, not correctness
        assert self._probe(self._net(with_dhcp=False), [
            "-A CHAIN -p udp -m udp --dport PORT -j ACCEPT",
            "-A CHAIN -j DROP"]) is False

    def test_missing_expected_hole_is_not_intact(self):
        assert self._probe(self._net(), [
            "-A CHAIN -j DROP"]) is False

    def test_a_broad_accept_is_not_intact(self):
        # the case a "terminal DROP plus the right hole" check waves through:
        # everything is accepted before either rule is reached
        assert self._probe(self._net(), [
            "-A CHAIN -j ACCEPT",
            "-A CHAIN -p udp -m udp --dport PORT -j ACCEPT",
            "-A CHAIN -j DROP"]) is False

    def test_an_extra_rule_after_the_drop_is_not_intact(self):
        assert self._probe(self._net(), [
            "-A CHAIN -p udp -m udp --dport PORT -j ACCEPT",
            "-A CHAIN -j DROP",
            "-A CHAIN -j ACCEPT"]) is False

    def test_a_conditional_drop_is_not_intact(self):
        # a DROP carrying match criteria only drops some traffic
        assert self._probe(self._net(with_dhcp=False), [
            "-A CHAIN -p tcp -m tcp --dport 22 -j DROP"]) is False

    def test_a_hole_on_the_wrong_port_is_not_intact(self):
        assert self._probe(self._net(), [
            "-A CHAIN -p udp -m udp --dport 9999 -j ACCEPT",
            "-A CHAIN -j DROP"]) is False

    def test_match_modules_are_normalised_not_compared(self):
        # iptables echoes back `-m udp` that was never written; that must not
        # read as drift
        assert self._probe(self._net(), [
            "-A CHAIN -p udp -m udp --dport PORT -j ACCEPT",
            "-A CHAIN -j DROP"]) is True

    def test_untrimmed_offer_expects_no_hole(self):
        # while the live offer is untrimmed the hole must NOT be present,
        # so a drop-only chain is the correct state
        assert self._probe(self._net(), [
            "-A CHAIN -j DROP"], trimmed=False) is True


class TestRouteIsolationDrift:
    """Isolation is host iptables state, so it does not survive a reboot.

    libvirt autostarts the network again regardless, so without a reconcile
    the network comes back up unprotected and stays that way. Detecting that
    is what separates "already fine" from "was open, now repaired".
    """

    @staticmethod
    def _net(mode: str = "route") -> Network:
        return Network(name="routed-net",
                       info={"mode": mode, "bridge": {"name": "virbr9"}},
                       assign_new_bridge=True,
                       provider_config={"use_sudo": False})

    def test_a_present_jump_alone_is_not_enough(self):
        # the jump can be there while the chain is empty, which isolates
        # nothing; contents are checked in TestIsolationContentsAreChecked
        net = self._net()
        net.virsh.execute_shell = MagicMock(return_value=_result(return_code=0))
        net.virsh.execute = MagicMock(return_value=_result(stdout=""))
        assert net._isolation_is_intact() is False

    def test_not_intact_when_a_jump_is_missing(self):
        net = self._net()
        net.virsh.execute_shell = MagicMock(
            return_value=_result(ok=False, return_code=1))
        assert net._isolation_is_intact() is False

    def test_reconcile_reports_repaired_when_rules_had_vanished(self):
        net = self._net()
        net._isolation_is_intact = MagicMock(return_value=False)
        net.apply_route_iptables_rule = MagicMock(return_value=True)
        assert net.reconcile_isolation() == 'repaired'
        net.apply_route_iptables_rule.assert_called_once_with()

    def test_reconcile_reports_ok_when_nothing_had_drifted(self):
        net = self._net()
        net._isolation_is_intact = MagicMock(return_value=True)
        net.apply_route_iptables_rule = MagicMock(return_value=True)
        assert net.reconcile_isolation() == 'ok'

    def test_check_only_reports_drift_without_touching_anything(self):
        net = self._net()
        net._isolation_is_intact = MagicMock(return_value=False)
        net.apply_route_iptables_rule = MagicMock()
        assert net.reconcile_isolation(check_only=True) == 'drifted'
        net.apply_route_iptables_rule.assert_not_called()

    def test_failure_to_apply_is_reported(self):
        net = self._net()
        net._isolation_is_intact = MagicMock(return_value=False)
        net.apply_route_iptables_rule = MagicMock(return_value=False)
        assert net.reconcile_isolation() == 'failed'

    def test_non_routed_networks_are_skipped(self):
        net = self._net(mode="nat")
        net.virsh.execute_shell = MagicMock()
        assert net.reconcile_isolation() == 'skipped'
        net.virsh.execute_shell.assert_not_called()


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
