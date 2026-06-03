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
        with patch.object(sm, "_get_snapshot_overlay_files", return_value={}), \
             patch.object(sm, "_chain_order", return_value=["s"]):
            assert sm._preserve_snapshot_overlays("vm01", "s") == []

    def test_overlays_batched_into_single_rsync_command(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        overlay1 = tmp_path / "o1.qcow2"
        overlay1.write_bytes(b"x")
        overlay2 = tmp_path / "o2.qcow2"
        overlay2.write_bytes(b"x")

        # reverting to s1 (oldest) preserves s1 and the newer s2
        with patch.object(
            sm, "_get_snapshot_overlay_files",
            return_value={"s1": [str(overlay1)], "s2": [str(overlay2)]},
        ), patch.object(sm, "_chain_order", return_value=["s1", "s2"]), \
                patch.object(sm.virsh, "execute_shell", return_value=_result()) as shell:
            preserved = sm._preserve_snapshot_overlays("vm01", "s1")

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

    def test_preserves_only_target_and_newer(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        # chain: s1 (oldest) -> s2 -> s3 (newest), one overlay each
        overlays = {}
        for name in ("s1", "s2", "s3"):
            f = tmp_path / f"{name}.qcow2"
            f.write_bytes(b"x")
            overlays[name] = [str(f)]

        # restoring the latest snapshot backs up ONLY its own overlay
        # (the regression this narrowing fixes)
        with patch.object(sm, "_get_snapshot_overlay_files", return_value=overlays), \
                patch.object(sm, "_chain_order", return_value=["s1", "s2", "s3"]), \
                patch.object(sm.virsh, "execute_shell", return_value=_result()):
            preserved = sm._preserve_snapshot_overlays("vm01", "s3")
        assert [p[0] for p in preserved] == overlays["s3"]

        # restoring a middle snapshot backs up it + the newer one, not the older
        with patch.object(sm, "_get_snapshot_overlay_files", return_value=overlays), \
                patch.object(sm, "_chain_order", return_value=["s1", "s2", "s3"]), \
                patch.object(sm.virsh, "execute_shell", return_value=_result()):
            preserved = sm._preserve_snapshot_overlays("vm01", "s2")
        backed_up = sorted(p[0] for p in preserved)
        assert backed_up == sorted(overlays["s2"] + overlays["s3"])
        assert overlays["s1"][0] not in backed_up

    def test_falls_back_to_all_when_target_not_in_chain(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        overlays = {}
        for name in ("s1", "s2"):
            f = tmp_path / f"{name}.qcow2"
            f.write_bytes(b"x")
            overlays[name] = [str(f)]

        # orphaned/missing metadata: target absent from chain order ->
        # preserve everything (safe but slow fallback)
        with patch.object(sm, "_get_snapshot_overlay_files", return_value=overlays), \
                patch.object(sm, "_chain_order", return_value=[]), \
                patch.object(sm.virsh, "execute_shell", return_value=_result()):
            preserved = sm._preserve_snapshot_overlays("vm01", "gone")
        assert sorted(p[0] for p in preserved) == sorted(
            overlays["s1"] + overlays["s2"]
        )

    def test_skips_missing_files(self, sm: SnapshotManager, tmp_path: Path):
        with patch.object(
            sm, "_get_snapshot_overlay_files",
            return_value={"s": ["/does/not/exist.qcow2"]},
        ), patch.object(sm, "_chain_order", return_value=["s"]), \
                patch.object(sm.virsh, "execute_shell") as shell:
            preserved = sm._preserve_snapshot_overlays("vm01", "s")
        assert preserved == []
        shell.assert_not_called()

    def test_uses_sudo_when_configured(self, tmp_path: Path):
        sm = SnapshotManager({"use_sudo": True})
        overlay = tmp_path / "o.qcow2"
        overlay.write_bytes(b"x")
        with patch.object(
            sm, "_get_snapshot_overlay_files", return_value={"s": [str(overlay)]}
        ), patch.object(sm, "_chain_order", return_value=["s"]), \
                patch.object(sm.virsh, "execute_shell", return_value=_result()) as shell:
            sm._preserve_snapshot_overlays("vm01", "s")
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

    def test_cleanup_rm_is_not_sudo_prefixed(self, tmp_path: Path):
        """Cleanup rm must not be sudo-prefixed even when use_sudo=True —
        unlinking needs dir-write perms, not root, and a sudo that needs a
        password would silently leak the .preserve files (see TestRmNeverSudo).
        """
        sm = SnapshotManager({"use_sudo": True})
        overlay = tmp_path / "o.qcow2"
        overlay.write_bytes(b"x")  # original still present -> backup is cleanup
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"x")
        with patch.object(sm.virsh, "execute_shell", return_value=_result()) as shell:
            sm._restore_preserved_overlays([(str(overlay), str(backup))])
        cmd = shell.call_args.args[0]
        assert cmd.startswith("rm -f")
        assert "sudo " not in cmd


class TestSnapshotRestore:
    """End-to-end wiring for snapshot_restore (regression: 057eb7d)."""

    def test_success_calls_preserve_then_revert_then_restore(
        self, sm: SnapshotManager
    ):
        preserved_pairs = [("/overlays/a", "/overlays/a.preserve")]
        with patch.object(sm, "_preserve_snapshot_overlays",
                          return_value=preserved_pairs) as preserve, \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute", return_value=_result()) as execute, \
             patch.object(sm, "_restore_preserved_overlays") as restore:
            assert sm.snapshot_restore("vm01", "snap1") is True

        preserve.assert_called_once_with("vm01", "snap1")
        execute.assert_called_once()
        assert execute.call_args.args[0] == "snapshot-revert"
        restore.assert_called_once_with(preserved_pairs)

    def test_restore_called_even_when_revert_fails(self, sm: SnapshotManager):
        with patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
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
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute", side_effect=results), \
             patch.object(sm, "_restore_preserved_overlays"), \
             patch("boxman.providers.libvirt.snapshot.time.sleep"):
            assert sm.snapshot_restore("vm01", "snap1") is True

    def test_does_not_retry_on_non_lock_error(self, sm: SnapshotManager):
        with patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="bad name")) as execute, \
             patch.object(sm, "_restore_preserved_overlays"), \
             patch("boxman.providers.libvirt.snapshot.time.sleep"):
            assert sm.snapshot_restore("vm01", "snap1") is False
        assert execute.call_count == 1  # no retries on non-lock errors

    def test_exception_still_calls_restore(self, sm: SnapshotManager):
        with patch.object(sm, "_preserve_snapshot_overlays", return_value=[("a", "b")]), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute", side_effect=RuntimeError("x")), \
             patch.object(sm, "_restore_preserved_overlays") as restore:
            assert sm.snapshot_restore("vm01", "snap1") is False
        restore.assert_called_once()


