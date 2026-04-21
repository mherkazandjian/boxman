"""
Unit tests for boxman.providers.libvirt.snapshot.SnapshotManager.

Covers the overlay-preservation regression fix landed in commit 057eb7d —
``snapshot_restore`` must copy overlay files aside before reverting and put
them back afterwards so every snapshot remains reachable.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.snapshot import SnapshotManager


pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True, stderr: str = "", return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


SNAP_XML_WITH_OVERLAY = """\
<domainsnapshot>
  <name>snap1</name>
  <memory file='/var/lib/libvirt/vm01_snapshot_snap1.raw'/>
  <disks>
    <disk name='vda' snapshot='external'>
      <source file='/var/lib/libvirt/vm01.snap1.qcow2'/>
    </disk>
  </disks>
</domainsnapshot>
"""

SNAP_XML_NO_OVERLAY = """\
<domainsnapshot>
  <name>snap1</name>
  <disks>
    <disk name='vda' snapshot='internal'/>
  </disks>
</domainsnapshot>
"""


@pytest.fixture
def sm() -> SnapshotManager:
    return SnapshotManager(provider_config={"use_sudo": False, "uri": "qemu:///system"})


class TestInit:

    def test_defaults_when_no_config(self):
        sm = SnapshotManager()
        assert sm.uri == "qemu:///system"
        assert sm.use_sudo is False

    def test_reads_from_config(self):
        sm = SnapshotManager({"uri": "qemu+ssh://x", "use_sudo": True})
        assert sm.uri == "qemu+ssh://x"
        assert sm.use_sudo is True


class TestCreateSnapshot:

    def test_success_passes_memspec_and_atomic(self, sm: SnapshotManager, tmp_path: Path):
        with patch.object(sm, "_flatten_cdrom_overlays"), \
             patch.object(sm, "_cdrom_diskspec_args", return_value=[]), \
             patch.object(sm.virsh, "execute", return_value=_result()) as execute:
            assert sm.create_snapshot("vm01", str(tmp_path), "snap1", "desc") is True
        args = execute.call_args.args
        assert args[0] == "snapshot-create-as"
        assert any("--domain vm01" in a for a in args)
        assert any("--name snap1" in a for a in args)
        assert any("--atomic" in a for a in args)
        assert any("--memspec=" in a and "snap1.raw" in a for a in args)

    def test_includes_cdrom_diskspec_args(self, sm: SnapshotManager, tmp_path: Path):
        with patch.object(sm, "_flatten_cdrom_overlays"), \
             patch.object(sm, "_cdrom_diskspec_args",
                          return_value=["--diskspec hdc,snapshot=no"]), \
             patch.object(sm.virsh, "execute", return_value=_result()) as execute:
            sm.create_snapshot("vm01", str(tmp_path), "s", "d")
        args = execute.call_args.args
        assert "--diskspec hdc,snapshot=no" in args

    def test_command_failure_returns_false(self, sm: SnapshotManager, tmp_path: Path):
        with patch.object(sm, "_flatten_cdrom_overlays"), \
             patch.object(sm, "_cdrom_diskspec_args", return_value=[]), \
             patch.object(sm.virsh, "execute", return_value=_result(ok=False, stderr="x")):
            assert sm.create_snapshot("vm01", str(tmp_path), "s", "d") is False


class TestGetLatestSnapshot:

    def test_returns_name(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute", return_value=_result(stdout="snap1\n")):
            assert sm.get_latest_snapshot("vm01") == "snap1"

    def test_none_when_empty(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute", return_value=_result(stdout="\n")):
            assert sm.get_latest_snapshot("vm01") is None

    def test_none_when_failed(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute", return_value=_result(ok=False)):
            assert sm.get_latest_snapshot("vm01") is None


class TestValidateSnapshot:

    def test_missing_snapshot_info_fails(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="no such")):
            ok, errors = sm.validate_snapshot("vm01", "snap1")
        assert ok is False
        assert any("snapshot-info failed" in e for e in errors)

    def test_reports_missing_memory_file(self, sm: SnapshotManager, tmp_path: Path):
        missing_mem = str(tmp_path / "missing.raw")
        xml = (
            "<domainsnapshot>"
            f"<memory file='{missing_mem}'/>"
            "<disks/>"
            "</domainsnapshot>"
        )

        def fake(*args, **_kwargs):
            if args[0] == "snapshot-info":
                return _result(ok=True)
            if args[0] == "snapshot-dumpxml":
                return _result(stdout=xml)
            return _result()

        with patch.object(sm.virsh, "execute", side_effect=fake):
            ok, errors = sm.validate_snapshot("vm01", "snap1")
        assert ok is False
        assert any("memory file missing" in e for e in errors)

    def test_reports_missing_overlay(self, sm: SnapshotManager, tmp_path: Path):
        missing_overlay = str(tmp_path / "missing.qcow2")
        xml = (
            "<domainsnapshot>"
            f"<disks><disk name='vda' snapshot='external'>"
            f"<source file='{missing_overlay}'/></disk></disks>"
            "</domainsnapshot>"
        )

        def fake(*args, **_kwargs):
            if args[0] == "snapshot-info":
                return _result(ok=True)
            return _result(stdout=xml)

        with patch.object(sm.virsh, "execute", side_effect=fake):
            ok, errors = sm.validate_snapshot("vm01", "snap1")
        assert ok is False
        assert any("disk overlay missing" in e for e in errors)

    def test_all_present_is_valid(self, sm: SnapshotManager, tmp_path: Path):
        mem = tmp_path / "m.raw"
        mem.write_bytes(b"x")
        overlay = tmp_path / "o.qcow2"
        overlay.write_bytes(b"x")
        xml = (
            "<domainsnapshot>"
            f"<memory file='{mem}'/>"
            f"<disks><disk name='vda' snapshot='external'>"
            f"<source file='{overlay}'/></disk></disks>"
            "</domainsnapshot>"
        )

        def fake(*args, **_kwargs):
            if args[0] == "snapshot-info":
                return _result(ok=True)
            return _result(stdout=xml)

        with patch.object(sm.virsh, "execute", side_effect=fake):
            ok, errors = sm.validate_snapshot("vm01", "snap1")
        assert ok is True
        assert errors == []


class TestListSnapshots:

    def test_returns_name_and_description(self, sm: SnapshotManager):
        def fake(*args, **_kwargs):
            if args[0] == "snapshot-list":
                return _result(stdout="snap1\nsnap2\n")
            return _result(
                stdout=f"<domainsnapshot><description>description of {args[2]}</description></domainsnapshot>"
            )

        with patch.object(sm.virsh, "execute", side_effect=fake):
            out = sm.list_snapshots("vm01")
        assert out == [
            {"name": "snap1", "description": "description of snap1"},
            {"name": "snap2", "description": "description of snap2"},
        ]

    def test_empty_list_on_failure(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute", return_value=_result(ok=False)):
            assert sm.list_snapshots("vm01") == []


class TestGetSnapshotOverlayFiles:

    def test_collects_external_overlays_per_snapshot(self, sm: SnapshotManager):
        xml_for = {
            "snap1": (
                "<domainsnapshot><disks>"
                "<disk name='vda' snapshot='external'><source file='/p/vm.snap1.qcow2'/></disk>"
                "</disks></domainsnapshot>"
            ),
            "snap2": (
                "<domainsnapshot><disks>"
                "<disk name='vda' snapshot='internal'/>"
                "</disks></domainsnapshot>"
            ),
        }

        def fake(*args, **_kwargs):
            if args[0] == "snapshot-list":
                return _result(stdout="snap1\nsnap2\n")
            if args[0] == "snapshot-dumpxml":
                return _result(stdout=xml_for[args[2]])
            return _result()

        with patch.object(sm.virsh, "execute", side_effect=fake):
            overlays = sm._get_snapshot_overlay_files("vm01")
        assert overlays == {"snap1": ["/p/vm.snap1.qcow2"]}


class TestPreserveSnapshotOverlays:

    def test_no_overlays_returns_empty_list(self, sm: SnapshotManager):
        with patch.object(sm, "_get_snapshot_overlay_files", return_value={}):
            assert sm._preserve_snapshot_overlays("vm01") == []

    def test_overlays_batched_into_single_rsync_command(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        overlay1 = tmp_path / "o1.qcow2"
        overlay1.write_bytes(b"x")
        overlay2 = tmp_path / "o2.qcow2"
        overlay2.write_bytes(b"x")

        with patch.object(
            sm, "_get_snapshot_overlay_files",
            return_value={"s1": [str(overlay1)], "s2": [str(overlay2)]},
        ), patch.object(sm.virsh, "execute_shell", return_value=_result()) as shell:
            preserved = sm._preserve_snapshot_overlays("vm01")

        # one single command for BOTH overlays (regression: 057eb7d)
        assert shell.call_count == 1
        cmd = shell.call_args.args[0]
        assert " && " in cmd
        assert "rsync" in cmd
        assert str(overlay1) in cmd
        assert str(overlay2) in cmd
        assert sorted(pair[0] for pair in preserved) == sorted(
            [str(overlay1), str(overlay2)]
        )

    def test_skips_missing_files(self, sm: SnapshotManager, tmp_path: Path):
        with patch.object(
            sm, "_get_snapshot_overlay_files",
            return_value={"s": ["/does/not/exist.qcow2"]},
        ), patch.object(sm.virsh, "execute_shell") as shell:
            preserved = sm._preserve_snapshot_overlays("vm01")
        assert preserved == []
        shell.assert_not_called()

    def test_uses_sudo_when_configured(self, tmp_path: Path):
        sm = SnapshotManager({"use_sudo": True})
        overlay = tmp_path / "o.qcow2"
        overlay.write_bytes(b"x")
        with patch.object(
            sm, "_get_snapshot_overlay_files", return_value={"s": [str(overlay)]}
        ), patch.object(sm.virsh, "execute_shell", return_value=_result()) as shell:
            sm._preserve_snapshot_overlays("vm01")
        assert shell.call_args.args[0].startswith("sudo rsync")


class TestRestorePreservedOverlays:

    def test_noop_when_nothing_to_restore(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute_shell") as shell:
            sm._restore_preserved_overlays([])
        shell.assert_not_called()

    def test_restores_deleted_originals(self, sm: SnapshotManager, tmp_path: Path):
        overlay = tmp_path / "o.qcow2"  # does NOT exist (deleted by revert)
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"x")
        with patch.object(sm.virsh, "execute_shell", return_value=_result()) as shell:
            sm._restore_preserved_overlays([(str(overlay), str(backup))])
        cmd = shell.call_args.args[0]
        assert "rsync" in cmd
        assert "--remove-source-files" in cmd
        assert str(backup) in cmd

    def test_cleans_up_backup_when_original_still_present(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        overlay = tmp_path / "o.qcow2"
        overlay.write_bytes(b"x")  # original still exists
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"x")

        calls: list[str] = []

        def capture(cmd, *_a, **_kw):
            calls.append(cmd)
            return _result()

        with patch.object(sm.virsh, "execute_shell", side_effect=capture):
            sm._restore_preserved_overlays([(str(overlay), str(backup))])
        # one rm cleanup call
        assert any("rm -f" in c and str(backup) in c for c in calls)


class TestSnapshotRestore:
    """End-to-end wiring for snapshot_restore (regression: 057eb7d)."""

    def test_success_calls_preserve_then_revert_then_restore(
        self, sm: SnapshotManager
    ):
        preserved_pairs = [("/overlays/a", "/overlays/a.preserve")]
        with patch.object(sm, "_preserve_snapshot_overlays",
                          return_value=preserved_pairs) as preserve, \
             patch.object(sm.virsh, "execute", return_value=_result()) as execute, \
             patch.object(sm, "_restore_preserved_overlays") as restore:
            assert sm.snapshot_restore("vm01", "snap1") is True

        preserve.assert_called_once_with("vm01")
        execute.assert_called_once()
        assert execute.call_args.args[0] == "snapshot-revert"
        restore.assert_called_once_with(preserved_pairs)

    def test_restore_called_even_when_revert_fails(self, sm: SnapshotManager):
        with patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="boom")), \
             patch.object(sm, "_restore_preserved_overlays") as restore:
            assert sm.snapshot_restore("vm01", "snap1") is False
        restore.assert_called_once()

    def test_retries_on_write_lock_contention(self, sm: SnapshotManager):
        results = [
            _result(ok=False, stderr="unable to acquire write lock"),
            _result(ok=False, stderr="unable to acquire write lock"),
            _result(ok=True),
        ]
        with patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm.virsh, "execute", side_effect=results), \
             patch.object(sm, "_restore_preserved_overlays"), \
             patch("boxman.providers.libvirt.snapshot.time.sleep"):
            assert sm.snapshot_restore("vm01", "snap1") is True

    def test_does_not_retry_on_non_lock_error(self, sm: SnapshotManager):
        with patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="bad name")) as execute, \
             patch.object(sm, "_restore_preserved_overlays"), \
             patch("boxman.providers.libvirt.snapshot.time.sleep"):
            assert sm.snapshot_restore("vm01", "snap1") is False
        assert execute.call_count == 1  # no retries on non-lock errors

    def test_exception_still_calls_restore(self, sm: SnapshotManager):
        with patch.object(sm, "_preserve_snapshot_overlays", return_value=[("a", "b")]), \
             patch.object(sm.virsh, "execute", side_effect=RuntimeError("x")), \
             patch.object(sm, "_restore_preserved_overlays") as restore:
            assert sm.snapshot_restore("vm01", "snap1") is False
        restore.assert_called_once()


class TestDeleteSnapshot:

    def test_success(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute", return_value=_result()):
            assert sm.delete_snapshot("vm01", "snap1") is True

    def test_failure(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="x")):
            assert sm.delete_snapshot("vm01", "snap1") is False

    def test_exception(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute", side_effect=RuntimeError("x")):
            assert sm.delete_snapshot("vm01", "snap1") is False


class TestFlattenCdromOverlays:
    """Exercise _flatten_cdrom_overlays which guards against qcow2-over-raw ISO bug."""

    DOMAIN_XML_WITH_QCOW_OVERLAY = """\
