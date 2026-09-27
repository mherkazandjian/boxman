"""
The storage-removal routine shared by every VM teardown (#208).

``deprovision`` / ``destroy`` / ``provision --force`` / ``up --force`` reach
it through ``_destroy_vm_and_disks``, ``update`` through
``_destroy_removed_vm``. libvirt deletes nothing any more; these tests drive
the routine with real files in a temporary workdir and a mocked session.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from boxman import config_cache
from boxman.exceptions import ProvisionError
from boxman.providers.libvirt import disk_cleanup
from boxman.providers.libvirt.disk_ownership import (
    ROLE_ADOPTED,
    ROLE_DATA,
    DiskRecord,
)
from boxman.providers.libvirt.session import LibVirtSession
from boxman.providers.libvirt.virsh_parse import DomblkRow
from conftest import make_bare_manager

pytestmark = pytest.mark.unit

VM = 'bprj__demo__bprj_cluster_1_web'
OTHER = 'bprj__demo__bprj_cluster_1_db'


def _file(path, content=b'data'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _record(name, path, role=ROLE_DATA, target='vdb'):
    return DiskRecord(name=name, target=target, role=role, source=str(path))


#: default for ``_Teardown(chains=...)``: every disk that exists a
#: standalone image; one that does not is left out, as the host's answer
#: leaves out a source confirmed absent
STANDALONE = object()


def _live(table):
    """``backing_chains`` as the host would answer it now: each requested
    source that exists, with its chain from *table* (standalone when not
    listed); a source that does not exist is left out (confirmed absent),
    and a chain with a missing layer cannot be read."""
    table = {str(k): [str(layer) for layer in v] for k, v in table.items()}

    def chains(sources):
        answer = {}
        for source in map(str, sources):
            if not os.path.lexists(source):
                continue
            layers = table.get(source, [source])
            if not all(os.path.lexists(layer) for layer in layers):
                return None
            answer[source] = layers
        return answer
    return chains


needs_qemu_img = pytest.mark.skipif(shutil.which('qemu-img') is None,
                                    reason='needs qemu-img')

#: a directory mode 000 stops only a non-root lookup
unless_root = pytest.mark.skipif(os.geteuid() == 0,
                                 reason='root searches a mode-000 directory')


def _link(target, path):
    """*path* made another name of the file *target* (a hard link): the
    same file, which no resolved path can tell is the same."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.link(target, path)
    return path


def _virsh_result(stdout='', ok=True):
    result = MagicMock(name='invoke.Result')
    result.stdout, result.stderr, result.ok = stdout, '', ok
    return result


def _image(path, backing=None):
    """A real qcow2 image, built on *backing* when given."""
    path.parent.mkdir(parents=True, exist_ok=True)
    command = ['qemu-img', 'create', '-q', '-f', 'qcow2']
    if backing is not None:
        command += ['-b', str(backing), '-F', 'qcow2', str(path)]
    else:
        command += [str(path), '1M']
    subprocess.run(command, check=True)
    return path


def _production_chains(t):
    """Have *t*'s session read backing chains the way production does:
    ``LibVirtSession.backing_chains`` running the real ``qemu-img`` (and
    whatever else it runs) on this host, local runtime, no sudo."""
    t.session.backing_chains.side_effect = LibVirtSession(
        config={'provider': {'libvirt': {}}}).backing_chains


class _Teardown:
    """A bare manager whose session reports *disks* and *media* for VM (or
    exactly *rows*, e.g. a live and a persistent definition that differ).
    ``chains=None`` makes the backing chains unreadable."""

    def __init__(self, workdir, *, disks=(), media=(), rows=None,
                 records=None, records_error=None, in_use=None,
                 chains=STANDALONE):
        self.workdir = workdir
        self.mgr = make_bare_manager(
            {'project': 'demo',
             'clusters': {'cluster_1': {'workdir': str(workdir)}}})
        session = self.session = MagicMock()
        session.vm_storage_devices.return_value = rows if rows is not None else (
            [DomblkRow('file', 'disk', f'vd{chr(97 + i)}', str(p))
             for i, p in enumerate(disks)]
            + [DomblkRow('file', 'cdrom', f'sd{chr(97 + i)}', str(p))
               for i, p in enumerate(media)])
        session.confirm_vm_absent.return_value = True
        if chains is STANDALONE:
            session.backing_chains.side_effect = _live({})
        else:
            session.backing_chains.return_value = chains
        session.disk_paths_in_use.return_value = (
            {} if in_use is None else in_use)
        self.mgr.session_for_cluster = MagicMock(return_value=session)
        self.mgr.provider = session
        self.mgr._vm_disk_records = MagicMock(
            return_value=records, side_effect=records_error)

    def deprovision(self, config_disks=()):
        self.mgr._destroy_vm_and_disks(
            'cluster_1', {'workdir': str(self.workdir)}, 'web',
            {'disks': list(config_disks)})

    def update_remove(self):
        self.mgr._destroy_removed_vm(VM)

    @property
    def warnings(self):
        return ' '.join(str(c.args[0])
                        for c in self.mgr.logger.warning.call_args_list)


class TestOrdinaryTeardown:

    def test_boot_disk_overlay_memory_file_and_data_disk_are_removed(
            self, tmp_path):
        base = _file(tmp_path / f'{VM}.qcow2')
        overlay = _file(tmp_path / f'{VM}.s1')
        memory = _file(tmp_path / f'{VM}_snapshot_s1.raw')
        data = _file(tmp_path / f'{VM}_disk01.qcow2')
        t = _Teardown(tmp_path, disks=[overlay, data],
                      records=[_record('disk01', data)])

        t.deprovision([{'name': 'disk01'}])

        assert sorted(p.name for p in tmp_path.iterdir()) == []
        assert not (base.exists() or overlay.exists() or memory.exists()
                    or data.exists())
        assert t.warnings == ''

    def test_the_pools_that_held_them_are_refreshed(self, tmp_path):
        boot = _file(tmp_path / f'{VM}.qcow2')
        t = _Teardown(tmp_path, disks=[boot], records=[])

        t.deprovision()

        # the saved inventory too: it was removed before the refresh, which
        # would otherwise list it
        t.session.refresh_pools_holding.assert_called_once_with(
            [str(boot), str(tmp_path / f'.boxman-teardown-{VM}.json')])

    def test_no_refresh_when_nothing_was_removed(self, tmp_path):
        t = _Teardown(tmp_path, disks=[], records=[])

        t.deprovision()

        t.session.refresh_pools_holding.assert_not_called()

    def test_a_data_disk_moved_by_a_snapshot_goes_with_its_chain(
            self, tmp_path):
        """provision -> snapshot take -> deprovision leaves nothing: the
        data disk's snapshot chain is proven the VM's own (see
        TestOwnedSnapshotChains)."""
        boot_base = _file(tmp_path / f'{VM}.qcow2')
        boot_head = _file(tmp_path / f'{VM}.s1')
        memory = _file(tmp_path / f'{VM}_snapshot_s1.raw')
        data_base = _file(tmp_path / f'{VM}_disk01.qcow2')
        data_head = _file(tmp_path / f'{VM}_disk01.s1')
        t = _Teardown(tmp_path, disks=[boot_head, data_head],
                      records=[_record('disk01', data_base)],
                      chains={str(data_head): [str(data_head),
                                               str(data_base)]})

        t.deprovision([{'name': 'disk01'}])

        assert list(tmp_path.iterdir()) == []
        assert not (boot_base.exists() or memory.exists())
        assert t.warnings == ''


