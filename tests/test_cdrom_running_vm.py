"""
Adding or removing a CD-ROM drive on a running VM (#222).

libvirt cannot hot-plug or hot-unplug an IDE or SATA drive, the only buses
boxman gives a cdrom. ``attach-device --persistent`` and ``detach-device
--persistent`` against an active domain are refused whole, so ``boxman
update`` exited 2 with "CDROM update failed". On the test runner (libvirt
10.0, QEMU 8.2), for a running or a paused domain::

    error: Operation not supported: disk bus 'sata' cannot be hotplugged.
    error: Operation not supported: disk device type 'cdrom' cannot be detached

(``disk bus 'ide'`` on i440fx.) A drive can still be added to or removed
from the persistent definition alone (``--config``), and the guest sees the
change at its next boot: ``update`` reports it as a pending restart, and
``update --restart`` applies it.

The reconcile compares against the persistent definition, so the next
``update`` does not propose that drive again. Read from the live domain it
was new every time, and libvirt refused the duplicate ("target sdb already
exists").

Only virsh is faked, by :class:`FakeDomain`, which answers the way libvirt
10.0 answered on the runner.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

import pytest

from boxman.providers.libvirt.commands import VirshCommand
from boxman.providers.libvirt.session import LibVirtSession
from boxman.providers.libvirt.vm_differ import VMStateDiffer
from conftest import make_bare_manager

pytestmark = pytest.mark.unit

Q35 = 'pc-q35-noble'
I440FX = 'pc-i440fx-noble'

VM = 'bprj__demo__bprj_c1_vm01'
BOOT = '/wd/c1/vm01.qcow2'


class Result:
    """What ``VirshCommand.execute`` returns."""

    def __init__(self, stdout: str = '', stderr: str = '', code: int = 0):
        self.stdout, self.stderr, self.return_code = stdout, stderr, code
        self.ok = code == 0
        self.failed = not self.ok


def _refused(action: str, xml_path: str, reason: str) -> Result:
    return Result(code=1, stderr=(
        f"error: Failed to {action} device from {xml_path}\nerror: {reason}\n"))


class FakeDomain:
    """
    One libvirt domain as ``virsh`` shows it: a live definition while the
    domain is active, and the persistent one it starts from.

    The answers are libvirt 10.0's on the runner, captured with the device
    XML boxman generates against throwaway q35 and i440fx domains:

    * ``attach-device``: ``--config`` adds the drive to the persistent
      definition only, and is refused for a target it holds already; any
      live scope on an active domain is refused for an IDE or SATA cdrom,
      and nothing changes.
    * ``detach-device``: ``--config`` removes the drive from the persistent
      definition only; any live scope on an active domain is refused for an
      IDE or SATA cdrom, and nothing changes.
    * ``change-media``: with ``--config`` virsh looks the drive up in the
      persistent definition, otherwise in the live one; ``--live`` fails
      for a drive the live definition lacks.
    * a start boots the persistent definition, which is the live one from
      then on.

    A call it does not model fails the test. Every call is recorded in
    :attr:`calls`.
    """

    def __init__(self, machine: str = Q35, state: str = 'running',
                 cdroms: dict[str, str | None] | None = None,
                 live_cdroms: dict[str, str | None] | None = None):
        self.machine = machine
        self.state = state
        #: target -> (device, bus, source); source None for an empty drive
        self.persistent = self._devices(cdroms or {})
        self.live = (dict(self.persistent) if live_cdroms is None
                     else self._devices(live_cdroms))
        self.calls: list[tuple] = []

    def _devices(self, cdroms):
        bus = 'sata' if self.machine == Q35 else 'ide'
        return {'vda': ('disk', 'virtio', BOOT),
                **{t: ('cdrom', bus, s) for t, s in cdroms.items()}}

    @property
    def active(self) -> bool:
        return self.state in ('running', 'paused')

    def view(self, inactive: bool = False) -> dict:
        """The definition ``virsh`` reports: without ``--inactive``, the
        live one of an active domain; otherwise the persistent one."""
        return self.live if (self.active and not inactive) else self.persistent

    def cdroms(self, inactive: bool = False) -> dict[str, str | None]:
        return {t: s for t, (device, _bus, s) in self.view(inactive).items()
                if device == 'cdrom'}

    def restart(self) -> None:
        """A cold boot: the persistent definition becomes the live one."""
        self.state = 'running'
        self.live = dict(self.persistent)

    def calls_of(self, command: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == command]

    # --- virsh ----------------------------------------------------------

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        handler = getattr(self, '_' + args[0].replace('-', '_'), None)
        if handler is None or args[1] != VM:
            raise AssertionError(f'virsh call not modelled: {args!r}')
        return handler(*args[2:], **kwargs)

    def _scope(self, flags) -> set:
        """What a device command's flags reach (virsh's --persistent,
        --config, --live, or none for the current state)."""
        if '--persistent' in flags:
            return {'config', 'live'} if self.active else {'config'}
        scope = {name for name in ('config', 'live') if f'--{name}' in flags}
        if not scope:
            scope = {'live'} if self.active else {'config'}
        if 'live' in scope and not self.active:
            raise AssertionError('--live on an inactive domain: not modelled')
        return scope

    def _domstate(self, **_kwargs):
        return Result(stdout=f'{self.state}\n\n')

    def _domblklist(self, *flags, **_kwargs):
        assert '--details' in flags
        rows = ''.join(
            f' file   {device:<8} {target:<8} {source or "-"}\n'
            for target, (device, _bus, source) in
            self.view('--inactive' in flags).items())
        return Result(stdout=(' Type   Device   Target   Source\n'
                              + '-' * 60 + '\n' + rows + '\n'))

    def _dumpxml(self, *flags, **_kwargs):
        return Result(stdout=(
            f"<domain type='kvm'><name>{VM}</name>"
            "<memory unit='KiB'>1048576</memory>"
            "<currentMemory unit='KiB'>1048576</currentMemory>"
            "<vcpu placement='static'>1</vcpu>"
            f"<os><type arch='x86_64' machine='{self.machine}'>hvm</type></os>"
            "<devices><memballoon model='virtio'/></devices></domain>"))

    def _domblkinfo(self, target, **_kwargs):
        return Result(stdout=(
            'Capacity:       10737418240\nAllocation:     1048576\n'
            'Physical:       1048576\n'))

    def _metadata(self, **_kwargs):
        return Result(code=1, stderr=(
            'error: metadata not found: Requested metadata element is not '
            'present\n'))

    def _attach_device(self, xml_path, *flags, **_kwargs):
        disk = ET.parse(xml_path).getroot()
        target = disk.find('target').get('dev')
        bus = disk.find('target').get('bus')
        source = disk.find('source').get('file')
        scope = self._scope(flags)
        if 'config' in scope and target in self.persistent:
            return _refused('attach', xml_path, (
                f'Requested operation is not valid: target {target} '
                f'already exists'))
        if 'live' in scope:
            if bus in ('ide', 'sata'):
                return _refused('attach', xml_path, (
                    f"Operation not supported: disk bus '{bus}' cannot be "
                    f"hotplugged."))
            raise AssertionError(f'a live attach on {bus}: not modelled')
        self.persistent[target] = ('cdrom', bus, source)
        return Result(stdout='Device attached successfully\n\n')

    def _detach_device(self, xml_path, *flags, **_kwargs):
        target = ET.parse(xml_path).getroot().find('target').get('dev')
        scope = self._scope(flags)
        if 'config' in scope and target not in self.persistent:
            raise AssertionError('detaching an absent drive: not modelled')
        if 'live' in scope:
            device, bus, _source = self.live.get(target, (None, None, None))
            if device == 'cdrom' and bus in ('ide', 'sata'):
                return _refused('detach', xml_path, (
                    "Operation not supported: disk device type 'cdrom' "
                    "cannot be detached"))
            raise AssertionError(f'a live detach of {target}: not modelled')
        del self.persistent[target]
        return Result(stdout='Device detached successfully\n\n')

    def _change_media(self, target, source, *flags, **_kwargs):
        if '--live' in flags and not self.active:
            raise AssertionError('--live on an inactive domain: not modelled')
        live = '--live' in flags or (
            '--config' not in flags and self.active)
        config = '--config' in flags or not self.active
        looked_up = self.persistent if '--config' in flags else self.view()
        if looked_up.get(target, ('',))[0] != 'cdrom':
            return Result(code=1, stderr=(
                f'error: No disk found whose source path or target is '
                f'{target}\n'))
        if live and target not in self.live:
            return Result(code=1, stderr=(
                'error: Failed to complete action update on media\n'
                f"error: internal error: disk '{target}' not found\n"))
        if config:
            device, bus, _old = self.persistent[target]
            self.persistent[target] = (device, bus, source)
        if live:
            device, bus, _old = self.live[target]
            self.live[target] = (device, bus, source)
        return Result(stdout='Successfully updated media.\n')

    def _shutdown(self, **_kwargs):
        self.state = 'shut off'
        return Result(stdout=f"Domain '{VM}' is being shutdown\n\n")

    def _start(self, **_kwargs):
        self.restart()
        return Result(stdout=f"Domain '{VM}' started\n\n")


@pytest.fixture
def isos(tmp_path: Path) -> dict[str, str]:
    paths = {}
    for name in ('tools', 'data'):
        iso = tmp_path / f'{name}.iso'
        iso.write_bytes(b'iso')
        paths[name] = str(iso)
    return paths


def _session() -> LibVirtSession:
    return LibVirtSession(config={'provider': {'libvirt': {'use_sudo': False}}})


def _virsh(domain: FakeDomain):
    """Route every virsh call boxman makes to *domain*."""
    return patch.object(VirshCommand, 'execute', side_effect=domain)


def _apply(domain: FakeDomain, new=(), removed=(), changed=()) -> bool:
    with _virsh(domain):
        return _session().update_vm_cdroms(
            vm_name=VM, new_cdroms=list(new), removed_cdroms=list(removed),
            changed_cdroms=list(changed), vm_active=domain.active)


def _diff(domain: FakeDomain, desired: list[dict]) -> dict:
    with _virsh(domain):
        return VMStateDiffer(
            provider_config={'use_sudo': False}).diff_vm(
                domain_name=VM, desired_cpus=None, desired_memory_mb=None,
                desired_disks=None, workdir='/wd/c1', disk_prefix=VM,
                desired_cdroms=desired)


def _reconcile(domain: FakeDomain, desired: list[dict]) -> tuple[dict, bool]:
    """One update's cdrom half: diff, then apply what the diff asks for."""
    diff = _diff(domain, desired)
    ok = _apply(domain, diff['new_cdroms'], diff['removed_cdroms'],
                diff['changed_cdroms'])
    return diff, ok


