"""
Unit tests for boxman.providers.libvirt.disk_cleanup.remove_vm_disks.

Part of Phase 2.6 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).

Pins the contract that was previously inline in
:meth:`LibVirtSession.destroy_disks` — guards against a future session
refactor accidentally changing which files are swept.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from boxman.providers.libvirt.disk_cleanup import (
    _remove_if_unchanged,
    file_identities,
    remove_vm_disks,
)

pytestmark = pytest.mark.unit


class TestRemoveVmDisks:

    def test_removes_boot_disk(self, tmp_path: Path):
        (tmp_path / "vm01.qcow2").write_bytes(b"x")
        assert remove_vm_disks(str(tmp_path), "vm01", []) is True
        assert not (tmp_path / "vm01.qcow2").exists()

    def test_removes_named_extra_disks(self, tmp_path: Path):
        (tmp_path / "vm01.qcow2").write_bytes(b"x")
        (tmp_path / "vm01_data.qcow2").write_bytes(b"x")
        (tmp_path / "vm01_logs.qcow2").write_bytes(b"x")
        remove_vm_disks(str(tmp_path), "vm01", [
            {"name": "data"}, {"name": "logs"},
        ])
        assert not (tmp_path / "vm01_data.qcow2").exists()
        assert not (tmp_path / "vm01_logs.qcow2").exists()

    def test_sweeps_snapshot_artifacts(self, tmp_path: Path):
        # timestamp-suffixed overlay + memory snapshot .raw
        (tmp_path / "vm01.2026-04-21T08:00:00").write_bytes(b"x")
        (tmp_path / "vm01.1772465824").write_bytes(b"x")
        (tmp_path / "vm01_snapshot_baseline.raw").write_bytes(b"x")
        remove_vm_disks(str(tmp_path), "vm01", [])
        for leftover in tmp_path.glob("vm01*"):
            assert not leftover.exists()

    def test_leaves_other_vms_untouched(self, tmp_path: Path):
        (tmp_path / "vm01.qcow2").write_bytes(b"x")
        (tmp_path / "vm02.qcow2").write_bytes(b"x")
        (tmp_path / "other-vm.qcow2").write_bytes(b"x")
        remove_vm_disks(str(tmp_path), "vm01", [])
        # vm01 gone, others preserved
        assert not (tmp_path / "vm01.qcow2").exists()
        assert (tmp_path / "vm02.qcow2").exists()
        assert (tmp_path / "other-vm.qcow2").exists()

    def test_prefix_collision_leaves_longer_named_vm_untouched(
        self, tmp_path: Path
    ):
        """Regression for issue #85 item 1: destroying ``web`` in a
        shared workdir must not delete files of a VM named ``web2``."""
        # web's own files
        (tmp_path / "web.qcow2").write_bytes(b"x")
        (tmp_path / "web_data.qcow2").write_bytes(b"x")
        (tmp_path / "web.1772465824").write_bytes(b"x")
        (tmp_path / "web_snapshot_baseline.raw").write_bytes(b"x")
        # web2's files — same workdir, name starts with "web"
        (tmp_path / "web2.qcow2").write_bytes(b"x")
        (tmp_path / "web2_data.qcow2").write_bytes(b"x")
        (tmp_path / "web2.1772465824").write_bytes(b"x")
        (tmp_path / "web2_snapshot_baseline.raw").write_bytes(b"x")

        remove_vm_disks(str(tmp_path), "web", [{"name": "data"}])

        for leftover in tmp_path.glob("web.*"):
            assert not leftover.exists()
        assert not (tmp_path / "web.qcow2").exists()
        assert not (tmp_path / "web_data.qcow2").exists()
        assert not (tmp_path / "web_snapshot_baseline.raw").exists()
        # every web2 file survives
        assert (tmp_path / "web2.qcow2").exists()
        assert (tmp_path / "web2_data.qcow2").exists()
        assert (tmp_path / "web2.1772465824").exists()
        assert (tmp_path / "web2_snapshot_baseline.raw").exists()

    def test_unrelated_named_extra_disk_survives(self, tmp_path: Path):
        """Extra disks are only removed when named in extra_disks."""
        (tmp_path / "vm01.qcow2").write_bytes(b"x")
        (tmp_path / "vm01_orphan.qcow2").write_bytes(b"x")
        remove_vm_disks(str(tmp_path), "vm01", [])
        assert not (tmp_path / "vm01.qcow2").exists()
        assert (tmp_path / "vm01_orphan.qcow2").exists()

    def test_tolerates_missing_files(self, tmp_path: Path):
        # Nothing in tmp_path — must not raise
        assert remove_vm_disks(str(tmp_path), "ghost-vm", []) is True

    def test_expands_tilde_in_workdir(self, tmp_path: Path, monkeypatch):
        # HOME → tmp_path so ~/workdir resolves under tmp_path
        monkeypatch.setenv("HOME", str(tmp_path))
        workdir = tmp_path / "wd"
        workdir.mkdir()
        (workdir / "vmtilde.qcow2").write_bytes(b"x")
        assert remove_vm_disks("~/wd", "vmtilde", []) is True
        assert not (workdir / "vmtilde.qcow2").exists()

    def test_skips_directories_with_vm_prefix(self, tmp_path: Path):
        """The sweep only removes regular files, not directories."""
        (tmp_path / "vm01.qcow2").write_bytes(b"x")
        subdir = tmp_path / "vm01.backups"
        subdir.mkdir()
        (subdir / "keep.txt").write_bytes(b"keep")
        remove_vm_disks(str(tmp_path), "vm01", [])
        assert subdir.is_dir()
        assert (subdir / "keep.txt").exists()

    def test_extra_disks_default_is_empty(self, tmp_path: Path):
        """Default empty iterable — mirrors legacy kwarg default."""
        (tmp_path / "vm01.qcow2").write_bytes(b"x")
        # Call without the third positional to confirm default works
        assert remove_vm_disks(str(tmp_path), "vm01") is True
        assert not (tmp_path / "vm01.qcow2").exists()

    def test_protected_paths_survive_every_pattern(self, tmp_path: Path):
        """An extra disk whose logical name starts with ``snapshot_``
        matches the memory-snapshot pattern; a protected path is left for
        the caller to decide (#212 review round 2, R2-1)."""
        snapshot_named = tmp_path / "vm01_snapshot_data.qcow2"
        memory = tmp_path / "vm01_snapshot_s1.raw"
        extra = tmp_path / "vm01_disk01.qcow2"
        for path in (snapshot_named, memory, extra):
            path.write_bytes(b"x")
        remove_vm_disks(str(tmp_path), "vm01", [{"name": "disk01"}],
                        protected=[str(snapshot_named), str(extra)])
        assert snapshot_named.exists()
        assert extra.exists()
        assert not memory.exists()

    def test_protection_matches_through_a_symlinked_workdir(
            self, tmp_path: Path):
        real = tmp_path / "real"
        real.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)
        kept = real / "vm01_snapshot_data.qcow2"
        kept.write_bytes(b"x")
        remove_vm_disks(str(alias), "vm01", [], protected=[str(kept)])
        assert kept.exists()


