"""
Unit tests for boxman.providers.libvirt.snapshot.SnapshotManager.

Covers the overlay-preservation regression fix landed in commit 057eb7d —
``snapshot_restore`` must copy overlay files aside before reverting and put
them back afterwards so every snapshot remains reachable.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

import pytest

from boxman.exceptions import SnapshotError, SnapshotRecoveryError
from boxman.providers.libvirt.cdrom import CDROMManager
from boxman.providers.libvirt.commands import LibVirtCommandBase, VirshCommand
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


def _created(execute: MagicMock) -> bool:
    """Whether the ``virsh.execute`` double was asked to create a snapshot.

    Not "was it called at all": a take asks the domain's state first, to
    know what the snapshot can hold (#272).
    """
    return any(c.args and c.args[0] == "snapshot-create-as"
               for c in execute.call_args_list)


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


@contextlib.contextmanager
def _inventory(sm: SnapshotManager, overlays: dict, order: list,
               current: list = ()):
    """Patch the strict inventory with a complete, readable answer, and
    what the current snapshot records with *current* — nothing beyond the
    target-and-newer set unless a test says so (#287).

    Tests about the strictness itself must NOT use this — they drive
    ``virsh`` (see ``_virsh_snapshots``) so the enumeration really runs.
    """
    with patch.object(sm, "_strict_overlay_inventory",
                      return_value=(overlays, order)), \
         patch.object(sm, "_overlays_the_revert_deletes",
                      return_value=list(current)):
        yield


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
                     disk=None, current=None):
    """A ``virsh.execute`` double answering snapshot-list / snapshot-dumpxml
    / snapshot-current.

    ``xml_for(name)`` returns the XML text, or None to fail that dumpxml.
    The current snapshot is *current*, by default the last one listed.
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
        if args[0] == "snapshot-current":
            name = current if current is not None else (
                names[-1] if names else None)
            xml = xml_for(name) if name is not None else None
            if xml is None:
                return _result(ok=False, stderr=(
                    "error: domain 'vm01' has no current snapshot"))
            return _result(stdout=xml)
        return _result()
    return execute


@pytest.fixture
def sm() -> SnapshotManager:
    return SnapshotManager(provider_config={"use_sudo": False, "uri": "qemu:///system"})


def _nothing_else_records() -> FilesInUse:
    """The scan a delete asks once its snapshots are gone
    (``LibVirtSession.memory_images_recorded``), naming nothing: no other
    snapshot records their memory images."""
    return FilesInUse()


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

    @staticmethod
    def _validate(sm: SnapshotManager, xml: str):
        def fake(*args, **_kwargs):
            if args[0] == "snapshot-info":
                return _result(ok=True)
            return _result(stdout=xml)

        with patch.object(sm.virsh, "execute", side_effect=fake):
            return sm.validate_snapshot("vm01", "snap1")

    @staticmethod
    def _xml(overlays: dict, saved: dict, state: str = "running",
             inactive: dict | None = None, memory: Path | None = None) -> str:
        """A snapshot's XML: *overlays* the external ``<disks>`` it records,
        *saved* the disks of its ``<domain>``, *inactive* those of its
        ``<inactiveDomain>``."""
        def domain(tag, disks):
            return (f"<{tag}><devices>" + "".join(
                f"<disk type='file' device='disk'><source file='{p}'/>"
                f"<target dev='{t}' bus='virtio'/></disk>"
                for t, p in disks.items()) + f"</devices></{tag}>")
        return ("<domainsnapshot><name>snap1</name>"
                f"<state>{state}</state>"
                + (f"<memory file='{memory}'/>" if memory else "")
                + "<disks>" + "".join(
                    f"<disk name='{t}' snapshot='external'>"
                    f"<source file='{p}'/></disk>"
                    for t, p in overlays.items()) + "</disks>"
                + domain("domain", saved)
                + (domain("inactiveDomain", inactive) if inactive else "")
                + "</domainsnapshot>")

    @staticmethod
    def _touch(path: Path) -> Path:
        path.write_bytes(b"x")
        return path

    def test_reports_a_missing_image_a_revert_builds_on(
            self, sm: SnapshotManager, tmp_path: Path):
        """A revert lays a new overlay over each disk's source in the
        definition the snapshot saved: with that gone it fails, whatever the
        snapshot's own overlay (#287 left such a snapshot looking valid)."""
        overlay = self._touch(tmp_path / "vm01.snap1")
        base = tmp_path / "vm01.qcow2"
        ok, errors = self._validate(
            sm, self._xml({"vda": overlay}, {"vda": base}))
        assert ok is False
        assert errors == [f"disk image missing: {base} "
                          f"(a revert builds vda on it)"]

    def test_the_overlay_its_take_made_need_not_be_there(
            self, sm: SnapshotManager, tmp_path: Path):
        """No revert to it reads it. libvirt's delete of a child taken on it
        that is not current deletes it and leaves this record (#289)."""
        base = self._touch(tmp_path / "vm01.qcow2")
        ok, errors = self._validate(sm, self._xml(
            {"vda": tmp_path / "vm01.snap1"}, {"vda": base}))
        assert (ok, errors) == (True, [])

    def test_a_disk_only_snapshot_is_checked_against_its_inactive_definition(
            self, sm: SnapshotManager, tmp_path: Path):
        """libvirt reverts one that saved no memory from its
        ``<inactiveDomain>`` when it has one (qemuSnapshotRevertInactive),
        and one that did from its ``<domain>``."""
        live = self._touch(tmp_path / "vm01.qcow2")
        persistent = tmp_path / "vm01.other"
        overlays = {"vda": self._touch(tmp_path / "vm01.snap1")}
        disk_only = self._xml(overlays, {"vda": live}, state="disk-snapshot",
                              inactive={"vda": persistent})
        running = self._xml(overlays, {"vda": live},
                            inactive={"vda": persistent})

        assert self._validate(sm, disk_only) == (
            False, [f"disk image missing: {persistent} "
                    f"(a revert builds vda on it)"])
        assert self._validate(sm, running) == (True, [])

    def test_all_present_is_valid(self, sm: SnapshotManager, tmp_path: Path):
        mem = self._touch(tmp_path / "m.raw")
        overlay = self._touch(tmp_path / "o.qcow2")
        base = self._touch(tmp_path / "vm01.qcow2")
        ok, errors = self._validate(sm, self._xml(
            {"vda": overlay}, {"vda": base}, memory=mem))
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
        # no <state> recorded: neither is claimed to be disk-only
        assert out == [
            {"name": "snap1", "description": "description of snap1",
             "disk_only": False},
            {"name": "snap2", "description": "description of snap2",
             "disk_only": False},
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

    @pytest.mark.parametrize("source", [
        "<source protocol='nbd' name='web.s2'><host name='h'/></source>",
        "",
    ], ids=["a network source", "no source"])
    def test_an_external_disk_that_names_no_local_file_makes_them_unknown(
            self, sm: SnapshotManager, source):
        """Its overlay is somewhere, and can be built on local files: not
        knowing it is not "none recorded"."""
        xml = {"s2": self._xml("s2", f"<disk name='vda' snapshot='external' "
                                     f"type='network'>{source}</disk>")}
        with patch.object(sm.virsh, "execute",
                          side_effect=_virsh_snapshots(list(xml), xml.get)):
            assert sm.overlay_files("vm01") is None
            # the memory images are read as before
            assert sm.memory_images("vm01") == {}

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


def _recording(name: str, disks: dict, revert: dict | None = None,
               parent: str | None = None) -> str:
    """A snapshot's XML recording *disks* — ``{target: overlay or None}``,
    None for ``snapshot='no'`` — and *revert* as its ``<revertDisks>``."""
    def recorded(entries):
        return "".join(
            f"<disk name='{t}' snapshot='external'><source file='{p}'/>"
            f"</disk>" if p else f"<disk name='{t}' snapshot='no'/>"
            for t, p in entries.items())
    return (f"<domainsnapshot><name>{name}</name>"
            + (f"<parent><name>{parent}</name></parent>" if parent else "")
            + f"<disks>{recorded(disks)}</disks>"
            + (f"<revertDisks>{recorded(revert)}</revertDisks>"
               if revert is not None else "")
            + "</domainsnapshot>")


class TestWhatTheRevertDeletes:
    """#287: a revert deletes what the current snapshot records -- its
    ``<revertDisks>`` if it has any, else its ``<disks>``, the external ones
    (qemuSnapshotRevertExternalFinish) -- wherever it is in the chain. That
    is read as strictly as the inventory: a gap refuses the revert rather
    than leave something it deletes out of the backup."""

    @staticmethod
    def _current(sm, xml, names=("s1", "s2"), ok=True):
        with patch.object(sm.virsh, "execute", return_value=_result(
                stdout=xml, ok=ok, stderr="" if ok else
                "error: domain 'vm01' has no current snapshot")) as execute:
            found = sm._overlays_the_revert_deletes("vm01", list(names))
        assert execute.call_args.args[:2] == ("snapshot-current", "vm01")
        return found

    def test_its_revert_disks_when_it_has_any_not_its_disks(self, sm):
        xml = _recording("s1", {"vda": "/ws/vm01.s1", "sda": None},
                         revert={"vda": "/ws/vm01.1793626527", "sda": None})

        assert self._current(sm, xml) == ["/ws/vm01.1793626527"]

    def test_its_disks_when_it_has_no_revert_disks(self, sm):
        xml = _recording("s1", {"vda": "/ws/vm01.s1",
                                "vdb": "/fast/vm01_data.s1", "sda": None})

        assert self._current(sm, xml) == ["/ws/vm01.s1", "/fast/vm01_data.s1"]

    def test_a_domain_without_snapshots_asks_nothing(self, sm):
        with patch.object(sm.virsh, "execute") as execute:
            assert sm._overlays_the_revert_deletes("vm01", []) == []
        execute.assert_not_called()

    @pytest.mark.parametrize("xml,ok,names,expected", [
        ("", False, ("s1",), "cannot tell which snapshot"),
        ("<domainsnapshot><name>s1</name>", True, ("s1",), "does not parse"),
        (_recording("s9", {"vda": "/ws/vm01.s9"}), True, ("s1",),
         "not one it listed"),
        ("<domainsnapshot><name>s1</name><disks/>"
         "<revertDisks><disk name='vda' snapshot='external'/></revertDisks>"
         "</domainsnapshot>", True, ("s1",), "names no overlay file"),
        ("<domainsnapshot><name>s1</name><revertDisks/></domainsnapshot>",
         True, ("s1",), "lists no disks"),
    ], ids=["unreadable", "unparseable", "unlisted", "no-source",
            "empty-revert-disks"])
    def test_a_gap_refuses(self, sm, xml, ok, names, expected):
        with pytest.raises(SnapshotError, match=expected):
            self._current(sm, xml, names=names, ok=ok)

    def test_a_restore_that_cannot_tell_the_current_one_never_reverts(
            self, sm, tmp_path):
        overlays = {n: str(tmp_path / f"vm01.{n}") for n in ("s1", "s2")}
        for path in overlays.values():
            Path(path).write_bytes(b"x")
        seen: list[str] = []
        calls: list[str] = []
        with patch.object(sm.virsh, "execute", side_effect=_virsh_snapshots(
                ["s1", "s2"],
                lambda n: _snap_xml(n, overlays[n],
                                    "s1" if n == "s2" else None)
                if n in overlays else None,
                seen=seen, current="gone")), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies(calls)):
            with pytest.raises(SnapshotError, match="is current"):
                sm.snapshot_restore("vm01", "s2")

        assert "snapshot-revert" not in seen
        assert calls == []

    def test_what_an_older_current_one_records_is_backed_up_with_the_target(
            self, sm, tmp_path):
        """s1 current, older than the target s2: its overlay, which s2 is
        built on, goes in the backup as well as s2's."""
        old, new = tmp_path / "vm01.s1", tmp_path / "vm01.s2"
        old.write_bytes(b"s2 is built on this")
        new.write_bytes(b"s2's own")

        with _inventory(sm, {"s1": [str(old)], "s2": [str(new)]},
                        ["s1", "s2"], current=[str(old)]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_copies()):
            preserved = sm._preserve_snapshot_overlays("vm01", "s2")

        assert sorted(o for o, _b in preserved) == [str(old), str(new)]
        assert Path(f"{old}.preserve").read_bytes() == b"s2 is built on this"


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

        # no in-use scan given: no CDROM overlay is left unbacked
        preserve.assert_called_once_with("vm01", "snap1", skip=[])
        assert [call.args[0] for call in execute.call_args_list
                if call.args] == ["snapshot-revert"]
        # a revert that succeeded: what it discarded is not put back
        restore.assert_called_once_with(preserved_pairs, vm_name="vm01")

    def test_restore_called_even_when_revert_fails(self, sm: SnapshotManager):
        with _not_running(sm), \
             patch.object(sm, "_preserve_snapshot_overlays", return_value=[]), \
             patch.object(sm, "_memory_path_from_xml", return_value=None), \
             patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False, stderr="boom")), \
             patch.object(sm, "_restore_preserved_overlays",
                          return_value=[]) as restore:
            assert sm.snapshot_restore("vm01", "snap1") is False
        # one that failed tells nothing of what it discarded: all go back
        restore.assert_called_once_with([], vm_name=None)

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
            "disk_only": False,
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
        scan = MagicMock()
        with patch.object(sm.virsh, "execute", side_effect=fake), \
             patch.object(sm, "_delete_external_snapshot",
                          return_value=True) as ext:
            assert sm.delete_snapshot("vm01", "snap1",
                                      recorded_in_use=scan) is True
        ext.assert_called_once_with("vm01", "snap1", recorded_in_use=scan)

    def test_the_oldest_with_snapshots_on_it_names_them_and_no_collapse(
            self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False,
                                               stderr="external snapshots not supported")), \
             patch.object(sm, "_snapshot_tree",
                          return_value={"snap1": None, "snap2": "snap1", "snap3": "snap2"}), \
             patch.object(sm, "get_latest_snapshot", return_value="snap3"), \
             patch.object(sm.logger, "error") as err:
            # Try deleting snap1 — it's not the most-recent
            # _delete_external_snapshot is invoked by delete_snapshot
            assert sm._delete_external_snapshot("vm01", "snap1") is False
        msgs = " ".join(c.args[0] for c in err.call_args_list)
        # snap1 has no parent: no collapse drops it, and none is named
        assert "collapse --to" not in msgs
        assert "newer snapshots" in msgs and "['snap2', 'snap3']" in msgs

    def test_a_middle_snapshot_points_at_the_collapse_that_drops_it(
            self, sm: SnapshotManager):
        with patch.object(sm, "_snapshot_tree",
                          return_value={"snap1": None, "snap2": "snap1", "snap3": "snap2"}), \
             patch.object(sm, "get_latest_snapshot", return_value="snap3"), \
             patch.object(sm.logger, "error") as err:
            assert sm._delete_external_snapshot("vm01", "snap2") is False
        msgs = " ".join(c.args[0] for c in err.call_args_list)
        assert "collapse --to snap1" in msgs
        assert "newer snapshots" in msgs and "['snap3']" in msgs

    def test_external_only_snapshot_uses_blockcommit(
            self, sm: SnapshotManager):
        scan = MagicMock()
        with patch.object(sm, "_snapshot_tree", return_value={"snap1": None}), \
             patch.object(sm, "get_latest_snapshot", return_value="snap1"), \
             patch.object(sm, "_collapse_only_external_snapshot_online",
                          return_value=True) as collapse_only:
            assert sm._delete_external_snapshot(
                "vm01", "snap1", recorded_in_use=scan) is True
        collapse_only.assert_called_once_with("vm01", "snap1",
                                              recorded_in_use=scan)

    def test_external_most_recent_with_parent_calls_collapse_to(
            self, sm: SnapshotManager):
        scan = MagicMock()
        with patch.object(sm, "_snapshot_tree",
                          return_value={"snap1": None, "snap2": "snap1", "snap3": "snap2"}), \
             patch.object(sm, "get_latest_snapshot", return_value="snap3"), \
             patch.object(sm, "collapse_to", return_value=True) as collapse:
            # Deleting snap3 (most-recent) collapses everything newer than
            # its parent (snap2) into the head — i.e. just drops snap3.
            assert sm._delete_external_snapshot(
                "vm01", "snap3", recorded_in_use=scan) is True
        collapse.assert_called_once_with("vm01", "snap2", dry_run=False,
                                         recorded_in_use=scan)


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


