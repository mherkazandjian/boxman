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

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from boxman.manager import BoxmanManager
from conftest import make_bare_manager

pytestmark = pytest.mark.unit


class TestNetlabProperty:

    def test_none_when_no_config(self):
        mgr = make_bare_manager(None)
        assert mgr.netlab is None

    def test_none_when_containerlab_absent(self):
        mgr = make_bare_manager({"clusters": {}})
        assert mgr.netlab is None

    def test_none_when_explicitly_disabled(self):
        mgr = make_bare_manager({
            "containerlab": {"enabled": False, "lab_name": "netlab"},
        })
        assert mgr.netlab is None

    def test_instance_when_enabled(self, tmp_path):
        mgr = make_bare_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        netlab = mgr.netlab
        assert netlab is not None
        assert netlab.lab_name == "netlab"

    def test_cached(self, tmp_path):
        mgr = make_bare_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        first = mgr.netlab
        second = mgr.netlab
        assert first is second


class TestDeployNetlab:

    def test_noop_when_not_configured(self):
        mgr = make_bare_manager({"clusters": {}})
        # Should not raise or call anything.
        mgr.deploy_netlab()

    def test_calls_preflight_render_deploy_in_order(self, tmp_path):
        mgr = make_bare_manager({
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
        mgr = make_bare_manager({
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
        mgr = make_bare_manager({"clusters": {}})
        mgr.destroy_netlab()  # no raise

    def test_calls_destroy(self, tmp_path):
        mgr = make_bare_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        fake_netlab = MagicMock()
        mgr._netlab = fake_netlab

        mgr.destroy_netlab()
        fake_netlab.preflight.assert_called_once()
        fake_netlab.destroy.assert_called_once()

    def test_survives_missing_binary(self, tmp_path):
        mgr = make_bare_manager({
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
        mgr = make_bare_manager({"clusters": {}})
        mgr.ensure_netlab_up()  # no raise

    def test_calls_preflight_render_ensure_up_in_order(self, tmp_path):
        mgr = make_bare_manager({
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
        mgr = make_bare_manager({"clusters": {}})
        BoxmanManager.netlab_deploy(mgr, MagicMock())
        mgr.logger.error.assert_called_once()

    def test_netlab_destroy_logs_error_when_absent(self):
        mgr = make_bare_manager({"clusters": {}})
        BoxmanManager.netlab_destroy(mgr, MagicMock())
        mgr.logger.error.assert_called_once()

    def test_netlab_inspect_prints_json(self, tmp_path, capsys):
        mgr = make_bare_manager({
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
        mgr = make_bare_manager({
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
        mgr = make_bare_manager({
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
        mgr = make_bare_manager({
            "workspace": {"path": str(tmp_path)},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        args = MagicMock()
        args.node = None
        BoxmanManager.netlab_ssh(mgr, args)
        mgr.logger.error.assert_called_once()


class TestNetlabStartupConfigSourceRoot:
    """Regression: startup-config templates must resolve relative to the
    config file's *directory*, even when ``--conf`` is the bare relative
    default ``conf.yml`` (the documented ``cd boxes/<box> && boxman up``
    flow). Before the abspath() fix, ``os.path.dirname("conf.yml")`` was
    ``""``, which ``render_topology`` treated as "unset" and fell back to
    the workspace dir — so ``configs/<node>.cfg.j2`` was looked up in the
    wrong place and raised ``FileNotFoundError``.
    """

    @staticmethod
    def _capture_source_root(mgr):
        captured = {}
        fake = MagicMock()
        fake.render_topology.side_effect = (
            lambda source_root=None: captured.__setitem__("root", source_root)
        )
        mgr._netlab = fake
        return captured

    @staticmethod
    def _assert_resolves_to_cwd(captured, tmp_path):
        root = captured["root"]
        assert root, "source_root must not be empty (the bug produced '')"
        assert os.path.isabs(root), f"source_root must be absolute, got {root!r}"
        assert os.path.realpath(root) == os.path.realpath(str(tmp_path))

    def test_deploy_netlab_relative_conf_resolves_to_box_dir(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mgr = make_bare_manager({
            "workspace": {"path": str(tmp_path / "ws")},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        mgr.config_path = "conf.yml"  # bare relative default
        captured = self._capture_source_root(mgr)

        mgr.deploy_netlab()
        self._assert_resolves_to_cwd(captured, tmp_path)

    def test_ensure_netlab_up_relative_conf_resolves_to_box_dir(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mgr = make_bare_manager({
            "workspace": {"path": str(tmp_path / "ws")},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        mgr.config_path = "conf.yml"
        captured = self._capture_source_root(mgr)

        mgr.ensure_netlab_up()
        self._assert_resolves_to_cwd(captured, tmp_path)


class TestNetlabWorkdirAbsolute:
    """Regression: the netlab workdir must be absolute.

    Containerlab resolves relative ``startup-config`` paths in the
    topology against the topology file's own directory, so a relative
    workdir would emit paths like ``netlab/configs/sw1.cfg`` inside
    ``netlab/<lab>.clab.yml`` — containerlab then looks for
    ``netlab/netlab/configs/sw1.cfg`` and the deploy fails.
    """

    def test_workdir_absolute_when_config_path_relative(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mgr = make_bare_manager({
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })
        mgr.config_path = "conf.yml"  # bare relative default, no workspace.path

        netlab = mgr.netlab
        assert netlab is not None
        assert netlab.workdir.is_absolute()
        assert netlab.workdir == tmp_path

    def test_workdir_absolute_when_workspace_path_relative(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        mgr = make_bare_manager({
            "workspace": {"path": "ws"},
            "containerlab": {"lab_name": "netlab", "topology": {"nodes": {}}},
        })

        netlab = mgr.netlab
        assert netlab is not None
        assert netlab.workdir.is_absolute()
        assert netlab.workdir == tmp_path / "ws"

    def test_rendered_topology_startup_config_paths_absolute(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "sw1.cfg.j2").write_text("hostname sw1\n")
        mgr = make_bare_manager({
            "containerlab": {
                "lab_name": "netlab",
                "topology": {
                    "nodes": {
                        "sw1": {
                            "kind": "arista_ceos",
                            "startup-config": "configs/sw1.cfg.j2",
                        },
                    },
                },
            },
        })
        mgr.config_path = "conf.yml"

        topology_path = mgr.netlab.render_topology()

        rendered = yaml.safe_load(topology_path.read_text())
        startup = rendered["topology"]["nodes"]["sw1"]["startup-config"]
        assert os.path.isabs(startup), (
            f"startup-config path must be absolute, got {startup!r}")
        assert Path(startup).exists()
