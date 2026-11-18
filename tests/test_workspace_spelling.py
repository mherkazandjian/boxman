"""
One spelling of the workspace for everything boxman writes and records
(#294 finding 4).

``workspace.path: <ws>/link/../vms``, with ``<ws>/link`` a symlink to
``<other>/deep``, names two directories: ``<ws>/vms`` with ``..`` folded as
written, ``<other>/vms`` where the kernel resolves it. The cluster files,
the disks and the pools went to the first, the SSH key pair and
``ssh_config`` to the second; the teardown looked for the boot disk in the
second and kept both disks, the data disk as "outside every cluster
workdir", the boot disk without a word.

Regression: the workspace path and each explicit cluster workdir are
respelled once, as the config is loaded (``workdir_spelling``), and every
writer and the teardown use that spelling.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.manager_parts.workspace import workdir_spelling
from boxman.providers.libvirt import disk_cleanup
from conftest import make_bare_manager
from test_vm_teardown_storage import VM, _file, _inventory, _record, _Teardown

pytestmark = [pytest.mark.unit, pytest.mark.regression]


def _split(tmp_path):
    """``<ws>/link -> <other>/deep``; returns the spelling as written, the
    directory it names with ``..`` folded as written, and the one the
    kernel resolves it to."""
    root = Path(os.path.realpath(tmp_path))
    (root / 'other' / 'deep').mkdir(parents=True)
    (root / 'ws').mkdir()
    (root / 'ws' / 'link').symlink_to(root / 'other' / 'deep',
                                      target_is_directory=True)
    written = f"{root}/ws/link/../vms"
    return written, root / 'ws' / 'vms', root / 'other' / 'vms'


def _config(workspace_path, **cluster):
    return {
        'project': 'demo',
        'workspace': {'path': workspace_path},
        'clusters': {'cluster_1': {
            'vms': {'web': {'disks': [
                {'name': 'disk01', 'target': 'vdb', 'size': 1024}]}},
            **cluster}},
    }


def _loaded(config):
    mgr = make_bare_manager(config)
    mgr._runtime_name = 'local'
    mgr.resolve_workspace_defaults()
    return mgr


class TestTheSpelling:

    def test_dotdot_is_folded_as_written_not_through_the_link(self, tmp_path):
        written, lexical, kernel = _split(tmp_path)

        assert workdir_spelling(written) == str(lexical)
        # what the kernel would open is another directory
        assert os.path.realpath(written) == str(kernel)

    def test_tilde_is_expanded_and_a_relative_path_made_absolute(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv('HOME', str(tmp_path / 'home'))
        monkeypatch.chdir(tmp_path)

        assert workdir_spelling('~/ws/./vms/') == str(tmp_path / 'home/ws/vms')
        assert workdir_spelling('rel/vms') == str(tmp_path / 'rel/vms')

    def test_a_plain_absolute_path_is_unchanged(self):
        assert workdir_spelling('/srv/boxman/ws') == '/srv/boxman/ws'


class TestDecidedOnceAtLoad:

    def test_the_workspace_and_derived_workdir_are_respelled(self, tmp_path):
        written, lexical, _kernel = _split(tmp_path)
        config = _config(written)

        mgr = _loaded(config)

        assert config['workspace']['path'] == str(lexical)
        assert config['clusters']['cluster_1']['workdir'] == str(
            lexical / 'cluster_1')
        assert mgr._cluster_workdirs() == [str(lexical / 'cluster_1')]
        # the spellings as written are kept, for what an earlier boxman
        # left where the kernel resolves them
        assert mgr._workdirs_as_written == {
            str(lexical): [written],
            str(lexical / 'cluster_1'): [f"{written}/cluster_1"]}

    def test_an_explicit_cluster_workdir_is_respelled(self, tmp_path):
        written, lexical, _kernel = _split(tmp_path)
        config = _config(str(tmp_path / 'unused'), workdir=f"{written}/c1")

        mgr = _loaded(config)

        assert config['clusters']['cluster_1']['workdir'] == str(
            lexical / 'c1')
        assert mgr._workdirs_as_written[str(lexical / 'c1')] == [
            f"{written}/c1"]

    def test_a_path_already_in_its_spelling_records_nothing(self, tmp_path):
        mgr = _loaded(_config(str(tmp_path / 'ws')))

        assert mgr._workdirs_as_written == {}

    def test_two_spellings_of_one_directory_are_both_kept(self, tmp_path):
        """workspace.path and a cluster workdir that settle to one path:
        neither spelling as written hides the other's."""
        written, lexical, _kernel = _split(tmp_path)
        root = lexical.parent.parent
        mgr = _loaded(_config(written, workdir=f"{root}/ws/./vms"))

        assert mgr._workdirs_as_written[str(lexical)] == [
            written, f"{root}/ws/./vms"]


