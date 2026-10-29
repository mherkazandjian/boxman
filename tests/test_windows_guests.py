"""
``os: windows``, and the answer ISO boxman builds for an unattended install
(doc/windows-guests.md, §5.2 items 4 and 5).

The PoC box built its answer ISO with a script, baked a key of the user's
into it, and could not be reached with ``boxman ssh``: the ssh config said
``User admin``, the key push was ``ssh-copy-id`` -- which writes the file
sshd ignores for an administrator -- and the inventory had no shell type.

Only the session, the ISO tool and virsh are faked; the rendering, the
validation and the file handling run for real.
"""

from __future__ import annotations

import os
import stat
import types
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

import pytest
import yaml

from boxman.exceptions import ConfigError, ProvisionError
from boxman.manager import BoxmanManager
from boxman.manager_parts import guest_os
from boxman.manager_parts.guest_os import (
    ANSWER_CDROM_MARK,
    answer_iso_path,
    default_answer_files,
    default_answer_template,
    password_problem,
    render_answer_file,
    write_answer_iso,
)
from boxman.providers.libvirt.clone_vm import CloneVM
from boxman.providers.libvirt.install_media import InstallMediaOutcome
from boxman.providers.libvirt.iso_image import build_iso
from conftest import make_bare_manager
from test_ssh_access import Lab, full

pytestmark = pytest.mark.unit

PASSWORD = 'Qz7&<pass>"x'
NS = {'u': 'urn:schemas-microsoft-com:unattend'}


def _win_vm(**overrides) -> dict:
    vm = {'os': 'windows', 'boot_order': ['cdrom', 'hd'],
          'cdroms': [{'name': 'winsrv', 'install_only': True}],
          'admin_pass': PASSWORD}
    vm.update(overrides)
    return vm


def _manager(tmp_path, vms=None, cluster=None, templates=None) -> BoxmanManager:
    config = {
        'project': 'demo',
        'isos': {'winsrv': {'uri': 'https://example.org/win.iso'}},
        'workspace': {'path': str(tmp_path / 'ws')},
        'clusters': {'c1': {'workdir': str(tmp_path / 'ws'),
                            'vms': vms if vms is not None else
                            {'win01': _win_vm()},
                            **(cluster or {})}},
    }
    if templates:
        config['templates'] = templates
    mgr = make_bare_manager(config)
    mgr.config_path = str(tmp_path / 'box' / 'conf.yml')
    mgr.app_config = {}
    mgr._runtime_name = 'local'
    return mgr


def _refused(mgr) -> str:
    with pytest.raises(ConfigError) as excinfo:
        mgr.validate_guest_os_config()
    return str(excinfo.value)


def _answer(vm_info, **context) -> ET.Element:
    """The default answer file rendered for one VM, parsed."""
    base = {'admin_user': 'Administrator', 'admin_pass': PASSWORD,
            'computer_name': 'win01', 'image_index': 1, 'vm_name': 'win01'}
    base.update(context)
    return ET.fromstring(
        render_answer_file(default_answer_template(), base).encode())


# ---------------------------------------------------------------------------
# The os: key
# ---------------------------------------------------------------------------

class TestTheOsKey:

    def test_a_vm_without_one_is_linux(self):
        assert BoxmanManager.guest_os({}) == 'linux'
        assert BoxmanManager.guest_os({'os': 'windows'}) == 'windows'
        assert not BoxmanManager.is_windows(None)

    def test_an_unknown_value_is_refused(self, tmp_path):
        mgr = _manager(tmp_path, vms={'vm1': {'os': 'win'}})
        assert "c1.vms.vm1.os must be one of linux, windows, got 'win'" in (
            _refused(mgr))

    def test_unattend_on_a_linux_vm_is_refused(self, tmp_path):
        mgr = _manager(tmp_path, vms={'vm1': {'unattend': {}}})
        assert "c1.vms.vm1.unattend is for an os: windows VM" in _refused(mgr)

    def test_a_linux_vm_is_not_changed(self, tmp_path):
        mgr = _manager(tmp_path, vms={'vm1': {'boot_order': ['cdrom'],
                                              'cdroms': ['winsrv']}})
        mgr.validate_guest_os_config()
        mgr.add_answer_cdroms()
        assert mgr.config['clusters']['c1']['vms']['vm1']['cdroms'] == [
            'winsrv']


# ---------------------------------------------------------------------------
# What an os: windows VM may not have
# ---------------------------------------------------------------------------