class TestSnapshotInfo:
    """Parser for `virsh snapshot-info` text output."""

    SAMPLE_OUTPUT = """\
Name:           snap1
Domain:         vm01
Current:        yes
State:          running
Location:       external
Parent:         baseline
Children:       0
Descendants:    0
Metadata:       yes
Creation Time:  2026-04-22 18:30:00 +0200
"""

    def test_returns_none_on_virsh_failure(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="not found")):
            assert sm.snapshot_info("vm01", "ghost") is None

    def test_parses_known_keys(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout=self.SAMPLE_OUTPUT)):
            info = sm.snapshot_info("vm01", "snap1")
        assert info is not None
        assert info["name"] == "snap1"
        assert info["domain"] == "vm01"
        assert info["current"] is True
        assert info["state"] == "running"
        assert info["location"] == "external"
        assert info["parent"] == "baseline"
        assert info["children"] == 0
        assert info["descendants"] == 0
        assert info["metadata"] is True
        assert info["creation_time"] == "2026-04-22 18:30:00 +0200"

    def test_parent_dash_maps_to_none(self, sm: SnapshotManager):
        out = self.SAMPLE_OUTPUT.replace("Parent:         baseline",
                                         "Parent:         -")
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout=out)):
            info = sm.snapshot_info("vm01", "snap1")
        assert info["parent"] is None

    def test_current_no_maps_to_false(self, sm: SnapshotManager):
        out = self.SAMPLE_OUTPUT.replace("Current:        yes",
                                         "Current:        no")
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout=out)):
            info = sm.snapshot_info("vm01", "snap1")
        assert info["current"] is False

    def test_creation_time_string_compares_correctly(self,
                                                     sm: SnapshotManager):
        # Same format as libvirt — lexicographic compare matches chronological.
        a = "2026-04-22 18:30:00 +0200"
        b = "2026-04-23 09:00:00 +0200"
        assert b > a  # used by manager.snapshot_log as a tiebreaker


