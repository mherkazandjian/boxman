"""
Unit tests for boxman.providers.libvirt.snapshot.SnapshotManager.

Covers the overlay-preservation regression fix landed in commit 057eb7d —
``snapshot_restore`` must copy overlay files aside before reverting and put
them back afterwards so every snapshot remains reachable.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import SnapshotError, SnapshotRecoveryError
from boxman.providers.libvirt.disk_cleanup import FilesInUse
from boxman.providers.libvirt.session import LibVirtSession
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


def _not_running(sm: SnapshotManager):
    """Answer the pre-backup pause probe with "it is not running".

    For tests that are not about pausing: the guest state is a `domstate`
    call on the same mock these tests use for `snapshot-revert`, and it
    would otherwise show up in their call counts. See
    TestTheGuestHoldsStillWhileCopying for the pause itself.
    """
    return patch.object(sm, "_pause_for_backup", return_value=False)


def _inventory(sm: SnapshotManager, overlays: dict, order: list):
    """Patch the strict inventory with a complete, readable answer.

    Tests about the strictness itself must NOT use this — they drive
    ``virsh`` (see ``_virsh_snapshots``) so the enumeration really runs.
    """
    return patch.object(sm, "_strict_overlay_inventory",
                        return_value=(overlays, order))


def _shell_that_copies(calls: list | None = None,
                       ok: bool = True,
                       stderr: str = "",
                       strip_sudo: bool = False):
    """An ``execute_shell`` double that really runs the command it is given.

    The production code decides by looking at the filesystem -- inode,
    size and mtime against the reservation it made -- rather than at exit
    codes, so a double that only records command strings can neither
    confirm nor refute what it concludes. Every path involved is under
    tmp_path and shlex-quoted by the code under test.

    With ``ok=False`` the command is not run and a failure is reported,
    for the tests about what happens when the copy does not work.
    ``strip_sudo`` records the command as built but runs it without the
    prefixes, for the tests that are about which elements got one.
    """
    def run(cmd, *_a, **_kw):
        if calls is not None:
            calls.append(cmd)
        if not ok:
            return _result(ok=False, stderr=stderr)
        runnable = cmd.replace("sudo ", "") if strip_sudo else cmd
        proc = subprocess.run(runnable, shell=True,
                              capture_output=True, text=True)
        return _result(ok=proc.returncode == 0, stderr=proc.stderr,
                       return_code=proc.returncode)
    return run


def _shell_that_runs(calls: list | None = None):
    """An ``execute_shell`` double that actually runs the command.

    Real cp/mv/rm against tmp_path files. The production code decides by
    looking at the filesystem rather than at exit codes, so a double that
    only records command strings can neither confirm nor refute what it
    concludes. Paths are shlex-quoted by the code under test and every one
    of them is under tmp_path.
    """
    def run(cmd, *_a, **_kw):
        if calls is not None:
            calls.append(cmd)
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return _result(ok=proc.returncode == 0, stdout=proc.stdout,
                       stderr=proc.stderr, return_code=proc.returncode)
    return run


def _snap_xml(name: str, overlay: str, parent: str | None = None) -> str:
    parent_xml = f"<parent><name>{parent}</name></parent>" if parent else ""
    return (f"<domainsnapshot><name>{name}</name>{parent_xml}"
            f"<disks><disk name='vda' snapshot='external'>"
            f"<source file='{overlay}'/></disk></disks></domainsnapshot>")


def _virsh_snapshots(names, xml_for, list_ok: bool = True, seen=None,
                     disk=None):
    """A ``virsh.execute`` double answering snapshot-list / snapshot-dumpxml.

    ``xml_for(name)`` returns the XML text, or None to fail that dumpxml.
    ``disk`` is accepted and ignored — kept so callers read uniformly.
    """
    def execute(*args, **_kw):
        if seen is not None:
            seen.append(args[0])
        if args[0] == "snapshot-list":
            if not list_ok:
                return _result(ok=False, stderr="error: no such domain")
            return _result(stdout="".join(f"{n}\n" for n in names))
        if args[0] == "snapshot-dumpxml":
            xml = xml_for(args[2])
            if xml is None:
                return _result(ok=False, stderr="error: cannot read metadata")
            return _result(stdout=xml)
        return _result()
    return execute


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
        # flags and values are passed as separate tokens — build_command
        # quotes each token centrally, so "--domain vm01" as a single token
        # would reach virsh as one literal argument
        assert "--domain" in args and "vm01" in args
        assert "--name" in args and "snap1" in args
        assert "--atomic" in args
        assert any(a.startswith("--memspec=") and "snap1.raw" in a for a in args)

    def test_includes_cdrom_diskspec_args(self, sm: SnapshotManager, tmp_path: Path):
        with patch.object(sm, "_flatten_cdrom_overlays"), \
             patch.object(sm, "_cdrom_diskspec_args",
                          return_value=["--diskspec", "hdc,snapshot=no"]), \
             patch.object(sm.virsh, "execute", return_value=_result()) as execute:
            sm.create_snapshot("vm01", str(tmp_path), "s", "d")
        args = execute.call_args.args
        assert "--diskspec" in args and "hdc,snapshot=no" in args

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

    def test_passes_warn_true(self, sm: SnapshotManager):
        """Regression for issue #85 item 12: without warn=True a VM with
        no current snapshot makes virsh exit non-zero and execute raise
        RuntimeError instead of returning None."""
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout="snap1\n")) as execute:
            sm.get_latest_snapshot("vm01")
        assert execute.call_args.kwargs.get("warn") is True


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

    @staticmethod
    def _memory_only(sm: SnapshotManager, mem: Path):
        xml = f"<domainsnapshot><memory file='{mem}'/><disks/></domainsnapshot>"

        def fake(*args, **_kwargs):
            if args[0] == "snapshot-info":
                return _result(ok=True)
            return _result(stdout=xml)

        return patch.object(sm.virsh, "execute", side_effect=fake)

    def test_compressed_memory_is_valid(self, sm: SnapshotManager, tmp_path: Path):
        """``--compress-memory`` leaves only ``<raw>.zst``, while libvirt
        still records the ``.raw`` path. The take's own verification and
        the restore's pre-validation both reported such a snapshot as
        "memory file missing", though snapshot_restore decompresses it."""
        mem = tmp_path / "vm01_snapshot_snap1.raw"
        (tmp_path / "vm01_snapshot_snap1.raw.zst").write_bytes(b"x")
        with self._memory_only(sm, mem):
            ok, errors = sm.validate_snapshot("vm01", "snap1")
        assert ok is True
        assert errors == []

    def test_neither_raw_nor_zst_names_both(self, sm: SnapshotManager, tmp_path: Path):
        mem = tmp_path / "vm01_snapshot_snap1.raw"
        with self._memory_only(sm, mem):
            ok, errors = sm.validate_snapshot("vm01", "snap1")
        assert ok is False
        assert errors == [f"memory file missing: {mem} (nor {mem}.zst)"]

    def test_a_zst_of_another_name_does_not_count(self, sm: SnapshotManager,
                                                  tmp_path: Path):
        mem = tmp_path / "vm01_snapshot_snap1.raw"
        (tmp_path / "vm01_snapshot_snap2.raw.zst").write_bytes(b"x")
        (tmp_path / "vm01_snapshot_snap1.zst").write_bytes(b"x")
        with self._memory_only(sm, mem):
            ok, _errors = sm.validate_snapshot("vm01", "snap1")
        assert ok is False


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


class TestMemoryImages:
    """The memory file libvirt recorded for each snapshot that saved the
    guest's memory — or ``None`` when they cannot all be read, which is
    not "none recorded"."""

    @staticmethod
    def _xml(name: str, memory: str) -> str:
        return (f"<domainsnapshot><name>{name}</name>{memory}"
                f"<disks><disk name='vda' snapshot='external'>"
                f"<source file='/w/vm01.{name}'/></disk></disks>"
                f"</domainsnapshot>")

    def test_each_snapshot_that_saved_memory_names_its_file(
            self, sm: SnapshotManager):
        xml = {
            "s1": self._xml("s1", "<memory snapshot='external' "
                                  "file='/snaps/vm01_snapshot_s1.raw'/>"),
            "disk-only": self._xml("disk-only", "<memory snapshot='no'/>"),
            "s2": self._xml("s2", "<memory snapshot='external' "
                                  "file='/old/vm01_snapshot_s2.raw'/>"),
        }
        with patch.object(sm.virsh, "execute",
                          side_effect=_virsh_snapshots(list(xml), xml.get)):
            assert sm.memory_images("vm01") == {
                "s1": "/snaps/vm01_snapshot_s1.raw",
                "s2": "/old/vm01_snapshot_s2.raw",
            }

    def test_no_snapshots_record_none(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute",
                          side_effect=_virsh_snapshots([], lambda _n: None)):
            assert sm.memory_images("vm01") == {}

    def test_a_list_that_fails_is_unknown(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute", side_effect=_virsh_snapshots(
                ["s1"], lambda _n: None, list_ok=False)):
            assert sm.memory_images("vm01") is None

    @staticmethod
    def _listing(stdout: str):
        """snapshot-list printing exactly *stdout*; every snapshot saved
        its memory to ``/s/<name>.raw``."""
        def execute(*args, **_kw):
            if args[0] == "snapshot-list":
                return _result(stdout=stdout)
            return _result(stdout=TestMemoryImages._xml(
                args[2], f"<memory snapshot='external' "
                         f"file='/s/{args[2]}.raw'/>"))
        return execute

    def test_the_lines_virsh_ends_its_list_with_are_no_entry(
            self, sm: SnapshotManager):
        """Each name, a newline, and the blank line virsh prints after a
        command."""
        with patch.object(sm.virsh, "execute",
                          side_effect=self._listing("s1\n\n")):
            assert sm.memory_images("vm01") == {"s1": "/s/s1.raw"}

    @pytest.mark.parametrize("stdout", ["\n\n\n", "s1\n\ns2\n\n",
                                        "s1\ns1\n\n"],
                             ids=["a name of newlines", "a blank entry",
                                  "a name twice"])
    def test_a_list_that_cannot_be_trusted_is_unknown(
            self, sm: SnapshotManager, stdout):
        """A blank entry cannot be told apart from a name made only of
        newlines, whose snapshot would drop out unread — as
        _strict_overlay_inventory refuses it."""
        with patch.object(sm.virsh, "execute",
                          side_effect=self._listing(stdout)):
            assert sm.memory_images("vm01") is None

    @pytest.mark.parametrize("bad", [None, "<domainsnapshot><name>s2"],
                             ids=["unreadable", "not xml"])
    def test_one_snapshot_that_cannot_be_read_makes_it_unknown(
            self, sm: SnapshotManager, bad):
        xml = {"s1": self._xml("s1", "<memory snapshot='external' "
                                     "file='/s/vm01_snapshot_s1.raw'/>"),
               "s2": bad}
        with patch.object(sm.virsh, "execute",
                          side_effect=_virsh_snapshots(list(xml), xml.get)):
            assert sm.memory_images("vm01") is None


class TestOverlayFiles:
    """The external overlays libvirt recorded for each snapshot, read as
    the memory images are: a disk that names no file, whatever its mode,
    records none — only a list or a description that cannot be read, or
    trusted, makes them unknown."""

    @staticmethod
    def _xml(name: str, disks: str) -> str:
        return (f"<domainsnapshot><name>{name}</name>"
                f"<memory snapshot='no'/><disks>{disks}</disks>"
                f"</domainsnapshot>")

    def test_each_external_disk_names_its_overlay(self, sm: SnapshotManager):
        xml = {
            "s1": self._xml("s1", "<disk name='vda' snapshot='external'>"
                                  "<source file='/ov/vm01.s1'/></disk>"
                                  "<disk name='vdb' snapshot='no'/>"),
            "manual": self._xml("manual",
                                "<disk name='vda' snapshot='manual'/>"),
            "internal": self._xml("internal",
                                  "<disk name='vda' snapshot='internal'/>"),
            "block": self._xml("block",
                               "<disk name='vda' snapshot='external' "
                               "type='block'><source dev='/dev/vg/vm01'/>"
                               "</disk>"),
        }
        with patch.object(sm.virsh, "execute",
                          side_effect=_virsh_snapshots(list(xml), xml.get)):
            assert sm.overlay_files("vm01") == {
                "s1": ["/ov/vm01.s1"], "block": ["/dev/vg/vm01"]}

    @pytest.mark.parametrize("bad", [None, "<domainsnapshot><name>s2"],
                             ids=["unreadable", "not xml"])
    def test_one_snapshot_that_cannot_be_read_makes_them_unknown(
            self, sm: SnapshotManager, bad):
        xml = {"s1": self._xml("s1", "<disk name='vda' snapshot='no'/>"),
               "s2": bad}
        with patch.object(sm.virsh, "execute",
                          side_effect=_virsh_snapshots(list(xml), xml.get)):
            assert sm.overlay_files("vm01") is None


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
        with _inventory(sm, {}, ["s"]):
            assert sm._preserve_snapshot_overlays("vm01", "s") == []

    def test_overlays_batched_into_single_copy_command(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        overlay1 = tmp_path / "o1.qcow2"
        overlay1.write_bytes(b"one")
        overlay2 = tmp_path / "o2.qcow2"
        overlay2.write_bytes(b"two")

        calls: list[str] = []
        # reverting to s1 (oldest) preserves s1 and the newer s2
        with _inventory(sm, {"s1": [str(overlay1)], "s2": [str(overlay2)]},
                        ["s1", "s2"]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies(calls)):
            preserved = sm._preserve_snapshot_overlays("vm01", "s1")

        # one single command for BOTH overlays (regression: 057eb7d)
        assert len(calls) == 1
        cmd = calls[0]
        assert " && " in cmd
        # a reflink where the filesystem has them, a sparse copy where it
        # does not -- never a second full copy beside the original (CL-R2)
        assert "cp --reflink=auto --sparse=always" in cmd
        assert "rsync" not in cmd
        assert str(overlay1) in cmd
        assert str(overlay2) in cmd
        # each copy lands on a scratch name and is renamed over the
        # reservation, so the reserved path is never a zero-length file
        assert cmd.count("mv -fT ") == 2
        assert sorted(pair[0] for pair in preserved) == sorted(
            [str(overlay1), str(overlay2)]
        )
        # and the backups are really there, with the right bytes in them
        assert (tmp_path / "o1.qcow2.preserve").read_bytes() == b"one"
        assert (tmp_path / "o2.qcow2.preserve").read_bytes() == b"two"

    def test_preserves_only_target_and_newer(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        # chain: s1 (oldest) -> s2 -> s3 (newest), one overlay each
        overlays = {}
        for name in ("s1", "s2", "s3"):
            f = tmp_path / f"{name}.qcow2"
            f.write_bytes(b"x")
            overlays[name] = [str(f)]
        order = ["s1", "s2", "s3"]

        # restoring the latest snapshot backs up ONLY its own overlay
        # (the regression this narrowing fixes)
        with _inventory(sm, overlays, order), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies()):
            preserved = sm._preserve_snapshot_overlays("vm01", "s3")
        assert [p[0] for p in preserved] == overlays["s3"]

        # a backup path has to be free, so clear the first run's scratch
        # copy before the second (see TestBackupPathsAreClaimed)
        for _o, b in preserved:
            os.unlink(b)

        # restoring a middle snapshot backs up it + the newer one, not the older
        with _inventory(sm, overlays, order), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies()):
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

        # a target that is not a snapshot of this domain at all ->
        # preserve everything (safe but slow fallback)
        with _inventory(sm, overlays, ["s1", "s2"]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies()):
            preserved = sm._preserve_snapshot_overlays("vm01", "gone")
        assert sorted(p[0] for p in preserved) == sorted(
            overlays["s1"] + overlays["s2"]
        )

    def test_skips_missing_files(self, sm: SnapshotManager, tmp_path: Path):
        with _inventory(sm, {"s": ["/does/not/exist.qcow2"]}, ["s"]), \
             patch.object(sm.virsh, "execute_shell") as shell:
            preserved = sm._preserve_snapshot_overlays("vm01", "s")
        assert preserved == []
        shell.assert_not_called()

    def test_uses_sudo_when_configured(self, tmp_path: Path):
        sm = SnapshotManager({"use_sudo": True})
        overlay = tmp_path / "o.qcow2"
        overlay.write_bytes(b"x")
        calls: list[str] = []
        with _inventory(sm, {"s": [str(overlay)]}, ["s"]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies(calls,
                                                         strip_sudo=True)):
            sm._preserve_snapshot_overlays("vm01", "s")
        assert calls[0].startswith("sudo cp ")
        assert "sudo mv -fT " in calls[0]


class TestStrictOverlayInventory:
    """#193 review finding 1.

    The helpers behind the display verbs skip a snapshot they cannot read.
    Preservation cannot: a snapshot missing from the inventory is one whose
    overlay nothing backs up, and ``snapshot-revert`` deletes it anyway.
    """

    def _chain(self, tmp_path: Path):
        overlays = {}
        for name in ("s1", "s2", "s3"):
            f = tmp_path / f"{name}.qcow2"
            f.write_bytes(b"x")
            overlays[name] = str(f)
        return overlays

    def _xml_for(self, overlays, parents):
        def xml_for(name):
            if overlays.get(name) is None:
                return None
            return _snap_xml(name, overlays[name], parents.get(name))
        return xml_for

    def test_a_readable_chain_is_enumerated_oldest_first(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        overlays = self._chain(tmp_path)
        parents = {"s2": "s1", "s3": "s2"}
        with patch.object(sm.virsh, "execute", side_effect=_virsh_snapshots(
                ["s2", "s3", "s1"], self._xml_for(overlays, parents))):
            found, order = sm._strict_overlay_inventory("vm01")
        assert order == ["s1", "s2", "s3"]
        assert found == {n: [overlays[n]] for n in ("s1", "s2", "s3")}

    @pytest.mark.parametrize("fault,expected", [
        ("unparseable", "does not parse"),
        ("unreadable", "cannot read the metadata"),
        ("no-list", "cannot list the snapshots"),
        ("orphan", "does not place"),
    ])
    def test_a_gap_in_the_inventory_never_reaches_snapshot_revert(
        self, sm: SnapshotManager, tmp_path: Path, fault, expected
    ):
        overlays = self._chain(tmp_path)
        parents = {"s2": "s1", "s3": "s2"}

        def xml_for(name):
            if fault == "unreadable" and name == "s2":
                return None
            if fault == "unparseable" and name == "s2":
                return "<domainsnapshot><disks>"          # truncated
            return _snap_xml(name, overlays[name], parents.get(name))

        if fault == "orphan":
            parents["s2"] = "a-snapshot-that-is-not-listed"

        seen: list[str] = []
        calls: list[str] = []
        # a double that really performs the copy: with a guard removed the
        # backup then *succeeds*, so the unsafe path is observable instead
        # of being masked by the later "no backup on disk" refusal
        with patch.object(sm.virsh, "execute", side_effect=_virsh_snapshots(
                ["s1", "s2", "s3"], xml_for,
                list_ok=(fault != "no-list"), seen=seen,
                disk=str(tmp_path / "disk.qcow2"))), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies(calls)):
            with pytest.raises(SnapshotError, match=expected):
                sm.snapshot_restore("vm01", "s1")

        # the destructive command was never issued, and nothing was copied
        assert "snapshot-revert" not in seen
        assert calls == []
        # every overlay is still on disk
        for path in overlays.values():
            assert os.path.isfile(path)


class TestBackupPathsAreClaimed:
    """#193 review findings 2 and 3.

    ``.preserve`` is not a reserved namespace and ``cp`` is happy to write
    through whatever is already at the destination, so a backup path is
    only usable once it has been shown to be nobody else's.
    """

    def test_a_snapshot_named_dot_preserve_refuses_rather_than_clobbering_it(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """Snapshot names are free-form: take `s1`, then take `s1.preserve`,
        and libvirt names the second one's overlay exactly what the backup
        of the first one's overlay wants to be called."""
        overlay = tmp_path / "disk.s1"
        overlay.write_bytes(b"older")
        sibling = tmp_path / "disk.s1.preserve"     # a live overlay
        sibling.write_bytes(b"newer")

        with _inventory(sm,
                        {"s1": [str(overlay)], "s1.preserve": [str(sibling)]},
                        ["s1", "s1.preserve"]), \
             patch.object(sm.virsh, "execute_shell") as shell:
            with pytest.raises(SnapshotError) as excinfo:
                sm._preserve_snapshot_overlays("vm01", "s1")

        assert str(sibling) in str(excinfo.value)
        shell.assert_not_called()
        # the newer snapshot's only copy is neither overwritten nor reaped
        assert sibling.read_bytes() == b"newer"

    def test_the_collision_is_caught_by_name_not_by_the_path_being_occupied(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """The "must not already exist" check would also stop the case
        above, which makes it a poor test of the name check. Here the
        colliding overlay is declared but is not on disk, so only knowing
        the name can refuse."""
        overlay = tmp_path / "disk.s1"
        overlay.write_bytes(b"older")
        sibling = tmp_path / "disk.s1.preserve"     # declared, absent
        assert not sibling.exists()

        calls: list[str] = []
        with _inventory(sm,
                        {"s1": [str(overlay)], "s1.preserve": [str(sibling)]},
                        ["s1", "s1.preserve"]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies(calls)):
            with pytest.raises(SnapshotError, match="overlays of"):
                sm._preserve_snapshot_overlays("vm01", "s1")

        # nothing ran, and in particular nothing was written to the path
        # that is the other snapshot's live overlay
        assert calls == []
        assert not sibling.exists()

    def test_a_backup_path_that_already_exists_is_refused_not_written_through(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """cp follows an existing destination symlink, so the bytes land in
        whatever it points at — and the reap afterwards would remove it."""
        overlay = tmp_path / "o.qcow2"
        overlay.write_bytes(b"overlay")
        victim = tmp_path / "somebody-elses-file"
        victim.write_bytes(b"do not touch")
        (tmp_path / "o.qcow2.preserve").symlink_to(victim)

        with _inventory(sm, {"s1": [str(overlay)]}, ["s1"]), \
             patch.object(sm.virsh, "execute_shell") as shell:
            with pytest.raises(SnapshotError, match="already exists"):
                sm._preserve_snapshot_overlays("vm01", "s1")

        shell.assert_not_called()
        assert victim.read_bytes() == b"do not touch"

    def test_an_overlay_that_is_a_symlink_is_refused_not_dereferenced(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """os.path.isfile says True for a symlink to a regular file. cp would
        copy the referent under the link's name, and the put-back would then
        replace the link with a plain file."""
        real = tmp_path / "elsewhere.qcow2"
        real.write_bytes(b"x")
        overlay = tmp_path / "o.qcow2"
        overlay.symlink_to(real)

        with _inventory(sm, {"s1": [str(overlay)]}, ["s1"]), \
             patch.object(sm.virsh, "execute_shell") as shell:
            with pytest.raises(SnapshotError, match="not a regular file"):
                sm._preserve_snapshot_overlays("vm01", "s1")
        shell.assert_not_called()

    def test_a_copy_that_exits_zero_without_writing_anything_is_refused(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """cp can exit 0 having written nothing into the file reserved for
        it, and an empty backup puts back an empty overlay. The backup is
        confirmed on the filesystem, not by exit code."""
        overlay = tmp_path / "o.qcow2"
        overlay.write_bytes(b"x")

        calls: list[str] = []

        def capture(cmd, *_a, **_kw):
            calls.append(cmd)
            return _result()

        with _inventory(sm, {"s1": [str(overlay)]}, ["s1"]), \
             patch.object(sm.virsh, "execute_shell", side_effect=capture):
            with pytest.raises(SnapshotError, match="not a usable copy"):
                sm._preserve_snapshot_overlays("vm01", "s1")

        # the empty reservation is reaped, not left to block the next run
        assert any(c.startswith("rm -f") and "o.qcow2.preserve" in c
                   for c in calls)


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
        assert cmd.startswith("mv -fT ")
        assert "rsync" not in cmd
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


class TestSnapshotMetadataIsComplete:
    """#193 round-2 finding 1.

    Well-formed XML is not the same as usable metadata. Each shape below
    leaves the inventory short of a descendant's overlay while looking
    exactly like "that snapshot owns none" — and the revert deletes it all
    the same.
    """

    @pytest.mark.parametrize("fault,expected", [
        ("no-disks", "has no <disks> element"),
        ("empty-disks", "lists no disks at all"),
        ("no-source", "names no overlay file"),
        ("empty-source", "names no overlay file"),
        ("unclassified-disk", "does not say whether"),
        ("empty-mode", "unrecognised snapshot mode"),
        ("unknown-mode", "unrecognised snapshot mode"),
        ("whitespace-source", "names no overlay file"),
        ("relative-source", "relative overlay path"),
        ("wrong-root", "not a <domainsnapshot>"),
        ("name-mismatch", "got metadata naming"),
        ("duplicate-name", "more than once"),
        ("blank-entry", "blank entry"),
    ])
    def test_incomplete_metadata_never_reaches_snapshot_revert(
        self, sm: SnapshotManager, tmp_path: Path, fault, expected
    ):
        overlays = {n: str(tmp_path / f"{n}.qcow2")
                    for n in ("s1", "s2", "s3")}
        for target in overlays.values():
            Path(target).write_bytes(b"x")
        parents = {"s2": "s1", "s3": "s2"}

        bodies = {
            "no-disks": "",
            "empty-disks": "<disks></disks>",
            "no-source":
                "<disks><disk name='vda' snapshot='external'/></disks>",
            "empty-source":
                "<disks><disk name='vda' snapshot='external'>"
                "<source file=''/></disk></disks>",
            "unclassified-disk":
                f"<disks><disk name='vda'>"
                f"<source file='{overlays['s2']}'/></disk></disks>",
            # an empty or unknown mode is not a positively identified
            # internal disk, and must not be read as one
            "empty-mode":
                f"<disks><disk name='vda' snapshot=''>"
                f"<source file='{overlays['s2']}'/></disk></disks>",
            "unknown-mode":
                f"<disks><disk name='vda' snapshot='externl'>"
                f"<source file='{overlays['s2']}'/></disk></disks>",
            "whitespace-source":
                "<disks><disk name='vda' snapshot='external'>"
                "<source file='   '/></disk></disks>",
            "relative-source":
                "<disks><disk name='vda' snapshot='external'>"
                "<source file='disk.s2'/></disk></disks>",
        }

        def xml_for(name):
            if name != "s2":
                return _snap_xml(name, overlays[name], parents.get(name))
            if fault in bodies:
                return (f"<domainsnapshot><name>s2</name>"
                        f"<parent><name>s1</name></parent>"
                        f"{bodies[fault]}</domainsnapshot>")
            good = _snap_xml("s2", overlays["s2"], "s1")
            if fault == "wrong-root":
                return good.replace("domainsnapshot", "domain")
            if fault == "name-mismatch":
                return good.replace("<name>s2</name>",
                                    "<name>s2-renamed</name>", 1)
            return good

        names = ["s1", "s2", "s3"]
        if fault == "duplicate-name":
            names = ["s1", "s2", "s2", "s3"]
        if fault == "blank-entry":
            names = ["s1", "", "s2", "s3"]

        seen: list[str] = []
        calls: list[str] = []
        with patch.object(sm.virsh, "execute", side_effect=_virsh_snapshots(
                names, xml_for, seen=seen,
                disk=str(tmp_path / "disk.qcow2"))), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies(calls)):
            with pytest.raises(SnapshotError, match=expected):
                sm.snapshot_restore("vm01", "s1")

        assert "snapshot-revert" not in seen
        assert calls == []
        for target in overlays.values():
            assert os.path.isfile(target)


class TestSnapshotNamesSurviveTheParse:
    """#193 round-3 finding 1.

    libvirt accepts a snapshot named three spaces, and one containing
    U+2028. ``splitlines()`` breaks on the second and ``.strip()`` erases
    the first — and a snapshot that never reaches the inventory is one
    whose overlay nothing backs up, while the revert deletes it anyway.
    """

    @pytest.mark.parametrize("leaf", ["   ", "a\u2028b"],
                             ids=["three-spaces", "u2028"])
    def test_an_oddly_named_leaf_is_enumerated_and_backed_up(
        self, sm: SnapshotManager, tmp_path: Path, leaf
    ):
        overlays = {"s1": str(tmp_path / "s1.qcow2"),
                    leaf: str(tmp_path / "leaf.qcow2")}
        for target in overlays.values():
            Path(target).write_bytes(b"x")
        parents = {leaf: "s1"}

        def xml_for(name):
            return _snap_xml(name, overlays[name], parents.get(name))

        with patch.object(sm.virsh, "execute", side_effect=_virsh_snapshots(
                ["s1", leaf], xml_for)):
            found, order = sm._strict_overlay_inventory("vm01")

            assert order == ["s1", leaf]
            assert found[leaf] == [overlays[leaf]]

            # and reverting to s1 actually backs the leaf's overlay up
            with patch.object(sm.virsh, "execute_shell",
                              side_effect=_shell_that_runs()):
                preserved = sm._preserve_snapshot_overlays("vm01", "s1")

        assert sorted(o for o, _b in preserved) == sorted(overlays.values())


class TestBackupPathsAreReserved:
    """#193 round-3 findings 2 and 3.

    Checking that a path is free is not the same as owning it. Another
    restore, or a ``snapshot take`` whose overlay is named the same thing,
    can take it in between — and then the copy writes through somebody
    else's file and the cleanup deletes it.
    """

    def test_a_path_taken_after_the_check_is_refused_not_written_through(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """Patching out the check models the race exactly: the path was
        free when it was looked at, and taken by the time it was used —
        which is what libvirt creating `<other>.preserve` does."""
        overlay = tmp_path / "o.qcow2"
        overlay.write_bytes(b"overlay")
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"somebody else's overlay")

        with _inventory(sm, {"s1": [str(overlay)]}, ["s1"]), \
             patch.object(sm, "_claim_backup_paths"), \
             patch.object(sm.virsh, "execute_shell") as shell:
            with pytest.raises(SnapshotError, match="could not reserve"):
                sm._preserve_snapshot_overlays("vm01", "s1")

        shell.assert_not_called()
        assert backup.read_bytes() == b"somebody else's overlay"

    def test_the_reservation_is_a_non_empty_file(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """Measured against libvirt 10.0.0: an external snapshot whose
        overlay path already holds an *empty* file silently adopts that
        file as the overlay, and only a non-empty one is refused with
        "already exists and is not a block device". An empty reservation
        would therefore hand libvirt the very placeholder it is meant to
        defend, and the copy would then overwrite a live overlay."""
        backup = tmp_path / "o.qcow2.preserve"
        sm._reserve_backup_paths([(str(tmp_path / "o.qcow2"), str(backup))])

        assert backup.stat().st_size > 0
        # and the temporary it was built through is not left lying about
        assert [entry.name for entry in tmp_path.iterdir()] == [backup.name]

    def test_a_failed_marker_write_leaves_nothing_behind(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """The temporary is scaffolding. If writing the marker fails it is
        never linked into place, so nothing tracks it for cleanup — it has
        to remove itself."""
        backup = tmp_path / "o.qcow2.preserve"

        def explode(*_a, **_kw):
            raise OSError(28, "No space left on device")

        with patch("boxman.providers.libvirt.snapshot.os.write",
                   side_effect=explode):
            with pytest.raises(SnapshotError, match="could not reserve"):
                sm._reserve_backup_paths(
                    [(str(tmp_path / "o.qcow2"), str(backup))])

        assert not backup.exists()
        assert list(tmp_path.iterdir()) == []

    def test_the_marker_is_written_whole_even_in_short_writes(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """A short write would truncate the marker, and a truncated marker
        is one _backup_was_written does not recognise — so an untouched
        reservation would pass for a finished backup."""
        backup = tmp_path / "o.qcow2.preserve"
        real_write = os.write

        def one_byte_at_a_time(fd, data):
            return real_write(fd, data[:1])

        with patch("boxman.providers.libvirt.snapshot.os.write",
                   side_effect=one_byte_at_a_time):
            signatures = sm._reserve_backup_paths(
                [(str(tmp_path / "o.qcow2"), str(backup))])

        assert backup.read_bytes() == sm._RESERVATION_MARKER
        # and the reservation still reads as "nothing copied here yet"
        assert not sm._backup_was_written(str(backup), signatures[str(backup)])

    def test_reserving_a_taken_path_fails_without_disturbing_it(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"not ours")

        with pytest.raises(SnapshotError, match="could not reserve"):
            sm._reserve_backup_paths([(str(tmp_path / "o.qcow2"), str(backup))])

        assert backup.read_bytes() == b"not ours"
        assert [entry.name for entry in tmp_path.iterdir()] == [backup.name]

    def test_a_partial_reservation_removes_only_the_files_it_made(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        first = tmp_path / "a.qcow2"
        first.write_bytes(b"a")
        second = tmp_path / "b.qcow2"
        second.write_bytes(b"b")
        taken = tmp_path / "b.qcow2.preserve"
        taken.write_bytes(b"not ours")

        with _inventory(sm, {"s": [str(first), str(second)]}, ["s"]), \
             patch.object(sm, "_claim_backup_paths"), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            with pytest.raises(SnapshotError, match="could not reserve"):
                sm._preserve_snapshot_overlays("vm01", "s")

        # the reservation it did make is gone ...
        assert not (tmp_path / "a.qcow2.preserve").exists()
        # ... and the file it did not make is untouched
        assert taken.read_bytes() == b"not ours"

    def test_a_real_preserve_and_put_back_round_trips_the_bytes(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """End to end with real cp, mv and rm — the commands as built,
        against real files."""
        overlay = tmp_path / "o.qcow2"
        overlay.write_bytes(b"important")

        with _inventory(sm, {"s1": [str(overlay)]}, ["s1"]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            preserved = sm._preserve_snapshot_overlays("vm01", "s1")
            backup = Path(preserved[0][1])
            assert backup.read_bytes() == b"important"

            overlay.unlink()                     # the revert deletes it
            unrecovered = sm._restore_preserved_overlays(preserved)

        assert unrecovered == []
        assert overlay.read_bytes() == b"important"
        assert not backup.exists()

    def test_a_real_move_into_a_directory_leaves_the_backup_where_it_says(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """`mv -f` puts the backup *inside* the directory and exits 0, so
        the terminal error would name a path that no longer held anything.
        `-T` refuses, and the recovery instructions stay true."""
        overlay = tmp_path / "o.qcow2"
        overlay.mkdir()                          # a directory, not the disk
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"the only copy")

        with patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            unrecovered = sm._restore_preserved_overlays(
                [(str(overlay), str(backup))])

        assert unrecovered == [(str(overlay), str(backup))]
        assert backup.read_bytes() == b"the only copy"
        assert not (overlay / "o.qcow2.preserve").exists()


class TestOwnershipSurvivesFailures:
    """#193 round-6 findings 1 and 2.

    Once a backup path is reserved it belongs to this call until it is
    handed back or removed. A runner that raises rather than returning a
    result used to walk out of the function leaving reservations behind,
    and the check that a copy landed used to need a read permission the
    copy itself does not.
    """

    def test_a_runner_that_cannot_start_the_copy_reaps_and_refuses(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """execute_shell turns a command that ran and failed into a
        result, but a runner that cannot start one at all still raises."""
        overlay = tmp_path / "o.qcow2"
        overlay.write_bytes(b"x")
        calls: list[str] = []

        def explode(cmd, *_a, **_kw):
            calls.append(cmd)
            if cmd.startswith("rm -f"):
                # really run the cleanup, so the assertion below is about
                # the filesystem and not about the double
                subprocess.run(cmd, shell=True, check=False)
                return _result()
            raise OSError(24, "Too many open files")

        with _inventory(sm, {"s1": [str(overlay)]}, ["s1"]), \
             patch.object(sm.virsh, "execute_shell", side_effect=explode):
            with pytest.raises(SnapshotError,
                               match="could not run the overlay backup"):
                sm._preserve_snapshot_overlays("vm01", "s1")

        assert any(c.startswith("rm -f") and "o.qcow2.preserve" in c
                   for c in calls)
        assert not (tmp_path / "o.qcow2.preserve").exists()

    def test_a_failure_before_the_revert_does_not_strand_the_backups(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """The memory handling runs after preservation and before the
        revert. A raise there used to leave the backups behind, and the
        next restore refuses on them."""
        backup = tmp_path / "o.qcow2.preserve"
        calls: list[str] = []

        def capture(cmd, *_a, **_kw):
            calls.append(cmd)
            return _result()

        with _not_running(sm), \
             patch.object(sm, "_preserve_snapshot_overlays",
                          return_value=[(str(tmp_path / "o.qcow2"),
                                         str(backup))]), \
             patch.object(sm, "_memory_path_from_xml",
                          side_effect=OSError(5, "I/O error")), \
             patch.object(sm.virsh, "execute") as execute, \
             patch.object(sm.virsh, "execute_shell", side_effect=capture):
            with pytest.raises(SnapshotError, match="could not prepare"):
                sm.snapshot_restore("vm01", "s1")

        execute.assert_not_called()
        assert any(c.startswith("rm -f") and str(backup) in c for c in calls)

    def test_a_cleanup_that_cannot_run_still_names_what_it_left(
        self, sm: SnapshotManager
    ):
        """This is the function that promises to name what it left behind,
        so it must not be the one that dies quietly."""
        def explode(*_a, **_kw):
            raise OSError(24, "Too many open files")

        with patch.object(sm.virsh, "execute_shell", side_effect=explode), \
             patch.object(sm, "logger") as logger:
            sm._reap_preserve_files(["/overlays/a.qcow2.preserve"])

        assert any("/overlays/a.qcow2.preserve" in str(call)
                   for call in logger.warning.call_args_list)

    @pytest.mark.skipif(os.geteuid() == 0,
                        reason="root can read a 0000 file")
    def test_verification_does_not_need_to_read_the_backup(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """A `sudo cp -p` of a root-owned 0600 overlay hands back a
        root-owned 0600 backup. Reading it as the boxman user then fails,
        and a check that reads would condemn a perfectly good copy."""
        backup = tmp_path / "o.qcow2.preserve"
        signatures = sm._reserve_backup_paths(
            [(str(tmp_path / "o.qcow2"), str(backup))])

        backup.write_bytes(b"a real overlay")      # the copy lands ...
        os.chmod(backup, 0o000)                    # ... unreadable by us
        try:
            assert sm._backup_was_written(str(backup),
                                          signatures[str(backup)])
        finally:
            os.chmod(backup, 0o600)


class TestRoundSevenGaps:
    """#193 round-7 findings.

    Four ways the branch could still lose an overlay or mis-report one:
    a scratch path nobody owned, a name the list parse swallowed, a
    reservation published before the caller could clean it up, and a
    recovery failure that escaped as the wrong kind of error.
    """

    def test_a_snapshot_whose_overlay_is_the_scratch_path_is_refused(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """The scratch a copy lands on before being renamed is a real file
        in the overlay directory, and cleanup removes it — so like the
        backup itself it has to be provably nobody else's."""
        overlay = tmp_path / "disk.s1"
        overlay.write_bytes(b"older")
        sibling = tmp_path / "disk.s1.preserve.copy"      # a live overlay
        sibling.write_bytes(b"newer")

        calls: list[str] = []
        with _inventory(sm,
                        {"s1": [str(overlay)],
                         "s1.preserve.copy": [str(sibling)]},
                        ["s1", "s1.preserve.copy"]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies(calls)):
            with pytest.raises(SnapshotError, match="overlays of"):
                sm._preserve_snapshot_overlays("vm01", "s1")

        assert calls == []
        assert sibling.read_bytes() == b"newer"

    @pytest.mark.parametrize("names", [["\n"], ["s1", "\n"]],
                             ids=["only-an-lf-name", "lf-named-tail-leaf"])
    def test_a_name_made_of_newlines_is_refused_not_swallowed(
        self, sm: SnapshotManager, tmp_path: Path, names
    ):
        """Such a name leaves no fragment to refuse on. Popping every
        trailing empty fragment erased the evidence, and the snapshot
        simply vanished from the inventory."""
        with patch.object(sm.virsh, "execute",
                          side_effect=_virsh_snapshots(names, lambda _n: None)):
            with pytest.raises(SnapshotError, match="blank entry"):
                sm._strict_overlay_inventory("vm01")

    def test_a_reservation_is_not_published_before_it_can_be_cleaned_up(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """The signature used to be read from the published name. A
        failure there left the marker in place while the caller had no
        idea the path existed — and every later restore refuses on it."""
        backup = tmp_path / "o.qcow2.preserve"

        def explode(*_a, **_kw):
            raise OSError(5, "Input/output error")

        with patch("boxman.providers.libvirt.snapshot.os.stat",
                   side_effect=explode):
            with pytest.raises(SnapshotError, match="could not reserve"):
                sm._reserve_backup_paths(
                    [(str(tmp_path / "o.qcow2"), str(backup))])

        assert list(tmp_path.iterdir()) == []

    def test_a_put_back_runner_that_raises_is_still_terminal(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """A raised runner error used to escape before the postcondition,
        so the caller never learned an overlay was still in its backup —
        and the manager retried it as an ordinary failure."""
        overlay = tmp_path / "o.qcow2"          # deleted by the revert
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"the only copy")

        with _not_running(sm), \
             patch.object(sm, "_preserve_snapshot_overlays",
                          return_value=[(str(overlay), str(backup))]), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute", return_value=_result()), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=OSError(24, "Too many open files")):
            with pytest.raises(SnapshotRecoveryError) as excinfo:
                sm.snapshot_restore("vm01", "snap1")

        assert "mv -fT" in str(excinfo.value)
        assert backup.read_bytes() == b"the only copy"

    def test_recompression_cannot_mask_the_terminal_overlay_error(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """Re-compressing the memory image is housekeeping. It used to run
        before the terminal raise, so its own failure hid an overlay that
        was already known not to be back."""
        overlay = tmp_path / "o.qcow2"          # deleted by the revert
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"the only copy")

        with _not_running(sm), \
             patch.object(sm, "_preserve_snapshot_overlays",
                          return_value=[(str(overlay), str(backup))]), \
             patch.object(sm, "_memory_path_from_xml",
                          return_value=str(tmp_path / "mem.raw")), \
             patch.object(sm, "_recompress_after_revert",
                          side_effect=OSError(5, "Input/output error")), \
             patch.object(sm.virsh, "execute", return_value=_result()), \
             patch.object(sm.virsh, "execute_shell",
                          return_value=_result(ok=False, stderr="refused")):
            with pytest.raises(SnapshotRecoveryError):
                sm.snapshot_restore("vm01", "snap1")

        assert backup.read_bytes() == b"the only copy"


class TestTheGuestHoldsStillWhileCopying:
    """#195.

    The head overlay is the live disk — `domblklist` names it — and a
    revert deletes it, which is why it is in the backup set. Measured
    against libvirt 10.0.0: a memory snapshot lets a running domain be
    reverted, and the revert removes that file. Copying it while qemu
    writes gives a mix of blocks from different instants, and the
    put-back would install that in its place.
    """

    def _virsh(self, running, calls, suspend_ok=True, revert_ok=True):
        """Answer the running-domain probe; record every verb in *calls*."""
        def execute(*args, **_kw):
            calls.append(args[0])
            if args[0] == "list":
                return _result(stdout="".join(f"{n}\n" for n in running))
            if args[0] == "domstate":
                # nothing should consult this: its output is translated
                return _result(stdout="en cours d'exécution\n")
            if args[0] == "suspend" and not suspend_ok:
                return _result(ok=False, stderr="domain is not running")
            if args[0] == "snapshot-revert" and not revert_ok:
                return _result(ok=False, stderr="bad name")
            return _result()
        return execute

    def _restoring(self, sm, calls, **kw):
        """The common patches for a restore whose backup is a no-op."""
        return (
            patch.object(sm, "_memory_path_from_xml", return_value=None),
            patch.object(sm, "_restore_preserved_overlays", return_value=[]),
            patch.object(sm.virsh, "execute",
                         side_effect=self._virsh(calls=calls, **kw)),
        )

    def test_a_running_guest_is_paused_before_its_overlays_are_copied(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        calls: list[str] = []

        def preserve(*_a, **_kw):
            calls.append("the copy")
            return []

        mem, restore, execute = self._restoring(sm, calls, running=["vm01"])
        with patch.object(sm, "_preserve_snapshot_overlays",
                          side_effect=preserve), mem, restore, execute:
            assert sm.snapshot_restore("vm01", "s1") is True

        # before the copy, which is the whole point -- asserting only that
        # it precedes the revert would allow pausing after the copy
        assert calls.index("suspend") < calls.index("the copy")
        assert calls.index("the copy") < calls.index("snapshot-revert")
        # a successful revert sets the state itself, from the snapshot's
        # memory image -- resuming here would fight it
        assert "resume" not in calls

    def test_the_running_check_does_not_read_a_translated_state_name(
        self, sm: SnapshotManager
    ):
        """`virsh domstate` prints its state through gettext. Comparing
        that against the English "running" reads a running guest as idle
        under any other locale -- exactly the unpaused copy this
        prevents. Domain names are not translated."""
        calls: list[str] = []
        mem, restore, execute = self._restoring(sm, calls, running=["vm01"])
        with patch.object(sm, "_preserve_snapshot_overlays",
                          return_value=[]), mem, restore, execute:
            assert sm.snapshot_restore("vm01", "s1") is True

        # the double answers domstate in French; the guest is paused anyway
        assert "suspend" in calls
        assert "domstate" not in calls

    def test_a_guest_that_is_not_running_is_left_alone(
        self, sm: SnapshotManager
    ):
        calls: list[str] = []
        mem, restore, execute = self._restoring(
            sm, calls, running=["some-other-vm"])
        with patch.object(sm, "_preserve_snapshot_overlays",
                          return_value=[]), mem, restore, execute:
            assert sm.snapshot_restore("vm01", "s1") is True

        assert "suspend" not in calls
        assert "resume" not in calls

    def test_a_refused_backup_resumes_the_guest(self, sm: SnapshotManager):
        """Refusing to revert must not leave the VM paused: nothing was
        changed, so nothing should look different afterwards."""
        calls: list[str] = []
        with patch.object(sm, "_preserve_snapshot_overlays",
                          side_effect=SnapshotError("no space")), \
             patch.object(sm.virsh, "execute",
                          side_effect=self._virsh(["vm01"], calls)):
            with pytest.raises(SnapshotError):
                sm.snapshot_restore("vm01", "s1")

        assert "suspend" in calls
        assert "resume" in calls
        assert "snapshot-revert" not in calls

    def test_a_failed_revert_reports_the_pause_rather_than_undoing_it(
        self, sm: SnapshotManager
    ):
        """libvirt installs the restored domain's paused state *before*
        writing the snapshot metadata, and that write can fail — so a
        revert reported as failed may already have put a legitimately
        paused domain in place. Resuming on a failed result would start
        it. A failed command result cannot tell the two apart."""
        calls: list[str] = []
        mem, restore, execute = self._restoring(
            sm, calls, running=["vm01"], revert_ok=False)
        with patch.object(sm, "_preserve_snapshot_overlays",
                          return_value=[]), mem, restore, execute, \
             patch.object(sm, "logger") as logger:
            assert sm.snapshot_restore("vm01", "s1") is False

        assert "suspend" in calls
        assert "resume" not in calls
        # but it says so, and says what to check
        assert any("virsh resume vm01" in str(call)
                   for call in logger.error.call_args_list)

    def test_a_guest_that_cannot_be_paused_refuses_the_restore(
        self, sm: SnapshotManager
    ):
        """Copying the live disk anyway is the thing this exists to stop."""
        calls: list[str] = []
        with patch.object(sm, "_preserve_snapshot_overlays") as preserve, \
             patch.object(sm.virsh, "execute",
                          side_effect=self._virsh(["vm01"], calls,
                                                  suspend_ok=False)):
            with pytest.raises(SnapshotError, match="could not pause"):
                sm.snapshot_restore("vm01", "s1")

        preserve.assert_not_called()
        assert "snapshot-revert" not in calls

    def test_an_unanswerable_guest_state_refuses_the_restore(
        self, sm: SnapshotManager
    ):
        """Not knowing whether the guest is writing is not the same as
        knowing it is not."""
        with patch.object(sm, "_preserve_snapshot_overlays") as preserve, \
             patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="no domain")):
            with pytest.raises(SnapshotError, match="cannot tell whether"):
                sm.snapshot_restore("vm01", "s1")
        preserve.assert_not_called()

    def test_a_resume_that_cannot_run_does_not_replace_the_real_error(
        self, sm: SnapshotManager
    ):
        """The resume runs while another error is on its way up. Raising
        here would swap the cause for the consequence."""
        def execute(*args, **_kw):
            if args[0] == "list":
                return _result(stdout="vm01\n")
            if args[0] == "resume":
                raise OSError(24, "Too many open files")
            return _result()

        with patch.object(sm, "_preserve_snapshot_overlays",
                          side_effect=SnapshotError("no space")), \
             patch.object(sm.virsh, "execute", side_effect=execute), \
             patch.object(sm, "logger") as logger:
            with pytest.raises(SnapshotError, match="no space"):
                sm.snapshot_restore("vm01", "s1")

        assert any("virsh resume vm01" in str(call)
                   for call in logger.error.call_args_list)


class TestPendingRecoveryBlocksTheNextRestore:
    """#193 round-2 finding 3.

    A ``.preserve`` whose original is *missing* is exactly what an
    interrupted restore leaves behind. Dropping the missing original before
    its backup was ever examined meant the next restore never saw it:
    it reverted a second time and reported success.
    """

    def test_a_leftover_is_noticed_even_though_its_original_is_gone(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        present = tmp_path / "s1.qcow2"
        present.write_bytes(b"x")
        missing = tmp_path / "s2.qcow2"          # the interrupted restore
        leftover = tmp_path / "s2.qcow2.preserve"
        leftover.write_bytes(b"the only copy")
        assert not missing.exists()

        calls: list[str] = []
        with _inventory(sm, {"s1": [str(present)], "s2": [str(missing)]},
                        ["s1", "s2"]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies(calls)):
            with pytest.raises(SnapshotError, match="already exists"):
                sm._preserve_snapshot_overlays("vm01", "s1")

        assert calls == []
        assert leftover.read_bytes() == b"the only copy"

    def test_the_check_runs_even_when_every_original_is_missing(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """The early "nothing at risk" return used to skip the backup-path
        check completely, which is the very state a failed put-back leaves
        behind."""
        missing = tmp_path / "s1.qcow2"
        leftover = tmp_path / "s1.qcow2.preserve"
        leftover.write_bytes(b"the only copy")

        with _inventory(sm, {"s1": [str(missing)]}, ["s1"]), \
             patch.object(sm.virsh, "execute_shell") as shell:
            with pytest.raises(SnapshotError, match="already exists"):
                sm._preserve_snapshot_overlays("vm01", "s1")
        shell.assert_not_called()


class TestPutBackPostcondition:
    """#193 round-2 finding 2, second half.

    Whether the overlays are back is a question about the filesystem, not
    about what ``mv`` returned.
    """

    def test_a_pair_with_neither_original_nor_backup_is_unrecovered(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        overlay = tmp_path / "o.qcow2"           # deleted by the revert
        backup = tmp_path / "o.qcow2.preserve"   # and never created
        with patch.object(sm.virsh, "execute_shell",
                          return_value=_result()) as shell:
            unrecovered = sm._restore_preserved_overlays(
                [(str(overlay), str(backup))])

        # nothing to move and nothing to reap, so neither work list held it
        # -- but the overlay is still missing, which is the whole question
        assert unrecovered == [(str(overlay), str(backup))]
        shell.assert_not_called()

    def test_a_move_into_a_directory_at_the_original_path_is_unrecovered(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """`mv -f backup dir` succeeds by putting the backup *inside* the
        directory, and exits 0 with the overlay still not there."""
        overlay = tmp_path / "o.qcow2"
        overlay.mkdir()                          # a directory, not the disk
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"x")

        with patch.object(sm.virsh, "execute_shell", return_value=_result()):
            unrecovered = sm._restore_preserved_overlays(
                [(str(overlay), str(backup))])
        assert unrecovered == [(str(overlay), str(backup))]


class TestBackupIsAPrecondition:
    """#164 CL-D1 / CL-D2 / CL-R2.

    ``snapshot-revert`` deletes the overlays of the target snapshot and of
    every snapshot newer than it. The backup taken beforehand is therefore
    the only thing standing between a restore and the loss of the rest of
    the chain -- so a backup that did not happen has to stop the revert,
    clean up whatever half of it landed, and not have needed a second full
    copy's worth of free space to attempt in the first place.
    """

    def test_a_failed_backup_refuses_instead_of_reporting_nothing_to_do(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        overlay = tmp_path / "s1.qcow2"
        overlay.write_bytes(b"x")
        with _inventory(sm, {"s1": [str(overlay)]}, ["s1"]), \
             patch.object(
                 sm.virsh, "execute_shell",
                 return_value=_result(ok=False,
                                      stderr="No space left on device")):
            with pytest.raises(SnapshotError) as excinfo:
                sm._preserve_snapshot_overlays("vm01", "s1")

        # the operator has to be able to act on this: which vm, which
        # snapshot, and what the copy itself said
        message = str(excinfo.value)
        assert "vm01" in message
        assert "s1" in message
        assert "No space left on device" in message

    def test_a_failed_backup_never_reaches_snapshot_revert(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """The whole of CL-D1: an empty return used to mean both "nothing
        was at risk" and "the copy failed", and the revert ran on either."""
        overlay = tmp_path / "s1.qcow2"
        overlay.write_bytes(b"x")
        with _not_running(sm), \
             _inventory(sm, {"s1": [str(overlay)]}, ["s1"]), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute_shell",
                          return_value=_result(ok=False, stderr="disk full")), \
             patch.object(sm.virsh, "execute",
                          return_value=_result()) as execute:
            with pytest.raises(SnapshotError):
                sm.snapshot_restore("vm01", "s1")

        assert not any(call.args and call.args[0] == "snapshot-revert"
                       for call in execute.call_args_list)

    def test_a_failed_backup_reaps_every_copy_it_may_have_made(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        overlays = {}
        for name in ("s1", "s2"):
            f = tmp_path / f"{name}.qcow2"
            f.write_bytes(b"x")
            overlays[name] = [str(f)]

        calls: list[str] = []

        def capture(cmd, *_a, **_kw):
            calls.append(cmd)
            # the copy chain fails; the reap that follows it succeeds
            return _result(ok=cmd.startswith("rm "), stderr="disk full")

        with _inventory(sm, overlays, ["s1", "s2"]), \
             patch.object(sm.virsh, "execute_shell", side_effect=capture):
            with pytest.raises(SnapshotError):
                sm._preserve_snapshot_overlays("vm01", "s1")

        assert len(calls) == 2
        reap = calls[1]
        assert reap.startswith("rm -f ")
        # every destination, not only the one the chain died on: the copies
        # that already succeeded earlier in the && chain are what leaked
        for name in ("s1", "s2"):
            assert f"{overlays[name][0]}.preserve" in reap
        # one rm taking both paths -- an && chain would stop at the first
        # path that resisted and leave the rest behind, which is CL-D2 again
        assert " && " not in reap

    def test_an_overlay_that_cannot_be_stat_ed_refuses_rather_than_dropping_out(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """``os.path.isfile`` answers False for "unreadable" as flatly as
        for "absent". Reading that as absent shrinks the backup set without
        saying so, and the revert then deletes a file nothing copied."""
        overlay = tmp_path / "s1.qcow2"
        overlay.write_bytes(b"x")

        def deny(*_a, **_kw):
            raise PermissionError(13, "Permission denied")

        with _inventory(sm, {"s1": [str(overlay)]}, ["s1"]), \
             patch("boxman.providers.libvirt.snapshot.os.lstat",
                   side_effect=deny), \
             patch.object(sm.virsh, "execute_shell") as shell:
            with pytest.raises(SnapshotError) as excinfo:
                sm._preserve_snapshot_overlays("vm01", "s1")

        assert str(overlay) in str(excinfo.value)
        assert "Permission denied" in str(excinfo.value)
        shell.assert_not_called()

    def test_overlay_paths_are_quoted_not_interpolated(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """The old f"'{src}'" ended its quoted string at an apostrophe in
        the path, handing the rest of the name to the shell as syntax."""
        odd = tmp_path / "it's a snapshot.qcow2"
        odd.write_bytes(b"x")
        calls: list[str] = []
        with _inventory(sm, {"s1": [str(odd)]}, ["s1"]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies(calls)):
            sm._preserve_snapshot_overlays("vm01", "s1")

        # the copy names the overlay; the rename that follows names the
        # reservation it replaces
        words = shlex.split(calls[0])
        assert str(odd) in words
        assert f"{odd}.preserve" == words[-1]

    def test_putting_the_overlays_back_is_a_rename_not_a_copy(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """This path runs after the overlay is already gone, so it must not
        be the one that needs free space in order to succeed."""
        overlay = tmp_path / "o.qcow2"          # deleted by the revert
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"x")
        with patch.object(sm.virsh, "execute_shell",
                          return_value=_result()) as shell:
            sm._restore_preserved_overlays([(str(overlay), str(backup))])

        cmd = shell.call_args.args[0]
        assert cmd.startswith("mv -fT ")
        assert "cp " not in cmd and "rsync" not in cmd

    def test_the_put_back_keeps_the_privilege_the_copy_had(self, tmp_path: Path):
        sm = SnapshotManager({"use_sudo": True})
        overlay = tmp_path / "o.qcow2"          # deleted by the revert
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"x")
        with patch.object(sm.virsh, "execute_shell",
                          return_value=_result()) as shell:
            sm._restore_preserved_overlays([(str(overlay), str(backup))])
        assert shell.call_args.args[0].startswith("sudo mv -fT ")

    def test_a_failed_put_back_keeps_the_only_copy_it_has_left(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        overlay = tmp_path / "o.qcow2"          # deleted by the revert
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"x")

        calls: list[str] = []

        def capture(cmd, *_a, **_kw):
            calls.append(cmd)
            return _result(ok=False, stderr="read-only file system")

        with patch.object(sm.virsh, "execute_shell", side_effect=capture):
            unrecovered = sm._restore_preserved_overlays(
                [(str(overlay), str(backup))])

        # here the backup is the last copy of that overlay in existence;
        # reaping it is precisely the loss the preservation exists to stop.
        # Match any rm naming it, prefixed or not -- `sudo rm -f` deletes
        # just as thoroughly as `rm -f`.
        assert not any("rm " in c and str(backup) in c for c in calls)
        assert backup.exists()
        assert unrecovered == [(str(overlay), str(backup))]

    def test_a_failed_put_back_is_terminal_not_a_successful_restore(
        self, sm: SnapshotManager, tmp_path: Path
    ):
        """The revert already ran. Reporting True here tells the caller the
        chain is intact when an overlay is still sitting in its backup."""
        overlay = tmp_path / "o.qcow2"          # deleted by the revert
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"x")

        with _not_running(sm), \
             patch.object(sm, "_preserve_snapshot_overlays",
                          return_value=[(str(overlay), str(backup))]), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute", return_value=_result()), \
             patch.object(sm.virsh, "execute_shell",
                          return_value=_result(ok=False, stderr="read-only")):
            with pytest.raises(SnapshotRecoveryError) as excinfo:
                sm.snapshot_restore("vm01", "snap1")

        # and it says exactly what to run to finish the job
        message = str(excinfo.value)
        assert str(backup) in message
        # -T, or following the instruction moves the last copy *into* a
        # directory sitting at the original path
        assert "mv -fT" in message


class TestBatchedCommandPrivilege:
    """#193 review finding 5.

    ``execute_shell`` prefixes the first word of the string it is handed,
    which in an ``&&`` chain is the first command only — so a batch has to
    decide privilege per command, and must not write ``sudo `` by hand,
    which skips the policy altogether.
    """

    def _preserve(self, sm, tmp_path, calls):
        overlay = tmp_path / "o.qcow2"
        overlay.write_bytes(b"x")
        other = tmp_path / "p.qcow2"
        other.write_bytes(b"y")
        with _inventory(sm, {"s": [str(overlay), str(other)]}, ["s"]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies(calls,
                                                         strip_sudo=True)):
            sm._preserve_snapshot_overlays("vm01", "s")
        return calls[0]

    def test_a_skipped_copy_is_not_sudo_prefixed_even_under_use_sudo(
        self, tmp_path: Path
    ):
        sm = SnapshotManager({"use_sudo": True, "sudo_skip_commands": ["cp"]})
        cmd = self._preserve(sm, tmp_path, [])
        parts = cmd.split(" && ")
        # the skip applies to cp, and only to cp: mv is a different
        # executable and keeps the privilege use_sudo gives it
        assert not any(part.startswith("sudo cp ") for part in parts)
        assert any(part.startswith("sudo mv -fT ") for part in parts)

    def test_a_forced_copy_is_sudo_prefixed_on_every_element(
        self, tmp_path: Path
    ):
        sm = SnapshotManager({"use_sudo": False,
                              "force_sudo_commands": ["cp"]})
        cmd = self._preserve(sm, tmp_path, [])
        parts = cmd.split(" && ")
        # every cp, not just the first: the ones after an && used to
        # inherit nothing at all
        assert cmd.count("sudo cp ") == 2
        assert not any(part.startswith("cp ") for part in parts)
        # and the force names cp, so mv is left as use_sudo=False has it
        assert any(part.startswith("mv -fT ") for part in parts)

    def test_a_skipped_rename_is_not_sudo_prefixed_either(self, tmp_path: Path):
        sm = SnapshotManager({"use_sudo": True, "sudo_skip_commands": ["mv"]})
        overlay = tmp_path / "o.qcow2"          # deleted by the revert
        backup = tmp_path / "o.qcow2.preserve"
        backup.write_bytes(b"x")
        with patch.object(sm.virsh, "execute_shell",
                          return_value=_result()) as shell:
            sm._restore_preserved_overlays([(str(overlay), str(backup))])
        assert shell.call_args.args[0].startswith("mv -fT ")


class TestSnapshotRestore:
    """End-to-end wiring for snapshot_restore (regression: 057eb7d)."""

    def test_success_calls_preserve_then_revert_then_restore(
        self, sm: SnapshotManager
    ):
        preserved_pairs = [("/overlays/a", "/overlays/a.preserve")]
        with _not_running(sm), \
             patch.object(sm, "_preserve_snapshot_overlays",
                          return_value=preserved_pairs) as preserve, \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute", return_value=_result()) as execute, \
             patch.object(sm, "_restore_preserved_overlays",
                          return_value=[]) as restore:
            assert sm.snapshot_restore("vm01", "snap1") is True

        preserve.assert_called_once_with("vm01", "snap1")
        assert [call.args[0] for call in execute.call_args_list
                if call.args] == ["snapshot-revert"]
        restore.assert_called_once_with(preserved_pairs)

    def test_restore_called_even_when_revert_fails(self, sm: SnapshotManager):
        with _not_running(sm), \
             patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="boom")), \
             patch.object(sm, "_restore_preserved_overlays",
                          return_value=[]) as restore:
            assert sm.snapshot_restore("vm01", "snap1") is False
        restore.assert_called_once()

    def test_retries_on_write_lock_contention(self, sm: SnapshotManager):
        results = [
            _result(ok=False, stderr="unable to acquire write lock"),
            _result(ok=False, stderr="unable to acquire write lock"),
            _result(ok=True),
        ]
        with _not_running(sm), \
             patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute", side_effect=results), \
             patch.object(sm, "_restore_preserved_overlays",
                          return_value=[]), \
             patch("boxman.providers.libvirt.snapshot.time.sleep"):
            assert sm.snapshot_restore("vm01", "snap1") is True

    def test_does_not_retry_on_non_lock_error(self, sm: SnapshotManager):
        with _not_running(sm), \
             patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="bad name")) as execute, \
             patch.object(sm, "_restore_preserved_overlays",
                          return_value=[]), \
             patch("boxman.providers.libvirt.snapshot.time.sleep"):
            assert sm.snapshot_restore("vm01", "snap1") is False
        assert execute.call_count == 1  # no retries on non-lock errors

    def test_exception_still_calls_restore(self, sm: SnapshotManager):
        with _not_running(sm), \
             patch.object(sm, "_preserve_snapshot_overlays", return_value=[("a", "b")]), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute", side_effect=RuntimeError("x")), \
             patch.object(sm, "_restore_preserved_overlays",
                          return_value=[]) as restore:
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
        with _not_running(sm), \
             patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm, "_restore_preserved_overlays",
                          return_value=[]), \
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
        with _not_running(sm), \
             patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm, "_restore_preserved_overlays",
                          return_value=[]), \
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
        # snapshot-list lists no names; the chain is built from it, not
        # from list_snapshots, so patching that reached the real virsh
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout="")) as execute:
            assert sm._chain_order("vm01") == []
        execute.assert_called_once_with(
            "snapshot-list", "vm01", "--name", warn=True)


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
            if cmd.startswith("stat "):
                # the identity check before the rebase reads the real files
                return _shell_that_runs()(cmd)
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


needs_qemu = pytest.mark.skipif(
    not (shutil.which("qemu-img") and shutil.which("qemu-io")),
    reason="needs qemu-img and qemu-io to build real overlay chains")


def _qemu(*args: str) -> str:
    return subprocess.run(list(args), check=True, capture_output=True,
                          text=True).stdout


def _pattern(era: int) -> str:
    """The byte the guest writes during *era* (0: before any snapshot)."""
    return f"0x{0xb0 + era:x}"


def _write(image: Path, era: int) -> None:
    _qemu("qemu-io", "-c", f"write -P {_pattern(era)} {era * 64}k 64k",
          str(image))


def _reads(image: Path, era: int, pattern: str | None = None) -> bool:
    """Whether reading *image* returns what *era* wrote (or *pattern*)."""
    proc = subprocess.run(
        ["qemu-io", "-r", "-c",
         f"read -P {pattern or _pattern(era)} {era * 64}k 64k", str(image)],
        capture_output=True, text=True)
    return (proc.returncode == 0
            and "Pattern verification failed" not in proc.stdout)


def _chain(image: Path) -> list[str]:
    """The files qemu opens for *image*, top first."""
    out = _qemu("qemu-img", "info", "-U", "--backing-chain",
                "--output=json", str(image))
    return [entry["filename"] for entry in json.loads(out)]


def _take_external(tmp_path: Path, names: list[str], real: bool = True):
    """Lay out what ``snapshot-create-as --memspec`` leaves, one take per name.

    libvirt records, per disk, the *new* overlay a take creates, and that
    overlay becomes the domain's active disk -- so after the last take the
    newest snapshot's recorded overlay is the head. With *real*, each
    overlay is a qcow2 on the one before and the guest writes era *i*'s
    pattern into it while it is the head; otherwise the files are plain.

    Returns ``(base, overlays, memory)``, the last two keyed by name.
    """
    base = tmp_path / "vm01_disk.qcow2"
    if real:
        _qemu("qemu-img", "create", "-q", "-f", "qcow2", str(base), "1M")
        _write(base, 0)
    else:
        base.write_bytes(b"base")
    overlays: dict[str, Path] = {}
    memory: dict[str, Path] = {}
    backing = base
    for era, name in enumerate(names, start=1):
        overlay = tmp_path / f"vm01_disk.{name}"
        if real:
            _qemu("qemu-img", "create", "-q", "-f", "qcow2",
                  "-b", str(backing), "-F", "qcow2", str(overlay))
            _write(overlay, era)
        else:
            overlay.write_bytes(name.encode())
        memory[name] = tmp_path / f"vm01_snapshot_{name}.raw"
        memory[name].write_bytes(b"memory image")
        overlays[name] = overlay
        backing = overlay
    return base, overlays, memory


class _Libvirt:
    """A ``virsh.execute`` double for a domain with external snapshots.

    Answers the calls :meth:`SnapshotManager.collapse_to` and
    :meth:`SnapshotManager.delete_snapshot` make, shaped like libvirt's own
    output: ``snapshot-dumpxml`` records each snapshot's overlay under
    ``<disks>`` (next to a cdrom left out with ``snapshot='no'``) and the
    disk as it was when the snapshot was taken under ``<domain>``;
    ``domblklist --details`` names *heads*; a plain ``snapshot-delete``
    refuses an external snapshot, and ``--metadata`` forgets it.

    *records* maps snapshot -> {disk target: recorded overlay}; *parents*
    defaults to a linear chain in the order given.
    """

    def __init__(self, records: dict, heads: dict, memory: dict,
                 parents: dict | None = None):
        self.records = records
        self.heads = heads
        self.memory = memory
        names = list(records)
        self.parents = parents or {
            n: (names[i - 1] if i else None) for i, n in enumerate(names)}
        self.snapshots = names

    def _snapshot_xml(self, name: str) -> str:
        parent = self.parents[name]
        disks = "".join(
            f"<disk name='{target}' snapshot='external' type='file'>"
            f"<driver type='qcow2'/><source file='{path}'/></disk>"
            for target, path in self.records[name].items())
        taken_on = "".join(
            f"<disk type='file' device='disk'>"
            f"<source file='{self.records[parent][t] if parent else 'base'}'/>"
            f"<target dev='{t}' bus='virtio'/></disk>"
            for t in self.records[name])
        return (f"<domainsnapshot><name>{name}</name>"
                f"<state>running</state>"
                + (f"<parent><name>{parent}</name></parent>" if parent else "")
                + f"<creationTime>1785187648</creationTime>"
                f"<memory snapshot='external' file='{self.memory[name]}'/>"
                f"<disks>{disks}<disk name='sda' snapshot='no'/></disks>"
                f"<domain type='kvm'><name>vm01</name><devices>{taken_on}"
                f"</devices></domain></domainsnapshot>")

    def __call__(self, *args, **_kw):
        verb = args[0]
        if verb == "snapshot-info":
            if args[2] in self.snapshots:
                return _result()
            return _result(ok=False, stderr="error: Domain snapshot not found")
        if verb == "snapshot-list":
            return _result(stdout="".join(f"{n}\n" for n in self.snapshots)
                           + "\n")
        if verb == "snapshot-dumpxml":
            if args[2] not in self.snapshots:
                return _result(ok=False, stderr="error: Domain snapshot not found")
            return _result(stdout=self._snapshot_xml(args[2]))
        if verb == "domblklist":
            rows = "".join(f" file   disk     {t}      {p}\n"
                           for t, p in self.heads.items())
            return _result(stdout=(
                " Type   Device   Target   Source\n"
                "------------------------------------------------\n"
                + rows + " file   cdrom    sda      /p/seed.iso\n"))
        if verb == "dumpxml":
            disks = "".join(
                f"<disk type='file' device='disk'><source file='{p}'/>"
                f"<backingStore type='file'><source file='/stale'/>"
                f"</backingStore><target dev='{t}'/></disk>"
                for t, p in self.heads.items())
            return _result(stdout=f"<domain><devices>{disks}</devices></domain>")
        if verb == "define":
            return _result()
        if verb == "snapshot-delete":
            if "--metadata" in args:
                self.snapshots.remove(args[2])
                return _result()
            return _result(ok=False, stderr=(
                "error: unsupported configuration: deletion of 1 external "
                "disk snapshots not supported yet"))
        raise AssertionError(f"unexpected virsh call: {args}")


class TestCollapseKeepsTheLiveDisk:
    """collapse_to must not remove the disk the domain runs on.

    An external snapshot records the overlay it created, and that overlay
    becomes the active disk: after ``take s1; take s2`` the head *is* s2's
    recorded overlay. Removing every dropped snapshot's recorded overlay
    therefore removed the live disk -- after ``collapse --to s1``, and after
    ``snapshot delete`` of the newest snapshot, which collapses to its
    parent.
    """

    @needs_qemu
    def test_two_snapshots_keep_the_head(self, sm, tmp_path):
        base, overlays, memory = _take_external(tmp_path, ["s1", "s2"])
        head = overlays["s2"]
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(head)}, memory)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            assert sm.collapse_to("vm01", "s1") is True

        assert head.is_file(), "the live disk was removed"
        assert _chain(head) == [str(head), str(overlays["s1"]), str(base)]
        assert all(_reads(head, era) for era in (0, 1, 2))
        # the rest of the drop still happens
        assert not memory["s2"].exists()
        assert memory["s1"].exists()
        assert virsh.snapshots == ["s1"]

    @needs_qemu
    def test_three_snapshots_keep_the_head_the_middle_layer_goes(
            self, sm, tmp_path):
        base, overlays, memory = _take_external(tmp_path, ["s1", "s2", "s3"])
        head = overlays["s3"]
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(head)}, memory)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            assert sm.collapse_to("vm01", "s1") is True

        assert head.is_file(), "the live disk was removed"
        # rebased past s2, with s2's writes merged into the head
        assert _chain(head) == [str(head), str(overlays["s1"]), str(base)]
        assert all(_reads(head, era) for era in (0, 1, 2, 3))
        assert not overlays["s2"].exists()
        assert not memory["s2"].exists() and not memory["s3"].exists()
        assert overlays["s1"].exists() and memory["s1"].exists()
        assert virsh.snapshots == ["s1"]

    @needs_qemu
    def test_deleting_the_newest_snapshot_keeps_the_head(self, sm, tmp_path):
        base, overlays, memory = _take_external(tmp_path, ["s1", "s2", "s3"])
        head = overlays["s3"]
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(head)}, memory)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            assert sm.delete_snapshot("vm01", "s3") is True

        assert head.is_file(), "the live disk was removed"
        assert _chain(head) == [str(head), str(overlays["s2"]),
                                str(overlays["s1"]), str(base)]
        assert all(_reads(head, era) for era in (0, 1, 2, 3))
        assert not memory["s3"].exists()
        assert virsh.snapshots == ["s1", "s2"]

    @needs_qemu
    def test_after_a_revert_the_dropped_overlay_goes_the_head_stays(
            self, sm, tmp_path):
        # take s1, take s2, revert to s1: s1 has a child, so libvirt puts
        # the domain on a fresh overlay of the disk s1 was taken from and
        # leaves s1's record alone. s2's overlay is then not live.
        base, overlays, memory = _take_external(tmp_path, ["s1", "s2"])
        head = tmp_path / "vm01_disk.1785187700"
        _qemu("qemu-img", "create", "-q", "-f", "qcow2",
              "-b", str(base), "-F", "qcow2", str(head))
        _write(head, 5)
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(head)}, memory)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            assert sm.collapse_to("vm01", "s1") is True

        assert head.is_file()
        assert not overlays["s2"].exists()
        assert _chain(head) == [str(head), str(overlays["s1"]), str(base)]
        # what the guest sees is still the reverted state: the writes made
        # after s1 was taken stay undone, the ones after the revert stay
        assert _reads(head, 0) and _reads(head, 5)
        assert _reads(head, 1, pattern="0") and _reads(head, 2, pattern="0")

    @needs_qemu
    def test_a_head_that_is_the_targets_own_overlay_is_not_rebased(
            self, sm, tmp_path):
        # take s1, s2, s4; revert to s1; take s3. The chain order puts s4,
        # on the abandoned branch, after s3 -- and s3's overlay is the head.
        # Rebasing a file onto itself either trips qemu's image lock or,
        # without one, writes a backing chain that loops.
        base, overlays, memory = _take_external(tmp_path, ["s1", "s2", "s4"])
        reverted = tmp_path / "vm01_disk.1785187700"
        _qemu("qemu-img", "create", "-q", "-f", "qcow2",
              "-b", str(base), "-F", "qcow2", str(reverted))
        head = tmp_path / "vm01_disk.s3"
        _qemu("qemu-img", "create", "-q", "-f", "qcow2",
              "-b", str(reverted), "-F", "qcow2", str(head))
        _write(head, 6)
        memory["s3"] = tmp_path / "vm01_snapshot_s3.raw"
        memory["s3"].write_bytes(b"memory image")
        records = {n: {"vda": str(overlays[n])} for n in ("s1", "s2", "s4")}
        records["s3"] = {"vda": str(head)}
        virsh = _Libvirt(records, {"vda": str(head)}, memory,
                         parents={"s1": None, "s2": "s1", "s4": "s2",
                                  "s3": "s1"})
        calls: list[str] = []

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs(calls)):
            assert sm._chain_order("vm01") == ["s1", "s2", "s3", "s4"]
            assert sm.collapse_to("vm01", "s3") is True

        assert not any("qemu-img rebase" in c for c in calls)
        assert head.is_file()
        assert _chain(head) == [str(head), str(reverted), str(base)]
        assert _reads(head, 0) and _reads(head, 6)
        assert not overlays["s4"].exists() and not memory["s4"].exists()
        assert overlays["s2"].exists() and overlays["s1"].exists()
        assert virsh.snapshots == ["s1", "s2", "s3"]

    def test_a_head_that_is_another_file_where_qemu_img_runs_is_rebased(
            self, sm, tmp_path):
        # one file here need not be one file where qemu-img runs (a
        # container with mounts of its own): what stat says there decides
        _base, overlays, memory = _take_external(
            tmp_path, ["s1", "s2"], real=False)
        (tmp_path / "other").mkdir()
        head = tmp_path / "other" / "vm01_disk.s1"
        os.link(overlays["s1"], head)
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(head)}, memory)
        runs = _shell_that_runs()

        def shell(cmd, *args, **kwargs):
            if cmd.startswith("stat "):
                return _result(stdout="64768 11\n64768 12\n")
            return runs(cmd, *args, **kwargs)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell", side_effect=shell), \
             patch.object(sm, "_qemu_img_rebase",
                          return_value=True) as rebase:
            assert sm.collapse_to("vm01", "s1") is True

        rebase.assert_called_once_with(str(head), str(overlays["s1"]))

    @needs_qemu
    @pytest.mark.parametrize("alias", ["symlink", "hardlink"])
    def test_the_targets_own_overlay_named_otherwise_is_not_rebased(
            self, sm, tmp_path, alias):
        # as test_a_head_that_is_the_targets_own_overlay_is_not_rebased,
        # with the head reported under another spelling: rebased onto
        # itself it trips qemu's image lock, or without one loops
        base, overlays, memory = _take_external(tmp_path, ["s1", "s2", "s4"])
        reverted = tmp_path / "vm01_disk.1785187700"
        _qemu("qemu-img", "create", "-q", "-f", "qcow2",
              "-b", str(base), "-F", "qcow2", str(reverted))
        s3 = tmp_path / "vm01_disk.s3"
        _qemu("qemu-img", "create", "-q", "-f", "qcow2",
              "-b", str(reverted), "-F", "qcow2", str(s3))
        _write(s3, 6)
        other = tmp_path / "other"
        if alias == "symlink":
            other.symlink_to(tmp_path)
        else:
            other.mkdir()
            os.link(s3, other / s3.name)
        head = other / s3.name
        memory["s3"] = tmp_path / "vm01_snapshot_s3.raw"
        memory["s3"].write_bytes(b"memory image")
        records = {n: {"vda": str(overlays[n])} for n in ("s1", "s2", "s4")}
        records["s3"] = {"vda": str(s3)}
        virsh = _Libvirt(records, {"vda": str(head)}, memory,
                         parents={"s1": None, "s2": "s1", "s4": "s2",
                                  "s3": "s1"})
        calls: list[str] = []

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs(calls)):
            assert sm.collapse_to("vm01", "s3") is True

        assert not any("qemu-img rebase" in c for c in calls)
        assert _chain(s3) == [str(s3), str(reverted), str(base)]
        assert _reads(s3, 0) and _reads(s3, 6)
        assert not overlays["s4"].exists()
        assert virsh.snapshots == ["s1", "s2", "s3"]

    @pytest.mark.parametrize("output", ["64768 11\n", "64768 11\nx y\n",
                                        "64768 11 7\n64768 11 7\n"],
                             ids=["one line", "not numbers", "three fields"])
    def test_an_identity_check_that_says_something_else_tells_nothing(
            self, sm, output):
        with patch.object(sm.virsh, "execute_shell",
                          return_value=_result(stdout=output)):
            assert sm._one_file_where_qemu_runs("/a/x", "/b/y") is None

    def test_every_disk_is_told_before_any_is_rebased(self, sm, tmp_path):
        # the second disk cannot be told: the first is not rebased either
        _base, overlays, memory = _take_external(
            tmp_path, ["s1", "s2"], real=False)
        data = {n: tmp_path / f"vm01_disk01.{n}" for n in ("s1", "s2")}
        for p in data.values():
            p.write_bytes(b"data")
        head = tmp_path / "vm01_disk.1785187700"
        head.write_bytes(b"head")
        virsh = _Libvirt(
            {n: {"vda": str(overlays[n]), "vdb": str(data[n])}
             for n in ("s1", "s2")},
            {"vda": str(head), "vdb": str(data["s2"])}, memory)
        runs = _shell_that_runs()

        def shell(cmd, *args, **kwargs):
            if cmd.startswith("stat ") and str(data["s2"]) in cmd:
                return _result(ok=False, stderr="stat: Permission denied")
            return runs(cmd, *args, **kwargs)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell", side_effect=shell), \
             patch.object(sm, "_qemu_img_rebase",
                          return_value=True) as rebase:
            assert sm.collapse_to("vm01", "s1") is False

        rebase.assert_not_called()
        assert all(p.exists() for p in [*overlays.values(), *data.values()])
        assert virsh.snapshots == ["s1", "s2"]

    @pytest.mark.parametrize("skip, sudo", [([], True), (["stat"], False)],
                             ids=["sudo", "stat skipped"])
    def test_the_identity_check_takes_sudo_as_configured(self, skip, sudo):
        # the force and skip lists decide it, as for every command, not a
        # hand-written prefix
        sm = SnapshotManager(provider_config={
            "use_sudo": True, "sudo_skip_commands": skip,
            "uri": "qemu:///system"})
        run: list[str] = []

        def shell_run(command, **_kwargs):
            run.append(command)
            return _result(stdout="64768 11\n64768 12\n")

        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=shell_run):
            assert sm._one_file_where_qemu_runs("/a/x", "/b/y") is False

        assert len(run) == 1 and "stat -L" in run[0]
        assert run[0].startswith("sudo ") is sudo

    def test_one_that_cannot_be_examined_where_qemu_img_runs_stops(
            self, sm, tmp_path):
        _base, overlays, memory = _take_external(
            tmp_path, ["s1", "s2"], real=False)
        head = tmp_path / "vm01_disk.1785187700"
        head.write_bytes(b"head")
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(head)}, memory)
        runs = _shell_that_runs()

        def shell(cmd, *args, **kwargs):
            if cmd.startswith("stat "):
                return _result(ok=False, stderr="stat: Permission denied")
            return runs(cmd, *args, **kwargs)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell", side_effect=shell), \
             patch.object(sm, "_qemu_img_rebase",
                          return_value=True) as rebase:
            assert sm.collapse_to("vm01", "s1") is False

        rebase.assert_not_called()
        assert all(p.exists() for p in overlays.values())
        assert memory["s2"].exists()
        assert virsh.snapshots == ["s1", "s2"]

    @needs_qemu
    def test_a_target_overlay_that_is_not_there_stops_the_collapse(
            self, sm, tmp_path):
        # s1 records its overlay through a directory that is not there:
        # as text the path folds to the head, but nothing is at it
        base, overlays, memory = _take_external(tmp_path, ["s1", "s2"])
        head = overlays["s1"]
        virsh = _Libvirt(
            {"s1": {"vda": str(tmp_path / "absent" / ".." / head.name)},
             "s2": {"vda": str(overlays["s2"])}},
            {"vda": str(head)}, memory)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            assert sm.collapse_to("vm01", "s1") is False

        assert _chain(head) == [str(head), str(base)]
        assert all(p.exists() for p in overlays.values())
        assert memory["s2"].exists()
        assert virsh.snapshots == ["s1", "s2"]

    def test_dry_run_reports_no_rebase_of_the_targets_own_overlay(
            self, sm, tmp_path):
        _base, overlays, memory = _take_external(
            tmp_path, ["s1", "s2"], real=False)
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(overlays["s1"])}, memory)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.logger, "info") as info:
            assert sm.collapse_to("vm01", "s1", dry_run=True) is True

        lines = [str(c.args[0]) for c in info.call_args_list]
        assert not any("would rebase" in line for line in lines)
        assert any("nothing to rebase" in line for line in lines)

    @pytest.mark.parametrize("alias", ["symlink", "hardlink"])
    def test_the_head_is_recognised_under_another_spelling(
            self, sm, tmp_path, alias):
        # domblklist and the snapshot metadata need not spell the path the
        # same way: a symlinked image directory, a bind mount (stood in for
        # by a hard link, which no path resolution sees through either)
        _base, overlays, memory = _take_external(
            tmp_path, ["s1", "s2"], real=False)
        other = tmp_path / "other"
        if alias == "symlink":
            other.symlink_to(tmp_path)
        else:
            other.mkdir()
            os.link(overlays["s2"], other / "vm01_disk.s2")
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(other / "vm01_disk.s2")}, memory)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()), \
             patch.object(sm, "_qemu_img_rebase", return_value=True):
            assert sm.collapse_to("vm01", "s1") is True

        assert overlays["s2"].is_file(), "the live disk was removed"
        assert not memory["s2"].exists()

    def test_the_overlay_the_head_is_rebased_onto_is_kept(
            self, sm, tmp_path):
        # metadata in which a dropped snapshot names the target's overlay:
        # after the rebase that file backs the head, and the kept target
        # still records it
        _base, overlays, memory = _take_external(
            tmp_path, ["s1", "s2"], real=False)
        head = tmp_path / "vm01_disk.1785187700"
        head.write_bytes(b"head")
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path)
        virsh = _Libvirt({"s1": {"vda": str(overlays["s1"])},
                          "s2": {"vda": str(alias / "vm01_disk.s1")}},
                         {"vda": str(head)}, memory)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()), \
             patch.object(sm, "_qemu_img_rebase", return_value=True):
            assert sm.collapse_to("vm01", "s1") is True

        assert overlays["s1"].is_file()
        assert head.is_file()

    def test_a_head_that_cannot_be_examined_keeps_every_overlay(
            self, sm, tmp_path):
        # the head is s2's overlay under a spelling that cannot be stat'ed,
        # so nothing can show that s2's recorded overlay is not that file
        _base, overlays, memory = _take_external(
            tmp_path, ["s1", "s2"], real=False)
        other = tmp_path / "other"
        other.mkdir()
        hidden = other / "vm01_disk.s2"
        os.link(overlays["s2"], hidden)
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(hidden)}, memory)
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if os.fspath(path) == str(hidden):
                raise PermissionError(13, "Permission denied", str(hidden))
            return real_stat(path, *args, **kwargs)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()), \
             patch.object(sm, "_qemu_img_rebase", return_value=True), \
             patch.object(sm.logger, "warning") as warning, \
             patch("os.stat", side_effect=stat):
            assert sm.collapse_to("vm01", "s1") is True

        assert overlays["s2"].is_file(), "the live disk was removed"
        warned = " ".join(str(c.args[0]) for c in warning.call_args_list)
        assert str(overlays["s2"]) in warned
        # the snapshot itself is still dropped
        assert not memory["s2"].exists()
        assert virsh.snapshots == ["s1"]

    @pytest.mark.parametrize("error", [
        PermissionError(13, "Permission denied"),
        # in a container a path can be there that boxman, on the host,
        # does not see -- and the rm runs in the container
        FileNotFoundError(2, "No such file or directory"),
    ], ids=["denied", "missing"])
    def test_a_kept_overlay_that_cannot_be_examined_keeps_every_overlay(
            self, sm, tmp_path, error):
        # kept s1 records its overlay under a spelling that cannot be
        # stat'ed, and the dropped s2 records that file under one that can:
        # nothing shows that s2's is not the file the head stands on
        _base, overlays, memory = _take_external(
            tmp_path, ["s1", "s2"], real=False)
        head = tmp_path / "vm01_disk.1785187700"
        head.write_bytes(b"head")
        other = tmp_path / "other"
        other.mkdir()
        hidden = other / "vm01_disk.s1"
        os.link(overlays["s1"], hidden)
        virsh = _Libvirt({"s1": {"vda": str(hidden)},
                          "s2": {"vda": str(overlays["s1"])}},
                         {"vda": str(head)}, memory)
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if os.fspath(path) == str(hidden):
                raise error
            return real_stat(path, *args, **kwargs)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()), \
             patch.object(sm, "_qemu_img_rebase", return_value=True), \
             patch.object(sm.logger, "warning") as warning, \
             patch("os.stat", side_effect=stat):
            assert sm.collapse_to("vm01", "s1") is True

        assert overlays["s1"].is_file(), "what the head stands on was removed"
        warned = " ".join(str(c.args[0]) for c in warning.call_args_list)
        assert str(overlays["s1"]) in warned
        assert not memory["s2"].exists()
        assert virsh.snapshots == ["s1"]

    def test_dry_run_reports_the_head_as_kept(self, sm, tmp_path):
        _base, overlays, memory = _take_external(
            tmp_path, ["s1", "s2", "s3"], real=False)
        head = overlays["s3"]
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(head)}, memory)
        calls: list[str] = []

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs(calls)), \
             patch.object(sm.logger, "info") as info:
            assert sm.collapse_to("vm01", "s1", dry_run=True) is True

        # nothing but the read-only identity check
        assert all(c.startswith("stat ") for c in calls)
        assert all(p.exists() for p in overlays.values())
        lines = [str(c.args[0]) for c in info.call_args_list]
        assert any("would remove" in line and str(overlays["s2"]) in line
                   for line in lines)
        assert any("would keep" in line and str(head) in line
                   for line in lines)
        assert not any("would remove" in line and str(head) in line
                       for line in lines)
        assert virsh.snapshots == ["s1", "s2", "s3"]


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