class TestEveryWriterUsesIt:

    def test_workspace_and_cluster_files(self, tmp_path):
        written, lexical, kernel = _split(tmp_path)
        mgr = _loaded(_config(written))

        mgr.provision_files()

        for name in ('env.sh', 'ansible.cfg', 'inventory/01-hosts.yml',
                     'cluster_1/inventory/01-hosts.yml'):
            assert (lexical / name).is_file(), name
        assert not kernel.exists()
        assert f"{lexical}/.ansible_facts" in (
            lexical / 'ansible.cfg').read_text()

    def test_the_ssh_key_pair(self, tmp_path):
        written, lexical, kernel = _split(tmp_path)
        mgr = _loaded(_config(written))
        made = []

        def keygen(cmd, **_kw):
            path = cmd.split(' -f ')[1].split(' ')[0].strip("'")
            made.append(path)
            Path(path).write_text('private')
            Path(f"{path}.pub").write_text('public')
            return MagicMock(ok=True)

        with patch('boxman.manager_parts.ssh.run', side_effect=keygen):
            assert mgr.generate_ssh_keys()

        assert made == [str(lexical / 'id_ed25519_boxman')]
        assert (lexical / 'id_ed25519_boxman.pub').is_file()
        assert not kernel.exists()

    def test_ssh_config(self, tmp_path):
        written, lexical, _kernel = _split(tmp_path)
        mgr = _loaded(_config(written))

        assert list(mgr._ssh_config_groups()) == [str(lexical / 'ssh_config')]


class TestTheTeardown:
    """The real per-VM teardown, with the VM's disks where libvirt has them
    -- spelled with ``..`` folded, as ``libvirt_disk_source`` and
    virt-install spell them."""

    @staticmethod
    def _teardown(tmp_path, workdir_as_loaded, records):
        written, lexical, _kernel = _split(tmp_path)
        boot = _file(lexical / 'cluster_1' / f'{VM}.qcow2')
        data = _file(lexical / 'cluster_1' / f'{VM}_disk01.qcow2')
        t = _Teardown(lexical / 'cluster_1', disks=[boot, data],
                      records=records(data))
        config = _config(written)
        if workdir_as_loaded:
            t.mgr.config = config
            t.mgr.resolve_workspace_defaults()
        else:
            # as before the fix: the workdir as written
            config['clusters']['cluster_1']['workdir'] = f"{written}/cluster_1"
            t.mgr.config = config
        saved = []
        save = t.mgr._save_teardown_inventory

        def keep_a_copy(inventory, save_dir):
            save(inventory, save_dir)
            with open(inventory.saved_at) as fobj:
                saved.append(json.load(fobj))

        t.mgr._save_teardown_inventory = keep_a_copy
        return t, config, boot, data, lexical, saved

    def _deprovision(self, t, config):
        cluster = config['clusters']['cluster_1']
        t.mgr._destroy_vm_and_disks('cluster_1', cluster, 'web',
                                    cluster['vms']['web'])

    @pytest.mark.parametrize('records', [
        pytest.param(lambda data: [_record('disk01', data)], id='recorded'),
        pytest.param(lambda data: None, id='legacy-by-name'),
    ])
    def test_both_disks_are_removed(self, tmp_path, records):
        t, config, boot, data, lexical, saved = self._teardown(
            tmp_path, True, records)

        self._deprovision(t, config)

        assert not boot.exists() and not data.exists()
        assert t.warnings == ''
        # the saved inventory spells every path one way
        (inventory,) = saved
        paths = [*inventory['disk_sources'], *inventory['boot_family'],
                 *(inventory['legacy_disks'] or []),
                 *(r['source'] for r in inventory['records'] or [])]
        assert paths and all(
            p.startswith(f"{lexical}/cluster_1/") for p in paths), paths
        if inventory['legacy_disks'] is not None:
            assert inventory['legacy_disks'] == [str(data)]

    def test_before_the_fix_both_were_kept_now_each_is_named(self, tmp_path):
        """The workdir as written (``_destroy_removed_vm`` of a config
        respelled since would see the same): both disks stay, and each is
        named -- the boot disk was kept without a word."""
        t, config, boot, data, _lexical, _saved = self._teardown(
            tmp_path, False, lambda data: [_record('disk01', data)])

        self._deprovision(t, config)

        assert boot.exists() and data.exists()
        assert f"left {boot} in place because {disk_cleanup._OUTSIDE}" in (
            t.warnings)
        assert f"left {data} in place because {disk_cleanup._OUTSIDE}" in (
            t.warnings)


