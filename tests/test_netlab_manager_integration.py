"""Tests for BoxmanManager <-> ContainerlabManager wiring.

Covers:
- ``manager.netlab`` property is lazy; returns ``None`` unless a
  ``containerlab:`` block is present and enabled.
- ``deploy_netlab`` runs preflight → render_topology → deploy in order.
- ``destroy_netlab`` calls destroy (and survives a missing-binary
  preflight).
- ``provision`` / ``deprovision`` hook points invoke netlab at the
  expected lifecycle stages.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from boxman.manager import BoxmanManager


pytestmark = pytest.mark.unit


def _make_manager(config: dict | None) -> BoxmanManager:
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = config
    mgr.config_path = None
    mgr.logger = MagicMock()
    mgr._netlab = None
    return mgr


class TestNetlabProperty:

    def test_none_when_no_config(self):
        mgr = _make_manager(None)
        assert mgr.netlab is None

    def test_none_when_containerlab_absent(self):
        mgr = _make_manager({"clusters": {}})
        assert mgr.netlab is None

    def test_none_when_explicitly_disabled(self):
        mgr = _make_manager({
            "containerlab": {"enabled": False, "lab_name": "netlab"},
        })
        assert mgr.netlab is None

    def test_instance_when_enabled(self, tmp_path):
        mgr = _make_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        netlab = mgr.netlab
        assert netlab is not None
        assert netlab.lab_name == "netlab"

    def test_cached(self, tmp_path):
        mgr = _make_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        first = mgr.netlab
        second = mgr.netlab
        assert first is second


class TestDeployNetlab:

    def test_noop_when_not_configured(self):
        mgr = _make_manager({"clusters": {}})
        # Should not raise or call anything.
        mgr.deploy_netlab()

    def test_calls_preflight_render_deploy_in_order(self, tmp_path):
        mgr = _make_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        fake_netlab = MagicMock(name="ContainerlabManager")
        mgr._netlab = fake_netlab  # pre-seed to bypass lazy construction

        call_order = []
        fake_netlab.preflight.side_effect = lambda: call_order.append("preflight")
        fake_netlab.render_topology.side_effect = \
            lambda source_root=None: call_order.append("render")
        fake_netlab.deploy.side_effect = lambda: call_order.append("deploy")

        mgr.deploy_netlab()
        assert call_order == ["preflight", "render", "deploy"]

    def test_propagates_preflight_error(self, tmp_path):
        mgr = _make_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        from boxman.netlab.containerlab import ContainerlabNotInstalled
        fake_netlab = MagicMock()
        fake_netlab.preflight.side_effect = ContainerlabNotInstalled("boom")
        mgr._netlab = fake_netlab

        with pytest.raises(ContainerlabNotInstalled):
            mgr.deploy_netlab()
        fake_netlab.render_topology.assert_not_called()
        fake_netlab.deploy.assert_not_called()


class TestDestroyNetlab:

    def test_noop_when_not_configured(self):
        mgr = _make_manager({"clusters": {}})
        mgr.destroy_netlab()  # no raise

    def test_calls_destroy(self, tmp_path):
        mgr = _make_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        fake_netlab = MagicMock()
        mgr._netlab = fake_netlab

        mgr.destroy_netlab()
        fake_netlab.preflight.assert_called_once()
        fake_netlab.destroy.assert_called_once()

    def test_survives_missing_binary(self, tmp_path):
        mgr = _make_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        from boxman.netlab.containerlab import ContainerlabNotInstalled
        fake_netlab = MagicMock()
        fake_netlab.preflight.side_effect = ContainerlabNotInstalled("gone")
        mgr._netlab = fake_netlab

        mgr.destroy_netlab()  # no raise
        fake_netlab.destroy.assert_not_called()
        mgr.logger.warning.assert_called()


class TestEnsureNetlabUp:
    """`boxman up` path: reconcile the lab without tearing it down."""

    def test_noop_when_not_configured(self):
        mgr = _make_manager({"clusters": {}})
        mgr.ensure_netlab_up()  # no raise

    def test_calls_preflight_render_ensure_up_in_order(self, tmp_path):
        mgr = _make_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        fake_netlab = MagicMock()
        mgr._netlab = fake_netlab

        call_order = []
        fake_netlab.preflight.side_effect = lambda: call_order.append("preflight")
        fake_netlab.render_topology.side_effect = \
            lambda source_root=None: call_order.append("render")
        fake_netlab.ensure_up.side_effect = lambda: call_order.append("ensure_up")

        mgr.ensure_netlab_up()
        assert call_order == ["preflight", "render", "ensure_up"]


class TestNetlabCliHandlers:
    """Unit-test the four static CLI handlers without argparse plumbing."""

    def test_netlab_deploy_logs_error_when_absent(self):
        mgr = _make_manager({"clusters": {}})
        BoxmanManager.netlab_deploy(mgr, MagicMock())
        mgr.logger.error.assert_called_once()

    def test_netlab_destroy_logs_error_when_absent(self):
        mgr = _make_manager({"clusters": {}})
        BoxmanManager.netlab_destroy(mgr, MagicMock())
        mgr.logger.error.assert_called_once()

    def test_netlab_inspect_prints_json(self, tmp_path, capsys):
        mgr = _make_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        fake_netlab = MagicMock()
        fake_netlab.inspect.return_value = {"nodes": ["r1", "sw1"]}
        mgr._netlab = fake_netlab

        BoxmanManager.netlab_inspect(mgr, MagicMock())
        out = capsys.readouterr().out
        assert '"nodes"' in out
        assert "r1" in out and "sw1" in out
        fake_netlab.preflight.assert_called_once()

    def test_netlab_ssh_prints_command(self, tmp_path, capsys):
        mgr = _make_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {
                "lab_name": "netlab",
                "topology": {"nodes": {"sw1": {"kind": "arista_ceos"}}},
            },
        })
        args = MagicMock()
        args.node = "sw1"
        args.user = None
        BoxmanManager.netlab_ssh(mgr, args)

        out = capsys.readouterr().out.strip()
        assert out == "ssh admin@clab-netlab-sw1"

    def test_netlab_ssh_with_custom_user(self, tmp_path, capsys):
        mgr = _make_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {
                "lab_name": "netlab",
                "topology": {"nodes": {"sw1": {"kind": "arista_ceos"}}},
            },
        })
        args = MagicMock()
        args.node = "sw1"
        args.user = "root"
        BoxmanManager.netlab_ssh(mgr, args)

        assert "ssh root@clab-netlab-sw1" in capsys.readouterr().out

    def test_netlab_ssh_missing_node_logs_error(self, tmp_path):
        mgr = _make_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        args = MagicMock()
        args.node = None
        BoxmanManager.netlab_ssh(mgr, args)
        mgr.logger.error.assert_called_once()