class TestOwnedSnapshotChains:
    """A ``data`` disk whose recorded source a snapshot moved behind
    overlays is removed whole — head first — only when the chain is provably
    the VM's own; otherwise it is kept whole and named, as before."""

    def _chain(self, tmp_path, *names):
        """Files ``<vm>_disk01.<name>``, base first; returns them head
        first, the way qemu-img lists a chain."""
        layers = [_file(tmp_path / f'{VM}_disk01.{name}') for name in names]
        return list(reversed(layers))

    def _teardown(self, tmp_path, chain, *, target='vdb', role=ROLE_DATA,
                  **kwargs):
        head, base = chain[0], chain[-1]
        boot = _file(tmp_path / f'{VM}.qcow2')
        return _Teardown(
            tmp_path, disks=[boot, head],
            records=[_record('disk01', base, role=role, target=target)],
            chains=kwargs.pop('chains', {str(head): [str(p) for p in chain]}),
            **kwargs)

    @pytest.mark.parametrize("path", ["deprovision", "update_remove"])
    def test_a_proven_chain_is_removed_head_first(self, tmp_path,
                                                   monkeypatch, path):
        chain = self._chain(tmp_path, 'qcow2', 's1', 's2')
        order = []
        real = disk_cleanup.remove_if_unchanged

        def recording(p, identity):
            order.append(os.path.basename(p))
            return real(p, identity)

        monkeypatch.setattr(disk_cleanup, 'remove_if_unchanged', recording)
        t = self._teardown(tmp_path, chain)

        getattr(t, path)()

        assert not any(layer.exists() for layer in chain)
        chain_order = [o for o in order if o.startswith(f'{VM}_disk01.')]
        assert chain_order == [f'{VM}_disk01.s2', f'{VM}_disk01.s1',
                               f'{VM}_disk01.qcow2']
        assert t.warnings == ''

    def _assert_kept_whole(self, t, chain):
        for layer in chain:
            assert layer.exists(), layer
        assert str(chain[-1]) in t.warnings

    def test_a_head_attached_elsewhere_than_recorded_keeps_it(self, tmp_path):
        chain = self._chain(tmp_path, 'qcow2', 's1')
        t = self._teardown(tmp_path, chain, target='vdc')

        t.deprovision()

        self._assert_kept_whole(t, chain)

    def test_an_image_below_the_recorded_base_keeps_it(self, tmp_path):
        """boxman creates data disks standalone: a base that itself has a
        backing file is not what boxman made -- even one named like the
        disk, in the same directory."""
        chain = self._chain(tmp_path, 'qcow2', 's1')
        below = _file(tmp_path / f'{VM}_disk01.orig')
        t = self._teardown(
            tmp_path, chain,
            chains={str(chain[0]): [str(p) for p in chain] + [str(below)]})

        t.deprovision()

        self._assert_kept_whole(t, chain)
        assert below.exists()

    def test_a_layer_not_named_for_the_disk_keeps_it(self, tmp_path):
        base = _file(tmp_path / f'{VM}_disk01.qcow2')
        foreign = _file(tmp_path / 'somebody.qcow2')
        head = _file(tmp_path / f'{VM}_disk01.s2')
        chain = [head, foreign, base]
        t = self._teardown(tmp_path, chain)

        t.deprovision()

        self._assert_kept_whole(t, chain)

    def test_a_layer_in_another_directory_keeps_it(self, tmp_path):
        base = _file(tmp_path / f'{VM}_disk01.qcow2')
        away = _file(tmp_path / 'elsewhere' / f'{VM}_disk01.s1')
        head = _file(tmp_path / f'{VM}_disk01.s2')
        chain = [head, away, base]
        t = self._teardown(tmp_path, chain)

        t.deprovision()

        self._assert_kept_whole(t, chain)

    def test_a_layer_another_domain_uses_keeps_the_whole_chain(
            self, tmp_path):
        chain = self._chain(tmp_path, 'qcow2', 's1', 's2')
        t = self._teardown(tmp_path, chain,
                           in_use={os.path.realpath(chain[1]): OTHER})

        t.deprovision()

        for layer in chain:
            assert layer.exists()
        assert OTHER in t.warnings

    def test_a_chain_outside_every_cluster_workdir_keeps_it(self, tmp_path):
        away = tmp_path / 'elsewhere'
        chain = self._chain(away, 'qcow2', 's1')
        t = self._teardown(tmp_path, chain)

        t.deprovision()

        self._assert_kept_whole(t, chain)

    def test_a_layer_that_is_not_a_regular_file_keeps_it(self, tmp_path):
        base = _file(tmp_path / f'{VM}_disk01.qcow2')
        fifo = tmp_path / f'{VM}_disk01.s1'
        os.mkfifo(fifo)
        head = _file(tmp_path / f'{VM}_disk01.s2')
        chain = [head, fifo, base]
        t = self._teardown(tmp_path, chain)

        t.deprovision()

        assert head.exists() and base.exists() and fifo.exists()
        assert str(base) in t.warnings

    def test_a_failed_scan_keeps_the_whole_chain(self, tmp_path):
        chain = self._chain(tmp_path, 'qcow2', 's1')
        t = self._teardown(tmp_path, chain)
        t.session.disk_paths_in_use.return_value = None

        t.deprovision()

        for layer in chain:
            assert layer.exists()
            assert str(layer) in t.warnings

    def test_an_adopted_disk_moved_by_a_snapshot_keeps_it(self, tmp_path):
        chain = self._chain(tmp_path, 'qcow2', 's1')
        t = self._teardown(tmp_path, chain, role=ROLE_ADOPTED)

        t.deprovision()

        self._assert_kept_whole(t, chain)

    def test_an_unreadable_chain_keeps_it(self, tmp_path):
        """``chains=None``: the session could not read them (review, 7)."""
        chain = self._chain(tmp_path, 'qcow2', 's1')
        t = self._teardown(tmp_path, chain, chains=None)
        assert t.session.backing_chains([]) is None

        t.deprovision()

        self._assert_kept_whole(t, chain)

    @pytest.mark.parametrize("layer", [0, -1])
    def test_a_chain_with_a_media_layer_is_kept_whole(self, tmp_path, layer):
        """The head or the base is also a CD-ROM source in one definition
        (review, 2)."""
        chain = self._chain(tmp_path, 'qcow2', 's1')
        t = self._teardown(tmp_path, chain, media=[chain[layer]])

        t.deprovision()

        for kept in chain:
            assert kept.exists()

    def test_a_base_attached_in_the_other_definition_is_kept_under_its_head(
            self, tmp_path):
        """The live definition holds the overlay at vdb, the persistent one
        the recorded base itself: the base must not go while its head is
        kept (review, 6)."""
        boot = _file(tmp_path / f'{VM}.qcow2')
        base = _file(tmp_path / f'{VM}_disk01.qcow2')
        head = _file(tmp_path / f'{VM}_disk01.s1')
        t = _Teardown(tmp_path, rows=[
            DomblkRow('file', 'disk', 'vda', str(boot)),
            DomblkRow('file', 'disk', 'vdb', str(head)),
            DomblkRow('file', 'disk', 'vdb', str(base))],
            records=[_record('disk01', base)],
            chains={str(boot): [str(boot)],
                    str(head): [str(head), str(base)],
                    str(base): [str(base)]})

        t.deprovision([{'name': 'disk01'}])

        assert head.exists() and base.exists()
        assert str(base) in t.warnings
        assert not boot.exists()

    def test_nothing_left_to_remove_asks_no_host_wide_scan(self, tmp_path):
        """Chain dependencies are settled before the scan of every domain's
        disks, so a teardown whose candidates they all keep never runs it."""
        base = _file(tmp_path / f'{VM}_disk01.qcow2')
        head = _file(tmp_path / f'{VM}_disk01.s1')
        t = _Teardown(tmp_path, rows=[
            DomblkRow('file', 'disk', 'vdb', str(head)),
            DomblkRow('file', 'disk', 'vdb', str(base))],
            records=[_record('disk01', base)],
            chains={str(head): [str(head), str(base)],
                    str(base): [str(base)]})

        t.deprovision([{'name': 'disk01'}])

        t.session.disk_paths_in_use.assert_not_called()
        assert head.exists() and base.exists()

    def test_a_shared_base_at_a_separate_target_is_kept(self, tmp_path):
        """The recorded base at vdb, an unrecorded overlay of it at vdc."""
        boot = _file(tmp_path / f'{VM}.qcow2')
        base = _file(tmp_path / f'{VM}_disk01.qcow2')
        head = _file(tmp_path / f'{VM}_disk01.other')
        t = _Teardown(tmp_path, rows=[
            DomblkRow('file', 'disk', 'vda', str(boot)),
            DomblkRow('file', 'disk', 'vdb', str(base)),
            DomblkRow('file', 'disk', 'vdc', str(head))],
            records=[_record('disk01', base)],
            chains={str(boot): [str(boot)],
                    str(base): [str(base)],
                    str(head): [str(head), str(base)]})

        t.deprovision([{'name': 'disk01'}])

        assert head.exists() and base.exists()
        assert not boot.exists()

    def test_a_symlinked_layer_found_by_qemu_img_keeps_the_chain(
            self, tmp_path):
        """Through the real ``backing_chains`` normalisation: a symlink
        head must stay recognisable as one, not become its target
        (review, 5)."""
        boot = _file(tmp_path / f'{VM}.qcow2')
        base = _file(tmp_path / f'{VM}_disk01.qcow2')
        target = _file(tmp_path / f'{VM}_disk01.real')
        alias = tmp_path / f'{VM}_disk01.s1'
        alias.symlink_to(target)
        answer = json.dumps([
            {"filename": str(alias), "full-backing-filename": str(base)},
            {"filename": str(base)}])
        session = LibVirtSession(config={"provider": {"libvirt": {}}})
        with patch("boxman.providers.libvirt.session.LibVirtCommandBase") as cmd:
            cmd.return_value.execute_shell.side_effect = (
                lambda command, **kw: MagicMock(
                    ok=True,
                    stdout=answer if str(alias) in command
                    else json.dumps({"filename": str(boot)})))
            chains = session.backing_chains([str(boot), str(alias)])
        t = _Teardown(tmp_path, disks=[boot, alias],
                      records=[_record('disk01', base)], chains=chains)

        t.deprovision([{'name': 'disk01'}])

        assert alias.is_symlink()
        assert target.exists() and base.exists()
        assert str(base) in t.warnings

    def test_a_replaced_layer_keeps_it_and_everything_below(self, tmp_path):
        """Removal runs head first, so what is left is never an overlay
        whose backing file is gone."""
        chain = self._chain(tmp_path, 'qcow2', 's1', 's2')
        t = self._teardown(tmp_path, chain)

        def replace_middle(*_args, **_kwargs):
            fresh = tmp_path / 'fresh'
            fresh.write_bytes(b'someone else')
            fresh.replace(chain[1])

        t.session.destroy_vm.side_effect = replace_middle

        t.deprovision()

        assert not chain[0].exists()
        assert chain[1].read_bytes() == b'someone else'
        assert chain[2].exists()
        assert str(chain[2]) in t.warnings


class TestNeverRemoved:
    """#208: what libvirt's --remove-all-storage used to wipe and delete."""

    @pytest.mark.parametrize("path", ["deprovision", "update_remove"])
    def test_a_cdrom_source_is_never_removed(self, tmp_path, path):
        """Even one named like the VM's boot family, and one in a pooled
        directory of its own."""
        boot = _file(tmp_path / f'{VM}.qcow2')
        named_iso = _file(tmp_path / f'{VM}.iso', b'iso')
        pooled_iso = _file(tmp_path / 'isos' / 'installer.iso', b'iso')
        t = _Teardown(tmp_path, disks=[boot], media=[named_iso, pooled_iso],
                      records=[])

        getattr(t, path)()

        assert named_iso.read_bytes() == b'iso'
        assert pooled_iso.read_bytes() == b'iso'
        assert not boot.exists()
        assert str(named_iso) in t.warnings

    def test_an_adopted_recorded_disk_survives_deprovision(self, tmp_path):
        """Declared in conf.yml and named as boxman names it, but attached
        attach_only: boxman did not create it."""
        adopted = _file(tmp_path / f'{VM}_disk01.qcow2')
        t = _Teardown(tmp_path, disks=[adopted],
                      records=[_record('disk01', adopted, ROLE_ADOPTED)])

        t.deprovision([{'name': 'disk01'}])

        assert adopted.exists()
        assert str(adopted) in t.warnings

    def test_a_disk_another_domain_uses_directly_is_kept(self, tmp_path):
        data = _file(tmp_path / f'{VM}_disk01.qcow2')
        t = _Teardown(tmp_path, disks=[data],
                      records=[_record('disk01', data)],
                      in_use={str(data): OTHER})

        t.deprovision([{'name': 'disk01'}])

        assert data.exists()
        assert OTHER in t.warnings

    def test_a_boot_disk_another_domain_backs_onto_is_kept(self, tmp_path):
        """disk_paths_in_use maps backing files too: an overlay of another
        domain built on this VM's boot disk."""
        boot = _file(tmp_path / f'{VM}.qcow2')
        memory = _file(tmp_path / f'{VM}_snapshot_s1.raw')
        t = _Teardown(tmp_path, disks=[boot], records=[],
                      in_use={str(boot): OTHER})

        t.deprovision()

        assert boot.exists()
        assert not memory.exists()
        assert OTHER in t.warnings

    def test_a_boot_named_file_outside_every_cluster_workdir_is_kept(
            self, tmp_path):
        """update takes the VM's disk directories from libvirt, which can
        name one outside every cluster workdir (#212+#208 review, 1)."""
        workdir = tmp_path / 'cluster'
        workdir.mkdir()
        outside = _file(tmp_path / 'external' / f'{VM}.qcow2')
        t = _Teardown(workdir, disks=[outside], records=[])

        t.update_remove()

        assert outside.exists()
        assert str(outside) in t.warnings

    def test_a_recorded_data_disk_that_is_also_media_is_kept(self, tmp_path):
        """One definition attaches it as a disk, the other as a CD-ROM: the
        media exclusion holds on every removal route (review, 2)."""
        data = _file(tmp_path / f'{VM}_disk01.qcow2')
        t = _Teardown(tmp_path, rows=[
            DomblkRow('file', 'disk', 'vdb', str(data)),
            DomblkRow('file', 'cdrom', 'sda', str(data))],
            records=[_record('disk01', data)])

        t.deprovision([{'name': 'disk01'}])

        assert data.exists()
        assert str(data) in t.warnings

    def test_a_retried_deprovision_keeps_an_adopted_disk(self, tmp_path):
        """The first pass keeps it and undefines the domain; the retry finds
        the domain gone -- and its record with it. Gone is not "no record":
        the legacy by-name rule must not take it (review, 4)."""
        adopted = _file(tmp_path / f'{VM}_disk01.qcow2')
        t = _Teardown(tmp_path, disks=[adopted],
                      records=[_record('disk01', adopted, ROLE_ADOPTED)])

        t.deprovision([{'name': 'disk01'}])
        assert adopted.exists()

        t.session.vm_storage_devices.return_value = None
        t.session.confirm_vm_absent.return_value = True
        t.mgr.logger.reset_mock()

        t.deprovision([{'name': 'disk01'}])

        assert adopted.exists()
        assert str(adopted) in t.warnings
        t.mgr._vm_disk_records.assert_called_once()

    def test_a_symlink_under_the_vms_names_is_kept(self, tmp_path):
        """boxman never creates one; unlinking it would report a disk as
        removed while its data lives on elsewhere."""
        target = _file(tmp_path / 'elsewhere' / 'image.qcow2')
        link = tmp_path / f'{VM}.qcow2'
        link.symlink_to(target)
        t = _Teardown(tmp_path, disks=[link], records=[])

        t.deprovision()

        assert link.is_symlink() and target.exists()
        assert str(link) in t.warnings

    def test_a_boot_base_another_domain_uses_keeps_its_overlay_too(
            self, tmp_path):
        """A chain goes as a whole or not at all: the overlay stays on its
        kept base rather than going on its own."""
        base = _file(tmp_path / f'{VM}.qcow2')
        head = _file(tmp_path / f'{VM}.s1')
        t = _Teardown(tmp_path, disks=[head], records=[],
                      chains={str(head): [str(head), str(base)]},
                      in_use={str(base): OTHER})

        t.deprovision()

        assert base.exists() and head.exists()
        assert OTHER in t.warnings

    def test_a_chain_that_goes_through_a_symlink_is_kept(self, tmp_path):
        """qemu-img names the backing layer by a symlink to the boot base:
        resolved, every layer looks removable, so the check runs on the
        name qemu-img gave (#212+#208 review, 5)."""
        base = _file(tmp_path / f'{VM}.qcow2')
        head = _file(tmp_path / f'{VM}.s1')
        link = tmp_path / 'base-link.qcow2'
        link.symlink_to(base)
        t = _Teardown(tmp_path, disks=[head], records=[],
                      chains={str(head): [str(head), str(link)]})

        t.deprovision()

        assert head.exists() and base.exists() and link.is_symlink()
        assert 'symlink' in t.warnings

    def test_a_failed_scan_keeps_every_candidate_and_names_each(
            self, tmp_path):
        boot = _file(tmp_path / f'{VM}.qcow2')
        memory = _file(tmp_path / f'{VM}_snapshot_s1.raw')
        data = _file(tmp_path / f'{VM}_disk01.qcow2')
        t = _Teardown(tmp_path, disks=[boot, data],
                      records=[_record('disk01', data)])
        t.session.disk_paths_in_use.return_value = None

        t.deprovision([{'name': 'disk01'}])

        for kept in (boot, memory, data):
            assert kept.exists()
            assert str(kept) in t.warnings
        t.session.refresh_pools_holding.assert_not_called()