class TestParseSnapshotXml:

    XML_FULL = """\
<domainsnapshot>
  <name>snap1</name>
  <description>before slurm</description>
  <state>running</state>
  <parent><name>baseline</name></parent>
  <creationTime>1714568400</creationTime>
  <disks/>
</domainsnapshot>
"""

    XML_NO_PARENT = """\
<domainsnapshot>
  <name>baseline</name>
  <description>initial</description>
  <creationTime>1714568000</creationTime>
</domainsnapshot>
"""

    XML_NO_CREATION_TIME = """\
<domainsnapshot>
  <name>old</name>
  <description>legacy</description>
  <parent><name>x</name></parent>
</domainsnapshot>
"""

    def test_parses_all_fields(self, sm: SnapshotManager):
        out = sm._parse_snapshot_xml(self.XML_FULL)
        assert out["description"] == "before slurm"
        assert out["parent"] == "baseline"
        # Unix-epoch parsing works regardless of libvirt version.
        assert out["creation_time"].startswith("2024-")  # 1714568400 = 2024-05-01
        assert "20" in out["creation_time"]

    def test_no_parent_returns_dict_without_parent(self, sm: SnapshotManager):
        out = sm._parse_snapshot_xml(self.XML_NO_PARENT)
        assert "parent" not in out
        assert out["description"] == "initial"

    def test_no_creation_time_omits_field(self, sm: SnapshotManager):
        out = sm._parse_snapshot_xml(self.XML_NO_CREATION_TIME)
        assert "creation_time" not in out
        assert out["parent"] == "x"

    def test_returns_empty_on_bad_xml(self, sm: SnapshotManager):
        assert sm._parse_snapshot_xml("not xml") == {}


class TestListSnapshotsDetailed:

    def _xml_for(self, snap_name: str, parent: str | None,
                 epoch: int = 1714568400) -> str:
        parent_block = (f"<parent><name>{parent}</name></parent>"
                        if parent else "")
        return (f"<domainsnapshot><name>{snap_name}</name>"
                f"<description>desc-{snap_name}</description>"
                f"{parent_block}"
                f"<creationTime>{epoch}</creationTime></domainsnapshot>")

    def test_empty_when_no_snapshots(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout="\n")):
            assert sm.list_snapshots_detailed("vm01") == []

    def test_failure_returns_empty_list(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="boom")):
            assert sm.list_snapshots_detailed("vm01") == []

    def test_orders_by_chain_oldest_first_with_depth(self, sm: SnapshotManager):
        # Listed by virsh out of order → topo sort restores chain.
        list_xml_map = {
            "snap2": self._xml_for("snap2", "snap1", 1714000200),
            "snap1": self._xml_for("snap1", None, 1714000100),
            "snap3": self._xml_for("snap3", "snap2", 1714000300),
        }

        def fake(*args, **_kwargs):
            verb = args[0]
            if verb == "snapshot-list":
                return _result(stdout="snap2\nsnap1\nsnap3\n")
            if verb == "snapshot-dumpxml":
                return _result(stdout=list_xml_map[args[2]])
            return _result()

        with patch.object(sm.virsh, "execute", side_effect=fake):
            rows = sm.list_snapshots_detailed("vm01")
        assert [r["name"] for r in rows] == ["snap1", "snap2", "snap3"]
        assert [r["depth"] for r in rows] == [0, 1, 2]
        assert rows[0]["parent"] is None
        assert rows[1]["parent"] == "snap1"
        assert rows[2]["parent"] == "snap2"
        # creation_time gets populated from <creationTime> regardless of
        # libvirt version.
        assert all(r["creation_time"] for r in rows)

    def test_handles_missing_xml(self, sm: SnapshotManager):
        # snapshot-list returns one name; snapshot-dumpxml fails for it.
        def fake(*args, **_kwargs):
            if args[0] == "snapshot-list":
                return _result(stdout="snap1\n")
            if args[0] == "snapshot-dumpxml":
                return _result(ok=False, stderr="oops")
            return _result()

        with patch.object(sm.virsh, "execute", side_effect=fake):
            rows = sm.list_snapshots_detailed("vm01")
        # Defensive fallback emits the row with empty/None fields.
        assert rows == [{
            "name": "snap1",
            "description": "",
            "creation_time": None,
            "parent": None,
            "depth": 0,
        }]


