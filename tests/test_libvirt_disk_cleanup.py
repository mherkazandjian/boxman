"""
Unit tests for boxman.providers.libvirt.disk_cleanup.remove_vm_disks.

Part of Phase 2.6 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).

Pins the contract that was previously inline in
:meth:`LibVirtSession.destroy_disks` — guards against a future session
refactor accidentally changing which files are swept.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boxman.providers.libvirt.disk_cleanup import remove_vm_disks


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