class TestLegacyDomains:
    """USER DECISION: a domain with no ownership record at all."""

    def test_deprovision_removes_its_declared_disks_by_name(self, tmp_path):
        boot = _file(tmp_path / f'{VM}.qcow2')
        qcow = _file(tmp_path / f'{VM}_disk01.qcow2')
        raw = _file(tmp_path / f'{VM}_logs.raw')
        undeclared = _file(tmp_path / f'{VM}_extra.qcow2')
        t = _Teardown(tmp_path, disks=[boot, qcow, raw, undeclared],
                      records=None)

        t.deprovision([{'name': 'disk01'},
                       {'name': 'logs', 'driver': {'type': 'raw'}}])

        assert not boot.exists() and not qcow.exists() and not raw.exists()
        # attached but not declared: nothing names it as this VM's
        assert undeclared.exists()
        assert str(undeclared) in t.warnings

    def test_update_keeps_the_extra_disks_of_a_vm_gone_from_the_config(
            self, tmp_path):
        boot = _file(tmp_path / f'{VM}.qcow2')
        data = _file(tmp_path / f'{VM}_disk01.qcow2')
        t = _Teardown(tmp_path, disks=[boot, data], records=None)

        t.update_remove()

        assert not boot.exists()
        assert data.exists()
        assert str(data) in t.warnings

    def test_unreadable_records_are_not_taken_for_no_records(self, tmp_path):
        data = _file(tmp_path / f'{VM}_disk01.qcow2')
        t = _Teardown(tmp_path, disks=[data],
                      records_error=ProvisionError("could not parse it"))

        t.deprovision([{'name': 'disk01'}])

        assert data.exists()
        assert 'could not be read' in t.warnings

    def test_a_declared_disk_another_domain_uses_is_kept(self, tmp_path):
        data = _file(tmp_path / f'{VM}_disk01.qcow2')
        t = _Teardown(tmp_path, disks=[data], records=None,
                      in_use={str(data): OTHER})

        t.deprovision([{'name': 'disk01'}])

        assert data.exists()
        assert OTHER in t.warnings

    def test_a_declared_disk_that_is_a_cdrom_source_is_kept(self, tmp_path):
        iso = _file(tmp_path / f'{VM}_disk01.qcow2', b'iso')
        t = _Teardown(tmp_path, media=[iso], records=None)

        t.deprovision([{'name': 'disk01'}])

        assert iso.read_bytes() == b'iso'


def _qcow2(path):
    """A file that starts like a qcow2 image."""
    return _file(path, b'QFI\xfb' + b'\0' * 60)


def _saved(tmp_path):
    return tmp_path / f'.boxman-teardown-{VM}.json'


class TestRetriedTeardown:
    """A teardown interrupted after the undefine, retried: the domain, its
    devices and its ownership record are gone (#208 review round 2, 1)."""

    @pytest.mark.parametrize("path", ["deprovision", "update_remove"])
    def test_a_cdrom_under_the_vms_names_survives_a_retry(self, tmp_path,
                                                          path):
        """``<vm>.install.iso`` is a CD-ROM source; the first pass keeps it,
        and the retry — with no devices to read — must keep it too."""
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        iso = _file(tmp_path / f'{VM}.install.iso', b'iso')
        t = _Teardown(tmp_path, disks=[boot], media=[iso], records=[])

        getattr(t, path)()
        assert not boot.exists() and iso.exists()

        t.session.vm_storage_devices.return_value = None
        t.session.confirm_vm_absent.return_value = True
        t.mgr.logger.reset_mock()

        getattr(t, path)()

        assert iso.read_bytes() == b'iso'
        assert str(iso) in t.warnings

    def test_a_retry_decides_with_the_saved_inventory(self, tmp_path):
        """The first pass kept a recorded data disk another domain was
        using; once that domain lets go, the retry removes it by the saved
        record — without one, a gone domain's extra disks are kept."""
        data = _qcow2(tmp_path / f'{VM}_disk01.qcow2')
        t = _Teardown(tmp_path, disks=[data],
                      records=[_record('disk01', data)],
                      in_use={str(data): OTHER})

        t.deprovision([{'name': 'disk01'}])
        assert data.exists() and _saved(tmp_path).exists()

        t.session.vm_storage_devices.return_value = None
        t.session.disk_paths_in_use.return_value = {}
        t.mgr.logger.reset_mock()

        t.deprovision([{'name': 'disk01'}])

        assert not data.exists()
        assert not _saved(tmp_path).exists()
        assert t.warnings == ''

    def test_a_saved_file_replaced_since_is_kept(self, tmp_path):
        """The saved identities still guard every unlink."""
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        data = _qcow2(tmp_path / f'{VM}_disk01.qcow2')
        t = _Teardown(tmp_path, disks=[boot, data],
                      records=[_record('disk01', data)],
                      in_use={str(data): OTHER})
        t.deprovision([{'name': 'disk01'}])

        fresh = tmp_path / 'fresh'
        fresh.write_bytes(b'someone else')
        fresh.replace(data)
        t.session.vm_storage_devices.return_value = None
        t.session.disk_paths_in_use.return_value = {}

        t.deprovision([{'name': 'disk01'}])

        assert data.read_bytes() == b'someone else'
        assert _saved(tmp_path).exists()

    def test_without_a_saved_inventory_only_boxman_artifacts_go(
            self, tmp_path):
        """A failed provision's leftovers, or a manual undefine: qcow2
        images (told by their magic) and memory files are removed by name;
        anything else under the VM's names, and every extra disk, stays."""
        removed = [_qcow2(tmp_path / f'{VM}.qcow2'),
                   _qcow2(tmp_path / f'{VM}.s1'),
                   _file(tmp_path / f'{VM}_snapshot_s1.raw'),
                   _file(tmp_path / f'{VM}_snapshot_s2.raw.zst')]
        kept = [_file(tmp_path / f'{VM}.install.iso', b'iso'),
                _file(tmp_path / f'{VM}.notes.qcow2', b'not really'),
                _file(tmp_path / f'{VM}_snapshot_s1.xml'),
                _qcow2(tmp_path / f'{VM}_disk01.qcow2')]
        t = _Teardown(tmp_path, records=[])
        t.session.vm_storage_devices.return_value = None
        t.session.confirm_vm_absent.return_value = True

        t.deprovision([{'name': 'disk01'}])

        for path in removed:
            assert not path.exists(), path
        for path in kept:
            assert path.exists(), path
            assert str(path) in t.warnings
        t.mgr._vm_disk_records.assert_not_called()

    def test_without_a_saved_inventory_in_use_files_stay(self, tmp_path):
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        t = _Teardown(tmp_path, records=[], in_use={str(boot): OTHER})
        t.session.vm_storage_devices.return_value = None

        t.deprovision()

        assert boot.exists()

    # -- #208 review round 3 -----------------------------------------------

    @pytest.mark.parametrize("path", ["deprovision", "update_remove"])
    def test_a_retry_finds_an_inventory_saved_beside_a_boot_disk_elsewhere(
            self, tmp_path, path):
        """The boot disk lives outside the configured workdir, so the first
        pass saves the inventory beside it, there; the retry must still
        decide with it and keep the qcow2 CD-ROM under the VM's names in
        the workdir, which the no-inventory fallback would take for one of
        boxman's images (round 3, 1)."""
        workdir = tmp_path / 'work'
        elsewhere = tmp_path / 'elsewhere'
        boot = _qcow2(elsewhere / f'{VM}.qcow2')
        cdrom = _qcow2(workdir / f'{VM}.cd.qcow2')
        t = _Teardown(workdir, disks=[boot], media=[cdrom], records=[])

        getattr(t, path)()
        assert boot.exists() and cdrom.exists()
        assert _saved(elsewhere).exists() and not _saved(workdir).exists()

        t.session.vm_storage_devices.return_value = None
        t.mgr.logger.reset_mock()

        getattr(t, path)()

        assert cdrom.exists()
        if path == "deprovision":
            # searched by name in the workdir, and kept as the media it is
            # (update searches only where the attached disks were)
            assert (f'left {cdrom} in place because it is a CD-ROM'
                    in t.warnings)
        assert _saved(elsewhere).exists()

    def test_a_retry_finishes_a_chain_whose_head_was_removed(self, tmp_path):
        """Heads go first; a teardown interrupted after removing the head
        of the boot chain, retried, removes the rest (round 3, 2)."""
        base = _qcow2(tmp_path / f'{VM}.qcow2')
        head = _qcow2(tmp_path / f'{VM}.s1')
        t = _Teardown(tmp_path, disks=[head], records=[])
        t.session.backing_chains.side_effect = _live({head: [head, base]})

        self._interrupt_at(t, base)
        assert not head.exists() and base.exists()
        assert _saved(tmp_path).exists()

        t.session.vm_storage_devices.return_value = None
        t.mgr.logger.reset_mock()
        t.deprovision()

        assert not base.exists()
        assert not _saved(tmp_path).exists()
        assert t.warnings == ''

    def test_a_retry_finishes_a_data_disk_chain_whose_head_was_removed(
            self, tmp_path):
        """The same for a recorded data disk an external snapshot moved
        behind an overlay: the saved record and chain still prove the base
        the VM's own once its overlay is gone (round 3, 2)."""
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        base = _qcow2(tmp_path / f'{VM}_disk01.qcow2')
        head = _qcow2(tmp_path / f'{VM}_disk01.s1')
        t = _Teardown(tmp_path, disks=[boot, head],
                      records=[_record('disk01', base)])
        t.session.backing_chains.side_effect = _live({head: [head, base]})

        self._interrupt_at(t, base, config_disks=[{'name': 'disk01'}])
        assert not (boot.exists() or head.exists()) and base.exists()

        t.session.vm_storage_devices.return_value = None
        t.mgr.logger.reset_mock()
        t.deprovision([{'name': 'disk01'}])

        assert not base.exists()
        assert not _saved(tmp_path).exists()
        assert t.warnings == ''

    def test_what_is_left_of_a_chain_that_cannot_be_read_is_kept(
            self, tmp_path):
        """The rest of an interrupted chain is read again too, and failing
        to read it keeps everything."""
        base = _qcow2(tmp_path / f'{VM}.qcow2')
        head = _qcow2(tmp_path / f'{VM}.s1')
        t = _Teardown(tmp_path, disks=[head], records=[])
        t.session.backing_chains.side_effect = _live({head: [head, base]})
        self._interrupt_at(t, base)

        t.session.vm_storage_devices.return_value = None
        t.session.backing_chains.side_effect = _live(
            {base: [base, tmp_path / 'gone.qcow2']})
        t.deprovision()

        assert base.exists()
        assert _saved(tmp_path).exists()
        assert 'backing chains of the vm' in t.warnings

    def test_a_layer_replaced_since_still_keeps_what_is_below_it(
            self, tmp_path):
        """What survives of an interrupted chain keeps its identity
        checks: a middle layer replaced since keeps itself and the base."""
        base = _qcow2(tmp_path / f'{VM}.qcow2')
        middle = _qcow2(tmp_path / f'{VM}.s1')
        head = _qcow2(tmp_path / f'{VM}.s2')
        t = _Teardown(tmp_path, disks=[head], records=[])
        t.session.backing_chains.side_effect = _live(
            {head: [head, middle, base], middle: [middle, base]})

        self._interrupt_at(t, middle)
        assert not head.exists()
        fresh = tmp_path / 'fresh'
        fresh.write_bytes(b'QFI\xfb someone else')
        fresh.replace(middle)

        t.session.vm_storage_devices.return_value = None
        t.deprovision()

        assert middle.read_bytes() == b'QFI\xfb someone else'
        assert base.exists()
        assert _saved(tmp_path).exists()

    @staticmethod
    def _interrupt_at(t, stop, config_disks=()):
        """Run *t*'s deprovision with the removal of *stop* failing, as an
        interruption would leave it."""
        remove = disk_cleanup.remove_if_unchanged

        def interrupted(path, identity):
            if path == str(stop):
                raise OSError(5, 'Input/output error')
            return remove(path, identity)

        with patch.object(disk_cleanup, 'remove_if_unchanged',
                          side_effect=interrupted), \
                pytest.raises(OSError, match='Input/output error'):
            t.deprovision(list(config_disks))