class TestMemoryCompression:
    """zstd compression of the snapshot .raw memory file."""

    def test_compress_no_op_when_already_compressed(self, sm: SnapshotManager,
                                                    tmp_path: Path):
        zst = tmp_path / "vm01_snapshot_s.raw.zst"
        zst.write_bytes(b"compressed")
        # raw doesn't exist; .zst does → idempotent True
        with patch.object(sm.virsh, "execute_shell") as shell:
            assert sm.compress_memory_file(str(tmp_path / "vm01_snapshot_s.raw")) is True
        shell.assert_not_called()

    def test_compress_runs_zstd_when_available(self, sm: SnapshotManager,
                                               tmp_path: Path):
        raw = tmp_path / "vm01_snapshot_s.raw"
        raw.write_bytes(b"x" * 1024)
        with patch.object(sm, "_is_zstd_available", return_value=True), \
             patch.object(sm.virsh, "execute_shell", return_value=_result()) as shell:
            assert sm.compress_memory_file(str(raw), level=3) is True
        cmd = shell.call_args.args[0]
        assert "zstd -3 -T0 --rm" in cmd
        assert str(raw) in cmd
        assert f"{raw}.zst" in cmd

    def test_compress_refuses_when_zstd_missing(self, sm: SnapshotManager,
                                                tmp_path: Path):
        raw = tmp_path / "vm01_snapshot_s.raw"
        raw.write_bytes(b"x")
        with patch.object(sm, "_is_zstd_available", return_value=False), \
             patch.object(sm.logger, "error") as err:
            assert sm.compress_memory_file(str(raw)) is False
        assert any("zstd" in c.args[0] for c in err.call_args_list)

    def test_decompress_no_op_when_raw_present(self, sm: SnapshotManager,
                                              tmp_path: Path):
        raw = tmp_path / "vm01_snapshot_s.raw"
        zst = tmp_path / "vm01_snapshot_s.raw.zst"
        raw.write_bytes(b"x")
        zst.write_bytes(b"y")
        with patch.object(sm.virsh, "execute_shell") as shell:
            assert sm.decompress_memory_file(str(raw)) is True
        shell.assert_not_called()

    def test_decompress_runs_zstd(self, sm: SnapshotManager, tmp_path: Path):
        raw = tmp_path / "vm01_snapshot_s.raw"
        zst = tmp_path / "vm01_snapshot_s.raw.zst"
        zst.write_bytes(b"x")
        # raw doesn't exist
        with patch.object(sm, "_is_zstd_available", return_value=True), \
             patch.object(sm.virsh, "execute_shell", return_value=_result()) as shell:
            assert sm.decompress_memory_file(str(raw), keep_zst=True) is True
        cmd = shell.call_args.args[0]
        assert "zstd -d" in cmd
        assert "-k" in cmd
        assert str(zst) in cmd

    def test_create_snapshot_compresses_when_requested(self, sm: SnapshotManager,
                                                      tmp_path: Path):
        with patch.object(sm, "_flatten_cdrom_overlays"), \
             patch.object(sm, "_cdrom_diskspec_args", return_value=[]), \
             patch.object(sm.virsh, "execute", return_value=_result()), \
             patch.object(sm, "compress_memory_file", return_value=True) as compress:
            assert sm.create_snapshot(
                "vm01", str(tmp_path), "snap1", "desc",
                compress_memory=True, compress_level=10) is True
        compress.assert_called_once()
        path_arg, kwargs = compress.call_args.args[0], compress.call_args.kwargs
        assert path_arg == str(tmp_path / "vm01_snapshot_snap1.raw")
        assert kwargs.get("level") == 10

    def test_create_snapshot_does_not_compress_by_default(self, sm: SnapshotManager,
                                                         tmp_path: Path):
        with patch.object(sm, "_flatten_cdrom_overlays"), \
             patch.object(sm, "_cdrom_diskspec_args", return_value=[]), \
             patch.object(sm.virsh, "execute", return_value=_result()), \
             patch.object(sm, "compress_memory_file") as compress:
            sm.create_snapshot("vm01", str(tmp_path), "snap1", "desc")
        compress.assert_not_called()

    def test_compress_all_memory_iterates_snapshots(self, sm: SnapshotManager,
                                                   tmp_path: Path):
        # two snapshots; one .raw on disk, one already compressed (no .raw)
        m1 = tmp_path / "vm01_snapshot_a.raw"
        m1.write_bytes(b"x")
        m2 = tmp_path / "vm01_snapshot_b.raw"  # NOT created → already compressed

        with patch.object(sm, "list_snapshots",
                          return_value=[{"name": "a", "description": ""},
                                        {"name": "b", "description": ""}]), \
             patch.object(sm, "_memory_path_from_xml",
                          side_effect=[str(m1), str(m2)]), \
             patch.object(sm, "compress_memory_file", return_value=True) as compress:
            compressed, total = sm.compress_all_memory("vm01")
        assert (compressed, total) == (1, 1)
        compress.assert_called_once()


