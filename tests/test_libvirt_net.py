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