class TestSavedInventoryLifecycle:

    def test_saved_before_the_undefine_and_removed_when_nothing_is_kept(
            self, tmp_path):
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        t = _Teardown(tmp_path, disks=[boot], records=[])
        seen = []
        t.session.destroy_vm.side_effect = (
            lambda *a, **k: seen.append(_saved(tmp_path).exists()))

        t.deprovision()

        assert seen and all(seen)
        assert not _saved(tmp_path).exists()
        assert not boot.exists()

    def test_kept_while_it_protects_a_kept_file(self, tmp_path):
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        iso = _file(tmp_path / f'{VM}.install.iso', b'iso')
        t = _Teardown(tmp_path, disks=[boot], media=[iso], records=[])

        t.deprovision()

        saved = json.loads(_saved(tmp_path).read_text())
        assert saved['vm_name'] == VM
        assert saved['media_sources'] == [str(iso)]

    def test_a_capture_of_an_existing_domain_overwrites_it(self, tmp_path):
        _saved(tmp_path).write_text('{"stale": true}')
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        t = _Teardown(tmp_path, disks=[boot], records=[])

        t.deprovision()

        assert not boot.exists()
        assert not _saved(tmp_path).exists()

    def test_an_unreadable_saved_inventory_keeps_everything(self, tmp_path):
        _saved(tmp_path).write_text('{"version": 1, "vm_na')
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        t = _Teardown(tmp_path, records=[])
        t.session.vm_storage_devices.return_value = None

        with pytest.raises(ProvisionError, match='could not be read'):
            t.deprovision()

        assert boot.exists()

    @pytest.mark.parametrize("layout", [
        "a list", "a string", "version true", "present records missing",
        "a record of numbers", "a record short of a field",
        "disk sources a string", "a chain a string",
        "identities of booleans", "an identity of three",
        "targets of lists", "boot family of numbers",
    ])
    def test_a_malformed_saved_inventory_keeps_everything(self, tmp_path,
                                                          layout):
        """Well-formed JSON of the wrong shape is as unreadable as a
        truncated file (round 3, 5)."""
        data = _malformed(tmp_path, layout)
        _saved(tmp_path).write_text(json.dumps(data))
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        t = _Teardown(tmp_path, records=[])
        t.session.vm_storage_devices.return_value = None

        with pytest.raises(ProvisionError, match='could not be read'):
            t.deprovision()

        assert boot.exists()
        t.session.destroy_vm.assert_not_called()

    def test_an_inventory_saved_for_another_vm_is_refused(self, tmp_path):
        path = disk_cleanup.save_teardown_inventory(disk_cleanup.StorageInventory(
            vm_name=OTHER, disk_sources=[], media_sources=[], records=[],
            records_state='present', chains={}, boot_family=[],
            legacy_disks=None), str(tmp_path))
        (tmp_path / os.path.basename(path)).replace(_saved(tmp_path))
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        t = _Teardown(tmp_path, records=[])
        t.session.vm_storage_devices.return_value = None

        with pytest.raises(ProvisionError, match='could not be read'):
            t.deprovision()

        assert boot.exists()

    def test_a_domain_whose_inventory_cannot_be_saved_stays_defined(
            self, tmp_path, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise PermissionError(13, 'Permission denied')

        monkeypatch.setattr(
            'boxman.manager_parts.vms.save_teardown_inventory', refuse)
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        t = _Teardown(tmp_path, disks=[boot], records=[])

        with pytest.raises(ProvisionError, match='could not save'):
            t.deprovision()

        t.session.destroy_vm.assert_not_called()
        assert boot.exists()


class TestMediaChains:
    """A qcow2 CD-ROM built on a file the vm also attaches as a recorded
    data disk (#208 review round 2, 2)."""

    def test_the_base_of_a_media_head_is_kept(self, tmp_path):
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        data = _qcow2(tmp_path / f'{VM}_disk01.qcow2')
        media = _qcow2(tmp_path / 'isos' / 'media.qcow2')
        t = _Teardown(tmp_path, disks=[boot, data], media=[media],
                      records=[_record('disk01', data)],
                      chains={str(boot): [str(boot)],
                              str(data): [str(data)],
                              str(media): [str(media), str(data)]})

        t.deprovision([{'name': 'disk01'}])

        assert media.exists() and data.exists()
        assert not boot.exists()
        # protected as a layer of the media, before it could be admitted
        assert f'left {data} in place because it backs a CD-ROM' in t.warnings
        # the media source's chain was asked for, not just the disks'
        asked = t.session.backing_chains.call_args.args[0]
        assert str(media) in asked

    # -- #208 review round 3, 4 ----------------------------------------------

    @needs_qemu_img
    @pytest.mark.parametrize("missing", ["cdrom", "disk"])
    def test_a_source_gone_from_the_host_does_not_keep_the_boot_disk(
            self, tmp_path, missing):
        """A seed ISO still attached after it was deleted -- tolerated
        everywhere else -- has no chain, so nothing to protect: the boot
        disk is reclaimed. So is a recorded extra disk deleted by hand."""
        boot = _image(tmp_path / f'{VM}.qcow2')
        if missing == "cdrom":
            gone = tmp_path / 'seed.iso'
            t = _Teardown(tmp_path, disks=[boot], media=[gone], records=[])
        else:
            gone = tmp_path / f'{VM}_disk01.qcow2'
            t = _Teardown(tmp_path, disks=[boot, gone],
                          records=[_record('disk01', gone)])
        _production_chains(t)

        t.deprovision()

        assert not boot.exists()
        assert not _saved(tmp_path).exists()
        assert t.warnings == ''

    @needs_qemu_img
    def test_an_existing_media_image_that_cannot_be_read_keeps_it(
            self, tmp_path):
        """Absence is not a read error: an existing image qemu-img cannot
        read still keeps everything."""
        if os.geteuid() == 0:
            pytest.skip('root reads a mode-000 file')
        boot = _image(tmp_path / f'{VM}.qcow2')
        media = _image(tmp_path / 'isos' / 'media.qcow2')
        media.chmod(0)
        t = _Teardown(tmp_path, disks=[boot], media=[media], records=[])
        _production_chains(t)

        t.deprovision()

        assert boot.exists()
        assert 'backing chains of the vm' in t.warnings


@needs_qemu_img
class TestStaleSavedDependencies:
    """A saved inventory keeps the provenance a retry can no longer read
    -- records, roles, targets, identities -- not the dependencies: a file
    it left in place can be rebased since, in place, on the same inode
    (#208 review round 3, 3)."""

    @pytest.mark.parametrize("kept", ["media", "adopted disk"])
    def test_a_kept_image_rebased_onto_a_kept_data_disk_keeps_it(
            self, tmp_path, kept):
        boot = _image(tmp_path / f'{VM}.qcow2')
        data = _image(tmp_path / f'{VM}_disk01.qcow2')
        records = [_record('disk01', data)]
        if kept == "media":
            head = _image(tmp_path / 'isos' / 'media.qcow2')
            t = _Teardown(tmp_path, disks=[boot, data], media=[head],
                          records=records, in_use={str(data): OTHER})
        else:
            head = _image(tmp_path / 'shared' / 'adopted.qcow2')
            records.append(_record('shared', head, role=ROLE_ADOPTED,
                                   target='vdc'))
            t = _Teardown(tmp_path, disks=[boot, data, head],
                          records=records, in_use={str(data): OTHER})
        _production_chains(t)

        t.deprovision([{'name': 'disk01'}])
        assert data.exists() and head.exists() and not boot.exists()

        before = disk_cleanup.file_identities([str(head), str(data)])
        subprocess.run(['qemu-img', 'rebase', '-u', '-F', 'qcow2', '-b',
                        str(data), str(head)], check=True)
        assert disk_cleanup.file_identities([str(head), str(data)]) == before

        # the other domain lets go; the retry must see what the kept image
        # depends on now, not what it depended on when it was saved
        t.session.vm_storage_devices.return_value = None
        t.session.disk_paths_in_use.return_value = {}
        t.mgr.logger.reset_mock()
        t.deprovision([{'name': 'disk01'}])

        assert data.exists()
        assert str(data) in t.warnings
        chain = subprocess.run(
            ['qemu-img', 'info', '--backing-chain', '--output=json', '-U',
             str(head)], check=True, capture_output=True, text=True)
        assert [image['filename'] for image in json.loads(chain.stdout)] == [
            str(head), str(data)]

    def test_a_chain_that_cannot_be_read_now_keeps_everything(
            self, tmp_path):
        """The re-read fails closed: a kept image whose chain cannot be
        read any more keeps every candidate."""
        if os.geteuid() == 0:
            pytest.skip('root reads a mode-000 file')
        boot = _image(tmp_path / f'{VM}.qcow2')
        data = _image(tmp_path / f'{VM}_disk01.qcow2')
        media = _image(tmp_path / 'isos' / 'media.qcow2')
        t = _Teardown(tmp_path, disks=[boot, data], media=[media],
                      records=[_record('disk01', data)],
                      in_use={str(data): OTHER})
        _production_chains(t)
        t.deprovision([{'name': 'disk01'}])

        media.chmod(0)
        t.session.vm_storage_devices.return_value = None
        t.session.disk_paths_in_use.return_value = {}
        t.deprovision([{'name': 'disk01'}])

        assert data.exists()


def _locator():
    return disk_cleanup.teardown_locator_path(
        config_cache.teardown_locator_dir(), VM)


class TestTeardownLocator:
    """Where the inventory was saved is recorded under boxman's per-user
    state dir, keyed by the full vm name, so a retry finds it wherever the
    boot disk was; a locator that leads nowhere readable fails the retry
    closed, never falling back (#208 review round 3, 1)."""

    def test_it_lives_under_the_per_user_cache_dir(self, tmp_path,
                                                   monkeypatch):
        monkeypatch.setattr(config_cache, 'DEFAULT_CACHE_DIR',
                            str(tmp_path / 'cache'))
        assert _locator() == str(tmp_path / 'cache' / 'teardown'
                                 / f'{VM}.json')

    def test_written_before_the_undefine_and_removed_with_the_inventory(
            self, tmp_path):
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        t = _Teardown(tmp_path, disks=[boot], records=[])
        seen = []
        t.session.destroy_vm.side_effect = (
            lambda *a, **k: seen.append(json.loads(open(_locator()).read())))

        t.deprovision()

        assert seen and seen[0] == {'version': 1, 'vm_name': VM,
                                    'inventory': str(_saved(tmp_path))}
        assert not os.path.lexists(_locator())
        assert not _saved(tmp_path).exists() and not boot.exists()

    def _first_pass_keeps_media(self, tmp_path):
        """A deprovision that keeps a qcow2 CD-ROM under the VM's names --
        what the no-inventory fallback would take for a boot image --
        leaving the inventory and its locator behind; then the domain is
        gone."""
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        cdrom = _qcow2(tmp_path / f'{VM}.cd.qcow2')
        t = _Teardown(tmp_path, disks=[boot], media=[cdrom], records=[])
        t.deprovision()
        assert cdrom.exists() and not boot.exists()
        assert json.loads(open(_locator()).read())['inventory'] == str(
            _saved(tmp_path))
        t.session.vm_storage_devices.return_value = None
        t.session.destroy_vm.reset_mock()
        return t, cdrom

    def test_kept_with_the_inventory_while_it_protects_a_kept_file(
            self, tmp_path):
        t, cdrom = self._first_pass_keeps_media(tmp_path)

        t.deprovision()

        assert cdrom.exists()
        assert os.path.lexists(_locator()) and _saved(tmp_path).exists()

    def test_an_inventory_it_names_that_is_missing_fails_closed(
            self, tmp_path):
        t, cdrom = self._first_pass_keeps_media(tmp_path)
        _saved(tmp_path).unlink()

        with pytest.raises(ProvisionError, match='which is missing') as exc:
            t.deprovision()

        assert _locator() in str(exc.value)
        assert str(_saved(tmp_path)) in str(exc.value)
        assert cdrom.exists()
        t.session.destroy_vm.assert_not_called()

    def test_an_inventory_it_names_that_cannot_be_read_fails_closed(
            self, tmp_path):
        t, cdrom = self._first_pass_keeps_media(tmp_path)
        _saved(tmp_path).write_text('[]')

        with pytest.raises(ProvisionError, match='could not be read') as exc:
            t.deprovision()

        assert _locator() in str(exc.value)
        assert str(_saved(tmp_path)) in str(exc.value)
        assert cdrom.exists()
        t.session.destroy_vm.assert_not_called()

    @pytest.mark.parametrize("content", [
        '[]', '{"version": 1, "vm_na',
        # each of these is refused on one count alone; '@saved@' is the
        # real inventory, which it would otherwise lead to
        json.dumps({'version': 2, 'vm_name': VM, 'inventory': '@saved@'}),
        json.dumps({'version': True, 'vm_name': VM, 'inventory': '@saved@'}),
        json.dumps({'version': 1, 'vm_name': OTHER, 'inventory': '@saved@'}),
        json.dumps({'version': 1, 'vm_name': VM,
                    'inventory': f'.boxman-teardown-{VM}.json'}),
        json.dumps({'version': 1, 'vm_name': VM, 'inventory': '/etc/passwd'}),
    ])
    def test_a_locator_that_cannot_be_read_fails_closed(self, tmp_path,
                                                        content):
        t, cdrom = self._first_pass_keeps_media(tmp_path)
        with open(_locator(), 'w') as fh:
            fh.write(content.replace('@saved@', str(_saved(tmp_path))))

        with pytest.raises(ProvisionError, match='could not be read') as exc:
            t.deprovision()

        assert _locator() in str(exc.value)
        assert cdrom.exists()
        t.session.destroy_vm.assert_not_called()

    def test_it_is_consulted_before_the_workdir(self, tmp_path):
        """With a locator, a file at the inventory's name in the searched
        directories is never read in its place."""
        workdir = tmp_path / 'work'
        elsewhere = tmp_path / 'elsewhere'
        boot = _qcow2(elsewhere / f'{VM}.qcow2')
        cdrom = _qcow2(workdir / f'{VM}.cd.qcow2')
        t = _Teardown(workdir, disks=[boot], media=[cdrom], records=[])
        t.deprovision()
        _saved(workdir).write_text('not an inventory')
        t.session.vm_storage_devices.return_value = None

        t.deprovision()

        assert cdrom.exists()

    def test_without_it_the_inventory_beside_the_boot_disk_still_counts(
            self, tmp_path):
        """boxman's state dir cleared: the retry still finds the inventory
        in the directories it searches, and keeps the media."""
        t, cdrom = self._first_pass_keeps_media(tmp_path)
        os.remove(_locator())

        t.deprovision()

        assert cdrom.exists()
        assert _saved(tmp_path).exists()

    def test_a_locator_naming_another_file_never_gets_it_removed(
            self, tmp_path):
        """What a locator names is removed once superseded — so only a file
        named as the vm's teardown inventory is ever taken from one."""
        victim = _file(tmp_path / 'elsewhere' / 'precious.json', b'{}')
        os.makedirs(os.path.dirname(_locator()))
        with open(_locator(), 'w') as fh:
            json.dump({'version': 1, 'vm_name': VM,
                       'inventory': str(victim)}, fh)
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        t = _Teardown(tmp_path, disks=[boot], records=[])

        t.deprovision()

        assert victim.read_bytes() == b'{}'
        assert not boot.exists()
        assert not os.path.lexists(_locator())

    def test_destroy_retires_only_the_locators_whose_inventory_is_gone(
            self, tmp_path):
        """``destroy`` removes the workspace a kept inventory was saved in;
        its locator would then fail every later teardown of the VM closed.
        A locator whose inventory survives (saved outside the workspace)
        still protects, and stays."""
        mgr = make_bare_manager(
            {'project': 'demo',
             'clusters': {'cluster_1': {'workdir': str(tmp_path / 'w'),
                                        'vms': {'web': {}, 'db': {}}}}})
        gone = tmp_path / 'w' / f'.boxman-teardown-{VM}.json'
        kept = _file(tmp_path / 'outside' / f'.boxman-teardown-{OTHER}.json',
                     b'{}')
        other = disk_cleanup.teardown_locator_path(
            config_cache.teardown_locator_dir(), OTHER)
        disk_cleanup.save_teardown_locator(_locator(), VM, str(gone))
        disk_cleanup.save_teardown_locator(other, OTHER, str(kept))

        mgr._retire_stale_teardown_locators()

        assert not os.path.lexists(_locator())
        assert os.path.lexists(other) and kept.exists()

    # -- #208 review round 4, 1: only "not there" is absence ---------------

    @unless_root
    def test_a_locator_that_cannot_be_looked_up_fails_the_retry_closed(
            self, tmp_path):
        """Codex's reproduction: the locator's directory cannot be searched,
        which is not "no locator" -- falling back deleted the VM-named
        qcow2 CD-ROM the inventory protects."""
        workdir, elsewhere = tmp_path / 'work', tmp_path / 'elsewhere'
        boot = _qcow2(elsewhere / f'{VM}.qcow2')
        cdrom = _qcow2(workdir / f'{VM}.cd.qcow2')
        t = _Teardown(workdir, disks=[boot], media=[cdrom], records=[])
        t.deprovision()
        t.session.vm_storage_devices.return_value = None
        t.session.destroy_vm.reset_mock()
        locators = os.path.dirname(_locator())
        os.chmod(locators, 0)
        try:
            with pytest.raises(ProvisionError,
                               match='could not be looked up') as exc:
                t.deprovision()
        finally:
            os.chmod(locators, 0o700)

        assert _locator() in str(exc.value)
        assert cdrom.exists() and boot.exists()
        assert _saved(elsewhere).exists() and os.path.lexists(_locator())
        t.session.destroy_vm.assert_not_called()

    @unless_root
    def test_an_inventory_it_names_that_cannot_be_looked_up_fails_closed(
            self, tmp_path):
        workdir, elsewhere = tmp_path / 'work', tmp_path / 'elsewhere'
        boot = _qcow2(elsewhere / f'{VM}.qcow2')
        cdrom = _qcow2(workdir / f'{VM}.cd.qcow2')
        t = _Teardown(workdir, disks=[boot], media=[cdrom], records=[])
        t.deprovision()
        t.session.vm_storage_devices.return_value = None
        elsewhere.chmod(0)
        try:
            with pytest.raises(ProvisionError,
                               match='could not be looked up') as exc:
                t.deprovision()
        finally:
            elsewhere.chmod(0o700)

        assert _locator() in str(exc.value)
        assert str(_saved(elsewhere)) in str(exc.value)
        assert cdrom.exists() and _saved(elsewhere).exists()

    @unless_root
    def test_a_searched_directory_that_cannot_be_looked_up_fails_closed(
            self, tmp_path):
        """No locator (state dir cleared): an inventory beside the boot disk
        that cannot be looked up is not skipped for the fallback."""
        workdir = tmp_path / 'w'
        t, cdrom = self._first_pass_keeps_media(workdir)
        os.remove(_locator())
        workdir.chmod(0)
        try:
            with pytest.raises(ProvisionError,
                               match='could not be looked up') as exc:
                t.deprovision()
        finally:
            workdir.chmod(0o700)

        assert str(_saved(workdir)) in str(exc.value)
        assert cdrom.exists() and _saved(workdir).exists()

    @unless_root
    def test_destroy_keeps_a_locator_whose_inventory_cannot_be_looked_up(
            self, tmp_path):
        mgr = make_bare_manager(
            {'project': 'demo',
             'clusters': {'cluster_1': {'workdir': str(tmp_path / 'w'),
                                        'vms': {'web': {}}}}})
        hidden = tmp_path / 'hidden'
        inventory = _file(hidden / f'.boxman-teardown-{VM}.json', b'{}')
        disk_cleanup.save_teardown_locator(_locator(), VM, str(inventory))
        hidden.chmod(0)
        try:
            mgr._retire_stale_teardown_locators()
        finally:
            hidden.chmod(0o700)

        assert os.path.lexists(_locator()) and inventory.exists()
        warned = ' '.join(str(c.args[0])
                          for c in mgr.logger.warning.call_args_list)
        assert _locator() in warned and str(inventory) in warned

    @unless_root
    def test_destroy_carries_on_past_a_locator_it_cannot_read(self, tmp_path):
        mgr = make_bare_manager(
            {'project': 'demo',
             'clusters': {'cluster_1': {'workdir': str(tmp_path / 'w'),
                                        'vms': {'web': {}, 'db': {}}}}})
        disk_cleanup.save_teardown_locator(
            _locator(), VM, str(tmp_path / 'gone' / f'.boxman-teardown-{VM}.json'))
        locators = os.path.dirname(_locator())
        os.chmod(locators, 0)
        try:
            mgr._retire_stale_teardown_locators()
        finally:
            os.chmod(locators, 0o700)

        assert os.path.lexists(_locator())
        warned = ' '.join(str(c.args[0])
                          for c in mgr.logger.warning.call_args_list)
        assert _locator() in warned

    @unless_root
    def test_destroy_carries_on_past_a_locator_it_cannot_remove(
            self, tmp_path):
        mgr = make_bare_manager(
            {'project': 'demo',
             'clusters': {'cluster_1': {'workdir': str(tmp_path / 'w'),
                                        'vms': {'web': {}}}}})
        disk_cleanup.save_teardown_locator(
            _locator(), VM, str(tmp_path / 'gone' / f'.boxman-teardown-{VM}.json'))
        locators = os.path.dirname(_locator())
        os.chmod(locators, 0o500)
        try:
            mgr._retire_stale_teardown_locators()
        finally:
            os.chmod(locators, 0o700)

        assert os.path.lexists(_locator())
        warned = ' '.join(str(c.args[0])
                          for c in mgr.logger.warning.call_args_list)
        assert _locator() in warned

    def test_one_that_cannot_be_written_leaves_the_domain_defined(
            self, tmp_path, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise PermissionError(13, 'Permission denied')

        monkeypatch.setattr(
            'boxman.manager_parts.vms.save_teardown_locator', refuse)
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        t = _Teardown(tmp_path, disks=[boot], records=[])

        with pytest.raises(ProvisionError, match='could not save') as exc:
            t.deprovision()

        assert _locator() in str(exc.value)
        t.session.destroy_vm.assert_not_called()
        assert boot.exists()

    def test_an_inventory_of_an_earlier_definition_is_retired(
            self, tmp_path):
        """A locator left by an earlier definition of the vm names an
        inventory elsewhere; a capture of the domain as it is now replaces
        both, and the superseded inventory goes."""
        earlier = tmp_path / 'earlier'
        earlier.mkdir()
        stale = _saved(earlier)
        stale.write_text('{}')
        disk_cleanup.save_teardown_locator(_locator(), VM, str(stale))
        workdir = tmp_path / 'work'
        boot = _qcow2(workdir / f'{VM}.qcow2')
        iso = _file(workdir / f'{VM}.install.iso', b'iso')
        t = _Teardown(workdir, disks=[boot], media=[iso], records=[])

        t.deprovision()

        assert not stale.exists()
        assert json.loads(open(_locator()).read())['inventory'] == str(
            _saved(workdir))


def _inventory(**fields):
    """A StorageInventory for VM, empty but for *fields*."""
    base = dict(vm_name=VM, disk_sources=[], media_sources=[], records=None,
                records_state=disk_cleanup.RECORDS_NONE, chains={},
                boot_family=[], legacy_disks=None)
    base.update(fields)
    return disk_cleanup.StorageInventory(**base)


class TestUnsearchableDirectories:
    """A file whose directory cannot be searched is not known to be gone,
    so it is kept -- and so are the saved inventory and the locator that
    protect it, which a teardown removes once nothing is kept (#208 review
    round 4, 1)."""

    @unless_root
    @pytest.mark.parametrize("site", [
        "recorded, moved by a snapshot", "recorded and attached",
        "declared by a legacy domain", "attached to a legacy domain",
        "attached, records unknown", "attached, records unreadable",
    ])
    def test_a_file_that_cannot_be_looked_up_is_kept(self, tmp_path, site):
        other = tmp_path / 'other'
        path = _qcow2(other / f'{VM}_disk01.qcow2')
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        sources = [str(boot)]
        chains = {str(boot): [str(boot)]}
        fields = {}
        if site == "recorded, moved by a snapshot":
            fields = dict(records=[_record('disk01', path)],
                          records_state=disk_cleanup.RECORDS_PRESENT)
        elif site == "declared by a legacy domain":
            fields = dict(legacy_disks=[str(path)])
        else:
            sources.append(str(path))
            chains[str(path)] = [str(path)]
            fields = {
                "recorded and attached": dict(
                    records=[_record('disk01', path, role=ROLE_ADOPTED)],
                    records_state=disk_cleanup.RECORDS_PRESENT),
                "attached to a legacy domain": dict(legacy_disks=[]),
                "attached, records unknown": dict(
                    records_state=disk_cleanup.RECORDS_UNKNOWN),
                "attached, records unreadable": dict(
                    records_state=disk_cleanup.RECORDS_UNREADABLE),
            }[site]
        inventory = _inventory(
            disk_sources=sources, chains=chains, boot_family=[str(boot)],
            identities=disk_cleanup.file_identities([str(boot), str(path)]),
            **fields)
        other.chmod(0)
        try:
            outcome = disk_cleanup.remove_vm_storage(
                inventory, [str(tmp_path), str(other)], lambda: {})
        finally:
            other.chmod(0o700)

        assert str(path) in [kept for kept, _ in outcome.kept]
        assert path.exists()

    @unless_root
    def test_a_kept_file_that_turns_unsearchable_keeps_the_inventory(
            self, tmp_path):
        """The kept list decides whether the inventory and its locator go:
        a kept file that cannot be looked up by then is still kept."""
        other = tmp_path / 'other'
        adopted = _qcow2(other / 'adopted.qcow2')
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        t = _Teardown(tmp_path, disks=[boot, adopted],
                      records=[_record('shared', adopted, role=ROLE_ADOPTED,
                                       target='vdb')])

        def scan():
            # after every decision, before the kept list is drawn up
            other.chmod(0)
            return {}

        t.session.disk_paths_in_use.side_effect = scan
        try:
            t.deprovision()
        finally:
            other.chmod(0o700)

        assert adopted.exists() and not boot.exists()
        assert _saved(tmp_path).exists() and os.path.lexists(_locator())
        assert str(adopted) in t.warnings


class TestUnlistableDirectories:
    """The files under the VM's own names are listed from its directories,
    and a teardown decides with that listing -- at capture, and in the
    fallback for a VM undefined with nothing saved. A directory counts as
    holding none only when it does not exist; one that cannot be listed
    stops the teardown, and an entry that cannot be looked up is still a
    candidate, kept by the refusals (#208 review round 4, residual)."""

    @unless_root
    def test_an_unlistable_workdir_at_capture_leaves_the_domain_defined(
            self, tmp_path):
        """(a) Nothing undefined, nothing removed, nothing saved."""
        workdir, elsewhere = tmp_path / 'work', tmp_path / 'elsewhere'
        boot = _qcow2(elsewhere / f'{VM}.qcow2')
        cdrom = _qcow2(workdir / f'{VM}.cd.qcow2')
        t = _Teardown(workdir, disks=[boot], media=[cdrom], records=[])
        workdir.chmod(0)
        try:
            with pytest.raises(ProvisionError, match='could not list') as exc:
                t.deprovision()
        finally:
            workdir.chmod(0o700)

        assert f'could not list {workdir} (' in str(exc.value)
        assert 'leaving it defined' in str(exc.value)
        assert 'make that directory readable' in str(exc.value)
        t.session.destroy_vm.assert_not_called()
        assert boot.exists() and cdrom.exists()
        assert not _saved(elsewhere).exists()
        assert not os.path.lexists(_locator())

    @unless_root
    def test_a_listable_unsearchable_workdir_keeps_what_is_under_the_names(
            self, tmp_path):
        """(b) Every entry there fails to be looked up; the VM-named qcow2
        CD-ROM is still a candidate, so it is kept, and so are the saved
        inventory and the locator."""
        workdir, elsewhere = tmp_path / 'work', tmp_path / 'elsewhere'
        boot = _qcow2(elsewhere / f'{VM}.qcow2')
        cdrom = _qcow2(workdir / f'{VM}.cd.qcow2')
        t = _Teardown(workdir, disks=[boot], media=[cdrom], records=[])
        workdir.chmod(0o400)
        try:
            t.deprovision()
        finally:
            workdir.chmod(0o700)

        assert cdrom.exists()
        assert f'left {cdrom} in place' in t.warnings
        assert _saved(elsewhere).exists() and os.path.lexists(_locator())

    @unless_root
    def test_the_fallback_with_an_unlistable_workdir_removes_nothing(
            self, tmp_path):
        """(c) Undefined, nothing saved: a workdir that can be searched but
        not listed stops the name-based fallback."""
        workdir = tmp_path / 'w'
        boot = _qcow2(workdir / f'{VM}.qcow2')
        t = _Teardown(workdir, records=[])
        t.session.vm_storage_devices.return_value = None
        workdir.chmod(0o100)
        try:
            with pytest.raises(ProvisionError, match='could not list') as exc:
                t.deprovision()
        finally:
            workdir.chmod(0o700)

        assert f'could not list {workdir} (' in str(exc.value)
        assert boot.exists()

    # -- only the cluster workdirs are listed strictly -----------------------

    @unless_root
    def test_update_removes_a_vm_whose_adopted_disk_sits_unlistable(
            self, tmp_path):
        """An adopted disk in a directory the user cannot list -- Ubuntu's
        /var/lib/libvirt/images is root's, 0711, so searchable but not
        listable to a user, as 0111 is to its owner -- is outside every
        cluster workdir, where nothing is removed: listing it strictly only
        made update unable to remove the VM."""
        workdir, outside = tmp_path / 'work', tmp_path / 'outside'
        boot = _qcow2(workdir / f'{VM}.qcow2')
        adopted = _qcow2(outside / 'adopted.qcow2')
        t = _Teardown(workdir, disks=[boot, adopted],
                      records=[_record('shared', adopted, role=ROLE_ADOPTED,
                                       target='vdb')])
        outside.chmod(0o111)
        try:
            t.update_remove()
        finally:
            outside.chmod(0o700)

        t.session.destroy_vm.assert_called()
        assert not boot.exists()
        assert adopted.exists()
        assert (f"left {adopted} in place because boxman attached it but did "
                f"not create it (role 'adopted')") in t.warnings

    @unless_root
    def test_an_adopted_disk_that_cannot_be_looked_up_keeps_everything(
            self, tmp_path):
        """Its directory cannot even be searched (000): whether the adopted
        disk is another name of a file under the VM's names cannot be told,
        so nothing is removed -- the VM is undefined, and the warning says
        what to make accessible."""
        workdir, outside = tmp_path / 'work', tmp_path / 'outside'
        boot = _qcow2(workdir / f'{VM}.qcow2')
        adopted = _qcow2(outside / 'adopted.qcow2')
        t = _Teardown(workdir, disks=[boot, adopted],
                      records=[_record('shared', adopted, role=ROLE_ADOPTED,
                                       target='vdb')])
        outside.chmod(0)
        try:
            t.update_remove()
        finally:
            outside.chmod(0o700)

        t.session.destroy_vm.assert_called()
        assert boot.exists() and adopted.exists()
        assert (f'the identity of {adopted} could not be read'
                in t.warnings)
        assert 'make it accessible' in t.warnings
        assert _saved(workdir).exists() and os.path.lexists(_locator())

    @unless_root
    def test_update_with_an_unlistable_workdir_leaves_the_domain_defined(
            self, tmp_path):
        """The update path lists the directories of the attached disks; the
        cluster workdir among them is still listed strictly."""
        workdir = tmp_path / 'work'
        boot = _qcow2(workdir / f'{VM}.qcow2')
        t = _Teardown(workdir, disks=[boot], records=[])
        workdir.chmod(0)
        try:
            with pytest.raises(ProvisionError, match='could not list') as exc:
                t.update_remove()
        finally:
            workdir.chmod(0o700)

        assert f'could not list {workdir} (' in str(exc.value)
        t.session.destroy_vm.assert_not_called()
        assert boot.exists()

    @unless_root
    def test_a_workdir_reached_through_a_symlink_is_listed_strictly(
            self, tmp_path):
        """A cluster workdir is recognised by its resolved path, whichever
        way the config or libvirt spells it."""
        real = tmp_path / 'real'
        boot = _qcow2(real / f'{VM}.qcow2')
        link = tmp_path / 'link'
        link.symlink_to(real)
        t = _Teardown(link, disks=[boot], records=[])
        real.chmod(0)
        try:
            with pytest.raises(ProvisionError, match='could not list'):
                t.update_remove()
        finally:
            real.chmod(0o700)

        t.session.destroy_vm.assert_not_called()
        assert boot.exists()

    # -- #208 review round 5: a directory is skipped only once it is proved
    # -- to be none of the cluster workdirs, by identity ---------------------

    @unless_root
    def test_an_unresolvable_workdir_alias_stops_the_teardown(self, tmp_path):
        """Codex's case: the workdir is configured as ``hidden/work ->
        real``, libvirt names the sources under ``real``, ``hidden`` is 000
        and ``real`` 0300 (writable and searchable, not listable). A path
        comparison could not resolve the alias, took ``real`` for an
        outside directory and skipped it: the CD-ROM was never a
        candidate, the inventory and locator were dropped, and a later
        retry deleted it."""
        hidden, real = tmp_path / 'hidden', tmp_path / 'real'
        hidden.mkdir()
        boot = _qcow2(real / f'{VM}.qcow2')
        cdrom = _qcow2(real / f'{VM}.cd.qcow2')
        (hidden / 'work').symlink_to(real)
        t = _Teardown(hidden / 'work', disks=[boot], media=[cdrom],
                      records=[])
        hidden.chmod(0)
        real.chmod(0o300)
        try:
            with pytest.raises(ProvisionError,
                               match='could not resolve') as exc:
                t.update_remove()
        finally:
            hidden.chmod(0o700)
            real.chmod(0o700)

        assert str(hidden / 'work') in str(exc.value)
        assert 'make it accessible' in str(exc.value)
        t.session.destroy_vm.assert_not_called()
        assert boot.exists() and cdrom.exists()
        assert not _saved(real).exists()
        assert not os.path.lexists(_locator())

        # the modes restored, the VM is still defined and a retry keeps it
        t.update_remove()

        assert cdrom.exists() and not boot.exists()
        assert f'left {cdrom} in place because it is a CD-ROM' in t.warnings

    @unless_root
    def test_an_outside_directory_whose_identity_cannot_be_read_stops_it(
            self, tmp_path):
        """An unlistable directory under a parent that cannot be searched
        cannot be proved to be none of the cluster workdirs."""
        workdir, parent = tmp_path / 'work', tmp_path / 'parent'
        boot = _qcow2(workdir / f'{VM}.qcow2')
        adopted = _qcow2(parent / 'outside' / 'adopted.qcow2')
        t = _Teardown(workdir, disks=[boot, adopted],
                      records=[_record('shared', adopted, role=ROLE_ADOPTED,
                                       target='vdb')])
        parent.chmod(0)
        try:
            with pytest.raises(ProvisionError,
                               match='could not resolve') as exc:
                t.update_remove()
        finally:
            parent.chmod(0o700)

        assert str(parent / 'outside') in str(exc.value)
        t.session.destroy_vm.assert_not_called()
        assert boot.exists() and adopted.exists()

    @unless_root
    def test_a_configured_workdir_that_does_not_exist_is_ignored(
            self, tmp_path):
        """A workdir that is not there cannot be the unlistable directory."""
        workdir, outside = tmp_path / 'work', tmp_path / 'outside'
        boot = _qcow2(workdir / f'{VM}.qcow2')
        adopted = _qcow2(outside / 'adopted.qcow2')
        t = _Teardown(workdir, disks=[boot, adopted],
                      records=[_record('shared', adopted, role=ROLE_ADOPTED,
                                       target='vdb')])
        t.mgr.config['clusters']['cluster_2'] = {
            'workdir': str(tmp_path / 'never-created')}
        outside.chmod(0o111)
        try:
            t.update_remove()
        finally:
            outside.chmod(0o700)

        t.session.destroy_vm.assert_called()
        assert not boot.exists() and adopted.exists()

    def test_a_directory_gone_since_it_failed_to_list_holds_nothing(
            self, tmp_path, monkeypatch):
        """Listing failed, then the directory vanished: nothing is in it."""
        workdir, gone = tmp_path / 'work', tmp_path / 'gone'
        boot = _qcow2(workdir / f'{VM}.qcow2')
        adopted = gone / 'adopted.qcow2'
        t = _Teardown(workdir, disks=[boot, adopted],
                      records=[_record('shared', adopted, role=ROLE_ADOPTED,
                                       target='vdb')])
        real_listing = disk_cleanup.boot_family_candidates

        def listing(directory, vm_name):
            if directory == str(gone):
                raise PermissionError(13, 'Permission denied', directory)
            return real_listing(directory, vm_name)

        monkeypatch.setattr('boxman.manager_parts.vms.boot_family_candidates',
                            listing)

        t.update_remove()

        t.session.destroy_vm.assert_called()
        assert not boot.exists()

    def test_a_directory_that_lists_fine_gives_what_it_always_did(
            self, tmp_path):
        for name in (f'{VM}.qcow2', f'{VM}.s1', f'{VM}.', f'{VM}.iso',
                     f'{VM}_snapshot_s1.raw', f'{VM}_disk01.qcow2',
                     f'{VM}2.qcow2', 'other.qcow2'):
            _file(tmp_path / name)
        (tmp_path / f'{VM}.d').mkdir()
        (tmp_path / f'{VM}_snapshot_d').mkdir()
        (tmp_path / f'{VM}.link').symlink_to(tmp_path / f'{VM}.qcow2')
        (tmp_path / f'{VM}.dir-link').symlink_to(tmp_path / f'{VM}.d')
        (tmp_path / f'{VM}.dangling').symlink_to(tmp_path / 'nowhere')

        listed = disk_cleanup.boot_family_candidates(str(tmp_path), VM)

        assert listed == disk_cleanup.boot_family_files(str(tmp_path), VM)
        assert len(listed) == 8
        assert disk_cleanup.boot_family_candidates(
            str(tmp_path / 'missing'), VM) == []

    @unless_root
    @pytest.mark.parametrize("mode, listed", [(0o000, None), (0o100, None),
                                              (0o400, 2)])
    def test_what_a_directory_that_cannot_be_read_gives(self, tmp_path,
                                                        mode, listed):
        workdir = tmp_path / 'w'
        _qcow2(workdir / f'{VM}.qcow2')
        _file(workdir / f'{VM}_snapshot_s1.raw')
        _file(workdir / 'other.qcow2')
        workdir.chmod(mode)
        try:
            if listed is None:
                with pytest.raises(PermissionError):
                    disk_cleanup.boot_family_candidates(str(workdir), VM)
            else:
                assert disk_cleanup.boot_family_candidates(
                    str(workdir), VM) == [str(workdir / f'{VM}.qcow2'),
                                          str(workdir / f'{VM}_snapshot_s1.raw')]
        finally:
            workdir.chmod(0o700)


class TestLiteralVmNames:
    """The VM's names are matched literally, as every other name rule of
    the teardown is: a VM whose name holds a glob metacharacter lists only
    the files under its own names, never another VM's (#208 review round
    4, residual)."""

    @pytest.mark.parametrize("vm", ["node[1]", "node*", "node?"])
    def test_only_files_under_its_literal_names_are_listed(self, tmp_path,
                                                            vm):
        own = [_file(tmp_path / f'{vm}.qcow2'),
               _file(tmp_path / f'{vm}_snapshot_s1.raw')]
        for name in ('node1.qcow2', 'nodeX.qcow2', 'node1_snapshot_x'):
            _file(tmp_path / name)

        assert disk_cleanup.boot_family_candidates(str(tmp_path), vm) == (
            sorted(str(path) for path in own))

    def test_removing_node_1_leaves_an_undefined_node1_alone(self, tmp_path):
        prefix = 'bprj__demo__bprj_cluster_1_'
        mine = _qcow2(tmp_path / f'{prefix}node[1].qcow2')
        # the boot disk of a VM node1 that is not defined any more, so no
        # in-use scan can speak for it
        other = _qcow2(tmp_path / f'{prefix}node1.qcow2')
        t = _Teardown(tmp_path, disks=[mine], records=[])

        t.mgr._destroy_vm_and_disks('cluster_1', {'workdir': str(tmp_path)},
                                    'node[1]', {'disks': []})

        assert not mine.exists()
        assert other.exists()


class TestTheSameFileUnderAnotherName:
    """A protected file is recognised by its identity too, not only by its
    resolved path: under a bind mount, a hard link or a path boxman cannot
    resolve, the same file has another name that no resolved path
    unifies. A hard link models that here (#208, the alias review)."""

    def test_a_cdrom_also_named_for_the_vm_is_kept(self, tmp_path):
        """Scenario 1: the CD-ROM is attached by one name, and the same file
        sits under the VM's names in its workdir."""
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        iso = _file(tmp_path / 'isos' / 'install.iso', b'iso')
        named = _link(iso, tmp_path / f'{VM}.install.iso')
        t = _Teardown(tmp_path, disks=[boot], media=[iso], records=[])

        t.deprovision()

        assert named.exists() and not boot.exists()
        assert f'left {named} in place because it is a CD-ROM' in t.warnings

    def test_a_cdrom_given_through_a_symlink_to_another_name_is_kept(
            self, tmp_path):
        """The CD-ROM's path is a symlink to another name of the file: only
        its identity read now, links followed, is the file's."""
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        named = _file(tmp_path / f'{VM}.install.iso', b'iso')
        other = _link(named, tmp_path / 'isos' / 'install.iso')
        cdrom = tmp_path / 'isos' / 'current.iso'
        cdrom.symlink_to(other)
        t = _Teardown(tmp_path, disks=[boot], media=[cdrom], records=[])

        t.deprovision()

        assert named.exists() and not boot.exists()
        assert f'left {named} in place because it is a CD-ROM' in t.warnings

    def test_an_image_under_a_qcow2_cdrom_by_another_name_is_kept(
            self, tmp_path):
        """Scenario 2: a qcow2 CD-ROM is built on the boot disk, named by
        another path."""
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        base = _link(boot, tmp_path / 'isos' / 'base.qcow2')
        cdrom = _qcow2(tmp_path / 'isos' / 'cd.qcow2')
        t = _Teardown(tmp_path, disks=[boot], media=[cdrom], records=[])
        t.session.backing_chains.side_effect = _live({cdrom: [cdrom, base]})

        t.deprovision()

        assert boot.exists()
        assert f'left {boot} in place because it backs a CD-ROM' in t.warnings

    @needs_qemu_img
    def test_a_file_another_domain_uses_by_another_name_is_kept(
            self, tmp_path):
        """Scenario 3, through the real in-use scan (real qemu-img, a
        mocked virsh that knows one other domain)."""
        boot = _image(tmp_path / f'{VM}.qcow2')
        alias = _link(boot, tmp_path / 'other' / 'disk.qcow2')
        t = _Teardown(tmp_path, disks=[boot], records=[])
        blk = (" Type   Device   Target   Source\n"
               "------------------------------------\n"
               f" file   disk     vda      {alias}\n")

        def virsh_execute(*args, **kwargs):
            if args[0] == 'list':
                # defined, not running
                return _virsh_result('vm-other\n' if '--all' in args else '')
            return _virsh_result(blk)

        real = LibVirtSession(config={'provider': {'libvirt': {}}})
        t.session.disk_paths_in_use.side_effect = real.disk_paths_in_use
        with patch('boxman.providers.libvirt.session.VirshCommand') as virsh:
            virsh.return_value.execute.side_effect = virsh_execute
            t.deprovision()

        assert boot.exists()
        assert f'left {boot} in place because domain vm-other uses it' in (
            t.warnings)

    def test_an_adopted_disk_also_named_for_the_vm_is_kept(self, tmp_path):
        """Scenario 4: an adopted disk is attached by one name, and the same
        file sits under the VM's names in its workdir."""
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        adopted = _qcow2(tmp_path / 'shared' / 'data.qcow2')
        named = _link(adopted, tmp_path / f'{VM}.data.qcow2')
        t = _Teardown(tmp_path, disks=[boot, adopted],
                      records=[_record('shared', adopted, role=ROLE_ADOPTED,
                                       target='vdb')])

        t.deprovision()

        assert named.exists() and adopted.exists()
        assert not boot.exists()

    def test_a_chain_kept_by_another_name_keeps_its_layer(self, tmp_path):
        """The chain-whole rule: a boot overlay is kept because its base,
        named by another path, is not the VM's -- the same file under the
        VM's names is kept too."""
        base = _qcow2(tmp_path / f'{VM}.qcow2')
        alias = _link(base, tmp_path / 'shared' / 'base.qcow2')
        head = _qcow2(tmp_path / f'{VM}.s1')
        t = _Teardown(tmp_path, disks=[head], records=[])
        t.session.backing_chains.side_effect = _live({head: [head, alias]})

        t.deprovision()

        assert base.exists() and head.exists()

    @needs_qemu_img
    def test_a_head_kept_by_another_names_refusal_keeps_its_own_base(
            self, tmp_path):
        """Codex's case (#208 review round 6, 1): one head, hard-linked
        into two workdirs, whose relative backing name resolves to a
        different base in each. Replacing it under one name keeps it under
        both -- and each name's own base with it."""
        a, b = tmp_path / 'a', tmp_path / 'b'
        base_a = _image(a / f'{VM}.qcow2')
        base_b = _image(b / f'{VM}.qcow2')
        head_a = a / f'{VM}.s1'
        subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', '-b',
                        f'{VM}.qcow2', '-F', 'qcow2', str(head_a)],
                       check=True)
        head_b = _link(head_a, b / f'{VM}.s1')
        t = _Teardown(a, disks=[head_a, head_b], records=[])
        t.mgr.config['clusters']['cluster_2'] = {'workdir': str(b)}
        _production_chains(t)

        def replace(*_args, **_kwargs):
            fresh = a / 'fresh'
            fresh.write_bytes(b'someone else')
            fresh.replace(head_a)

        t.session.destroy_vm.side_effect = replace

        t.update_remove()

        assert head_a.read_bytes() == b'someone else'
        assert base_a.exists() and head_b.exists() and base_b.exists()
        chain = subprocess.run(['qemu-img', 'info', '--backing-chain', '-U',
                                str(head_b)], capture_output=True)
        assert chain.returncode == 0, chain.stderr

    def test_a_name_already_unlinked_holds_nothing_up(self, tmp_path):
        """The head's first name is unlinked before its second is found
        replaced: the first name's base no longer holds anything up, and
        goes; the second keeps its own."""
        a, b = tmp_path / 'a', tmp_path / 'b'
        base_a, base_b = _qcow2(a / f'{VM}.qcow2'), _qcow2(b / f'{VM}.qcow2')
        head_a = _qcow2(a / f'{VM}.s1')
        head_b = _link(head_a, b / f'{VM}.s1')
        t = _Teardown(a, disks=[head_a, head_b], records=[])
        t.mgr.config['clusters']['cluster_2'] = {'workdir': str(b)}
        t.session.backing_chains.side_effect = _live(
            {head_a: [head_a, base_a], head_b: [head_b, base_b]})

        def replace(*_args, **_kwargs):
            fresh = b / 'fresh'
            fresh.write_bytes(b'QFI\xfb someone else')
            fresh.replace(head_b)

        t.session.destroy_vm.side_effect = replace

        t.update_remove()

        assert not head_a.exists() and not base_a.exists()
        assert head_b.read_bytes() == b'QFI\xfb someone else'
        assert base_b.exists()

    def test_a_layer_kept_by_withdrawal_keeps_what_is_below_it_too(
            self, tmp_path):
        """Transitively: a kept head keeps its middle layer, whose other
        name heads another chain -- which keeps its own base."""
        a1, a2 = _qcow2(tmp_path / f'{VM}.a1'), _qcow2(tmp_path / f'{VM}.a2')
        b1 = _link(a2, tmp_path / f'{VM}.b1')
        b2 = _qcow2(tmp_path / f'{VM}.b2')
        t = _Teardown(tmp_path, disks=[a1, b1], records=[])
        t.session.backing_chains.side_effect = _live({a1: [a1, a2],
                                                      b1: [b1, b2]})

        def replace(*_args, **_kwargs):
            fresh = tmp_path / 'fresh'
            fresh.write_bytes(b'QFI\xfb someone else')
            fresh.replace(a1)

        t.session.destroy_vm.side_effect = replace

        t.deprovision()

        assert a1.read_bytes() == b'QFI\xfb someone else'
        assert a2.exists() and b1.exists() and b2.exists()

    def test_a_data_disks_file_is_never_decided_by_another_name(
            self, tmp_path):
        """Extra-disk files are never decided by name -- nor under another
        name of the same file among the VM's names."""
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        data = _qcow2(tmp_path / f'{VM}_disk01.qcow2')
        other = _link(data, tmp_path / f'{VM}.disk01-copy')
        t = _Teardown(tmp_path, disks=[boot, data],
                      records=[_record('disk01', data)])

        t.deprovision([{'name': 'disk01'}])

        assert not boot.exists() and not data.exists()
        assert other.exists()

    def test_a_snapshot_chain_holding_a_cdrom_by_another_name_is_kept(
            self, tmp_path):
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        base = _qcow2(tmp_path / f'{VM}_disk01.qcow2')
        head = _qcow2(tmp_path / f'{VM}_disk01.s1')
        cdrom = _link(head, tmp_path / 'isos' / 'cd.iso')
        t = _Teardown(tmp_path, disks=[boot, head], media=[cdrom],
                      records=[_record('disk01', base)])
        t.session.backing_chains.side_effect = _live({head: [head, base]})

        t.deprovision([{'name': 'disk01'}])

        assert head.exists() and base.exists()
        assert f'{head} in its chain is a media source of the vm' in (
            t.warnings)

    def test_a_refusal_under_one_name_blocks_admitting_the_other(
            self, tmp_path):
        one = _file(tmp_path / 'one')
        other = _link(one, tmp_path / 'other')
        admission = disk_cleanup._Admission(disk_cleanup._Identities(
            disk_cleanup.file_identities([str(one), str(other)]), ()))

        admission.refuse(str(one), 'kept')
        admission.admit(str(other))

        assert admission.admitted == {}

    def test_a_refusal_under_one_name_withdraws_the_others_admission(
            self, tmp_path):
        one = _file(tmp_path / 'one')
        other = _link(one, tmp_path / 'other')
        admission = disk_cleanup._Admission(disk_cleanup._Identities(
            disk_cleanup.file_identities([str(one), str(other)]), ()))

        admission.admit(str(other))
        admission.refuse(str(one), 'kept')

        assert admission.admitted == {}
        assert not admission.is_admitted_as(str(other))

    @unless_root
    def test_a_protected_path_that_cannot_be_resolved_keeps_everything(
            self, tmp_path):
        """The CD-ROM is named through a directory boxman cannot search, so
        whether it is one of the files under the VM's names cannot be told:
        everything is kept, and the warning names it."""
        workdir, hidden = tmp_path / 'w', tmp_path / 'hidden'
        boot = _qcow2(workdir / f'{VM}.qcow2')
        named = _file(workdir / f'{VM}.install.iso', b'iso')
        hidden.mkdir()
        (hidden / 'link').symlink_to(workdir)
        cdrom = hidden / 'link' / f'{VM}.install.iso'
        t = _Teardown(workdir, disks=[boot], media=[cdrom], records=[])
        hidden.chmod(0)
        try:
            t.deprovision()
        finally:
            hidden.chmod(0o700)

        assert boot.exists() and named.exists()
        assert str(cdrom) in t.warnings
        assert 'make it accessible' in t.warnings
        assert _saved(workdir).exists()