class TestABootDiskNoRuleDecided:

    def test_one_in_a_workdir_not_listed_is_named(self, tmp_path):
        workdir = tmp_path / 'wd'
        boot = _file(workdir / f'{VM}.qcow2')
        inventory = _inventory(
            disk_sources=[str(boot)],
            identities=disk_cleanup.file_identities([str(boot)]))

        outcome = disk_cleanup.remove_vm_storage(
            inventory, [str(workdir)], lambda: {})

        assert outcome.removed == []
        assert outcome.kept == [(str(boot), disk_cleanup._NOT_LISTED)]
        assert boot.exists()

    def test_one_outside_every_workdir_is_named_so(self, tmp_path):
        boot = _file(tmp_path / 'elsewhere' / f'{VM}.qcow2')
        inventory = _inventory(
            disk_sources=[str(boot)],
            identities=disk_cleanup.file_identities([str(boot)]))

        outcome = disk_cleanup.remove_vm_storage(
            inventory, [str(tmp_path / 'wd')], lambda: {})

        assert outcome.kept == [(str(boot), disk_cleanup._OUTSIDE)]

    def test_one_listed_is_removed_as_before(self, tmp_path):
        workdir = tmp_path / 'wd'
        boot = _file(workdir / f'{VM}.qcow2')
        inventory = _inventory(
            disk_sources=[str(boot)], boot_family=[str(boot)],
            identities=disk_cleanup.file_identities([str(boot)]))

        outcome = disk_cleanup.remove_vm_storage(
            inventory, [str(workdir)], lambda: {})

        assert outcome.removed == [str(boot)] and outcome.kept == []

    def test_a_legacy_disk_under_its_names_is_still_removed(self, tmp_path):
        """A config-declared disk whose name folds to ``<vm>.<x>``
        (``x/../<vm>.data``) is admitted by the legacy route (3); the
        fallback runs after it and leaves it to that route."""
        workdir = tmp_path / 'wd'
        disk = _file(workdir / f'{VM}.data.qcow2')
        inventory = _inventory(
            disk_sources=[str(disk)], boot_family=[str(disk)],
            legacy_disks=[str(disk)],
            identities=disk_cleanup.file_identities([str(disk)]))

        outcome = disk_cleanup.remove_vm_storage(
            inventory, [str(workdir)], lambda: {})

        assert outcome.removed == [str(disk)] and outcome.kept == []

    def test_another_vms_disk_is_not_its_to_name(self, tmp_path):
        """Only a disk under its own names: an adopted or attached one is
        decided by the extra-disk rules."""
        workdir = tmp_path / 'wd'
        other = _file(workdir / 'someone-else.qcow2')
        inventory = _inventory(
            disk_sources=[str(other)],
            records_state=disk_cleanup.RECORDS_PRESENT, records=[],
            identities=disk_cleanup.file_identities([str(other)]))

        outcome = disk_cleanup.remove_vm_storage(
            inventory, [str(workdir)], lambda: {})

        assert [path for path, _ in outcome.kept] == [str(other)]
        assert outcome.kept[0][1] != disk_cleanup._NOT_LISTED