class TestWhatWindowsRefuses:

    def test_cloudinit_is_refused(self, tmp_path):
        mgr = _manager(tmp_path, vms={'win01': _win_vm(
            cloudinit='#cloud-config\n', cloudinit_metadata='x')})
        message = _refused(mgr)
        assert ("c1.vms.win01.cloudinit: cloud-init does not run on Windows; "
                "an os: windows VM is set up by its answer file (unattend:)"
                in message)
        assert "c1.vms.win01.cloudinit_metadata:" in message

    def test_a_clone_of_a_cloud_init_template_is_refused(self, tmp_path):
        mgr = _manager(
            tmp_path, vms={'win01': {'os': 'windows', 'base_image': 'tpl'}},
            templates={'tpl-key': {'name': 'tpl', 'image': {'uri': 'x'}}})
        assert ("c1.vms.win01: base_image 'tpl' is a templates: entry, which "
                "boxman builds with cloud-init" in _refused(mgr))

    def test_a_clone_of_another_domain_is_not(self, tmp_path):
        mgr = _manager(tmp_path, vms={'win01': {'os': 'windows',
                                                'base_image': 'golden'}})
        mgr.validate_guest_os_config()

    def test_a_required_clone_identity_is_refused(self, tmp_path):
        mgr = _manager(tmp_path, vms={'win01': {
            'os': 'windows', 'base_image': 'golden',
            'clone_ssh_host_keys': 'required'}})
        assert ("c1.vms.win01.clone_ssh_host_keys is required, but the clone "
                "identity pass is Linux-only" in _refused(mgr))

    @pytest.mark.parametrize('policy', ['auto', 'required', 'off'])
    def test_the_clone_identity_pass_is_off(self, tmp_path, policy):
        clone = CloneVM('golden', 'bprj__demo__bprj_c1_win01',
                        {'os': 'windows', 'clone_machine_id': policy,
                         'clone_hostname': policy}, workdir=str(tmp_path))
        assert (clone.machine_id_policy, clone.ssh_host_keys_policy,
                clone.hostname_policy) == ('off', 'off', 'off')
        assert not clone.build_identity_plan().operations

    def test_an_invalid_policy_is_still_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="must be one of"):
            CloneVM('golden', 'win01', {'os': 'windows',
                                        'clone_hostname': 'sometimes'},
                    workdir=str(tmp_path))

    def test_the_linux_hostname_rule_does_not_apply(self, tmp_path):
        mgr = _manager(tmp_path, vms={'win_01': {'os': 'windows',
                                                 'base_image': 'golden',
                                                 'clone_hostname': 'required'}})
        # win_01 is no valid Linux hostname; only validate_guest_os_config
        # speaks for an os: windows clone
        mgr.validate_clone_identity_config()


class TestTheUnattendBlock:

    def test_it_needs_a_vm_that_boots_from_an_iso(self, tmp_path):
        mgr = _manager(tmp_path, vms={'win01': {
            'os': 'windows', 'base_image': 'golden', 'unattend': {}}})
        assert "c1.vms.win01.unattend needs a VM that boots from an ISO" in (
            _refused(mgr))

    def test_unknown_keys_are_refused(self, tmp_path):
        mgr = _manager(tmp_path, vms={'win01': _win_vm(
            unattend={'templat': 'x.j2'})})
        assert ("c1.vms.win01.unattend has unknown key(s) templat (it takes "
                "files, image_index, template, vars)" in _refused(mgr))

    def test_a_template_that_is_not_there_is_refused(self, tmp_path):
        mgr = _manager(tmp_path, vms={'win01': _win_vm(
            unattend={'template': './answer.xml.j2'})})
        assert (f"c1.vms.win01.unattend.template {tmp_path}/box/answer.xml.j2 "
                f"does not exist" in _refused(mgr))

    def test_paths_are_read_from_the_conf_dir(self, tmp_path):
        box = tmp_path / 'box'
        box.mkdir()
        (box / 'answer.xml.j2').write_text('<a/>')
        (box / 'setup.ps1').write_text('')
        mgr = _manager(tmp_path, vms={'win01': _win_vm(unattend={
            'template': 'answer.xml.j2', 'files': ['./setup.ps1']})})
        mgr.validate_guest_os_config()
        vm_info = mgr.config['clusters']['c1']['vms']['win01']
        assert mgr._answer_template(vm_info) == str(box / 'answer.xml.j2')
        assert mgr._answer_files(vm_info) == [str(box / 'setup.ps1')]

    def test_files_that_clash_are_refused(self, tmp_path):
        for sub in ('a', 'b'):
            (tmp_path / sub).mkdir()
            (tmp_path / sub / 'setup.ps1').write_text('')
        (tmp_path / 'a' / 'autounattend.xml').write_text('')
        mgr = _manager(tmp_path, vms={'win01': _win_vm(unattend={'files': [
            str(tmp_path / 'a' / 'setup.ps1'), str(tmp_path / 'b' / 'setup.ps1'),
            str(tmp_path / 'a' / 'autounattend.xml')]})})
        message = _refused(mgr)
        assert "two files are named setup.ps1" in message
        assert ("autounattend.xml is the name of a file boxman writes itself"
                in message)

    @pytest.mark.parametrize('index', [0, '1', True, 1.5])
    def test_the_image_index_is_a_positive_integer(self, tmp_path, index):
        mgr = _manager(tmp_path, vms={'win01': _win_vm(
            unattend={'image_index': index})})
        assert "c1.vms.win01.unattend.image_index must be a positive integer" in (
            _refused(mgr))

    def test_it_needs_the_local_runtime(self, tmp_path):
        mgr = _manager(tmp_path)
        mgr._runtime_name = 'docker-compose'
        assert "an unattended install needs the local runtime" in _refused(mgr)

    def test_false_is_an_install_by_hand(self, tmp_path):
        mgr = _manager(tmp_path, vms={'win01': _win_vm(unattend=False)})
        mgr.validate_guest_os_config()
        mgr.add_answer_cdroms()
        assert len(mgr.config['clusters']['c1']['vms']['win01']['cdroms']) == 1


