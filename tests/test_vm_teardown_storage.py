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
from unittest.mock import MagicMock, patch

import pytest

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


#: default for ``_Teardown(chains=...)``: every disk a standalone image
STANDALONE = object()


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
            session.backing_chains.side_effect = (
                lambda sources: {str(s): [str(s)] for s in sources})
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

        t.session.refresh_pools_holding.assert_called_once_with([str(boot)])

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

        boot = _file(tmp_path / f'{VM}.qcow2')
        t.session.vm_storage_devices.return_value = None
        t.session.confirm_vm_absent.return_value = True
        t.mgr.logger.reset_mock()

        t.deprovision([{'name': 'disk01'}])

        assert adopted.exists()
        assert str(adopted) in t.warnings
        # the boot family, under the VM's exclusive names, still goes
        assert not boot.exists()
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
