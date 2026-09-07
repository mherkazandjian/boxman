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


def _sync_run_parallel(self, tasks, op_label='parallel task', max_workers=None):
    """Run the real worker targets in-process, with _run_parallel's contract.

    Lets a test drive the closures a verb hands to the pool (and their
    return-value checks) instead of a stand-in for them.
    """
    results, failures = {}, {}
    for label, target, args in tasks:
        try:
            results[label] = target(*args)
        except Exception as exc:
            failures[label] = f"{type(exc).__name__}: {exc}"
    return results, failures


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
        with pytest.raises(SnapshotError, match="verification failed"):
            mgr.snapshot_take(ns)

    def test_refused_take_fails_even_when_validation_passes(self, monkeypatch):
        """A take the provider refused must reach the exit code.

        Validation alone cannot catch it: an *older* snapshot of the same
        name left on disk passes every check the validator makes, so the
        refused take would be reported as a clean success (#164 X3).
        """
        mgr = _manager()
        monkeypatch.setattr(
            BoxmanManager, "_run_parallel", _sync_run_parallel)
        mgr.provider.snapshot_take.return_value = False
        mgr.provider.validate_snapshot.return_value = (True, [])
        ns = types.SimpleNamespace(
            snapshot_name="s1", snapshot_descr="", vms="all", cluster=None)
        with pytest.raises(SnapshotError, match="take failed for"):
            mgr.snapshot_take(ns)

    def test_successful_take_and_validation_does_not_raise(self, monkeypatch):
        mgr = _manager()
        monkeypatch.setattr(
            BoxmanManager, "_run_parallel", _sync_run_parallel)
        mgr.provider.snapshot_take.return_value = True
        mgr.provider.validate_snapshot.return_value = (True, [])
        ns = types.SimpleNamespace(
            snapshot_name="s1", snapshot_descr="", vms="all", cluster=None)
        mgr.snapshot_take(ns)

    def test_collapse_aggregates_worker_failures(self, monkeypatch):
        mgr = _manager()
        monkeypatch.setattr(
            BoxmanManager, "_run_parallel",
            lambda self, tasks, op_label='parallel task': (
                {}, {"vm1": "SnapshotError: collapse failed"}))
        ns = types.SimpleNamespace(
            target="snap1", dry_run=False, no_shutdown=False, yes=True,
            vms="all", cluster=None)
        with pytest.raises(SnapshotError, match="snapshot collapse failed"):
            mgr.snapshot_collapse(ns)

    def test_compact_aggregates_worker_failures(self, monkeypatch):
        mgr = _manager()
        monkeypatch.setattr(
            BoxmanManager, "_run_parallel",
            lambda self, tasks, op_label='parallel task': (
                {}, {"vm1": "SnapshotError: compact failed"}))
        ns = types.SimpleNamespace(
            method="in-place", drop_snapshots=False, dry_run=False,
            no_shutdown=False, yes=True, vms="all", cluster=None)
        with pytest.raises(SnapshotError, match="storage compact failed"):
            mgr.storage_compact(ns)

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

    def test_restore_gives_up_after_max_rounds(self, monkeypatch):
        """Workers that keep failing every round must surface as a raised
        SnapshotError, not a logged error with exit 0 (#85 item 3)."""
        mgr = _manager()
        mgr.provider.validate_snapshot.return_value = (True, [])
        monkeypatch.setattr(
            BoxmanManager, "_run_parallel",
            lambda self, tasks, op_label='parallel task':
                ({}, {task[0]: "worker died" for task in tasks}))
        monkeypatch.setattr("time.sleep", lambda *_: None)
        ns = types.SimpleNamespace(snapshot_name="s1", vms="all", cluster=None)
        with pytest.raises(SnapshotError, match="gave up after 20 rounds"):
            mgr.snapshot_restore(ns)

    def test_delete_requires_snapshot_name(self):
        mgr = _manager()
        ns = types.SimpleNamespace(snapshot_name=None, vms="all", cluster=None)
        with pytest.raises(SnapshotError, match="snapshot name is required"):
            mgr.snapshot_delete(ns)