# ---------------------------------------------------------------------------
# The answer ISO's cdrom entry
# ---------------------------------------------------------------------------

class TestTheAnswerCdrom:

    def test_it_is_an_install_only_cdrom_in_the_workdir(self, tmp_path):
        mgr = _manager(tmp_path)
        mgr.add_answer_cdroms()
        cdroms = mgr.config['clusters']['c1']['vms']['win01']['cdroms']
        assert cdroms[-1] == {
            'name': 'unattend',
            'source': str(tmp_path / 'ws' / 'bprj__demo__bprj_c1_win01-unattend.iso'),
            'install_only': True,
            ANSWER_CDROM_MARK: True,
        }
        # an install-only source the eject and the differ recognise
        assert cdroms[-1]['source'] in mgr._install_only_sources(
            mgr.config['clusters']['c1']['vms']['win01'])

    def test_it_is_added_once(self, tmp_path):
        mgr = _manager(tmp_path)
        mgr.add_answer_cdroms()
        mgr.add_answer_cdroms()
        assert len(mgr.config['clusters']['c1']['vms']['win01']['cdroms']) == 2

    def test_a_cloned_windows_vm_gets_none(self, tmp_path):
        mgr = _manager(tmp_path, vms={'win01': {'os': 'windows',
                                                'base_image': 'golden'}})
        mgr.add_answer_cdroms()
        assert 'cdroms' not in mgr.config['clusters']['c1']['vms']['win01']

    def test_the_workspace_defaults_add_it(self, tmp_path):
        mgr = _manager(tmp_path)
        del mgr.config['clusters']['c1']['workdir']
        mgr.resolve_workspace_defaults()
        cdroms = mgr.config['clusters']['c1']['vms']['win01']['cdroms']
        assert cdroms[-1]['source'] == str(
            tmp_path / 'ws' / 'c1' / 'bprj__demo__bprj_c1_win01-unattend.iso')

    def test_its_name_is_not_taken_for_a_seed(self):
        # CDROMManager leaves seed* media out of every comparison
        assert not os.path.basename(answer_iso_path('/wd', 'x')).startswith(
            'seed')


# ---------------------------------------------------------------------------
# The password, and the computer name
# ---------------------------------------------------------------------------