class TestRunningDomainWithABlockJob:
    """Another running domain's block copy writes into its destination:
    a file under this VM's names that is one is kept (#208, the mirror
    analysis)."""

    @needs_qemu_img
    def test_a_block_copy_into_a_file_under_the_vms_names_keeps_it(
            self, tmp_path):
        boot = _image(tmp_path / f'{VM}.qcow2')
        destination = _image(tmp_path / f'{VM}.copy.qcow2')
        source = _image(tmp_path / 'other' / 'disk.qcow2')
        t = _Teardown(tmp_path, disks=[boot], records=[])
        blk = (" Type   Device   Target   Source\n"
               "------------------------------------\n"
               f" file   disk     vda      {source}\n")
        live = ("<domain><devices><disk type='file' device='disk'>"
                f"<source file='{source}' index='1'/><backingStore/>"
                f"<mirror type='file' file='{destination}' format='qcow2' "
                "job='copy'><format type='qcow2'/>"
                f"<source file='{destination}' index='2'/><backingStore/>"
                "</mirror><target dev='vda'/></disk></devices></domain>")

        def virsh_execute(*args, **kwargs):
            if args[0] == 'list':
                return _virsh_result('vm-other\n')
            if args[0] == 'dumpxml':
                return _virsh_result(live)
            return _virsh_result(blk)

        real = LibVirtSession(config={'provider': {'libvirt': {}}})
        t.session.disk_paths_in_use.side_effect = real.disk_paths_in_use
        with patch('boxman.providers.libvirt.session.VirshCommand') as virsh:
            virsh.return_value.execute.side_effect = virsh_execute
            t.deprovision()

        assert destination.exists()
        assert (f'left {destination} in place because domain vm-other uses '
                f'it') in t.warnings
        assert not boot.exists()


