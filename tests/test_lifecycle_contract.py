"""
Part 5 of #164 — the lifecycle verbs must do what they promise.

FB-3: ``boxman down`` saves guest state that ``boxman up`` never restores.
FB-5: ``boxman update`` on an ISO-boot VM detaches the install ISO.

The shared theme is fail-open behaviour: a query that fails, or a value that
cannot be resolved, is currently read as a benign answer ("nothing exists",
"nothing is saved", "this path") and acted on destructively.
"""

import contextlib
import hashlib
import os
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import ProvisionError
from conftest import make_bare_manager

PRJ = 'bprj__proj__bprj'
VM1 = f'{PRJ}_web_node01'
VM2 = f'{PRJ}_web_node02'


def _config():
    return {
        'project': 'proj',
        'clusters': {
            'web': {
                'workdir': '/tmp/wd',
                'vms': {'node01': {}, 'node02': {}},
            },
        },
    }


def _list_table(rows):
    """Render rows of (id, name, state) as ``virsh list --all`` output."""
    out = [' Id   Name                 State',
           '----------------------------------']
    for dom_id, name, state in rows:
        out.append(f' {dom_id}    {name}          {state}')
    return '\n'.join(out) + '\n'


class _Virsh:
    """
    Dispatching fake for VirshCommand.

    Keyed on the subcommand and its flags rather than call order — an
    ordered ``side_effect`` list breaks the moment a caller adds or reorders
    a query, which has bitten this suite before.
    """

    def __init__(self, table=None, managed_saved=None,
                 list_ok=True, managed_ok=True):
        self.table = table or ''
        self.managed_saved = managed_saved or []
        self.list_ok = list_ok
        self.managed_ok = managed_ok
        self.calls = []

    def execute(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == 'list' and '--with-managed-save' in args:
            return MagicMock(
                ok=self.managed_ok, return_code=0 if self.managed_ok else 1,
                stdout='\n'.join(self.managed_saved) + '\n',
                stderr='' if self.managed_ok else 'probe failed')
        if args[0] == 'list':
            return MagicMock(
                ok=self.list_ok, return_code=0 if self.list_ok else 1,
                stdout=self.table,
                stderr='' if self.list_ok else 'connection refused')
        raise AssertionError(f'unexpected virsh call: {args}')

    @property
    def managed_save_queries(self):
        return [c for c in self.calls if '--with-managed-save' in c]


def _manager(virsh):
    mgr = make_bare_manager(_config())
    mgr.provider = MagicMock()
    mgr.provider.provider_config = {'uri': 'qemu:///system'}
    mgr._virsh = MagicMock(return_value=virsh)
    return mgr


# ---------------------------------------------------------------------------
# FB-3 — up must be able to see that a guest has saved state
# ---------------------------------------------------------------------------
class TestVmStateClassification:

    def test_query_failure_raises_instead_of_reporting_no_vms(self):
        """
        A failed ``virsh list`` used to return ``{}``.

        ``up`` reads an empty mapping as "no VM exists" and runs a full
        provision, so an unreachable libvirt looked exactly like a fresh
        project.
        """
        virsh = _Virsh(list_ok=False)
        mgr = _manager(virsh)

        with pytest.raises(ProvisionError) as exc:
            mgr._get_vm_states()

        assert 'could not query VM states' in str(exc.value)
        assert 'connection refused' in str(exc.value)

    def test_shut_off_domain_with_managed_save_is_reported_as_managedsave(self):
        """The whole point of FB-3: 'shut off' hides a saved guest."""
        virsh = _Virsh(table=_list_table([('-', VM1, 'shut off')]),
                       managed_saved=[VM1])
        mgr = _manager(virsh)

        assert mgr._get_vm_states() == {VM1: 'managedsave'}

    def test_shut_off_domain_without_managed_save_stays_shut_off(self):
        virsh = _Virsh(table=_list_table([('-', VM1, 'shut off')]),
                       managed_saved=[])
        mgr = _manager(virsh)

        assert mgr._get_vm_states() == {VM1: 'shut off'}

    def test_only_the_shut_off_domains_are_relabelled(self):
        """A running domain is never relabelled, even if virsh names it."""
        virsh = _Virsh(
            table=_list_table([('1', VM1, 'running'), ('-', VM2, 'shut off')]),
            managed_saved=[VM1, VM2])
        mgr = _manager(virsh)

        assert mgr._get_vm_states() == {VM1: 'running', VM2: 'managedsave'}

    def test_no_managed_save_query_when_nothing_is_shut_off(self):
        """A running or paused domain cannot have a managed save waiting."""
        virsh = _Virsh(table=_list_table([('1', VM1, 'running'),
                                          ('2', VM2, 'paused')]))
        mgr = _manager(virsh)

        mgr._get_vm_states()

        assert virsh.managed_save_queries == []

    def test_one_bulk_managed_save_query_not_one_per_vm(self):
        virsh = _Virsh(
            table=_list_table([('-', VM1, 'shut off'), ('-', VM2, 'shut off')]),
            managed_saved=[])
        mgr = _manager(virsh)

        mgr._get_vm_states()

        assert len(virsh.managed_save_queries) == 1

    def test_managed_save_probe_failure_raises_rather_than_cold_booting(self):
        """
        An empty answer here means "nothing is saved".

        Acting on that after a *failed* probe cold-boots a guest whose
        memory image is sitting on disk — the fail-open answer this query
        exists to avoid.
        """
        virsh = _Virsh(table=_list_table([('-', VM1, 'shut off')]),
                       managed_ok=False)
        mgr = _manager(virsh)

        with pytest.raises(ProvisionError) as exc:
            mgr._get_vm_states()

        assert 'managed saved state' in str(exc.value)

    def test_shutoff_spelling_variant_is_classified_too(self):
        virsh = _Virsh(table=_list_table([('-', VM1, 'shutoff')]),
                       managed_saved=[VM1])
        mgr = _manager(virsh)

        assert mgr._get_vm_states() == {VM1: 'managedsave'}


# ---------------------------------------------------------------------------
# FB-3 — down must save state that up can actually bring back
# ---------------------------------------------------------------------------
class _FakeLibvirt:
    """
    A very small stateful stand-in for the virsh commands save/restore use.

    Stateful on purpose: ``managedsave`` changes what the next ``domstate``
    reports, so an ordered ``side_effect`` list would encode call order rather
    than behaviour and break on any reordering.
    """

    def __init__(self, state='running', managed_saved=False,
                 managedsave_ok=True, start_ok=True, restore_ok=True,
                 probe_ok=True, domstate_ok=True, stops_on_save=True):
        self.state = state
        self.managed_saved = managed_saved
        self.managedsave_ok = managedsave_ok
        self.start_ok = start_ok
        self.restore_ok = restore_ok
        self.probe_ok = probe_ok
        self.domstate_ok = domstate_ok
        self.stops_on_save = stops_on_save
        self.calls = []

    def execute(self, *args, **kwargs):
        self.calls.append(args)
        cmd = args[0]

        if cmd == 'domstate':
            return MagicMock(ok=self.domstate_ok, return_code=0,
                             stdout=f'{self.state}\n', stderr='')

        if cmd == 'list' and '--with-managed-save' in args:
            return MagicMock(
                ok=self.probe_ok, return_code=0 if self.probe_ok else 1,
                stdout=(f'{VM1}\n' if self.managed_saved else '\n'),
                stderr='' if self.probe_ok else 'probe failed')

        if cmd == 'managedsave':
            if self.managedsave_ok:
                if self.stops_on_save:
                    self.state = 'shut off'
                self.managed_saved = True
            return MagicMock(ok=self.managedsave_ok,
                             return_code=0 if self.managedsave_ok else 1,
                             stdout='', stderr='unsupported by this guest')

        if cmd == 'start':
            if self.start_ok:
                self.state = 'running'
                self.managed_saved = False
            return MagicMock(ok=self.start_ok,
                             return_code=0 if self.start_ok else 1,
                             stdout='', stderr='start failed')

        if cmd == 'restore':
            if self.restore_ok:
                self.state = 'running'
            return MagicMock(ok=self.restore_ok,
                             return_code=0 if self.restore_ok else 1,
                             stdout='', stderr='restore failed')

        raise AssertionError(f'unexpected virsh call: {args}')

    def commands(self):
        return [c[0] for c in self.calls]


def _libvirt_session():
    from boxman.providers.libvirt.session import LibVirtSession
    session = LibVirtSession(config={'provider': {'libvirt': {}}})
    session.logger = MagicMock()
    return session


class TestSaveUsesManagedSave:

    def _run(self, fake, method='save_vm', **kwargs):
        with patch('boxman.providers.libvirt.session.VirshCommand',
                   return_value=fake):
            session = _libvirt_session()
            return getattr(session, method)(VM1, '/tmp/wd', **kwargs)

    def test_save_uses_managedsave_not_an_external_file(self):
        """
        The external ``virsh save`` is what made the state unrecoverable:
        libvirt kept no record of it, so ``up`` cold-booted over it.
        """
        fake = _FakeLibvirt(state='running')

        assert self._run(fake) is True
        assert 'managedsave' in fake.commands()
        assert 'save' not in fake.commands()

    def test_unsavable_guest_is_reported_not_silently_stopped(self):
        """
        Some guests (PCI passthrough) cannot be saved. That is a failure to
        report, never a reason to substitute a shutdown.
        """
        fake = _FakeLibvirt(state='running', managedsave_ok=False)

        assert self._run(fake) is False
        assert 'shutdown' not in fake.commands()
        assert 'destroy' not in fake.commands()

    def test_save_fails_when_the_guest_did_not_stop(self):
        """A guest still running after managedsave has not been saved."""
        fake = _FakeLibvirt(state='running', stops_on_save=False)

        assert self._run(fake) is False

    def test_save_fails_when_the_image_cannot_be_confirmed(self):
        """A stopped guest with no memory image is not a save."""
        fake = _FakeLibvirt(state='running')
        fake.managedsave_ok = True

        with patch('boxman.providers.libvirt.session.VirshCommand',
                   return_value=fake):
            session = _libvirt_session()
            session.has_managed_save = MagicMock(return_value=False)
            assert session.save_vm(VM1, '/tmp/wd') is False

    def test_save_fails_when_presence_probe_cannot_answer(self):
        fake = _FakeLibvirt(state='running')

        with patch('boxman.providers.libvirt.session.VirshCommand',
                   return_value=fake):
            session = _libvirt_session()
            session.has_managed_save = MagicMock(return_value=None)
            assert session.save_vm(VM1, '/tmp/wd') is False

    def test_save_refuses_when_the_state_query_fails(self):
        """An unreadable state is not 'not running'."""
        fake = _FakeLibvirt(state='running', domstate_ok=False)

        assert self._run(fake) is False
        assert 'managedsave' not in fake.commands()

    def test_not_running_guest_is_not_saved(self):
        fake = _FakeLibvirt(state='shut off')

        assert self._run(fake) is False
        assert 'managedsave' not in fake.commands()


class TestRestorePolicy:

    def test_managed_save_is_restored_by_start(self):
        """libvirt reads and removes the image itself on start."""
        fake = _FakeLibvirt(state='shut off', managed_saved=True)

        with patch('boxman.providers.libvirt.session.VirshCommand',
                   return_value=fake):
            session = _libvirt_session()
            assert session.restore_vm(VM1, '/tmp/wd') is True

        assert 'start' in fake.commands()
        assert 'restore' not in fake.commands()

    def test_probe_failure_never_falls_through_to_a_legacy_restore(
            self, tmp_path):
        """
        An unanswered probe may be hiding managed saved state; applying an
        external file on top of it would be the worst outcome available.

        The save file has to be present for this to test anything: without
        one the legacy branch bails on the missing file, and the test passes
        whether or not the probe failure is handled.
        """
        (tmp_path / f'{VM1}.save').write_bytes(b'memory')
        fake = _FakeLibvirt(state='shut off', probe_ok=False)

        with patch('boxman.providers.libvirt.session.VirshCommand',
                   return_value=fake):
            session = _libvirt_session()
            assert session.restore_vm(VM1, str(tmp_path),
                                      allow_legacy=True) is False

        assert 'restore' not in fake.commands()
        assert (tmp_path / f'{VM1}.save').exists()

    def test_external_save_is_refused_unless_explicitly_allowed(self, tmp_path):
        (tmp_path / f'{VM1}.save').write_bytes(b'stale memory')
        fake = _FakeLibvirt(state='shut off', managed_saved=False)

        with patch('boxman.providers.libvirt.session.VirshCommand',
                   return_value=fake):
            session = _libvirt_session()
            assert session.restore_vm(VM1, str(tmp_path)) is False

        assert 'restore' not in fake.commands()
        assert (tmp_path / f'{VM1}.save').exists()

    def test_external_save_is_applied_when_explicitly_allowed(self, tmp_path):
        save = tmp_path / f'{VM1}.save'
        save.write_bytes(b'memory')
        fake = _FakeLibvirt(state='shut off', managed_saved=False)

        with patch('boxman.providers.libvirt.session.VirshCommand',
                   return_value=fake):
            session = _libvirt_session()
            assert session.restore_vm(VM1, str(tmp_path),
                                      allow_legacy=True) is True

        assert 'restore' in fake.commands()
        # removed, so it cannot be applied again to a disk that has moved on
        assert not save.exists()

    def test_a_running_guest_is_never_stopped_to_apply_an_external_save(
            self, tmp_path):
        """
        The old code shut a running guest down in order to restore a file it
        had no way to validate.
        """
        (tmp_path / f'{VM1}.save').write_bytes(b'memory')
        fake = _FakeLibvirt(state='running', managed_saved=False)

        with patch('boxman.providers.libvirt.session.VirshCommand',
                   return_value=fake):
            session = _libvirt_session()
            assert session.restore_vm(VM1, str(tmp_path),
                                      allow_legacy=True) is False

        assert 'shutdown' not in fake.commands()
        assert 'restore' not in fake.commands()

    def test_nothing_saved_at_all_is_a_failure(self, tmp_path):
        fake = _FakeLibvirt(state='shut off', managed_saved=False)

        with patch('boxman.providers.libvirt.session.VirshCommand',
                   return_value=fake):
            session = _libvirt_session()
            assert session.restore_vm(VM1, str(tmp_path)) is False


class TestUpRefusesToColdBootOverAnExternalSave:

    def _mgr(self):
        mgr = make_bare_manager(_config())
        mgr.logger = MagicMock()
        return mgr

    def test_refuses_when_an_external_save_exists(self, tmp_path):
        (tmp_path / f'{VM1}.save').write_bytes(b'memory')

        with pytest.raises(ProvisionError) as exc:
            self._mgr()._refuse_stale_external_save(VM1, str(tmp_path))

        message = str(exc.value)
        assert 'control start --restore' in message
        assert 'rm ' in message

    def test_no_save_file_is_a_no_op(self, tmp_path):
        self._mgr()._refuse_stale_external_save(VM1, str(tmp_path))

    def test_empty_workdir_is_a_no_op(self):
        self._mgr()._refuse_stale_external_save(VM1, '')


# ---------------------------------------------------------------------------
# FB-3 — a revert libvirt will refuse must not be retried twenty times
# ---------------------------------------------------------------------------
class TestSnapshotManagedSaveConflict:

    def _mgr(self, saved, has_memory):
        mgr = make_bare_manager(_config())
        mgr.logger = MagicMock()
        session = MagicMock()
        session.has_managed_save.return_value = saved
        session.snapshot_has_memory.return_value = has_memory
        mgr.session_for_vm = MagicMock(return_value=session)
        return mgr

    def test_managed_save_plus_memoryless_snapshot_is_refused(self):
        from boxman.exceptions import SnapshotError

        mgr = self._mgr(saved=True, has_memory=False)

        with pytest.raises(SnapshotError) as exc:
            mgr._refuse_managed_save_conflicts([(VM1, 'snap1')])

        message = str(exc.value)
        assert 'managedsave-remove' in message
        assert 'Nothing was changed' in message

    def test_managed_save_plus_memory_snapshot_is_allowed(self):
        mgr = self._mgr(saved=True, has_memory=True)

        mgr._refuse_managed_save_conflicts([(VM1, 'snap1')])

    def test_no_managed_save_is_allowed(self):
        mgr = self._mgr(saved=False, has_memory=False)

        mgr._refuse_managed_save_conflicts([(VM1, 'snap1')])

    def test_unanswerable_managed_save_probe_is_refused(self):
        from boxman.exceptions import SnapshotError

        mgr = self._mgr(saved=None, has_memory=True)

        with pytest.raises(SnapshotError, match='could not determine'):
            mgr._refuse_managed_save_conflicts([(VM1, 'snap1')])

    def test_unanswerable_memory_probe_is_refused(self):
        from boxman.exceptions import SnapshotError

        mgr = self._mgr(saved=True, has_memory=None)

        with pytest.raises(SnapshotError, match='could not be determined'):
            mgr._refuse_managed_save_conflicts([(VM1, 'snap1')])

    def test_every_conflicting_vm_is_named(self):
        from boxman.exceptions import SnapshotError

        mgr = self._mgr(saved=True, has_memory=False)

        with pytest.raises(SnapshotError) as exc:
            mgr._refuse_managed_save_conflicts([(VM1, 's1'), (VM2, 's2')])

        assert VM1 in str(exc.value)
        assert VM2 in str(exc.value)


class TestSnapshotHasMemory:

    def _session(self, info):
        from boxman.providers.libvirt.session import LibVirtSession
        session = LibVirtSession(config={'provider': {'libvirt': {}}})
        session.logger = MagicMock()
        mgr = MagicMock()
        mgr.snapshot_info.return_value = info
        return session, mgr

    @pytest.mark.parametrize('state,expected', [
        ('running', True),
        ('paused', True),
        ('shutoff', False),
        ('disk-snapshot', False),
    ])
    def test_state_maps_to_memory_presence(self, state, expected):
        session, mgr = self._session({'state': state})
        with patch('boxman.providers.libvirt.session.SnapshotManager',
                   return_value=mgr):
            assert session.snapshot_has_memory(VM1, 'snap1') is expected

    def test_unknown_snapshot_is_unanswerable(self):
        session, mgr = self._session(None)
        with patch('boxman.providers.libvirt.session.SnapshotManager',
                   return_value=mgr):
            assert session.snapshot_has_memory(VM1, 'snap1') is None

    def test_missing_state_field_is_unanswerable(self):
        session, mgr = self._session({'name': 'snap1'})
        with patch('boxman.providers.libvirt.session.SnapshotManager',
                   return_value=mgr):
            assert session.snapshot_has_memory(VM1, 'snap1') is None


# ---------------------------------------------------------------------------
# FB-5 — update must not detach the ISO the guest is installing from
# ---------------------------------------------------------------------------
def _diff_cdroms(desired_cdroms, actual_cdroms, vm_state='running'):
    """Run diff_vm with every probe but the CDROM one mocked out."""
    from boxman.providers.libvirt.vm_differ import VMStateDiffer

    differ = VMStateDiffer(provider_config={'use_sudo': False,
                                            'uri': 'qemu:///system'})
    with patch.object(differ, 'get_vm_state', return_value=vm_state), \
         patch.object(differ, 'get_actual_cpu',
                      return_value={'sockets': 1, 'cores': 1, 'threads': 1,
                                    'total_vcpus': 1, 'current_vcpus': 1}), \
         patch.object(differ, 'get_max_vcpus', return_value=1), \
         patch.object(differ, 'get_actual_memory_mb', return_value=1024), \
         patch.object(differ, 'get_max_memory_mb', return_value=1024), \
         patch.object(differ, 'get_actual_disks', return_value=[]), \
         patch.object(differ, 'get_actual_shared_folders', return_value=[]), \
         patch.object(differ, 'get_actual_memballoon',
                      return_value={'free_page_reporting': False,
                                    'autodeflate': False,
                                    'stats_period': None}), \
         patch.object(differ, 'get_actual_cdroms',
                      return_value=actual_cdroms):
        return differ.diff_vm(
            domain_name='vm01',
            desired_cpus=None,
            desired_memory_mb=None,
            desired_disks=[],
            desired_cdroms=desired_cdroms,
            workdir='/tmp/wd',
            disk_prefix='vm01',
        )


class TestCdromDiff:

    def test_unresolved_entry_is_refused_not_turned_into_the_cwd(self):
        """
        ``os.path.abspath('')`` is the working directory. An unresolved
        cdrom was silently turned into that plausible-looking path, which
        matched nothing — so the ISO genuinely attached to the guest fell
        into removed_cdroms and was detached (#164 FB-5).
        """
        with pytest.raises(ProvisionError, match='no resolved source'):
            _diff_cdroms(
                desired_cdroms=[{'name': 'ubuntu-noble-live'}],
                actual_cdroms=[{'target': 'hdc',
                                'source': '/isos/ubuntu.iso'}])

    def test_attached_iso_that_is_still_declared_is_left_alone(self):
        """The headline case: a resolved, unchanged ISO is not touched."""
        diff = _diff_cdroms(
            desired_cdroms=[{'name': 'ubuntu-noble-live',
                             'source': '/isos/ubuntu.iso'}],
            actual_cdroms=[{'target': 'hdc', 'source': '/isos/ubuntu.iso'}])

        assert diff['new_cdroms'] == []
        assert diff['removed_cdroms'] == []
        assert diff['changed_cdroms'] == []

    def test_explicit_target_wins_over_source_membership(self):
        """
        With hdc=A and hdd=B attached and hdc=B desired, source membership
        was tested first: B was 'already attached somewhere', so no swap was
        generated and hdc was simply removed, leaving B on the wrong target
        (#164 FB-5).
        """
        diff = _diff_cdroms(
            desired_cdroms=[{'target': 'hdc', 'source': '/isos/b.iso'}],
            actual_cdroms=[{'target': 'hdc', 'source': '/isos/a.iso'},
                           {'target': 'hdd', 'source': '/isos/b.iso'}])

        assert diff['changed_cdroms'] == [{'target': 'hdc',
                                           'source': '/isos/b.iso'}]
        assert [c['target'] for c in diff['removed_cdroms']] == ['hdd']

    def test_swapping_two_isos_between_targets_is_detected(self):
        """This produced no changes at all."""
        diff = _diff_cdroms(
            desired_cdroms=[{'target': 'hdc', 'source': '/isos/b.iso'},
                            {'target': 'hdd', 'source': '/isos/a.iso'}],
            actual_cdroms=[{'target': 'hdc', 'source': '/isos/a.iso'},
                           {'target': 'hdd', 'source': '/isos/b.iso'}])

        assert sorted(diff['changed_cdroms'],
                      key=lambda c: c['target']) == [
            {'target': 'hdc', 'source': '/isos/b.iso'},
            {'target': 'hdd', 'source': '/isos/a.iso'},
        ]
        assert diff['removed_cdroms'] == []

    def test_media_for_an_empty_drive_is_a_change_not_an_addition(self):
        """
        An empty drive is where media gets inserted. Treating it as a free
        slot produced an attempted device addition on a target that already
        had a device (#164 FB-5).
        """
        diff = _diff_cdroms(
            desired_cdroms=[{'target': 'hdc', 'source': '/isos/a.iso'}],
            actual_cdroms=[{'target': 'hdc', 'source': None}])

        assert diff['changed_cdroms'] == [{'target': 'hdc',
                                           'source': '/isos/a.iso'}]
        assert diff['new_cdroms'] == []

    def test_an_empty_drive_is_never_removed(self):
        """It holds no media, and dropping it changes the topology."""
        diff = _diff_cdroms(
            desired_cdroms=[],
            actual_cdroms=[{'target': 'hdc', 'source': None}])

        assert diff['removed_cdroms'] == []

    def test_two_entries_on_one_target_are_refused(self):
        with pytest.raises(ProvisionError, match='more than one cdrom'):
            _diff_cdroms(
                desired_cdroms=[{'target': 'hdc', 'source': '/isos/a.iso'},
                                {'target': 'hdc', 'source': '/isos/b.iso'}],
                actual_cdroms=[])

    def test_an_undeclared_iso_is_removed(self):
        diff = _diff_cdroms(
            desired_cdroms=[],
            actual_cdroms=[{'target': 'hdc', 'source': '/isos/a.iso'}])

        assert [c['target'] for c in diff['removed_cdroms']] == ['hdc']


# ---------------------------------------------------------------------------
# FB-5 — resolving media for an update must fetch nothing and verify anyway
# ---------------------------------------------------------------------------
ISO_BYTES = b'pretend this is an installer'
ISO_SHA = 'sha256:' + hashlib.sha256(ISO_BYTES).hexdigest()


def _iso_manager(tmp_path, isos, cdroms):
    cfg = {
        'project': 'proj',
        'isos': isos,
        'clusters': {
            'web': {
                'workdir': '/tmp/wd',
                'vms': {'node01': {'cdroms': cdroms}},
            },
        },
    }
    mgr = make_bare_manager(cfg)
    mgr.app_config = {'cache': {'enabled': True,
                                'cache_dir': str(tmp_path)}}
    mgr.provider = MagicMock()
    return mgr


def _cache_the_iso(mgr, tmp_path, name, uri, content=ISO_BYTES):
    filename = mgr._iso_cache_filename(name, uri)
    path = tmp_path / filename
    path.write_bytes(content)
    return path


class TestUpdateMediaResolution:

    URI = 'https://example.invalid/ubuntu-noble-live.iso'

    def test_cached_iso_resolves_without_downloading(self, tmp_path):
        mgr = _iso_manager(tmp_path, {'live': {'uri': self.URI}},
                           [{'name': 'live'}])
        _cache_the_iso(mgr, tmp_path, 'live', self.URI)

        with patch.object(type(mgr), '_download_iso',
                          side_effect=AssertionError('must not download')):
            failures = mgr._normalize_cdroms_for_update({VM1})

        assert failures == {}
        entry = mgr.config['clusters']['web']['vms']['node01']['cdroms'][0]
        assert entry['source'].endswith('.iso')
        assert os.path.isfile(entry['source'])

    def test_a_dry_run_never_downloads(self, tmp_path):
        """
        Previewing an update must have no side effects, so a dry run reports
        what it *would* fetch instead of fetching it.
        """
        mgr = _iso_manager(tmp_path, {'live': {'uri': self.URI}},
                           [{'name': 'live'}])

        with patch.object(type(mgr), '_resolve_isos',
                          side_effect=AssertionError('must not download')):
            failures = mgr._normalize_cdroms_for_update(
                {VM1}, allow_fetch=False)

        assert 'would download it' in failures[VM1]
        assert '--dry-run' in failures[VM1]

    def test_a_missing_iso_is_fetched_on_a_real_run(self, tmp_path):
        """
        `update` is the only non-destructive verb that can give an existing
        VM media it does not have: nothing else resolves cdroms for a VM that
        already exists, so refusing to fetch made "attach this ISO" an
        unsatisfiable declaration (#164 FB-5).
        """
        mgr = _iso_manager(tmp_path, {'live': {'uri': self.URI}},
                           [{'name': 'live'}])
        iso = tmp_path / 'fetched.iso'
        iso.write_bytes(ISO_BYTES)

        with patch.object(type(mgr), '_resolve_isos',
                          return_value={'live': str(iso)}) as fetch:
            failures = mgr._normalize_cdroms_for_update({VM1})

        assert failures == {}
        fetch.assert_called_once_with({'live'})
        entry = mgr.config['clusters']['web']['vms']['node01']['cdroms'][0]
        assert entry['source'] == str(iso)

    def test_a_failed_fetch_fails_only_its_own_vm(self, tmp_path):
        mgr = _iso_manager(tmp_path, {'live': {'uri': self.URI}},
                           [{'name': 'live'}])

        with patch.object(type(mgr), '_resolve_isos',
                          side_effect=RuntimeError('404 not found')):
            failures = mgr._normalize_cdroms_for_update({VM1})

        assert '404 not found' in failures[VM1]

    def test_media_already_present_is_never_re_downloaded(self, tmp_path):
        """
        The downloader writes straight to the cache path, so fetching over a
        file that exists can truncate an ISO a running guest has open.
        """
        mgr = _iso_manager(tmp_path, {'live': {'uri': self.URI}},
                           [{'name': 'live'}])
        _cache_the_iso(mgr, tmp_path, 'live', self.URI)

        with patch.object(type(mgr), '_resolve_isos',
                          side_effect=AssertionError('must not re-download')):
            assert mgr._normalize_cdroms_for_update({VM1}) == {}

    def test_a_checksum_mismatch_is_never_fixed_by_downloading(self, tmp_path):
        """A bad file is an error to report, not a reason to fetch over it."""
        mgr = _iso_manager(
            tmp_path,
            {'live': {'uri': self.URI, 'checksum': ISO_SHA}},
            [{'name': 'live'}])
        _cache_the_iso(mgr, tmp_path, 'live', self.URI,
                       content=b'not the declared bytes')

        with patch.object(type(mgr), '_resolve_isos',
                          side_effect=AssertionError('must not download')):
            failures = mgr._normalize_cdroms_for_update({VM1})

        assert 'checksum' in failures[VM1]

    def test_checksum_mismatch_is_reported_and_the_file_is_kept(self, tmp_path):
        """
        Verification must survive the split away from _resolve_isos, and this
        read-only path must not evict: deleting the file would turn a diff
        into a mutation.
        """
        mgr = _iso_manager(
            tmp_path,
            {'live': {'uri': self.URI, 'checksum': ISO_SHA}},
            [{'name': 'live'}])
        path = _cache_the_iso(mgr, tmp_path, 'live', self.URI,
                              content=b'not the declared bytes')

        failures = mgr._normalize_cdroms_for_update({VM1})

        assert 'checksum' in failures[VM1]
        assert path.exists()

    def test_matching_checksum_resolves(self, tmp_path):
        mgr = _iso_manager(
            tmp_path,
            {'live': {'uri': self.URI, 'checksum': ISO_SHA}},
            [{'name': 'live'}])
        _cache_the_iso(mgr, tmp_path, 'live', self.URI)

        assert mgr._normalize_cdroms_for_update({VM1}) == {}

    def test_undeclared_iso_name_is_reported(self, tmp_path):
        mgr = _iso_manager(tmp_path, {}, [{'name': 'nope'}])

        failures = mgr._normalize_cdroms_for_update({VM1})

        assert "not declared in the 'isos:' section" in failures[VM1]

    def test_explicit_source_needs_no_isos_declaration(self, tmp_path):
        """
        SKILL.md documents ``{name: installer, source: /iso/ubuntu.iso}``.
        Resolving name-first rejected it with "references unknown iso".
        """
        iso = tmp_path / 'ubuntu.iso'
        iso.write_bytes(ISO_BYTES)
        mgr = _iso_manager(tmp_path, {},
                           [{'name': 'installer', 'source': str(iso)}])

        failures = mgr._normalize_cdroms_for_update({VM1})

        assert failures == {}
        entry = mgr.config['clusters']['web']['vms']['node01']['cdroms'][0]
        assert entry['source'] == str(iso)

    def test_a_vm_with_no_cdroms_needs_no_resolution(self, tmp_path):
        mgr = _iso_manager(tmp_path, {}, [])

        assert mgr._normalize_cdroms_for_update({VM1}) == {}

    def test_resolution_works_with_caching_disabled(self, tmp_path):
        """
        With `cache.enabled: false`, _resolve_isos still downloads to
        cache_path_for(), so an update finds the image at the path this
        read-only resolver computes. Correct, but only because two code
        paths agree about the location — pin it so it stays that way.
        """
        mgr = _iso_manager(tmp_path, {'live': {'uri': self.URI}},
                           [{'name': 'live'}])
        mgr.app_config = {'cache': {'enabled': False,
                                    'cache_dir': str(tmp_path)}}
        _cache_the_iso(mgr, tmp_path, 'live', self.URI)

        assert mgr._normalize_cdroms_for_update({VM1}) == {}

    def test_only_media_referenced_by_the_updated_vms_is_resolved(
            self, tmp_path):
        """
        Resolution used to process every declared ISO. An update that touches
        one VM should not care about an ISO only some other VM references.
        """
        mgr = _iso_manager(tmp_path, {'unused': {'uri': self.URI}}, [])

        assert mgr._normalize_cdroms_for_update({VM1}) == {}


class TestCdromApplyOrdering:

    def _session(self):
        from boxman.providers.libvirt.session import LibVirtSession
        session = LibVirtSession(config={'provider': {'libvirt': {}}})
        session.logger = MagicMock()
        return session

    def _run(self, tmp_path, new=(), removed=(), changed=(),
             attach_ok=True, change_ok=True, vm_active=True):
        iso = tmp_path / 'a.iso'
        iso.write_bytes(ISO_BYTES)
        manager = MagicMock()
        manager.configure_from_config.return_value = attach_ok
        manager.change_media.return_value = change_ok
        manager.detach_cdrom.return_value = True
        with patch('boxman.providers.libvirt.session.CDROMManager',
                   return_value=manager):
            ok = self._session().update_vm_cdroms(
                vm_name=VM1,
                new_cdroms=list(new),
                removed_cdroms=list(removed),
                changed_cdroms=list(changed),
                vm_active=vm_active)
        return ok, manager

    def test_a_failed_attach_does_not_detach_the_existing_iso(self, tmp_path):
        """
        The old order was attach, detach, swap, and a failed attach only set
        a flag — so the install ISO was detached anyway and the guest was
        left with no media at all (#164 FB-5).
        """
        ok, manager = self._run(
            tmp_path,
            new=[{'name': 'new', 'source': str(tmp_path / 'a.iso')}],
            removed=[{'target': 'hdc'}],
            attach_ok=False)

        assert ok is False
        manager.detach_cdrom.assert_not_called()

    def test_a_failed_media_change_does_not_detach_either(self, tmp_path):
        """Gating removals on additions alone left replacements uncovered."""
        ok, manager = self._run(
            tmp_path,
            changed=[{'target': 'hdc', 'source': str(tmp_path / 'a.iso')}],
            removed=[{'target': 'hdd'}],
            change_ok=False)

        assert ok is False
        manager.detach_cdrom.assert_not_called()

    def test_a_missing_iso_is_refused_before_anything_is_mutated(
            self, tmp_path):
        ok, manager = self._run(
            tmp_path,
            new=[{'name': 'gone', 'source': str(tmp_path / 'missing.iso')}],
            removed=[{'target': 'hdc'}])

        assert ok is False
        manager.configure_from_config.assert_not_called()
        manager.change_media.assert_not_called()
        manager.detach_cdrom.assert_not_called()

    def test_a_media_change_with_no_target_is_refused(self, tmp_path):
        ok, manager = self._run(
            tmp_path,
            changed=[{'source': str(tmp_path / 'a.iso')}])

        assert ok is False
        manager.change_media.assert_not_called()

    def test_removals_run_when_everything_before_them_worked(self, tmp_path):
        ok, manager = self._run(
            tmp_path,
            new=[{'name': 'new', 'source': str(tmp_path / 'a.iso')}],
            removed=[{'target': 'hdc'}])

        assert ok is True
        manager.detach_cdrom.assert_called_once_with('hdc')

    def test_activity_decides_the_media_change_scope(self, tmp_path):
        _ok, manager = self._run(
            tmp_path,
            changed=[{'target': 'hdc', 'source': str(tmp_path / 'a.iso')}],
            vm_active=False)

        assert manager.change_media.call_args.kwargs['live'] is False


class TestDomainActivity:

    @pytest.mark.parametrize('state,expected', [
        ('running', True),
        ('paused', True),
        ('in shutdown', True),
        ('pmsuspended', True),
        ('shut off', False),
        ('shutoff', False),
    ])
    def test_known_states(self, state, expected):
        from boxman.providers.libvirt.vm_differ import VMStateDiffer

        assert VMStateDiffer.domain_is_active(state) is expected

    def test_a_paused_guest_is_active_not_merely_running(self):
        """
        vm_running was `state == 'running'`. A paused guest would have had
        only its persistent config edited, reported success, and still shown
        the old media on resume (#164 FB-5).
        """
        from boxman.providers.libvirt.vm_differ import VMStateDiffer

        assert VMStateDiffer.domain_is_active('paused') is True

    @pytest.mark.parametrize('state', ['unknown', '', 'something else'])
    def test_an_unknown_state_is_refused(self, state):
        """'unknown' is what get_vm_state() returns when domstate failed."""
        from boxman.providers.libvirt.vm_differ import VMStateDiffer

        with pytest.raises(ProvisionError, match='cannot tell whether'):
            VMStateDiffer.domain_is_active(state)


class TestUpdateResolvesMediaBeforeDiffing:
    """
    The wiring, not just the helper.

    Every other test in this file drives ``_normalize_cdroms_for_update``
    directly, which says nothing about whether ``update()`` calls it. It did
    not: ``_resolve_iso_config()`` lives inside ``_clone_and_configure_new_vms``,
    so an update where no VM was new resolved nothing at all (#164 FB-5).
    """

    URI = 'https://example.invalid/ubuntu-noble-live.iso'

    def _manager(self, tmp_path, cdroms):
        mgr = _iso_manager(tmp_path, {'live': {'uri': self.URI}}, cdroms)
        for name in ('_update_sessions_with_runtime', 'ensure_shared_bridges',
                     'report_network_results', 'raise_on_network_failures',
                     'setup_ssh_access', 'connect_info'):
            setattr(mgr, name, MagicMock())
        mgr.reconcile_networks = MagicMock(return_value={})
        mgr._find_all_existing_project_vms = MagicMock(return_value=[VM1])
        return mgr

    @staticmethod
    def _arm(mgr):
        """Capture what update() dispatches, without running any worker."""
        captured: dict = {'tasks': []}

        def _capture(tasks, op_label='parallel task'):
            captured['tasks'] = list(tasks)
            for _label, _target, args in tasks:
                captured['vm_info'] = args[3]
            return {}, {}

        mgr._run_parallel = MagicMock(side_effect=_capture)
        return captured

    @staticmethod
    def _cli():
        return MagicMock(dry_run=False, yes=True, recreate_networks=False)

    def test_the_differ_receives_a_resolved_source(self, tmp_path):
        mgr = self._manager(tmp_path, [{'name': 'live'}])
        _cache_the_iso(mgr, tmp_path, 'live', self.URI)
        captured = self._arm(mgr)

        mgr.update(self._cli())

        vm_info = captured.get('vm_info')
        assert vm_info is not None, 'no VM was dispatched for update'
        assert vm_info['cdroms'][0].get('source'), (
            'the differ was handed an unresolved cdrom entry, which it maps '
            'to the working directory and reports as a removal')

    def test_an_unresolvable_iso_fails_its_vm_without_touching_media(
            self, tmp_path):
        """
        A cache miss must not dispatch the VM into the differ — where the
        unresolved entry would read as "detach what is attached" — and must
        still reach the exit code as an aggregated failure.
        """
        mgr = self._manager(tmp_path, [{'name': 'live'}])
        captured = self._arm(mgr)

        with patch.object(type(mgr), '_resolve_isos',
                          side_effect=RuntimeError('404 not found')), \
             pytest.raises(ProvisionError, match='update finished with 1'):
            mgr.update(self._cli())

        assert captured['tasks'] == [], (
            'the VM was dispatched into the differ with unresolved media')
        errors = [c.args[0] for c in mgr.logger.error.call_args_list if c.args]
        assert any('could not resolve declared media' in e for e in errors)


class TestCdromTargetReservation:
    """
    Explicit targets are reserved before any matching happens.

    Doing it inside one loop made the result depend on declaration order: a
    targetless entry could match the very drive a later explicit entry was
    about to overwrite (#164 FB-5).
    """

    ACTUAL = [{'target': 'hdc', 'source': '/isos/a.iso'},
              {'target': 'hdd', 'source': '/isos/b.iso'}]

    def test_a_targetless_entry_does_not_lose_its_media_to_a_later_one(self):
        """
        Declared: a.iso anywhere, and b.iso specifically on hdc.

        hdc became b.iso and hdd was removed, so a.iso — still declared —
        ended up attached nowhere, and the update reported success.
        """
        diff = _diff_cdroms(
            desired_cdroms=[{'source': '/isos/a.iso'},
                            {'target': 'hdc', 'source': '/isos/b.iso'}],
            actual_cdroms=self.ACTUAL)

        assert [c['source'] for c in diff['new_cdroms']] == ['/isos/a.iso']
        assert diff['changed_cdroms'] == [{'target': 'hdc',
                                           'source': '/isos/b.iso'}]

    def test_the_same_holds_with_the_entries_reversed(self):
        """Reservation happens up front, so declaration order cannot matter."""
        diff = _diff_cdroms(
            desired_cdroms=[{'target': 'hdc', 'source': '/isos/b.iso'},
                            {'source': '/isos/a.iso'}],
            actual_cdroms=self.ACTUAL)

        assert [c['source'] for c in diff['new_cdroms']] == ['/isos/a.iso']
        assert diff['changed_cdroms'] == [{'target': 'hdc',
                                           'source': '/isos/b.iso'}]

    def test_targetless_entries_match_one_to_one(self):
        """Two declarations of the same ISO need two drives, not one twice."""
        diff = _diff_cdroms(
            desired_cdroms=[{'source': '/isos/a.iso'},
                            {'source': '/isos/a.iso'}],
            actual_cdroms=[{'target': 'hdc', 'source': '/isos/a.iso'}])

        assert len(diff['new_cdroms']) == 1
        assert diff['removed_cdroms'] == []


class TestLegacySaveIsConsumed:
    """
    Once libvirt has read an external save file it is stale: the guest may
    already have written to its disks. Leaving one where a later restore can
    find it lets that image be replayed against those changes (#164 FB-3).

    The guarantee is the file's *absence from the restore path*, not the
    success of a deletion — which is why it is moved aside before being
    applied rather than deleted afterwards.
    """

    def _session(self):
        from boxman.providers.libvirt.session import LibVirtSession
        session = LibVirtSession(config={'provider': {'libvirt': {}}})
        session.logger = MagicMock()
        return session

    def _restore(self, tmp_path, fake, **patches):
        with patch('boxman.providers.libvirt.session.VirshCommand',
                   return_value=fake):
            session = self._session()
            for target, value in patches.items():
                setattr(session, target, value)
            return session, session.restore_vm(VM1, str(tmp_path),
                                               allow_legacy=True)

    @staticmethod
    def _restored_paths(fake):
        return [c[1] for c in fake.calls if c[0] == 'restore']

    def test_the_image_is_moved_aside_before_it_is_applied(self, tmp_path):
        save = tmp_path / f'{VM1}.save'
        save.write_bytes(b'memory')
        fake = _FakeLibvirt(state='shut off', managed_saved=False)

        _session, ok = self._restore(tmp_path, fake)

        assert ok is True
        # restored from the quarantined name, never from the original
        assert self._restored_paths(fake) != [str(save)]
        assert not save.exists()

    def test_a_restore_that_cannot_move_the_image_aside_is_refused(
            self, tmp_path):
        """
        Applying an image that cannot be taken out of the restore path first
        risks leaving it there for a second, corrupting restore.
        """
        save = tmp_path / f'{VM1}.save'
        save.write_bytes(b'memory')
        fake = _FakeLibvirt(state='shut off', managed_saved=False)

        with patch('boxman.providers.libvirt.session.os.rename',
                   side_effect=OSError('read-only filesystem')):
            _session, ok = self._restore(tmp_path, fake)

        assert ok is False
        assert self._restored_paths(fake) == []
        assert save.exists()

    def test_the_image_is_gone_even_when_the_guest_does_not_come_up(
            self, tmp_path):
        """A failed confirmation does not un-apply the image."""
        save = tmp_path / f'{VM1}.save'
        save.write_bytes(b'memory')
        fake = _FakeLibvirt(state='shut off', managed_saved=False)

        _session, ok = self._restore(
            tmp_path, fake, _confirm_running=MagicMock(return_value=False))

        assert ok is False
        assert not save.exists()

    def test_a_failed_restore_keeps_the_image_quarantined(self, tmp_path):
        """
        A failed restore does not prove the image was never applied: libvirt
        resumes the guest's CPUs before it finishes writing runtime status,
        and stops the guest again if that write fails — by which point the
        disks may already have changed. Returning the image to its replayable
        name would offer it to the next restore as if nothing had happened
        (#164 FB-3).
        """
        save = tmp_path / f'{VM1}.save'
        save.write_bytes(b'memory')
        fake = _FakeLibvirt(state='shut off', managed_saved=False,
                            restore_ok=False)

        session, ok = self._restore(tmp_path, fake)

        assert ok is False
        assert not save.exists()
        kept = list(tmp_path.glob(f'{VM1}.save.restoring-*'))
        assert len(kept) == 1
        errors = [c.args[0] for c in session.logger.error.call_args_list
                  if c.args]
        assert any(str(kept[0]) in e for e in errors)

    def test_a_failed_restore_is_not_silently_retried(self, tmp_path):
        """The quarantined image must not be found by a later restore."""
        save = tmp_path / f'{VM1}.save'
        save.write_bytes(b'memory')
        first = _FakeLibvirt(state='shut off', managed_saved=False,
                             restore_ok=False)
        self._restore(tmp_path, first)

        second = _FakeLibvirt(state='shut off', managed_saved=False)
        _session, ok = self._restore(tmp_path, second)

        assert ok is False
        assert self._restored_paths(second) == []

    def test_a_second_restore_finds_nothing_to_replay(self, tmp_path):
        """
        The whole point. After the guest has been restored, written to its
        disks and shut down, a fresh session must not apply that image again.
        """
        save = tmp_path / f'{VM1}.save'
        save.write_bytes(b'memory')

        first = _FakeLibvirt(state='shut off', managed_saved=False)
        _session, ok = self._restore(tmp_path, first)
        assert ok is True

        second = _FakeLibvirt(state='shut off', managed_saved=False)
        _session2, ok2 = self._restore(tmp_path, second)

        assert ok2 is False
        assert self._restored_paths(second) == []

    def test_an_undeletable_image_still_cannot_be_replayed(self, tmp_path):
        """
        Deletion failing is only a space problem: the file is already out of
        the path restore_vm looks at, so a second restore finds nothing.
        """
        save = tmp_path / f'{VM1}.save'
        save.write_bytes(b'memory')
        fake = _FakeLibvirt(state='shut off', managed_saved=False)

        with patch('boxman.providers.libvirt.session.os.remove',
                   side_effect=OSError('read-only filesystem')):
            _session, ok = self._restore(tmp_path, fake)

        assert ok is True
        assert not save.exists()

        second = _FakeLibvirt(state='shut off', managed_saved=False)
        _session2, ok2 = self._restore(tmp_path, second)
        assert ok2 is False
        assert self._restored_paths(second) == []


class TestCreationTimeResolutionIsScoped:
    """
    Resolving every declared ISO for every VM meant an update adding one new
    VM would also fetch media used only by guests already running — and with
    caching disabled the downloader writes straight to the cache path,
    truncating an ISO an existing guest has open (#164 FB-5).
    """

    def _manager(self, tmp_path):
        cfg = {
            'project': 'proj',
            'isos': {'a': {'uri': 'https://x.invalid/a.iso'},
                     'b': {'uri': 'https://x.invalid/b.iso'}},
            'clusters': {
                'web': {
                    'workdir': '/tmp/wd',
                    'vms': {'node01': {'cdroms': [{'name': 'a'}]},
                            'node02': {'cdroms': [{'name': 'b'}]}},
                },
            },
        }
        mgr = make_bare_manager(cfg)
        mgr.app_config = {'cache': {'enabled': True,
                                    'cache_dir': str(tmp_path)}}
        return mgr

    def test_only_the_named_vms_media_is_resolved(self, tmp_path):
        mgr = self._manager(tmp_path)

        with patch.object(type(mgr), '_resolve_isos',
                          return_value={'a': '/isos/a.iso',
                                        'b': '/isos/b.iso'}) as resolve:
            mgr._resolve_iso_config({VM1})

        resolve.assert_called_once_with({'a'})

    def test_no_names_means_every_vm(self, tmp_path):
        """The full-provision path still resolves everything."""
        mgr = self._manager(tmp_path)

        with patch.object(type(mgr), '_resolve_isos',
                          return_value={'a': '/isos/a.iso',
                                        'b': '/isos/b.iso'}) as resolve:
            mgr._resolve_iso_config()

        resolve.assert_called_once_with({'a', 'b'})

    def test_media_shared_with_an_existing_vm_survives_a_failed_download(
            self, tmp_path):
        """
        Scoping protects media referenced *only* by existing VMs. An ISO
        referenced by both a new VM and a running one is still selected, and
        the downloader truncates on open and deletes on failure — so the
        running guest's media has to be protected by never fetching over a
        file that is present, whatever the cache setting (#164 FB-5).
        """
        cfg = {
            'project': 'proj',
            'isos': {'shared': {'uri': 'https://x.invalid/shared.iso'}},
            'clusters': {
                'web': {
                    'workdir': '/tmp/wd',
                    'vms': {'node01': {'cdroms': [{'name': 'shared'}]},
                            'node02': {'cdroms': [{'name': 'shared'}]}},
                },
            },
        }
        mgr = make_bare_manager(cfg)
        # caching disabled: the path the old code downloaded straight over
        mgr.app_config = {'cache': {'enabled': False,
                                    'cache_dir': str(tmp_path)}}
        attached = tmp_path / mgr._iso_cache_filename(
            'shared', 'https://x.invalid/shared.iso')
        attached.write_bytes(b'the running guest is booted from this')

        # node02 is new; node01 is already running with this ISO attached
        with patch.object(type(mgr), '_download_iso',
                          side_effect=AssertionError('must not download')):
            mgr._resolve_iso_config({VM2})

        assert attached.read_bytes() == b'the running guest is booted from this'

    def test_the_clone_path_passes_only_the_new_vms(self, tmp_path):
        """
        The wiring, not the helper: calling _resolve_iso_config() unscoped
        from the clone path is exactly the bug, and asserting on the helper
        alone does not catch it.
        """
        mgr = self._manager(tmp_path)
        mgr._ensure_libvirt_storage_pool = MagicMock()
        mgr._run_parallel = MagicMock(return_value=({}, {}))
        mgr.wait_for_vm_ips = MagicMock()
        mgr.provider = MagicMock()
        mgr._resolve_iso_config = MagicMock()

        mgr._clone_and_configure_new_vms({VM1})

        mgr._resolve_iso_config.assert_called_once_with({VM1})


class TestDryRunDoesNotFetch:

    URI = 'https://example.invalid/ubuntu-noble-live.iso'

    def test_update_dry_run_never_downloads(self, tmp_path):
        """
        The wiring for allow_fetch. Driving _normalize_cdroms_for_update with
        allow_fetch=False directly says nothing about whether update() passes
        it.
        """
        mgr = _iso_manager(tmp_path, {'live': {'uri': self.URI}},
                           [{'name': 'live'}])
        for name in ('_update_sessions_with_runtime', 'ensure_shared_bridges',
                     'report_network_results', 'raise_on_network_failures',
                     'setup_ssh_access', 'connect_info'):
            setattr(mgr, name, MagicMock())
        mgr.reconcile_networks = MagicMock(return_value={})
        mgr._find_all_existing_project_vms = MagicMock(return_value=[VM1])
        mgr._run_parallel = MagicMock(return_value=({}, {}))

        with patch.object(type(mgr), '_resolve_isos',
                          side_effect=AssertionError('must not download')):
            with contextlib.suppress(ProvisionError):
                mgr.update(MagicMock(dry_run=True, yes=True,
                                     recreate_networks=False))
