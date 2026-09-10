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
    occupied_target_conflicts,
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


class TestAnOmittedTargetStillClaimsOne:
    """A declaration without an explicit ``target:`` still occupies vdb.

    The differ and DiskManager both default it. The removal rule filtered
    those declarations out of its claimed-target set, so renaming a disk
    without spelling out a target detached the very disk the operator had
    just renamed (#164 F2 review, finding 1).
    """

    def test_a_rename_without_an_explicit_target_is_refused(self):
        removals, refusals = plan_disk_removals(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[{'name': 'logs', 'size': 2048}],   # no target
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert removals == [], 'detached a disk the operator still declares'
        assert 'claims target vdb' in refusals[0][1]

    def test_an_omitted_target_is_not_reported_as_a_stray(self):
        listed = unowned_disks(
            records=[_record()],
            desired_disks=[{'name': 'logs', 'size': 2048}],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)],
            root_source=ROOT)

        assert listed == []


class TestDetachUsesTheConfiguredConnection:
    """The mutation must reach the domain the checks looked at.

    execute_shell() applies the sudo and runtime wrappers but supplies
    neither the configured virsh executable nor ``-c <uri>``, so the
    ownership checks ran against the configured connection and the detach
    against the default one (#164 F2 review, finding 2).
    """

    def test_detach_goes_through_the_command_builder(self):
        from boxman.providers.libvirt.disk_ownership import detach_disk

        virsh = MagicMock()
        virsh.execute.return_value = MagicMock(ok=True, stderr='')

        detach_disk(virsh, 'node01', 'vdb')

        virsh.execute.assert_called_once()
        assert virsh.execute_shell.call_count == 0, (
            'execute_shell carries no connection uri')
        args = virsh.execute.call_args.args
        assert args[0] == 'detach-disk'
        assert args[1:] == ('node01', 'vdb')
        assert virsh.execute.call_args.kwargs['config'] is True

    def test_the_built_command_carries_the_configured_uri(self):
        from boxman.providers.libvirt.commands import VirshCommand

        virsh = VirshCommand(provider_config={
            'uri': 'qemu+ssh://elsewhere/system', 'use_sudo': False})
        built = virsh.build_command('detach-disk', 'node01', 'vdb',
                                    config=True, live=None)

        assert '-c qemu+ssh://elsewhere/system' in built
        assert built.endswith('detach-disk node01 vdb --config')