class TestSnapshotRestoreCompressed:
    """snapshot_restore must transparently decompress .raw.zst before revert."""

    def test_decompresses_when_only_zst_on_disk(self, sm: SnapshotManager,
                                                tmp_path: Path):
        zst = tmp_path / "vm01_snapshot_s.raw.zst"
        zst.write_bytes(b"x")
        raw = str(tmp_path / "vm01_snapshot_s.raw")
        with patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm, "_restore_preserved_overlays"), \
             patch.object(sm, "_memory_path_from_xml", return_value=raw), \
             patch.object(sm, "decompress_memory_file",
                          return_value=True) as decomp, \
             patch.object(sm, "_recompress_after_revert") as recomp, \
             patch.object(sm.virsh, "execute", return_value=_result()):
            assert sm.snapshot_restore("vm01", "s") is True
        decomp.assert_called_once_with(raw, keep_zst=True)
        recomp.assert_called_once()

    def test_does_not_decompress_when_raw_present(self, sm: SnapshotManager,
                                                  tmp_path: Path):
        raw_path = tmp_path / "vm01_snapshot_s.raw"
        raw_path.write_bytes(b"x")
        with patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm, "_restore_preserved_overlays"), \
             patch.object(sm, "_memory_path_from_xml", return_value=str(raw_path)), \
             patch.object(sm, "decompress_memory_file") as decomp, \
             patch.object(sm.virsh, "execute", return_value=_result()):
            assert sm.snapshot_restore("vm01", "s") is True
        decomp.assert_not_called()

    def test_recompress_after_revert_removes_raw(self, sm: SnapshotManager,
                                                 tmp_path: Path):
        raw = tmp_path / "vm01_snapshot_s.raw"
        zst = tmp_path / "vm01_snapshot_s.raw.zst"
        raw.write_bytes(b"x")
        zst.write_bytes(b"y")
        with patch.object(sm.virsh, "execute_shell", return_value=_result()) as shell:
            sm._recompress_after_revert(str(raw), decompressed_for_revert=True)
        # When .zst exists, recompress_after_revert just rm's the .raw
        assert shell.call_count == 1
        assert "rm -f" in shell.call_args.args[0]
        assert str(raw) in shell.call_args.args[0]