class TestRunningDomainWithAMissingSource:
    """Another domain still running with a deleted image attached can hold
    it open in QEMU together with the images below it: a base of it that
    still exists must not read as unused (#208 review round 4, 2)."""

    @needs_qemu_img
    def test_a_mirror_on_a_second_disk_with_the_same_source_keeps_all(
            self, tmp_path):
        """#208 review round 6, 2: the same deleted head on two live disks,
        a block copy running on the second into a file under this VM's
        names. The scan cannot tell what that domain holds, so it fails,
        and the copy's destination is kept."""
        boot = _qcow2(tmp_path / f'{VM}.qcow2')
        destination = _qcow2(tmp_path / f'{VM}.copy.qcow2')
        base = _image(tmp_path / 'other' / 'base.qcow2')
        head = _image(tmp_path / 'other' / 'head.qcow2', backing=base)
        head.unlink()
        t = _Teardown(tmp_path, disks=[boot], records=[])
        blk = (" Type   Device   Target   Source\n"
               "------------------------------------\n"
               f" file   disk     vda      {head}\n"
               f" file   disk     vdb      {head}\n")
        chain = ("<backingStore type='file' index='2'><format type='qcow2'/>"
                 f"<source file='{base}'/><backingStore/></backingStore>")
        live = ("<domain><devices>"
                "<disk type='file' device='disk'>"
                f"<source file='{head}' index='1'/>{chain}"
                "<target dev='vda'/><readonly/></disk>"
                "<disk type='file' device='disk'>"
                f"<source file='{head}' index='3'/>{chain}"
                "<mirror type='file' job='copy' ready='yes'>"
                f"<format type='qcow2'/><source file='{destination}'/>"
                "</mirror><target dev='vdb'/><readonly/></disk>"
                "</devices></domain>")

        def virsh_execute(*args, **kwargs):
            if args[0] == 'list':
                return _virsh_result('vm-other\n')
            if args[0] == 'domstate':
                return _virsh_result('running\n')
            if args[0] == 'dumpxml':
                return _virsh_result(live)
            return _virsh_result(blk)

        real = LibVirtSession(config={'provider': {'libvirt': {}}})
        t.session.disk_paths_in_use.side_effect = real.disk_paths_in_use
        with patch('boxman.providers.libvirt.session.VirshCommand') as virsh:
            virsh.return_value.execute.side_effect = virsh_execute
            t.deprovision()

        assert destination.exists() and boot.exists()
        assert (f'left {destination} in place because could not check '
                f'whether another domain uses it') in t.warnings

    @needs_qemu_img
    def test_the_base_of_an_unlinked_head_it_holds_is_kept(self, tmp_path):
        """The running domain holds exactly the chain libvirt records in
        its live definition: the base is kept as used by it, and an
        unrelated file of the VM is removed as usual."""
        boot = _image(tmp_path / f'{VM}.qcow2')
        memory = _file(tmp_path / f'{VM}_snapshot_s1.raw')
        head = _image(tmp_path / 'other' / 'head.qcow2', backing=boot)
        head.unlink()
        t = _Teardown(tmp_path, disks=[boot], records=[])
        blk = (" Type   Device   Target   Source\n"
               "------------------------------------\n"
               f" file   disk     vda      {head}\n")
        live = ("<domain><devices><disk type='file' device='disk'>"
                f"<source file='{head}' index='1'/>"
                "<backingStore type='file' index='2'><format type='qcow2'/>"
                f"<source file='{boot}'/><backingStore/></backingStore>"
                "<target dev='vda'/></disk></devices></domain>")

        def virsh_execute(*args, **kwargs):
            if args[0] == 'list':
                return _virsh_result('vm-other\n')
            if args[0] == 'domstate':
                return _virsh_result('running\n')
            if args[0] == 'dumpxml':
                return _virsh_result(live)
            return _virsh_result(blk)

        # the real in-use scan: real qemu-img and absence probe, a mocked
        # virsh that knows one other, running, domain
        real = LibVirtSession(config={'provider': {'libvirt': {}}})
        t.session.disk_paths_in_use.side_effect = real.disk_paths_in_use
        with patch('boxman.providers.libvirt.session.VirshCommand') as virsh:
            virsh.return_value.execute.side_effect = virsh_execute
            t.deprovision()

        assert boot.exists()
        assert (f'left {boot} in place because domain vm-other uses it'
                in t.warnings)
        assert not memory.exists()


