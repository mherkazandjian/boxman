"""
Which disks boxman may detach (#164 F2).

The removal rule went through four designs, each of which detached
something it should not have. Every one of those scenarios is a test here,
named after what it destroys, so a regression fails with the specific
mistake rather than a generic assertion.
"""

from unittest.mock import MagicMock

import pytest

from boxman.exceptions import ProvisionError
from boxman.providers.libvirt.disk_ownership import (
    DiskRecord,
    plan_disk_removals,
    read_disk_records,
    record_attached_disk,
    records_from_xml,
    records_to_xml,
    unowned_disks,
)

pytestmark = pytest.mark.unit

ROOT = "/vm/bprj__demo__bprj_cluster_node.qcow2"
DATA = "/vm/bprj__demo__bprj_cluster_node_data.qcow2"


def _attached(target, source, size_mb=1024):
    return {'target': target, 'source': source, 'size_mb': size_mb}


def _record(name="data", target="vdb", role="data", source=DATA):
    return DiskRecord(name=name, target=target, role=role, source=source)


class TestTheRuleRemovesWhatItShould:

    def test_undeclared_recorded_disk_is_removed(self):
        removals, refusals = plan_disk_removals(
            records=[_record()],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert [r.name for r in removals] == ['data']
        assert refusals == []

    def test_still_declared_disk_is_kept(self):
        removals, refusals = plan_disk_removals(
            records=[_record()],
            desired_disks=[{'name': 'data', 'target': 'vdb'}],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert removals == []
        assert refusals == []

    def test_already_detached_disk_is_not_reported(self):
        """Nothing to do, and nothing worth telling the operator about."""
        removals, refusals = plan_disk_removals(
            records=[_record()],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT)])

        assert removals == []
        assert refusals == []


class TestTheRootDiskSurvivesEveryDesign:
    """Design 1 -- subtraction: attached minus declared.

    ``get_actual_disks()`` returns every file-backed disk, the root disk
    included, and the root disk is never in ``disks:``. Subtraction
    therefore removes it first.
    """

    def test_root_disk_is_not_removed_when_nothing_is_declared(self):
        removals, _refusals = plan_disk_removals(
            records=[],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT)])

        assert removals == []

    def test_root_disk_is_not_removed_even_when_recorded(self):
        removals, _refusals = plan_disk_removals(
            records=[DiskRecord(name='root', target='vda', role='root',
                                source=ROOT)],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT)])

        assert removals == []