def _flags(call: tuple) -> set:
    return {arg for arg in call if str(arg).startswith('--')}


# ---------------------------------------------------------------------------
# Adding a drive
# ---------------------------------------------------------------------------

class TestANewDriveOnARunningVM:
    """What exited 2: libvirt refuses to hot-plug the drive."""

    @pytest.mark.parametrize('machine,target', [(Q35, 'sdb'), (I440FX, 'hdb')],
                             ids=['q35-sata', 'i440fx-ide'])
    @pytest.mark.parametrize('state', ['running', 'paused'])
    def test_it_goes_into_the_persistent_definition(
            self, isos, machine, target, state):
        # the template's empty seed drive holds sda (hda on i440fx)
        domain = FakeDomain(machine=machine, state=state,
                            cdroms={target[:2] + 'a': None})

        ok = _apply(domain, new=[{'name': 'tools', 'source': isos['tools']}])

        assert ok is True, 'the refused hot-plug failed the update'
        assert domain.cdroms(inactive=True)[target] == isos['tools']
        assert target not in domain.cdroms(), (
            'a drive libvirt cannot hot-plug is in the live definition')
        [attach] = domain.calls_of('attach-device')
        assert _flags(attach) == {'--config'}

    def test_one_warning_names_the_vm_and_the_target(self, isos, captured_logs):
        domain = FakeDomain(cdroms={'sda': None})

        _apply(domain, new=[{'name': 'tools', 'source': isos['tools']}])

        notices = [r.getMessage() for r in captured_logs.records
                   if r.levelno == logging.WARNING]
        assert len(notices) == 1, notices
        assert VM in notices[0] and 'sdb' in notices[0]
        assert 'next boot' in notices[0]

    def test_a_stopped_vm_is_unchanged(self, isos):
        domain = FakeDomain(state='shut off', cdroms={'sda': None})

        assert _apply(domain, new=[{'name': 'tools',
                                    'source': isos['tools']}]) is True

        [attach] = domain.calls_of('attach-device')
        assert _flags(attach) == {'--persistent'}
        assert domain.cdroms(inactive=True)['sdb'] == isos['tools']

    def test_the_target_of_a_pending_drive_is_not_handed_out_again(self, isos):
        """#217's _find_next_available_target reads both definitions."""
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']},
                            live_cdroms={'sda': None})

        assert _apply(domain, new=[{'name': 'data',
                                    'source': isos['data']}]) is True

        assert domain.cdroms(inactive=True) == {
            'sda': None, 'sdb': isos['tools'], 'sdc': isos['data']}


