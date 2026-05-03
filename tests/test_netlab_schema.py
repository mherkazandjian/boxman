"""Unit tests for shared_networks schema resolution.

Covers two integration points:

1. ``BoxmanManager.resolve_adapter_network`` — adapter ``network_source``
   that matches a top-level ``shared_networks:`` entry is rewritten to
   the host bridge name with ``source_type: 'bridge'``, bypassing the
   cluster/project namespacing applied to ordinary libvirt networks.
2. ``NetworkInterface.add_interface`` — when ``source_type='bridge'`` is
   passed, the rendered libvirt XML uses ``<interface type='bridge'>``
   with ``<source bridge=...>`` instead of the libvirt-network form.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from boxman.manager import BoxmanManager
from boxman.providers.libvirt.net import NetworkInterface


pytestmark = pytest.mark.unit


def _make_manager(config: dict) -> BoxmanManager:
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = config
    mgr.logger = MagicMock()
    return mgr


class TestAdapterResolution:

    def test_shared_network_rewrites_to_bridge(self):
        mgr = _make_manager({
            "project": "p1",
            "shared_networks": {
                "lab_mgmt": {"bridge": "shared_lab_mgmt"},
            },
            "clusters": {},
        })
        adapter = {"network_source": "lab_mgmt"}
        mgr.resolve_adapter_network(adapter, cluster_name="cluster_1")

        assert adapter["network_source"] == "shared_lab_mgmt"
        assert adapter["source_type"] == "bridge"

    def test_global_adapter_unchanged(self):
        mgr = _make_manager({
            "project": "p1",
            "shared_networks": {"lab_mgmt": {"bridge": "br1"}},
            "clusters": {},
        })
        adapter = {"network_source": "existing_libvirt_net", "is_global": True}
        mgr.resolve_adapter_network(adapter, cluster_name="cluster_1")

        assert adapter["network_source"] == "existing_libvirt_net"
        assert "source_type" not in adapter

    def test_cluster_scoped_network_prefixed(self):
        mgr = _make_manager({
            "project": "p1",
            "shared_networks": {},
            "clusters": {},
        })
        adapter = {"network_source": "nat1"}
        mgr.resolve_adapter_network(adapter, cluster_name="cluster_1")

        assert "bprj__p1__bprj" in adapter["network_source"]
        assert "clstr__cluster_1__clstr" in adapter["network_source"]
        assert adapter["network_source"].endswith("__nat1")
        assert "source_type" not in adapter

    def test_shared_takes_precedence_over_is_global(self):
        # A name that matches shared_networks should resolve as a bridge
        # even if is_global is also set. Shared bridges are already the
        # "global" concept for hybrid L2 glue.
        mgr = _make_manager({
            "project": "p1",
            "shared_networks": {"lab_mgmt": {"bridge": "br_lab"}},
            "clusters": {},
        })
        adapter = {"network_source": "lab_mgmt", "is_global": True}
        mgr.resolve_adapter_network(adapter, cluster_name="cluster_1")

        assert adapter["network_source"] == "br_lab"
        assert adapter["source_type"] == "bridge"

    def test_no_shared_networks_key_falls_back(self):
        mgr = _make_manager({
            "project": "p1",
            "clusters": {},
        })
        adapter = {"network_source": "nat1"}
        mgr.resolve_adapter_network(adapter, cluster_name="cluster_1")
        assert adapter["network_source"].endswith("__nat1")


class TestInterfaceXmlRendering:

    def _capture_xml(self, **kwargs) -> str:
        """Invoke add_interface and return the rendered XML content."""
        iface = NetworkInterface.__new__(NetworkInterface)
        iface.vm_name = "vm1"
        iface.logger = MagicMock()
        iface.provider_config = {"use_sudo": False}

        captured: dict[str, str] = {}

        def fake_execute(*args, **kw):
            # args: ("attach-device", vm_name, temp_path, "--persistent")
            path = args[2]
            with open(path) as f:
                captured["xml"] = f.read()
            result = MagicMock()
            result.ok = True
            return result

        iface.execute = fake_execute
        ok = iface.add_interface(**kwargs)
        assert ok
        return captured["xml"]

    def test_bridge_source_type_emits_bridge_xml(self):
        xml = self._capture_xml(
            network_source="shared_lab_mgmt",
            source_type="bridge",
            link_state="active",
        )
        assert "<interface type='bridge'>" in xml
        assert "<source bridge='shared_lab_mgmt'/>" in xml
        assert "type='network'" not in xml

    def test_default_source_type_emits_network_xml(self):
        xml = self._capture_xml(
            network_source="virbr-foo",
            link_state="active",
        )
        assert "<interface type='network'>" in xml
        assert "<source network='virbr-foo'/>" in xml
        assert "type='bridge'" not in xml

    def test_mac_address_emitted_for_bridge_form(self):
        xml = self._capture_xml(
            network_source="shared_lab_mgmt",
            source_type="bridge",
            link_state="active",
            mac_address="52:54:00:ab:cd:ef",
        )
        assert "<mac address='52:54:00:ab:cd:ef'/>" in xml
        assert "<interface type='bridge'>" in xml


class TestConfigureFromConfig:
    """Ensure the adapter-dict path propagates source_type."""

    def test_source_type_bridge_passed_through(self):
        iface = NetworkInterface.__new__(NetworkInterface)
        iface.vm_name = "vm1"
        iface.logger = MagicMock()
        iface.provider_config = {}

        with patch.object(NetworkInterface, "add_interface",
                          return_value=True) as add_iface:
            iface.configure_from_config({
                "network_source": "shared_lab_mgmt",
                "link_state": "active",
                "source_type": "bridge",
            })
            add_iface.assert_called_once()
            kwargs = add_iface.call_args.kwargs
            assert kwargs["source_type"] == "bridge"
            assert kwargs["network_source"] == "shared_lab_mgmt"

    def test_default_source_type_is_network(self):
        iface = NetworkInterface.__new__(NetworkInterface)
        iface.vm_name = "vm1"
        iface.logger = MagicMock()
        iface.provider_config = {}

        with patch.object(NetworkInterface, "add_interface",
                          return_value=True) as add_iface:
            iface.configure_from_config({
                "network_source": "virbr-x",
                "link_state": "active",
            })
            kwargs = add_iface.call_args.kwargs
            assert kwargs["source_type"] == "network"