class TestUpdateFailuresRaise:
    """``update()`` abort paths must raise, mirroring ``provision`` (#85 item 3)."""

    @staticmethod
    def _update_ready(mgr, monkeypatch):
        """Stub everything ``update()`` touches before the new-VM template
        checks so the test reaches them with two VMs to add."""
        monkeypatch.setattr(
            BoxmanManager, "_update_sessions_with_runtime", lambda cls: None)
        monkeypatch.setattr(
            BoxmanManager, "ensure_shared_bridges", lambda cls: None)
        monkeypatch.setattr(
            BoxmanManager, "reconcile_networks",
            lambda cls, **kwargs: {})
        monkeypatch.setattr(
            BoxmanManager, "report_network_results", lambda cls, results: None)
        monkeypatch.setattr(
            BoxmanManager, "_find_all_existing_project_vms", lambda cls: [])
        monkeypatch.setattr(
            BoxmanManager, "_expand_oci_base_images", lambda cls: None)
        return types.SimpleNamespace(dry_run=False, yes=True)

    def test_ensure_templates_failure(self, monkeypatch):
        mgr = _manager()
        ns = self._update_ready(mgr, monkeypatch)
        monkeypatch.setattr(
            BoxmanManager, "ensure_templates_exist", lambda cls: False)
        with pytest.raises(ProvisionError, match="could be created"):
            mgr.update(ns)

    def test_validate_base_images_failure(self, monkeypatch):
        mgr = _manager()
        ns = self._update_ready(mgr, monkeypatch)
        monkeypatch.setattr(
            BoxmanManager, "ensure_templates_exist", lambda cls: True)

        def _invalid(cls):
            raise ValueError("base image 'x' not found")

        monkeypatch.setattr(BoxmanManager, "validate_base_images", _invalid)
        with pytest.raises(ConfigError, match="base image 'x' not found"):
            mgr.update(ns)


class TestLoadConfigFailuresRaise:

    def test_missing_config_file(self, tmp_path):
        """A missing conf.yml must surface as ConfigError (exit 2 via the
        BoxmanError mapping in app.py), not a raw FileNotFoundError
        traceback (#85 item 17)."""
        mgr = _manager()
        with pytest.raises(ConfigError, match="project config not found"):
            mgr.load_config(str(tmp_path / "conf.yml"))


