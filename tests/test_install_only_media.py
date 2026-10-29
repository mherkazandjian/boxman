r"""
Install-only media (G7 in doc/windows-guests.md).

A ``cdroms:`` entry of an ISO-boot VM marked ``install_only: true`` reaches
virt-install like any other, is ejected once the install is done, and is
never compared by ``boxman update``. The install counts as done when the
guest agent reports the guest's root -- ``/`` or a Windows drive root --
on the VM's boot disk (``virsh guestinfo --filesystem``).

Before this, the PoC Windows box had to give virt-install its answer ISO
as a raw ``--disk``: declared in ``cdroms:``, the ISO was put back by every
``update`` after it was ejected; not declared, every ``update`` took it for
a removed drive and detached it.

Only virsh is faked: :class:`InstallDomain` adds the guest agent and the
eject to the libvirt 10.0 model of ``test_cdrom_running_vm``.
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import invoke
import pytest

from boxman.exceptions import ConfigError, ProvisionError
from boxman.manager import BoxmanManager
from boxman.providers.libvirt.install_media import (
    InstallMediaOutcome,
    installed_system_is_up,
    is_install_only,
    parse_guestinfo,
    root_filesystem_disks,
)
from boxman.providers.libvirt.session import LibVirtSession
from conftest import make_bare_manager
from test_cdrom_running_vm import VM, FakeDomain, Result, _diff, _virsh

pytestmark = pytest.mark.unit

#: ``virsh guestinfo --filesystem`` of an installed Rocky 9 guest
LINUX_INSTALLED = """\
fs.count            : 2
fs.0.name           : vda4
fs.0.mountpoint     : /
fs.0.fstype         : xfs
fs.0.total-bytes    : 9650507776
fs.0.used-bytes     : 1574707200
fs.0.disk.count     : 1
fs.0.disk.0.alias   : vda
fs.0.disk.0.device  : /dev/vda4
fs.1.name           : vda3
fs.1.mountpoint     : /boot
fs.1.fstype         : xfs
fs.1.disk.count     : 1
fs.1.disk.0.alias   : vda
fs.1.disk.0.device  : /dev/vda3

"""

#: the same of a Windows Server 2022 guest: a root per drive letter, the
#: answer ISO's drive included
WINDOWS_INSTALLED = """\
fs.count            : 3
fs.0.name           : \\\\?\\Volume{0d6d8a3e-0000-0000-0000-100000000000}\\
fs.0.mountpoint     : System Reserved
fs.0.fstype         : NTFS
fs.0.disk.count     : 1
fs.0.disk.0.alias   : vda
fs.1.name           : \\\\?\\Volume{0d6d8a3e-0000-0000-0000-501f00000000}\\
fs.1.mountpoint     : C:\\
fs.1.fstype         : NTFS
fs.1.total-bytes    : 42410029056
fs.1.used-bytes     : 7924191232
fs.1.disk.count     : 1
fs.1.disk.0.alias   : vda
fs.1.disk.0.serial  :
fs.2.name           : \\\\?\\Volume{8a1c22e6-0000-0000-0000-000000000000}\\
fs.2.mountpoint     : F:\\
fs.2.fstype         : CDFS
fs.2.disk.count     : 1
fs.2.disk.0.alias   : sdc

"""

#: a live installer: its root is an overlay, the ISO is mounted, and the
#: disk it installs to is mounted under /mnt
LIVE_INSTALLER = """\
fs.count            : 3
fs.0.name           : overlay
fs.0.mountpoint     : /
fs.0.fstype         : overlay
fs.0.disk.count     : 0
fs.1.name           : sr0
fs.1.mountpoint     : /iso
fs.1.fstype         : iso9660
fs.1.disk.count     : 1
fs.1.disk.0.alias   : sda
fs.2.name           : vda1
fs.2.mountpoint     : /mnt
fs.2.fstype         : ext4
fs.2.disk.count     : 1
fs.2.disk.0.alias   : vda