class TestAWorkspaceSplitByAnEarlierBoxman:
    """A workspace made before the fix has its key pair and ssh_config (a
    compose cluster: its docker-compose.yml and snapshots.json) where the
    kernel resolves the spelling as written. Loading the config copies each
    missing one into the directory boxman now uses, if that exists; the
    originals stay, and are named at teardown, not removed: that directory
    is outside the workspace, and may be another project's."""

    SSH = ('id_ed25519_boxman', 'id_ed25519_boxman.pub', 'ssh_config')

    def _made_before_the_fix(self, tmp_path, new_dir=True):
        written, lexical, kernel = _split(tmp_path)
        old = [_file(kernel / name, f'old {name}'.encode()) for name in
               self.SSH]
        os.chmod(old[0], 0o600)
        if new_dir:
            _file(lexical / 'cluster_1' / f'{VM}.qcow2')  # its disks
        return written, lexical, kernel, old

    @staticmethod
    def _warnings(mgr):
        return [str(c.args[0]) for c in mgr.logger.warning.call_args_list]

    def test_loading_copies_the_key_pair_and_ssh_config(self, tmp_path):
        written, lexical, kernel, old = self._made_before_the_fix(tmp_path)

        mgr = _loaded(_config(written))

        for path in old:
            new = lexical / path.name
            assert new.read_bytes() == path.read_bytes()
            assert path.exists()
            assert (os.stat(new).st_mode & 0o777) == (
                os.stat(path).st_mode & 0o777)
        assert (os.stat(lexical / 'id_ed25519_boxman').st_mode & 0o777) == 0o600
        warnings = self._warnings(mgr)
        assert len(warnings) == 3
        assert all(w.startswith(f"copied {written}/") for w in warnings)

    def test_the_copied_key_pair_is_the_one_used(self, tmp_path):
        """``up`` on running VMs only rewrites ssh_config: the IdentityFile
        it names is the key the VMs trust, not a missing one."""
        written, lexical, _kernel, old = self._made_before_the_fix(tmp_path)
        mgr = _loaded(_config(written))

        with patch('boxman.manager_parts.ssh.run') as keygen:
            assert mgr.generate_ssh_keys()

        keygen.assert_not_called()
        assert (lexical / 'id_ed25519_boxman').read_bytes() == old[0].read_bytes()

    def test_a_file_already_there_is_never_overwritten(self, tmp_path):
        written, lexical, _kernel, _old = self._made_before_the_fix(tmp_path)
        _file(lexical / 'id_ed25519_boxman', b'mine')

        mgr = _loaded(_config(written))

        assert (lexical / 'id_ed25519_boxman').read_bytes() == b'mine'
        assert not any('id_ed25519_boxman ' in w or
                       w.startswith(f"copied {written}/id_ed25519_boxman to")
                       for w in self._warnings(mgr))

    def test_nothing_is_copied_into_a_project_not_made_yet(self, tmp_path):
        written, lexical, _kernel, _old = self._made_before_the_fix(
            tmp_path, new_dir=False)

        mgr = _loaded(_config(written))

        assert not lexical.exists()
        assert self._warnings(mgr) == []

    def test_a_compose_clusters_files_are_copied(self, tmp_path):
        written, lexical, kernel = _split(tmp_path)
        old = [_file(kernel / 'dc1' / name, f'old {name}'.encode())
               for name in ('docker-compose.yml', 'snapshots.json')]
        (lexical / 'dc1').mkdir(parents=True)
        config = {'project': 'demo', 'workspace': {'path': written},
                  'clusters': {'dc1': {'provider': 'docker-compose',
                                       'boxes': {'web': {'image': 'nginx'}}}}}

        _loaded(config)

        assert config['clusters']['dc1']['workdir'] == str(lexical / 'dc1')
        for path in old:
            # where the compose session's teardown looks for it
            assert (lexical / 'dc1' / path.name).read_bytes() == (
                path.read_bytes())

    def test_teardown_names_the_originals_once_and_keeps_them(self, tmp_path):
        written, lexical, kernel, old = self._made_before_the_fix(tmp_path)
        mgr = _loaded(_config(written))
        mgr.logger.reset_mock()

        mgr.deprovision_files()

        assert not any((lexical / name).exists() for name in self.SSH)
        assert all(path.exists() for path in old)
        warnings = self._warnings(mgr)
        assert len(warnings) == 3
        for path, warning in zip(old, warnings, strict=True):
            assert warning.startswith(
                f"left {written}/{path.name} ({kernel / path.name}) in place")
            assert str(lexical) in warning

    def test_two_spellings_of_one_directory_still_name_them(self, tmp_path):
        written, lexical, _kernel, old = self._made_before_the_fix(tmp_path)
        root = lexical.parent.parent
        mgr = _loaded(_config(written, workdir=f"{root}/ws/./vms"))
        mgr.logger.reset_mock()

        mgr.deprovision_files()

        assert len(self._warnings(mgr)) == 3

    def test_nothing_is_named_when_there_is_no_split(self, tmp_path):
        ws = tmp_path / 'ws'
        _file(ws / 'id_ed25519_boxman')
        mgr = _loaded(_config(f"{ws}/./"))

        mgr.deprovision_files()

        assert mgr.logger.warning.call_args_list == []
        assert not (ws / 'id_ed25519_boxman').exists()