class TestWouldBeOverlayPaths:
    """`snapshot take --force` predicts the overlay libvirt would create."""

    def test_derives_from_current_source(self, sm: SnapshotManager):
        # libvirt drops the source's final suffix and appends .<snapshot>
        with patch.object(sm, "_data_disk_targets",
                          return_value=[("vda", "/ws/vm01.qcow2"),
                                        ("vdb", "/ws/vm01-data.1784942899")]), \
             patch.object(sm, "_snapshot_excluded_targets",
                          return_value=set()):
            paths = sm._would_be_overlay_paths("vm01", "snapX")
        assert paths == ["/ws/vm01.snapX", "/ws/vm01-data.snapX"]

    def test_skips_placeholder_source(self, sm: SnapshotManager):
        with patch.object(sm, "_data_disk_targets", return_value=[("vda", "-")]), \
             patch.object(sm, "_snapshot_excluded_targets",
                          return_value=set()):
            assert sm._would_be_overlay_paths("vm01", "snapX") == []


class TestForceClearForRetake:
    """Clearing a name for re-take: orphan files vs. a live snapshot."""

    @staticmethod
    def _touch(p: Path) -> str:
        p.write_bytes(b"x")
        return str(p)

    def test_orphan_files_removed_active_kept(self, sm: SnapshotManager,
                                              tmp_path: Path):
        # Reproduces the bug: the snapshot name is gone from libvirt but its
        # per-disk overlay + memory files linger and collide on re-take.
        active = self._touch(tmp_path / "vm01.qcow2")          # live source
        orphan_overlay = self._touch(tmp_path / "vm01.snapX")  # <stem>.snapX
        mem = self._touch(tmp_path / "vm01_snapshot_snapX.raw")
        mem_zst = self._touch(tmp_path / "vm01_snapshot_snapX.raw.zst")

        removed_cmds = []

        def fake_shell(cmd, *_a, **_k):
            removed_cmds.append(cmd)
            return _result()

        absent = _result(
            ok=False,
            stderr="Domain snapshot not found: no domain snapshot with "
                   "matching name 'snapX'")
        with patch.object(sm.virsh, "execute", return_value=absent), \
             patch.object(sm, "_data_disk_targets",
                          return_value=[("vda", active)]), \
             patch.object(sm, "_snapshot_excluded_targets",
                          return_value=set()), \
             patch.object(sm.virsh, "execute_shell", side_effect=fake_shell):
            assert sm._force_clear_for_retake(
                "vm01", str(tmp_path), "snapX",
                paths_in_use=MagicMock(return_value={}),
                recorded_in_use=MagicMock(return_value={})) is True

        joined = "\n".join(removed_cmds)
        assert orphan_overlay in joined
        assert mem in joined
        assert mem_zst in joined
        # the live source is never a removal target
        assert active not in joined

    def test_ambiguous_snapshot_info_failure_aborts(self, sm: SnapshotManager,
                                                    tmp_path: Path):
        # A transient snapshot-info failure (not "snapshot absent") must NOT
        # lead to removing candidate files — the snapshot might really exist.
        orphan = self._touch(tmp_path / "vm01.snapX")
        transient = _result(
            ok=False, stderr="error: failed to connect to the hypervisor")
        with patch.object(sm.virsh, "execute", return_value=transient), \
             patch.object(sm, "_data_disk_targets",
                          return_value=[("vda", str(tmp_path / "vm01.qcow2"))]), \
             patch.object(sm.virsh, "execute_shell") as esh:
            assert sm._force_clear_for_retake(
                "vm01", str(tmp_path), "snapX") is False
        esh.assert_not_called()          # no rm attempted
        assert os.path.isfile(orphan)    # file left intact

    def test_live_snapshot_deleted_first(self, sm: SnapshotManager,
                                         tmp_path: Path):
        with patch.object(sm.virsh, "execute", return_value=_result(ok=True)), \
             patch.object(sm, "delete_snapshot", return_value=True) as dele, \
             patch.object(sm, "_data_disk_targets", return_value=[]), \
             patch.object(sm.virsh, "execute_shell", return_value=_result()):
            assert sm._force_clear_for_retake(
                "vm01", str(tmp_path), "snapX") is True
        dele.assert_called_once_with("vm01", "snapX")

    def test_live_snapshot_delete_failure_aborts(self, sm: SnapshotManager,
                                                 tmp_path: Path):
        with patch.object(sm.virsh, "execute", return_value=_result(ok=True)), \
             patch.object(sm, "delete_snapshot", return_value=False), \
             patch.object(sm, "_data_disk_targets", return_value=[]):
            assert sm._force_clear_for_retake(
                "vm01", str(tmp_path), "snapX") is False