class TestRemoveIfUnchanged:
    """The claim rename can fail; its still-empty private directory must
    not be left behind in the workdir (#212 review round 3, R3-1)."""

    @pytest.mark.parametrize("err", [errno.EACCES, errno.EXDEV])
    def test_a_failed_claim_keeps_the_file_and_leaves_no_debris(
            self, tmp_path: Path, monkeypatch, err):
        disk = tmp_path / "vm01_disk01.qcow2"
        disk.write_bytes(b"data")
        identity = file_identities([str(disk)])[str(disk)]

        def refuse(src, dst, *args, **kwargs):
            raise OSError(err, os.strerror(err))

        monkeypatch.setattr(os, "rename", refuse)

        with pytest.raises(OSError) as raised:
            _remove_if_unchanged(str(disk), identity)

        # the original error, not one from the cleanup
        assert raised.value.errno == err
        assert disk.read_bytes() == b"data"
        assert sorted(p.name for p in tmp_path.iterdir()) == [disk.name]

    def test_a_file_already_gone_leaves_no_debris(
            self, tmp_path: Path, monkeypatch):
        disk = tmp_path / "vm01_disk01.qcow2"

        def gone(src, dst, *args, **kwargs):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))

        monkeypatch.setattr(os, "rename", gone)

        assert _remove_if_unchanged(str(disk), (1, 2)) == ("gone", None)
        assert list(tmp_path.iterdir()) == []
