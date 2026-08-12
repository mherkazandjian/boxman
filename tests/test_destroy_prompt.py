"""
Regression test for #85 item 16: the ``destroy`` confirmation prompt must
abort cleanly on EOF (CI / piped stdin) instead of dying with an EOFError
traceback — same guard its siblings (destroy_runtime, _recreate_network,
snapshot_collapse) already have. ``update``'s removed-VMs prompt got the
same guard in the final item-16 sweep.
"""

import types
from unittest.mock import MagicMock

import pytest

from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit


def _manager():
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = {
        "project": "demo",
        "clusters": {
            "cluster_1": {
                "workdir": "/tmp/ws/c1",
                "vms": {"node01": {}},
            },
        },
    }
    mgr.provider = MagicMock()
    mgr.logger = MagicMock()
    mgr.cache = MagicMock()
    # Registered project → destroy gets past the "nothing to do" gate.
    mgr.cache.projects = {"demo": {}}
    mgr._runtime_instance = MagicMock()  # not a DockerComposeRuntime
    mgr.config_path = "conf.yml"
    return mgr


def test_destroy_prompt_aborts_cleanly_on_eof(monkeypatch, capsys):
    mgr = _manager()

    def _eof_input(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof_input)
    ns = types.SimpleNamespace(auto_accept=False, templates=False)

    # Must return cleanly, not raise EOFError.
    mgr.destroy(ns)

    assert "No input available, aborted." in capsys.readouterr().out
    # Nothing destructive ran: the runtime was never started.
    mgr._runtime_instance.ensure_ready.assert_not_called()


def test_update_prompt_aborts_cleanly_on_eof(monkeypatch, capsys):
    """update()'s removed-VMs confirmation must abort cleanly on EOF too."""
    mgr = _manager()
    # No VMs left in config, one stale VM in libvirt → the removal prompt.
    mgr.config = {"project": "demo", "clusters": {}}
    monkeypatch.setattr(
        BoxmanManager, "_update_sessions_with_runtime", lambda self: None)
    monkeypatch.setattr(
        BoxmanManager, "ensure_shared_bridges", lambda self: None)
    monkeypatch.setattr(
        BoxmanManager, "reconcile_networks", lambda self, **kw: {})
    monkeypatch.setattr(
        BoxmanManager, "report_network_results", lambda self, r: None)
    monkeypatch.setattr(
        BoxmanManager, "_find_all_existing_project_vms",
        lambda self: ["bprj__demo__bprj_cluster_1_old01"])
    monkeypatch.setattr(BoxmanManager, "_run_parallel", MagicMock())

    def _eof_input(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof_input)
    ns = types.SimpleNamespace(dry_run=False, yes=False,
                               recreate_networks=False)

    # Must return cleanly, not raise EOFError.
    mgr.update(ns)

    assert "No input available, aborted." in capsys.readouterr().out
    # Nothing destructive ran: no VM was destroyed in parallel.
    mgr._run_parallel.assert_not_called()