class TestSiblingVmDisksSurvive:
    """Design 2 -- "the file is named after this VM".

    ``disk_path_for(wd, 'db_data', prefix='..._node')`` and
    ``disk_path_for(wd, 'data', prefix='..._node_db')`` produce the same
    path, and a sibling VM named ``node_db`` has a boot disk whose name
    starts with ``node``'s prefix. Nothing in the rule looks at filenames.
    """

    SIBLING_ROOT = "/vm/bprj__demo__bprj_cluster_node_db.qcow2"

    def test_a_sibling_whose_name_extends_this_one_is_untouched(self):
        removals, _refusals = plan_disk_removals(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert [r.source for r in removals] == [DATA]
        assert self.SIBLING_ROOT not in [r.source for r in removals]


class TestReplacementDiskAtAReusedTarget:
    """Design 3 -- a name/target record with no source check.

    The record says "I put ``data`` at ``vdb``". If ``data`` is dropped
    from the config and a different disk is attached at ``vdb``, a rule
    that trusts the target alone detaches the replacement.
    """

    REPLACEMENT = "/vm/bprj__demo__bprj_cluster_node_scratch.qcow2"

    def test_a_replacement_at_the_recorded_target_is_refused(self):
        removals, refusals = plan_disk_removals(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT),
                          _attached('vdb', self.REPLACEMENT)])

        assert removals == []
        assert len(refusals) == 1
        record, reason = refusals[0]
        assert record.name == 'data'
        assert self.REPLACEMENT in reason

    def test_a_declared_disk_claiming_the_target_is_refused(self):
        removals, refusals = plan_disk_removals(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[{'name': 'scratch', 'target': 'vdb'}],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert removals == []
        assert 'claims target vdb' in refusals[0][1]


class TestIndependentOverlayBackedByTheRecordedFile:
    """Design 4 -- adding a backing-chain identity check.

    An overlay created independently, backed by the recorded file, passes
    a backing-chain test: its chain contains the recorded source. Exact
    source matching is what rejects it -- the overlay's own path differs.
    """

    OVERLAY = "/vm/bprj__demo__bprj_cluster_node_data.overlay.qcow2"

    def test_an_overlay_backed_by_the_recorded_disk_is_refused(self):
        removals, refusals = plan_disk_removals(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT),
                          _attached('vdb', self.OVERLAY)])

        assert removals == []
        assert self.OVERLAY in refusals[0][1]

    def test_a_snapshot_head_is_refused_the_same_way(self):
        """A snapshot moves the head; the source no longer matches.

        Phrased as "the source differs", not "while a snapshot exists":
        collapse_to() rebases the existing head rather than restoring the
        original pathname, so collapsing does not necessarily make this
        removable again.
        """
        snap = "/vm/bprj__demo__bprj_cluster_node_data.snap1"
        removals, refusals = plan_disk_removals(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[],
            actual_disks=[_attached('vdb', snap)])

        assert removals == []
        assert 'not the' in refusals[0][1]


class TestDomainsWithoutRecords:
    """A VM boxman has no ownership record for is reported, never touched."""

    def test_no_metadata_removes_nothing(self):
        removals, refusals = plan_disk_removals(
            records=None,
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert removals == []
        assert refusals == []

    def test_unowned_disks_are_not_listed_without_metadata(self):
        assert unowned_disks(None, [], [_attached('vdb', DATA)]) == []

    def test_unowned_disks_are_listed_when_metadata_exists(self):
        stray = "/vm/attached-by-hand.qcow2"
        listed = unowned_disks(
            records=[_record()],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA),
                          _attached('vdc', stray)],
            root_source=ROOT)

        assert [d['source'] for d in listed] == [stray]


class TestMetadataRoundTrip:

    def test_records_survive_a_round_trip(self):
        records = [
            DiskRecord('data', 'vdb', 'data', DATA),
            DiskRecord('root', 'vda', 'root', ROOT),
        ]
        assert records_from_xml(records_to_xml(records)) == records

    def test_unparseable_metadata_raises(self):
        """Not "this domain has no records" -- that is the dangerous read."""
        with pytest.raises(ProvisionError, match='could not parse'):
            records_from_xml("<disks><disk name=")

    def test_incomplete_entry_raises(self):
        with pytest.raises(ProvisionError, match='missing'):
            records_from_xml('<disks><disk name="data" target="vdb"/></disks>')


class TestReadingRecordsFromADomain:

    def _virsh(self, ok=True, stdout="", stderr=""):
        virsh = MagicMock()
        virsh.execute.return_value = MagicMock(
            ok=ok, stdout=stdout, stderr=stderr)
        return virsh

    def test_absent_metadata_is_none_not_empty(self):
        virsh = self._virsh(ok=False, stderr="error: metadata not found")

        assert read_disk_records(virsh, 'node') is None

    def test_a_failed_query_raises(self):
        """A query that could not be answered is not an answer."""
        virsh = self._virsh(ok=False, stderr="error: failed to connect")

        with pytest.raises(ProvisionError, match='could not read'):
            read_disk_records(virsh, 'node')

    def test_records_are_parsed(self):
        virsh = self._virsh(
            ok=True, stdout=records_to_xml([_record()]))

        assert read_disk_records(virsh, 'node') == [_record()]

    def test_recording_a_disk_replaces_a_stale_entry_at_the_target(self):
        """A new disk at a reused target must not leave the old record."""
        virsh = self._virsh(ok=True, stdout=records_to_xml([_record()]))

        record_attached_disk(virsh, 'node', name='scratch', target='vdb',
                             source='/vm/scratch.qcow2')

        written = virsh.execute.call_args.kwargs['set']
        records = records_from_xml(written)
        assert [(r.name, r.source) for r in records] == [
            ('scratch', '/vm/scratch.qcow2')]


