"""
Regression tests for #85 item 3: failure paths in ``provision``, ``up``,
``snapshot take`` and ``snapshot restore`` must raise (so the CLI exits
non-zero via the BoxmanError → exit 2 mapping in app.py) instead of logging
an error and returning 0.
"""

import types
from unittest.mock import MagicMock

import pytest

from boxman.exceptions import ConfigError, ProvisionError, SnapshotError
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
        },
    }
    mgr.provider = MagicMock()
    mgr.logger = MagicMock()
    mgr.cache = MagicMock()
    mgr.cache.projects = {}
    return mgr


@pytest.fixture
def no_existing_state(monkeypatch):
    """No live VMs, no cache entry, cache registration succeeds."""
    monkeypatch.setattr(
        BoxmanManager, "_find_existing_project_vms", lambda cls: [])
    monkeypatch.setattr(
        BoxmanManager, "register_project_in_cache", lambda cls: None)


class TestProvisionFailuresRaise:

    def test_existing_state_without_force(self, monkeypatch):
        mgr = _manager()
        monkeypatch.setattr(
            BoxmanManager, "_find_existing_project_vms", lambda cls: ["vm1"])
        with pytest.raises(ProvisionError, match="cannot provision"):
            mgr.provision(types.SimpleNamespace(force=False))

    def test_cache_registration_conflict(self, monkeypatch, no_existing_state):
        mgr = _manager()

        def _conflict(cls):
            raise RuntimeError("project already registered")

        monkeypatch.setattr(
            BoxmanManager, "register_project_in_cache", _conflict)
        with pytest.raises(ProvisionError, match="already registered"):
            mgr.provision(types.SimpleNamespace(force=False))

    def test_template_rebuild_failure(self, monkeypatch, no_existing_state):
        mgr = _manager()
        monkeypatch.setattr(
            BoxmanManager, "_create_templates_impl",
            lambda cls, requested=None, force=False: ["failed-template"])
        ns = types.SimpleNamespace(force=False, rebuild_templates=True)
        with pytest.raises(ProvisionError, match="could be rebuilt"):
            mgr.provision(ns)

    def test_ensure_templates_failure(self, monkeypatch, no_existing_state):
        mgr = _manager()
        monkeypatch.setattr(
            BoxmanManager, "ensure_templates_exist", lambda cls: False)
        ns = types.SimpleNamespace(force=False, rebuild_templates=False)
        with pytest.raises(ProvisionError, match="could be created"):
            mgr.provision(ns)

    def test_validate_base_images_failure(self, monkeypatch, no_existing_state):
        mgr = _manager()
        monkeypatch.setattr(
            BoxmanManager, "ensure_templates_exist", lambda cls: True)

        def _invalid(cls):
            raise ValueError("base image 'x' not found")

        monkeypatch.setattr(BoxmanManager, "validate_base_images", _invalid)
        ns = types.SimpleNamespace(force=False, rebuild_templates=False)
        with pytest.raises(ConfigError, match="base image 'x' not found"):
            mgr.provision(ns)


class TestUpFailuresRaise:

    def test_no_vms_defined(self):
        mgr = _manager()
        mgr.config = {"project": "demo", "clusters": {}}
        with pytest.raises(ConfigError, match="no VMs defined"):
            mgr.up(types.SimpleNamespace())

    def test_partial_state_without_force(self, monkeypatch):
        mgr = _manager()
        monkeypatch.setattr(
            BoxmanManager, "_get_vm_states",
            lambda cls: {"bprj__demo__bprj_cluster_1_service01": "running"})
        with pytest.raises(ProvisionError, match="partial infrastructure state"):
            mgr.up(types.SimpleNamespace(force=False))


class TestSnapshotFailuresRaise:

    def test_take_failed_verification(self, monkeypatch):
        mgr = _manager()
        monkeypatch.setattr(
            BoxmanManager, "_run_parallel",
            lambda self, tasks, op_label='parallel task': ({}, {}))
        mgr.provider.validate_snapshot.return_value = (False, ["corrupt"])
        ns = types.SimpleNamespace(
            snapshot_name="s1", snapshot_descr="", vms="all", cluster=None)
        with pytest.raises(SnapshotError, match="failed verification"):
            mgr.snapshot_take(ns)

    def test_restore_no_snapshot_found(self):
        mgr = _manager()
        mgr.provider.get_latest_snapshot.return_value = None
        ns = types.SimpleNamespace(snapshot_name=None, vms="all", cluster=None)
        with pytest.raises(SnapshotError, match="no snapshot found"):
            mgr.snapshot_restore(ns)

    def test_restore_validation_abort(self):
        mgr = _manager()
        mgr.provider.validate_snapshot.return_value = (False, ["bad chain"])
        ns = types.SimpleNamespace(snapshot_name="s1", vms="all", cluster=None)
        with pytest.raises(SnapshotError, match="aborting restore"):
            mgr.snapshot_restore(ns)