class TestForceClearProtectsFilesInUse:
    """A leftover is removed only once no domain is known to use it — by
    path or by identity, the VM's own disks and every backing layer
    included (``LibVirtSession.disk_paths_in_use``). A memory_snapshot_dir
    outside the workdir can reach a disk under another name: a symlink to
    the directory a live disk is in, say. Comparing names missed that, and
    ``rm -f`` deleted the disk."""

    _ABSENT = _result(
        ok=False, stderr="Domain snapshot not found: no domain snapshot "
                         "with matching name 'snapX'")

    @staticmethod
    def _touch(p: Path) -> str:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return str(p)

    def _clear(self, sm, vm_dir, scan, active=(), memory=None):
        memory = memory or MagicMock(return_value={})
        rm = []
        with patch.object(sm.virsh, "execute", return_value=self._ABSENT), \
             patch.object(sm, "_data_disk_targets", return_value=[
                 (f"vd{chr(97 + i)}", source)
                 for i, source in enumerate(active)]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=lambda cmd, *_a, **_k:
                          rm.append(cmd) or _result()), \
             patch.object(sm, "logger") as logger:
            ok = sm._force_clear_for_retake("vm01", str(vm_dir), "snapX",
                                            paths_in_use=scan,
                                            recorded_in_use=memory)
        errors = " ".join(str(c.args[0]) for c in logger.error.call_args_list)
        return ok, rm, errors

    def test_a_live_disk_reached_through_an_alias_of_the_directory_is_kept(
            self, sm: SnapshotManager, tmp_path: Path):
        live = self._touch(tmp_path / "shared" / "vm01_snapshot_snapX.raw")
        os.symlink(tmp_path / "shared", tmp_path / "snaps")
        scan = MagicMock(return_value=FilesInUse(
            {os.path.realpath(live): "vm01"}))

        ok, rm, errors = self._clear(sm, tmp_path / "snaps", scan,
                                     active=[live])

        assert ok is False
        assert rm == []
        assert os.path.isfile(live)
        assert "domain vm01 uses it" in errors

    def test_a_leftover_another_domain_uses_is_kept(
            self, sm: SnapshotManager, tmp_path: Path):
        mem = self._touch(tmp_path / "vm01_snapshot_snapX.raw")
        scan = MagicMock(return_value=FilesInUse(
            {os.path.realpath(mem): "vm02"}))

        ok, rm, errors = self._clear(sm, tmp_path, scan)

        assert ok is False and rm == [] and os.path.isfile(mem)
        assert f"{mem}: domain vm02 uses it" in errors

    def test_one_used_under_another_name_of_its_file_is_kept(
            self, sm: SnapshotManager, tmp_path: Path):
        mem = self._touch(tmp_path / "vm01_snapshot_snapX.raw")
        other = tmp_path / "elsewhere" / "disk.raw"
        other.parent.mkdir()
        os.link(mem, other)
        used = FilesInUse({str(other): "vm02"})
        st = os.stat(other)
        used.identities[(st.st_dev, st.st_ino)] = "vm02"

        ok, rm, _errors = self._clear(sm, tmp_path,
                                      MagicMock(return_value=used))

        assert ok is False and rm == [] and os.path.isfile(mem)

    def test_a_scan_that_fails_keeps_everything(
            self, sm: SnapshotManager, tmp_path: Path):
        mem = self._touch(tmp_path / "vm01_snapshot_snapX.raw")

        ok, rm, errors = self._clear(sm, tmp_path,
                                     MagicMock(return_value=None))

        assert ok is False and rm == [] and os.path.isfile(mem)
        assert "could not check whether a domain uses" in errors

    def test_with_nothing_left_over_nothing_is_asked(
            self, sm: SnapshotManager, tmp_path: Path):
        scan, memory = MagicMock(return_value={}), MagicMock(return_value={})

        ok, rm, _errors = self._clear(sm, tmp_path, scan, memory=memory)

        assert ok is True and rm == []
        scan.assert_not_called()
        memory.assert_not_called()

    def test_a_leftover_another_domains_snapshot_records_is_kept(
            self, sm: SnapshotManager, tmp_path: Path):
        """vm01's snapshot ``replica_snapshot_snapX`` would write where
        vm01_snapshot_replica's ``snapX`` keeps its RAM: no domain uses that
        file as a disk, but a snapshot records it."""
        mem = self._touch(tmp_path / "vm01_snapshot_snapX.raw")
        memory = MagicMock(return_value=FilesInUse(
            {os.path.realpath(mem): "vm01_snapshot_replica"}))

        ok, rm, errors = self._clear(sm, tmp_path,
                                     MagicMock(return_value={}),
                                     memory=memory)

        assert ok is False and rm == [] and os.path.isfile(mem)
        assert (f"{mem}: a snapshot of domain vm01_snapshot_replica records "
                f"it") in errors

    def test_other_snapshots_that_cannot_be_read_keep_everything(
            self, sm: SnapshotManager, tmp_path: Path):
        mem = self._touch(tmp_path / "vm01_snapshot_snapX.raw")

        ok, rm, errors = self._clear(sm, tmp_path,
                                     MagicMock(return_value={}),
                                     memory=MagicMock(return_value=None))

        assert ok is False and rm == [] and os.path.isfile(mem)
        assert "could not check whether a snapshot of another domain" in errors

    def test_the_session_hands_it_its_scan(self, tmp_path: Path):
        session = LibVirtSession(
            config={'provider': {'libvirt': {'virsh_cmd': 'false'}}})
        with patch.object(SnapshotManager, "create_snapshot",
                          return_value=True) as create:
            session.snapshot_take(vm_name="vm01", vm_dir=str(tmp_path),
                                  snapshot_name="snapX", description="",
                                  force=True)

        assert create.call_args.kwargs["paths_in_use"] == (
            session.disk_paths_in_use)
        assert create.call_args.kwargs["recorded_in_use"] == (
            session.snapshot_files_in_use)