class TestTheNextUpdate:
    """
    The drive waits in the persistent definition for a boot: the state the
    first update leaves (see TestUpdateEndToEnd for the whole sequence).
    """

    @staticmethod
    def _deferred(isos) -> tuple[FakeDomain, list[dict]]:
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']},
                            live_cdroms={'sda': None})
        return domain, [{'name': 'tools', 'source': isos['tools']}]

    def test_it_does_not_attach_the_drive_again(self, isos):
        domain, desired = self._deferred(isos)

        diff, ok = _reconcile(domain, desired)

        assert diff['new_cdroms'] == [], (
            'proposed a drive the persistent definition already holds')
        assert diff['removed_cdroms'] == []
        assert diff['changed_cdroms'] == []
        assert ok is True
        assert domain.calls_of('attach-device') == []
        assert domain.cdroms(inactive=True) == {'sda': None,
                                                'sdb': isos['tools']}

    def test_it_is_still_reported_as_waiting_for_a_boot(self, isos):
        domain, desired = self._deferred(isos)

        diff = _diff(domain, desired)

        assert diff['cdroms_restart_pending'] is True
        assert diff['cdroms_pending_targets'] == ['sdb']

    def test_after_the_boot_nothing_is_pending(self, isos):
        domain, desired = self._deferred(isos)
        domain.restart()

        diff = _diff(domain, desired)

        assert domain.cdroms()['sdb'] == isos['tools']
        assert diff['cdroms_restart_pending'] is False
        assert (diff['new_cdroms'], diff['removed_cdroms'],
                diff['changed_cdroms']) == ([], [], [])

    def test_a_stopped_vm_has_nothing_pending(self, isos):
        domain = FakeDomain(state='shut off',
                            cdroms={'sda': None, 'sdb': isos['tools']})

        diff = _diff(domain, [{'name': 'tools', 'source': isos['tools']}])

        assert diff['cdroms_restart_pending'] is False
        assert diff['new_cdroms'] == []