class TestThePassword:

    def _context(self, tmp_path, **vm):
        mgr = _manager(tmp_path, vms={'win01': _win_vm(**vm)})
        cluster = mgr.config['clusters']['c1']
        return mgr._answer_context('c1', cluster, 'win01',
                                   cluster['vms']['win01'])

    def test_a_vm_without_one_is_refused_and_told_to_use_env(self, tmp_path):
        with pytest.raises(ConfigError) as excinfo:
            self._context(tmp_path, admin_pass='')
        assert ("c1.vms.win01 is installed unattended, so it needs admin_pass"
                in str(excinfo.value))
        assert ("admin_pass: '{{ env(\"BOXMAN_WIN_ADMIN_PASS\") }}'"
                in str(excinfo.value))

    @pytest.mark.parametrize('weak', ['short1A', 'alllowercase1',
                                      'Administrator-9x', 'NoDigitsHere'])
    def test_one_windows_refuses_is_refused_without_being_named(
            self, tmp_path, weak):
        with pytest.raises(ConfigError) as excinfo:
            self._context(tmp_path, admin_pass=weak)
        assert "the admin_pass of c1.vms.win01" in str(excinfo.value)
        assert weak not in str(excinfo.value)

    def test_the_policy(self):
        assert password_problem('Abcdefg1', ('Administrator',)) is None
        assert password_problem('abcdefg1!', ('Administrator',)) is None
        assert 'shorter than 8' in password_problem('Ab1!', ())
        assert "account name 'boxadmin'" in password_problem(
            'xBoxAdmin99!', ('Administrator', 'boxadmin'))

    def test_the_vms_wins_over_the_clusters(self, tmp_path):
        mgr = _manager(tmp_path, cluster={'admin_pass': 'Cluster-pass-1'})
        cluster = mgr.config['clusters']['c1']
        context = mgr._answer_context('c1', cluster, 'win01',
                                      cluster['vms']['win01'])
        assert context['admin_pass'] == PASSWORD

    def test_a_legacy_env_reference_is_resolved(self, tmp_path, monkeypatch):
        monkeypatch.setenv('WIN_PASS_FOR_TEST', PASSWORD)
        context = self._context(tmp_path,
                                admin_pass='${env:WIN_PASS_FOR_TEST}')
        assert context['admin_pass'] == PASSWORD

    def test_an_unset_env_reference_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.delenv('WIN_PASS_FOR_TEST', raising=False)
        with pytest.raises(ConfigError, match="cannot be resolved"):
            self._context(tmp_path, admin_pass='${env:WIN_PASS_FOR_TEST}')

    @pytest.mark.parametrize('name', ['win_01', 'a-very-long-computer-name',
                                      '12345'])
    def test_a_name_windows_cannot_take_is_refused(self, tmp_path, name):
        with pytest.raises(ConfigError, match=(
                f"'{name}' is not a Windows computer name")):
            self._context(tmp_path, hostname=name)

    def test_the_context(self, tmp_path):
        assert self._context(tmp_path, admin_user='boxadmin',
                             unattend={'image_index': 2,
                                       'vars': {'timezone': 'X'}}) == {
            'admin_user': 'boxadmin', 'admin_pass': PASSWORD,
            'computer_name': 'win01', 'image_index': 2, 'vm_name': 'win01',
            'timezone': 'X'}


# ---------------------------------------------------------------------------
# The answer file
# ---------------------------------------------------------------------------

class TestTheDefaultAnswerFile:

    def test_it_renders_into_xml_with_the_values(self):
        root = _answer({})
        assert root.find('.//u:ComputerName', NS).text == 'win01'
        assert root.find('.//u:MetaData/u:Value', NS).text == '1'
        assert root.find('.//u:AdministratorPassword/u:Value', NS).text == (
            PASSWORD)
        assert root.find('.//u:AutoLogon/u:Password/u:Value', NS).text == (
            PASSWORD)

    def test_the_password_is_escaped_for_xml(self):
        text = render_answer_file(default_answer_template(), {
            'admin_user': 'Administrator', 'admin_pass': PASSWORD,
            'computer_name': 'w', 'image_index': 1, 'vm_name': 'w'})
        assert '&amp;&lt;pass&gt;' in text and PASSWORD not in text

    def test_another_admin_user_gets_an_account_in_administrators(self):
        account = _answer({}, admin_user='boxadmin').find(
            './/u:LocalAccount', NS)
        assert account.find('u:Name', NS).text == 'boxadmin'
        assert account.find('u:Group', NS).text == 'Administrators'
        assert account.find('u:Password/u:Value', NS).text == PASSWORD

    def test_administrator_gets_no_second_account(self):
        assert _answer({}, admin_user='administrator').find(
            './/u:LocalAccounts', NS) is None

    def test_the_drivers_are_looked_for_on_every_likely_cd(self):
        paths = [p.text for p in _answer({}).findall('.//u:DriverPaths//u:Path',
                                                     NS)]
        assert paths == [f'{letter}:\\{driver}\\2k22\\amd64'
                         for driver in ('viostor', 'NetKVM')
                         for letter in 'DEF']
        keys = [p.get('{http://schemas.microsoft.com/WMIConfig/2002/State}'
                      'keyValue')
                for p in _answer({}).findall('.//u:PathAndCredentials', NS)]
        assert keys == [str(i) for i in range(1, 7)]

    def test_vars_override_the_defaults(self):
        root = _answer({}, timezone='W. Europe Standard Time', locale='nl-NL',
                       virtio_os='2k25')
        assert root.find('.//u:TimeZone', NS).text == 'W. Europe Standard Time'
        assert root.find('.//u:UILanguage', NS).text == 'nl-NL'
        assert '2k25' in root.find('.//u:DriverPaths//u:Path', NS).text

    def test_it_carries_no_jinja_comment(self):
        text = render_answer_file(default_answer_template(), {
            'admin_user': 'Administrator', 'admin_pass': PASSWORD,
            'computer_name': 'w', 'image_index': 1, 'vm_name': 'w'})
        assert 'Variables (an `unattend: vars:`' not in text
        assert text.startswith('<?xml version="1.0" encoding="utf-8"?>\n'
                               '<unattend ')