# ---------------------------------------------------------------------------
# The wiring: what `update` actually does with a cleared removal.
# ---------------------------------------------------------------------------

from unittest.mock import patch  # noqa: E402

from boxman.providers.libvirt.vm_differ import VMStateDiffer  # noqa: E402
from conftest import make_bare_manager  # noqa: E402


def _diff(vm_state='shut off', removed=(), refused=(), unowned=(),
          new_disks=(), resize_disks=()):
    return {
        'cpu_changed': False, 'memory_changed': False,
        'max_vcpus_changed': False, 'max_memory_changed': False,
        'new_disks': list(new_disks), 'resize_disks': list(resize_disks),
        'removed_disks': list(removed),
        'refused_disk_removals': list(refused),
        'unowned_disks': list(unowned),
        'new_cdroms': [], 'removed_cdroms': [], 'changed_cdroms': [],
        'new_shared_folders': [], 'removed_shared_folders': [],
        'changed_shared_folders': [],
        'memballoon_changed': False, 'memballoon_restart_pending': False,
        'actual_cpus': 2, 'desired_cpus': 2,
        'actual_memory_mb': 2048, 'desired_memory_mb': 2048,
        'desired_max_vcpus': None, 'desired_max_memory_mb': None,
        'vm_state': vm_state,
    }


def _run(diff, allow_restart=False, disks_ok=True):
    mgr = make_bare_manager({'project': 'demo'})
    mgr.provider = MagicMock()
    mgr.provider.provider_config = {'uri': 'qemu:///system'}
    mgr.provider.update_vm_disks.return_value = disks_ok
    mgr.provider.update_vm_cpu_memory.return_value = {
        'success': True, 'restart_needed': False}
    mgr.provider.shutdown_and_wait.return_value = True
    mgr.provider.start_vm.return_value = True
    queue = MagicMock()

    with patch.object(VMStateDiffer, 'diff_vm', return_value=diff):
        mgr._update_single_vm('cluster1', {'workdir': '/tmp'}, 'node01',
                              {'disks': []}, queue,
                              dry_run=False, allow_restart=allow_restart)

    return mgr, queue.put.call_args.args[0][1]


class TestRemovalIsAppliedToAStoppedGuest:

    def test_the_detach_is_handed_to_the_provider(self):
        record = _record()
        mgr, result = _run(_diff(vm_state='shut off', removed=[record]))

        kwargs = mgr.provider.update_vm_disks.call_args.kwargs
        assert kwargs['removed_disks'] == [record]
        assert result['status'] == 'updated'

    def test_the_detach_is_named_in_the_changes(self):
        _mgr, result = _run(_diff(vm_state='shut off', removed=[_record()]))

        assert 'detach disks: data (vdb)' in result['details']


class TestRemovalIsDeferredOnALiveGuest:
    """Pulling a disk out from under a mounted filesystem waits."""

    def test_a_running_guest_defers_without_the_flag(self):
        mgr, result = _run(_diff(vm_state='running', removed=[_record()]))

        assert mgr.provider.update_vm_disks.call_args.kwargs['removed_disks'] == []
        assert result['status'] == 'needs_restart'

    def test_a_paused_guest_defers_too(self):
        mgr, result = _run(_diff(vm_state='paused', removed=[_record()]))

        assert mgr.provider.update_vm_disks.call_args.kwargs['removed_disks'] == []
        assert result['status'] == 'needs_restart'

    def test_the_restart_flag_authorises_the_detach(self):
        record = _record()
        mgr, _result = _run(_diff(vm_state='running', removed=[record]),
                            allow_restart=True)

        assert mgr.provider.update_vm_disks.call_args.kwargs['removed_disks'] == [record]