# ---------------------------------------------------------------------------
# Removing a drive
# ---------------------------------------------------------------------------

class TestRemovingADriveFromARunningVM:
    """``detach-device --persistent`` is refused just the same (#222)."""

    @pytest.mark.parametrize('machine,target', [(Q35, 'sdb'), (I440FX, 'hdb')],
                             ids=['q35-sata', 'i440fx-ide'])
    def test_it_leaves_the_persistent_definition(self, isos, machine, target):
        seed = target[:2] + 'a'
        domain = FakeDomain(machine=machine,
                            cdroms={seed: None, target: isos['tools']})

        diff, ok = _reconcile(domain, [])

        assert [c['target'] for c in diff['removed_cdroms']] == [target]
        assert ok is True, 'the refused hot-unplug failed the update'
        assert target not in domain.cdroms(inactive=True)
        assert domain.cdroms()[target] == isos['tools'], (
            'the running guest keeps the drive until its next boot')
        [detach] = domain.calls_of('detach-device')
        assert _flags(detach) == {'--config'}

    def test_one_warning_names_the_vm_and_the_target(self, isos, captured_logs):
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']})

        _apply(domain, removed=[{'target': 'sdb', 'source': isos['tools']}])

        notices = [r.getMessage() for r in captured_logs.records
                   if r.levelno == logging.WARNING]
        assert len(notices) == 1, notices
        assert VM in notices[0] and 'sdb' in notices[0]
        assert 'next boot' in notices[0]

    def test_the_next_update_does_not_detach_it_again(self, isos):
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']})
        _reconcile(domain, [])
        domain.calls.clear()

        diff, ok = _reconcile(domain, [])

        assert diff['removed_cdroms'] == [], (
            'a drive only the live definition holds is removed already')
        assert ok is True
        assert domain.calls_of('detach-device') == []
        assert diff['cdroms_restart_pending'] is True
        assert diff['cdroms_pending_targets'] == ['sdb']

    def test_a_stopped_vm_is_unchanged(self, isos):
        domain = FakeDomain(state='shut off',
                            cdroms={'sda': None, 'sdb': isos['tools']})

        diff, ok = _reconcile(domain, [])

        assert ok is True
        [detach] = domain.calls_of('detach-device')
        assert _flags(detach) == {'--persistent'}
        assert 'sdb' not in domain.cdroms(inactive=True)
        assert diff['cdroms_restart_pending'] is False