class TestTakeWritesOverNoFile:
    """The memory image goes where no file is: one already there is a
    leftover, or another VM's image — VM ``vm01``'s snapshot
    ``replica_snapshot_s1`` is named as ``s1`` of ``vm01_snapshot_replica``
    is — and the take would write the guest's RAM over it. ``--force``
    clears a leftover no domain uses first; what it leaves is protected."""

    @staticmethod
    def _take(sm, tmp_path, force=False):
        with patch.object(sm, "_force_clear_for_retake", return_value=True), \
             patch.object(sm, "_flatten_cdrom_overlays"), \
             patch.object(sm, "_cdrom_diskspec_args", return_value=[]), \
             patch.object(sm.virsh, "execute",
                          return_value=_result()) as execute, \
             patch.object(sm, "logger") as logger:
            ok = sm.create_snapshot("vm01", str(tmp_path), "s1", "d",
                                    force=force)
        errors = " ".join(str(c.args[0]) for c in logger.error.call_args_list)
        return ok, execute, errors

    @pytest.mark.parametrize("name", ["vm01_snapshot_s1.raw",
                                      "vm01_snapshot_s1.raw.zst"])
    @pytest.mark.parametrize("force", [False, True], ids=["plain", "force"])
    def test_a_file_already_there_refuses_it(self, sm: SnapshotManager,
                                             tmp_path: Path, name, force):
        (tmp_path / name).write_bytes(b"someone's RAM")

        ok, execute, errors = self._take(sm, tmp_path, force=force)

        assert ok is False
        execute.assert_not_called()
        assert (tmp_path / name).read_bytes() == b"someone's RAM"
        assert f"{tmp_path / name} already exists" in errors

    @pytest.mark.skipif(os.geteuid() == 0,
                        reason="root searches a mode-000 directory")
    def test_one_that_cannot_be_looked_up_refuses_it(
            self, sm: SnapshotManager, tmp_path: Path):
        hidden = tmp_path / "hidden"
        hidden.mkdir()
        hidden.chmod(0)
        try:
            ok, execute, errors = self._take(sm, hidden)
        finally:
            hidden.chmod(0o700)

        assert ok is False
        execute.assert_not_called()
        assert "cannot tell whether" in errors

    def test_with_none_there_it_is_taken(self, sm: SnapshotManager,
                                         tmp_path: Path):
        ok, execute, _errors = self._take(sm, tmp_path)

        assert ok is True
        assert execute.call_args.args[0] == "snapshot-create-as"


