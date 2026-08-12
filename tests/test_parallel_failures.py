"""
Regression tests for #85 item 4: parallel child-process failures must be
reported, not swallowed — a worker that raises or is killed must surface as
a failure (and must never deadlock the parent on a blocking queue.get).
"""

import os
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
    return mgr


def _ok_worker(value):
    return value


def _raising_worker():
    raise RuntimeError("boom")


def _dying_worker():
    # Killed before it can report — simulates a hard crash (OOM, segfault).
    os._exit(1)


class TestRunParallel:

    def test_success_collects_results(self):
        mgr = _manager()
        results, failures = mgr._run_parallel(
            [("a", _ok_worker, (1,)), ("b", _ok_worker, (2,))])
        assert results == {"a": 1, "b": 2}
        assert failures == {}

    def test_raising_worker_is_a_reported_failure(self):
        mgr = _manager()
        results, failures = mgr._run_parallel(
            [("good", _ok_worker, (1,)), ("bad", _raising_worker, ())])
        assert results == {"good": 1}
        assert "bad" in failures
        assert "boom" in failures["bad"]
        mgr.logger.error.assert_called()

    def test_killed_worker_is_a_failure_not_a_hang(self):
        mgr = _manager()
        results, failures = mgr._run_parallel([("dead", _dying_worker, ())])
        assert results == {}
        assert "dead" in failures

    def test_empty_task_list(self):
        mgr = _manager()
        assert mgr._run_parallel([]) == ({}, {})


class TestRestoreRetryLoop:

    def test_raising_restore_worker_never_reports_success(self, monkeypatch):
        """The old queue-drain loop printed 'all VMs restored successfully'
        when a worker died before queue.put — it must retry/fail instead."""
        monkeypatch.setattr("boxman.manager_parts.snapshots.time.sleep", lambda _s: None)
        mgr = _manager()
        mgr.provider.snapshot_restore.side_effect = RuntimeError("libvirt gone")
        mgr.provider.validate_snapshot.return_value = (True, [])
        ns = types.SimpleNamespace(snapshot_name="s1", vms="all", cluster=None)
        mgr.snapshot_restore(ns)
        infos = [c.args[0] for c in mgr.logger.info.call_args_list if c.args]
        assert not any("all VMs restored successfully" in m for m in infos)
        errors = [c.args[0] for c in mgr.logger.error.call_args_list if c.args]
        assert any("libvirt gone" in m for m in errors)


class TestGetConnectInfo:

    def test_crashed_child_does_not_deadlock(self):
        """A worker that raises must not hang the parent on queue.get()."""
        mgr = _manager()
        mgr.provider.get_vm_ip_addresses.side_effect = RuntimeError("virsh down")
        assert mgr.get_connect_info() is False