class TestCancellingAPendingDrive:
    """Declared, deferred, then dropped from the config before the boot."""

    def test_it_is_removed_from_the_persistent_definition(self, isos):
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']},
                            live_cdroms={'sda': None})

        diff, ok = _reconcile(domain, [])

        assert [c['target'] for c in diff['removed_cdroms']] == ['sdb'], (
            'a pending drive dropped from the config stayed configured')
        assert ok is True
        assert domain.cdroms(inactive=True) == domain.cdroms() == {'sda': None}

    def test_then_nothing_is_pending(self, isos):
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']},
                            live_cdroms={'sda': None})
        _reconcile(domain, [])

        with _virsh(domain):
            assert _session().cdroms_pending(VM) is False

    def test_no_drive_is_announced_for_the_next_boot(self, isos, captured_logs):
        """The guest never had it, so nothing waits for a boot."""
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']},
                            live_cdroms={'sda': None})

        _diff_first, ok = _reconcile(domain, [])

        assert ok is True
        [detach] = domain.calls_of('detach-device')
        assert _flags(detach) == {'--config'}
        assert [r.getMessage() for r in captured_logs.records
                if r.levelno == logging.WARNING] == []


class TestAPendingRemovalIsRedeclared:
    """A drive only the live definition holds, declared again."""

    def test_at_its_own_target_it_is_configured_there_again(self, isos):
        domain = FakeDomain(cdroms={'sda': None},
                            live_cdroms={'sda': None, 'sdb': isos['tools']})

        diff, ok = _reconcile(
            domain, [{'name': 'tools', 'source': isos['tools'],
                      'target': 'sdb'}])

        assert [c['name'] for c in diff['new_cdroms']] == ['tools']
        assert ok is True
        assert domain.cdroms(inactive=True) == domain.cdroms()
        with _virsh(domain):
            assert _session().cdroms_pending(VM) is False

    def test_with_other_media_it_waits_for_the_boot(self, isos):
        """Configured with the new media; the guest still holds the old."""
        domain = FakeDomain(cdroms={'sda': None},
                            live_cdroms={'sda': None, 'sdb': isos['tools']})

        _diff_first, ok = _reconcile(
            domain, [{'name': 'data', 'source': isos['data'],
                      'target': 'sdb'}])

        assert ok is True
        assert domain.cdroms(inactive=True)['sdb'] == isos['data']
        with _virsh(domain):
            assert _session().cdroms_pending(VM) is True


# ---------------------------------------------------------------------------
# Changing the media, which libvirt does live
# ---------------------------------------------------------------------------