"""

NOT_CONNECTED = ("error: Guest agent is not responding: QEMU guest agent is "
                 "not connected\n")


class InstallDomain(FakeDomain):
    """:class:`FakeDomain` with a guest agent and ``change-media --eject``.

    *agent* is what ``virsh guestinfo --filesystem`` prints, or None for an
    agent that is not connected. *refuse_eject* makes every eject fail the
    way libvirt fails one for a locked tray it cannot force.
    """

    def __init__(self, *args, agent: str | None = None,
                 refuse_eject: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.agent = agent
        self.refuse_eject = refuse_eject

    def _guestinfo(self, *flags, **_kwargs):
        assert '--filesystem' in flags
        if self.state != 'running' or self.agent is None:
            return Result(code=1, stderr=NOT_CONNECTED)
        return Result(stdout=self.agent)

    def _change_media(self, target, *flags, **kwargs):
        if target and '--eject' in flags:
            if self.refuse_eject:
                return Result(code=1, stderr=(
                    "error: Failed to complete action eject on media\n"
                    "error: internal error: unable to execute QEMU command "
                    "'blockdev-remove-medium': Tray of device is not open\n"))
            if '--live' in flags:
                assert self.active and target in self.live
                device, bus, _old = self.live[target]
                self.live[target] = (device, bus, None)
            if '--config' in flags:
                device, bus, _old = self.persistent[target]
                self.persistent[target] = (device, bus, None)
            return Result(stdout='Successfully ejected media.\n')
        return super()._change_media(target, *flags, **kwargs)


@pytest.fixture
def media(tmp_path: Path) -> dict[str, str]:
    """The installer, a driver ISO, an answer ISO and a tools ISO."""
    paths = {}
    for name in ('installer', 'drivers', 'answer', 'tools'):
        iso = tmp_path / f'{name}.iso'
        iso.write_bytes(b'iso')
        paths[name] = str(iso)
    return paths


def _windows_domain(media, **kwargs) -> InstallDomain:
    """The three CD-ROMs of the Windows box, as virt-install attaches them."""
    return InstallDomain(cdroms={'sda': media['installer'],
                                 'sdb': media['drivers'],
                                 'sdc': media['answer']}, **kwargs)


def _eject(domain: InstallDomain, sources, dry_run: bool = False):
    with _virsh(domain):
        return LibVirtSession(
            config={'provider': {'libvirt': {'use_sudo': False}}}
        ).eject_install_media(VM, sources, dry_run=dry_run)


def _virsh_for(domain):
    from boxman.providers.libvirt.commands import VirshCommand
    return VirshCommand(provider_config={'use_sudo': False})


# ---------------------------------------------------------------------------
# The entry
# ---------------------------------------------------------------------------

class TestTheEntry:

    def test_only_a_literal_true_marks_an_entry(self):
        assert is_install_only({'name': 'x', 'install_only': True})
        assert not is_install_only({'name': 'x', 'install_only': False})
        assert not is_install_only({'name': 'x', 'install_only': 'yes'})
        assert not is_install_only({'name': 'x'})
        assert not is_install_only('x')


# ---------------------------------------------------------------------------
# When the install is done
# ---------------------------------------------------------------------------

class TestTheInstalledSystemSignal:

    def test_a_linux_root_on_the_boot_disk_means_the_install_is_done(self):
        assert root_filesystem_disks(parse_guestinfo(LINUX_INSTALLED)) == {'vda'}

    def test_a_windows_drive_root_on_the_boot_disk_means_it_too(self):
        roots = root_filesystem_disks(parse_guestinfo(WINDOWS_INSTALLED))
        # C:\ on the boot disk; the answer ISO's F:\ on its own drive, and
        # the unlettered System Reserved volume is no root at all
        assert roots == {'vda', 'sdc'}

    def test_the_value_keeps_its_colon(self):
        info = parse_guestinfo(WINDOWS_INSTALLED)
        assert info['fs.1.mountpoint'] == 'C:\\'
        assert info['fs.1.disk.0.serial'] == ''

    def test_a_live_installer_has_no_root_on_its_target_disk(self):
        # the target disk is mounted, but under /mnt, and / is an overlay
        assert root_filesystem_disks(parse_guestinfo(LIVE_INSTALLER)) == set()

    def test_garbage_is_no_root(self):
        assert root_filesystem_disks(parse_guestinfo(
            'fs.count : two\nfs.0.mountpoint : /\n')) == set()
        assert root_filesystem_disks(parse_guestinfo('')) == set()

    def test_the_agent_of_the_installed_system_says_so(self):
        domain = InstallDomain(agent=LINUX_INSTALLED)
        with _virsh(domain):
            assert installed_system_is_up(_virsh_for(domain), VM, 'vda') == (
                True, '')

    def test_a_live_installer_with_an_agent_is_still_installing(self):
        domain = InstallDomain(agent=LIVE_INSTALLER)
        with _virsh(domain):
            done, why = installed_system_is_up(_virsh_for(domain), VM, 'vda')
        assert not done
        assert "root on no disk of the domain" in why
        assert "not on the boot disk vda" in why

    def test_an_agent_that_does_not_answer_says_why(self):
        domain = InstallDomain(agent=None)
        with _virsh(domain):
            done, why = installed_system_is_up(_virsh_for(domain), VM, 'vda')
        assert not done
        assert why == ("its guest agent does not answer (error: Guest agent "
                       "is not responding: QEMU guest agent is not connected)")

    def test_an_agent_that_hangs_is_given_up_on(self):
        virsh = MagicMock()
        virsh.execute.side_effect = invoke.exceptions.CommandTimedOut(
            MagicMock(), 60)
        done, why = installed_system_is_up(virsh, VM, 'vda')
        assert not done
        assert why == "its guest agent did not answer within 60s"
        assert virsh.execute.call_args.kwargs['timeout'] == 60


# ---------------------------------------------------------------------------
# The eject
# ---------------------------------------------------------------------------

class TestEjectingInstallMedia:

    def test_a_finished_install_loses_them_live_and_in_the_config(self, media):
        domain = _windows_domain(media, agent=WINDOWS_INSTALLED)
        outcome = _eject(domain, {media['drivers'], media['answer']})
        assert outcome.ejected == [('sdb', media['drivers']),
                                   ('sdc', media['answer'])]
        assert not outcome.failed and outcome.waiting is None
        for view in (domain.live, domain.persistent):
            assert view['sdb'][2] is None and view['sdc'][2] is None
        ejects = domain.calls_of('change-media')
        assert ejects == [
            ('change-media', VM, 'sdb', '--eject', '--force', '--live',
             '--config'),
            ('change-media', VM, 'sdc', '--eject', '--force', '--live',
             '--config')]

    def test_the_drives_stay_and_other_media_stay_in_theirs(self, media):
        domain = _windows_domain(media, agent=WINDOWS_INSTALLED)
        _eject(domain, {media['answer']})
        assert domain.persistent['sda'][2] == media['installer']
        assert domain.persistent['sdb'][2] == media['drivers']
        # ejected, not detached: the drive is still there, empty
        assert 'sdc' in domain.persistent and 'sdc' in domain.live

    def test_a_vm_still_installing_keeps_them(self, media):
        domain = _windows_domain(media, agent=None)
        outcome = _eject(domain, {media['answer']})
        assert outcome.held == [('sdc', media['answer'])]
        assert outcome.waiting.startswith("its guest agent does not answer")
        assert not domain.calls_of('change-media')

    def test_a_live_installer_keeps_its_iso(self, media):
        domain = InstallDomain(cdroms={'sda': media['installer']},
                               agent=LIVE_INSTALLER)
        outcome = _eject(domain, {media['installer']})
        assert outcome.waiting and not outcome.ejected
        assert domain.live['sda'][2] == media['installer']

    def test_a_vm_that_is_not_running_is_not_asked(self, media):
        domain = _windows_domain(media, state='shut off',
                                 agent=WINDOWS_INSTALLED)
        outcome = _eject(domain, {media['answer']})
        assert outcome.waiting == "it is shut off"
        assert not domain.calls_of('guestinfo')
        assert not domain.calls_of('change-media')

    def test_a_dry_run_ejects_nothing(self, media):
        domain = _windows_domain(media, agent=WINDOWS_INSTALLED)
        outcome = _eject(domain, {media['answer']}, dry_run=True)
        assert outcome.would_eject == [('sdc', media['answer'])]
        assert not domain.calls_of('change-media')

    def test_a_vm_holding_none_is_not_asked_anything_more(self, media):
        domain = _windows_domain(media, agent=WINDOWS_INSTALLED)
        domain.live['sdc'] = domain.persistent['sdc'] = ('cdrom', 'sata', None)
        outcome = _eject(domain, {media['answer']})
        assert outcome.held == [] and outcome.waiting is None
        assert not domain.calls_of('guestinfo')

    def test_a_failed_eject_is_reported(self, media):
        domain = _windows_domain(media, agent=WINDOWS_INSTALLED,
                                 refuse_eject=True)
        outcome = _eject(domain, {media['answer']})
        assert outcome.failed == [(
            'sdc', media['answer'],
            "error: internal error: unable to execute QEMU command "
            "'blockdev-remove-medium': Tray of device is not open")]
        assert not outcome.ejected

    def test_a_medium_only_the_config_holds_is_ejected_there_only(self, media):
        # an earlier media change put the answer ISO in sdc's persistent
        # definition only; the guest has the tools ISO there
        domain = _windows_domain(media, agent=WINDOWS_INSTALLED)
        domain.live['sdc'] = ('cdrom', 'sata', media['tools'])
        _eject(domain, {media['answer']})
        assert domain.calls_of('change-media') == [
            ('change-media', VM, 'sdc', '--eject', '--force', '--config')]
        assert domain.live['sdc'][2] == media['tools']

    def test_the_boot_disk_is_read_from_the_persistent_definition(self, media):
        domain = _windows_domain(media, agent=LINUX_INSTALLED)
        _eject(domain, {media['answer']})
        listing = [call for call in domain.calls_of('domblklist')
                   if '--inactive' in call]
        assert listing, "the boot disk comes from the persistent definition"

    def test_a_path_is_compared_the_way_the_differ_compares_it(self, media):
        domain = _windows_domain(media, agent=WINDOWS_INSTALLED)
        relative = media['answer'].replace('/answer.iso', '/./answer.iso')
        outcome = _eject(domain, {relative})
        assert outcome.ejected == [('sdc', media['answer'])]


# ---------------------------------------------------------------------------
# update never compares them
# ---------------------------------------------------------------------------

class TestUpdateNeverComparesThem:

    @staticmethod
    def _desired(media, *, answer_source=True):
        answer = {'name': 'answer', 'install_only': True}
        if answer_source:
            answer['source'] = media['answer']
        return [{'name': 'installer', 'source': media['installer'],
                 'install_only': True},
                {'name': 'drivers', 'source': media['drivers']},
                answer]

    @staticmethod
    def _changes(diff):
        return (diff['new_cdroms'], diff['removed_cdroms'],
                diff['changed_cdroms'])

    def test_mid_install_an_attached_medium_is_no_removal(self, media):
        domain = _windows_domain(media)
        assert self._changes(_diff(domain, self._desired(media))) == (
            [], [], [])

    def test_after_the_eject_it_is_not_put_back(self, media):
        domain = _windows_domain(media)
        for view in (domain.live, domain.persistent):
            view['sda'] = ('cdrom', 'sata', None)
            view['sdc'] = ('cdrom', 'sata', None)
        diff = _diff(domain, self._desired(media))
        assert self._changes(diff) == ([], [], [])
        assert not diff['cdroms_restart_pending']

    def test_a_medium_that_is_not_install_only_is_still_compared(self, media):
        domain = _windows_domain(media)
        domain.live['sdb'] = domain.persistent['sdb'] = ('cdrom', 'sata', None)
        new, removed, changed = self._changes(
            _diff(domain, self._desired(media)))
        assert [c['name'] for c in new] == ['drivers']
        assert removed == [] and changed == []

    def test_an_undeclared_drive_is_still_removed(self, media):
        domain = _windows_domain(media)
        domain.live['sdd'] = domain.persistent['sdd'] = (
            'cdrom', 'sata', media['tools'])
        _new, removed, _changed = self._changes(
            _diff(domain, self._desired(media)))
        assert [c['target'] for c in removed] == ['sdd']

    def test_an_install_medium_without_a_resolved_source_is_refused(
            self, media):
        # unrecognised, its drive would be an undeclared one to detach
        domain = _windows_domain(media)
        with pytest.raises(ProvisionError, match="has no resolved source"):
            _diff(domain, self._desired(media, answer_source=False))


# ---------------------------------------------------------------------------
# update resolves them without verifying or fetching
# ---------------------------------------------------------------------------

PRJ = 'bprj__demo__bprj'
FULL = f'{PRJ}_c1_win01'


def _iso_config(cdroms, boot_order=('cdrom', 'hd')):
    return {
        'project': 'demo',
        'isos': {
            'installer': {'uri': 'https://example.org/win.iso',
                          'checksum': 'sha256:' + '0' * 64},
            'drivers': {'uri': 'https://example.org/drivers.iso'},
        },
        'clusters': {'c1': {'workdir': '/wd/c1', 'vms': {
            'win01': {'boot_order': list(boot_order), 'cdroms': cdroms}}}},
    }


def _manager(config, cache_dir) -> BoxmanManager:
    mgr = make_bare_manager(config)
    mgr.app_config = {'cache': {'enabled': True, 'cache_dir': str(cache_dir)}}
    return mgr


class TestUpdateResolution:

    def test_an_install_medium_is_neither_verified_nor_fetched(
            self, tmp_path):
        cache = tmp_path / 'cache'
        mgr = _manager(_iso_config([
            {'name': 'installer', 'install_only': True},
            {'name': 'drivers'}]), cache)
        drivers = Path(mgr._iso_cache_path('drivers'))
        drivers.parent.mkdir(parents=True)
        drivers.write_bytes(b'iso')
        with patch.object(BoxmanManager, '_fetch_missing_isos') as fetch, \
                patch('boxman.image_cache.ImageCache.verify_checksum') as verify:
            failures = mgr._normalize_cdroms_for_update({FULL})
        assert failures == {}
        fetch.assert_not_called()
        verify.assert_not_called()

    def test_its_cache_path_reaches_the_differ(self, tmp_path):
        cache = tmp_path / 'cache'
        mgr = _manager(_iso_config([
            {'name': 'installer', 'install_only': True}]), cache)
        assert mgr._normalize_cdroms_for_update({FULL}, allow_fetch=False) == {}
        entry = mgr.config['clusters']['c1']['vms']['win01']['cdroms'][0]
        assert entry == {'name': 'installer', 'install_only': True,
                         'source': mgr._iso_cache_path('installer')}
        assert entry['source'].startswith(str(cache))

    def test_an_undeclared_install_medium_fails_its_vm(self, tmp_path):
        mgr = _manager(_iso_config([
            {'name': 'nowhere', 'install_only': True}]), tmp_path)
        failures = mgr._normalize_cdroms_for_update({FULL})
        assert failures == {
            FULL: "iso 'nowhere' is not declared in the 'isos:' section"}

    def test_the_sources_are_the_paths_it_would_attach(self, tmp_path):
        mgr = _manager(_iso_config([
            {'name': 'installer', 'install_only': True},
            {'name': 'drivers'},
            {'source': '~/answer.iso', 'install_only': True}]), tmp_path)
        vm_info = mgr.config['clusters']['c1']['vms']['win01']
        assert mgr._install_only_sources(vm_info) == {
            mgr._iso_cache_path('installer'),
            str(Path('~/answer.iso').expanduser())}


# ---------------------------------------------------------------------------
# Only an ISO-boot VM may mark one, and only with a boolean
# ---------------------------------------------------------------------------

class TestValidation:

    def test_install_only_on_a_cloned_vm_is_refused(self):
        mgr = make_bare_manager(_iso_config(
            [{'name': 'installer', 'install_only': True}],
            boot_order=('hd',)))
        with pytest.raises(ConfigError, match=(
                "cdrom 'installer' is install_only, but the VM does not "
                "boot from an ISO")):
            mgr.validate_direct_boot_config()

    def test_a_value_other_than_a_boolean_is_refused(self):
        mgr = make_bare_manager(_iso_config(
            [{'name': 'installer', 'install_only': 'yes'}]))
        with pytest.raises(ConfigError, match=(
                "has install_only: 'yes'; it must be true or false")):
            mgr.validate_direct_boot_config()

    def test_an_iso_boot_vm_may_mark_any_of_its_cdroms(self):
        mgr = make_bare_manager(_iso_config([
            {'name': 'installer', 'install_only': True},
            {'source': '/iso/answer.iso', 'install_only': True},
            {'name': 'drivers', 'install_only': False}]))
        mgr.validate_direct_boot_config()


# ---------------------------------------------------------------------------
# provision, up and update ask
# ---------------------------------------------------------------------------

class TestTheManagerPass:

    def _manager(self, tmp_path, outcome):
        config = _iso_config([{'name': 'installer', 'install_only': True},
                              {'name': 'drivers'}])
        config['clusters']['c1']['vms']['linux01'] = {
            'boot_order': ['cdrom'], 'cdroms': [{'name': 'drivers'}]}
        mgr = _manager(config, tmp_path)
        session = MagicMock()
        session.eject_install_media.return_value = outcome
        session.virsh_invocation.side_effect = (
            lambda *args: 'virsh ' + ' '.join(args))
        mgr.session_for_cluster = MagicMock(return_value=session)
        return mgr, session

    @staticmethod
    def _outcome(**fields):
        from boxman.providers.libvirt.install_media import InstallMediaOutcome
        return InstallMediaOutcome(**fields)

    def test_only_a_vm_with_install_media_is_asked(self, tmp_path):
        mgr, session = self._manager(tmp_path, self._outcome())
        assert mgr.eject_install_media() == []
        session.eject_install_media.assert_called_once_with(
            FULL, {mgr._iso_cache_path('installer')}, dry_run=False,
            declared_done=False)

    def test_a_vm_left_out_is_not_asked(self, tmp_path):
        mgr, session = self._manager(tmp_path, self._outcome())
        mgr.eject_install_media([f'{PRJ}_c1_linux01'])
        session.eject_install_media.assert_not_called()

    def test_waiting_names_the_reason_and_the_command_to_finish(self, tmp_path):
        mgr, _session = self._manager(tmp_path, self._outcome(
            held=[('sda', '/c/win.iso')], live={'sda'}, persistent={'sda'},
            waiting="its guest agent does not answer (error: x)"))
        assert mgr.eject_install_media() == []
        line = mgr.logger.info.call_args.args[0]
        assert line.startswith(
            "vm c1/win01: keeping its install-only media sda (/c/win.iso): "
            "the install is not known to be done, as its guest agent does "
            "not answer (error: x).")
        assert line.endswith(
            "a guest without an agent can be told so: "
            "boxman control finish-install --vms c1_win01")

    def test_a_failed_eject_is_a_problem(self, tmp_path):
        mgr, _session = self._manager(tmp_path, self._outcome(
            held=[('sda', '/c/win.iso')],
            failed=[('sda', '/c/win.iso', 'error: tray locked')]))
        assert mgr.eject_install_media() == [
            "vm c1/win01: install-only medium /c/win.iso is still in sda: "
            "error: tray locked"]

    def test_unreadable_drives_are_a_problem(self, tmp_path):
        mgr, session = self._manager(tmp_path, None)
        session.eject_install_media.side_effect = ProvisionError("no virsh")
        assert mgr.eject_install_media() == [
            "vm c1/win01: install-only media not checked: no virsh"]

    def test_a_dry_run_says_what_it_would_eject(self, tmp_path):
        mgr, session = self._manager(tmp_path, self._outcome(
            held=[('sda', '/c/win.iso')], would_eject=[('sda', '/c/win.iso')]))
        mgr.eject_install_media(dry_run=True)
        assert session.eject_install_media.call_args.kwargs == {
            'dry_run': True, 'declared_done': False}
        mgr.logger.info.assert_called_with(
            "[dry-run] vm c1/win01: would eject its install-only media sda "
            "(/c/win.iso), now that its install is done")


class TestTheVerbsAsk:
    """Each verb asks; ``up``'s existing-VM branches above all, since the
    ``up`` that starts an install has ended before it is done."""

    def _up_manager(self, monkeypatch, state):
        mgr = make_bare_manager({'project': 'demo', 'clusters': {}})
        for name in ("_update_sessions_with_runtime", "ensure_shared_bridges",
                     "report_network_results", "raise_on_network_failures",
                     "ensure_netlab_up", "connect_info", "wait_for_vm_ips"):
            monkeypatch.setattr(BoxmanManager, name,
                                lambda cls, *a, **kw: None)
        monkeypatch.setattr(BoxmanManager, "_get_vm_states",
                            lambda cls: {FULL: state})
        monkeypatch.setattr(BoxmanManager, "_get_project_vm_names",
                            lambda cls: [FULL])
        monkeypatch.setattr(BoxmanManager, "reconcile_networks",
                            lambda cls, **kw: {})
        monkeypatch.setattr(BoxmanManager, "_reconcile_compose_clusters",
                            lambda cls: '')
        monkeypatch.setattr(BoxmanManager, "_rewrite_ssh_config",
                            lambda cls: [])
        monkeypatch.setattr(BoxmanManager, "_control_vm_targets",
                            lambda cls, cli_args: [(FULL, '/wd/c1')])
        mgr.eject_install_media = MagicMock(return_value=[])
        return mgr

    def test_a_second_up_on_running_vms_asks(self, monkeypatch):
        mgr = self._up_manager(monkeypatch, 'running')
        mgr.up(types.SimpleNamespace())
        mgr.eject_install_media.assert_called_once_with()

    def test_an_up_that_starts_vms_asks_for_the_ones_that_came_up(
            self, monkeypatch):
        mgr = self._up_manager(monkeypatch, 'shut off')
        session = MagicMock()
        session.start_vm.return_value = True
        mgr.session_for_vm = MagicMock(return_value=session)
        monkeypatch.setattr(BoxmanManager, "_refuse_stale_external_save",
                            lambda cls, *a: None)
        monkeypatch.setattr(BoxmanManager, "_vms_worth_waiting_for",
                            lambda cls: [FULL])
        monkeypatch.setattr(
            BoxmanManager, "_run_parallel",
            lambda cls, tasks, op_label='', **kw: (
                {label: fn(*args) for label, fn, args in tasks}, {}))
        mgr.up(types.SimpleNamespace())
        mgr.eject_install_media.assert_called_once_with([FULL])

    def test_a_failed_eject_fails_up(self, monkeypatch):
        mgr = self._up_manager(monkeypatch, 'running')
        mgr.eject_install_media.return_value = ['vm c1/win01: still in sda']
        with pytest.raises(ProvisionError, match="still in sda"):
            mgr.up(types.SimpleNamespace())

    def test_update_asks_before_it_resolves_the_media_it_diffs(self):
        # the eject comes first: the diff after it then sees empty drives,
        # and a dry run passes its flag on
        mgr = make_bare_manager(_iso_config([{'name': 'installer',
                                              'install_only': True}]))
        calls = MagicMock()
        for name in ('validate_direct_boot_config',
                     'validate_clone_identity_config',
                     '_update_sessions_with_runtime', 'ensure_shared_bridges',
                     'report_network_results', 'raise_on_network_failures',
                     'setup_ssh_access', 'connect_info',
                     'report_clone_degradations'):
            setattr(mgr, name, MagicMock())
        mgr.reconcile_networks = MagicMock(return_value={})
        mgr._get_project_vm_names = MagicMock(return_value=[FULL])
        mgr._find_all_existing_project_vms = MagicMock(return_value=[FULL])
        mgr.eject_install_media = calls.eject_install_media
        mgr.eject_install_media.return_value = []
        mgr.eject_install_media.side_effect = (
            lambda names, dry_run=False, outcomes=None: [])
        mgr._normalize_cdroms_for_update = calls._normalize_cdroms_for_update
        mgr._normalize_cdroms_for_update.return_value = {}
        mgr._run_parallel = MagicMock(return_value=({}, {}))
        mgr.update(types.SimpleNamespace(dry_run=True, yes=False))
        assert [c[0] for c in calls.mock_calls] == [
            'eject_install_media', '_normalize_cdroms_for_update']
        assert calls.eject_install_media.call_args.args == ({FULL},)
        assert calls.eject_install_media.call_args.kwargs['dry_run'] is True

    def test_a_failed_eject_fails_update(self):
        mgr = make_bare_manager(_iso_config([{'name': 'installer',
                                              'install_only': True}]))
        for name in ('validate_direct_boot_config',
                     'validate_clone_identity_config',
                     '_update_sessions_with_runtime', 'ensure_shared_bridges',
                     'report_network_results', 'raise_on_network_failures',
                     'setup_ssh_access', 'connect_info',
                     'report_clone_degradations'):
            setattr(mgr, name, MagicMock())
        mgr.reconcile_networks = MagicMock(return_value={})
        mgr._get_project_vm_names = MagicMock(return_value=[FULL])
        mgr._find_all_existing_project_vms = MagicMock(return_value=[FULL])
        mgr.eject_install_media = MagicMock(return_value=['still in sda'])
        mgr._normalize_cdroms_for_update = MagicMock(return_value={})
        mgr._run_parallel = MagicMock(return_value=({}, {}))
        with pytest.raises(ProvisionError, match="still in sda"):
            mgr.update(types.SimpleNamespace(dry_run=False, yes=False))

    def _provision_manager(self):
        mgr = make_bare_manager(_iso_config([{'name': 'installer',
                                              'install_only': True}]))
        mgr.cache = MagicMock(projects={})
        for name in ('_update_sessions_with_runtime',
                     'validate_direct_boot_config',
                     'validate_clone_identity_config', 'validate_iso_runtime',
                     'register_project_in_cache', '_expand_oci_base_images',
                     'validate_base_images', 'provision_files',
                     'ensure_shared_bridges', 'define_networks', 'clone_vms',
                     'configure_and_start_vms', 'wait_for_vm_ips',
                     'setup_ssh_access', 'connect_info',
                     'provision_compose_clusters', 'deploy_netlab',
                     'report_clone_degradations'):
            setattr(mgr, name, MagicMock())
        mgr._find_existing_project_vms = MagicMock(return_value=[])
        mgr.ensure_templates_exist = MagicMock(return_value=True)
        mgr._get_project_vm_names = MagicMock(return_value=[FULL])
        mgr._get_vm_states = MagicMock(return_value={FULL: 'running'})
        mgr.session_for_cluster = MagicMock()
        mgr.eject_install_media = MagicMock(return_value=[])
        return mgr

    def test_provision_asks_once_the_vms_have_their_addresses(self):
        mgr = self._provision_manager()
        order = MagicMock()
        order.attach_mock(mgr.wait_for_vm_ips, 'wait_for_vm_ips')
        order.attach_mock(mgr.eject_install_media, 'eject_install_media')
        order.attach_mock(mgr.setup_ssh_access, 'setup_ssh_access')
        mgr.provision(types.SimpleNamespace(force=False))
        assert [c[0] for c in order.mock_calls] == [
            'wait_for_vm_ips', 'eject_install_media', 'setup_ssh_access']
        mgr.eject_install_media.assert_called_once_with([FULL])

    def test_a_failed_eject_fails_provision_at_its_end(self):
        mgr = self._provision_manager()
        mgr.eject_install_media.return_value = ['vm c1/win01: still in sda']
        with pytest.raises(ProvisionError, match="still in sda"):
            mgr.provision(types.SimpleNamespace(force=False))
        # the rest of provision ran first
        mgr.setup_ssh_access.assert_called_once()
        mgr.deploy_netlab.assert_called_once()


# ---------------------------------------------------------------------------
# boxman control finish-install: the install declared done
# ---------------------------------------------------------------------------

class TestDeclaringTheInstallDone:
    """A guest whose installed system runs no agent kept its install-only
    media for ever; `boxman control finish-install` ejects them on the
    user's word."""

    def test_a_running_guest_without_an_agent_loses_them_live(self, media):
        domain = _windows_domain(media, agent=None)
        with _virsh(domain):
            outcome = LibVirtSession(
                config={'provider': {'libvirt': {'use_sudo': False}}}
            ).eject_install_media(VM, {media['answer']}, declared_done=True)
        assert outcome.ejected == [('sdc', media['answer'])]
        assert domain.calls_of('change-media') == [
            ('change-media', VM, 'sdc', '--eject', '--force', '--live',
             '--config')]
        assert not domain.calls_of('guestinfo')

    def test_a_shut_off_one_loses_them_from_its_config(self, media):
        domain = _windows_domain(media, state='shut off')
        with _virsh(domain):
            outcome = LibVirtSession(
                config={'provider': {'libvirt': {'use_sudo': False}}}
            ).eject_install_media(VM, {media['answer']}, declared_done=True)
        assert outcome.ejected == [('sdc', media['answer'])]
        assert domain.calls_of('change-media') == [
            ('change-media', VM, 'sdc', '--eject', '--force', '--config')]
        assert domain.persistent['sdc'][2] is None

    def test_afterwards_up_and_update_find_nothing_to_say(self, media):
        domain = _windows_domain(media, agent=None)
        sources = {media['installer'], media['drivers'], media['answer']}
        with _virsh(domain):
            session = LibVirtSession(
                config={'provider': {'libvirt': {'use_sudo': False}}})
            session.eject_install_media(VM, sources, declared_done=True)
            outcome = session.eject_install_media(VM, sources)
        assert outcome.held == [] and outcome.waiting is None

    def _manager(self, tmp_path, outcome=None, cdroms=None):
        config = _iso_config(cdroms if cdroms is not None else [
            {'name': 'installer', 'install_only': True}, {'name': 'drivers'}])
        config['clusters']['c1']['vms']['linux01'] = {
            'boot_order': ['cdrom'], 'cdroms': [{'name': 'drivers'}]}
        mgr = _manager(config, tmp_path)
        session = MagicMock()
        session.eject_install_media.return_value = (
            outcome or InstallMediaOutcome())
        mgr.session_for_cluster = MagicMock(return_value=session)
        mgr._update_sessions_with_runtime = MagicMock()
        mgr._is_compose_cluster = lambda name: False
        return mgr, session

    @staticmethod
    def _cli(vms='all', dry_run=False, cluster=None):
        return types.SimpleNamespace(vms=vms, dry_run=dry_run, cluster=cluster)

    def test_it_ejects_one_named_vm_whatever_the_agent_says(self, tmp_path):
        mgr, session = self._manager(tmp_path, InstallMediaOutcome(
            held=[('sda', '/c/win.iso')], ejected=[('sda', '/c/win.iso')]))
        mgr.finish_install(self._cli(vms='win01'))
        session.eject_install_media.assert_called_once_with(
            FULL, {mgr._iso_cache_path('installer')}, dry_run=False,
            declared_done=True)
        mgr.logger.info.assert_any_call(
            "vm c1/win01: ejected install-only medium /c/win.iso from sda: "
            "its install is declared done")

    def test_a_vm_with_nothing_left_is_a_no_op_that_says_so(self, tmp_path):
        mgr, _session = self._manager(tmp_path, InstallMediaOutcome())
        mgr.finish_install(self._cli(vms='c1_win01'))
        mgr.logger.info.assert_any_call(
            "vm c1/win01: no install-only medium is left in its drives; "
            "nothing to eject")

    def test_a_named_vm_without_install_only_media_is_refused(self, tmp_path):
        mgr, session = self._manager(tmp_path)
        with pytest.raises(ConfigError, match=(
                "vm c1/linux01 has no install-only media \\(no cdroms: entry "
                "with install_only: true\\), so it has no install to declare "
                "done")):
            mgr.finish_install(self._cli(vms='win01,linux01'))
        session.eject_install_media.assert_not_called()

    def test_an_unknown_vm_is_refused(self, tmp_path):
        mgr, session = self._manager(tmp_path)
        with pytest.raises(ConfigError, match="there is no VM 'win02'"):
            mgr.finish_install(self._cli(vms='win02'))
        session.eject_install_media.assert_not_called()

    def test_all_leaves_out_the_vms_without_them(self, tmp_path):
        mgr, session = self._manager(tmp_path)
        mgr.finish_install(self._cli())
        assert [c.args[0] for c in session.eject_install_media.call_args_list
                ] == [FULL]

    def test_a_project_without_any_is_refused(self, tmp_path):
        mgr, _session = self._manager(tmp_path, cdroms=[{'name': 'drivers'}])
        with pytest.raises(ConfigError,
                           match="no selected VM has install-only media"):
            mgr.finish_install(self._cli())

    def test_a_dry_run_ejects_nothing(self, tmp_path):
        mgr, session = self._manager(tmp_path, InstallMediaOutcome(
            held=[('sda', '/c/win.iso')], would_eject=[('sda', '/c/win.iso')]))
        mgr.finish_install(self._cli(vms='win01', dry_run=True))
        assert session.eject_install_media.call_args.kwargs == {
            'dry_run': True, 'declared_done': True}
        mgr.logger.info.assert_any_call(
            "[dry-run] vm c1/win01: would eject its install-only media sda "
            "(/c/win.iso), now that its install is declared done")

    def test_a_failed_eject_fails_it(self, tmp_path):
        mgr, _session = self._manager(tmp_path, InstallMediaOutcome(
            held=[('sda', '/c/win.iso')],
            failed=[('sda', '/c/win.iso', 'error: locked')]))
        with pytest.raises(ProvisionError,
                           match="control finish-install failed for 1"):
            mgr.finish_install(self._cli(vms='win01'))

    def test_the_cli_has_it(self):
        from boxman.scripts.cli_parser import parse_args
        args = parse_args().parse_args([
            'control', 'finish-install', '--vms', 'c1_win01', '--cluster',
            'c1', '--dry-run'])
        assert (args.handler, args.vms, args.cluster, args.dry_run) == (
            'finish_install', 'c1_win01', 'c1', True)
        from boxman.scripts.app import _CLI_HANDLERS
        assert 'finish_install' in _CLI_HANDLERS


