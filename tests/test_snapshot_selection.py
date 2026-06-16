"""
Tests for BoxmanManager._select_vm_targets — the --cluster / --vms selector
that scopes snapshot take/restore/delete to a single cluster or VM in a
multi-cluster project.
"""

import types
from unittest.mock import MagicMock

import pytest

from boxman.manager import BoxmanManager


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
    return mgr


def _select(mgr, **kwargs):
    ns = types.SimpleNamespace(**kwargs)
    return [(c, v) for _full, c, v, _wd in BoxmanManager._select_vm_targets(mgr, ns)]


class TestSelectVmTargets:

    def test_default_selects_every_vm(self):
        mgr = _manager()
        assert _select(mgr, cluster=None, vms="all") == [
            ("cluster_1", "service01"),
            ("cluster_1", "node01"),
            ("cluster_2", "service01"),
            ("cluster_2", "node01"),
        ]

    def test_missing_attrs_default_to_all(self):
        """No --cluster/--vms on the namespace → whole project (back-compat)."""
        mgr = _manager()
        ns = types.SimpleNamespace()
        targets = BoxmanManager._select_vm_targets(mgr, ns)
        assert len(targets) == 4

    def test_cluster_filter(self):
        mgr = _manager()
        assert _select(mgr, cluster="cluster_2", vms="all") == [
            ("cluster_2", "service01"),
            ("cluster_2", "node01"),
        ]

    def test_vms_filter_matches_bare_name_across_clusters(self):
        mgr = _manager()
        assert _select(mgr, cluster=None, vms="node01") == [
            ("cluster_1", "node01"),
            ("cluster_2", "node01"),
        ]

    def test_vms_filter_matches_qualified_name(self):
        mgr = _manager()
        assert _select(mgr, cluster=None, vms="cluster_2_service01") == [
            ("cluster_2", "service01"),
        ]

    def test_cluster_and_vms_compose(self):
        mgr = _manager()
        assert _select(mgr, cluster="cluster_1", vms="node01") == [
            ("cluster_1", "node01"),
        ]

    def test_csv_vms_filter(self):
        mgr = _manager()
        got = _select(mgr, cluster="cluster_1", vms="service01,node01")
        assert got == [("cluster_1", "service01"), ("cluster_1", "node01")]

    def test_full_vm_name_is_project_and_cluster_scoped(self):
        mgr = _manager()
        ns = types.SimpleNamespace(cluster="cluster_2", vms="node01")
        targets = BoxmanManager._select_vm_targets(mgr, ns)
        full, cname, vname, workdir = targets[0]
        assert full == "bprj__demo__bprj_cluster_2_node01"
        assert (cname, vname) == ("cluster_2", "node01")
        assert workdir == "/tmp/ws/c2"

    def test_unknown_cluster_raises(self):
        mgr = _manager()
        with pytest.raises(ValueError, match="cluster 'nope' not found"):
            _select(mgr, cluster="nope", vms="all")

    def test_unmatched_vms_filter_returns_empty(self):
        mgr = _manager()
        assert _select(mgr, cluster=None, vms="does-not-exist") == []


class TestSnapshotDeleteScoping:
    """
    snapshot_delete runs in the main process, so it provides an end-to-end
    check that a real snapshot operation honors the --cluster/--vms selector
    (take/restore share the same _select_vm_targets path).
    """

    def _manager_with_provider(self):
        mgr = _manager()
        mgr.provider = MagicMock()
        mgr.logger = MagicMock()
        return mgr

    def test_delete_scoped_to_one_cluster(self):
        mgr = self._manager_with_provider()
        ns = types.SimpleNamespace(
            snapshot_name="s1", cluster="cluster_2", vms="all")
        BoxmanManager.snapshot_delete(mgr, ns)
        deleted = {c.args[0] for c in mgr.provider.snapshot_delete.call_args_list}
        assert deleted == {
            "bprj__demo__bprj_cluster_2_service01",
            "bprj__demo__bprj_cluster_2_node01",
        }

    def test_delete_whole_project_by_default(self):
        mgr = self._manager_with_provider()
        ns = types.SimpleNamespace(snapshot_name="s1")
        BoxmanManager.snapshot_delete(mgr, ns)
        assert mgr.provider.snapshot_delete.call_count == 4

    def test_delete_no_match_is_noop(self):
        mgr = self._manager_with_provider()
        ns = types.SimpleNamespace(
            snapshot_name="s1", cluster=None, vms="ghost")
        BoxmanManager.snapshot_delete(mgr, ns)
        mgr.provider.snapshot_delete.assert_not_called()