class TestMediaChanges:

    def test_a_running_vm_gets_it_live_and_in_the_config(self, isos):
        """Unchanged: change-media --live --config."""
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']})

        diff, ok = _reconcile(
            domain, [{'name': 'data', 'source': isos['data'], 'target': 'sdb'}])

        assert diff['changed_cdroms'] == [{'target': 'sdb',
                                           'source': isos['data']}]
        assert ok is True
        [change] = domain.calls_of('change-media')
        assert _flags(change) == {'--live', '--config'}
        assert domain.cdroms()['sdb'] == domain.cdroms(inactive=True)['sdb'] \
            == isos['data']

    @pytest.mark.parametrize('declared', ['explicit', 'targetless'])
    def test_live_media_other_than_the_config_is_brought_along(
            self, isos, declared):
        """The persistent definition has the declared ISO, the running
        guest another one: a live media change, not a restart."""
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['data']},
                            live_cdroms={'sda': None, 'sdb': isos['tools']})
        entry = {'name': 'data', 'source': isos['data']}
        if declared == 'explicit':
            entry['target'] = 'sdb'

        diff, ok = _reconcile(domain, [entry])

        assert diff['changed_cdroms'] == [{'target': 'sdb',
                                           'source': isos['data']}]
        assert diff['new_cdroms'] == [] and diff['removed_cdroms'] == []
        assert ok is True
        assert domain.cdroms()['sdb'] == isos['data']
        with _virsh(domain):
            assert _session().cdroms_pending(VM) is False

    def test_a_pending_drive_gets_it_in_the_config_only(self, isos):
        """The drive is not in the live definition, where ``--live`` fails
        ("disk 'sdb' not found")."""
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']},
                            live_cdroms={'sda': None})

        diff, ok = _reconcile(
            domain, [{'name': 'data', 'source': isos['data'], 'target': 'sdb'}])

        assert [c['target'] for c in diff['changed_cdroms']] == ['sdb']
        assert ok is True
        [change] = domain.calls_of('change-media')
        assert _flags(change) == {'--config'}
        assert domain.cdroms(inactive=True)['sdb'] == isos['data']
        assert 'sdb' not in domain.cdroms()

    def test_a_stopped_vm_is_unchanged(self, isos):
        domain = FakeDomain(state='shut off',
                            cdroms={'sda': None, 'sdb': isos['tools']})

        _diff_first, ok = _reconcile(
            domain, [{'name': 'data', 'source': isos['data'], 'target': 'sdb'}])

        assert ok is True
        [change] = domain.calls_of('change-media')
        assert _flags(change) == {'--config'}
        assert domain.cdroms(inactive=True)['sdb'] == isos['data']


# ---------------------------------------------------------------------------
# After the changes: is a boot still owed?
# ---------------------------------------------------------------------------

class TestPendingIsAskedAfterTheChanges:

    @pytest.mark.parametrize('persistent,live,pending', [
        ({'sda': None, 'sdb': '/i/tools.iso'}, {'sda': None}, True),
        ({'sda': None}, {'sda': None, 'sdb': '/i/tools.iso'}, True),
        ({'sda': None, 'sdb': '/i/data.iso'},
         {'sda': None, 'sdb': '/i/tools.iso'}, True),
        ({'sda': None, 'sdb': '/i/tools.iso'},
         {'sda': None, 'sdb': '/i/tools.iso'}, False),
    ], ids=['added', 'removed', 'other-media', 'agreeing'])
    def test_the_two_definitions_are_compared(self, persistent, live, pending):
        domain = FakeDomain(cdroms=persistent, live_cdroms=live)

        with _virsh(domain):
            assert _session().cdroms_pending(VM) is pending


# ---------------------------------------------------------------------------
# The whole update, through the worker: only virsh is faked
# ---------------------------------------------------------------------------

