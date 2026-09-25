"""
The storage-removal routine shared by every VM teardown (#208).

``deprovision`` / ``destroy`` / ``provision --force`` / ``up --force`` reach
it through ``_destroy_vm_and_disks``, ``update`` through
``_destroy_removed_vm``. libvirt deletes nothing any more; these tests drive
the routine with real files in a temporary workdir and a mocked session.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from boxman.exceptions import ProvisionError
from boxman.providers.libvirt.disk_ownership import (
    ROLE_ADOPTED,
    ROLE_DATA,
    DiskRecord,
)
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


class _Teardown:
    """A bare manager whose session reports *disks* and *media* for VM."""

    def __init__(self, workdir, *, disks=(), media=(), records=None,
                 records_error=None, in_use=None, chains=None):
        self.workdir = workdir
        self.mgr = make_bare_manager(
            {'project': 'demo',
             'clusters': {'cluster_1': {'workdir': str(workdir)}}})
        session = self.session = MagicMock()
        session.vm_storage_devices.return_value = (
            [DomblkRow('file', 'disk', f'vd{chr(97 + i)}', str(p))
             for i, p in enumerate(disks)]
            + [DomblkRow('file', 'cdrom', f'sd{chr(97 + i)}', str(p))
               for i, p in enumerate(media)])
        session.confirm_vm_absent.return_value = True
        if chains is None:
            session.backing_chain_files.side_effect = (
                lambda sources: sorted(str(s) for s in sources))
        else:
            session.backing_chain_files.return_value = chains
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

    def test_a_data_disk_moved_by_a_snapshot_is_kept_whole(self, tmp_path):
        """Deferred, as #212 left it: the record names only the base, and
        nothing recorded names the overlays, so the chain is kept whole and
        named rather than half removed."""
        _file(tmp_path / f'{VM}.qcow2')
        data_base = _file(tmp_path / f'{VM}_disk01.qcow2')
        data_head = _file(tmp_path / f'{VM}_disk01.s1')
        t = _Teardown(tmp_path, disks=[data_head],
                      records=[_record('disk01', data_base)])

        t.deprovision([{'name': 'disk01'}])

        assert data_base.exists() and data_head.exists()
        assert not (tmp_path / f'{VM}.qcow2').exists()
        assert str(data_base) in t.warnings and str(data_head) in t.warnings


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
