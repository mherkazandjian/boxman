"""
Shared folders reconcile against the persistent definition (#164 C1, finding 7).

Attaching a share can fall back to a config-only attachment: the persistent
definition gets it, the live domain does not. With restarts now deferred by
default, that state persists across runs — and the diff read the *live*
domain, so:

* the next update proposed the same share again, and libvirt rejected the
  duplicate persistent target, so even a follow-up ``update --restart``
  failed before it could restart; and
* a pending share removed from the config before the restart produced no
  removal at all, because it had never appeared live. It stayed in the
  persistent definition permanently.

The reconcile now compares the complete desired configuration against the
persistent definition, and derives "still pending a boot" separately from
the live one.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from boxman.providers.libvirt.vm_differ import VMStateDiffer

pytestmark = pytest.mark.unit


@pytest.fixture
def differ() -> VMStateDiffer:
    return VMStateDiffer(provider_config={"use_sudo": False,
                                          "uri": "qemu:///system"})


def _folder(name, host_path, readonly=False):
    return {'name': name, 'host_path': host_path, 'readonly': readonly}


def _diff(differ, desired, persistent, live, tmp_path, vm_state="running"):
    def folders(_domain, inactive=False):
        return persistent if inactive else live

    with patch.object(differ, "get_vm_state", return_value=vm_state), \
         patch.object(differ, "get_actual_cpu",
                      return_value={"sockets": 1, "cores": 1, "threads": 1,
                                    "total_vcpus": 1, "current_vcpus": 1}), \
         patch.object(differ, "get_max_vcpus", return_value=1), \
         patch.object(differ, "get_actual_memory_mb", return_value=1024), \
         patch.object(differ, "get_max_memory_mb", return_value=1024), \
         patch.object(differ, "get_actual_disks", return_value=[]), \
         patch.object(differ, "get_disk_records", return_value=None), \
         patch.object(differ, "get_actual_memballoon",
                      return_value={'free_page_reporting': False,
                                    'autodeflate': False,
                                    'stats_period': None}), \
         patch.object(differ, "get_actual_cdroms", return_value=[]), \
         patch.object(differ, "get_actual_shared_folders", side_effect=folders):
        return differ.diff_vm(
            domain_name="vm01", desired_cpus=None, desired_memory_mb=None,
            desired_disks=[], workdir=str(tmp_path), disk_prefix="vm01",
            desired_shared_folders=desired)


class TestARepeatedAdditionIsNotProposedAgain:
    """The retry that libvirt rejected."""

    def test_a_config_only_share_is_not_proposed_a_second_time(
            self, differ, tmp_path: Path):
        share = _folder('logs', '/srv/logs')
        diff = _diff(differ, desired=[share],
                     persistent=[share],   # configured last run
                     live=[],              # never reached the live domain
                     tmp_path=tmp_path)

        assert diff['new_shared_folders'] == [], (
            'proposed an attachment libvirt would reject as a duplicate')
        assert diff['changed_shared_folders'] == []

    def test_but_it_is_still_reported_as_pending(self, differ, tmp_path: Path):
        share = _folder('logs', '/srv/logs')
        diff = _diff(differ, desired=[share], persistent=[share], live=[],
                     tmp_path=tmp_path)

        assert diff['shared_folders_restart_pending'] is True


class TestAChangedPendingDeclaration:
    """The superseded configuration must not be preserved."""

    def test_a_changed_source_is_a_change_not_a_no_op(self, differ, tmp_path):
        diff = _diff(differ,
                     desired=[_folder('logs', '/srv/logs-v2')],
                     persistent=[_folder('logs', '/srv/logs')],
                     live=[], tmp_path=tmp_path)

        assert [f['host_path'] for f in diff['changed_shared_folders']] == [
            '/srv/logs-v2']

    def test_a_changed_readonly_is_a_change(self, differ, tmp_path):
        diff = _diff(differ,
                     desired=[_folder('logs', '/srv/logs', readonly=True)],
                     persistent=[_folder('logs', '/srv/logs', readonly=False)],
                     live=[], tmp_path=tmp_path)

        assert len(diff['changed_shared_folders']) == 1


class TestCancellingAPendingShare:
    """Removed from the config before the restart ever happened."""

    def test_it_is_removed_from_the_persistent_definition(self, differ, tmp_path):
        diff = _diff(differ,
                     desired=[],
                     persistent=[_folder('logs', '/srv/logs')],
                     live=[], tmp_path=tmp_path)

        assert [f['name'] for f in diff['removed_shared_folders']] == ['logs'], (
            'a pending share that was cancelled stayed configured forever')

    def test_a_live_share_removed_from_config_is_still_pending(self, differ,
                                                               tmp_path):
        share = _folder('logs', '/srv/logs')
        diff = _diff(differ, desired=[], persistent=[share], live=[share],
                     tmp_path=tmp_path)

        assert [f['name'] for f in diff['removed_shared_folders']] == ['logs']
        assert diff['shared_folders_restart_pending'] is True


class TestNothingPendingWhenLiveAgrees:

    def test_a_fully_applied_share_is_quiet(self, differ, tmp_path):
        share = _folder('logs', '/srv/logs')
        diff = _diff(differ, desired=[share], persistent=[share], live=[share],
                     tmp_path=tmp_path)

        assert diff['new_shared_folders'] == []
        assert diff['changed_shared_folders'] == []
        assert diff['removed_shared_folders'] == []
        assert diff['shared_folders_restart_pending'] is False

    def test_a_stopped_guest_has_no_separate_live_view(self, differ, tmp_path):
        """Nothing is running, so nothing is pending a boot."""
        share = _folder('logs', '/srv/logs')
        diff = _diff(differ, desired=[share], persistent=[share], live=[],
                     tmp_path=tmp_path, vm_state='shut off')

        assert diff['shared_folders_restart_pending'] is False
        assert diff['new_shared_folders'] == []


# ---------------------------------------------------------------------------
# The application half. The tests above exercise the differ only, so they
# cannot see the *application* reading live state, nor a pending flag that
# describes the state before the changes were applied (#164 C1 review
# round 2, findings 4 and 5).
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock  # noqa: E402


class TestApplicationReconcilesThePersistentOccupant:
    """Finding 4: changing a *pending* share must detach the old entry."""

    def _session(self, persistent, live):
        from boxman.providers.libvirt.session import LibVirtSession

        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {'uri': 'qemu:///system'}
        session.logger = MagicMock()
        manager = MagicMock()
        manager.get_attached_shared_folders.side_effect = (
            lambda inactive=False: persistent if inactive else live)
        manager.configure_from_config.return_value = {
            'success': True, 'restart_needed': False}
        manager.detach_shared_folder.return_value = {
            'success': True, 'restart_needed': False}
        return session, manager

    def test_the_old_persistent_entry_is_detached(self):
        """persistent logs=A, nothing live, desired logs=B."""
        old = _folder('logs', '/srv/A')
        session, manager = self._session(persistent=[old], live=[])

        with patch('boxman.providers.libvirt.session.SharedFolderManager',
                   return_value=manager):
            result = session.update_vm_shared_folders(
                vm_name='node01', new_folders=[], removed_folders=[],
                changed_folders=[_folder('logs', '/srv/B')],
                vm_running=True)

        assert result['success'] is True
        manager.detach_shared_folder.assert_called_once()
        assert manager.detach_shared_folder.call_args.args[1] == '/srv/A', (
            'skipped the old entry, so the re-attach would hit an occupied '
            'persistent target')

    def test_a_live_entry_is_still_found_when_persistent_lacks_it(self):
        live_only = _folder('logs', '/srv/A')
        session, manager = self._session(persistent=[], live=[live_only])

        with patch('boxman.providers.libvirt.session.SharedFolderManager',
                   return_value=manager):
            session.update_vm_shared_folders(
                vm_name='node01', new_folders=[], removed_folders=[],
                changed_folders=[_folder('logs', '/srv/B')],
                vm_running=True)

        manager.detach_shared_folder.assert_called_once()


class TestPendingIsAskedAfterTheChanges:
    """Finding 5: a successful hot attach must not report pending."""

    def _session(self, live):
        from boxman.providers.libvirt.session import LibVirtSession

        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {'uri': 'qemu:///system'}
        session.logger = MagicMock()
        manager = MagicMock()
        manager.get_attached_shared_folders.return_value = live
        return session, manager

    def test_a_share_that_reached_the_live_domain_is_not_pending(self):
        share = _folder('logs', '/srv/logs')
        session, manager = self._session(live=[share])

        with patch('boxman.providers.libvirt.session.SharedFolderManager',
                   return_value=manager):
            assert session.shared_folders_pending(
                'node01', [share], vm_active=True) is False

    def test_a_share_that_did_not_reach_it_is_pending(self):
        session, manager = self._session(live=[])

        with patch('boxman.providers.libvirt.session.SharedFolderManager',
                   return_value=manager):
            assert session.shared_folders_pending(
                'node01', [_folder('logs', '/srv/logs')],
                vm_active=True) is True

    def test_a_changed_source_that_reached_it_is_not_pending(self):
        session, manager = self._session(live=[_folder('logs', '/srv/B')])

        with patch('boxman.providers.libvirt.session.SharedFolderManager',
                   return_value=manager):
            assert session.shared_folders_pending(
                'node01', [_folder('logs', '/srv/B')],
                vm_active=True) is False

    def test_a_removal_that_reached_it_is_not_pending(self):
        session, manager = self._session(live=[])

        with patch('boxman.providers.libvirt.session.SharedFolderManager',
                   return_value=manager):
            assert session.shared_folders_pending(
                'node01', [], vm_active=True) is False

    def test_a_removal_still_live_is_pending(self):
        session, manager = self._session(live=[_folder('logs', '/srv/logs')])

        with patch('boxman.providers.libvirt.session.SharedFolderManager',
                   return_value=manager):
            assert session.shared_folders_pending(
                'node01', [], vm_active=True) is True

    def test_an_inactive_guest_is_never_pending(self):
        session, manager = self._session(live=[])

        with patch('boxman.providers.libvirt.session.SharedFolderManager',
                   return_value=manager):
            assert session.shared_folders_pending(
                'node01', [_folder('logs', '/srv/logs')],
                vm_active=False) is False