class TestCreateSnapshotForce:
    """create_snapshot(force=...) runs the clear step before create-as."""

    def test_force_invokes_clear_before_create(self, sm: SnapshotManager,
                                               tmp_path: Path):
        scan, memory = MagicMock(), MagicMock()
        with patch.object(sm, "_force_clear_for_retake",
                          return_value=True) as clear, \
             patch.object(sm, "_flatten_cdrom_overlays"), \
             patch.object(sm, "_cdrom_diskspec_args", return_value=[]), \
             patch.object(sm.virsh, "execute", return_value=_result()):
            assert sm.create_snapshot(
                "vm01", str(tmp_path), "snapX", "d", force=True,
                paths_in_use=scan, recorded_in_use=memory) is True
        clear.assert_called_once_with("vm01", str(tmp_path), "snapX",
                                      paths_in_use=scan, recorded_in_use=memory,
                                      disk_dir=None)

    def test_force_clear_failure_aborts_create(self, sm: SnapshotManager,
                                              tmp_path: Path):
        with patch.object(sm, "_force_clear_for_retake",
                          return_value=False), \
             patch.object(sm.virsh, "execute", return_value=_result()) as execute:
            assert sm.create_snapshot(
                "vm01", str(tmp_path), "snapX", "d", force=True) is False
        # never reaches snapshot-create-as
        execute.assert_not_called()

    def test_no_force_skips_clear(self, sm: SnapshotManager, tmp_path: Path):
        with patch.object(sm, "_force_clear_for_retake") as clear, \
             patch.object(sm, "_flatten_cdrom_overlays"), \
             patch.object(sm, "_cdrom_diskspec_args", return_value=[]), \
             patch.object(sm.virsh, "execute", return_value=_result()):
            sm.create_snapshot("vm01", str(tmp_path), "snapX", "d")
        clear.assert_not_called()


