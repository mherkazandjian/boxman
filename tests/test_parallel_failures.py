"""
Regression tests for #85 item 4: parallel child-process failures must be
reported, not swallowed — a worker that raises or is killed must surface as
a failure (and must never deadlock the parent on a blocking queue.get).
"""

import os
import types
from unittest.mock import MagicMock

import pytest

from boxman.exceptions import ProvisionError
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
    # No managed saved state by default: these helpers build managers for
    # tests about other things, and a MagicMock would otherwise answer the
    # managed-save/snapshot conflict probe with a truthy mock (#164 FB-3).
    mgr.provider.has_managed_save.return_value = False
    mgr.logger = MagicMock()
    return mgr


def _ok_worker(value):
    return value


def _raising_worker():
    raise RuntimeError("boom")


def _dying_worker():
    # Killed before it can report — simulates a hard crash (OOM, segfault).
    os._exit(1)


def _dying_update_worker(*_args):
    # Same hard crash, but tolerant of the update-worker's arg list.
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
        when a worker died before queue.put — it must retry, then raise
        SnapshotError after the final round (#85 item 3)."""
        from boxman.exceptions import SnapshotError
        monkeypatch.setattr("boxman.manager_parts.snapshots.time.sleep", lambda _s: None)
        mgr = _manager()
        mgr.provider.snapshot_restore.side_effect = RuntimeError("libvirt gone")
        mgr.provider.validate_snapshot.return_value = (True, [])
        ns = types.SimpleNamespace(snapshot_name="s1", vms="all", cluster=None)
        with pytest.raises(SnapshotError, match="gave up after 20 rounds"):
            mgr.snapshot_restore(ns)
        infos = [c.args[0] for c in mgr.logger.info.call_args_list if c.args]
        assert not any("all VMs restored successfully" in m for m in infos)
        errors = [c.args[0] for c in mgr.logger.error.call_args_list if c.args]
        assert any("libvirt gone" in m for m in errors)


class TestUpdateParallelFailures:

    def test_dying_update_worker_lands_in_failed_summary(self, monkeypatch):
        """update()'s per-VM diff/apply batch runs through _run_parallel:
        a worker killed before queue.put must land in the failed summary,
        not vanish silently (#85 items 4/16)."""
        mgr = _manager()
        full = "bprj__demo__bprj_cluster_1_node01"
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
            lambda self: [full])
        monkeypatch.setattr(
            BoxmanManager, "setup_ssh_access", lambda self: None)
        monkeypatch.setattr(
            BoxmanManager, "connect_info", lambda self: None)
        monkeypatch.setattr(
            BoxmanManager, "_update_single_vm", _dying_update_worker)
        ns = types.SimpleNamespace(
            dry_run=False, yes=True, recreate_networks=False)
        # the summary is still logged per VM, and the command now also fails:
        # reporting the failure and then exiting 0 was the defect
        with pytest.raises(ProvisionError, match="node01"):
            mgr.update(ns)
        errors = [c.args[0] for c in mgr.logger.error.call_args_list if c.args]
        assert any("failed" in msg and "node01" in msg for msg in errors)


class TestGetConnectInfo:

    def test_crashed_child_does_not_deadlock(self):
        """A worker that raises must not hang the parent on queue.get()."""
        mgr = _manager()
        mgr.provider.get_vm_ip_addresses.side_effect = RuntimeError("virsh down")
        assert mgr.get_connect_info() is False


def _big_payload_worker(size):
    # Returns more than a pipe buffer's worth of data. The old
    # join-then-drain loop deadlocked here: the child blocked in the queue
    # feeder waiting for the parent to read, the parent sat in join().
    return "x" * size


def _concurrency_probe(live, peak, lock, hold):
    """Track how many copies of this worker are alive at the same time."""
    import time
    with lock:
        live.value += 1
        peak.value = max(peak.value, live.value)
    time.sleep(hold)
    with lock:
        live.value -= 1
    return True


class TestParallelWorkerLimit:
    """The fan-out cap: one process per VM does not scale to a large
    cluster, so the window is bounded (and still overridable)."""

    def test_explicit_request_wins_over_env(self, monkeypatch):
        mgr = _manager()
        monkeypatch.setenv("BOXMAN_MAX_PARALLEL", "3")
        assert mgr._parallel_worker_limit(2, 10) == 2

    def test_env_override_is_used(self, monkeypatch):
        mgr = _manager()
        monkeypatch.setenv("BOXMAN_MAX_PARALLEL", "3")
        assert mgr._parallel_worker_limit(None, 10) == 3

    def test_default_is_capped_and_never_exceeds_batch(self, monkeypatch):
        mgr = _manager()
        monkeypatch.delenv("BOXMAN_MAX_PARALLEL", raising=False)
        assert mgr._parallel_worker_limit(None, 200) <= 8
        assert mgr._parallel_worker_limit(None, 2) == 2

    def test_non_positive_means_unbounded(self, monkeypatch):
        mgr = _manager()
        monkeypatch.delenv("BOXMAN_MAX_PARALLEL", raising=False)
        assert mgr._parallel_worker_limit(0, 50) == 50
        assert mgr._parallel_worker_limit(-1, 50) == 50

    def test_garbage_env_warns_and_falls_back(self, monkeypatch):
        mgr = _manager()
        monkeypatch.setenv("BOXMAN_MAX_PARALLEL", "lots")
        assert mgr._parallel_worker_limit(None, 200) <= 8
        mgr.logger.warning.assert_called()


class TestRunParallelScheduling:

    def test_large_payloads_do_not_deadlock(self):
        """Four 1 MiB payloads: every result must come back."""
        mgr = _manager()
        size = 1024 * 1024
        tasks = [(f"t{i}", _big_payload_worker, (size,)) for i in range(4)]
        results, failures = mgr._run_parallel(tasks, max_workers=2)
        assert failures == {}
        assert sorted(results) == ["t0", "t1", "t2", "t3"]
        assert all(len(v) == size for v in results.values())

    def test_concurrency_never_exceeds_the_limit(self):
        from multiprocessing import Lock, Value

        mgr = _manager()
        live = Value("i", 0)
        peak = Value("i", 0)
        lock = Lock()
        tasks = [
            (f"vm{i}", _concurrency_probe, (live, peak, lock, 0.15))
            for i in range(8)
        ]
        results, failures = mgr._run_parallel(tasks, max_workers=2)
        assert failures == {}
        assert len(results) == 8
        assert peak.value <= 2, f"peak concurrency was {peak.value}"

    def test_all_tasks_still_run_when_bounded(self):
        mgr = _manager()
        tasks = [(f"n{i}", _ok_worker, (i,)) for i in range(10)]
        results, failures = mgr._run_parallel(tasks, max_workers=3)
        assert failures == {}
        assert results == {f"n{i}": i for i in range(10)}