class TestUpdateEndToEnd:
    """
    ``_update_single_vm`` with the real differ and the real session, so the
    needs-restart wiring is exercised, not assumed.
    """

    def _update(self, domain: FakeDomain, cdroms: list[dict],
                allow_restart: bool = False) -> dict:
        mgr = make_bare_manager({'project': 'demo'})
        mgr.provider = _session()
        queue = MagicMock()
        with _virsh(domain):
            mgr._update_single_vm('c1', {'workdir': '/wd/c1'}, 'vm01',
                                  {'cdroms': cdroms}, queue,
                                  dry_run=False, allow_restart=allow_restart)
        vm_name, result = queue.put.call_args.args[0]
        assert vm_name == 'vm01'
        return result

    def test_add_then_restart(self, isos):
        domain = FakeDomain(cdroms={'sda': None})
        declared = [{'name': 'tools', 'source': isos['tools']}]

        first = self._update(domain, declared)
        assert first['status'] == 'needs_restart', first['details']
        assert '--restart' in first['details']
        assert 'sdb' not in domain.cdroms()
        [attach] = domain.calls_of('attach-device')
        assert _flags(attach) == {'--config'}

        domain.calls.clear()
        second = self._update(domain, declared)
        assert second['status'] == 'needs_restart', second['details']
        assert 'sdb' in second['details']
        assert domain.calls_of('attach-device') == [], 'attached it again'
        assert domain.calls_of('shutdown') == []

        third = self._update(domain, declared, allow_restart=True)
        assert third['status'] == 'updated', third['details']
        assert len(domain.calls_of('shutdown')) == 1
        assert len(domain.calls_of('start')) == 1
        assert domain.cdroms()['sdb'] == isos['tools']

        assert self._update(domain, declared)['status'] == 'no_change'

    def test_remove_then_restart(self, isos):
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']})

        first = self._update(domain, [])
        assert first['status'] == 'needs_restart', first['details']
        assert domain.cdroms()['sdb'] == isos['tools']

        domain.calls.clear()
        assert self._update(domain, [])['status'] == 'needs_restart'
        assert domain.calls_of('detach-device') == [], 'detached it again'

        third = self._update(domain, [], allow_restart=True)
        assert third['status'] == 'updated', third['details']
        assert 'sdb' not in domain.cdroms()

        assert self._update(domain, [])['status'] == 'no_change'

    def test_a_media_change_on_a_running_vm_needs_no_restart(self, isos):
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']})

        result = self._update(
            domain, [{'name': 'data', 'source': isos['data'], 'target': 'sdb'}])

        assert result['status'] == 'updated', result['details']
        assert domain.calls_of('shutdown') == []

    def test_cancelling_a_pending_drive_needs_no_restart(self, isos):
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']},
                            live_cdroms={'sda': None})

        result = self._update(domain, [])

        assert result['status'] == 'updated', result['details']
        assert 'waiting' not in result['details'], (
            'reported a drive this update removed as still waiting')

    def test_redeclaring_a_drive_waiting_to_leave_needs_no_restart(self, isos):
        """Its removal is cancelled: configured again, as the guest has it."""
        domain = FakeDomain(cdroms={'sda': None},
                            live_cdroms={'sda': None, 'sdb': isos['tools']})

        result = self._update(domain, [
            {'name': 'tools', 'source': isos['tools'], 'target': 'sdb'}])

        assert result['status'] == 'updated', result['details']
        assert 'waiting' not in result['details']
        assert domain.calls_of('shutdown') == []

    def test_a_drive_left_pending_is_named_beside_new_changes(self, isos):
        domain = FakeDomain(cdroms={'sda': None, 'sdb': isos['tools']},
                            live_cdroms={'sda': None})

        result = self._update(domain, [
            {'name': 'tools', 'source': isos['tools']},
            {'name': 'data', 'source': isos['data']}])

        assert result['status'] == 'needs_restart', result['details']
        assert 'new cdroms: data' in result['details']
        assert 'cdroms waiting for a restart: sdb' in result['details']

    def test_a_paused_vm_is_never_power_cycled(self, isos):
        """Active but not running: the drive waits for a boot, and even
        --restart does not shut a paused guest down."""
        domain = FakeDomain(state='paused', cdroms={'sda': None})

        result = self._update(domain, [{'name': 'tools',
                                        'source': isos['tools']}],
                              allow_restart=True)

        assert result['status'] == 'needs_restart', result['details']
        assert domain.calls_of('shutdown') == []
        assert domain.cdroms(inactive=True)['sdb'] == isos['tools']

    def test_a_stopped_vm_is_updated_without_a_restart(self, isos):
        domain = FakeDomain(state='shut off', cdroms={'sda': None})

        result = self._update(domain, [{'name': 'tools',
                                        'source': isos['tools']}])

        assert result['status'] == 'updated', result['details']
        assert domain.cdroms(inactive=True)['sdb'] == isos['tools']
