"""
Regression tests for #164 X3 — snapshot/storage worker failure semantics.

Three separate defects are pinned here:

* a shutdown boxman *attempted and lost* was treated like the deliberate
  ``--no-shutdown`` skip, so the collapse/compact never happened and the
  command still exited 0;
* the restart sat below the operation, so raising on a failed operation
  would have left a guest boxman had powered down switched off;
* ``collapse_to(dry_run=True)`` returns False for a target that does not
  resolve, and the dry-run branch discarded it — ``--dry-run`` exited 0
  on a misspelled snapshot name, the one thing it is asked to check.
"""

from unittest.mock import MagicMock

import pytest

from boxman.exceptions import SnapshotError
from boxman.manager_parts.snapshots import SnapshotsMixin

pytestmark = pytest.mark.unit


@pytest.fixture
def managers(monkeypatch):
    """Swap the libvirt managers the workers import for mocks.

    Both workers import them inside the function body (they have to be
    picklable), so the patch targets the defining modules.
    """
    snapshot_mgr = MagicMock()
    storage = MagicMock()
    storage.count_snapshots.return_value = 0
    storage.disk_info.return_value = {'actual-size': 100}
    storage.disk_measure.return_value = {'required': 50}
    monkeypatch.setattr(
        "boxman.providers.libvirt.snapshot.SnapshotManager",
        lambda cfg: snapshot_mgr)
    monkeypatch.setattr(
        "boxman.providers.libvirt.storage.StorageManager",
        lambda cfg: storage)
    return snapshot_mgr, storage


def _collapse(**kw):
    args = dict(provider_config={}, full_vm_name="vm1", workdir="/tmp/ws",
                vm_info={}, target="snap1", no_shutdown=False, dry_run=False)
    args.update(kw)
    return SnapshotsMixin._collapse_one_vm(**args)


def _compact(**kw):
    args = dict(provider_config={}, full_vm_name="vm1", workdir="/tmp/ws",
                vm_info={}, method="in-place", drop_snapshots=False,
                no_shutdown=False, dry_run=False)
    args.update(kw)
    return SnapshotsMixin._compact_one_vm(**args)


@pytest.fixture
def disk(tmp_path, monkeypatch):
    """A real file on disk — the compact worker filters with os.path.isfile."""
    path = tmp_path / "vm1.qcow2"
    path.write_bytes(b"x")
    monkeypatch.setattr(
        "boxman.providers.libvirt.storage.vm_disk_paths",
        lambda workdir, full_vm_name, vm_info: [str(path)])
    return str(path)


class TestCollapseWorkerShutdown:

    def test_lost_shutdown_is_a_failure(self, managers):
        snapshot_mgr, storage = managers
        storage.is_running.return_value = True
        storage.shutdown_and_wait.return_value = False

        with pytest.raises(SnapshotError, match="shutdown failed"):
            _collapse()

        snapshot_mgr.collapse_to.assert_not_called()

    def test_requested_skip_is_not_a_failure(self, managers):
        """``--no-shutdown`` on a running VM is what the operator asked
        for; it must stay a skip and must not become an error."""
        snapshot_mgr, storage = managers
        storage.is_running.return_value = True

        _collapse(no_shutdown=True)

        storage.shutdown_and_wait.assert_not_called()
        snapshot_mgr.collapse_to.assert_not_called()