class TestRenderingATemplate:

    def _render(self, tmp_path, text, **context):
        template = tmp_path / 'answer.xml.j2'
        template.write_text(text)
        return render_answer_file(str(template), context)

    def test_an_undefined_name_is_an_error(self, tmp_path):
        with pytest.raises(ConfigError, match="'computer_nam' is undefined"):
            self._render(tmp_path, '<a>{{ computer_nam }}</a>',
                         computer_name='x')

    def test_conf_ymls_env_helpers_are_there(self, tmp_path, monkeypatch):
        monkeypatch.setenv('BOXMAN_TEST_TZ', 'UTC')
        assert self._render(tmp_path, '<a>{{ env("BOXMAN_TEST_TZ") }}</a>') == (
            '<a>UTC</a>')

    def test_xml_that_is_not_well_formed_is_refused_without_its_text(
            self, tmp_path):
        with pytest.raises(ConfigError) as excinfo:
            self._render(tmp_path, '<a>{{ admin_pass | safe }}</a>',
                         admin_pass='x<y&z')
        assert "is not well-formed XML" in str(excinfo.value)
        assert 'x<y&z' not in str(excinfo.value)


# ---------------------------------------------------------------------------
# The ISO
# ---------------------------------------------------------------------------

class FakeIsoTool:
    """build_iso's runner: records each attempt and, for the tool named
    *works*, writes the files it was given into the image, one per line."""

    def __init__(self, works='genisoimage'):
        self.works = works
        self.commands: list[str] = []
        self.staged: dict[str, str] = {}

    def __call__(self, command, **_kwargs):
        import shlex
        self.commands.append(command)
        words = shlex.split(command)
        if words[0] != self.works:
            return types.SimpleNamespace(ok=False)
        image = words[words.index('-output') + 1]
        files = words[words.index('-rock') + 1:]
        self.staged = {os.path.basename(f): Path(f).read_text() for f in files}
        self.modes = {os.path.basename(f): stat.S_IMODE(os.stat(f).st_mode)
                      for f in files}
        self.stage_mode = stat.S_IMODE(os.stat(os.path.dirname(image)).st_mode)
        Path(image).write_text('\n'.join(sorted(self.staged)))
        return types.SimpleNamespace(ok=True)


class TestBuildIso:

    def test_the_tools_are_tried_in_turn(self, tmp_path):
        tool = FakeIsoTool(works='xorrisofs')
        (tmp_path / 'user-data').write_text('u')
        assert build_iso(tool, str(tmp_path / 'seed.iso'), 'cidata',
                         [str(tmp_path / 'user-data')]) == 'xorrisofs'
        assert [c.split()[0] for c in tool.commands] == [
            'genisoimage', 'mkisofs', 'xorrisofs']
        assert '-volid cidata -joliet -rock' in tool.commands[0]

    def test_none_of_them_is_none(self, tmp_path):
        assert build_iso(FakeIsoTool(works='nothing'), str(tmp_path / 'x.iso'),
                         'L', []) is None

    def test_paths_are_quoted(self, tmp_path):
        tool = FakeIsoTool(works='nothing')
        build_iso(tool, '/a b/x.iso', 'MY LABEL', ['/c d/e'])
        assert ("-output '/a b/x.iso' -volid 'MY LABEL' -joliet -rock "
                "'/c d/e'") in tool.commands[0]


class TestTheAnswerIso:

    def _write(self, tmp_path, tool):
        iso = tmp_path / 'wd' / 'vm-unattend.iso'
        with patch.object(guest_os, 'run', tool):
            write_answer_iso(str(iso), {'autounattend.xml': '<a/>',
                                        'authorized_keys': 'ssh-ed25519 K\n'},
                             default_answer_files())
        return iso

    def test_it_holds_the_answer_file_the_keys_and_the_files(self, tmp_path):
        tool = FakeIsoTool()
        self._write(tmp_path, tool)
        assert sorted(tool.staged) == ['authorized_keys', 'autounattend.xml',
                                       'boxman-setup.ps1']
        assert tool.staged['authorized_keys'] == 'ssh-ed25519 K\n'
        assert '-volid BOXMAN_ANSWER' in tool.commands[0]

    def test_nobody_else_can_read_it_at_any_point(self, tmp_path):
        tool = FakeIsoTool()
        iso = self._write(tmp_path, tool)
        assert stat.S_IMODE(os.stat(iso).st_mode) == 0o600
        assert tool.stage_mode == 0o700
        assert set(tool.modes.values()) == {0o600}

    def test_the_staging_directory_goes(self, tmp_path):
        iso = self._write(tmp_path, FakeIsoTool())
        assert os.listdir(iso.parent) == [iso.name]

    def test_a_failed_build_leaves_nothing(self, tmp_path):
        with pytest.raises(ProvisionError, match="install genisoimage"):
            self._write(tmp_path, FakeIsoTool(works='nothing'))
        assert os.listdir(tmp_path / 'wd') == []

    def test_an_earlier_one_is_replaced(self, tmp_path):
        stale = tmp_path / 'wd' / 'vm-unattend.iso'
        stale.parent.mkdir()
        stale.write_text('stale')
        self._write(tmp_path, FakeIsoTool())
        assert 'autounattend.xml' in stale.read_text()


