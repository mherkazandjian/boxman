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
        # the spelling as written is kept, for naming what an earlier
        # boxman left where the kernel resolves it
        assert mgr._workdirs_as_written == {str(lexical): written}

    def test_an_explicit_cluster_workdir_is_respelled(self, tmp_path):
        written, lexical, _kernel = _split(tmp_path)
        config = _config(str(tmp_path / 'unused'), workdir=f"{written}/c1")

        mgr = _loaded(config)

        assert config['clusters']['cluster_1']['workdir'] == str(
            lexical / 'c1')
        assert mgr._workdirs_as_written[str(lexical / 'c1')] == (
            f"{written}/c1")

    def test_a_path_already_in_its_spelling_records_nothing(self, tmp_path):
        mgr = _loaded(_config(str(tmp_path / 'ws')))

        assert mgr._workdirs_as_written == {}


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
    """A workspace made before the fix has its key pair and ssh_config where
    the kernel resolves the spelling as written. They are named at teardown,
    not removed: that directory is outside the workspace, and may be
    another project's."""

    def _destroy_files(self, tmp_path):
        written, lexical, kernel = _split(tmp_path)
        old = [_file(kernel / name, b'old') for name in
               ('id_ed25519_boxman', 'id_ed25519_boxman.pub', 'ssh_config')]
        new = [_file(lexical / name, b'new') for name in
               ('id_ed25519_boxman', 'id_ed25519_boxman.pub', 'ssh_config')]
        mgr = _loaded(_config(written))
        mgr.deprovision_files()
        warnings = [str(c.args[0]) for c in mgr.logger.warning.call_args_list]
        return written, lexical, old, new, warnings

    def test_the_old_ones_are_named_once_and_kept(self, tmp_path):
        written, lexical, old, new, warnings = self._destroy_files(tmp_path)

        assert not any(path.exists() for path in new)
        assert all(path.read_bytes() == b'old' for path in old)
        assert len(warnings) == 3
        for path, warning in zip(old, warnings, strict=True):
            assert warning.startswith(f"left {written}/{path.name} in place")
            assert str(lexical) in warning

    def test_nothing_is_named_when_there_is_no_split(self, tmp_path):
        ws = tmp_path / 'ws'
        _file(ws / 'id_ed25519_boxman')
        mgr = _loaded(_config(f"{ws}/./"))

        mgr.deprovision_files()

        assert mgr.logger.warning.call_args_list == []
        assert not (ws / 'id_ed25519_boxman').exists()
