"""
Part 5 of #164 — the lifecycle verbs must do what they promise.

FB-3: ``boxman down`` saves guest state that ``boxman up`` never restores.
FB-5: ``boxman update`` on an ISO-boot VM detaches the install ISO.

The shared theme is fail-open behaviour: a query that fails, or a value that
cannot be resolved, is currently read as a benign answer ("nothing exists",
"nothing is saved", "this path") and acted on destructively.
"""

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