# ---------------------------------------------------------------------------
# update --restart skips a guest mid-install; --force-restart does not
# ---------------------------------------------------------------------------

class TestNoRestartMidInstall:

    @staticmethod
    def _run(held, allow_restart=True, force_restart=False, dry_run=False,
             install_done=False, state='running'):
        from boxman.providers.libvirt.vm_differ import VMStateDiffer
        from test_update_restart_opt_in import _diff
        mgr = make_bare_manager({'project': 'demo'})
        mgr.provider = MagicMock()
        mgr.provider.update_vm_cpu_memory.return_value = {
            'success': True, 'restart_needed': True}
        mgr.provider.shutdown_and_wait.return_value = True
        mgr.provider.start_vm.return_value = True
        queue = MagicMock()
        diff = _diff(memory_changed=True, desired_memory_mb=4096,
                     install_media_held=held, vm_state=state)
        with patch.object(VMStateDiffer, 'diff_vm', return_value=diff):
            mgr._update_single_vm(
                'c1', {'workdir': '/tmp'}, 'win01', {'memory': 4096}, queue,
                dry_run=dry_run, allow_restart=allow_restart,
                force_restart=force_restart, install_done=install_done)
        return mgr, queue.put.call_args.args[0][1]

    def test_restart_skips_a_guest_mid_install_and_says_why(self):
        mgr, result = self._run(['sda', 'sdc'])
        mgr.provider.shutdown_and_wait.assert_not_called()
        mgr.provider.start_vm.assert_not_called()
        assert result['status'] == 'needs_restart'
        assert ("the restart was skipped: win01 is still installing (its "
                "install-only media in sda, sdc are attached), and update "
                "does not restart a guest mid-install; re-run with "
                "--force-restart to restart it anyway, or declare the "
                "install done first: boxman control finish-install --vms "
                "c1_win01") in result['details']
        mgr.logger.warning.assert_called_once()

    def test_its_other_changes_still_apply(self):
        # as without --restart: written now, live where libvirt can
        mgr, result = self._run(['sda'])
        mgr.provider.update_vm_cpu_memory.assert_called_once()
        assert 'written to the persistent config' in result['details']

    def test_force_restart_restarts_it(self):
        mgr, result = self._run(['sda'], force_restart=True)
        mgr.provider.shutdown_and_wait.assert_called_once()
        mgr.provider.start_vm.assert_called_once()
        assert result['status'] == 'updated'
        assert 'still installing' in mgr.logger.warning.call_args.args[0]

    def test_a_finished_install_is_restarted_by_restart(self):
        mgr, result = self._run([])
        mgr.provider.shutdown_and_wait.assert_called_once()
        assert result['details'].endswith('(restarted)')

    def test_a_dry_run_says_the_restart_would_be_skipped(self):
        mgr, result = self._run(['sda'], dry_run=True)
        assert result['status'] == 'dry_run'
        assert ("a restart these changes need would be skipped: win01 is "
                "still installing") in result['details']
        mgr.provider.update_vm_cpu_memory.assert_not_called()

    def test_a_dry_run_says_force_restart_would_restart_it(self):
        _mgr, result = self._run(['sda'], dry_run=True, force_restart=True)
        assert ("a restart these changes need would go ahead although win01 "
                "is still installing (its install-only media in sda are "
                "attached): --force-restart") in result['details']

    def test_a_dry_run_whose_eject_pass_found_the_install_done(self):
        # the real run ejects before it diffs
        _mgr, result = self._run(['sda'], dry_run=True, install_done=True)
        assert 'still installing' not in result['details']

    def test_without_restart_nothing_is_said_about_the_install(self):
        _mgr, result = self._run(['sda'], allow_restart=False)
        assert 'still installing' not in result['details']
        assert '--restart' in result['details']

    def test_force_restart_implies_restart(self):
        from boxman.scripts.cli_parser import parse_args
        args = parse_args().parse_args(['update', '--force-restart'])
        assert args.force_restart is True
        mgr = make_bare_manager({'project': 'demo', 'clusters': {}})
        captured = {}
        for name in ('validate_direct_boot_config',
                     'validate_clone_identity_config',
                     '_update_sessions_with_runtime', 'ensure_shared_bridges',
                     'report_network_results', 'raise_on_network_failures',
                     'setup_ssh_access', 'connect_info',
                     'report_clone_degradations'):
            setattr(mgr, name, MagicMock())
        mgr.reconcile_networks = MagicMock(return_value={})
        mgr._get_project_vm_names = MagicMock(return_value=[FULL])
        mgr._find_all_existing_project_vms = MagicMock(return_value=[FULL])
        mgr.config['clusters'] = {'c1': {'workdir': '/wd',
                                         'vms': {'win01': {}}}}
        mgr.eject_install_media = MagicMock(return_value=[])
        mgr._normalize_cdroms_for_update = MagicMock(return_value={})

        def run_parallel(tasks, op_label='', **_kw):
            for _label, _target, args in tasks:
                captured['args'] = args
            return {}, {}

        mgr._run_parallel = run_parallel
        mgr.update(types.SimpleNamespace(dry_run=False, yes=False,
                                         restart=False, force_restart=True))
        # (..., dry_run, allow_restart, force_restart, install_done)
        assert captured['args'][5:] == (False, True, True, False)


class TestTheDifferSaysWhatIsHeld:

    def test_drives_holding_install_media_are_named(self, media):
        domain = _windows_domain(media)
        diff = _diff(domain, [
            {'name': 'installer', 'source': media['installer'],
             'install_only': True},
            {'name': 'drivers', 'source': media['drivers']},
            {'name': 'answer', 'source': media['answer'],
             'install_only': True}])
        assert diff['install_media_held'] == ['sda', 'sdc']

    def test_none_once_they_are_ejected(self, media):
        domain = _windows_domain(media)
        for view in (domain.live, domain.persistent):
            view['sda'] = ('cdrom', 'sata', None)
            view['sdc'] = ('cdrom', 'sata', None)
        diff = _diff(domain, [
            {'name': 'installer', 'source': media['installer'],
             'install_only': True},
            {'name': 'answer', 'source': media['answer'],
             'install_only': True}])
        assert diff['install_media_held'] == []