class TestBuildingTheAnswerMedia:

    def _built(self, tmp_path, **kwargs):
        mgr = _manager(tmp_path, **kwargs)
        mgr.add_answer_cdroms()
        ws = tmp_path / 'ws'

        def keys():
            ws.mkdir(exist_ok=True)
            (ws / 'id_ed25519_boxman.pub').write_text('ssh-ed25519 CLUSTER\n')
            return True

        mgr.generate_ssh_keys = MagicMock(side_effect=keys)
        mgr.get_global_authorized_keys = MagicMock(
            return_value=['ssh-ed25519 GLOBAL'])
        tool = FakeIsoTool()
        with patch.object(guest_os, 'run', tool):
            mgr.build_answer_media()
        return mgr, tool

    def test_the_cluster_key_and_the_global_keys_go_on_it(self, tmp_path):
        mgr, tool = self._built(tmp_path)
        mgr.generate_ssh_keys.assert_called_once_with()
        assert tool.staged['authorized_keys'] == (
            'ssh-ed25519 CLUSTER\nssh-ed25519 GLOBAL\n')

    def test_the_answer_file_is_the_rendered_default(self, tmp_path):
        _mgr, tool = self._built(tmp_path)
        root = ET.fromstring(tool.staged['autounattend.xml'].encode())
        assert root.find('.//u:ComputerName', NS).text == 'win01'

    def test_it_is_where_the_cdrom_entry_says(self, tmp_path):
        mgr, _tool = self._built(tmp_path)
        source = mgr.config['clusters']['c1']['vms']['win01']['cdroms'][-1][
            'source']
        assert os.path.isfile(source)
        assert stat.S_IMODE(os.stat(source).st_mode) == 0o600

    def test_nothing_is_logged_with_the_password(self, tmp_path):
        mgr, _tool = self._built(tmp_path)
        for level in ('debug', 'info', 'warning', 'error'):
            for call in getattr(mgr.logger, level).call_args_list:
                assert PASSWORD not in str(call)
                assert '&amp;&lt;pass' not in str(call)

    def test_a_vm_that_is_not_created_gets_none(self, tmp_path):
        mgr = _manager(tmp_path)
        mgr.generate_ssh_keys = MagicMock()
        mgr.build_answer_media({'bprj__demo__bprj_c1_other'})
        mgr.generate_ssh_keys.assert_not_called()

    def test_a_workdir_in_a_git_work_tree_is_warned_about(self, tmp_path):
        (tmp_path / '.git').mkdir()
        mgr, _tool = self._built(tmp_path)
        warning = mgr.logger.warning.call_args.args[0]
        assert "is inside the git work tree" in warning
        assert str(tmp_path) in warning

    def test_provision_checks_the_password_before_anything_is_built(
            self, tmp_path):
        mgr = _manager(tmp_path, vms={'win01': _win_vm(admin_pass='weak')})
        mgr.provider = MagicMock()
        for name in ('_update_sessions_with_runtime',
                     'validate_direct_boot_config',
                     'validate_clone_identity_config', 'validate_iso_runtime'):
            setattr(mgr, name, MagicMock())
        mgr._find_existing_project_vms = MagicMock()
        with pytest.raises(ConfigError, match="admin_pass of c1.vms.win01"):
            mgr._provision(types.SimpleNamespace())
        mgr._find_existing_project_vms.assert_not_called()


# ---------------------------------------------------------------------------
# Removing it
# ---------------------------------------------------------------------------