<domain>
  <devices>
    <disk type='file' device='cdrom'>
      <source file='/var/lib/libvirt/seed.1772.qcow2'/>
      <target dev='hdc' bus='ide'/>
      <backingStore>
        <format type='raw'/>
        <source file='/var/lib/libvirt/seed.iso'/>
      </backingStore>
    </disk>
  </devices>
</domain>
"""

    DOMAIN_XML_PLAIN_RAW_CDROM = """\
<domain>
  <devices>
    <disk type='file' device='cdrom'>
      <source file='/var/lib/libvirt/seed.iso'/>
      <target dev='hdc' bus='ide'/>
    </disk>
  </devices>
</domain>
"""

    def test_switches_qcow_overlay_back_to_raw_iso(self, sm: SnapshotManager):
        def fake(*args, **_kwargs):
            if args[0] == "dumpxml":
                return _result(stdout=self.DOMAIN_XML_WITH_QCOW_OVERLAY)
            return _result()

        with patch.object(sm.virsh, "execute", side_effect=fake) as execute:
            sm._flatten_cdrom_overlays("vm01")

        change_media_calls = [
            c for c in execute.call_args_list if c.args and c.args[0] == "change-media"
        ]
        assert len(change_media_calls) == 1
        args = change_media_calls[0].args
        assert args[1] == "vm01"
        assert args[2] == "hdc"
        assert args[3] == "/var/lib/libvirt/seed.iso"
        assert "--live" in args
        assert "--config" in args
        assert "--force" in args

    def test_skips_cdroms_already_pointing_at_raw_iso(self, sm: SnapshotManager):
        def fake(*args, **_kwargs):
            if args[0] == "dumpxml":
                return _result(stdout=self.DOMAIN_XML_PLAIN_RAW_CDROM)
            return _result()

        with patch.object(sm.virsh, "execute", side_effect=fake) as execute:
            sm._flatten_cdrom_overlays("vm01")

        assert not any(
            c.args and c.args[0] == "change-media" for c in execute.call_args_list
        )
