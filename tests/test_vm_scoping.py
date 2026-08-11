"""
Regression tests for #85 item 2: ``--vms`` (and ``--cluster``) must scope
``control``, ``storage`` and ``snapshot list``/``snapshot log`` — previously
these verbs silently operated on every VM in the project.
"""

import types
from unittest.mock import MagicMock

import pytest

import boxman.manager
from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit


def _manager():
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = {
        "project": "demo",
        "clusters": {
            "cluster_1": {
                "workdir": "/tmp/ws/c1",
                "vms": {"service01": {}, "node01": {}},
            },
            "cluster_2": {
                "workdir": "/tmp/ws/c2",
                "vms": {"service01": {}, "node01": {}},
            },
        },
    }
    mgr.provider = MagicMock()
    mgr.logger = MagicMock()
    return mgr


def _full(cluster, vm):
    return f"bprj__demo__bprj_{cluster}_{vm}"


class _FakeProcess:
    """Stand-in for multiprocessing.Process that only records its args."""

    instances = []

    def __init__(self, target=None, args=()):
        self.target = target
        self.args = args
        _FakeProcess.instances.append(self)

    def start(self):
        pass

    def join(self):
        pass


@pytest.fixture
def fake_process(monkeypatch):
    _FakeProcess.instances = []
    monkeypatch.setattr(boxman.manager, "Process", _FakeProcess)
    return _FakeProcess.instances


class TestControlScoping:

    def test_save_scoped_by_vms(self):
        mgr = _manager()
        ns = types.SimpleNamespace(vms="node01", cluster=None)
        BoxmanManager.save_vm(mgr, ns)
        saved = {c.args[0] for c in mgr.provider.save_vm.call_args_list}
        assert saved == {_full("cluster_1", "node01"), _full("cluster_2", "node01")}

    def test_save_defaults_to_all_vms(self):
        mgr = _manager()
        BoxmanManager.save_vm(mgr, types.SimpleNamespace())
        assert mgr.provider.save_vm.call_count == 4

    def test_suspend_scoped_by_vms(self):
        mgr = _manager()
        ns = types.SimpleNamespace(vms="cluster_2_service01", cluster=None)
        BoxmanManager.suspend_vm(mgr, ns)
        suspended = {c.args[0] for c in mgr.provider.suspend_vm.call_args_list}
        assert suspended == {_full("cluster_2", "service01")}

    def test_resume_scoped_by_cluster(self):
        mgr = _manager()
        ns = types.SimpleNamespace(vms="all", cluster="cluster_1")
        BoxmanManager.resume_vm(mgr, ns)
        resumed = {c.args[0] for c in mgr.provider.resume_vm.call_args_list}
        assert resumed == {_full("cluster_1", "service01"), _full("cluster_1", "node01")}

    def test_start_scoped_by_vms(self):
        mgr = _manager()
        ns = types.SimpleNamespace(vms="node01", cluster=None, restore=False)
        BoxmanManager.start_vm(mgr, ns)
        started = {c.args[0] for c in mgr.provider.start_vm.call_args_list}
        assert started == {_full("cluster_1", "node01"), _full("cluster_2", "node01")}


class TestSnapshotListLogScoping:

    def test_snapshot_list_scoped_by_vms(self):
        mgr = _manager()
        ns = types.SimpleNamespace(vms="node01", cluster=None)
        BoxmanManager.snapshot_list(mgr, ns)
        listed = {c.args[0] for c in mgr.provider.snapshot_list.call_args_list}
        assert listed == {_full("cluster_1", "node01"), _full("cluster_2", "node01")}

    def test_snapshot_list_defaults_to_all_vms(self):
        mgr = _manager()
        BoxmanManager.snapshot_list(mgr, types.SimpleNamespace())
        assert mgr.provider.snapshot_list.call_count == 4

    def test_snapshot_log_scoped_by_vms(self):
        mgr = _manager()
        mgr.provider.snapshot_log_data.return_value = {"chain": [], "current": None}
        ns = types.SimpleNamespace(vms="node01", cluster=None)
        BoxmanManager.snapshot_log(mgr, ns)
        logged = {c.args[0] for c in mgr.provider.snapshot_log_data.call_args_list}
        assert logged == {_full("cluster_1", "node01"), _full("cluster_2", "node01")}


class TestStorageScoping:

    def test_storage_df_scoped_by_vms(self, monkeypatch):
        mgr = _manager()
        mgr.provider.storage.snapshot_memory_files.return_value = []
        mgr.provider.storage.count_snapshots.return_value = 0
        seen = []
        monkeypatch.setattr(
            "boxman.providers.libvirt.storage.vm_disk_paths",
            lambda workdir, full_vm_name, vm_info: seen.append(full_vm_name) or [])
        BoxmanManager.storage_df(mgr, types.SimpleNamespace(vms="node01"))
        assert set(seen) == {_full("cluster_1", "node01"), _full("cluster_2", "node01")}

    def test_storage_trim_scoped_by_vms(self):
        mgr = _manager()
        mgr.provider.storage.is_running.return_value = False
        BoxmanManager.storage_trim(mgr, types.SimpleNamespace(vms="node01"))
        trimmed = {c.args[0] for c in mgr.provider.storage.is_running.call_args_list}
        assert trimmed == {_full("cluster_1", "node01"), _full("cluster_2", "node01")}

    def test_storage_compact_scoped_by_vms(self, fake_process):
        mgr = _manager()
        BoxmanManager.storage_compact(mgr, types.SimpleNamespace(vms="node01"))
        compacted = {p.args[1] for p in fake_process}
        assert compacted == {_full("cluster_1", "node01"), _full("cluster_2", "node01")}

    def test_storage_compress_snapshots_scoped_by_vms(self):
        mgr = _manager()
        mgr.provider.compress_snapshots_memory.return_value = (1, 1)
        BoxmanManager.storage_compress_snapshots(
            mgr, types.SimpleNamespace(vms="node01"))
        compressed = {
            c.args[0] for c in mgr.provider.compress_snapshots_memory.call_args_list}
        assert compressed == {_full("cluster_1", "node01"), _full("cluster_2", "node01")}

    def test_snapshot_collapse_scoped_by_vms(self, fake_process):
        mgr = _manager()
        ns = types.SimpleNamespace(
            vms="node01", target="snap1", dry_run=False, no_shutdown=False, yes=True)
        BoxmanManager.snapshot_collapse(mgr, ns)
        collapsed = {p.args[1] for p in fake_process}
        assert collapsed == {_full("cluster_1", "node01"), _full("cluster_2", "node01")}