class TestCollapseWorkerRestartOrdering:

    def test_failed_collapse_still_restarts(self, managers):
        snapshot_mgr, storage = managers
        storage.is_running.return_value = True
        storage.shutdown_and_wait.return_value = True
        storage.start.return_value = True
        snapshot_mgr.collapse_to.return_value = False

        with pytest.raises(SnapshotError, match="collapse failed"):
            _collapse()

        storage.start.assert_called_once_with("vm1")

    def test_both_failures_are_reported(self, managers):
        snapshot_mgr, storage = managers
        storage.is_running.return_value = True
        storage.shutdown_and_wait.return_value = True
        storage.start.return_value = False
        snapshot_mgr.collapse_to.return_value = False

        with pytest.raises(SnapshotError) as excinfo:
            _collapse()

        message = str(excinfo.value)
        assert "collapse failed" in message
        assert "failed to restart" in message

    def test_raising_collapse_still_restarts(self, managers):
        """The restart is in a ``finally``: an exception from the collapse
        must not leave the guest powered off either."""
        snapshot_mgr, storage = managers
        storage.is_running.return_value = True
        storage.shutdown_and_wait.return_value = True
        snapshot_mgr.collapse_to.side_effect = RuntimeError("libvirt is gone")

        with pytest.raises(RuntimeError, match="libvirt is gone"):
            _collapse()

        storage.start.assert_called_once_with("vm1")

    def test_restart_blowup_does_not_mask_the_collapse_failure(self, managers):
        snapshot_mgr, storage = managers
        storage.is_running.return_value = True
        storage.shutdown_and_wait.return_value = True
        storage.start.side_effect = OSError("no such domain")
        snapshot_mgr.collapse_to.return_value = False

        with pytest.raises(SnapshotError) as excinfo:
            _collapse()

        message = str(excinfo.value)
        assert "collapse failed" in message
        assert "no such domain" in message

    def test_vm_that_was_off_is_not_started(self, managers):
        snapshot_mgr, storage = managers
        storage.is_running.return_value = False
        snapshot_mgr.collapse_to.return_value = True

        _collapse()

        storage.start.assert_not_called()


class TestCollapseWorkerDryRun:

    def test_invalid_target_is_a_failure(self, managers):
        snapshot_mgr, storage = managers
        snapshot_mgr.collapse_to.return_value = False

        with pytest.raises(SnapshotError, match="not a usable collapse target"):
            _collapse(dry_run=True)

        storage.shutdown_and_wait.assert_not_called()

    def test_valid_target_passes(self, managers):
        snapshot_mgr, storage = managers
        snapshot_mgr.collapse_to.return_value = True

        _collapse(dry_run=True)

        snapshot_mgr.collapse_to.assert_called_once_with(
            "vm1", "snap1", dry_run=True)
        storage.is_running.assert_not_called()


class TestCompactWorkerShutdown:

    def test_lost_shutdown_is_a_failure(self, managers, disk):
        _snapshot_mgr, storage = managers
        storage.is_running.return_value = True
        storage.shutdown_and_wait.return_value = False

        with pytest.raises(SnapshotError, match="shutdown failed"):
            _compact()

        storage.compact_disk.assert_not_called()

    def test_requested_skip_is_not_a_failure(self, managers, disk):
        _snapshot_mgr, storage = managers
        storage.is_running.return_value = True

        _compact(no_shutdown=True)

        storage.shutdown_and_wait.assert_not_called()
        storage.compact_disk.assert_not_called()


class TestCompactWorkerRestartOrdering:

    def test_failed_disk_still_restarts(self, managers, disk):
        _snapshot_mgr, storage = managers
        storage.is_running.return_value = True
        storage.shutdown_and_wait.return_value = True
        storage.start.return_value = True
        storage.compact_disk.return_value = False

        with pytest.raises(SnapshotError, match="compact failed"):
            _compact()

        storage.start.assert_called_once_with("vm1")

    def test_both_failures_are_reported(self, managers, disk):
        _snapshot_mgr, storage = managers
        storage.is_running.return_value = True
        storage.shutdown_and_wait.return_value = True
        storage.start.return_value = False
        storage.compact_disk.return_value = False

        with pytest.raises(SnapshotError) as excinfo:
            _compact()

        message = str(excinfo.value)
        assert "compact failed" in message
        assert "failed to restart" in message

    def test_raising_disk_loop_still_restarts(self, managers, disk):
        _snapshot_mgr, storage = managers
        storage.is_running.return_value = True
        storage.shutdown_and_wait.return_value = True
        storage.compact_disk.side_effect = RuntimeError("qemu-img died")

        with pytest.raises(RuntimeError, match="qemu-img died"):
            _compact()

        storage.start.assert_called_once_with("vm1")

    def test_dry_run_neither_shuts_down_nor_restarts(self, managers, disk):
        _snapshot_mgr, storage = managers
        storage.is_running.return_value = True

        _compact(dry_run=True)

        storage.shutdown_and_wait.assert_not_called()
        storage.compact_disk.assert_not_called()
        storage.start.assert_not_called()

    def test_successful_compact_restarts_and_does_not_raise(self, managers, disk):
        _snapshot_mgr, storage = managers
        storage.is_running.return_value = True
        storage.shutdown_and_wait.return_value = True
        storage.start.return_value = True
        storage.compact_disk.return_value = True

        _compact()

        storage.start.assert_called_once_with("vm1")