class TestTheUpgradeCopyIsCareful:
    """Second review of the upgrade copy (#294 finding 4)."""

    def test_a_symlinked_key_is_copied_through_the_link(self, tmp_path):
        """generate_ssh_keys() used a symlinked key as it was; a new key
        made in its place would lock the running VMs out."""
        written, lexical, kernel = _split(tmp_path)
        real_key = _file(tmp_path / 'keys' / 'real', b'the trusted key')
        os.chmod(real_key, 0o600)
        kernel.mkdir(parents=True)
        (kernel / 'id_ed25519_boxman').symlink_to(real_key)
        _file(lexical / 'cluster_1' / f'{VM}.qcow2')

        _loaded(_config(written))

        copy = lexical / 'id_ed25519_boxman'
        assert not copy.is_symlink()
        assert copy.read_bytes() == b'the trusted key'
        assert (os.stat(copy).st_mode & 0o777) == 0o600

    def test_a_compose_cluster_dir_not_there_yet_is_made(self, tmp_path):
        """A compose cluster all of whose services come from compose_extra
        left nothing in its new workdir; the workspace says it was made."""
        written, lexical, kernel = _split(tmp_path)
        manifest = _file(kernel / 'dc1' / 'docker-compose.yml', b'services: {}')
        _file(lexical / 'env.sh')
        config = {'project': 'demo', 'workspace': {'path': written},
                  'clusters': {'dc1': {'provider': 'docker-compose',
                                       'boxes': {'web': {'image': 'nginx'}}}}}

        _loaded(config)

        assert (lexical / 'dc1' / 'docker-compose.yml').read_bytes() == (
            manifest.read_bytes())

    def test_a_failed_copy_leaves_nothing_under_the_name(self, tmp_path,
                                                         monkeypatch):
        written, lexical, kernel = _split(tmp_path)
        _file(kernel / 'id_ed25519_boxman', b'the trusted key')
        _file(lexical / 'cluster_1' / f'{VM}.qcow2')

        def full(_fd):
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(os, 'fsync', full)
        mgr = _loaded(_config(written))

        assert sorted(p.name for p in lexical.iterdir()) == ['cluster_1']
        assert any(str(c.args[0]).startswith('could not copy')
                   for c in mgr.logger.warning.call_args_list)

        monkeypatch.undo()
        _loaded(_config(written))

        assert (lexical / 'id_ed25519_boxman').read_bytes() == (
            b'the trusted key')
        assert not list(lexical.glob('.boxman-copy.*'))

    def test_a_staging_file_already_there_is_left_alone(self, tmp_path):
        """A name a killed copy left (or another process uses) is neither
        reused nor removed: the copy stages under a name of its own."""
        written, lexical, kernel = _split(tmp_path)
        _file(kernel / 'id_ed25519_boxman', b'the trusted key')
        _file(lexical / 'cluster_1' / f'{VM}.qcow2')
        other = _file(lexical / f'.boxman-copy.{os.getpid()}.id_ed25519_boxman',
                      b'not ours')

        _loaded(_config(written))

        assert other.read_bytes() == b'not ours'
        assert (lexical / 'id_ed25519_boxman').read_bytes() == (
            b'the trusted key')
        assert [p.name for p in lexical.glob('.boxman-copy.*')] == [other.name]


class TestMemoryImagesOfASplitWorkspace:
    """A boxman before the fix wrote snapshot memory images to the workdir
    as written, where the kernel resolves it. The teardown looks there as in
    a memory_snapshot_dir: the ones its snapshots recorded go, any other
    named as its memory image is named."""

    def test_recorded_ones_are_removed_and_others_named(self, tmp_path):
        written, lexical, kernel = _split(tmp_path)
        boot = _file(lexical / 'cluster_1' / f'{VM}.qcow2')
        recorded = _file(kernel / 'cluster_1' / f'{VM}_snapshot_s1.raw')
        unrecorded = _file(kernel / 'cluster_1' / f'{VM}_snapshot_s0.raw')
        t = _Teardown(lexical / 'cluster_1', disks=[boot], records=[],
                      memory_images={'s1': str(recorded)})
        config = _config(written)
        t.mgr.config = config
        t.mgr.resolve_workspace_defaults()

        cluster = config['clusters']['cluster_1']
        t.mgr._destroy_vm_and_disks('cluster_1', cluster, 'web',
                                    cluster['vms']['web'])

        assert not boot.exists() and not recorded.exists()
        assert unrecorded.exists()
        assert (f"left {written}/cluster_1/{unrecorded.name} in place "
                f"because none of its snapshots") in t.warnings
        assert str(recorded) not in t.warnings

    def test_a_fresh_project_has_no_such_directory(self, tmp_path):
        written, lexical, _kernel = _split(tmp_path)
        mgr = _loaded(_config(written))

        # named, but absent: a teardown finds nothing there and says nothing
        assert mgr._split_workdirs() == [f"{written}/cluster_1"]