class TestLifecycleFailuresRaise:
    """#164 X3 — `up` and `down` discarded ``_run_parallel``'s failures, and
    their workers discarded the provider's booleans, so there was nothing
    for the caller to aggregate in the first place.

    These drive the real worker closures through _sync_run_parallel with a
    provider that reports failure the way libvirt does — by returning
    False — rather than injecting a failure dict, which would skip the
    unchecked calls entirely.

    Both raises are deferred: a lifecycle verb that has already done half
    its work still finishes the rest before reporting. Failing on the spot
    turned one dead VM into a project with no netlab, no compose clusters
    and a stale ssh config — or, on ``down``, containers left running with
    nothing to say so.
    """

    def _down_manager(self, monkeypatch, save_ok):
        mgr = _manager()
        monkeypatch.setattr(
            BoxmanManager, "_update_sessions_with_runtime", lambda cls: None)
        monkeypatch.setattr(
            BoxmanManager, "_control_vm_targets",
            lambda cls, cli_args: [("vm1", "/tmp/ws/c1")])
        monkeypatch.setattr(
            BoxmanManager, "_run_parallel", _sync_run_parallel)
        mgr.provider.save_vm.return_value = save_ok
        mgr.provider.suspend_vm.return_value = save_ok
        mgr.stop_compose_clusters = MagicMock()
        return mgr

    def test_failed_save_is_reported(self, monkeypatch):
        mgr = self._down_manager(monkeypatch, save_ok=False)
        with pytest.raises(ProvisionError, match="could not bring down"):
            mgr.down(types.SimpleNamespace(suspend=False, vms="all", cluster=None))

    def test_failed_suspend_is_reported(self, monkeypatch):
        mgr = self._down_manager(monkeypatch, save_ok=False)
        with pytest.raises(ProvisionError, match="could not bring down"):
            mgr.down(types.SimpleNamespace(suspend=True, vms="all", cluster=None))

    def test_down_stops_compose_clusters_before_raising(self, monkeypatch):
        mgr = self._down_manager(monkeypatch, save_ok=False)
        with pytest.raises(ProvisionError):
            mgr.down(types.SimpleNamespace(suspend=False, vms="all", cluster=None))
        mgr.stop_compose_clusters.assert_called_once()

    def test_down_reports_vm_and_compose_failures_together(self, monkeypatch):
        mgr = self._down_manager(monkeypatch, save_ok=False)
        mgr.stop_compose_clusters.side_effect = ProvisionError(
            "docker-compose stop failed for 1 cluster(s): svc")
        with pytest.raises(ProvisionError) as excinfo:
            mgr.down(types.SimpleNamespace(suspend=False, vms="all", cluster=None))
        message = str(excinfo.value)
        assert "could not bring down" in message
        assert "docker-compose stop failed" in message

    def test_down_is_silent_when_every_vm_saves(self, monkeypatch):
        mgr = self._down_manager(monkeypatch, save_ok=True)
        mgr.down(types.SimpleNamespace(suspend=False, vms="all", cluster=None))
        mgr.stop_compose_clusters.assert_called_once()

    def _up_manager(self, monkeypatch, start_ok, state="shut off"):
        mgr = _manager()
        for name in ("_update_sessions_with_runtime", "ensure_shared_bridges",
                     "report_network_results", "raise_on_network_failures"):
            monkeypatch.setattr(BoxmanManager, name,
                                lambda cls, *a, **kw: None)
        monkeypatch.setattr(
            BoxmanManager, "_get_vm_states", lambda cls: {"vm1": state})
        monkeypatch.setattr(
            BoxmanManager, "reconcile_networks", lambda cls, **kw: {})
        monkeypatch.setattr(
            BoxmanManager, "_control_vm_targets",
            lambda cls, cli_args: [("vm1", "/tmp/ws/c1")])
        monkeypatch.setattr(
            BoxmanManager, "_get_project_vm_names", lambda cls: ["vm1"])
        monkeypatch.setattr(
            BoxmanManager, "_run_parallel", _sync_run_parallel)
        mgr.provider.start_vm.return_value = start_ok
        mgr.provider.resume_vm.return_value = start_ok
        mgr.provider.restore_vm.return_value = start_ok
        for name in ("wait_for_vm_ips", "ensure_netlab_up",
                     "provision_compose_clusters", "connect_info",
                     "write_ssh_config"):
            setattr(mgr, name, MagicMock())
        return mgr

    def test_failed_start_is_reported(self, monkeypatch):
        mgr = self._up_manager(monkeypatch, start_ok=False)
        with pytest.raises(ProvisionError, match="could not bring up"):
            mgr.up(types.SimpleNamespace(force=True, vms="all", cluster=None))

    def test_failed_restore_is_reported(self, monkeypatch):
        mgr = self._up_manager(monkeypatch, start_ok=False, state="saved")
        with pytest.raises(ProvisionError, match="could not bring up"):
            mgr.up(types.SimpleNamespace(force=True, vms="all", cluster=None))

    def test_up_reconciles_the_rest_before_raising(self, monkeypatch):
        mgr = self._up_manager(monkeypatch, start_ok=False)
        with pytest.raises(ProvisionError):
            mgr.up(types.SimpleNamespace(force=True, vms="all", cluster=None))
        mgr.ensure_netlab_up.assert_called_once()
        mgr.provision_compose_clusters.assert_called_once()
        mgr.write_ssh_config.assert_called_once()

    def test_up_does_not_wait_for_ips_of_failed_vms(self, monkeypatch):
        """A VM that would not start has no lease coming; waiting on it
        just burns the timeout before `up` can report why."""
        mgr = self._up_manager(monkeypatch, start_ok=False)
        with pytest.raises(ProvisionError):
            mgr.up(types.SimpleNamespace(force=True, vms="all", cluster=None))
        mgr.wait_for_vm_ips.assert_not_called()

    def test_up_reports_vm_and_compose_failures_together(self, monkeypatch):
        mgr = self._up_manager(monkeypatch, start_ok=False)
        mgr.provision_compose_clusters.side_effect = ProvisionError(
            "docker-compose up failed for 1 cluster(s): svc")
        with pytest.raises(ProvisionError) as excinfo:
            mgr.up(types.SimpleNamespace(force=True, vms="all", cluster=None))
        message = str(excinfo.value)
        assert "could not bring up" in message
        assert "docker-compose up failed" in message

    def test_up_is_silent_when_every_vm_starts(self, monkeypatch):
        mgr = self._up_manager(monkeypatch, start_ok=True)
        mgr.up(types.SimpleNamespace(force=True, vms="all", cluster=None))
        mgr.write_ssh_config.assert_called_once()
        mgr.wait_for_vm_ips.assert_called_once()