class TestDeleteSnapshot:

    def test_internal_snapshot_uses_simple_path(self, sm: SnapshotManager):
        """If virsh accepts the plain delete, no external dance happens."""
        def fake(*args, **_kwargs):
            return _result()  # snapshot-info ok, snapshot-delete ok
        with patch.object(sm.virsh, "execute", side_effect=fake):
            assert sm.delete_snapshot("vm01", "snap1") is True

    def test_missing_snapshot_returns_false(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="not found")):
            assert sm.delete_snapshot("vm01", "snap1") is False

    def test_external_with_no_chain_falls_back(self, sm: SnapshotManager):
        """External-snapshot error triggers _delete_external_snapshot."""
        def fake(*args, **_kwargs):
            verb = args[0]
            if verb == "snapshot-info":
                return _result()
            if verb == "snapshot-delete":
                return _result(ok=False,
                               stderr="deletion of external snapshots is not supported")
            return _result()
        with patch.object(sm.virsh, "execute", side_effect=fake), \
             patch.object(sm, "_delete_external_snapshot",
                          return_value=True) as ext:
            assert sm.delete_snapshot("vm01", "snap1") is True
        ext.assert_called_once_with("vm01", "snap1")

    def test_external_non_current_refuses_with_collapse_hint(
            self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False,
                                               stderr="external snapshots not supported")), \
             patch.object(sm, "_chain_order",
                          return_value=["snap1", "snap2", "snap3"]), \
             patch.object(sm.logger, "error") as err:
            # Try deleting snap1 — it's not the most-recent
            # _delete_external_snapshot is invoked by delete_snapshot
            assert sm._delete_external_snapshot("vm01", "snap1") is False
        msgs = " ".join(c.args[0] for c in err.call_args_list)
        assert "collapse --to" in msgs
        assert "newer snapshots" in msgs

    def test_external_only_snapshot_uses_blockcommit(
            self, sm: SnapshotManager):
        with patch.object(sm, "_chain_order", return_value=["snap1"]), \
             patch.object(sm, "_collapse_only_external_snapshot_online",
                          return_value=True) as collapse_only:
            assert sm._delete_external_snapshot("vm01", "snap1") is True
        collapse_only.assert_called_once_with("vm01", "snap1")

    def test_external_most_recent_with_parent_calls_collapse_to(
            self, sm: SnapshotManager):
        with patch.object(sm, "_chain_order",
                          return_value=["snap1", "snap2", "snap3"]), \
             patch.object(sm, "collapse_to", return_value=True) as collapse:
            # Deleting snap3 (most-recent) collapses everything newer than
            # its parent (snap2) into the head — i.e. just drops snap3.
            assert sm._delete_external_snapshot("vm01", "snap3") is True
        collapse.assert_called_once_with("vm01", "snap2", dry_run=False)


class TestDeleteOnlyExternalSnapshot:
    """Online deletion of the only external snapshot via blockcommit."""

    def test_blockcommit_per_disk(self, sm: SnapshotManager):
        executed: list[str] = []

        def fake_execute(*args, **_kwargs):
            executed.append(args[0])
            if args[0] == "domblklist":
                return _result(stdout=(
                    " Type   Device   Target   Source\n"
                    "------------------------------------\n"
                    " file   disk     vda      /p/vm01.qcow2\n"
                    " file   disk     vdb      /p/vm01_data.qcow2\n"
                ))
            if args[0] == "snapshot-dumpxml":
                return _result(stdout=(
                    "<domainsnapshot><disks/></domainsnapshot>"
                ))
            return _result()

        with patch.object(sm.virsh, "execute", side_effect=fake_execute), \
             patch.object(sm.virsh, "execute_shell", return_value=_result()):
            assert sm._collapse_only_external_snapshot_online(
                "vm01", "snap1") is True

        # Two blockcommit calls (one per data disk)
        assert executed.count("blockcommit") == 2
        # Cleanup metadata at the end
        assert "snapshot-delete" in executed

    def test_blockcommit_failure_aborts(self, sm: SnapshotManager):
        def fake_execute(*args, **_kwargs):
            if args[0] == "domblklist":
                return _result(stdout=(
                    " Type Device Target Source\n"
                    "----\n"
                    " file disk   vda    /p/vm01.qcow2\n"
                ))
            if args[0] == "blockcommit":
                return _result(ok=False, stderr="boom")
            return _result()

        with patch.object(sm.virsh, "execute", side_effect=fake_execute):
            assert sm._collapse_only_external_snapshot_online(
                "vm01", "snap1") is False


