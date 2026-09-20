"""
Unit tests for boxman.providers.libvirt.snapshot.SnapshotManager.

Covers the overlay-preservation regression fix landed in commit 057eb7d —
``snapshot_restore`` must copy overlay files aside before reverting and put
them back afterwards so every snapshot remains reachable.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import SnapshotError, SnapshotRecoveryError
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
        return _result(ok=proc.returncode == 0, stderr=proc.stderr,
                       return_code=proc.returncode)
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


class TestWouldBeOverlayPaths:
    """`snapshot take --force` predicts the overlay libvirt would create."""

    def test_derives_from_current_source(self, sm: SnapshotManager):
        # libvirt drops the source's final suffix and appends .<snapshot>
        with patch.object(sm, "_data_disk_targets",
                          return_value=[("vda", "/ws/vm01.qcow2"),
                                        ("vdb", "/ws/vm01-data.1784942899")]):
            paths = sm._would_be_overlay_paths("vm01", "snapX")
        assert paths == ["/ws/vm01.snapX", "/ws/vm01-data.snapX"]

    def test_skips_placeholder_source(self, sm: SnapshotManager):
        with patch.object(sm, "_data_disk_targets", return_value=[("vda", "-")]):
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
             patch.object(sm.virsh, "execute_shell", side_effect=fake_shell):
            assert sm._force_clear_for_retake(
                "vm01", str(tmp_path), "snapX") is True

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


class TestCreateSnapshotForce:
    """create_snapshot(force=...) runs the clear step before create-as."""

    def test_force_invokes_clear_before_create(self, sm: SnapshotManager,
                                               tmp_path: Path):
        with patch.object(sm, "_force_clear_for_retake",
                          return_value=True) as clear, \
             patch.object(sm, "_flatten_cdrom_overlays"), \
             patch.object(sm, "_cdrom_diskspec_args", return_value=[]), \
             patch.object(sm.virsh, "execute", return_value=_result()):
            assert sm.create_snapshot(
                "vm01", str(tmp_path), "snapX", "d", force=True) is True
        clear.assert_called_once_with("vm01", str(tmp_path), "snapX")

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