class TestConfigureAndStartVmRaises:
    """#164 X3 — every step of _configure_and_start_vm returned a bool that
    was logged at warning level and dropped, so a VM with no disks, no NICs
    and a failed start still reported success."""

    def _mgr(self):
        mgr = _manager()
        mgr.config['clusters']['cluster_1']['vms'] = {
            'node01': {
                'cpus': 2, 'memory': 2048,
                'network_adapters': [{'network': 'net1'}],
                'disks': [{'size': '10G'}],
            },
        }
        mgr.resolve_adapter_network = MagicMock()
        return mgr

    def _run(self, mgr):
        cluster = mgr.config['clusters']['cluster_1']
        return mgr._configure_and_start_vm(
            'cluster_1', cluster, 'node01', cluster['vms']['node01'])

    def test_failed_start_raises(self):
        mgr = self._mgr()
        mgr.provider.configure_vm_cpu_memory.return_value = True
        mgr.provider.configure_vm_network_interfaces.return_value = True
        mgr.provider.configure_vm_disks.return_value = True
        mgr.provider.start_vm.return_value = False
        with pytest.raises(ProvisionError, match="start"):
            self._run(mgr)

    def test_failed_disks_raises_and_still_starts(self):
        mgr = self._mgr()
        mgr.provider.configure_vm_cpu_memory.return_value = True
        mgr.provider.configure_vm_network_interfaces.return_value = True
        mgr.provider.configure_vm_disks.return_value = False
        mgr.provider.start_vm.return_value = True
        with pytest.raises(ProvisionError, match="disks"):
            self._run(mgr)
        mgr.provider.start_vm.assert_called_once()

    def test_every_failure_is_named_in_one_error(self):
        mgr = self._mgr()
        mgr.provider.configure_vm_cpu_memory.return_value = False
        mgr.provider.configure_vm_network_interfaces.return_value = False
        mgr.provider.configure_vm_disks.return_value = False
        mgr.provider.start_vm.return_value = False
        with pytest.raises(ProvisionError) as excinfo:
            self._run(mgr)
        message = str(excinfo.value)
        for expected in ("cpu/memory", "network interfaces", "disks", "start"):
            assert expected in message

    def test_fully_successful_vm_does_not_raise(self):
        mgr = self._mgr()
        mgr.provider.configure_vm_cpu_memory.return_value = True
        mgr.provider.configure_vm_network_interfaces.return_value = True
        mgr.provider.configure_vm_disks.return_value = True
        mgr.provider.start_vm.return_value = True
        self._run(mgr)