class TestChainOrder:

    def test_topological_sort_oldest_first(self, sm: SnapshotManager):
        # snap1 → snap2 → snap3 (snap1 is base, snap3 is head)
        xml_for = {
            "snap1": "<domainsnapshot><name>snap1</name></domainsnapshot>",
            "snap2": (
                "<domainsnapshot><name>snap2</name>"
                "<parent><name>snap1</name></parent></domainsnapshot>"),
            "snap3": (
                "<domainsnapshot><name>snap3</name>"
                "<parent><name>snap2</name></parent></domainsnapshot>"),
        }

        def fake(*args, **_kwargs):
            if args[0] == "snapshot-list":
                return _result(stdout="snap1\nsnap2\nsnap3\n")
            if args[0] == "snapshot-dumpxml":
                return _result(stdout=xml_for[args[2]])
            return _result()

        with patch.object(sm.virsh, "execute", side_effect=fake):
            assert sm._chain_order("vm01") == ["snap1", "snap2", "snap3"]

    def test_returns_empty_when_no_snapshots(self, sm: SnapshotManager):
        with patch.object(sm, "list_snapshots", return_value=[]):
            assert sm._chain_order("vm01") == []


class TestStripBackingStoreCache:

    DOMAIN_XML_WITH_BACKING_STORE = """\
<domain>
  <devices>
    <disk type='file' device='disk'>
      <source file='/p/vm01.qcow2'/>
      <backingStore type='file'>
        <source file='/p/vm01.snap2.qcow2'/>
        <backingStore/>
      </backingStore>
    </disk>
    <disk type='file' device='cdrom'>
      <source file='/p/seed.iso'/>
    </disk>
  </devices>
</domain>
"""

    def test_strips_backing_store_and_redefines(
            self, sm: SnapshotManager, tmp_path: Path):
        defined_xml: list[str] = []

        def fake_execute(*args, **_kwargs):
            if args[0] == "dumpxml":
                return _result(stdout=self.DOMAIN_XML_WITH_BACKING_STORE)
            if args[0] == "define":
                # Read the temp file the manager just wrote
                with open(args[1]) as f:
                    defined_xml.append(f.read())
                return _result()
            return _result()

        with patch.object(sm.virsh, "execute", side_effect=fake_execute):
            assert sm._strip_backing_store_cache("vm01") is True

        assert defined_xml, "virsh define was never called"
        assert "<backingStore" not in defined_xml[0]

    def test_no_op_when_no_backing_store(self, sm: SnapshotManager):
        plain_xml = ("<domain><devices>"
                     "<disk type='file' device='disk'>"
                     "<source file='/p/vm01.qcow2'/>"
                     "</disk></devices></domain>")

        def fake_execute(*args, **_kwargs):
            if args[0] == "dumpxml":
                return _result(stdout=plain_xml)
            return _result()

        with patch.object(sm.virsh, "execute", side_effect=fake_execute) as exe:
            assert sm._strip_backing_store_cache("vm01") is True
        verbs = [c.args[0] for c in exe.call_args_list]
        assert "define" not in verbs


class TestQemuImgRebase:

    def test_command_format(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute_shell",
                          return_value=_result()) as shell:
            assert sm._qemu_img_rebase("/p/head.qcow2", "/p/base.qcow2") is True
        cmd = shell.call_args.args[0]
        assert "qemu-img rebase" in cmd
        assert "-p" in cmd
        assert "-F qcow2" in cmd
        assert "/p/head.qcow2" in cmd
        assert "/p/base.qcow2" in cmd

    def test_failure_returns_false(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute_shell",
                          return_value=_result(ok=False, stderr="boom")):
            assert sm._qemu_img_rebase("/p/h", "/p/b") is False


