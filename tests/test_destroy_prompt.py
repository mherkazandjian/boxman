"""
Regression test for #85 item 16: the ``destroy`` confirmation prompt must
abort cleanly on EOF (CI / piped stdin) instead of dying with an EOFError
traceback — same guard its siblings (destroy_runtime, _recreate_network,
snapshot_collapse) already have.
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