class TestRemovingIt:

    def _with_iso(self, tmp_path, vms=None):
        mgr = _manager(tmp_path, vms=vms)
        mgr.add_answer_cdroms()
        iso = Path(answer_iso_path(str(tmp_path / 'ws'),
                                   'bprj__demo__bprj_c1_win01'))
        iso.parent.mkdir(parents=True, exist_ok=True)
        iso.write_text('password inside')
        return mgr, iso

    def test_it_goes_with_the_vm(self, tmp_path):
        mgr, iso = self._with_iso(tmp_path)
        mgr._teardown_vm = MagicMock()
        mgr.session_for_cluster = MagicMock()
        cluster = mgr.config['clusters']['c1']
        mgr._destroy_vm_and_disks('c1', cluster, 'win01',
                                  cluster['vms']['win01'])
        assert not iso.exists()

    def test_not_if_the_domain_could_not_be_confirmed_gone(self, tmp_path):
        mgr, iso = self._with_iso(tmp_path)
        mgr._teardown_vm = MagicMock(side_effect=ProvisionError('still there'))
        mgr.session_for_cluster = MagicMock()
        cluster = mgr.config['clusters']['c1']
        with pytest.raises(ProvisionError):
            mgr._destroy_vm_and_disks('c1', cluster, 'win01',
                                      cluster['vms']['win01'])
        assert iso.exists()

    def test_a_vm_removed_from_the_config_loses_it_too(self, tmp_path):
        mgr, iso = self._with_iso(tmp_path)
        mgr._teardown_vm = MagicMock()
        mgr.provider = MagicMock()
        mgr._destroy_removed_vm('bprj__demo__bprj_c1_win01')
        assert not iso.exists()

    def test_an_unsearchable_workdir_is_only_warned_about_for_one(
            self, tmp_path):
        mgr, _iso = self._with_iso(tmp_path)
        with patch('os.lstat', side_effect=PermissionError(13, 'Permission denied')):
            assert mgr.remove_answer_media('x', [str(tmp_path)],
                                           strict=False) == []
            with pytest.raises(ProvisionError, match="could not look up"):
                mgr.remove_answer_media('x', [str(tmp_path)])
        assert "remove it by hand" in mgr.logger.warning.call_args.args[0]

    def test_one_that_cannot_be_removed_fails(self, tmp_path):
        mgr, iso = self._with_iso(tmp_path)
        with patch('os.remove', side_effect=PermissionError(13, 'Permission denied')), \
                pytest.raises(ProvisionError, match="holds the Administrator"):
            mgr.remove_answer_media('bprj__demo__bprj_c1_win01',
                                    [str(tmp_path / 'ws')])

    def _eject(self, mgr, outcome, dry_run=False):
        session = MagicMock()
        session.eject_install_media.return_value = outcome
        session.virsh_invocation.side_effect = (
            lambda *args: 'virsh ' + ' '.join(args))
        mgr.session_for_cluster = MagicMock(return_value=session)
        return mgr.eject_install_media(dry_run=dry_run)

    def test_it_goes_once_its_drive_is_ejected(self, tmp_path):
        mgr, iso = self._with_iso(tmp_path)
        assert self._eject(mgr, InstallMediaOutcome(
            held=[('sdc', str(iso))], ejected=[('sdc', str(iso))])) == []
        assert not iso.exists()

    def test_it_goes_when_no_drive_holds_it_any_more(self, tmp_path):
        mgr, iso = self._with_iso(tmp_path)
        self._eject(mgr, InstallMediaOutcome())
        assert not iso.exists()

    def test_it_stays_while_the_install_runs(self, tmp_path):
        mgr, iso = self._with_iso(tmp_path)
        self._eject(mgr, InstallMediaOutcome(held=[('sdc', str(iso))],
                                             waiting='its agent does not answer'))
        assert iso.exists()

    def test_it_stays_when_its_eject_failed(self, tmp_path):
        mgr, iso = self._with_iso(tmp_path)
        self._eject(mgr, InstallMediaOutcome(
            held=[('sdc', str(iso))], failed=[('sdc', str(iso), 'locked')]))
        assert iso.exists()

    def test_a_dry_run_keeps_it(self, tmp_path):
        mgr, iso = self._with_iso(tmp_path)
        self._eject(mgr, InstallMediaOutcome(
            held=[('sdc', str(iso))], would_eject=[('sdc', str(iso))]),
            dry_run=True)
        assert iso.exists()


# ---------------------------------------------------------------------------
# ssh, ssh_config and the inventory
# ---------------------------------------------------------------------------