class TestCollapseTo:

    def test_no_op_when_target_is_head(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute", return_value=_result()), \
             patch.object(sm, "_chain_order",
                          return_value=["snap1", "snap2"]), \
             patch.object(sm, "_qemu_img_rebase") as rebase:
            # snap2 is already the head — nothing to drop
            assert sm.collapse_to("vm01", "snap2") is True
        rebase.assert_not_called()

    def test_dry_run_does_not_rebase(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute", return_value=_result()), \
             patch.object(sm, "_chain_order",
                          return_value=["snap1", "snap2", "snap3"]), \
             patch.object(sm, "_data_disk_targets",
                          return_value=[("vda", "/p/vm01.qcow2")]), \
             patch.object(sm, "_overlay_path_for_snapshot",
                          return_value="/p/vm01.snap1.qcow2"), \
             patch.object(sm, "_qemu_img_rebase") as rebase, \
             patch.object(sm, "_strip_backing_store_cache") as strip:
            assert sm.collapse_to("vm01", "snap1", dry_run=True) is True
        rebase.assert_not_called()
        strip.assert_not_called()

    def test_target_not_in_chain_fails(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute", return_value=_result()), \
             patch.object(sm, "_chain_order",
                          return_value=["snap1", "snap2"]):
            assert sm.collapse_to("vm01", "ghost") is False

    def test_missing_target_snapshot_fails(self, sm: SnapshotManager):
        # snapshot-info fails outright
        with patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="no such")):
            assert sm.collapse_to("vm01", "snap1") is False

    def test_happy_path_rebases_and_cleans(self, sm: SnapshotManager,
                                           tmp_path: Path):
        # Fake disk + overlay layout
        head = tmp_path / "vm01.qcow2"
        head.write_bytes(b"head")
        snap1_overlay = tmp_path / "vm01.snap1.qcow2"
        snap1_overlay.write_bytes(b"o1")
        snap2_overlay = tmp_path / "vm01.snap2.qcow2"
        snap2_overlay.write_bytes(b"o2")
        snap3_overlay = tmp_path / "vm01.snap3.qcow2"
        snap3_overlay.write_bytes(b"o3")
        # memory file for snap3
        mem3 = tmp_path / "vm01_snapshot_snap3.raw"
        mem3.write_bytes(b"m3")

        executed: list[tuple] = []
        shell_executed: list[str] = []

        def fake_execute(*args, **_kwargs):
            executed.append(args)
            if args[0] == "snapshot-info":
                return _result()
            return _result()

        def fake_shell(cmd, *_a, **_kw):
            shell_executed.append(cmd)
            return _result()

        # Returns the right overlay path for (target, disk)
        overlay_map = {
            ("snap1", "vda"): str(snap1_overlay),
            ("snap2", "vda"): str(snap2_overlay),
            ("snap3", "vda"): str(snap3_overlay),
        }

        with patch.object(sm.virsh, "execute", side_effect=fake_execute), \
             patch.object(sm.virsh, "execute_shell", side_effect=fake_shell), \
             patch.object(sm, "_chain_order",
                          return_value=["snap1", "snap2", "snap3"]), \
             patch.object(sm, "_data_disk_targets",
                          return_value=[("vda", str(head))]), \
             patch.object(sm, "_overlay_path_for_snapshot",
                          side_effect=lambda _vm, snap, disk: overlay_map[(snap, disk)]), \
             patch.object(sm, "_qemu_img_rebase",
                          return_value=True) as rebase, \
             patch.object(sm, "_strip_backing_store_cache",
                          return_value=True) as strip, \
             patch.object(sm, "_memory_path_from_xml",
                          side_effect=lambda _vm, snap: str(tmp_path / f"vm01_snapshot_{snap}.raw")):
            assert sm.collapse_to("vm01", "snap1") is True

        # Rebased the head onto snap1's overlay
        rebase.assert_called_once_with(str(head), str(snap1_overlay))
        # Stripped backingStore cache
        strip.assert_called_once()
        # Cleaned up snap2 + snap3's overlay files via shell rm
        rms = [c for c in shell_executed if "rm -f" in c]
        assert any(str(snap2_overlay) in c for c in rms)
        assert any(str(snap3_overlay) in c for c in rms)
        # And snap3's memory file
        assert any(str(mem3) in c for c in rms)
        # snapshot-delete --metadata called for snap2 and snap3
        meta_calls = [c for c in executed
                      if c[0] == "snapshot-delete" and "--metadata" in c]
        assert len(meta_calls) == 2


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