class TestRefusalsAndStraysAreReported:

    def test_a_refusal_is_logged_and_not_applied(self):
        record = _record()
        mgr, _result = _run(_diff(
            vm_state='shut off',
            refused=[(record, 'the disk at vdb is now something else')]))

        warnings = [c.args[0] for c in mgr.logger.warning.call_args_list if c.args]
        assert any("not detaching 'data'" in w for w in warnings)
        # nothing to apply -- and the refusal is still reported, which it was
        # not while the report sat after the "no changes detected" return
        mgr.provider.update_vm_disks.assert_not_called()

    def test_a_stray_disk_is_reported_and_left_alone(self):
        mgr, _result = _run(_diff(
            vm_state='shut off',
            removed=[_record()],
            unowned=[_attached('vdc', '/vm/attached-by-hand.qcow2')]))

        warnings = [c.args[0] for c in mgr.logger.warning.call_args_list if c.args]
        assert any('neither declared nor recorded' in w for w in warnings)
        assert mgr.provider.update_vm_disks.call_args.kwargs['removed_disks'] == [
            _record()]


class TestDetachOrderingAndSafety:
    """The provider-side half: ordering, and never deleting the image."""

    def _session(self):
        from boxman.providers.libvirt.session import LibVirtSession

        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {'uri': 'qemu:///system'}
        session.logger = MagicMock()
        return session

    def test_removals_are_skipped_after_a_failed_addition(self):
        """A detach after a failed add removes a disk whose replacement
        never arrived."""
        session = self._session()
        disk_manager = MagicMock()
        disk_manager.configure_from_disk_config.return_value = False

        with patch('boxman.providers.libvirt.session.DiskManager',
                   return_value=disk_manager), \
             patch('boxman.providers.libvirt.session.detach_disk') as detach:
            ok = session.update_vm_disks(
                vm_name='node01',
                new_disks=[{'name': 'scratch', 'target': 'vdc'}],
                resize_disks=[], workdir='/tmp', disk_prefix='node01',
                vm_running=False, removed_disks=[_record()])

        assert ok is False
        detach.assert_not_called()

    def test_removals_run_when_everything_before_them_worked(self):
        session = self._session()
        disk_manager = MagicMock()
        disk_manager.configure_from_disk_config.return_value = True

        with patch('boxman.providers.libvirt.session.DiskManager',
                   return_value=disk_manager), \
             patch('boxman.providers.libvirt.session.detach_disk') as detach, \
             patch('boxman.providers.libvirt.session.forget_disk') as forget:
            ok = session.update_vm_disks(
                vm_name='node01',
                new_disks=[{'name': 'scratch', 'target': 'vdc'}],
                resize_disks=[], workdir='/tmp', disk_prefix='node01',
                vm_running=False, removed_disks=[_record()])

        assert ok is True
        detach.assert_called_once()
        forget.assert_called_once()

    def test_detaching_never_removes_the_image_file(self, tmp_path):
        """The qcow2 outlives the detach, deliberately."""
        from boxman.providers.libvirt.disk_ownership import detach_disk

        image = tmp_path / "data.qcow2"
        image.write_bytes(b"important")
        virsh = MagicMock()
        virsh.execute_shell.return_value = MagicMock(ok=True, stderr='')

        detach_disk(virsh, 'node01', 'vdb')

        assert image.read_bytes() == b"important"
        issued = virsh.execute_shell.call_args.args[0]
        assert 'detach-disk' in issued
        for destructive in ('rm ', 'qemu-img', '--wipe-storage',
                            '--delete-storage'):
            assert destructive not in issued