def _malformed(tmp_path, layout):
    """A saved inventory for VM, well-formed JSON, bent into *layout*."""
    base = disk_cleanup.StorageInventory(
        vm_name=VM, disk_sources=['/w/a'], media_sources=[],
        records=[_record('disk01', '/w/a')], records_state='present',
        chains={'/w/a': ['/w/a']}, boot_family=['/w/a'], legacy_disks=None,
        identities={'/w/a': (1, 2)}, targets={'/w/a': 'vdb'})
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    data = json.loads(open(disk_cleanup.save_teardown_inventory(
        base, str(scratch))).read())
    record = data['records'][0]
    return {
        "a list": [],
        "a string": "inventory",
        "version true": {**data, "version": True},
        "present records missing": {**data, "records": None},
        "a record of numbers": {**data, "records": [{**record, "name": 1}]},
        "a record short of a field": {
            **data, "records": [{k: v for k, v in record.items()
                                 if k != "role"}]},
        "disk sources a string": {**data, "disk_sources": "/w/a"},
        "a chain a string": {**data, "chains": {"/w/a": "/w/a"}},
        "identities of booleans": {**data,
                                   "identities": {"/w/a": [True, False]}},
        "an identity of three": {**data, "identities": {"/w/a": [1, 2, 3]}},
        "targets of lists": {**data, "targets": {"/w/a": ["vdb"]}},
        "boot family of numbers": {**data, "boot_family": [1]},
    }[layout]