class TestOverlaysInADiskSnapshotDir:
    """``create_snapshot(disk_dir=...)`` creates each disk's overlay in that
    directory, under the name libvirt gives it beside the disk: the current
    source with its last suffix dropped, then ``.<snapshot>``."""

    BLKLIST = (
        "Type   Device  Target  Source\n"
        "file   disk    vda     /ws/vm01.qcow2\n"
        "file   disk    vdb     /fast/ov/vm01_data.s1\n"
        "file   cdrom   sda     /iso/seed.iso\n"
    )

    @staticmethod
    def _blklist(*rows: tuple[str, str]) -> str:
        return "Type Device Target Source\n" + "".join(
            f"file disk {target} {source}\n" for target, source in rows)

    #: a definition whose disks a snapshot takes as it is told
    DOMAIN = "<domain><devices/></domain>"

    @staticmethod
    def _domain(*disks: str) -> str:
        return f"<domain><devices>{''.join(disks)}</devices></domain>"

    def _create(self, sm, tmp_path, disk_dir, blklist=None, listed=True,
                domain=DOMAIN, **kw):
        """*domain*: what ``dumpxml`` answers; ``None``, it fails."""
        calls = []

        def execute(*args, **_kw):
            calls.append(args)
            if args[0] == "domblklist":
                if not listed:
                    return _result(ok=False, stderr="error: no domain")
                return _result(stdout=self.BLKLIST if blklist is None
                               else blklist)
            if args[0] == "dumpxml":
                if domain is None:
                    return _result(ok=False, stderr="error: no domain")
                return _result(stdout=domain)
            return _result()

        with patch.object(sm, "_flatten_cdrom_overlays"), \
             patch.object(sm.virsh, "execute", side_effect=execute):
            ok = sm.create_snapshot("vm01", str(tmp_path), "s2", "d",
                                    disk_dir=disk_dir, **kw)
        creates = [c for c in calls if c[0] == "snapshot-create-as"]
        return ok, creates

    @staticmethod
    def _specs(args) -> list[str]:
        return [args[i + 1] for i, a in enumerate(args) if a == "--diskspec"]

    def test_each_disk_gets_its_overlay_there(self, sm, tmp_path):
        ok, [args] = self._create(sm, tmp_path, "/fast/ov")

        assert ok is True
        assert self._specs(args) == [
            "vda,snapshot=external,file=/fast/ov/vm01.s2",
            "vdb,snapshot=external,file=/fast/ov/vm01_data.s2",
            "sda,snapshot=no",
        ]

    def test_unset_names_no_overlay_file(self, sm, tmp_path):
        """libvirt then creates each beside its disk, as before."""
        ok, [args] = self._create(sm, tmp_path, None)

        assert ok is True
        assert self._specs(args) == ["sda,snapshot=no"]

    def test_a_comma_in_the_path_is_doubled(self, sm, tmp_path):
        """virsh splits a diskspec at commas; ``,,`` is a literal one."""
        ok, [args] = self._create(
            sm, tmp_path, "/fast/a,b",
            blklist=self._blklist(("vda", "/ws/vm01.qcow2")))

        assert self._specs(args) == [
            "vda,snapshot=external,file=/fast/a,,b/vm01.s2"]

    def test_two_disks_that_would_share_an_overlay_refuse_the_take(
            self, sm, tmp_path):
        ok, creates = self._create(
            sm, tmp_path, "/fast/ov",
            blklist=self._blklist(("vda", "/a/data.qcow2"),
                                  ("vdb", "/b/data.qcow2")))

        assert ok is False
        assert creates == []

    def test_disks_that_cannot_be_listed_refuse_the_take(self, sm, tmp_path):
        """Taken anyway, the overlays would land beside the disks."""
        ok, creates = self._create(sm, tmp_path, "/fast/ov", listed=False)

        assert ok is False
        assert creates == []

    @pytest.mark.parametrize("excluded", [
        "<disk type='file' device='disk'><readonly/>"
        "<target dev='vdb'/></disk>",
        "<disk type='file' device='disk' snapshot='no'>"
        "<target dev='vdb'/></disk>",
    ], ids=["read-only", "snapshot-no"])
    def test_a_disk_a_snapshot_leaves_out_gets_no_overlay(self, sm, tmp_path,
                                                          excluded):
        """libvirt leaves it out unless told otherwise, and domblklist
        does not say so: named, it would be forced into the snapshot — and
        here would collide with the writable disk's overlay."""
        ok, [args] = self._create(
            sm, tmp_path, "/fast/ov",
            blklist=self._blklist(("vda", "/work/vm01.qcow2"),
                                  ("vdb", "/reference/vm01.qcow2")),
            domain=self._domain(excluded))

        assert ok is True
        assert self._specs(args) == [
            "vda,snapshot=external,file=/fast/ov/vm01.s2"]

    def test_a_definition_that_cannot_be_read_refuses_the_take(self, sm,
                                                               tmp_path):
        ok, creates = self._create(sm, tmp_path, "/fast/ov", domain=None)

        assert ok is False
        assert creates == []

    def test_force_predicts_the_leftovers_there(self, sm):
        with patch.object(sm, "_data_disk_targets",
                          return_value=[("vda", "/ws/vm01.qcow2"),
                                        ("vdb", "/fast/ov/vm01_data.s1")]), \
                patch.object(sm, "_snapshot_excluded_targets",
                             return_value=set()):
            assert sm._would_be_overlay_paths(
                "vm01", "snapX", disk_dir="/fast/ov") == [
                    "/fast/ov/vm01.snapX", "/fast/ov/vm01_data.snapX"]

    @pytest.mark.parametrize("disk_dir", [None, "/fast/ov"])
    def test_force_predicts_none_for_a_disk_a_snapshot_leaves_out(
            self, sm, disk_dir):
        """Never created there, a file at its name is no leftover."""
        with patch.object(sm, "_data_disk_targets",
                          return_value=[("vda", "/work/vm01.qcow2"),
                                        ("vdb", "/reference/vm01.qcow2")]), \
                patch.object(sm, "_snapshot_excluded_targets",
                             return_value={"vdb"}):
            paths = sm._would_be_overlay_paths("vm01", "snapX",
                                               disk_dir=disk_dir)

        assert paths == [os.path.join(disk_dir or "/work", "vm01.snapX")]

    def test_force_predicts_nothing_when_it_cannot_tell(self, sm):
        with patch.object(sm, "_data_disk_targets",
                          return_value=[("vda", "/work/vm01.qcow2")]), \
                patch.object(sm, "_snapshot_excluded_targets",
                             return_value=None):
            assert sm._would_be_overlay_paths("vm01", "snapX") == []

    def test_what_a_snapshot_leaves_out_is_read_from_the_definition(self, sm):
        domain = self._domain(
            "<disk device='disk'><target dev='vda'/></disk>",
            "<disk device='disk'><readonly/><target dev='vdb'/></disk>",
            "<disk device='disk' snapshot='no'><target dev='vdc'/></disk>",
            "<disk device='disk' snapshot='external'>"
            "<target dev='vdd'/></disk>")
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout=domain)) as execute:
            assert sm._snapshot_excluded_targets("vm01") == {"vdb", "vdc"}
        assert execute.call_args.args[:2] == ("dumpxml", "vm01")

    def test_force_clears_a_leftover_there(self, sm, tmp_path):
        ov = tmp_path / "ov"
        ov.mkdir()
        orphan = ov / "vm01.snapX"
        orphan.write_bytes(b"x")
        beside = tmp_path / "vm01.snapX"
        beside.write_bytes(b"not this one")
        removed = []
        absent = _result(ok=False, stderr="Domain snapshot not found")
        with patch.object(sm.virsh, "execute", return_value=absent), \
             patch.object(sm, "_data_disk_targets",
                          return_value=[("vda", str(tmp_path / "vm01.qcow2"))]), \
             patch.object(sm, "_snapshot_excluded_targets",
                          return_value=set()), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=lambda cmd, *a, **k: (
                              removed.append(cmd), _result())[1]):
            assert sm._force_clear_for_retake(
                "vm01", str(tmp_path), "snapX", disk_dir=str(ov),
                paths_in_use=MagicMock(return_value={}),
                recorded_in_use=MagicMock(return_value={})) is True

        joined = "\n".join(removed)
        assert str(orphan) in joined
        assert str(beside) not in joined

    def test_force_keeps_a_leftover_there_another_domain_uses(self, sm,
                                                              tmp_path):
        ov = tmp_path / "ov"
        ov.mkdir()
        used = ov / "vm01.snapX"
        used.write_bytes(b"another domain's disk")
        scan = MagicMock(return_value=FilesInUse(
            {os.path.realpath(used): "vm02"}))
        removed = []
        absent = _result(ok=False, stderr="Domain snapshot not found")
        with patch.object(sm.virsh, "execute", return_value=absent), \
             patch.object(sm, "_data_disk_targets",
                          return_value=[("vda", str(tmp_path / "vm01.qcow2"))]), \
             patch.object(sm, "_snapshot_excluded_targets",
                          return_value=set()), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=lambda cmd, *a, **k: (
                              removed.append(cmd), _result())[1]):
            assert sm._force_clear_for_retake(
                "vm01", str(tmp_path), "snapX", disk_dir=str(ov),
                paths_in_use=scan,
                recorded_in_use=MagicMock(return_value={})) is False

        assert str(used) not in "\n".join(removed)
        assert used.read_bytes() == b"another domain's disk"