class TestSnapshotTree:

    @staticmethod
    def _virsh(xml_for, names=None):
        def fake(*args, **_kwargs):
            if args[0] == "snapshot-list":
                return _result(stdout="".join(
                    f"{n}\n" for n in names or xml_for) + "\n")
            if args[0] == "snapshot-dumpxml":
                xml = xml_for[args[2]]
                return (_result(ok=False, stderr="error: boom") if xml is None
                        else _result(stdout=xml))
            raise AssertionError(f"unexpected virsh call: {args}")
        return fake

    def test_each_snapshot_maps_to_its_parent(self, sm: SnapshotManager):
        # snap1 with two children: snap2, and snap3 taken after a restore
        xml_for = {
            "snap1": "<domainsnapshot><name>snap1</name></domainsnapshot>",
            "snap2": _snap_xml("snap2", "/p/o2", parent="snap1"),
            "snap3": _snap_xml("snap3", "/p/o3", parent="snap1"),
        }
        with patch.object(sm.virsh, "execute", side_effect=self._virsh(xml_for)):
            assert sm._snapshot_tree("vm01") == {
                "snap1": None, "snap2": "snap1", "snap3": "snap1"}

    def test_no_snapshots_is_an_empty_tree(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout="")) as execute:
            assert sm._snapshot_tree("vm01") == {}
        execute.assert_called_once_with(
            "snapshot-list", "vm01", "--name", warn=True)

    @pytest.mark.parametrize("xml", [None, "<domainsnapshot><name>"],
                             ids=["dumpxml fails", "does not parse"])
    def test_one_that_cannot_be_read_refuses(self, sm, xml):
        # a snapshot left out could be built on one a collapse drops
        xml_for = {"snap1": _snap_xml("snap1", "/p/o1"), "snap2": xml}
        with patch.object(sm.virsh, "execute", side_effect=self._virsh(xml_for)), \
             patch.object(sm.logger, "error") as error:
            assert sm._snapshot_tree("vm01") is None
        assert "snap2" in str(error.call_args.args[0])

    TREE = {"s1": None, "s2": "s1", "s4": "s2", "s3": "s1", "s5": "s3"}

    def test_what_is_built_on_one_is_every_branch_nearest_first(self):
        assert SnapshotManager._descendants(self.TREE, "s1") == [
            "s2", "s3", "s4", "s5"]
        assert SnapshotManager._descendants(self.TREE, "s3") == ["s5"]
        assert SnapshotManager._descendants(self.TREE, "s4") == []

    def test_metadata_in_a_loop_still_ends(self):
        assert SnapshotManager._descendants({"a": "b", "b": "a"}, "a") == ["b"]
        assert SnapshotManager._line_between({"a": "b", "b": "a"}, "c",
                                             "a") is None

    @pytest.mark.parametrize("ancestor, current, line", [
        ("s1", "s5", ["s3", "s5"]),
        ("s3", "s5", ["s5"]),
        ("s5", "s5", []),
        ("s2", "s5", None),
        ("s4", "s3", None),
        ("s1", None, None),
    ])
    def test_the_line_between_two_snapshots(self, ancestor, current, line):
        assert SnapshotManager._line_between(
            self.TREE, ancestor, current) == line


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
             patch.object(sm, "_snapshot_tree",
                          return_value={"snap1": None, "snap2": "snap1"}), \
             patch.object(sm, "_qemu_img_rebase") as rebase:
            # snap2 is already the head — nothing to drop
            assert sm.collapse_to("vm01", "snap2") is True
        rebase.assert_not_called()

    def test_dry_run_does_not_rebase(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute", return_value=_result()), \
             patch.object(sm, "_snapshot_tree", return_value={"snap1": None, "snap2": "snap1", "snap3": "snap2"}), \
             patch.object(sm, "get_latest_snapshot", return_value="snap3"), \
             patch.object(sm, "_data_disk_targets",
                          return_value=[("vda", "/p/vm01.qcow2")]), \
             patch.object(sm, "_overlay_path_for_snapshot",
                          return_value="/p/vm01.snap1.qcow2"), \
             patch.object(sm, "_collapse_base",
                          return_value=("/p/vm01.snap1.qcow2", False, None)), \
             patch.object(sm, "_qemu_img_rebase") as rebase, \
             patch.object(sm, "_strip_backing_store_cache") as strip:
            assert sm.collapse_to("vm01", "snap1", dry_run=True) is True
        rebase.assert_not_called()
        strip.assert_not_called()

    def test_target_not_in_chain_fails(self, sm: SnapshotManager):
        with patch.object(sm.virsh, "execute", return_value=_result()), \
             patch.object(sm, "_snapshot_tree",
                          return_value={"snap1": None, "snap2": "snap1"}):
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
             patch.object(sm, "_snapshot_tree", return_value={"snap1": None, "snap2": "snap1", "snap3": "snap2"}), \
             patch.object(sm, "get_latest_snapshot", return_value="snap3"), \
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
            assert sm.collapse_to(
                "vm01", "snap1",
                recorded_in_use=_nothing_else_records) is True

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
    refuses an external snapshot, and ``--metadata`` forgets it, moving
    its children to its parent and, when it was current, leaving that
    current. ``snapshot-create --redefine`` replaces what a snapshot
    records under ``<disks>``.

    *records* maps snapshot -> {disk target: recorded overlay}; *parents*
    defaults to a linear chain in the order given, and *current* to the
    last snapshot given. *taken_on* maps snapshot -> {disk target: the disk
    it was taken on}, by default its parent's recorded overlay.
    """

    def __init__(self, records: dict, heads: dict, memory: dict,
                 parents: dict | None = None, current: str | None = None,
                 taken_on: dict | None = None):
        self.records = records
        self.taken_on = taken_on or {}
        self.heads = heads
        self.memory = memory
        names = list(records)
        self.parents = dict(parents or {
            n: (names[i - 1] if i else None) for i, n in enumerate(names)})
        self.snapshots = names
        self.current = current or names[-1]
        #: the snapshot each redefine named
        self.redefined: list[str] = []

    def _taken_on(self, name: str, parent: str | None, target: str) -> str:
        if target in self.taken_on.get(name, {}):
            return self.taken_on[name][target]
        return self.records[parent][target] if parent else "base"

    def _snapshot_xml(self, name: str) -> str:
        parent = self.parents[name]
        disks = "".join(
            f"<disk name='{target}' snapshot='external' type='file'>"
            f"<driver type='qcow2'/><source file='{path}'/></disk>"
            for target, path in self.records[name].items())
        taken_on = "".join(
            f"<disk type='file' device='disk'>"
            f"<source file='{self._taken_on(name, parent, t)}'/>"
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
        if verb == "snapshot-current":
            return _result(stdout=f"{self.current}\n")
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
                name = args[2]
                self.snapshots.remove(name)
                for child, parent in self.parents.items():
                    if parent == name:
                        self.parents[child] = self.parents[name]
                if self.current == name:
                    self.current = self.parents[name]
                return _result()
            return _result(ok=False, stderr=(
                "error: unsupported configuration: deletion of 1 external "
                "disk snapshots not supported yet"))
        if verb == "snapshot-create":
            assert "--redefine" in args and "--current" not in args
            root = ET.fromstring(Path(args[2]).read_text())
            name = root.findtext("name")
            # what its children were taken on stays what it recorded
            for child, parent in self.parents.items():
                if parent == name:
                    self.taken_on.setdefault(child, {}).update({
                        t: p for t, p in self.records[name].items()
                        if t not in self.taken_on.get(child, {})})
            self.records[name] = {
                d.get("name"): d.find("source").get("file")
                for d in root.findall("disks/disk")
                if d.get("snapshot") == "external"}
            self.redefined.append(name)
            return _result()
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
                         {"vda": str(head)}, memory,
                         taken_on={"s1": {"vda": str(base)}})

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is True

        assert head.is_file(), "the live disk was removed"
        # on the disk s1 was taken on, s1's own overlay merged in with s2
        assert _chain(head) == [str(head), str(base)]
        assert all(_reads(head, era) for era in (0, 1, 2))
        assert virsh.records["s1"] == {"vda": str(head)}
        assert not overlays["s1"].exists()
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
                         {"vda": str(head)}, memory,
                         taken_on={"s1": {"vda": str(base)}})

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is True

        assert head.is_file(), "the live disk was removed"
        # rebased past s2 and s1's overlay, their writes merged into the head
        assert _chain(head) == [str(head), str(base)]
        assert all(_reads(head, era) for era in (0, 1, 2, 3))
        assert not overlays["s2"].exists() and not overlays["s1"].exists()
        assert not memory["s2"].exists() and not memory["s3"].exists()
        assert memory["s1"].exists()
        assert virsh.records["s1"] == {"vda": str(head)}
        assert virsh.snapshots == ["s1"]
        # what s1 was taken with is untouched
        assert _reads(base, 0) and _reads(base, 1, pattern="0")

    @needs_qemu
    def test_deleting_the_newest_snapshot_keeps_the_head(self, sm, tmp_path):
        base, overlays, memory = _take_external(tmp_path, ["s1", "s2", "s3"])
        head = overlays["s3"]
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(head)}, memory,
                         taken_on={"s1": {"vda": str(base)}})

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            assert sm.delete_snapshot("vm01", "s3",
                                      recorded_in_use=_nothing_else_records) is True

        assert head.is_file(), "the live disk was removed"
        # on the disk s2 was taken on, s1's overlay, which s1 still records
        assert _chain(head) == [str(head), str(overlays["s1"]), str(base)]
        assert all(_reads(head, era) for era in (0, 1, 2, 3))
        assert not overlays["s2"].exists()
        assert virsh.records["s2"] == {"vda": str(head)}
        assert virsh.records["s1"] == {"vda": str(overlays["s1"])}
        assert not memory["s3"].exists()
        assert virsh.snapshots == ["s1", "s2"]

    @needs_qemu
    def test_right_after_a_revert_the_snapshot_it_left_is_not_dropped(
            self, sm, tmp_path):
        # take s1, take s2, revert to s1: s1 has a child, so libvirt puts
        # the domain on a fresh overlay of the disk s1 was taken from and
        # leaves s1's record alone. s2's overlay is then not live, and the
        # live disk is not built on s2 at all: a collapse to s1 merged
        # nothing of it and dropped it (#291). It is refused now.
        base, overlays, memory = _take_external(tmp_path, ["s1", "s2"])
        head = tmp_path / "vm01_disk.1785187700"
        _qemu("qemu-img", "create", "-q", "-f", "qcow2",
              "-b", str(base), "-F", "qcow2", str(head))
        _write(head, 5)
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(head)}, memory, current="s1")

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()), \
             patch.object(sm.logger, "error") as error:
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is False

        assert "['s2']" in str(error.call_args.args[0])
        assert overlays["s2"].exists() and memory["s2"].exists()
        assert virsh.snapshots == ["s1", "s2"]
        assert _chain(head) == [str(head), str(base)]
        # what the guest sees is still the reverted state: the writes made
        # after s1 was taken stay undone, the ones after the revert stay
        assert _reads(head, 0) and _reads(head, 5)
        assert _reads(head, 1, pattern="0") and _reads(head, 2, pattern="0")
        # and s2 still holds what it was taken on
        assert _reads(overlays["s2"], 1) and _reads(overlays["s2"], 2)

    @needs_qemu
    def test_after_a_revert_to_the_newest_its_old_overlay_stays_the_head_too(
            self, sm, tmp_path):
        # take s1, take s2, revert to s2: s2 has no child, so libvirt
        # records the fresh overlay it puts the domain on, on the disk s2
        # was taken from, as s2's own. The head is that overlay, not the
        # one s2 was taken with.
        base, overlays, memory = _take_external(tmp_path, ["s1", "s2"])
        head = tmp_path / "vm01_disk.1785187700"
        _qemu("qemu-img", "create", "-q", "-f", "qcow2",
              "-b", str(overlays["s1"]), "-F", "qcow2", str(head))
        _write(head, 5)
        virsh = _Libvirt({"s1": {"vda": str(overlays["s1"])},
                          "s2": {"vda": str(head)}},
                         {"vda": str(head)}, memory,
                         taken_on={"s1": {"vda": str(base)}})

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is True

        assert head.is_file()
        assert _chain(head) == [str(head), str(base)]
        assert all(_reads(head, era) for era in (0, 1, 5))
        assert _reads(head, 2, pattern="0")
        assert virsh.records["s1"] == {"vda": str(head)}
        assert not overlays["s1"].exists()
        assert not memory["s2"].exists() and memory["s1"].exists()
        assert virsh.snapshots == ["s1"]

    @needs_qemu
    def test_a_head_that_is_the_targets_own_overlay_is_not_rebased(
            self, sm, tmp_path):
        # take s1, s2, s4; revert to s1; take s3. The chain order puts s4,
        # on the abandoned branch, after s3, and the collapse dropped it
        # (#291). Nothing is built on s3: nothing is dropped -- and s3's
        # overlay, the head, is not rebased onto itself, which either trips
        # qemu's image lock or, without one, writes a backing chain that
        # loops.
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
            assert sm.collapse_to("vm01", "s3",
                                  recorded_in_use=_nothing_else_records) is True

        assert not any("qemu-img rebase" in c for c in calls)
        assert head.is_file()
        assert _chain(head) == [str(head), str(reverted), str(base)]
        assert _reads(head, 0) and _reads(head, 6)
        assert overlays["s4"].exists() and memory["s4"].exists()
        assert overlays["s2"].exists() and overlays["s1"].exists()
        assert virsh.snapshots == ["s1", "s2", "s4", "s3"]

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
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is True

        rebase.assert_called_once_with(str(head), str(overlays["s1"]))

    @needs_qemu
    @pytest.mark.parametrize("alias", ["symlink", "hardlink"])
    def test_the_targets_own_overlay_named_otherwise_is_not_rebased(
            self, sm, tmp_path, alias):
        # as test_a_head_that_is_the_targets_own_overlay_is_not_rebased,
        # with the head reported under another spelling: rebased onto
        # itself it trips qemu's image lock, or without one loops; and s4,
        # on the abandoned branch, stays
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
            assert sm.collapse_to("vm01", "s3",
                                  recorded_in_use=_nothing_else_records) is True

        assert not any("qemu-img rebase" in c for c in calls)
        assert _chain(s3) == [str(s3), str(reverted), str(base)]
        assert _reads(s3, 0) and _reads(s3, 6)
        assert overlays["s4"].exists()
        assert virsh.snapshots == ["s1", "s2", "s4", "s3"]

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
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is False

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

    @pytest.mark.parametrize("skip, sudo", [([], True), (["sh"], False)],
                             ids=["sudo", "sh skipped"])
    def test_asking_whether_a_kept_overlay_is_gone_takes_sudo_whole(
            self, skip, sudo):
        # a sudo prefix on a shell `if` fails to run, which reads as "there"
        sm = SnapshotManager(provider_config={
            "use_sudo": True, "sudo_skip_commands": skip,
            "uri": "qemu:///system"})
        run: list[str] = []

        def shell_run(command, **_kwargs):
            run.append(command)
            return _result(stdout="absent\n")

        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=shell_run):
            assert sm._gone_where_qemu_runs("/a/vm01.s1") is True

        assert len(run) == 1 and run[0].startswith(
            "sudo sh -c " if sudo else "sh -c ")

    def test_reading_the_heads_chain_takes_sudo_as_configured(self):
        # libvirt makes the overlays root's: unread, the chain tells nothing
        sm = SnapshotManager(provider_config={
            "use_sudo": True, "uri": "qemu:///system"})
        run: list[str] = []

        def shell_run(command, **_kwargs):
            run.append(command)
            if "qemu-img info" in command:
                return _result(stdout=json.dumps([
                    {"filename": "/w/vm01.s3", "format": "qcow2"},
                    {"filename": "/w/vm01.1785187700", "format": "qcow2"},
                    {"filename": "/w/vm01.qcow2", "format": "qcow2"}]))
            return _result(ok=False, stderr="stat: No such file or directory")

        def execute(*args, **_kwargs):
            assert args[0] == "snapshot-dumpxml"
            return _result(stdout=(
                "<domainsnapshot><name>s1</name><disks>"
                "<disk name='vda' snapshot='external'>"
                "<source file='/w/vm01.s1'/></disk></disks>"
                "<domain><devices><disk type='file' device='disk'>"
                "<source file='/w/vm01.qcow2'/><target dev='vda'/></disk>"
                "</devices></domain></domainsnapshot>"))

        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=shell_run), \
             patch.object(sm.virsh, "execute", side_effect=execute):
            assert sm._collapse_base("vm01", "s1", "vda", "/w/vm01.s3") == (
                "/w/vm01.qcow2", False, None)

        assert any(c.startswith("sudo qemu-img info --backing-chain")
                   for c in run)

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
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is False

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
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is False

        assert _chain(head) == [str(head), str(base)]
        assert all(p.exists() for p in overlays.values())
        assert memory["s2"].exists()
        assert virsh.snapshots == ["s1", "s2"]

    @needs_qemu
    def test_after_its_branch_is_deleted_the_fork_collapses_onto_its_disk(
            self, sm, tmp_path):
        # take s1, take s2, restore s1, take s3, delete s2: libvirt's delete
        # of s2 unlinks the overlay s1's take made (#289), and the head never
        # stood on it -- it stands on the revert's overlay, on the disk s1
        # was taken on. The collapse rebased onto the overlay, and stopped
        # because it was gone (#291).
        base, overlays, memory = _take_external(tmp_path, ["s1"])
        overlays["s1"].unlink()
        reverted = tmp_path / "vm01_disk.1785187700"
        _qemu("qemu-img", "create", "-q", "-f", "qcow2",
              "-b", str(base), "-F", "qcow2", str(reverted))
        _write(reverted, 5)
        head = tmp_path / "vm01_disk.s3"
        _qemu("qemu-img", "create", "-q", "-f", "qcow2",
              "-b", str(reverted), "-F", "qcow2", str(head))
        _write(head, 6)
        memory["s3"] = tmp_path / "vm01_snapshot_s3.raw"
        memory["s3"].write_bytes(b"memory image")
        virsh = _Libvirt({"s1": {"vda": str(overlays["s1"])},
                          "s3": {"vda": str(head)}},
                         {"vda": str(head)}, memory,
                         taken_on={"s1": {"vda": str(base)}})

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()), \
             patch.object(sm.logger, "info") as info:
            assert sm.collapse_to("vm01", "s1", dry_run=True) is True
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is True

        assert any(f"would rebase {head} onto {base}" in str(c.args[0])
                   for c in info.call_args_list)
        assert _chain(head) == [str(head), str(base)]
        assert all(_reads(head, era) for era in (0, 5, 6))
        assert _reads(head, 1, pattern="0")
        assert virsh.snapshots == ["s1"] and virsh.current == "s1"
        assert virsh.records["s1"] == {"vda": str(head)}
        assert not memory["s3"].exists() and memory["s1"].exists()

    def test_a_dry_run_that_would_stop_says_so_and_fails(self, sm, tmp_path):
        # as test_one_that_cannot_be_examined_where_qemu_img_runs_stops: the
        # dry run said the collapse would stop, and exited 0
        _base, overlays, memory = _take_external(
            tmp_path, ["s1", "s2"], real=False)
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(tmp_path / "vm01_disk.1785187700")},
                         memory)

        def shell(cmd, *args, **kwargs):
            if cmd.startswith("stat "):
                return _result(ok=False, stderr="stat: Permission denied")
            return _shell_that_runs()(cmd, *args, **kwargs)

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell", side_effect=shell), \
             patch.object(sm.logger, "info") as info:
            assert sm.collapse_to("vm01", "s1", dry_run=True) is False

        assert not any("would remove" in str(c.args[0])
                       for c in info.call_args_list)

    @pytest.mark.parametrize("state, built_on", [
        ("running", "/w/live.qcow2"), ("shutoff", "/w/config.qcow2")])
    def test_the_disk_taken_on_is_the_one_validation_reads(
            self, sm, state, built_on):
        # a revert of one that saved no memory builds on its
        # <inactiveDomain> (#290): the collapse reads the same definition
        xml = (f"<domainsnapshot><name>s1</name><state>{state}</state>"
               "<domain><devices><disk type='file' device='disk'>"
               "<source file='/w/live.qcow2'/><target dev='vda'/></disk>"
               "</devices></domain><inactiveDomain><devices>"
               "<disk type='file' device='disk'>"
               "<source file='/w/config.qcow2'/><target dev='vda'/></disk>"
               "</devices></inactiveDomain></domainsnapshot>")
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout=xml)):
            assert sm._source_in_snapshot_domain(
                "vm01", "s1", "vda") == built_on

    def test_the_disk_a_kept_snapshot_is_restored_onto_is_kept(
            self, sm, tmp_path):
        # metadata in which the dropped s2 records, as its overlay, the disk
        # s1 was taken on: a revert to s1 builds on that file, which
        # validation checks, so the collapse leaves it
        _base, overlays, memory = _take_external(
            tmp_path, ["s1", "s2"], real=False)
        taken_on = tmp_path / "vm01_disk.qcow2"
        virsh = _Libvirt({"s1": {"vda": str(overlays["s1"])},
                          "s2": {"vda": str(taken_on)}},
                         {"vda": str(overlays["s2"])}, memory,
                         taken_on={"s1": {"vda": str(taken_on)}})

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()), \
             patch.object(sm, "_qemu_img_rebase", return_value=True), \
             patch.object(sm.logger, "info") as info:
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is True

        assert taken_on.is_file()
        assert any("builds on it" in str(c.args[0])
                   for c in info.call_args_list)
        assert virsh.snapshots == ["s1"]

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
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is True

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
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is True

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
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is True

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
            assert sm.collapse_to("vm01", "s1",
                                  recorded_in_use=_nothing_else_records) is True

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

        # nothing but the read-only identity and presence checks
        assert all(c.startswith(("stat ", "sh -c 'if test -e "))
                   for c in calls)
        assert all(p.exists() for p in overlays.values())
        lines = [str(c.args[0]) for c in info.call_args_list]
        assert any("would remove" in line and str(overlays["s2"]) in line
                   for line in lines)
        assert any("would keep" in line and str(head) in line
                   for line in lines)
        assert not any("would remove" in line and str(head) in line
                       for line in lines)
        assert virsh.snapshots == ["s1", "s2", "s3"]


class TestWhatTheRevertDiscards:
    """Reverting to a snapshot with no children, libvirt deletes its
    overlays and records the new ones it made in their place
    (qemuSnapshotRevertExternalFinish). Every overlay the revert deleted is
    put back all the same: whether another domain needs one cannot be told
    while it is gone. One no snapshot of the VM records any more is named,
    to remove by hand."""

    @staticmethod
    def _deleted(tmp_path):
        old = tmp_path / "vm01_disk.s1"
        backup = tmp_path / "vm01_disk.s1.preserve"
        backup.write_bytes(b"the only copy")
        return old, backup

    @staticmethod
    def _warned(warning):
        return " ".join(str(c.args[0]) for c in warning.call_args_list)

    def test_one_no_snapshot_records_is_put_back_and_named(
            self, sm, tmp_path):
        old, backup = self._deleted(tmp_path)

        with _inventory(sm, {"s1": ["/new"]}, ["s1"]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()), \
             patch.object(sm.logger, "warning") as warning:
            assert sm._restore_preserved_overlays(
                [(str(old), str(backup))], vm_name="vm01") == []

        assert old.read_bytes() == b"the only copy" and not backup.exists()
        warned = self._warned(warning)
        assert str(old) in warned and "remove" in warned

    def test_one_a_snapshot_still_records_is_put_back_unnamed(
            self, sm, tmp_path):
        old, backup = self._deleted(tmp_path)

        with _inventory(sm, {"s1": ["/new"], "s2": [str(old)]},
                        ["s1", "s2"]), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()), \
             patch.object(sm.logger, "warning") as warning:
            assert sm._restore_preserved_overlays(
                [(str(old), str(backup))], vm_name="vm01") == []

        assert old.read_bytes() == b"the only copy"
        assert str(old) not in self._warned(warning)

    @pytest.mark.parametrize("error", [SnapshotError("unreadable"),
                                       BlockingIOError(11, "busy")],
                             ids=["unreadable", "raises"])
    def test_with_the_snapshots_unread_it_is_put_back_unnamed(
            self, sm, tmp_path, error):
        old, backup = self._deleted(tmp_path)

        with patch.object(sm, "_strict_overlay_inventory",
                          side_effect=error), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()), \
             patch.object(sm.logger, "warning") as warning:
            assert sm._restore_preserved_overlays(
                [(str(old), str(backup))], vm_name="vm01") == []

        assert old.read_bytes() == b"the only copy"
        assert str(old) not in self._warned(warning)

    def test_after_a_failed_revert_nothing_is_named(self, sm, tmp_path):
        old, backup = self._deleted(tmp_path)

        with patch.object(sm, "_strict_overlay_inventory") as inventory, \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()):
            assert sm._restore_preserved_overlays(
                [(str(old), str(backup))]) == []

        assert old.read_bytes() == b"the only copy"
        inventory.assert_not_called()


#: a definition's metadata the way libvirt keeps it: prefixed elements whose
#: namespaces are declared on them, which an ElementTree round trip of the
#: whole document renames
_METADATA = (
    "<metadata><libosinfo:libosinfo xmlns:libosinfo="
    "'http://libosinfo.org/xmlns/libvirt/domain/1.0'>"
    "<libosinfo:os id='http://nixos.org/nixos/unknown'/>"
    "</libosinfo:libosinfo><boxman:disks xmlns:boxman="
    "'https://github.com/mherkazandjian/boxman/disks/1.0'>"
    "<boxman:disk name='root' target='vda' role='boot' source='x'/>"
    "</boxman:disks></metadata>")


class _RevertingLibvirt:
    """A ``virsh`` double for one domain, vm01 — a boot disk ``vda`` and a
    CDROM ``sda`` on an ISO — that does what libvirt 10.10's external
    revert does, as measured on a NixOS live-ISO guest (#273):

    * every disk with media gets a new qcow2 overlay, the CDROM included
      whatever the snapshot says of it: ``<source without its suffix>.
      <epoch>``, beside the disk's source in the snapshot's saved
      definition, the epoch one later at each revert
      (``qemuSnapshotRevertExternalPrepare``);
    * the current snapshot's recorded overlays are deleted, its
      ``<revertDisks>`` if it has any, else its ``<disks>``; and the new
      ones are recorded as the reverted-to snapshot's, under
      ``<revertDisks>`` when it has children, else as its ``<disks>``
      (``qemuSnapshotRevertExternalFinish``). A source in the snapshot's
      saved definition that is gone, or a layer under it, fails the revert
      before anything is made;
    * both definitions move onto them. The live one records each chain,
      the persistent one none. A snapshot of the shut-off VM is disk-only,
      and reverting to it leaves the VM shut off;
    * a take clears the current snapshot's ``<revertDisks>`` before the new
      one becomes current (``qemuSnapshotClearRevertdisks`` in
      ``qemuSnapshotCreate``, #287);
    * ``snapshot-list`` lists the snapshots by name, as virsh sorts them,
      and ``snapshot-current`` gives the current one's XML;
    * a domain the revert started does not know its CDROM has a tray: a
      live media change fails "Tray of device 'sata0-0-0' is not open"
      unless the tray was opened through QMP first. A live change that
      puts an image behind the wrong driver type fails as qemu fails it;
    * its own delete of the current snapshot, a leaf, takes the domain's
      disk for the snapshot's, not what the snapshot records, and
      block-commits it into the image under it, which the domain then reads
      (``qemuSnapshotExternalGetSnapDiskSrc``,
      ``qemuSnapshotDeleteExternalPrepareData``). It fails "failed to find
      parent disk source in backing chain" where that disk is built on
      nothing, and "snapshot VM disk source and parent disk source are not
      the same" where the image under it is not the disk the snapshot was
      taken on: the shape a collapse left on a line of takes, the head on
      the target's overlay. Of one that is not current (#289):

      - a leaf merges nothing and deletes the image its saved definition is
        built on, where an older snapshot records that disk
        (``qemuSnapshotDiscardExternal``), though that older one's
        ``<disks>`` still name it;
      - one with a child, in the active chain, merges the image the child
        was taken on into the one it was taken on itself
        (``qemuSnapshotExternalGetSnapDiskSrc``), never reading its own
        ``<disks>``: the image above in the domain's chain, and the saved
        disks of its descendants out of that chain that were built on it,
        are rebased (``qemuSnapshotGetDisksWithBackingStore``), and the
        saved definitions naming it name the image under it
        (``qemuSnapshotDeleteUpdateDisks``);
      - one with more children, or out of the active chain, is refused
        (``qemuSnapshotDeleteValidate``).

      Either way its children become its parent's, the parent's
      ``<revertDisks>``, if it has any, become its ``<disks>``
      (``qemuSnapshotDeleteUpdateParent``), and a current one leaves its
      parent current (``qemuSnapshotDiscardMetadata``). It unlinks the
      memory file the snapshot records, and not the ``.raw.zst`` that
      ``--compress-memory`` leaves in its place, which it does not know of
      (measured on libvirt 10.10, #274). With :attr:`native_delete` off it
      refuses external snapshots, as libvirt before 9.0 did, and
      ``blockcommit`` is run; with :attr:`fail_delete` or
      :attr:`fail_metadata_delete` the delete fails with that error;
    * a take of the running VM saves its memory where ``--memspec`` says,
      in the workdir when the double is driven by hand, and the snapshot
      records that file; one of the shut-off VM records none;
    * ``define`` sets the persistent definition's disks, and
      ``qemu-img rebase`` the chain in :attr:`backing`, as a collapse does;
    * QMP ``query-block`` tells what qemu holds in the drive
      (:attr:`medium`): a live change that fails with the tray open takes
      the medium out first when :attr:`fail_after_removal` says so.

    Its files are real, under *root*; their chains are in :attr:`backing`,
    which ``qemu-img`` (:meth:`qemu_img`) reads. With *data_disk* it has a
    second disk, ``vdb``; with *disk_dir* a take puts the overlays there,
    as a ``disk_snapshot_dir`` does.
    """

    VM = "vm01"
    EPOCH = 1793626526

    def __init__(self, root: Path, state: str = "running",
                 data_disk: bool = False, disk_dir: Path | None = None):
        self.root = root
        self.isodir = root / "image-cache"
        self.workdir = root / "workdir"
        self.isodir.mkdir()
        self.workdir.mkdir()
        self.disk_dir = disk_dir
        if disk_dir is not None:
            disk_dir.mkdir()
        self.iso = str(self.isodir / "nixos-minimal.iso")
        Path(self.iso).write_bytes(b"the ISO")
        boot = str(self.workdir / "vm01.qcow2")
        Path(boot).write_bytes(b"boot disk")
        #: image -> (format, the image under it or None)
        self.backing: dict[str, tuple[str, str | None]] = {
            self.iso: ("raw", None), boot: ("qcow2", None)}
        self.live = {"vda": boot, "sda": self.iso}
        if data_disk:
            data = str(self.workdir / "vm01_data.qcow2")
            Path(data).write_bytes(b"data disk")
            self.backing[data] = ("qcow2", None)
            self.live["vdb"] = data
        self.config = dict(self.live)
        self.active = state == "running"
        self.paused = False
        self.tray_known = True
        self.tray_open = False
        self.snapshots: dict[str, dict] = {}
        self.current: str | None = None
        self.epoch = self.EPOCH
        self.calls: list[tuple] = []
        #: (scope flags, driver type, whether the guest was paused) per
        #: update-device that worked
        self.updates: list[tuple] = []
        #: the XML each redefine was given
        self.redefined: list[str] = []
        self.fail_update = None
        self.fail_redefine = None
        #: (target, the overlay, the image it was committed into)
        self.commits: list[tuple] = []
        self.native_delete = True
        #: the error its own delete, or a ``--metadata`` one, fails with
        self.fail_delete = None
        self.fail_metadata_delete = None
        #: what qemu holds in sda, None for an empty drive
        self.medium: str | None = self.iso
        self.fail_after_removal = None
        self.managed_save = False
        #: the error ``snapshot-create-as`` fails with, if any
        self.fail_take = None
        #: every shell command :meth:`qemu_img` was handed
        self.shell: list[str] = []

    # --- what boxman did before -------------------------------------------

    def take(self, name: str, memory: str | None = None) -> None:
        """``snapshot take``: the CDROM left out (``snapshot=no``), every
        other disk on a new overlay, named as libvirt names it, beside the
        disk or in :attr:`disk_dir`; the domain saved as it is now, and
        disk-only when it is shut off, else its memory in *memory* (the
        ``--memspec`` file) or the workdir. The current snapshot's
        ``<revertDisks>`` are cleared first, as libvirt clears them."""
        saved = dict(self.live if self.active else self.config)
        disks: dict[str, str | None] = {}
        for target, source in saved.items():
            if target == "sda":
                disks[target] = None
                continue
            overlay = os.path.join(
                str(self.disk_dir or os.path.dirname(source)),
                SnapshotManager._overlay_name(source, name))
            Path(overlay).write_bytes(f"{name}'s overlay of {target}".encode())
            self.backing[overlay] = ("qcow2", source)
            disks[target] = overlay
        if self.current in self.snapshots:
            self.snapshots[self.current]["revert_disks"] = None
        if self.active:
            memory = memory or str(self.workdir / f"vm01_snapshot_{name}.raw")
            Path(memory).write_bytes(f"{name}'s memory".encode())
        else:
            memory = None
        self.snapshots[name] = {
            "parent": self.current, "disks": disks, "revert_disks": None,
            "domain": saved, "memory": memory,
            "state": "running" if self.active else "shutoff"}
        for target, overlay in disks.items():
            if overlay:
                self.live[target] = self.config[target] = overlay
        self.current = name

    def disks_now(self) -> dict:
        """The definition the domain runs on: the live one when it is
        active, else the persistent one."""
        return self.live if self.active else self.config

    def files_beside_the_iso(self) -> list[str]:
        return sorted(p.name for p in self.isodir.iterdir())

    def in_use(self, others=()):
        """``disk_paths_in_use``: both definitions' sources and their
        chains, and *others*, another domain's."""
        used = FilesInUse()
        for source in [*(self.live.values() if self.active else ()),
                       *self.config.values()]:
            path = source
            while path is not None:
                used.setdefault(path, self.VM)
                path = self.backing.get(path, (None, None))[1]
        for path in others:
            used[path] = "other01"
        return used

    def compress(self, name: str) -> str:
        """What ``--compress-memory`` leaves of snapshot *name*'s memory:
        the ``.raw.zst``, the ``.raw`` gone, the record unchanged."""
        raw = Path(self.snapshots[name]["memory"])
        zst = Path(f"{raw}.zst")
        zst.write_bytes(b"zstd " + raw.read_bytes())
        raw.unlink()
        return str(zst)

    def recorded(self, others=None) -> FilesInUse:
        """``memory_images_recorded``: the memory image each snapshot
        records, with the ``.raw.zst`` beside it, by resolved path and by
        identity, as the scan of every domain maps them -- and *others*,
        ``{path: domain}``, what another domain's snapshots record."""
        recorded = FilesInUse()
        mine = [(path, self.VM) for snap in self.snapshots.values()
                if snap.get("memory")
                for path in (snap["memory"], f"{snap['memory']}.zst")]
        for path, domain in [*mine, *(others or {}).items()]:
            recorded.setdefault(os.path.realpath(path), domain)
            if os.path.exists(path):
                st = os.stat(path)
                recorded.identities.setdefault((st.st_dev, st.st_ino), domain)
        return recorded

    def memory_files(self) -> list[str]:
        """Every memory image under :attr:`root`, either form."""
        return sorted(str(p) for p in self.root.rglob("*_snapshot_*.raw*"))

    # --- qemu-img -----------------------------------------------------------

    def qemu_img(self, command: str, *_a, **_kw):
        """``execute_shell``: ``qemu-img info --backing-chain`` from
        :attr:`backing`; anything else is run."""
        self.shell.append(command)
        words = shlex.split(command)
        if "rebase" in words:
            # qemu-img rebase -p -b <base> -F qcow2 <head>
            self.backing[words[-1]] = ("qcow2", words[words.index("-b") + 1])
            return _result()
        if not command.startswith("qemu-img info"):
            proc = subprocess.run(command, shell=True, capture_output=True,
                                  text=True)
            return _result(ok=proc.returncode == 0, stdout=proc.stdout,
                           stderr=proc.stderr, return_code=proc.returncode)
        self.calls.append(("qemu-img",))
        path = shlex.split(command)[-1]
        chain = []
        while path is not None:
            if path not in self.backing or not os.path.exists(path):
                return _result(ok=False, stderr=f"Could not open '{path}'")
            fmt, below = self.backing[path]
            chain.append({"filename": path, "format": fmt})
            path = below
        return _result(stdout=json.dumps(chain))

    # --- virsh --------------------------------------------------------------

    def _chain_xml(self, path: str) -> str:
        fmt, below = self.backing[path]
        if below is None:
            return "<backingStore/>"
        return (f"<backingStore type='file'><format type='"
                f"{self.backing[below][0]}'/><source file='{below}'/>"
                f"{self._chain_xml(below)}</backingStore>")

    def _disk_xml(self, target: str, path: str, live: bool) -> str:
        cdrom = target == "sda"
        return (f"<disk type='file' device='{'cdrom' if cdrom else 'disk'}'>"
                f"<driver name='qemu' type='{self.backing[path][0]}'/>"
                f"<source file='{path}'/>"
                + (self._chain_xml(path) if live else "")
                + f"<target dev='{target}' bus='{'sata' if cdrom else 'virtio'}'/>"
                + ("<readonly/>" if cdrom else "")
                + ("<alias name='sata0-0-0'/>" if cdrom and live else "")
                + "</disk>")

    def _domain_xml(self, disks: dict, live: bool, tag: str = "domain") -> str:
        devices = "".join(self._disk_xml(t, p, live) for t, p in disks.items())
        return (f"<{tag} type='kvm'><name>{self.VM}</name>{_METADATA}"
                f"<devices>{devices}</devices></{tag}>")

    def snapshot_xml(self, name: str) -> str:
        snap = self.snapshots[name]

        def recorded(disks):
            return "".join(
                f"<disk name='{t}' snapshot='external' type='file'>"
                f"<driver type='qcow2'/><source file='{p}'/></disk>"
                if p else f"<disk name='{t}' snapshot='no'/>"
                for t, p in disks.items())
        return (f"<domainsnapshot><name>{name}</name>"
                f"<state>{snap.get('state', 'running')}</state>"
                + (f"<parent><name>{snap['parent']}</name></parent>"
                   if snap["parent"] else "")
                + "<creationTime>1785187648</creationTime>"
                + (f"<memory snapshot='external' file='{snap['memory']}'/>"
                   if snap.get("memory") else "<memory snapshot='no'/>")
                + f"<disks>{recorded(snap['disks'])}</disks>"
                + (f"<revertDisks>{recorded(snap['revert_disks'])}"
                   f"</revertDisks>" if snap["revert_disks"] else "")
                + self._domain_xml(snap["domain"], live=False)
                + "</domainsnapshot>")

    def __call__(self, *args, **_kw):
        self.calls.append(args)
        verb = args[0]
        if verb == "list" and "--with-managed-save" in args:
            return _result(stdout=f"{self.VM}\n\n" if self.managed_save
                           else "\n")
        if verb == "list" and "--inactive" in args:
            return _result(stdout="\n" if self.active else f"{self.VM}\n\n")
        if verb == "list":
            running = self.active and ("--state-running" not in args
                                       or not self.paused)
            return _result(stdout=f"{self.VM}\n\n" if running else "\n")
        if verb == "suspend":
            self.paused = True
            return _result()
        if verb == "resume":
            self.paused = False
            return _result()
        if verb == "dumpxml":
            live = self.active and "--inactive" not in args
            return _result(stdout=self._domain_xml(
                self.live if live else self.config, live))
        if verb == "snapshot-list":
            # by name, as virsh sorts them without --topological
            names = sorted(self.snapshots, key=str.lower)
            return _result(stdout="".join(f"{n}\n" for n in names) + "\n")
        if verb == "snapshot-current":
            if self.current is None:
                # libvirt 10.10's own words
                return _result(ok=False, stderr=(
                    f"error: domain '{self.VM}' has no current snapshot"))
            return _result(stdout=f"{self.current}\n" if "--name" in args
                           else self.snapshot_xml(self.current) + "\n")
        if verb == "snapshot-info":
            return _result() if args[2] in self.snapshots else _result(
                ok=False, stderr="error: Domain snapshot not found")
        if verb == "snapshot-dumpxml":
            return _result(stdout=self.snapshot_xml(args[2]))
        if verb == "snapshot-revert":
            return self._revert(args[2])
        if verb == "snapshot-create":
            assert "--redefine" in args and "--current" not in args
            return self._redefine(Path(args[2]).read_text())
        if verb == "update-device":
            return self._update(ET.parse(args[2]).getroot(), args[3:])
        if verb == "define":
            for disk in ET.parse(args[1]).getroot().iter("disk"):
                self.config[disk.find("target").get("dev")] = (
                    disk.find("source").get("file"))
            return _result()
        if verb == "snapshot-delete":
            return self._delete(args[2], metadata="--metadata" in args)
        if verb == "domblklist":
            return _result(stdout=(
                " Type   Device   Target   Source\n" + "-" * 40 + "\n"
                + "".join(f" file   {'cdrom' if t == 'sda' else 'disk'}"
                          f"     {t}      {p}\n"
                          for t, p in self.disks_now().items())
                + "\n"))
        if verb == "blockcommit":
            self._commit(args[2], self.disks_now()[args[2]])
            return _result()
        if verb == "snapshot-create-as":
            if self.fail_take:
                return _result(ok=False, stderr=self.fail_take)
            memspec = next((a.split("=", 1)[1] for a in args
                            if a.startswith("--memspec=")), None)
            self.take(args[args.index("--name") + 1], memory=memspec)
            return _result()
        if verb == "qemu-monitor-command" and "query-block" in args[2]:
            inserted = {"inserted": {"file": self.medium}} if self.medium else {}
            return _result(stdout=json.dumps({"return": [
                {"device": "", "qdev": "sata0-0-0",
                 "tray_open": self.tray_open, **inserted}]}))
        if verb == "qemu-monitor-command":
            command = json.loads(args[2])
            assert command["arguments"]["id"] == "sata0-0-0"
            self.tray_open = command["execute"] == "blockdev-open-tray"
            return _result(stdout='{"return":{},"id":"libvirt-487"}')
        raise AssertionError(f"unexpected virsh call: {args}")

    def _missing_from_chain(self, path: str) -> str | None:
        """The first image of *path*'s chain, itself first, that is gone."""
        while path is not None:
            if not os.path.exists(path):
                return path
            path = self.backing.get(path, (None, None))[1]
        return None

    def _revert(self, name: str):
        snap = self.snapshots[name]
        for target, source in snap["domain"].items():
            missing = self._missing_from_chain(source)
            if missing == source:
                return _result(ok=False, stderr=(
                    f"error: Failed to revert snapshot {name}\nerror: "
                    f"unsupported configuration: source for disk '{target}' "
                    f"is not a regular file; refusing to generate external "
                    f"snapshot name"))
            if missing:
                return _result(ok=False, stderr=(
                    f"error: Failed to revert snapshot {name}\nerror: "
                    f"internal error: process exited while connecting to "
                    f"monitor: Could not open backing file: Could not open "
                    f"'{missing}': No such file or directory"))
        self.epoch += 1
        made = {}
        for target, source in snap["domain"].items():
            overlay = f"{os.path.splitext(source)[0]}.{self.epoch}"
            Path(overlay).write_bytes(b"qcow2 overlay")
            self.backing[overlay] = ("qcow2", source)
            made[target] = overlay
        current = self.snapshots.get(self.current)
        if current is not None:
            for path in (current["revert_disks"] or current["disks"]).values():
                if path:
                    Path(path).unlink(missing_ok=True)
            current["revert_disks"] = None
        if any(s["parent"] == name for s in self.snapshots.values()):
            snap["revert_disks"] = dict(made)
        else:
            snap["disks"] = dict(made)
        self.live, self.config = dict(made), dict(made)
        self.medium = made.get("sda")
        self.current = name
        # a disk-only snapshot is reverted to shut off
        self.active = snap.get("state", "running") == "running"
        self.paused = False
        self.tray_known, self.tray_open = False, False
        return _result()

    def _commit(self, target: str, overlay: str) -> None:
        below = self.backing[overlay][1]
        self.commits.append((target, overlay, below))
        self.live[target] = self.config[target] = below

    def _ancestors(self, name: str | None) -> list[str]:
        """*name*'s parent, its parent's, and so on."""
        found = []
        while name is not None and self.snapshots[name]["parent"]:
            name = self.snapshots[name]["parent"]
            found.append(name)
        return found

    def _descendants(self, name: str) -> list[str]:
        return [n for n in self.snapshots if name in self._ancestors(n)]

    def _chain(self, path: str | None) -> list[str]:
        """*path* and the images under it."""
        chain = []
        while path is not None:
            chain.append(path)
            path = self.backing.get(path, (None, None))[1]
        return chain

    def _records_disk_above(self, name: str, target: str) -> bool:
        """Whether a snapshot older than *name* records *target* as
        external (``qemuSnapshotFindParentSnapForDisk``)."""
        return any(self.snapshots[a]["disks"].get(target)
                   for a in self._ancestors(name))

    def _refusal(self, name: str, children: list[str]) -> str | None:
        """Why libvirt refuses to delete the external snapshot *name*, if
        it does (``qemuSnapshotDeleteValidate``)."""
        if children and name == self.current:
            return ("deletion of active external snapshot that is not a "
                    "leaf snapshot is not supported")
        if children and name not in self._ancestors(self.current):
            return ("deletion of non-leaf external snapshot that is not in "
                    "active chain is not supported")
        if len(children) > 1:
            return ("deletion of external disk snapshot with multiple "
                    "children snapshots not supported")
        return None

    def _merge(self, name: str, target: str, top: str, below: str) -> None:
        """Delete *name*, not current, by committing *top*, the image its
        child was taken on, into *below*, the one it was taken on, and
        deleting *top* (``qemuSnapshotDiscardExternal``)."""
        self.commits.append((target, top, below))
        chain = self._chain(self.disks_now()[target])
        above = chain.index(top) - 1
        rebased = {chain[above]} if above >= 0 else set()
        active = {self.current, *self._ancestors(self.current)}
        for other in self._descendants(name):
            saved = self.snapshots[other]["domain"].get(target)
            if other not in active and self.backing.get(
                    saved, (None, None))[1] == top:
                rebased.add(saved)
        for image in rebased:
            self.backing[image] = (self.backing[image][0], below)
        for other in self._descendants(name):
            if self.snapshots[other]["domain"].get(target) == top:
                self.snapshots[other]["domain"][target] = below
        Path(top).unlink(missing_ok=True)

    def _delete(self, name: str, metadata: bool):
        snap = self.snapshots[name]
        children = [n for n, s in self.snapshots.items() if s["parent"] == name]
        records = {t: p for t, p in snap["disks"].items() if p}
        if not metadata and self.fail_delete:
            return _result(ok=False, stderr=self.fail_delete)
        if metadata and self.fail_metadata_delete:
            return _result(ok=False, stderr=self.fail_metadata_delete)
        if not metadata and not self.native_delete:
            return _result(ok=False, stderr=(
                "error: unsupported configuration: deletion of 1 "
                "external disk snapshots not supported yet"))
        refusal = None if metadata else self._refusal(name, children)
        if refusal:
            return _result(ok=False, stderr=(
                f"error: Failed to delete snapshot {name}\nerror: "
                f"unsupported configuration: {refusal}"))
        if not metadata and children:
            # what its one child was taken on, not what it records
            child = self.snapshots[children[0]]
            merges = []
            for target in records:
                top, below = child["domain"][target], snap["domain"][target]
                if top not in self._chain(self.disks_now()[target]):
                    return _result(ok=False, stderr=(
                        f"error: invalid argument: could not find image "
                        f"'{top}' in chain for '{self.disks_now()[target]}'"))
                if self.backing[top][1] != below:
                    return _result(ok=False, stderr=(
                        "error: operation failed: snapshot VM disk source "
                        "and parent disk source are not the same"))
                merges.append((target, top, below))
            for target, top, below in merges:
                self._merge(name, target, top, below)
        elif not metadata and name == self.current:
            for target in records:
                below = self.backing.get(self.disks_now()[target],
                                         (None, None))[1]
                if below is None:
                    return _result(ok=False, stderr=(
                        f"error: Failed to delete snapshot {name}\nerror: "
                        f"operation failed: failed to find parent disk source "
                        f"in backing chain"))
                if below != snap["domain"][target]:
                    return _result(ok=False, stderr=(
                        f"error: Failed to delete snapshot {name}\nerror: "
                        f"operation failed: snapshot VM disk source and "
                        f"parent disk source are not the same"))
            for target in records:
                head = self.disks_now()[target]
                self._commit(target, head)
                Path(head).unlink(missing_ok=True)
        elif not metadata:
            # a revert away from it deleted its overlays, as libvirt sees
            # it: what is left of it is the image its definition is built on
            for target in records:
                if self._records_disk_above(name, target):
                    Path(snap["domain"][target]).unlink(missing_ok=True)
        if not metadata and snap.get("memory"):
            # the file the record names; a .raw.zst beside it is not one
            Path(snap["memory"]).unlink(missing_ok=True)
        for child in children:
            self.snapshots[child]["parent"] = snap["parent"]
        parent = self.snapshots.get(snap["parent"])
        if parent is not None and parent["revert_disks"]:
            parent["disks"], parent["revert_disks"] = (
                parent["revert_disks"], None)
        del self.snapshots[name]
        if name == self.current:
            self.current = snap["parent"]
        return _result()

    def _redefine(self, xml_text: str):
        if self.fail_redefine:
            return _result(ok=False, stderr=self.fail_redefine)
        self.redefined.append(xml_text)
        root = ET.fromstring(xml_text)
        snap = self.snapshots[root.findtext("name")]

        def recorded(section):
            return {d.get("name"): (d.find("source").get("file")
                                    if d.get("snapshot") == "external"
                                    else None)
                    for d in section.findall("disk")}
        snap["disks"] = recorded(root.find("disks"))
        if root.find("revertDisks") is not None:
            snap["revert_disks"] = recorded(root.find("revertDisks"))
        snap["domain"] = {
            d.find("target").get("dev"): d.find("source").get("file")
            for d in root.find("domain").iter("disk")}
        return _result()

    def _update(self, disk, flags):
        target = disk.find("target").get("dev")
        source = disk.find("source").get("file")
        driver = disk.find("driver").get("type")
        if self.fail_update:
            return _result(ok=False, stderr=self.fail_update)
        if "--live" in flags:
            if not self.active:
                return _result(ok=False, stderr="error: domain is not running")
            if not self.tray_known and not self.tray_open:
                return _result(ok=False, stderr=(
                    "error: Failed to update device from x.xml\nerror: "
                    "internal error: unable to execute QEMU command "
                    "'blockdev-remove-medium': Tray of device 'sata0-0-0' "
                    "is not open"))
            if self.tray_open and self.fail_after_removal:
                self.medium = None
                return _result(ok=False, stderr=self.fail_after_removal)
            if driver != self.backing[source][0]:
                return _result(ok=False, stderr=(
                    "error: internal error: unable to execute QEMU command "
                    "'blockdev-add': Image is not in qcow2 format"))
            self.live[target] = source
            self.medium = source
            self.tray_open = False
        if "--config" in flags:
            self.config[target] = source
        self.updates.append((tuple(flags), driver, self.paused))
        return _result()


def _restoring(sm: SnapshotManager, virsh: _RevertingLibvirt):
    """The patches that route a restore through *virsh*."""
    # on the classes: the CDROMManager the restore makes has a virsh of its own
    return (patch.object(VirshCommand, "execute", side_effect=virsh),
            patch.object(LibVirtCommandBase, "execute_shell",
                         side_effect=virsh.qemu_img))


def _restore(sm: SnapshotManager, virsh: _RevertingLibvirt, name: str,
             others=()) -> bool:
    execute, shell = _restoring(sm, virsh)
    with execute, shell:
        return sm.snapshot_restore(
            virsh.VM, name, paths_in_use=lambda: virsh.in_use(others))


class TestARestorePutsTheCdromBackOnItsIso:
    """#273: reverting an external snapshot, libvirt builds a qcow2 overlay
    of the CDROM's ISO beside the ISO, although the snapshot left the CDROM
    out, and moves the CDROM onto it. After the restore the CDROM reads its
    ISO again and nothing is left beside the ISO."""

    @pytest.fixture
    def vm(self, tmp_path: Path) -> _RevertingLibvirt:
        virsh = _RevertingLibvirt(tmp_path)
        virsh.take("s1")
        virsh.take("s2")
        return virsh

    def test_the_cdrom_reads_its_iso_again_in_both_definitions(self, sm, vm):
        assert _restore(sm, vm, "s1") is True

        assert vm.live["sda"] == vm.config["sda"] == vm.iso
        # behind a raw driver, live and persistent in one call
        assert vm.updates == [(("--live", "--config", "--force"), "raw",
                               True)]
        # the boot disk stays on the overlay the revert made
        assert vm.live["vda"].endswith(f".{vm.epoch}")

    def test_nothing_is_left_beside_the_iso_after_restores_in_a_row(
            self, sm, vm):
        for name in ("s1", "s2", "s2", "s1"):
            assert _restore(sm, vm, name) is True
            assert vm.files_beside_the_iso() == ["nixos-minimal.iso"]
            assert vm.live["sda"] == vm.config["sda"] == vm.iso
        assert Path(vm.iso).read_bytes() == b"the ISO"

    def test_no_snapshot_names_the_overlay_it_made(self, sm, vm):
        # s1 has a child: its record goes under <revertDisks>; s2 has none:
        # its <disks> are replaced
        for name in ("s1", "s2"):
            assert _restore(sm, vm, name) is True
            snap = vm.snapshots[name]
            records = (snap["revert_disks"] if name == "s1"
                       else snap["disks"])
            assert records["sda"] is None
            assert records["vda"] == vm.live["vda"]
            execute, shell = _restoring(sm, vm)
            with execute, shell:
                assert sm.validate_snapshot(vm.VM, name) == (True, [])
        # still the current one: redefined without --current
        assert vm.current == "s2"

    def test_the_rest_of_the_record_goes_back_as_libvirt_wrote_it(
            self, sm, vm):
        before = vm.snapshot_xml("s2")
        assert _restore(sm, vm, "s2") is True

        [xml] = vm.redefined
        assert "<libosinfo:os id='http://nixos.org/nixos/unknown'/>" in xml
        assert "<boxman:disk name='root'" in xml
        assert "ns0:" not in xml
        # the boot disk's record exactly as libvirt wrote it
        assert (f"<disk name='vda' snapshot='external' type='file'>"
                f"<driver type='qcow2'/><source file='{vm.live['vda']}'/>"
                f"</disk>") in xml
        assert xml.count("<disk ") == before.count("<disk ")

    def test_libvirts_tray_it_does_not_know_is_opened_through_qmp(
            self, sm, vm):
        assert _restore(sm, vm, "s1") is True

        verbs = [c[0] for c in vm.calls]
        first = verbs.index("update-device")
        # the guest paused across it, and running again after
        assert verbs[first - 1:first + 4] == [
            "suspend", "update-device", "qemu-monitor-command",
            "update-device", "resume"]
        assert vm.paused is False and vm.tray_open is False

    @staticmethod
    def _switching_after_a_revert(sm, vm, update):
        """Switch sda of a domain a revert started, with update-device
        answered by *update* (the double's own, wrapped)."""
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            vm._revert("s1")
            cdroms = CDROMManager(vm.VM, provider_config=sm.provider_config)
            disk = cdroms.cdrom_elements()["sda"]
            vm._update = update(vm._update)
            return sm._switch_cdrom_media(
                vm.VM, cdroms, "sda", disk, vm.iso, ["--live"], "sata0-0-0")

    def test_a_change_that_still_fails_closes_the_tray_again(self, sm, vm):
        """The medium is still in: the tray this opened is closed over it."""
        def refusing_an_open_tray_too(update):
            def refuse(disk, flags):
                if vm.tray_open:
                    return _result(ok=False, stderr="error: Permission denied")
                return update(disk, flags)
            return refuse

        ok, error, empty = self._switching_after_a_revert(
            sm, vm, refusing_an_open_tray_too)

        assert ok is False and empty is False and "Permission denied" in error
        assert vm.tray_open is False and vm.paused is False
        assert vm.medium == vm.live["sda"] != vm.iso

    def test_one_that_fails_with_the_medium_out_tries_the_iso_again(
            self, sm, vm):
        """libvirt takes the old medium out before it puts the new one in."""
        vm.fail_after_removal = "error: 'blockdev-add' failed"
        tries = []

        def once(update):
            def fail_once(disk, flags):
                tries.append(vm.tray_open)
                if len(tries) == 3:
                    vm.fail_after_removal = None
                return update(disk, flags)
            return fail_once

        ok, error, empty = self._switching_after_a_revert(sm, vm, once)

        # refused shut, refused open after taking the medium out, then in
        assert tries == [False, True, True]
        assert (ok, error, empty) == (True, "", False)
        assert vm.medium == vm.live["sda"] == vm.iso

    def test_a_drive_left_empty_is_said_to_be(self, sm, vm):
        vm.fail_after_removal = "error: 'blockdev-add' failed"

        ok, error, empty = self._switching_after_a_revert(
            sm, vm, lambda update: update)

        assert ok is False and empty is True
        assert "EMPTY" in error and "power cycle" in error
        assert vm.medium is None
        # left open, for the medium to go back in
        assert vm.tray_open is True and vm.paused is False

    def test_a_change_that_raises_still_closes_the_tray_and_resumes(
            self, sm, vm):
        def raising_when_open(update):
            def change(disk, flags):
                if vm.tray_open:
                    raise OSError("no space left for the temporary file")
                return update(disk, flags)
            return change

        with pytest.raises(OSError):
            self._switching_after_a_revert(sm, vm, raising_when_open)

        assert vm.tray_open is False and vm.paused is False

    def test_a_stopped_vm_has_its_persistent_definition_switched_alone(
            self, sm, vm):
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            vm._revert("s2")
            vm.active = False
            sm._put_cdroms_back_on_their_isos(vm.VM, paths_in_use=vm.in_use)

        assert vm.config["sda"] == vm.iso
        assert vm.updates == [(("--config",), "raw", False)]
        # its chain is not in the persistent definition: qemu-img read it
        assert ("qemu-img",) in vm.calls
        assert "suspend" not in [c[0] for c in vm.calls]
        assert vm.files_beside_the_iso() == ["nixos-minimal.iso"]

    def test_stacked_overlays_are_removed_top_first(self, sm, vm, tmp_path):
        """A snapshot taken while the CDROM was on a revert's overlay saved
        it there, and a revert to it stacks the next overlay on that one."""
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            vm._revert("s2")          # sda on <iso>.<e1>, as before #273
        under = vm.live["sda"]
        vm.take("s3")                 # saves sda on <iso>.<e1>
        removed = []
        real_remove = sm._remove_file
        with patch.object(sm, "_remove_file",
                          side_effect=lambda p: removed.append(p)
                          or real_remove(p)):
            assert _restore(sm, vm, "s3") is True
        top = f"{os.path.splitext(vm.iso)[0]}.{vm.epoch}"

        assert vm.live["sda"] == vm.config["sda"] == vm.iso
        assert removed.index(top) < removed.index(under)
        assert vm.files_beside_the_iso() == ["nixos-minimal.iso"]
        # no revert to s3 opens the overlay any more
        assert vm.snapshots["s3"]["domain"]["sda"] == vm.iso
        assert _restore(sm, vm, "s3") is True
        assert vm.files_beside_the_iso() == ["nixos-minimal.iso"]

    def test_one_another_domain_uses_is_kept_with_what_is_under_it(
            self, sm, vm):
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            vm._revert("s2")
        under = vm.live["sda"]
        vm.take("s3")
        top = f"{os.path.splitext(vm.iso)[0]}.{vm.epoch + 1}"

        assert _restore(sm, vm, "s3", others=[top]) is True

        assert vm.live["sda"] == vm.iso
        assert os.path.exists(top) and os.path.exists(under)

    def test_a_cdrom_overlay_is_not_backed_up_beside_the_iso(self, sm, vm):
        """A restore before #273 left the CDROM on an overlay that the
        current snapshot records: the backup copied it beside the ISO, and
        the put-back brought it back for good."""
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            vm._revert("s2")
        old = vm.live["sda"]
        commands = []
        real = vm.qemu_img

        def recording(command, *a, **kw):
            commands.append(command)
            return real(command, *a, **kw)
        with patch.object(VirshCommand, "execute", side_effect=vm), \
             patch.object(LibVirtCommandBase, "execute_shell",
                          side_effect=recording):
            assert sm.snapshot_restore(vm.VM, "s2",
                                       paths_in_use=vm.in_use) is True

        assert not any(str(vm.isodir) in c for c in commands
                       if c.startswith(("cp ", "sudo cp", "mv ", "sudo mv")))
        assert not os.path.exists(old)
        assert vm.files_beside_the_iso() == ["nixos-minimal.iso"]

    def test_a_switch_that_fails_leaves_the_overlay_but_no_record_of_it(
            self, sm, vm):
        """No record of a CDROM overlay is needed, and libvirt's delete
        commits the one a record names into the ISO."""
        vm.fail_update = "error: Failed to update device"

        assert _restore(sm, vm, "s1") is True

        overlay = vm.live["sda"]
        assert overlay != vm.iso and os.path.exists(overlay)
        assert vm.snapshots["s1"]["revert_disks"]["sda"] is None

    def test_after_a_switch_that_failed_another_restore_leaves_it_valid(
            self, sm, vm):
        """The backup leaves the overlay the CDROM is on to the revert,
        which deletes it and keeps the record that names it."""
        vm.fail_update = "error: Failed to update device"
        assert _restore(sm, vm, "s2") is True
        vm.fail_update = None

        assert _restore(sm, vm, "s1") is True

        execute, shell = _restoring(sm, vm)
        with execute, shell:
            assert sm.validate_snapshot(vm.VM, "s2") == (True, [])
        assert vm.snapshots["s2"]["disks"]["sda"] is None
        assert vm.files_beside_the_iso() == ["nixos-minimal.iso"]

    def test_a_restore_after_one_made_before_this_fix_leaves_every_snapshot_valid(
            self, sm, vm):
        """take s1, take s2, restore s1, restore s2 without #273: s2
        records the CDROM's overlay. A restore of s1 now leaves it to the
        revert, which deletes it and keeps s2's record of it -- and the
        restore of s2 is then refused for an overlay missing."""
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            vm._revert("s1")
            vm._revert("s2")
        stale = vm.snapshots["s2"]["disks"]["sda"]
        assert stale and os.path.exists(stale)

        assert _restore(sm, vm, "s1") is True

        execute, shell = _restoring(sm, vm)
        with execute, shell:
            assert sm.validate_snapshot(vm.VM, "s2") == (True, [])
        assert not os.path.exists(stale)
        assert vm.files_beside_the_iso() == ["nixos-minimal.iso"]
        assert _restore(sm, vm, "s2") is True
        assert vm.files_beside_the_iso() == ["nixos-minimal.iso"]

    def test_a_file_beside_the_iso_libvirt_did_not_name_is_left_alone(
            self, sm, vm):
        """Only ``<stem>.<epoch>``, as libvirt names a revert's overlay:
        a ``<stem>.patched`` of the user's is not boxman's."""
        patched = str(vm.isodir / "nixos-minimal.patched")
        Path(patched).write_bytes(b"the user's own overlay")
        vm.backing[patched] = ("qcow2", vm.iso)
        vm.live["sda"] = vm.config["sda"] = vm.medium = patched

        execute, shell = _restoring(sm, vm)
        with execute, shell:
            sm._put_cdroms_back_on_their_isos(vm.VM, paths_in_use=vm.in_use)

        assert vm.live["sda"] == vm.config["sda"] == patched
        assert os.path.exists(patched) and vm.updates == []

    def test_a_vm_holding_managed_saved_state_is_left_alone(self, sm, vm):
        """Its next start restores the definition saved with it, which
        names the overlay: switched and removed, that start would fail."""
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            vm._revert("s2")
            vm.active, vm.managed_save = False, True
            overlay = vm.config["sda"]
            sm._put_cdroms_back_on_their_isos(vm.VM, paths_in_use=vm.in_use)

        assert vm.config["sda"] == overlay and os.path.exists(overlay)
        assert vm.updates == [] and vm.redefined == []

    def test_a_record_that_cannot_be_rewritten_keeps_the_overlay(
            self, sm, vm):
        vm.fail_redefine = "error: invalid argument"

        assert _restore(sm, vm, "s2") is True

        overlay = vm.snapshots["s2"]["disks"]["sda"]
        assert vm.live["sda"] == vm.iso
        assert os.path.exists(overlay)

    def test_without_a_usage_check_nothing_is_touched(self, sm, vm):
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            assert sm.snapshot_restore(vm.VM, "s1") is True

        assert vm.live["sda"] != vm.iso
        assert vm.updates == [] and vm.redefined == []


class TestNoDeleteCommitsIntoAnIso:
    """libvirt's own delete of an external snapshot block-commits each
    overlay the snapshot records into the image under it, opening that image
    read-write (measured on libvirt 10.10). A reverted-to snapshot left
    recording its CDROM's overlay had libvirt commit it into the ISO: one
    every VM booted from it shares, where a running one fails it on the
    write lock and leaves the delete half-done (#273)."""

    @pytest.mark.parametrize("native", [True, False],
                             ids=["libvirts-delete", "boxmans-blockcommit"])
    def test_deleting_the_restored_snapshot_commits_nothing_into_the_iso(
            self, sm, tmp_path, native):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        assert _restore(sm, vm, "s1") is True
        vm.native_delete = native

        execute, shell = _restoring(sm, vm)
        with execute, shell:
            assert sm.delete_snapshot(vm.VM, "s1") is True

        # the boot disk's overlay is merged, and nothing else
        assert [target for target, _o, _b in vm.commits] == ["vda"]
        assert not any(below == vm.iso for _t, _o, below in vm.commits)
        assert not any(c[0] in ("blockcommit", "blockpull") and "sda" in c
                       for c in vm.calls)
        assert vm.live["sda"] == vm.iso
        assert Path(vm.iso).read_bytes() == b"the ISO"


class TestNoRecordOfACdromOverlayReachesADelete:
    """A VM restored before #273, or where undoing a revert failed, keeps a
    snapshot that records its CDROM's overlay. libvirt's own delete of it
    -- ``snapshot delete``, and ``take --force``, which deletes first --
    commits that overlay into the ISO."""

    @staticmethod
    def _restored_before_this_fix(tmp_path) -> _RevertingLibvirt:
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        vm.take("s2")
        execute, shell = _restoring(None, vm)
        with execute, shell:
            vm._revert("s1")
            vm._revert("s2")
        assert vm.snapshots["s2"]["disks"]["sda"]
        return vm

    @staticmethod
    def _committed_into_the_iso(vm) -> list:
        return [c for c in vm.commits if c[0] == "sda" or c[2] == vm.iso]

    def test_deleting_on_a_vm_restored_before_this_fix(self, sm, tmp_path):
        vm = self._restored_before_this_fix(tmp_path)

        execute, shell = _restoring(sm, vm)
        with execute, shell:
            assert sm.delete_snapshot(vm.VM, "s2") is True

        assert self._committed_into_the_iso(vm) == []
        assert [t for t, _o, _b in vm.commits] == ["vda"]

    def test_deleting_after_a_switch_that_failed(self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        vm.fail_update = "error: Failed to update device"
        assert _restore(sm, vm, "s1") is True
        vm.fail_update = None

        execute, shell = _restoring(sm, vm)
        with execute, shell:
            assert sm.delete_snapshot(vm.VM, "s1") is True

        assert self._committed_into_the_iso(vm) == []

    def test_a_record_that_cannot_be_rewritten_refuses_the_delete(
            self, sm, tmp_path):
        vm = self._restored_before_this_fix(tmp_path)
        vm.fail_redefine = "error: invalid argument"

        execute, shell = _restoring(sm, vm)
        with execute, shell, patch.object(sm.logger, "error") as error:
            assert sm.delete_snapshot(vm.VM, "s2") is False

        assert vm.commits == [] and "s2" in vm.snapshots
        assert "commit into the ISO" in " ".join(
            str(c.args[0]) for c in error.call_args_list)

    def test_take_force_on_a_vm_restored_before_this_fix(self, sm, tmp_path):
        vm = self._restored_before_this_fix(tmp_path)

        execute, shell = _restoring(sm, vm)
        with execute, shell:
            assert sm.create_snapshot(
                vm.VM, str(tmp_path), "s2", "again", force=True,
                paths_in_use=vm.in_use,
                recorded_in_use=lambda: FilesInUse()) is True

        assert self._committed_into_the_iso(vm) == []
        assert vm.live["sda"] == vm.iso
        assert vm.files_beside_the_iso() == ["nixos-minimal.iso"]

    def test_the_cdroms_go_back_before_force_deletes_anything(
            self, sm, tmp_path):
        vm = self._restored_before_this_fix(tmp_path)
        order = []
        flatten = sm._flatten_cdrom_overlays
        clear = sm._force_clear_for_retake

        execute, shell = _restoring(sm, vm)
        with execute, shell, \
             patch.object(sm, "_flatten_cdrom_overlays", side_effect=lambda
                          *a, **k: order.append("flatten") or flatten(*a, **k)), \
             patch.object(sm, "_force_clear_for_retake", side_effect=lambda
                          *a, **k: order.append("force") or clear(*a, **k)):
            assert sm.create_snapshot(
                vm.VM, str(tmp_path), "s2", "again", force=True,
                paths_in_use=vm.in_use,
                recorded_in_use=lambda: FilesInUse()) is True

        assert order == ["flatten", "force"]

    def test_a_flatten_that_raises_does_not_stop_the_take(self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)

        execute, shell = _restoring(sm, vm)
        with execute, shell, \
             patch.object(sm, "_flatten_cdrom_overlays",
                          side_effect=RuntimeError("virsh went away")):
            assert sm.create_snapshot(vm.VM, str(tmp_path), "s1", "first",
                                      paths_in_use=vm.in_use) is True

        assert "s1" in vm.snapshots


def _validity(sm: SnapshotManager, vm: _RevertingLibvirt) -> dict:
    """``validate_snapshot`` of every snapshot of *vm*."""
    execute, shell = _restoring(sm, vm)
    with execute, shell:
        return {name: sm.validate_snapshot(vm.VM, name)
                for name in vm.snapshots}


class TestARestoreKeepsWhatTheCurrentSnapshotRecords:
    """#287: whichever snapshot it goes to, a revert deletes the overlays
    the *current* snapshot records -- its ``<revertDisks>`` if it has any,
    else its ``<disks>`` (qemuSnapshotRevertExternalFinish). The current one
    is not always the target or newer in chain order: libvirt's own delete
    of the current snapshot makes its parent current, whose
    ``<revertDisks>`` a take has cleared, and whose ``<disks>`` name the
    overlays its other children are built on. What the current snapshot
    records is backed up and put back, and every snapshot stays valid and
    restorable."""

    @staticmethod
    def _delete(sm, vm, name: str) -> bool:
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            return sm.delete_snapshot(vm.VM, name)

    def _delete_the_current_one(self, sm, vm, switch_fails=False) -> None:
        """take s1, take s2, restore s1, take s3, delete s3: s1 is current
        again, and records the overlay s2 is built on."""
        vm.take("s1")
        vm.take("s2")
        vm.fail_update = "error: Failed to update device" if switch_fails \
            else None
        assert _restore(sm, vm, "s1") is True
        vm.fail_update = None
        vm.take("s3")
        assert self._delete(sm, vm, "s3") is True
        s1 = vm.snapshots["s1"]
        assert vm.current == "s1" and s1["revert_disks"] is None
        assert vm.snapshots["s2"]["domain"]["vda"] == s1["disks"]["vda"]

    @staticmethod
    def _restores(sm, vm, *names) -> None:
        """Restore each of *names* in turn: every snapshot is valid after
        each one."""
        for name in names:
            assert _restore(sm, vm, name) is True, name
            assert _validity(sm, vm) == {
                snap: (True, []) for snap in vm.snapshots}, name

    @pytest.mark.parametrize("switch_fails", [False, True],
                             ids=["cdrom-switched", "cdrom-left-on-overlay"])
    def test_restoring_a_newer_snapshot_after_deleting_the_current_one(
            self, sm, tmp_path, switch_fails):
        """The issue's sequence. The revert to s2 deleted s1's overlay, the
        base of s2: s1 failed validation, and every later revert to s2
        failed. Whatever the first restore did to the CDROM, no overlay of
        it is copied beside the ISO, and none is left there (#286)."""
        vm = _RevertingLibvirt(tmp_path)
        self._delete_the_current_one(sm, vm, switch_fails)
        base = vm.snapshots["s1"]["disks"]["vda"]
        held = Path(base).read_bytes()

        self._restores(sm, vm, "s2")

        assert Path(base).read_bytes() == held
        self._restores(sm, vm, "s2", "s1", "s2")
        assert not any(str(vm.isodir) in c for c in vm.shell
                       if c.startswith(("cp ", "mv ")))
        assert vm.files_beside_the_iso() == ["nixos-minimal.iso"]
        assert Path(vm.iso).read_bytes() == b"the ISO"
        assert not list(tmp_path.rglob("*.preserve*"))

    def test_the_current_snapshots_overlay_deeper_under_the_target_is_put_back(
            self, sm, tmp_path):
        """take s1, s2, s3, restore s1, take s4, delete s4: the revert to s3
        deletes s1's overlay, two layers under s3's."""
        vm = _RevertingLibvirt(tmp_path)
        for name in ("s1", "s2", "s3"):
            vm.take(name)
        assert _restore(sm, vm, "s1") is True
        vm.take("s4")
        assert self._delete(sm, vm, "s4") is True
        assert vm.current == "s1"

        self._restores(sm, vm, "s3", "s2", "s3", "s1")

    def test_a_current_snapshot_listed_before_the_target_keeps_its_overlay(
            self, sm, tmp_path):
        """virsh lists snapshots by name: b, taken after restoring a and
        current, comes before c in chain order, so a restore of c left the
        overlay the revert deletes out of the backup."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("a")
        vm.take("c")
        assert _restore(sm, vm, "a") is True
        vm.take("b")
        taken = vm.snapshots["b"]["disks"]["vda"]

        self._restores(sm, vm, "c")

        assert os.path.exists(taken)
        self._restores(sm, vm, "b", "c", "a")

    def test_deleting_a_snapshot_that_is_not_current_then_restoring(
            self, sm, tmp_path):
        """take s1, take s2, restore s1, take s3, restore s1, delete s3:
        libvirt moves s1's ``<revertDisks>`` into its ``<disks>``
        (qemuSnapshotDeleteUpdateParent), so s1 records the overlay the
        guest runs on, which the revert to s2 deletes."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        vm.take("s2")
        assert _restore(sm, vm, "s1") is True
        vm.take("s3")
        assert _restore(sm, vm, "s1") is True
        assert self._delete(sm, vm, "s3") is True
        assert vm.current == "s1"
        assert vm.snapshots["s1"]["disks"]["vda"] == vm.live["vda"]

        self._restores(sm, vm, "s2", "s1", "s2")

    def test_a_take_force_whose_take_fails_after_its_delete(
            self, sm, tmp_path):
        """--force deletes the snapshot it takes again first: when the take
        then fails, the parent is left current."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        vm.take("s2")
        assert _restore(sm, vm, "s1") is True
        vm.take("s3")
        vm.fail_take = "error: No space left on device"

        execute, shell = _restoring(sm, vm)
        with execute, shell:
            assert sm.create_snapshot(
                vm.VM, str(tmp_path), "s3", "again", force=True,
                paths_in_use=vm.in_use,
                recorded_in_use=lambda: FilesInUse()) is False
        assert "s3" not in vm.snapshots and vm.current == "s1"

        self._restores(sm, vm, "s2", "s2", "s1")

    def test_a_delete_boxman_carries_out_by_collapsing(self, sm, tmp_path):
        """libvirt before 9.0 refuses to delete an external snapshot, and
        boxman collapses it away instead: the metadata delete that ends the
        collapse makes the parent current all the same, and it records the
        live disk, which is what a revert deletes."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        vm.take("s2")
        vm.native_delete = False

        assert self._delete(sm, vm, "s2") is True
        assert "s2" not in vm.snapshots and vm.current == "s1"
        s1 = vm.snapshots["s1"]
        assert s1["disks"]["vda"] == vm.live["vda"]
        assert vm.backing[vm.live["vda"]][1] == s1["domain"]["vda"]

        self._restores(sm, vm, "s1", "s1")

    def test_a_delete_boxman_would_carry_out_by_collapsing_beside_a_sibling(
            self, sm, tmp_path):
        """The issue's sequence with libvirt refusing the delete of s3: the
        collapse went onto s2, the snapshot before s3 in chain order, which
        is its sibling (#291). Onto its parent s1 it would drop s2, so the
        delete is refused, and every snapshot stays restorable."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        vm.take("s2")
        assert _restore(sm, vm, "s1") is True
        vm.take("s3")
        vm.native_delete = False
        vm.shell.clear()

        assert self._delete(sm, vm, "s3") is False
        assert sorted(vm.snapshots) == ["s1", "s2", "s3"]
        assert vm.current == "s3"
        assert not any("rebase" in c for c in vm.shell)

        self._restores(sm, vm, "s2", "s2", "s1", "s3")

    def test_with_every_snapshot_disk_only(self, sm, tmp_path):
        """Taken of the shut-off VM, every snapshot is disk-only: a revert
        to one is libvirt's inactive revert, which deletes what the current
        one records all the same, and leaves the VM shut off."""
        vm = _RevertingLibvirt(tmp_path, state="shutoff")
        self._delete_the_current_one(sm, vm)
        assert vm.snapshots["s1"]["state"] == "shutoff"

        self._restores(sm, vm, "s2", "s2", "s1")

        assert vm.active is False

    def test_a_data_disk_with_its_overlays_in_a_disk_snapshot_dir(
            self, sm, tmp_path):
        fast = tmp_path / "fast"
        vm = _RevertingLibvirt(tmp_path, data_disk=True, disk_dir=fast)
        self._delete_the_current_one(sm, vm)
        bases = [p for p in vm.snapshots["s1"]["disks"].values() if p]
        assert len(bases) == 2
        assert {os.path.dirname(p) for p in bases} == {str(fast)}

        self._restores(sm, vm, "s2")

        assert all(os.path.exists(p) for p in bases)
        self._restores(sm, vm, "s2", "s1", "s2")
        assert not list(tmp_path.rglob("*.preserve*"))

    def test_a_current_snapshot_with_revert_disks_has_those_backed_up(
            self, sm, tmp_path):
        """Restored and not taken since, s1 still has ``<revertDisks>``:
        the revert to s2 deletes those -- the overlay the guest ran on
        since -- and leaves its ``<disks>`` alone. What it deletes is put
        back, as every overlay a revert deletes is, and named, as no
        snapshot records it; what it leaves alone is not copied."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        vm.take("s2")
        assert _restore(sm, vm, "s1") is True
        ran_on = vm.snapshots["s1"]["revert_disks"]["vda"]
        taken = vm.snapshots["s1"]["disks"]["vda"]
        assert ran_on == vm.live["vda"]
        vm.shell.clear()

        with patch.object(sm.logger, "warning") as warning:
            self._restores(sm, vm, "s2")

        assert os.path.exists(ran_on)
        copies = " ".join(c for c in vm.shell if c.startswith("cp "))
        assert f"{ran_on} " in copies and f"{taken} " not in copies
        assert ran_on in " ".join(str(c.args[0])
                                  for c in warning.call_args_list)


class TestDeletingASnapshotThatIsNotCurrent:
    """#289: libvirt's delete of a leaf that is not current merges nothing
    and deletes the image the leaf's saved definition is built on
    (qemuSnapshotDiscardExternal) -- its parent's take-time overlay, when it
    was taken on that. A take after a restore of the parent has cleared the
    parent's ``<revertDisks>``, so nothing moves into its ``<disks>``
    (qemuSnapshotDeleteUpdateParent), which still name the deleted file.

    The parent was then refused as broken, though nothing needs that file:
    a revert to the parent builds on the definition it saved
    (qemuSnapshotRevertExternalPrepare), libvirt's delete of it merges what
    its remaining child was taken on (qemuSnapshotExternalGetSnapDiskSrc),
    and a child taken after a restore is built on the restore's overlay.
    Every snapshot left stays valid and restorable."""

    @staticmethod
    def _delete(sm, vm, name: str) -> bool:
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            return sm.delete_snapshot(vm.VM, name)

    _restores = staticmethod(
        TestARestoreKeepsWhatTheCurrentSnapshotRecords._restores)

    @staticmethod
    def _all_valid(sm, vm) -> bool:
        return _validity(sm, vm) == {name: (True, []) for name in vm.snapshots}

    @staticmethod
    def _branched(sm, vm) -> list[str]:
        """take s1, take s2, restore s1, take s3: s2 is a leaf that is not
        current, built on s1's take-time overlays, which are returned."""
        vm.take("s1")
        vm.take("s2")
        assert _restore(sm, vm, "s1") is True
        vm.take("s3")
        taken = [p for p in vm.snapshots["s1"]["disks"].values() if p]
        assert vm.snapshots["s1"]["revert_disks"] is None
        assert vm.snapshots["s2"]["domain"]["vda"] in taken
        return taken

    def test_deleting_a_leaf_in_another_branch_keeps_its_parent_valid(
            self, sm, tmp_path):
        """The issue's sequence: take s1, take s2, restore s1, take s3,
        delete s2. s1 failed validation for the overlay libvirt deleted, and
        its restore was refused. A child taken after restoring it is built
        on the restore's overlay."""
        vm = _RevertingLibvirt(tmp_path)
        taken = self._branched(sm, vm)

        assert self._delete(sm, vm, "s2") is True

        assert not any(os.path.exists(p) for p in taken)
        assert vm.snapshots["s1"]["disks"]["vda"] in taken
        assert self._all_valid(sm, vm)
        self._restores(sm, vm, "s1", "s3", "s1")
        vm.take("s4")
        assert vm.snapshots["s4"]["domain"]["vda"] not in taken
        self._restores(sm, vm, "s3", "s4", "s1")

    def test_a_leaf_deeper_in_the_tree(self, sm, tmp_path):
        """take s1, s2, s3, restore s2, take s4, delete s3: s2's take-time
        overlay goes, with s1's still under s2's saved definition."""
        vm = _RevertingLibvirt(tmp_path)
        for name in ("s1", "s2", "s3"):
            vm.take(name)
        assert _restore(sm, vm, "s2") is True
        vm.take("s4")
        taken = vm.snapshots["s2"]["disks"]["vda"]

        assert self._delete(sm, vm, "s3") is True

        assert not os.path.exists(taken)
        assert self._all_valid(sm, vm)
        self._restores(sm, vm, "s2", "s4", "s1", "s2")

    def test_the_parent_made_current_again_by_deleting_the_other_child(
            self, sm, tmp_path):
        """... delete s2, then delete s3, the current one: libvirt makes s1
        current, still recording the overlay that is gone, which the next
        revert then has nothing of to delete."""
        vm = _RevertingLibvirt(tmp_path)
        taken = self._branched(sm, vm)
        assert self._delete(sm, vm, "s2") is True

        assert self._delete(sm, vm, "s3") is True

        assert vm.current == "s1"
        assert vm.snapshots["s1"]["disks"]["vda"] in taken
        assert self._all_valid(sm, vm)
        self._restores(sm, vm, "s1", "s1")

    def test_with_every_snapshot_disk_only(self, sm, tmp_path):
        """Taken of the shut-off VM, every snapshot is disk-only: libvirt's
        delete needs no running domain for a leaf, and a revert is its
        inactive revert, which builds on the same saved definition."""
        vm = _RevertingLibvirt(tmp_path, state="shutoff")
        taken = self._branched(sm, vm)

        assert self._delete(sm, vm, "s2") is True

        assert not any(os.path.exists(p) for p in taken)
        assert self._all_valid(sm, vm)
        self._restores(sm, vm, "s1", "s3", "s1")
        assert vm.active is False

    def test_a_data_disk_with_its_overlays_in_a_disk_snapshot_dir(
            self, sm, tmp_path):
        fast = tmp_path / "fast"
        vm = _RevertingLibvirt(tmp_path, data_disk=True, disk_dir=fast)
        taken = self._branched(sm, vm)
        assert len(taken) == 2
        assert {os.path.dirname(p) for p in taken} == {str(fast)}

        assert self._delete(sm, vm, "s2") is True

        assert not any(os.path.exists(p) for p in taken)
        assert self._all_valid(sm, vm)
        self._restores(sm, vm, "s1", "s3", "s1")
        assert not list(tmp_path.rglob("*.preserve*"))

    def test_deleting_the_parent_afterwards_merges_what_its_child_was_taken_on(
            self, sm, tmp_path):
        """libvirt's delete of s1, left with one child, commits the image s3
        was taken on -- the restore's overlay -- into the one s1 was taken
        on, and never reads s1's take-time overlay."""
        vm = _RevertingLibvirt(tmp_path)
        self._branched(sm, vm)
        assert self._delete(sm, vm, "s2") is True
        ran_on = vm.snapshots["s3"]["domain"]["vda"]
        base = vm.snapshots["s1"]["domain"]["vda"]

        assert self._delete(sm, vm, "s1") is True

        assert ("vda", ran_on, base) in vm.commits
        assert not os.path.exists(ran_on)
        assert vm.snapshots["s3"]["parent"] is None
        assert vm.snapshots["s3"]["domain"]["vda"] == base
        self._restores(sm, vm, "s3", "s3")

    def test_a_buried_one_in_the_active_chain_is_merged_into_the_one_under(
            self, sm, tmp_path):
        """take s1, s2, s3, delete s2: the image s3 was taken on, s2's
        overlay, is committed into s1's, which s3 is then built on. Nothing
        is left recording a file that is gone."""
        vm = _RevertingLibvirt(tmp_path)
        for name in ("s1", "s2", "s3"):
            vm.take(name)
        s1, s2 = (vm.snapshots[n]["disks"]["vda"] for n in ("s1", "s2"))

        assert self._delete(sm, vm, "s2") is True

        assert vm.commits == [("vda", s2, s1)] and not os.path.exists(s2)
        assert vm.snapshots["s3"]["parent"] == "s1"
        assert vm.snapshots["s3"]["domain"]["vda"] == s1
        self._restores(sm, vm, "s1", "s3")

    def test_a_vm_whose_records_name_cdrom_overlays_keeps_its_iso(
            self, sm, tmp_path):
        """Restored before #286, with the CDROM put back on its ISO by hand,
        s1 and s2 both record a CDROM overlay and s2's saved CDROM is the
        ISO: libvirt's delete of s2, not current, would delete the ISO
        itself. The records are rewritten first (#286), so it touches no
        CDROM image, and s1 stays valid."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        execute, shell = _restoring(None, vm)
        with execute, shell:
            vm._revert("s1")
            vm.live["sda"] = vm.config["sda"] = vm.medium = vm.iso
            vm.take("s2")
            vm._revert("s2")
            vm._revert("s1")
        vm.take("s3")
        assert vm.snapshots["s1"]["disks"]["sda"]
        assert vm.snapshots["s2"]["disks"]["sda"]
        assert vm.snapshots["s2"]["domain"]["sda"] == vm.iso

        assert self._delete(sm, vm, "s2") is True

        assert Path(vm.iso).read_bytes() == b"the ISO"
        assert not any(c[0] == "sda" for c in vm.commits)
        assert self._all_valid(sm, vm)
        self._restores(sm, vm, "s1", "s3")
        assert Path(vm.iso).read_bytes() == b"the ISO"


class TestACollapseInASnapshotTree:
    """#291: ``take s1, take s2, restore s1, take s3`` leaves s2 and s3 both
    built on s1, and the VM on s3. A collapse took the snapshots in chain
    order -- breadth-first, siblings by name -- as a line and dropped all
    of them after its target: s2, on the other branch, with its overlay
    and memory image, though the live disk is not built on it and nothing
    merged it anywhere. A collapse now drops only the line from its target
    to the current snapshot, and refuses, touching nothing, while anything
    else is built on the target. So does the delete boxman carries out by
    collapsing, which also took its parent from that order."""

    @staticmethod
    def _tree(sm, tmp_path, *steps) -> _RevertingLibvirt:
        """Each step a take, or ``restore <name>``."""
        vm = _RevertingLibvirt(tmp_path)
        for step in steps:
            if step.startswith("restore "):
                assert _restore(sm, vm, step.split()[1]) is True
            else:
                vm.take(step)
        return vm

    def _forked(self, sm, tmp_path) -> _RevertingLibvirt:
        vm = self._tree(sm, tmp_path, "s1", "s2", "restore s1", "s3")
        assert vm.snapshots["s2"]["parent"] == "s1" == (
            vm.snapshots["s3"]["parent"])
        return vm

    @staticmethod
    def _files(vm) -> dict:
        return {str(p): p.read_bytes() for p in vm.root.rglob("*")
                if p.is_file()}

    @staticmethod
    def _collapse(sm, vm, target, dry_run=False):
        """``collapse_to`` with the VM shut down first, as the manager does:
        ``(result, errors, info lines)``."""
        vm.active = False
        execute, shell = _restoring(sm, vm)
        with execute, shell, \
             patch.object(sm.logger, "error") as error, \
             patch.object(sm.logger, "info") as info:
            done = sm.collapse_to(vm.VM, target, dry_run=dry_run,
                                  recorded_in_use=vm.recorded)
        return (done, " ".join(str(c.args[0]) for c in error.call_args_list),
                [str(c.args[0]) for c in info.call_args_list])

    @staticmethod
    def _delete(sm, vm, name):
        execute, shell = _restoring(sm, vm)
        with execute, shell, patch.object(sm.logger, "error") as error:
            done = sm.delete_snapshot(vm.VM, name, recorded_in_use=vm.recorded)
        return done, " ".join(str(c.args[0]) for c in error.call_args_list)

    @staticmethod
    def _all_restore(sm, vm, *names) -> None:
        """Every snapshot is valid, and after restoring each of *names* in
        turn still is."""
        assert _validity(sm, vm) == {n: (True, []) for n in vm.snapshots}
        for name in names:
            assert _restore(sm, vm, name) is True, name
            assert _validity(sm, vm) == {
                n: (True, []) for n in vm.snapshots}, name

    def test_a_collapse_to_the_fork_is_refused_and_drops_nothing(
            self, sm, tmp_path):
        vm = self._forked(sm, tmp_path)
        before = self._files(vm)

        done, errors, _ = self._collapse(sm, vm, "s1")

        assert done is False
        assert "['s2']" in errors and "another branch" in errors
        assert sorted(vm.snapshots) == ["s1", "s2", "s3"]
        assert vm.current == "s3"
        assert self._files(vm) == before
        self._all_restore(sm, vm, "s2", "s3", "s1", "s2")

    def test_its_dry_run_refuses_in_the_same_words(self, sm, tmp_path):
        vm = self._forked(sm, tmp_path)
        before = self._files(vm)

        done, errors, info = self._collapse(sm, vm, "s1", dry_run=True)

        assert done is False
        assert "['s2']" in errors and "another branch" in errors
        assert not any("would" in line for line in info)
        assert self._files(vm) == before

    def test_a_collapse_to_the_other_branch_is_refused(self, sm, tmp_path):
        """The VM runs on s3, which is not built on s2: the collapse rebased
        the live disk onto s2's overlay and dropped s3, the current
        snapshot, with its memory image."""
        vm = self._forked(sm, tmp_path)
        before = self._files(vm)

        done, errors, _ = self._collapse(sm, vm, "s2")

        assert done is False
        assert "runs on 's3'" in errors
        assert sorted(vm.snapshots) == ["s1", "s2", "s3"]
        assert self._files(vm) == before
        assert vm.backing[vm.live["vda"]][1] != vm.snapshots["s2"]["disks"]["vda"]
        self._all_restore(sm, vm, "s3", "s2", "s1")

    def test_a_collapse_to_the_current_snapshot_keeps_a_sibling_after_it(
            self, sm, tmp_path):
        """take a, take c, restore a, take b: chain order is a, b, c, so a
        collapse to b, the snapshot the VM runs on, dropped c."""
        vm = self._tree(sm, tmp_path, "a", "c", "restore a", "b")
        before = self._files(vm)

        done, _, info = self._collapse(sm, vm, "b")

        assert done is True
        assert any("nothing to collapse" in line for line in info)
        assert sorted(vm.snapshots) == ["a", "b", "c"]
        assert self._files(vm) == before
        self._all_restore(sm, vm, "c", "b")

    def test_right_after_a_restore_the_snapshot_it_left_is_kept(
            self, sm, tmp_path):
        """take s1, take s2, restore s1: the VM runs on s1, and s2 is built
        on s1 but not under the live disk."""
        vm = self._tree(sm, tmp_path, "s1", "s2", "restore s1")
        before = self._files(vm)

        done, errors, _ = self._collapse(sm, vm, "s1")

        assert done is False
        assert "['s2']" in errors
        assert sorted(vm.snapshots) == ["s1", "s2"]
        assert self._files(vm) == before
        self._all_restore(sm, vm, "s2", "s1")

    def test_a_branch_two_snapshots_deep_is_named_whole(self, sm, tmp_path):
        vm = self._tree(sm, tmp_path, "s1", "s2", "s4", "restore s1", "s3")
        before = self._files(vm)

        done, errors, _ = self._collapse(sm, vm, "s1")

        assert done is False
        assert "['s2', 's4']" in errors
        assert sorted(vm.snapshots) == ["s1", "s2", "s3", "s4"]
        assert self._files(vm) == before

    def test_a_branch_beside_the_target_is_kept(self, sm, tmp_path):
        """take r, take x, restore r, take t, take p: x and t are built on
        r, p on t. Chain order is r, t, x, p, so a collapse to t dropped x,
        t's sibling, along with p."""
        vm = self._tree(sm, tmp_path, "r", "x", "restore r", "t", "p")
        x_files = {path: data for path, data in self._files(vm).items()
                   if path in (vm.snapshots["x"]["memory"],
                               vm.snapshots["x"]["disks"]["vda"])}
        head = vm.live["vda"]

        done, _, _ = self._collapse(sm, vm, "t")

        assert done is True
        assert sorted(vm.snapshots) == ["r", "t", "x"]
        assert vm.current == "t"
        assert len(x_files) == 2
        assert all(Path(path).read_bytes() == data
                   for path, data in x_files.items())
        assert os.path.exists(head)
        self._all_restore(sm, vm, "x", "t", "r")

    def test_the_line_from_a_snapshot_after_the_fork_still_collapses(
            self, sm, tmp_path):
        vm = self._tree(sm, tmp_path, "s1", "s2", "restore s1", "s3", "s4")
        head = vm.live["vda"]
        s4_memory = vm.snapshots["s4"]["memory"]

        done, _, info = self._collapse(sm, vm, "s3")

        assert done is True
        assert any("would" not in line and "collapsed 1" in line
                   for line in info)
        assert sorted(vm.snapshots) == ["s1", "s2", "s3"]
        assert vm.current == "s3"
        assert os.path.exists(head) and not os.path.exists(s4_memory)
        self._all_restore(sm, vm, "s2", "s1", "s3")

    def test_its_dry_run_names_the_line_and_what_it_keeps(self, sm, tmp_path):
        vm = self._tree(sm, tmp_path, "s1", "s2", "restore s1", "s3", "s4")

        done, _, info = self._collapse(sm, vm, "s3", dry_run=True)

        assert done is True
        assert any("would drop 1 snapshot(s) (['s4'])" in line
                   and "'s1', 's2', 's3'" in line for line in info)

    def test_after_the_other_branch_is_deleted_the_fork_collapses(
            self, sm, tmp_path):
        """take s1, take s2, restore s1, take s3, delete s2: what the error
        says to do. libvirt's delete of s2 unlinks the overlay s1's take
        made (#289); the live disk was never built on it, but on the disk
        s1 was taken on, and that is what it is rebased onto."""
        vm = self._forked(sm, tmp_path)
        taken = vm.snapshots["s1"]["disks"]["vda"]
        taken_on = vm.snapshots["s1"]["domain"]["vda"]
        done, _ = self._delete(sm, vm, "s2")
        assert done is True and not os.path.exists(taken)
        head = vm.live["vda"]

        with patch.object(sm.logger, "warning") as warning:
            done, errors, info = self._collapse(sm, vm, "s1", dry_run=True)
            assert done is True, errors
            assert any(f"would rebase {head} onto {taken_on}" in line
                       for line in info)
            assert any(f"would keep {head}: it is the disk" in line
                       for line in info)
            done, errors, _ = self._collapse(sm, vm, "s1")
            assert done is True, errors

        assert not warning.call_args_list
        assert vm.backing[head] == ("qcow2", taken_on)
        assert sorted(vm.snapshots) == ["s1"] and vm.current == "s1"
        assert os.path.exists(head)

    def test_an_overlay_the_live_disk_is_not_built_on_is_not_rebased_onto(
            self, sm, tmp_path):
        """s2's record forgotten and the overlay s1's take made left: the
        head is still not built on it, and is not rebased onto it."""
        vm = self._forked(sm, tmp_path)
        assert vm("snapshot-delete", vm.VM, "s2", "--metadata").ok
        taken_on = vm.snapshots["s1"]["domain"]["vda"]
        head = vm.live["vda"]

        done, errors, _ = self._collapse(sm, vm, "s1")

        assert done is True, errors
        assert vm.backing[head] == ("qcow2", taken_on)
        assert os.path.exists(vm.snapshots["s1"]["disks"]["vda"])

    # --- the delete boxman carries out by collapsing ------------------------

    def test_the_current_snapshot_is_not_collapsed_onto_its_sibling(
            self, sm, tmp_path):
        """libvirt refusing the delete of s3, boxman collapsed onto s2, the
        snapshot before s3 in chain order: its sibling, not its parent.
        Onto its parent s1 the collapse would drop s2, so it is refused."""
        vm = self._forked(sm, tmp_path)
        vm.native_delete = False
        before = self._files(vm)

        done, errors = self._delete(sm, vm, "s3")

        assert done is False
        assert "['s2']" in errors
        assert sorted(vm.snapshots) == ["s1", "s2", "s3"]
        assert self._files(vm) == before
        self._all_restore(sm, vm, "s2", "s3")

    def test_one_on_the_other_branch_points_at_no_collapse(self, sm, tmp_path):
        """s2 comes before s3 in chain order: its delete was refused as one
        with "newer snapshots above it" (s3, on the other branch), pointing
        at `collapse --to s1`, which drops s2 and s3 both."""
        vm = self._forked(sm, tmp_path)
        vm.native_delete = False
        before = self._files(vm)

        done, errors = self._delete(sm, vm, "s2")

        assert done is False
        assert "collapse" not in errors and "newer" not in errors
        assert "current snapshot" in errors
        assert self._files(vm) == before

    def test_the_fork_that_libvirt_refuses_points_at_no_collapse(
            self, sm, tmp_path):
        """libvirt 10.10 refuses to delete s1, with two children, and boxman
        pointed at `collapse --to s1`: that keeps s1 and drops s2."""
        vm = self._forked(sm, tmp_path)
        before = self._files(vm)

        done, errors = self._delete(sm, vm, "s1")

        assert done is False
        assert "collapse --to" not in errors
        assert "['s2', 's3']" in errors
        assert self._files(vm) == before

    def test_a_middle_snapshot_of_the_line_points_at_its_collapse(
            self, sm, tmp_path):
        vm = self._tree(sm, tmp_path, "s1", "s2", "s3")
        vm.native_delete = False

        done, errors = self._delete(sm, vm, "s2")

        assert done is False
        assert "collapse --to s1" in errors and "['s3']" in errors

    def test_a_snapshot_built_on_none_beside_another_is_refused(
            self, sm, tmp_path):
        """Two snapshots built on none: chain order put r before s, and
        the delete of s collapsed onto r, which s is not built on."""
        _base, overlays, memory = _take_external(
            tmp_path, ["r", "s"], real=False)
        virsh = _Libvirt({n: {"vda": str(p)} for n, p in overlays.items()},
                         {"vda": str(overlays["s"])}, memory,
                         parents={"r": None, "s": None})

        with patch.object(sm.virsh, "execute", side_effect=virsh), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs()), \
             patch.object(sm, "_qemu_img_rebase", return_value=True), \
             patch.object(sm, "_strip_backing_store_cache",
                          return_value=True), \
             patch.object(sm.logger, "error") as error:
            assert sm.delete_snapshot("vm01", "s") is False

        assert "['r']" in str(error.call_args.args[0])
        assert virsh.snapshots == ["r", "s"]
        assert all(p.exists() for p in [*overlays.values(), *memory.values()])


class TestADeleteAfterACollapse:
    """``take c1, take c2, collapse --to c1`` left c1 current, and libvirt
    then refused to delete it: "snapshot VM disk source and parent disk
    source are not the same". Its delete of the current snapshot takes the
    domain's disk for the snapshot's and requires the image right under it
    to be the disk the snapshot was taken on
    (``qemuSnapshotDeleteExternalPrepareData``), and on a line of takes the
    collapse had rebased the head onto c1's own overlay instead. boxman
    falls back only where libvirt says "external" or "not supported", so
    nothing was removed. c1 also went on recording that overlay: a restore
    of it unlinked the overlay and left the head where nothing names it,
    and a collapse onto it again left the first collapse's head behind.

    A collapse now rebases each head onto the disk the target was taken
    on, has the target record the head, and removes what it merged into the
    head and nothing needs: as libvirt's own delete of the dropped
    snapshots leaves them."""

    @staticmethod
    def _collapse(sm, vm, target, dry_run=False):
        """``collapse_to`` with the VM shut down first and started after,
        as the manager does: ``(result, warnings, info lines)``."""
        running, vm.active = vm.active, False
        execute, shell = _restoring(sm, vm)
        with execute, shell, \
             patch.object(sm.logger, "warning") as warning, \
             patch.object(sm.logger, "info") as info:
            done = sm.collapse_to(vm.VM, target, dry_run=dry_run,
                                  recorded_in_use=vm.recorded)
        if running:
            vm.active, vm.live = True, dict(vm.config)
        return (done, " ".join(str(c.args[0]) for c in warning.call_args_list),
                [str(c.args[0]) for c in info.call_args_list])

    @staticmethod
    def _delete(sm, vm, name):
        execute, shell = _restoring(sm, vm)
        with execute, shell, patch.object(sm.logger, "error") as error:
            done = sm.delete_snapshot(vm.VM, name, recorded_in_use=vm.recorded)
        return done, " ".join(str(c.args[0]) for c in error.call_args_list)

    @staticmethod
    def _unaccounted(vm) -> list[str]:
        """The disk images of the VM that nothing accounts for -- in no
        chain of a disk it runs on, recorded and taken on by no snapshot --
        which nothing removes until ``destroy``."""
        accounted = set()
        for source in vm.disks_now().values():
            accounted.update(vm._chain(source))
        for snap in vm.snapshots.values():
            for disks in (snap["disks"], snap["revert_disks"] or {},
                          snap["domain"]):
                accounted.update(p for p in disks.values() if p)
        dirs = [vm.workdir] + ([vm.disk_dir] if vm.disk_dir else [])
        return sorted(str(p) for d in dirs for p in d.iterdir()
                      if "_snapshot_" not in p.name and str(p) not in accounted)

    @staticmethod
    def _settled(vm, name) -> bool:
        """Whether *name* records each disk the VM runs on, and each is
        built right on the disk *name* was taken on: what libvirt's delete
        of it, current, needs."""
        snap = vm.snapshots[name]
        return all(snap["disks"][t] == vm.disks_now()[t]
                   and vm.backing[vm.disks_now()[t]][1] == snap["domain"][t]
                   for t, p in snap["disks"].items() if p)

    def test_the_snapshot_a_collapse_left_current_can_be_deleted(
            self, sm, tmp_path):
        """The issue's sequence."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("c1")
        vm.take("c2")
        taken_on, c1_overlay = (vm.snapshots["c1"]["domain"]["vda"],
                                vm.snapshots["c1"]["disks"]["vda"])

        assert self._collapse(sm, vm, "c1")[0] is True
        assert vm.current == "c1" and self._settled(vm, "c1")
        assert not os.path.exists(c1_overlay)
        assert _validity(sm, vm) == {"c1": (True, [])}

        done, errors = self._delete(sm, vm, "c1")

        assert done is True, errors
        assert vm.snapshots == {} and vm.current is None
        # what the guest wrote since c1 is committed into what c1 was
        # taken on, which the VM runs on again
        assert vm.live["vda"] == taken_on
        assert self._unaccounted(vm) == []
        assert vm.memory_files() == []

    def test_one_with_a_parent_is_merged_into_the_disk_it_was_taken_on(
            self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        for name in ("p0", "c1", "c2"):
            vm.take(name)
        assert self._collapse(sm, vm, "c1")[0] is True

        done, errors = self._delete(sm, vm, "c1")

        assert done is True, errors
        assert sorted(vm.snapshots) == ["p0"] and vm.current == "p0"
        assert vm.live["vda"] == vm.snapshots["p0"]["disks"]["vda"]
        assert self._unaccounted(vm) == []
        assert _validity(sm, vm) == {"p0": (True, [])}
        assert _restore(sm, vm, "p0") is True

    def test_a_child_taken_after_the_collapse_and_deleted_leaves_it_deletable(
            self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("c1")
        vm.take("c2")
        assert self._collapse(sm, vm, "c1")[0] is True
        vm.take("c3")

        assert self._delete(sm, vm, "c3")[0] is True
        assert vm.current == "c1"
        done, errors = self._delete(sm, vm, "c1")

        assert done is True, errors
        assert vm.snapshots == {} and self._unaccounted(vm) == []

    def test_a_second_collapse_leaves_nothing_behind(self, sm, tmp_path):
        """The first collapse's head was merged into the second's and left
        where nothing records it: the target still named its own overlay,
        which the head stood on."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("k1")
        vm.take("k2")
        assert self._collapse(sm, vm, "k1")[0] is True
        vm.take("k3")

        assert self._collapse(sm, vm, "k1")[0] is True

        assert self._unaccounted(vm) == []
        assert self._settled(vm, "k1")
        assert self._delete(sm, vm, "k1")[0] is True
        assert self._unaccounted(vm) == []

    def test_a_restore_after_the_collapse_names_every_file_it_leaves(
            self, sm, tmp_path):
        """The revert unlinks what the current snapshot records and makes a
        new overlay; the old ones are put back and named (#287). The head
        was not among them, and stayed unnamed."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("r1")
        vm.take("r2")
        assert self._collapse(sm, vm, "r1")[0] is True

        execute, shell = _restoring(sm, vm)
        with execute, shell, patch.object(sm.logger, "warning") as warning:
            assert sm.snapshot_restore(
                vm.VM, "r1", paths_in_use=vm.in_use) is True
        warned = " ".join(str(c.args[0]) for c in warning.call_args_list)

        left = self._unaccounted(vm)
        assert left and all(path in warned for path in left), (left, warned)
        assert _validity(sm, vm) == {"r1": (True, [])}
        assert self._delete(sm, vm, "r1")[0] is True

    def test_after_a_restore_of_the_target_and_a_take_the_reverts_overlay_goes(
            self, sm, tmp_path):
        """#291's fork, its other branch deleted: the head is rebased onto
        the disk s1 was taken on, and the overlay of the revert it stood
        on, merged into it, was recorded by nothing."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        vm.take("s2")
        assert _restore(sm, vm, "s1") is True
        reverted = vm.live["vda"]
        vm.take("s3")
        assert self._delete(sm, vm, "s2")[0] is True
        left = self._unaccounted(vm)

        assert self._collapse(sm, vm, "s1")[0] is True

        assert not os.path.exists(reverted)
        assert self._unaccounted(vm) == left and self._settled(vm, "s1")
        assert self._delete(sm, vm, "s1")[0] is True

    def test_the_delete_boxman_carries_out_by_collapsing_leaves_the_parent_deletable(
            self, sm, tmp_path):
        """libvirt before 9.0 refuses the delete of s3, and boxman collapses
        it onto s2; libvirt 9.0 and later then deletes s2 and s1."""
        vm = _RevertingLibvirt(tmp_path)
        for name in ("s1", "s2", "s3"):
            vm.take(name)
        vm.native_delete = False
        assert self._delete(sm, vm, "s3")[0] is True
        assert self._settled(vm, "s2")
        vm.native_delete = True

        for name in ("s2", "s1"):
            done, errors = self._delete(sm, vm, name)
            assert done is True, (name, errors)
        assert vm.snapshots == {} and self._unaccounted(vm) == []

    def test_two_disks_with_their_overlays_in_a_disk_snapshot_dir(
            self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path, data_disk=True,
                               disk_dir=tmp_path / "fast")
        vm.take("c1")
        vm.take("c2")

        assert self._collapse(sm, vm, "c1")[0] is True

        assert self._settled(vm, "c1")
        assert set(vm.snapshots["c1"]["disks"]) >= {"vda", "vdb"}
        assert self._unaccounted(vm) == []
        assert self._delete(sm, vm, "c1")[0] is True
        assert self._unaccounted(vm) == []

    def test_a_record_that_cannot_be_rewritten_keeps_its_overlay_and_says_so(
            self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("c1")
        vm.take("c2")
        c1_overlay = vm.snapshots["c1"]["disks"]["vda"]
        vm.fail_redefine = "error: invalid argument"

        done, warned, _ = self._collapse(sm, vm, "c1")

        assert done is True
        assert f"'c1' on vm01 still records {c1_overlay}" in warned
        assert vm.snapshots["c1"]["disks"]["vda"] == c1_overlay
        assert os.path.exists(c1_overlay)
        # the head is on the disk c1 was taken on all the same
        assert self._delete(sm, vm, "c1")[0] is True

    def test_its_dry_run_says_what_the_target_will_record_and_changes_nothing(
            self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("c1")
        vm.take("c2")
        c1 = vm.snapshots["c1"]
        head, overlay, taken_on = (vm.live["vda"], c1["disks"]["vda"],
                                   c1["domain"]["vda"])
        files = sorted(p.name for p in vm.workdir.iterdir())

        done, _warned, lines = self._collapse(sm, vm, "c1", dry_run=True)

        assert done is True
        assert f"[dry-run]   would rebase {head} onto {taken_on}" in lines
        assert (f"[dry-run]   'c1' would record {head} as its overlay of "
                f"vda, in place of {overlay}") in lines
        assert (f"[dry-run]   would remove {overlay}, merged into the head"
                in lines)
        assert sorted(p.name for p in vm.workdir.iterdir()) == files
        assert c1["disks"]["vda"] == overlay and vm.redefined == []

    def test_the_rest_of_the_record_goes_back_as_libvirt_wrote_it(
            self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("c1")
        vm.take("c2")
        before = vm.snapshot_xml("c1")

        assert self._collapse(sm, vm, "c1")[0] is True

        [xml] = vm.redefined
        assert "<libosinfo:os id='http://nixos.org/nixos/unknown'/>" in xml
        assert "<boxman:disk name='root'" in xml
        assert "ns0:" not in xml
        assert xml.count("<disk ") == before.count("<disk ")
        # the CDROM's record, and the definition c1 saved, as libvirt wrote
        assert "<disk name='sda' snapshot='no'/>" in xml
        assert before[before.index("<domain "):] == xml[xml.index("<domain "):]


class TestADeleteTakesItsCompressedMemoryImageWithIt:
    """#274: ``--compress-memory`` leaves a snapshot's memory image as a
    ``.raw.zst`` in place of the ``.raw`` libvirt records, which libvirt
    does not know of. Its own delete unlinks the ``.raw`` alone, and boxman
    returned as soon as that delete went through: the ``.raw.zst`` stayed
    until ``destroy``. Every delete -- libvirt's own, boxman's blockcommit,
    the collapse it falls back to, a collapse that drops snapshots, and
    ``take --force`` -- now removes the image in both forms, from wherever
    the snapshot recorded it, once no other snapshot, of this domain or
    another, records it (#253). What cannot be told is kept, and named."""

    REPLICA = "vm01_snapshot_replica"

    @staticmethod
    def _vm(tmp_path, where="workdir", **kw):
        """The double, and where its memory images go: the workdir, or a
        memory_snapshot_dir outside it."""
        vm = _RevertingLibvirt(tmp_path, **kw)
        if where == "workdir":
            return vm, vm.workdir
        memory_dir = tmp_path / "memory-snapshots"
        memory_dir.mkdir()
        return vm, memory_dir

    @staticmethod
    def _takes(vm, memory_dir, *names, compressed=()):
        for name in names:
            vm.take(name, memory=str(memory_dir / f"vm01_snapshot_{name}.raw"))
            if name in compressed:
                vm.compress(name)

    @staticmethod
    def _delete(sm, vm, name, recorded_in_use=None):
        execute, shell = _restoring(sm, vm)
        with execute, shell, patch.object(sm.logger, "warning") as warning:
            done = sm.delete_snapshot(
                vm.VM, name, recorded_in_use=recorded_in_use or vm.recorded)
        return done, " ".join(str(c.args[0]) for c in warning.call_args_list)

    @staticmethod
    def _collapse(sm, vm, target, recorded_in_use=None):
        """``collapse_to`` with the VM shut down first, as the manager does."""
        vm.active = False
        execute, shell = _restoring(sm, vm)
        with execute, shell, patch.object(sm.logger, "warning") as warning:
            done = sm.collapse_to(
                vm.VM, target, recorded_in_use=recorded_in_use or vm.recorded)
        return done, " ".join(str(c.args[0]) for c in warning.call_args_list)

    # --- libvirt's own delete -------------------------------------------------

    @pytest.mark.parametrize("where", ["workdir", "memory_snapshot_dir"])
    def test_deleting_newest_first_leaves_no_memory_image(
            self, sm, tmp_path, where):
        """The issue's sequence: take s1, s2 --compress-memory, s3, then
        delete s3, s2 and s1. s2's ``.raw.zst`` was left behind."""
        vm, memory_dir = self._vm(tmp_path, where)
        self._takes(vm, memory_dir, "s1", "s2", "s3", compressed=["s2"])
        assert [os.path.dirname(p) for p in vm.memory_files()] == [
            str(memory_dir)] * 3

        for name in ("s3", "s2", "s1"):
            assert self._delete(sm, vm, name)[0] is True, name

        assert vm.snapshots == {}
        assert vm.memory_files() == []

    def test_one_another_domains_snapshot_records_is_kept_and_named(
            self, sm, tmp_path):
        """#253: vm01's snapshot ``replica_snapshot_s1`` and ``s1`` of
        ``vm01_snapshot_replica`` name one file where their images share a
        directory."""
        vm, memory_dir = self._vm(tmp_path, "memory_snapshot_dir")
        self._takes(vm, memory_dir, "s1", compressed=["s1"])
        [zst] = vm.memory_files()

        done, warned = self._delete(
            sm, vm, "s1", lambda: vm.recorded({zst: self.REPLICA}))

        assert done is True and "s1" not in vm.snapshots
        assert vm.memory_files() == [zst]
        assert (f"not removing {zst}, the memory image of deleted snapshot "
                f"'s1' of vm01: a snapshot of domain {self.REPLICA} still "
                f"records it") in warned

    def test_one_recorded_under_another_name_is_kept(self, sm, tmp_path):
        vm, memory_dir = self._vm(tmp_path)
        self._takes(vm, memory_dir, "s1", compressed=["s1"])
        [zst] = vm.memory_files()
        other = tmp_path / "elsewhere" / "replica_s1.raw.zst"
        other.parent.mkdir()
        os.link(zst, other)

        done, warned = self._delete(
            sm, vm, "s1", lambda: vm.recorded({str(other): "vm02"}))

        assert done is True
        assert os.path.exists(zst)
        assert "a snapshot of domain vm02 still records it, under another " \
               "name" in warned

    @pytest.mark.parametrize("scan", [
        lambda: None,
        MagicMock(side_effect=RuntimeError("virsh went away")),
    ], ids=["records-unread", "scan-raises"])
    def test_with_the_records_unknown_it_is_kept_and_named(
            self, sm, tmp_path, scan):
        vm, memory_dir = self._vm(tmp_path)
        self._takes(vm, memory_dir, "s1", compressed=["s1"])
        [zst] = vm.memory_files()

        done, warned = self._delete(sm, vm, "s1", scan)

        assert done is True
        assert vm.memory_files() == [zst]
        assert f"not removing {zst}" in warned
        assert "whether another snapshot records it could not be checked" \
            in warned

    def test_without_a_scan_it_is_kept(self, sm, tmp_path):
        vm, memory_dir = self._vm(tmp_path)
        self._takes(vm, memory_dir, "s1", compressed=["s1"])
        [zst] = vm.memory_files()

        execute, shell = _restoring(sm, vm)
        with execute, shell:
            assert sm.delete_snapshot(vm.VM, "s1") is True

        assert vm.memory_files() == [zst]

    def test_a_delete_libvirt_fails_removes_nothing(self, sm, tmp_path):
        """In libvirt 10.10's words, on the snapshot a collapse left."""
        vm, memory_dir = self._vm(tmp_path)
        self._takes(vm, memory_dir, "s1", "s2", compressed=["s1", "s2"])
        held = vm.memory_files()
        vm.fail_delete = (
            "error: Failed to delete snapshot s2\nerror: operation failed: "
            "snapshot VM disk source and parent disk source are not the same")
        scan = MagicMock(side_effect=vm.recorded)

        assert self._delete(sm, vm, "s2", scan)[0] is False

        assert sorted(vm.snapshots) == ["s1", "s2"]
        assert vm.memory_files() == held
        scan.assert_not_called()

    def test_a_delete_boxman_refuses_removes_nothing(self, sm, tmp_path):
        """libvirt before 9.0 refuses s2, with s3 built on it, and so does
        boxman."""
        vm, memory_dir = self._vm(tmp_path)
        self._takes(vm, memory_dir, "s1", "s2", "s3", compressed=["s2"])
        held = vm.memory_files()
        vm.native_delete = False
        scan = MagicMock(side_effect=vm.recorded)

        assert self._delete(sm, vm, "s2", scan)[0] is False

        assert vm.memory_files() == held
        scan.assert_not_called()

    def test_a_disk_only_snapshot_has_none_and_asks_nothing(
            self, sm, tmp_path):
        vm, memory_dir = self._vm(tmp_path, state="shutoff")
        self._takes(vm, memory_dir, "s1")
        assert vm.snapshots["s1"]["memory"] is None
        scan = MagicMock(side_effect=vm.recorded)

        assert self._delete(sm, vm, "s1", scan)[0] is True

        scan.assert_not_called()

    # --- boxman's blockcommit, and the collapse a delete falls back to ------

    @pytest.mark.parametrize("where", ["workdir", "memory_snapshot_dir"])
    def test_boxmans_blockcommit_removes_both_forms(self, sm, tmp_path, where):
        """The only snapshot, which libvirt before 9.0 refuses to delete:
        boxman commits it into the base, and its memory image goes once
        its record has, the ``.raw`` a revert left beside the ``.raw.zst``
        too."""
        vm, memory_dir = self._vm(tmp_path, where)
        self._takes(vm, memory_dir, "s1", compressed=["s1"])
        raw = vm.snapshots["s1"]["memory"]
        Path(raw).write_bytes(b"decompressed for a revert")
        vm.native_delete = False

        assert self._delete(sm, vm, "s1")[0] is True

        assert [t for t, _o, _b in vm.commits] == ["vda"]
        assert vm.snapshots == {} and vm.memory_files() == []

    def test_boxmans_blockcommit_keeps_one_another_snapshot_records(
            self, sm, tmp_path):
        vm, memory_dir = self._vm(tmp_path)
        self._takes(vm, memory_dir, "s1", compressed=["s1"])
        [zst] = vm.memory_files()
        vm.native_delete = False

        done, warned = self._delete(
            sm, vm, "s1", lambda: vm.recorded({zst: self.REPLICA}))

        assert done is True and vm.snapshots == {}
        assert vm.memory_files() == [zst]
        assert f"a snapshot of domain {self.REPLICA} still records it" in warned

    def test_boxmans_blockcommit_keeps_it_while_the_record_stays(
            self, sm, tmp_path):
        """The metadata delete that ends the blockcommit fails: the
        snapshot still records its memory image, which stays."""
        vm, memory_dir = self._vm(tmp_path)
        self._takes(vm, memory_dir, "s1", compressed=["s1"])
        [zst] = vm.memory_files()
        vm.native_delete = False
        vm.fail_metadata_delete = "error: failed to remove snapshot metadata"

        done, warned = self._delete(sm, vm, "s1")

        assert done is True and "s1" in vm.snapshots
        assert vm.memory_files() == [zst]
        assert "its memory image is kept" in warned

    @pytest.mark.parametrize("where", ["workdir", "memory_snapshot_dir"])
    def test_the_collapse_a_delete_falls_back_to_removes_it(
            self, sm, tmp_path, where):
        """libvirt before 9.0 refuses to delete s2, the current one, built
        on s1: boxman collapses it onto s1."""
        vm, memory_dir = self._vm(tmp_path, where)
        self._takes(vm, memory_dir, "s1", "s2", compressed=["s1", "s2"])
        s1_zst = f"{vm.snapshots['s1']['memory']}.zst"
        vm.native_delete = False

        assert self._delete(sm, vm, "s2")[0] is True

        assert sorted(vm.snapshots) == ["s1"]
        assert vm.memory_files() == [s1_zst]

    def test_the_collapse_a_delete_falls_back_to_keeps_one_recorded(
            self, sm, tmp_path):
        vm, memory_dir = self._vm(tmp_path)
        self._takes(vm, memory_dir, "s1", "s2", compressed=["s2"])
        s2_zst = f"{vm.snapshots['s2']['memory']}.zst"
        vm.native_delete = False

        done, warned = self._delete(
            sm, vm, "s2", lambda: vm.recorded({s2_zst: self.REPLICA}))

        assert done is True and sorted(vm.snapshots) == ["s1"]
        assert os.path.exists(s2_zst)
        assert f"a snapshot of domain {self.REPLICA} still records it" in warned

    # --- a collapse that drops snapshots ------------------------------------

    def test_a_collapse_removes_each_it_drops_but_one_recorded_elsewhere(
            self, sm, tmp_path):
        vm, memory_dir = self._vm(tmp_path, "memory_snapshot_dir")
        self._takes(vm, memory_dir, "s1", "s2", "s3", compressed=["s2", "s3"])
        s1_raw = vm.snapshots["s1"]["memory"]
        s3_zst = f"{vm.snapshots['s3']['memory']}.zst"

        done, warned = self._collapse(
            sm, vm, "s1", lambda: vm.recorded({s3_zst: self.REPLICA}))

        assert done is True and sorted(vm.snapshots) == ["s1"]
        assert vm.memory_files() == sorted([s1_raw, s3_zst])
        assert (f"not removing {s3_zst}, the memory image of deleted "
                f"snapshot 's3'") in warned

    def test_a_collapse_keeps_the_image_of_one_whose_record_stays(
            self, sm, tmp_path):
        vm, memory_dir = self._vm(tmp_path)
        self._takes(vm, memory_dir, "s1", "s2", compressed=["s2"])
        held = vm.memory_files()
        vm.fail_metadata_delete = "error: failed to remove snapshot metadata"

        done, warned = self._collapse(sm, vm, "s1")

        assert done is True and "s2" in vm.snapshots
        assert vm.memory_files() == held
        assert "its memory image is kept" in warned

    def test_a_collapse_with_the_records_unknown_keeps_what_it_drops(
            self, sm, tmp_path):
        vm, memory_dir = self._vm(tmp_path)
        self._takes(vm, memory_dir, "s1", "s2", compressed=["s2"])
        held = vm.memory_files()

        done, warned = self._collapse(sm, vm, "s1", lambda: None)

        assert done is True and sorted(vm.snapshots) == ["s1"]
        assert vm.memory_files() == held
        assert "could not be checked" in warned

    # --- take --force ---------------------------------------------------------

    def test_take_force_removes_it_where_the_snapshot_recorded_it(
            self, sm, tmp_path):
        """The memory_snapshot_dir changed since s1 was taken: --force
        deletes s1, whose ``.raw.zst`` neither libvirt's delete nor the
        leftover clear, which looks in the new directory, removed."""
        vm, old = self._vm(tmp_path, "memory_snapshot_dir")
        new = tmp_path / "new-memory-snapshots"
        new.mkdir()
        self._takes(vm, old, "s1", compressed=["s1"])

        execute, shell = _restoring(sm, vm)
        with execute, shell:
            assert sm.create_snapshot(
                vm.VM, str(new), "s1", "again", force=True,
                paths_in_use=vm.in_use, recorded_in_use=vm.recorded) is True

        assert vm.memory_files() == [str(new / "vm01_snapshot_s1.raw")]

    def test_take_force_keeps_one_another_snapshot_records_and_refuses(
            self, sm, tmp_path):
        """The retake would write its image where another domain's snapshot
        records one: the image stays, and the take is refused."""
        vm, memory_dir = self._vm(tmp_path)
        self._takes(vm, memory_dir, "s1", compressed=["s1"])
        [zst] = vm.memory_files()

        execute, shell = _restoring(sm, vm)
        with execute, shell, patch.object(sm.logger, "error") as error:
            assert sm.create_snapshot(
                vm.VM, str(memory_dir), "s1", "again", force=True,
                paths_in_use=vm.in_use,
                recorded_in_use=lambda: vm.recorded(
                    {zst: self.REPLICA})) is False

        assert vm.memory_files() == [zst]
        assert f"a snapshot of domain {self.REPLICA} records it" in " ".join(
            str(c.args[0]) for c in error.call_args_list)

    # --- the session hands each its scan -------------------------------------

    def test_the_session_hands_the_delete_and_the_collapse_its_scan(self):
        session = LibVirtSession(
            config={'provider': {'libvirt': {'virsh_cmd': 'false'}}})
        with patch.object(SnapshotManager, "delete_snapshot",
                          return_value=True) as delete, \
             patch.object(SnapshotManager, "collapse_to",
                          return_value=True) as collapse:
            session.snapshot_delete("vm01", "s1")
            session.snapshot_collapse("vm01", "s1")

        for call in (delete.call_args, collapse.call_args):
            assert call.kwargs["recorded_in_use"] == (
                session.memory_images_recorded)
        with patch.object(session, "snapshot_files_in_use",
                          return_value="scan") as scan:
            assert session.memory_images_recorded() == "scan"
        scan.assert_called_once_with(overlays_too=False)


class TestFlattenCdromOverlays:
    """Before a take, a CDROM left on an overlay of its ISO goes back on the
    ISO, the way a restore puts it back (#273): through update-device with
    a raw driver. ``change-media`` kept the overlay's ``type='qcow2'``, and
    on a domain a revert started it never got the tray open."""

    def test_switches_qcow_overlay_back_to_raw_iso(self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            vm._revert("s1")
            sm._flatten_cdrom_overlays(vm.VM, paths_in_use=vm.in_use)

        assert vm.live["sda"] == vm.config["sda"] == vm.iso
        assert "change-media" not in [c[0] for c in vm.calls]
        assert [driver for _f, driver, _p in vm.updates] == ["raw"]
        assert vm.files_beside_the_iso() == ["nixos-minimal.iso"]

    def test_skips_cdroms_already_pointing_at_raw_iso(self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            sm._flatten_cdrom_overlays(vm.VM, paths_in_use=vm.in_use)

        assert vm.updates == [] and vm.redefined == []
        assert ("qemu-img",) not in vm.calls


#: a snapshot's XML as libvirt 10.10 writes it after a revert to a snapshot
#: with children: indented, attributes in its own order, live chains with
#: indexes, a <seclabel> on a CDROM's source, an empty drive, and metadata
#: whose namespaces are declared on the elements that use them
_LIBVIRT_METADATA = (
    '    <metadata>\n'
    '      <libosinfo:libosinfo xmlns:libosinfo="http://libosinfo.org/xmlns/libvirt/domain/1.0">\n'
    '        <libosinfo:os id="http://nixos.org/nixos/unknown"/>\n'
    '      </libosinfo:libosinfo>\n'
    '      <boxman:disks xmlns:boxman="https://github.com/mherkazandjian/boxman/disks/1.0">\n'
    '        <boxman:disk name="root" target="vda" role="boot"/>\n'
    '      </boxman:disks>\n'
    '    </metadata>\n')


def _libvirt_devices(live: bool) -> str:
    chain = ("        <backingStore type='file' index='3'>\n"
             "          <format type='raw'/>\n"
             "          <source file='/iso/nixos.iso'/>\n"
             "          <backingStore/>\n"
             "        </backingStore>\n") if live else ""
    return (
        "    <devices>\n"
        "      <disk type='file' device='disk'>\n"
        "        <driver name='qemu' type='qcow2' discard='unmap'/>\n"
        "        <source file='/ws/vm01.s1'"
        + (" index='2'" if live else "") + "/>\n"
        "        <target dev='vda' bus='virtio'/>\n"
        "      </disk>\n"
        "      <disk device='cdrom' type='file'>\n"
        "        <driver name='qemu' type='qcow2'/>\n"
        "        <source file='/iso/nixos.1793626526'"
        + (" index='4'" if live else "") + ">\n"
        "          <seclabel model='dac' relabel='no'/>\n"
        "        </source>\n"
        + chain +
        "        <target dev='sda' bus='sata'/>\n"
        "        <readonly/>\n"
        + ("        <alias name='sata0-0-0'/>\n" if live else "") +
        "        <address type='drive' controller='0' bus='0' target='0' "
        "unit='0'/>\n"
        "      </disk>\n"
        "      <disk type='file' device='cdrom'>\n"
        "        <driver name='qemu'/>\n"
        "        <target dev='sdb' bus='sata'/>\n"
        "        <readonly/>\n"
        "      </disk>\n"
        "    </devices>\n")


def _recorded(overlays: dict) -> str:
    return "".join(
        f"    <disk name='{t}' snapshot='external' type='file'>\n"
        f"      <driver type='qcow2'/>\n"
        f"      <source file='{p}'/>\n"
        f"    </disk>\n" for t, p in overlays.items()) + \
        "    <disk name='sdb' snapshot='no'/>\n"


_LIBVIRT_SNAPSHOT_XML = (
    "<domainsnapshot>\n"
    "  <name>s2</name>\n"
    "  <state>running</state>\n"
    "  <parent>\n    <name>s1</name>\n  </parent>\n"
    "  <creationTime>1793626043</creationTime>\n"
    "  <memory snapshot='external' file='/ws/vm01_snapshot_s2.raw'/>\n"
    "  <disks>\n"
    + _recorded({"vda": "/ws/vm01.1793626528",
                          "sda": "/iso/nixos.1793626528"}) +
    "  </disks>\n"
    "  <revertDisks>\n"
    + _recorded({"vda": "/ws/vm01.1793626530",
                                "sda": "/iso/nixos.1793626530"}) +
    "  </revertDisks>\n"
    "  <domain type='kvm' id='3'>\n"
    "    <name>vm01</name>\n"
    + _LIBVIRT_METADATA + _libvirt_devices(True) +
    "  </domain>\n"
    "  <inactiveDomain type='kvm'>\n"
    "    <name>vm01</name>\n"
    + _LIBVIRT_METADATA + _libvirt_devices(False) +
    "  </inactiveDomain>\n"
    "  <cookie>\n"
    "    <cpu mode='custom' match='exact' check='full'>\n"
    "      <model fallback='forbid'>EPYC</model>\n"
    "    </cpu>\n"
    "  </cookie>\n"
    "</domainsnapshot>\n")


class TestRetargetingASnapshotRecord:
    """What is rewritten of a snapshot's XML, and what is refused."""

    @staticmethod
    def _section(root, tag: str, name: str):
        return next(d for d in root.find(tag) if d.get("name") == name)

    def test_a_record_as_libvirt_writes_it(self):
        retired = {"/iso/nixos.1793626526": "/iso/nixos.iso"}

        out = SnapshotManager._retarget_snapshot_xml(_LIBVIRT_SNAPSHOT_XML,
                                                     retired)

        root = ET.fromstring(out)
        for tag in ("disks", "revertDisks"):
            # a CDROM's overlay is no record's, retired or not
            sda = self._section(root, tag, "sda")
            assert sda.get("snapshot") == "no" and sda.find("source") is None
            assert self._section(root, tag, "vda").get("snapshot") == "external"
        for tag in ("domain", "inactiveDomain"):
            cdrom, empty = [d for d in root.find(tag).iter("disk")
                            if d.get("device") == "cdrom"]
            source = cdrom.find("source")
            assert source.get("file") == "/iso/nixos.iso"
            assert source.find("seclabel").get("relabel") == "no"
            assert cdrom.find("driver").get("type") == "raw"
            assert cdrom.find("backingStore") is None
            assert empty.find("source") is None
        # everything else, as text, as libvirt wrote it
        assert out.count(_LIBVIRT_METADATA) == 2
        assert ("<memory snapshot='external' "
                "file='/ws/vm01_snapshot_s2.raw'/>") in out
        assert "<parent>\n    <name>s1</name>\n  </parent>" in out
        assert "<model fallback='forbid'>EPYC</model>" in out

    def test_a_cdrom_saved_on_an_overlay_not_retired_is_left_on_it(self):
        out = SnapshotManager._retarget_snapshot_xml(_LIBVIRT_SNAPSHOT_XML,
                                                     {})

        root = ET.fromstring(out)
        cdrom = next(d for d in root.find("domain").iter("disk")
                     if d.get("device") == "cdrom")
        assert cdrom.find("source").get("file") == "/iso/nixos.1793626526"
        assert self._section(root, "disks", "sda").get("snapshot") == "no"

    def test_a_record_naming_none_of_them_is_left_alone(self, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        assert SnapshotManager._retarget_snapshot_xml(
            vm.snapshot_xml("s1"), {"/elsewhere.1": vm.iso}) is None

    def test_a_disk_in_a_default_namespace_is_refused(self, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        overlay = vm.snapshots["s1"]["disks"]["vda"]
        xml = vm.snapshot_xml("s1").replace(
            "<metadata>", "<metadata><disks xmlns='urn:x'><disk/></disks>")
        with pytest.raises(ValueError, match="disk elements"):
            SnapshotManager._retarget_snapshot_xml(xml, {overlay: vm.iso})


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
        """... handed the scan of what other snapshots record, which the
        delete asks before it removes the memory image (#274)."""
        scan = MagicMock(return_value=FilesInUse())
        with patch.object(sm.virsh, "execute", return_value=_result(ok=True)), \
             patch.object(sm, "delete_snapshot", return_value=True) as dele, \
             patch.object(sm, "_data_disk_targets", return_value=[]), \
             patch.object(sm.virsh, "execute_shell", return_value=_result()):
            assert sm._force_clear_for_retake(
                "vm01", str(tmp_path), "snapX", recorded_in_use=scan) is True
        dele.assert_called_once_with("vm01", "snapX", recorded_in_use=scan)

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

    @pytest.mark.parametrize("disk_dir", [None, "/fast/ov"])
    def test_the_session_hands_it_its_scan(self, tmp_path: Path, disk_dir):
        """Other domains' overlays are asked about only with a
        disk_snapshot_dir; without one, the memory images alone, as
        before."""
        session = LibVirtSession(
            config={'provider': {'libvirt': {'virsh_cmd': 'false'}}})
        with patch.object(SnapshotManager, "create_snapshot",
                          return_value=True) as create:
            session.snapshot_take(vm_name="vm01", vm_dir=str(tmp_path),
                                  snapshot_name="snapX", description="",
                                  force=True, disk_dir=disk_dir)

        assert create.call_args.kwargs["paths_in_use"] == (
            session.disk_paths_in_use)
        with patch.object(session, "snapshot_files_in_use",
                          return_value="scan") as scan:
            assert create.call_args.kwargs["recorded_in_use"]() == "scan"
        scan.assert_called_once_with(overlays_too=disk_dir is not None)


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
        assert not _created(execute)
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
        assert not _created(execute)
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
        assert not _created(execute)

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
        "<disk type='file' device='disk' snapshot='manual'>"
        "<target dev='vdb'/></disk>",
        "<disk type='file' device='disk' snapshot='internal'>"
        "<target dev='vdb'/></disk>",
    ], ids=["read-only", "snapshot-no", "snapshot-manual",
            "snapshot-internal"])
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

    def test_a_read_only_disk_set_to_be_snapshotted_gets_its_overlay_there(
            self, sm, tmp_path):
        """Its own ``snapshot='external'`` overrides the read-only default:
        libvirt takes it, and left unnamed its overlay would land beside
        it."""
        ok, [args] = self._create(
            sm, tmp_path, "/fast/ov",
            blklist=self._blklist(("vda", "/work/vm01.qcow2"),
                                  ("vdb", "/reference/ref.qcow2")),
            domain=self._domain(
                "<disk type='file' device='disk' snapshot='external'>"
                "<readonly/><target dev='vdb'/></disk>"))

        assert ok is True
        assert self._specs(args) == [
            "vda,snapshot=external,file=/fast/ov/vm01.s2",
            "vdb,snapshot=external,file=/fast/ov/ref.s2"]

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
            "<target dev='vdd'/></disk>",
            # read-only, but set to be snapshotted: libvirt takes it
            "<disk device='disk' snapshot='external'><readonly/>"
            "<target dev='vde'/></disk>",
            # set to another mode of their own: no external overlay
            "<disk device='disk' snapshot='manual'><target dev='vdf'/></disk>",
            "<disk device='disk' snapshot='internal'>"
            "<target dev='vdg'/></disk>")
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout=domain)) as execute:
            assert sm._snapshot_excluded_targets("vm01") == {
                "vdb", "vdc", "vdf", "vdg"}
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