class TestSshToAWindowsVm:

    def _lab(self, tmp_path, admin_pass='secret', cluster_user=None, **vm):
        lab = Lab(tmp_path, vms=('win01', 'vm02'), admin_pass=admin_pass)
        cluster = lab.mgr.config['clusters']['cluster_1']
        # Lab's cluster says admin_user: admin, for its Linux VMs
        if cluster_user is None:
            del cluster['admin_user']
        else:
            cluster['admin_user'] = cluster_user
        cluster['vms']['win01'].update({'os': 'windows', **vm})
        lab.addresses = {full('win01'): '192.168.10.5',
                         full('vm02'): '192.168.10.6'}
        return lab, cluster

    def test_its_ssh_config_user_is_administrator(self, tmp_path):
        lab, _cluster = self._lab(tmp_path)
        lab.mgr.write_ssh_config()
        assert '    User Administrator' in lab.block('cluster_1_win01')
        # the Linux VM beside it keeps the default it always had
        assert '    User admin' in lab.block('cluster_1_vm02')

    def test_the_clusters_admin_user_is_its_too(self, tmp_path):
        # one name for the cluster's VMs: the answer file creates it
        lab, _cluster = self._lab(tmp_path, cluster_user='ops')
        lab.mgr.write_ssh_config()
        assert '    User ops' in lab.block('cluster_1_win01')
        assert '    User ops' in lab.block('cluster_1_vm02')

    def test_its_own_admin_user_wins(self, tmp_path):
        lab, _cluster = self._lab(tmp_path, cluster_user='ops',
                                  admin_user='boxadmin')
        lab.mgr.write_ssh_config()
        assert '    User boxadmin' in lab.block('cluster_1_win01')

    def test_the_identity_file_is_the_cluster_key(self, tmp_path):
        lab, _cluster = self._lab(tmp_path)
        lab.mgr.write_ssh_config()
        assert f'IdentityFile "{tmp_path}/id_ed25519_boxman"' in lab.block(
            'cluster_1_win01')

    def test_no_key_is_pushed_to_it_with_ssh_copy_id(self, tmp_path):
        lab, _cluster = self._lab(tmp_path)
        assert lab.mgr._push_ssh_keys(vm_states=dict(lab.states)) == []
        assert lab.pushed() == ['cluster_1_vm02']
        assert any("it runs Windows, and its key is installed from its "
                   "answer ISO" in line for line in lab.logged('info'))

    def test_a_windows_only_cluster_misses_no_admin_pass(self, tmp_path):
        lab = Lab(tmp_path, vms=('win01',), admin_pass=None)
        lab.mgr.config['clusters']['cluster_1']['vms']['win01']['os'] = (
            'windows')
        assert lab.mgr._push_ssh_keys(vm_states=dict(lab.states)) == []
        assert not any('has no admin_pass' in line
                       for line in lab.logged('warning'))

    def test_the_connection_info_names_its_user(self, tmp_path):
        lab, _cluster = self._lab(tmp_path)
        lab.mgr.connect_info()
        lines = [c.args[0] for c in lab.mgr.logger.status.call_args_list]
        assert any(line.endswith('Administrator@192.168.10.5')
                   for line in lines)

    def test_its_inventory_host_speaks_powershell(self, tmp_path):
        mgr = make_bare_manager({
            'project': 'demo', 'workspace': {'path': str(tmp_path)},
            'clusters': {'c1': {'vms': {'win01': {'os': 'windows'},
                                        'vm02': {}}}}})
        mgr._is_compose_cluster = lambda name: False
        mgr.resolve_workspace_defaults()
        inventory = yaml.safe_load(
            mgr.config['workspace']['files']['inventory/01-hosts.yml'])
        hosts = inventory['all']['hosts']
        assert hosts['c1_win01']['ansible_shell_type'] == 'powershell'
        assert 'ansible_connection' not in hosts['c1_win01']
        assert 'ansible_shell_type' not in hosts['c1_vm02']
        # still the VM boxman ssh goes to by default
        assert 'GATEWAYHOST=c1_win01' in mgr.config['workspace']['files'][
            'env.sh']


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------

class TestTheClock:
    """virt-install gives a Windows guest <clock offset='localtime'>, so
    the RTC held the host's local time; Windows, told its zone was UTC,
    read it as UTC and ran an hour ahead on a CET host."""

    def _virt_install(self, tmp_path, **info):
        from boxman.providers.libvirt.iso_boot_vm import IsoBootVM
        vm = IsoBootVM('win01', {'memory': 4096, 'vcpus': 2, **info},
                       {'use_sudo': False, 'uri': 'qemu:///system'},
                       str(tmp_path), '/iso/win.iso')
        with patch('boxman.providers.libvirt.direct_vm._shell_run') as run:
            run.return_value = types.SimpleNamespace(ok=True, stdout='',
                                                     stderr='')
            vm.create()
        return run.call_args_list[-1].args[0]

    def test_a_windows_domain_gets_a_utc_rtc(self, tmp_path):
        assert '--clock=offset=utc' in self._virt_install(tmp_path,
                                                          os='windows')

    def test_a_linux_one_keeps_virt_installs_choice(self, tmp_path):
        assert '--clock' not in self._virt_install(tmp_path)

    def test_a_clock_of_the_boxs_own_is_kept(self, tmp_path):
        command = self._virt_install(
            tmp_path, os='windows',
            virt_install_extra_args=['--clock=offset=localtime'])
        assert '--clock=offset=utc' not in command
        assert '--clock=offset=localtime' in command

    def test_the_answer_file_tells_windows_the_rtc_is_utc(self):
        commands = [c.text for c in _answer({}).findall(
            './/u:settings[@pass="specialize"]//u:RunSynchronousCommand/u:Path',
            NS)]
        assert commands == [
            'reg add HKLM\\SYSTEM\\CurrentControlSet\\Control\\'
            'TimeZoneInformation /v RealTimeIsUniversal /t REG_DWORD /d 1 /f']