class TestTheRecordedSourceMatchesTheXml:
    """The record must equal what libvirt will report back.

    The attachment XML absolutised and expanded the path; the record
    stored the raw one. A project with a relative or ``~`` workdir
    therefore recorded a source that could never match, and every removal
    on it was refused for a mismatch that was not real -- the feature
    silently did nothing (#164 F2 review, finding 5).
    """

    @pytest.mark.parametrize("given", [
        "relative/dir/vm01_data.qcow2",
        "~/vms/vm01_data.qcow2",
        "/abs/vms/vm01_data.qcow2",
    ])
    def test_record_and_xml_agree(self, given):
        import os

        from boxman.providers.libvirt.disk import (
            DiskManager,
            libvirt_disk_source,
        )

        manager = DiskManager.__new__(DiskManager)
        xml = manager._generate_disk_xml(
            disk_path=given, target_dev='vdb',
            driver_name='qemu', driver_type='qcow2', bus='virtio')

        recorded = libvirt_disk_source(given)
        assert f"source file='{recorded}'" in xml
        assert os.path.isabs(recorded)
        assert '~' not in recorded

    def test_a_relative_workdir_disk_is_still_removable(self):
        """End to end through the rule, with the path libvirt would give."""
        from boxman.providers.libvirt.disk import libvirt_disk_source

        given = "relative/dir/vm01_data.qcow2"
        source = libvirt_disk_source(given)
        removals, refusals = plan_disk_removals(
            records=[DiskRecord('data', 'vdb', 'data', source)],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', source)])

        assert [r.name for r in removals] == ['data']
        assert refusals == []


class TestDomainsWithoutRecords:
    """A VM boxman has no ownership record for is reported, never touched."""

    def test_no_metadata_removes_nothing(self):
        removals, refusals = plan_disk_removals(
            records=None,
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert removals == []
        assert refusals == []

    def test_unowned_disks_are_listed_without_metadata(self):
        """A legacy domain's undeclared disks are reported, not hidden.

        This used to return [], so a domain predating the ownership record
        produced no removals *and* no report: dropping a disk from its
        config looked like a no-op (#164 F2 review, finding 10). The
        no-detach behaviour is unchanged; the silence is not.
        """
        listed = unowned_disks(None, [], [_attached('vdb', DATA)])

        assert [d['source'] for d in listed] == [DATA]

    def test_a_declared_disk_is_not_listed_on_a_legacy_domain(self):
        assert unowned_disks(
            None,
            [{'name': 'data', 'target': 'vdb'}],
            [_attached('vdb', DATA)]) == []

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
          new_disks=(), resize_disks=(), cpu_restart=False):
    return {
        'cpu_changed': cpu_restart, 'memory_changed': False,
        'max_vcpus_changed': False, 'max_memory_changed': False,
        'new_disks': list(new_disks), 'resize_disks': list(resize_disks),
        'removed_disks': list(removed),
        'refused_disk_removals': list(refused),
        'unowned_disks': list(unowned),
        'disk_conflicts': [],
        'shared_folders_restart_pending': False,
        'has_disk_records': True,
        'new_cdroms': [], 'removed_cdroms': [], 'changed_cdroms': [],
        'new_shared_folders': [], 'removed_shared_folders': [],
        'changed_shared_folders': [],
        'memballoon_changed': False, 'memballoon_restart_pending': False,
        'actual_cpus': 2, 'desired_cpus': 2,
        'actual_memory_mb': 2048, 'desired_memory_mb': 2048,
        'desired_max_vcpus': None, 'desired_max_memory_mb': None,
        'vm_state': vm_state,
    }


def _run(diff, allow_restart=False, disks_ok=True, shutdown_ok=True,
         remove_error=None, order=None, deferral=None):
    mgr = make_bare_manager({'project': 'demo'})
    mgr.provider = MagicMock()
    mgr.provider.provider_config = {'uri': 'qemu:///system'}
    mgr.provider.update_vm_disks.return_value = disks_ok
    mgr.provider.update_vm_cpu_memory.return_value = {
        'success': True, 'restart_needed': diff.get('cpu_changed', False)}
    mgr.provider.virsh_invocation.side_effect = (
        lambda *a, **k: "docker exec c bash -c 'virsh -c qemu+ssh://host/system "
                        + ' '.join(str(x) for x in a) + " --config'")
    steps = order if order is not None else []

    def _shutdown(*a, **kw):
        steps.append('shutdown')
        return shutdown_ok
    def _detach(*a, **kw):
        steps.append('detach')
        if remove_error:
            raise remove_error
        return {'detached': [], 'deferred': deferral}
    def _start(*a, **kw):
        steps.append('start')
        return True

    mgr.provider.shutdown_and_wait.side_effect = _shutdown
    mgr.provider.remove_vm_disks.side_effect = _detach
    mgr.provider.start_vm.side_effect = _start
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

        mgr.provider.remove_vm_disks.assert_called_once_with(
            'bprj__demo__bprj_cluster1_node01', [record])
        assert result['status'] == 'updated'

    def test_the_detach_is_named_in_the_changes(self):
        _mgr, result = _run(_diff(vm_state='shut off', removed=[_record()]))

        assert 'detach disks: data (vdb)' in result['details']

    def test_a_refusing_provider_fails_the_vm(self):
        """remove_vm_disks re-verifies; its refusal must not be swallowed."""
        from boxman.exceptions import ProvisionError

        mgr, result = _run(_diff(vm_state='shut off', removed=[_record()]),
                           remove_error=ProvisionError('it holds managed saved state'))

        assert result['status'] == 'failed'
        assert 'managed saved state' in result['details']


class TestRemovalIsDeferredOnALiveGuest:
    """A detach is only ever applied to an inactive domain."""

    def test_a_running_guest_defers_without_the_flag(self):
        mgr, result = _run(_diff(vm_state='running', removed=[_record()]))

        mgr.provider.remove_vm_disks.assert_not_called()
        assert result['status'] == 'needs_restart'

    def test_a_paused_guest_defers_too(self):
        mgr, result = _run(_diff(vm_state='paused', removed=[_record()]))

        mgr.provider.remove_vm_disks.assert_not_called()
        assert result['status'] == 'needs_restart'

    def test_a_paused_guest_is_never_shut_down_for_a_detach(self):
        """Even with --restart. It did not ask to be stopped or resumed."""
        mgr, result = _run(_diff(vm_state='paused', removed=[_record()]),
                           allow_restart=True)

        mgr.provider.shutdown_and_wait.assert_not_called()
        mgr.provider.remove_vm_disks.assert_not_called()
        assert result['status'] == 'needs_restart'


class TestDetachHappensBetweenShutdownAndStart:
    """With --restart on a running guest, the ordering is the safety."""

    def test_the_order_is_shutdown_detach_start(self):
        order = []
        mgr, result = _run(_diff(vm_state='running', removed=[_record()]),
                           allow_restart=True, order=order)

        assert order == ['shutdown', 'detach', 'start']
        assert result['status'] == 'updated'

    def test_the_shutdown_is_not_forced(self):
        """force_after runs `virsh destroy` on timeout and still reports
        success, so a guest that never shut down cleanly would go on to
        have a disk removed."""
        mgr, _result = _run(_diff(vm_state='running', removed=[_record()]),
                            allow_restart=True)

        kwargs = mgr.provider.shutdown_and_wait.call_args.kwargs
        assert kwargs.get('force_after') is False

    def test_a_restart_without_a_detach_still_forces(self):
        """The existing restart behaviour is unchanged where no disk goes."""
        mgr, _result = _run(
            _diff(vm_state='running', cpu_restart=True), allow_restart=True)

        kwargs = mgr.provider.shutdown_and_wait.call_args.kwargs
        assert kwargs.get('force_after') is True

    def test_a_failed_shutdown_prevents_the_detach(self):
        mgr, result = _run(_diff(vm_state='running', removed=[_record()]),
                           allow_restart=True, shutdown_ok=False)

        mgr.provider.remove_vm_disks.assert_not_called()
        assert result['status'] == 'failed'

    def test_a_failed_detach_starts_the_guest_again(self):
        from boxman.exceptions import ProvisionError

        mgr, result = _run(_diff(vm_state='running', removed=[_record()]),
                           allow_restart=True,
                           remove_error=ProvisionError('source mismatch'))

        mgr.provider.start_vm.assert_called_once()
        assert result['status'] == 'failed'
        assert 'started again' in result['details']


class TestRefusalsAndStraysAreReported:

    def test_a_refusal_is_logged_and_not_applied(self):
        record = _record()
        mgr, _result = _run(_diff(
            vm_state='shut off',
            refused=[(record, 'the disk at vdb is now something else')]))

        warnings = [c.args[0] for c in mgr.logger.warning.call_args_list if c.args]
        assert any("not detaching 'data'" in w for w in warnings)
        mgr.provider.remove_vm_disks.assert_not_called()

    def test_a_stray_disk_is_reported_and_left_alone(self):
        mgr, _result = _run(_diff(
            vm_state='shut off',
            removed=[_record()],
            unowned=[_attached('vdc', '/vm/attached-by-hand.qcow2')]))

        warnings = [c.args[0] for c in mgr.logger.warning.call_args_list if c.args]
        assert any('neither declared nor recorded' in w for w in warnings)
        mgr.provider.remove_vm_disks.assert_called_once()


class TestDetachOrderingAndSafety:
    """Removals come after the additions, and only if those worked."""

    def test_removals_are_skipped_after_a_failed_addition(self):
        mgr, result = _run(
            _diff(vm_state='shut off', removed=[_record()],
                  new_disks=[{'name': 'scratch', 'target': 'vdc'}]),
            disks_ok=False)

        mgr.provider.remove_vm_disks.assert_not_called()
        assert result['status'] == 'failed'

    def test_removals_run_when_everything_before_them_worked(self):
        mgr, _result = _run(
            _diff(vm_state='shut off', removed=[_record()],
                  new_disks=[{'name': 'scratch', 'target': 'vdc'}]))

        mgr.provider.update_vm_disks.assert_called_once()
        mgr.provider.remove_vm_disks.assert_called_once()

    def test_detaching_never_removes_the_image_file(self, tmp_path):
        """The qcow2 outlives the detach, deliberately."""
        from boxman.providers.libvirt.disk_ownership import detach_disk

        image = tmp_path / "data.qcow2"
        image.write_bytes(b"important")
        virsh = MagicMock()
        virsh.execute.return_value = MagicMock(ok=True, stderr='')

        detach_disk(virsh, 'node01', 'vdb')

        assert image.read_bytes() == b"important"
        args = virsh.execute.call_args.args
        kwargs = virsh.execute.call_args.kwargs
        assert args[0] == 'detach-disk'
        for destructive in ('wipe_storage', 'delete_storage',
                            'delete_storage_volumes'):
            assert destructive not in kwargs


class TestLegacyDomainGuidance:
    """A domain predating the record says so, and how to act (#164 F2 rev 10)."""

    def test_the_message_names_the_manual_command(self):
        diff = _diff(vm_state='shut off',
                     unowned=[_attached('vdb', DATA)])
        diff['has_disk_records'] = False
        mgr, _result = _run(diff)

        warnings = [c.args[0] for c in mgr.logger.warning.call_args_list if c.args]
        legacy = [w for w in warnings if 'predates' in w]
        assert legacy, f'no legacy notice among {warnings}'
        # the advice has to name the connection it applies to -- a bare
        # `virsh` reaches the default one (#164 F2 review, findings 2 + 10)
        # the complete, runtime-wrapped command -- arguments inside the
        # quotes, not appended after them (#164 F2 review round 2, 9)
        assert "docker exec c bash -c 'virsh -c qemu+ssh://host/system " in legacy[0]
        assert "detach-disk" in legacy[0]
        assert 'not deleted' in legacy[0]

    def test_a_recorded_domain_gets_the_other_message(self):
        diff = _diff(vm_state='shut off',
                     unowned=[_attached('vdc', '/vm/by-hand.qcow2')])
        mgr, _result = _run(diff)

        warnings = [c.args[0] for c in mgr.logger.warning.call_args_list if c.args]
        assert any('neither declared nor recorded' in w for w in warnings)
        assert not any('predates' in w for w in warnings)


class TestRemoveVmDisksReVerifies:
    """The provider re-checks, as late as possible, what made it safe.

    The plan is computed at diff time and the guest has been shut down
    since. Two facts have to still hold at the moment of the detach: the
    domain is inactive, and the target holds the exact recorded source
    (#164 F2 review, amendment 1).
    """

    def _session(self, state='shut off', saved=False, persistent=None):
        from boxman.providers.libvirt.session import LibVirtSession

        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {'uri': 'qemu:///system'}
        session.logger = MagicMock()
        session.has_managed_save = MagicMock(return_value=saved)
        session.persistent_disks = MagicMock(
            return_value=persistent if persistent is not None
            else [_attached('vda', ROOT), _attached('vdb', DATA)])
        self._state = state
        return session

    def _call(self, session, records=None):
        from boxman.providers.libvirt.vm_differ import VMStateDiffer

        # only the state probe is stubbed; domain_is_active stays real, so
        # the classification under test is the production one
        with patch.object(VMStateDiffer, 'get_vm_state',
                          return_value=self._state), \
             patch('boxman.providers.libvirt.session.detach_disk') as det, \
             patch('boxman.providers.libvirt.session.forget_disk') as forg:
            result = session.remove_vm_disks(
                'node01', records if records is not None else [_record()])
        return result, det, forg

    def test_an_inactive_domain_without_saved_state_detaches(self):
        session = self._session()
        outcome, det, forg = self._call(session)

        assert outcome == {'detached': ['data'], 'deferred': None}
        det.assert_called_once()
        forg.assert_called_once()

    def test_an_active_domain_is_refused(self):
        session = self._session(state='running')
        with pytest.raises(ProvisionError, match='not inactive'):
            self._call(session)

    def test_managed_saved_state_defers_it_without_failing(self):
        """The next start restores an XML that still has the disk.

        An *expected* deferral, reported as pending. It used to raise, so
        the worker marked the VM failed and the command exited 2 -- which
        contradicted the agreed three-way contract, and the test asserted
        the wrong half of it (#164 F2 review round 2, finding 3).
        """
        session = self._session(saved=True)
        outcome, det, forg = self._call(session)

        assert outcome['detached'] == []
        assert 'managed saved state' in outcome['deferred']
        det.assert_not_called()
        forg.assert_not_called()

    def test_a_failed_probe_is_a_failure_not_a_deferral(self):
        """None must reach exit 2, not hide behind a pending status."""
        session = self._session(saved=None)
        with pytest.raises(ProvisionError, match='could not determine'):
            self._call(session)

    def test_a_changed_persistent_source_is_refused(self):
        session = self._session(
            persistent=[_attached('vda', ROOT),
                        _attached('vdb', '/vm/something-else.qcow2')])
        with pytest.raises(ProvisionError, match='not the'):
            self._call(session)

    def test_an_already_absent_target_just_drops_the_record(self):
        session = self._session(persistent=[_attached('vda', ROOT)])
        outcome, det, forg = self._call(session)

        assert outcome['detached'] == []
        det.assert_not_called()
        forg.assert_called_once()

    def test_a_refusal_leaves_the_ownership_record_intact(self):
        session = self._session(saved=None)
        with pytest.raises(ProvisionError):
            self._call(session)
        # forget_disk is only reachable past the gate; nothing was recorded
        # as detached, so the record survives for the next run to reconsider


class TestOccupiedTargetConflicts:
    """A declaration whose target still holds a different owned disk.

    Rename ``data`` to ``logs``, keep its explicit target, change the size:
    removal is refused because the target is claimed, but nothing stopped
    the reconciliation, which matches the occupant by target and grew the
    old ``data`` image -- the operator ended up with the disk they renamed
    away from, enlarged, reported as success (#164 F2 review, finding 4).

    Keyed on the actual occupant, not on the refusal: a refusal is also
    produced when the target is vacant and only stale metadata names it,
    and that is a legitimate addition.
    """

    def test_an_occupied_target_is_a_conflict(self):
        conflicts = occupied_target_conflicts(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[{'name': 'logs', 'target': 'vdb', 'size': 4096}],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert conflicts == [('logs', 'vdb', DATA)]

    def test_an_omitted_target_conflicts_the_same_way(self):
        conflicts = occupied_target_conflicts(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[{'name': 'logs', 'size': 4096}],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert conflicts == [('logs', 'vdb', DATA)]

    def test_a_vacant_target_with_stale_metadata_is_not_a_conflict(self):
        """The operator already detached the old disk by hand.

        Only the record remains. Rejecting this would refuse a legitimate
        addition.
        """
        conflicts = occupied_target_conflicts(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[{'name': 'logs', 'target': 'vdb', 'size': 4096}],
            actual_disks=[_attached('vda', ROOT)])

        assert conflicts == []

    def test_an_unrelated_occupant_is_also_a_conflict(self):
        """Replacing the recorded disk does not make the target free.

        The removal rule refuses to detach it, but nothing stopped the
        reconciliation from resizing it -- so declaring logs/vdb with a
        larger size grew a disk nobody had any claim to (#164 F2 review
        round 2, finding 2).
        """
        conflicts = occupied_target_conflicts(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[{'name': 'logs', 'target': 'vdb', 'size': 4096}],
            actual_disks=[_attached('vda', ROOT),
                          _attached('vdb', '/vm/someone-elses.qcow2')])

        assert conflicts == [('logs', 'vdb', '/vm/someone-elses.qcow2')]

    def test_swapping_targets_between_declared_disks_is_a_conflict(self):
        """Each one's target holds the other's file."""
        other = '/vm/vm01_logs.qcow2'
        conflicts = occupied_target_conflicts(
            records=[_record(name='data', target='vdb', source=DATA),
                     _record(name='logs', target='vdc', source=other)],
            desired_disks=[{'name': 'data', 'target': 'vdc'},
                           {'name': 'logs', 'target': 'vdb'}],
            actual_disks=[_attached('vdb', DATA), _attached('vdc', other)])

        assert len(conflicts) == 2

    def test_an_adopted_occupant_is_a_conflict_for_another_name(self):
        conflicts = occupied_target_conflicts(
            records=[DiskRecord('data', 'vdb', 'adopted', DATA)],
            desired_disks=[{'name': 'logs', 'target': 'vdb', 'size': 4096}],
            actual_disks=[_attached('vdb', DATA)])

        assert conflicts == [('logs', 'vdb', DATA)]

    def test_a_still_declared_disk_is_not_a_conflict(self):
        conflicts = occupied_target_conflicts(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[{'name': 'data', 'target': 'vdb', 'size': 4096}],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert conflicts == []

    def test_a_legacy_disk_at_its_expected_path_is_not_a_conflict(self):
        """A domain predating the record still updates.

        Nothing is recorded there, so keying purely on the record would
        make every one of its disks a conflict and no such project could
        be updated at all. The occupant being the declaration's own
        expected image file settles it.
        """
        assert occupied_target_conflicts(
            None,
            [{'name': 'data', 'target': 'vdb'}],
            [_attached('vdb', DATA)],
            expected_paths={'data': DATA}) == []

    def test_an_unrecorded_stranger_at_the_target_is_a_conflict(self):
        assert occupied_target_conflicts(
            None,
            [{'name': 'logs', 'target': 'vdb'}],
            [_attached('vdb', '/vm/attached-by-hand.qcow2')],
            expected_paths={'logs': '/vm/vm01_logs.qcow2'}) == [
                ('logs', 'vdb', '/vm/attached-by-hand.qcow2')]


class TestAConflictFailsBeforeAnyMutation:
    """Nothing is applied -- cpu and memory included."""

    def test_the_vm_fails_and_nothing_is_touched(self):
        diff = _diff(vm_state='shut off',
                     resize_disks=[{'target': 'vdb', 'source': DATA,
                                    'current_size_mb': 1024,
                                    'desired_size_mb': 4096}])
        diff['disk_conflicts'] = [('logs', 'vdb', DATA)]
        diff['cpu_changed'] = True
        mgr, result = _run(diff)

        assert result['status'] == 'failed'
        assert 'still holds' in result['details']
        assert 'nothing was changed' in result['details']
        mgr.provider.update_vm_disks.assert_not_called()
        mgr.provider.update_vm_cpu_memory.assert_not_called()
        mgr.provider.remove_vm_disks.assert_not_called()


class TestAdoptedDisksAreReportedNotDetached:
    """boxman attached it, but did not create it (#164 F2 review, finding 5).

    ``attach_only`` follows an existence check at the expected pathname.
    That is not proof boxman made the file — it could have been put there
    by hand, or be a naming collision. Such a disk is never detached
    automatically, and the ``role != data`` check used to skip it in
    silence, which is the same silence finding 10 was about.
    """

    def _adopted(self):
        return DiskRecord('data', 'vdb', 'adopted', DATA)

    def test_an_adopted_disk_is_not_removed(self):
        removals, _refusals = plan_disk_removals(
            records=[self._adopted()],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert removals == []

    def test_an_adopted_disk_is_reported(self):
        _removals, refusals = plan_disk_removals(
            records=[self._adopted()],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert len(refusals) == 1
        assert 'did not create it' in refusals[0][1]

    def test_the_root_disk_is_still_silent(self):
        """It is recorded so it need not be guessed at, not to be reported."""
        _removals, refusals = plan_disk_removals(
            records=[DiskRecord('root', 'vda', 'root', ROOT)],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT)])

        assert refusals == []

    def test_a_still_declared_adopted_disk_is_quiet(self):
        _removals, refusals = plan_disk_removals(
            records=[self._adopted()],
            desired_disks=[{'name': 'data', 'target': 'vdb'}],
            actual_disks=[_attached('vdb', DATA)])

        assert refusals == []

    def test_attach_only_is_recorded_as_adopted(self):
        from boxman.providers.libvirt.disk_ownership import (
            ROLE_ADOPTED,
            ROLE_DATA,
        )

        assert ROLE_ADOPTED != ROLE_DATA


class TestEachDetachIsVerifiedSeparately:
    """The checks are per record, not per batch (#164 F2 review 2, finding 1).

    Reading state, managed-save status and the inventory once before the
    loop meant the second detach in a batch was authorised by a snapshot
    taken before the first one happened.
    """

    def _session(self, states, saveds, inventories):
        """A session whose probes return a different answer each call."""
        from boxman.providers.libvirt.session import LibVirtSession

        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {'uri': 'qemu:///system'}
        session.logger = MagicMock()
        session.has_managed_save = MagicMock(side_effect=list(saveds))
        session.persistent_disks = MagicMock(side_effect=list(inventories))
        self._states = list(states)
        return session

    def _call(self, session, records):
        from boxman.providers.libvirt.vm_differ import VMStateDiffer

        with patch.object(VMStateDiffer, 'get_vm_state',
                          side_effect=self._states), \
             patch('boxman.providers.libvirt.session.detach_disk') as det, \
             patch('boxman.providers.libvirt.session.forget_disk') as forg:
            return session.remove_vm_disks('node01', records), det, forg

    def test_a_replacement_appearing_mid_batch_is_refused(self):
        """vdc is replaced after vdb comes off."""
        first = _record(name='data', target='vdb', source=DATA)
        second = _record(name='logs', target='vdc', source='/vm/logs.qcow2')
        session = self._session(
            states=['shut off', 'shut off'],
            saveds=[False, False],
            inventories=[
                # before the first detach: both as recorded
                [_attached('vdb', DATA), _attached('vdc', '/vm/logs.qcow2')],
                # before the second: vdc now holds something else
                [_attached('vdc', '/vm/replacement.qcow2')],
            ])

        with pytest.raises(ProvisionError, match='replacement.qcow2'):
            self._call(session, [first, second])

    def test_the_first_detach_still_happened(self):
        """It was verified against state that did hold at the time."""
        first = _record(name='data', target='vdb', source=DATA)
        second = _record(name='logs', target='vdc', source='/vm/logs.qcow2')
        session = self._session(
            states=['shut off', 'shut off'],
            saveds=[False, False],
            inventories=[
                [_attached('vdb', DATA), _attached('vdc', '/vm/logs.qcow2')],
                [_attached('vdc', '/vm/replacement.qcow2')],
            ])

        with pytest.raises(ProvisionError):
            _out, det, _forg = self._call(session, [first, second])

    def test_a_guest_started_mid_batch_is_refused(self):
        """The inactivity requirement is re-checked, not assumed."""
        first = _record(name='data', target='vdb', source=DATA)
        second = _record(name='logs', target='vdc', source='/vm/logs.qcow2')
        session = self._session(
            states=['shut off', 'running'],
            saveds=[False, False],
            inventories=[
                [_attached('vdb', DATA), _attached('vdc', '/vm/logs.qcow2')],
            ])

        with pytest.raises(ProvisionError, match='not inactive'):
            self._call(session, [first, second])

    def test_saved_state_appearing_mid_batch_defers_the_rest(self):
        first = _record(name='data', target='vdb', source=DATA)
        second = _record(name='logs', target='vdc', source='/vm/logs.qcow2')
        session = self._session(
            states=['shut off', 'shut off'],
            saveds=[False, True],
            inventories=[
                [_attached('vdb', DATA), _attached('vdc', '/vm/logs.qcow2')],
            ])

        outcome, _det, _forg = self._call(session, [first, second])

        assert outcome['detached'] == ['data']
        assert 'managed saved state' in outcome['deferred']

    def test_a_clean_batch_detaches_both(self):
        first = _record(name='data', target='vdb', source=DATA)
        second = _record(name='logs', target='vdc', source='/vm/logs.qcow2')
        inventory = [_attached('vdb', DATA), _attached('vdc', '/vm/logs.qcow2')]
        session = self._session(
            states=['shut off', 'shut off'],
            saveds=[False, False],
            inventories=[inventory, inventory])

        outcome, det, _forg = self._call(session, [first, second])

        assert outcome == {'detached': ['data', 'logs'], 'deferred': None}
        assert det.call_count == 2


class TestManualAdviceCarriesTheRuntime:
    """Built and wrapped whole, against a real runtime configuration.

    build_command() alone omits the runtime wrapper, so under the
    docker-compose runtime the advice named host libvirt. And because the
    wrapper *quotes* the command, arguments appended after a prefix would
    land outside the quotes and not run at all (#164 F2 review round 2, 9).
    """

    def _session(self, **provider_config):
        from boxman.providers.libvirt.session import LibVirtSession

        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {'uri': 'qemu:///system', 'use_sudo': False,
                                   **provider_config}
        session.logger = MagicMock()
        return session

    def test_the_local_runtime_gives_a_plain_command(self):
        advice = self._session().virsh_invocation(
            'detach-disk', 'node01', 'vdb', config=True)

        assert advice == 'virsh -c qemu:///system detach-disk node01 vdb --config'

    def test_the_docker_runtime_wraps_the_whole_command(self):
        advice = self._session(
            runtime='docker-compose',
            runtime_container='boxman-libvirt').virsh_invocation(
                'detach-disk', 'node01', 'vdb', config=True)

        assert advice.startswith('docker exec')
        assert 'boxman-libvirt' in advice
        # the arguments are inside the wrapper's quoting, not after it
        assert advice.rstrip().endswith("--config'")
        assert 'detach-disk node01 vdb --config' in advice

    def test_an_unwrappable_runtime_does_not_abort_the_run(self):
        """Generating a suggestion must never raise."""
        advice = self._session(runtime='docker-compose').virsh_invocation(
            'detach-disk', 'node01', 'vdb', config=True)

        assert 'detach-disk node01 vdb --config' in advice


class TestUnnamedDeclarationsKeepTheirIdentity:
    """An omitted ``name:`` still names a disk (#164 F2 review 3, finding 2).

    DiskManager defaults it to ``disk`` when it creates the image, so the
    record is under that name. Leaving the default out of the removal
    rule made an unnamed declaration look like a *different* logical disk
    from the one recorded — so moving it to a vacant target read as "the
    recorded disk is no longer declared" and authorised detaching it.
    """

    UNNAMED = "/vm/bprj__demo__bprj_cluster_node_disk.qcow2"

    def test_moving_an_unnamed_disk_does_not_detach_the_original(self):
        removals, _refusals = plan_disk_removals(
            records=[DiskRecord('disk', 'vdb', 'data', self.UNNAMED)],
            desired_disks=[{'target': 'vdc', 'size': 2048}],
            actual_disks=[_attached('vda', ROOT),
                          _attached('vdb', self.UNNAMED)])

        assert removals == [], 'detached the disk the declaration still names'

    def test_an_unnamed_disk_left_alone_is_no_conflict(self):
        conflicts = occupied_target_conflicts(
            records=[DiskRecord('disk', 'vdb', 'data', self.UNNAMED)],
            desired_disks=[{'size': 2048}],
            actual_disks=[_attached('vdb', self.UNNAMED)],
            expected_paths={'disk': self.UNNAMED})

        assert conflicts == []

    def test_a_genuinely_dropped_unnamed_disk_is_still_removed(self):
        removals, _refusals = plan_disk_removals(
            records=[DiskRecord('disk', 'vdb', 'data', self.UNNAMED)],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT),
                          _attached('vdb', self.UNNAMED)])

        assert [r.name for r in removals] == ['disk']


class TestAStaleRecordCannotVouchForAReplacement:
    """Finding 1: name match alone let a replacement through to a resize."""

    def test_a_replaced_source_under_the_same_name_is_a_conflict(self):
        conflicts = occupied_target_conflicts(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[{'name': 'data', 'target': 'vdb', 'size': 4096}],
            actual_disks=[_attached('vdb', '/vm/replacement.qcow2')],
            expected_paths={'data': DATA})

        assert conflicts == [('data', 'vdb', '/vm/replacement.qcow2')]

    def test_the_unchanged_disk_is_still_fine(self):
        conflicts = occupied_target_conflicts(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[{'name': 'data', 'target': 'vdb', 'size': 4096}],
            actual_disks=[_attached('vdb', DATA)],
            expected_paths={'data': DATA})

        assert conflicts == []


class TestDeferralsSurviveTheWorkerBoundary:
    """Finding 3: a structured deferral must reach the result."""

    def test_a_shut_off_guest_reports_the_deferral(self):
        """It is not active, so the pending branch used to skip it."""
        mgr, result = _run(_diff(vm_state='shut off', removed=[_record()]),
                           deferral='it holds managed saved state')

        assert result['status'] == 'needs_restart'
        assert 'managed saved state' in result['details']

    def test_a_partial_batch_deferral_is_reported(self):
        mgr, result = _run(_diff(vm_state='shut off', removed=[_record()]),
                           deferral='saved state appeared partway through')

        assert result['status'] == 'needs_restart'
        assert 'partway' in result['details']

    def test_no_deferral_still_reports_updated(self):
        _mgr, result = _run(_diff(vm_state='shut off', removed=[_record()]))

        assert result['status'] == 'updated'

    def test_a_deferral_after_the_restart_is_reported(self):
        _mgr, result = _run(_diff(vm_state='running', removed=[_record()]),
                            allow_restart=True,
                            deferral='saved state appeared after shutdown')

        assert result['status'] == 'needs_restart'
        assert 'restarted' in result['details']
        assert 'saved state' in result['details']
