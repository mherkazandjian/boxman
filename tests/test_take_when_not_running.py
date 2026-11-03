"""
``snapshot take`` of a VM that is not running (#272).

libvirt refuses a memory spec for a shut-off domain ("memory state cannot be
saved with offline or disk-only snapshot"), and the take passed one
whatever the VM's state, so it failed for every VM that was not running.
Now what a snapshot holds follows the state:

- a shut-off VM gets a disk-only snapshot: ``--disk-only`` and no
  ``--memspec``, its CD-ROMs still left out, its overlays still where the
  disk_snapshot_dir puts them;
- a VM ``boxman down`` saved (libvirt managed save) is refused, before
  anything is snapshotted, with ``boxman up`` as the way forward;
- a running or a paused VM still saves its memory.

Then the rest of the lifecycle with a disk-only snapshot: list and log mark
it, a restore reverts it with no memory image to decompress, and delete and
collapse drop it.

What libvirt does was measured on libvirt 10.10 with QEMU 9.1, on a
throwaway domain: a disk-only snapshot of a shut-off domain records
``<state>shutoff</state>`` and ``<memory snapshot='no'/>``; reverting to it
leaves the domain shut off; a paused domain's memory snapshot leaves it
paused, and reverting to it brings it back paused; libvirt takes a
disk-only snapshot of a domain in managed save, and the next start restores
the saved memory against the disks as they were before it.
"""

from __future__ import annotations

import shutil
import subprocess
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import SnapshotError
from boxman.manager import BoxmanManager
from boxman.providers.libvirt.session import LibVirtSession
from boxman.providers.libvirt.snapshot import SnapshotManager

pytestmark = pytest.mark.unit

VM = "vm01"

#: ``domblklist --details`` of the domain: two disks and a CD-ROM that an
#: earlier revert left on a qcow2 overlay of its ISO (#273)
BLKLIST = (
    "Type   Device  Target  Source\n"
    "file   disk    vda     /ws/vm01.qcow2\n"
    "file   disk    vdb     /ws/vm01_data.qcow2\n"
    "file   cdrom   sda     /iso/seed.1793626528\n"
)

#: its definition, the CD-ROM's ISO recorded as the overlay's backing store:
#: what a running domain shows, and what a shut-off one shows after a revert
#: installed a definition captured while it ran (the issue's repro)
DOMXML = """\
<domain type='kvm'>
  <name>vm01</name>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/ws/vm01.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/ws/vm01_data.qcow2'/>
      <target dev='vdb' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='qcow2'/>
      <source file='/iso/seed.1793626528'/>
      <backingStore type='file'>
        <format type='raw'/>
        <source file='/iso/seed.iso'/>
      </backingStore>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
  </devices>
</domain>
"""


def _result(stdout: str = "", ok: bool = True, stderr: str = "") -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = 0 if ok else 1
    return r


def _disk_only_xml(name: str, files: dict[str, str],
                   parent: str | None = None) -> str:
    """A snapshot's metadata as libvirt 10.10 records a disk-only snapshot
    of a shut-off domain: no memory, each disk's new overlay, the CD-ROM
    left out."""
    disks = "".join(
        f"<disk name='{target}' snapshot='external' type='file'>"
        f"<driver type='qcow2'/><source file='{path}'/></disk>"
        for target, path in files.items())
    return (f"<domainsnapshot><name>{name}</name>"
            f"<description>{name}</description>"
            f"<state>shutoff</state>"
            + (f"<parent><name>{parent}</name></parent>" if parent else "")
            + "<creationTime>1793626043</creationTime>"
            "<memory snapshot='no'/>"
            f"<disks>{disks}<disk name='sda' snapshot='no'/></disks>"
            "<domain type='kvm'><name>vm01</name></domain>"
            "</domainsnapshot>")


def _shell_that_runs(calls: list | None = None):
    """An ``execute_shell`` double that runs the command, under tmp_path:
    the code decides by looking at the files, not at exit codes."""
    def run(cmd, *_a, **_kw):
        if calls is not None:
            calls.append(cmd)
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return _result(ok=proc.returncode == 0, stdout=proc.stdout,
                       stderr=proc.stderr)
    return run


@pytest.fixture
def sm() -> SnapshotManager:
    return SnapshotManager(provider_config={"use_sudo": False,
                                            "uri": "qemu:///system"})


class _Domain:
    """A ``virsh.execute`` double for one domain being snapshotted.

    *state* is the domain's as virsh lists it -- 'running', 'paused',
    'shut off' -- or 'saved': shut off, holding managed saved state.
    *failing* names the listing that fails: 'inactive' or 'saved'.
    """

    def __init__(self, state: str, failing: str | None = None):
        self.state = state
        self.failing = failing
        self.calls: list[tuple] = []

    def __call__(self, *args, **_kw):
        self.calls.append(args)
        verb = args[0]
        if verb == "list":
            which = "saved" if "--with-managed-save" in args else "inactive"
            assert "--inactive" in args and "--name" in args, args
            if self.failing == which:
                return _result(ok=False, stderr="error: failed to connect")
            if which == "saved":
                listed = [VM] if self.state == "saved" else []
            else:
                # another domain whose name starts the same is not this one
                listed = ["vm010"] + (
                    [VM] if self.state in ("shut off", "saved") else [])
            return _result(stdout="".join(f"{n}\n" for n in listed) + "\n")
        if verb == "domblklist":
            return _result(stdout=BLKLIST)
        if verb == "dumpxml":
            return _result(stdout=DOMXML)
        if verb in ("snapshot-create-as", "change-media"):
            return _result()
        raise AssertionError(f"unexpected virsh call: {args}")

    def calls_to(self, verb: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == verb]


def _take(sm: SnapshotManager, domain: _Domain, vm_dir: Path, **kwargs):
    """Take snapshot 's1' of the domain; ``(ok, logger)``."""
    with patch.object(sm.virsh, "execute", side_effect=domain), \
         patch.object(sm, "logger") as logger:
        ok = sm.create_snapshot(VM, str(vm_dir), "s1", "d", **kwargs)
    return ok, logger


def _logged(logger: MagicMock, level: str) -> str:
    return " ".join(str(c.args[0])
                    for c in getattr(logger, level).call_args_list)


class TestAShutOffVm:

    def test_it_gets_a_disk_only_snapshot_with_no_memory_spec(
            self, sm, tmp_path):
        domain = _Domain("shut off")

        ok, logger = _take(sm, domain, tmp_path)

        assert ok is True, _logged(logger, "error")
        [args] = domain.calls_to("snapshot-create-as")
        assert "--disk-only" in args and "--atomic" in args
        assert not any(a.startswith("--memspec") for a in args)

    def test_its_cdroms_are_still_left_out_of_it(self, sm, tmp_path):
        domain = _Domain("shut off")

        _take(sm, domain, tmp_path)

        [args] = domain.calls_to("snapshot-create-as")
        assert args[args.index("sda,snapshot=no") - 1] == "--diskspec"

    def test_its_overlays_still_go_to_the_disk_snapshot_dir(
            self, sm, tmp_path):
        domain = _Domain("shut off")
        overlays = tmp_path / "ovl"

        ok, _logger = _take(sm, domain, tmp_path, disk_dir=str(overlays))

        assert ok is True
        [args] = domain.calls_to("snapshot-create-as")
        assert f"vda,snapshot=external,file={overlays}/vm01.s1" in args
        assert f"vdb,snapshot=external,file={overlays}/vm01_data.s1" in args
        assert "--disk-only" in args

    def test_its_cdrom_is_put_back_on_its_iso_first_too(self, sm, tmp_path):
        """The saved definition would name a CD-ROM overlay, and a revert to
        the snapshot would stack its next one on it (#273). The switch does
        the persistent definition alone behind a raw driver, so ``--config``
        is enough: ``change-media`` kept ``type='qcow2'`` on the raw ISO,
        and the VM then failed to start (see
        test_a_stopped_vm_has_its_persistent_definition_switched_alone)."""
        domain = _Domain("shut off")
        in_use = MagicMock(name="disk_paths_in_use")
        with patch.object(sm, "_flatten_cdrom_overlays") as flatten:
            ok, _logger = _take(sm, domain, tmp_path, paths_in_use=in_use)

        assert ok is True
        flatten.assert_called_once_with(VM, paths_in_use=in_use)
        assert domain.calls_to("change-media") == []

    def test_the_take_says_it_is_disk_only(self, sm, tmp_path):
        ok, logger = _take(sm, _Domain("shut off"), tmp_path)

        assert ok is True
        assert "disk-only" in _logged(logger, "info")

    def test_compress_memory_is_a_note_not_a_failure(self, sm, tmp_path):
        with patch.object(sm, "compress_memory_file") as compress:
            ok, logger = _take(sm, _Domain("shut off"), tmp_path,
                               compress_memory=True)

        assert ok is True
        compress.assert_not_called()
        assert "none to compress" in _logged(logger, "info")
        assert _logged(logger, "warning") == ""
        assert _logged(logger, "error") == ""

    @pytest.mark.parametrize("name", ["vm01_snapshot_s1.raw",
                                      "vm01_snapshot_s1.raw.zst"])
    def test_a_file_at_its_memory_image_name_does_not_stop_it(
            self, sm, tmp_path, name):
        """A disk-only snapshot writes no memory image, so the file is not
        in its way -- and is left as it is."""
        (tmp_path / name).write_bytes(b"someone's RAM")
        domain = _Domain("shut off")

        ok, _logger = _take(sm, domain, tmp_path)

        assert ok is True
        assert len(domain.calls_to("snapshot-create-as")) == 1
        assert (tmp_path / name).read_bytes() == b"someone's RAM"

    def test_force_clears_the_old_one_and_takes_it_disk_only_again(
            self, sm, tmp_path):
        domain = _Domain("shut off")
        with patch.object(sm, "_force_clear_for_retake",
                          return_value=True) as clear:
            ok, _logger = _take(sm, domain, tmp_path, force=True)

        assert ok is True
        clear.assert_called_once()
        [args] = domain.calls_to("snapshot-create-as")
        assert "--disk-only" in args


class TestARunningOrPausedVm:
    """A paused domain's memory snapshot works, leaves it paused, and a
    revert to it brings it back paused (measured): it is taken as a running
    one is."""

    @pytest.mark.parametrize("state", ["running", "paused"])
    def test_it_still_saves_its_memory(self, sm, tmp_path, state):
        domain = _Domain(state)

        ok, _logger = _take(sm, domain, tmp_path)

        assert ok is True
        [args] = domain.calls_to("snapshot-create-as")
        assert f"--memspec={tmp_path}/vm01_snapshot_s1.raw" in args
        assert "--disk-only" not in args

    @pytest.mark.parametrize("state", ["running", "paused"])
    def test_its_cdrom_is_still_switched_back_to_the_iso_first(
            self, sm, tmp_path, state):
        """Through update-device behind a raw driver (#273), before the
        memory is saved: see TestARestorePutsTheCdromBackOnItsIso."""
        domain = _Domain(state)
        in_use = MagicMock(name="disk_paths_in_use")
        before_the_snapshot = []
        with patch.object(sm, "_flatten_cdrom_overlays",
                          side_effect=lambda *_a, **_k: before_the_snapshot.append(
                              not domain.calls_to("snapshot-create-as"))
                          ) as flatten:
            ok, _logger = _take(sm, domain, tmp_path, paths_in_use=in_use)

        assert ok is True
        flatten.assert_called_once_with(VM, paths_in_use=in_use)
        assert before_the_snapshot == [True]
        assert domain.calls_to("change-media") == []

    def test_its_memory_is_compressed_when_asked(self, sm, tmp_path):
        with patch.object(sm, "compress_memory_file",
                          return_value=True) as compress:
            ok, _logger = _take(sm, _Domain("running"), tmp_path,
                                compress_memory=True)

        assert ok is True
        compress.assert_called_once_with(
            str(tmp_path / "vm01_snapshot_s1.raw"), level=3)


class TestAVmInManagedSave:
    """libvirt takes a disk-only snapshot of it all the same, and the next
    start restores the saved memory against the disks as they were before
    the snapshot, writing into its backing files (measured)."""

    def test_it_is_refused_and_told_to_come_up_first(self, sm, tmp_path):
        domain = _Domain("saved")

        ok, logger = _take(sm, domain, tmp_path)

        assert ok is False
        assert domain.calls_to("snapshot-create-as") == []
        assert domain.calls_to("change-media") == []
        errors = _logged(logger, "error")
        assert "managed saved state" in errors
        assert "Run `boxman up` first" in errors
        assert f"virsh managedsave-remove {VM}" in errors

    def test_force_removes_nothing_before_the_refusal(self, sm, tmp_path):
        with patch.object(sm, "_force_clear_for_retake") as clear:
            ok, _logger = _take(sm, _Domain("saved"), tmp_path, force=True)

        assert ok is False
        clear.assert_not_called()

    @pytest.mark.parametrize("failing", ["inactive", "saved"])
    def test_a_state_that_cannot_be_read_refuses_the_take(
            self, sm, tmp_path, failing):
        domain = _Domain("shut off", failing=failing)
        with patch.object(sm, "_force_clear_for_retake") as clear:
            ok, logger = _take(sm, domain, tmp_path, force=True)

        assert ok is False
        assert domain.calls_to("snapshot-create-as") == []
        clear.assert_not_called()
        assert f"cannot tell whether {VM} is running" in _logged(logger,
                                                                 "error")


class _Retaking:
    """A ``virsh.execute`` double for ``take --force s2`` of a shut-off
    domain whose disk vda is *head*, its snapshots recording *records*
    ({snapshot: vda overlay}, oldest first)."""

    def __init__(self, head: Path, records: dict):
        self.head = str(head)
        self.records = {n: str(p) for n, p in records.items()}
        self.names = list(records)

    def __call__(self, *args, **_kw):
        verb = args[0]
        if verb == "snapshot-info":
            return _result()
        if verb == "snapshot-list":
            return _result(stdout="".join(f"{n}\n" for n in self.names) + "\n")
        if verb == "snapshot-dumpxml":
            i = self.names.index(args[2])
            return _result(stdout=_disk_only_xml(
                args[2], {"vda": self.records[args[2]]},
                self.names[i - 1] if i else None))
        if verb == "domblklist":
            return _result(stdout=(
                "Type   Device  Target  Source\n"
                f"file   disk    vda     {self.head}\n"
                "file   cdrom   sda     /iso/seed.iso\n"))
        if verb == "dumpxml":
            return _result(stdout=(
                "<domain><devices><disk type='file' device='disk'>"
                f"<source file='{self.head}'/><target dev='vda'/></disk>"
                "<disk type='file' device='cdrom'><source file='/iso/seed.iso'/>"
                "<target dev='sda'/><readonly/></disk></devices></domain>"))
        raise AssertionError(f"unexpected virsh call: {args}")


class TestForceKeepsWhatItCannotReplace:
    """``take --force`` deleted the snapshot first and asked about the
    files left at its names after, so a refusal there lost the snapshot it
    was to replace and took no new one. A restore leaves such a file: it
    puts back the overlay a revert replaced, under the name a retake
    writes."""

    def test_a_leftover_it_would_refuse_over_is_refused_before_the_delete(
            self, sm, tmp_path):
        overlays = tmp_path / "ovl"
        overlays.mkdir()
        # restore s2 put s2's old overlay back; s2 now records the revert's
        head = overlays / "vm01.1793629103"
        put_back = overlays / "vm01.s2"
        for path in (head, put_back, overlays / "vm01.s1"):
            path.write_bytes(b"overlay")
        domain = _Retaking(head, {"s1": overlays / "vm01.s1", "s2": head})
        with patch.object(sm.virsh, "execute", side_effect=domain), \
             patch.object(sm, "delete_snapshot") as delete, \
             patch.object(sm, "logger") as logger:
            ok = sm._force_clear_for_retake(
                VM, str(tmp_path), "s2", paths_in_use=dict,
                recorded_in_use=lambda: None, disk_dir=str(overlays))

        assert ok is False
        delete.assert_not_called()
        assert put_back.read_bytes() == b"overlay"
        assert "'s2' is left as it is" in _logged(logger, "error")

    def test_the_snapshots_own_files_do_not_hold_it_up(self, sm, tmp_path):
        overlays = tmp_path / "ovl"
        overlays.mkdir()
        own = overlays / "vm01.s2"          # s2's overlay, never reverted
        own.write_bytes(b"overlay")
        domain = _Retaking(own, {"s2": own})
        scan = MagicMock(return_value=None)

        def delete(_vm, _name):
            own.unlink()                    # libvirt's delete merges it away
            return True

        with patch.object(sm.virsh, "execute", side_effect=domain), \
             patch.object(sm, "delete_snapshot", side_effect=delete) as deleted:
            ok = sm._force_clear_for_retake(
                VM, str(tmp_path), "s2", paths_in_use=dict,
                recorded_in_use=scan, disk_dir=str(overlays))

        assert ok is True
        deleted.assert_called_once_with(VM, "s2")
        scan.assert_not_called()


# ── the manager: a saved VM refuses the take before anything is taken ───

def _manager(tmp_path: Path, saved: dict) -> BoxmanManager:
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = {
        "project": "demo",
        "clusters": {"cluster_1": {"workdir": str(tmp_path / "c1"),
                                   "vms": {"node01": {}, "node02": {}}}},
    }
    (tmp_path / "c1").mkdir()
    mgr.provider = MagicMock()
    mgr.provider.has_managed_save.side_effect = (
        lambda vm: saved.get(vm.rsplit("_", 1)[-1], False))
    mgr.provider.snapshot_take.return_value = True
    mgr.provider.validate_snapshot.return_value = (True, [])
    mgr.logger = MagicMock()
    mgr.cache = MagicMock()
    mgr.cache.projects = {}
    return mgr


def _sync_run_parallel(self, tasks, op_label='parallel task', max_workers=None):
    results, failures = {}, {}
    for label, target, args in tasks:
        try:
            results[label] = target(*args)
        except Exception as exc:
            failures[label] = f"{type(exc).__name__}: {exc}"
    return results, failures


def _take_all(mgr: BoxmanManager, monkeypatch) -> None:
    monkeypatch.setattr(BoxmanManager, "_run_parallel", _sync_run_parallel)
    mgr.snapshot_take(types.SimpleNamespace(
        snapshot_name="s1", snapshot_descr="", vms="all", cluster=None))


class TestTheManagerRefusesASavedVm:

    def test_one_saved_vm_leaves_every_vm_unsnapshotted(
            self, tmp_path, monkeypatch):
        mgr = _manager(tmp_path, {"node02": True})

        with pytest.raises(SnapshotError) as refused:
            _take_all(mgr, monkeypatch)

        mgr.provider.snapshot_take.assert_not_called()
        message = str(refused.value)
        assert "bprj__demo__bprj_cluster_1_node02: holds managed saved" in message
        assert "node01" not in message
        assert "Run `boxman up` first" in message
        assert "Nothing was snapshotted" in message

    def test_a_docker_compose_cluster_is_not_snapshotted_either(
            self, tmp_path, monkeypatch):
        mgr = _manager(tmp_path, {"node01": True})
        mgr.config["clusters"]["web"] = {
            "provider": "docker-compose", "workdir": str(tmp_path / "web"),
            "boxes": {}}
        compose = MagicMock()
        compose.snapshot_take_cluster.return_value = True
        compose.has_managed_save.side_effect = (
            mgr.provider.has_managed_save.side_effect)
        mgr.session_for_cluster = MagicMock(return_value=compose)

        with pytest.raises(SnapshotError, match="holds managed saved state"):
            _take_all(mgr, monkeypatch)

        compose.snapshot_take_cluster.assert_not_called()

    def test_a_probe_that_cannot_answer_refuses_too(self, tmp_path,
                                                    monkeypatch):
        mgr = _manager(tmp_path, {"node01": None})

        with pytest.raises(SnapshotError,
                           match="could not determine whether it holds"):
            _take_all(mgr, monkeypatch)

        mgr.provider.snapshot_take.assert_not_called()

    def test_with_none_saved_each_vm_is_taken(self, tmp_path, monkeypatch):
        mgr = _manager(tmp_path, {})

        _take_all(mgr, monkeypatch)

        taken = {c.kwargs["vm_name"]
                 for c in mgr.provider.snapshot_take.call_args_list}
        assert taken == {"bprj__demo__bprj_cluster_1_node01",
                         "bprj__demo__bprj_cluster_1_node02"}


# ── list and log mark a snapshot that holds no memory ───────────────────

class TestListAndLogMarkIt:

    @staticmethod
    def _graph(states: dict):
        def execute(*args, **_kw):
            if args[0] == "snapshot-list":
                return _result(stdout="".join(f"{n}\n" for n in states) + "\n")
            if args[0] == "snapshot-dumpxml":
                state = states[args[2]]
                return _result(stdout=(
                    f"<domainsnapshot><name>{args[2]}</name>"
                    + (f"<state>{state}</state>" if state else "")
                    + "</domainsnapshot>"))
            raise AssertionError(f"unexpected virsh call: {args}")
        return execute

    def test_the_detailed_list_says_which_saved_no_memory(self, sm):
        states = {"off": "shutoff", "on": "running", "paused": "paused",
                  "disk": "disk-snapshot", "unknown": None}
        with patch.object(sm.virsh, "execute",
                          side_effect=self._graph(states)):
            rows = sm.list_snapshots_detailed(VM)
            listed = sm.list_snapshots(VM)

        expected = {"off": True, "on": False, "paused": False,
                    "disk": True, "unknown": False}
        assert {r["name"]: r["disk_only"] for r in rows} == expected
        assert {r["name"]: r["disk_only"] for r in listed} == expected

    def test_snapshot_list_marks_it(self):
        session = LibVirtSession(config={"provider": {"libvirt": {}}})
        session.logger = MagicMock()
        snapshots = MagicMock()
        snapshots.list_snapshots.return_value = [
            {"name": "s1", "description": "off", "disk_only": True},
            {"name": "s2", "description": "on", "disk_only": False}]
        with patch("boxman.providers.libvirt.session.SnapshotManager",
                   return_value=snapshots):
            session.snapshot_list(VM)

        lines = [str(c.args[0]) for c in session.logger.info.call_args_list]
        assert "  Snapshot: s1 - Description: off (disk only)" in lines
        assert "  Snapshot: s2 - Description: on" in lines

    @staticmethod
    def _log(capsys, disk_only: dict, as_json: bool = False) -> str:
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {"project": "demo", "clusters": {"c": {
            "workdir": "/tmp/ws", "vms": {"node01": {}, "node02": {}}}}}
        mgr.provider = MagicMock()
        mgr.provider.snapshot_log_data.side_effect = lambda vm: {
            "chain": [{"name": "s1", "description": "d", "parent": None,
                       "depth": 0, "creation_time": "2026-11-02 13:00:00",
                       "disk_only": disk_only[vm.rsplit("_", 1)[-1]]}],
            "current": "s1"}
        mgr.logger = MagicMock()
        mgr.snapshot_log(types.SimpleNamespace(
            vms="all", cluster=None, as_json=as_json, no_graph=True))
        return capsys.readouterr().out

    def test_snapshot_log_marks_it_taken_off_on_every_vm(self, capsys):
        out = self._log(capsys, {"node01": True, "node02": True})

        assert '"d"  [c_node01,c_node02]  (disk only)  ← current' in out

    def test_snapshot_log_names_the_vms_it_was_taken_off_on(self, capsys):
        out = self._log(capsys, {"node01": False, "node02": True})

        assert "(disk only: c_node02)" in out

    def test_snapshot_log_says_nothing_of_a_memory_snapshot(self, capsys):
        out = self._log(capsys, {"node01": False, "node02": False})

        assert "disk only" not in out

    def test_snapshot_log_json_names_the_vms(self, capsys):
        import json

        out = self._log(capsys, {"node01": False, "node02": True},
                        as_json=True)

        assert json.loads(out)[0]["disk_only_for"] == ["c_node02"]


# ── restore, validate, delete and collapse of a disk-only snapshot ──────

class _Reverting:
    """A ``virsh.execute`` double for a restore of disk-only snapshots.

    *overlays* maps snapshot -> {disk: overlay file}, oldest first. A revert
    deletes the overlays of every snapshot newer than its target, as
    libvirt did with s2's when reverting to s1, and leaves the domain shut
    off whatever it was.
    """

    def __init__(self, overlays: dict, running: bool = False):
        self.overlays = overlays
        self.names = list(overlays)
        self.running = running
        self.calls: list[tuple] = []

    def __call__(self, *args, **_kw):
        self.calls.append(args)
        verb = args[0]
        if verb == "list":
            return _result(stdout=(f"{VM}\n\n" if self.running else "\n"))
        if verb == "suspend":
            return _result()
        if verb == "snapshot-info":
            return _result(stdout="Name: x\nState: shutoff\n")
        if verb == "snapshot-list":
            return _result(stdout="".join(f"{n}\n" for n in self.names) + "\n")
        if verb == "snapshot-dumpxml":
            name = args[2]
            i = self.names.index(name)
            return _result(stdout=_disk_only_xml(
                name, self.overlays[name], self.names[i - 1] if i else None))
        if verb == "snapshot-revert":
            for newer in self.names[self.names.index(args[2]) + 1:]:
                for path in self.overlays[newer].values():
                    Path(path).unlink()
            self.running = False
            return _result()
        raise AssertionError(f"unexpected virsh call: {args}")

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


def _overlay_files(tmp_path: Path, names: list[str]) -> dict:
    overlays = {}
    for name in names:
        overlays[name] = {}
        for target, stem in (("vda", "vm01"), ("vdb", "vm01_data")):
            path = tmp_path / f"{stem}.{name}"
            path.write_bytes(f"{stem} after {name}".encode())
            overlays[name][target] = str(path)
    return overlays


class TestTheRestOfItsLifecycle:

    def test_its_metadata_is_a_complete_inventory(self, sm, tmp_path):
        """No memory, and a CD-ROM with ``snapshot='no'``: the strict
        inventory a restore backs up from accepts it."""
        domain = _Reverting(_overlay_files(tmp_path, ["s1", "s2"]))
        with patch.object(sm.virsh, "execute", side_effect=domain):
            overlays, order = sm._strict_overlay_inventory(VM)

        assert order == ["s1", "s2"]
        assert overlays["s1"] == list(domain.overlays["s1"].values())

    def test_it_validates_with_no_memory_image(self, sm, tmp_path):
        domain = _Reverting(_overlay_files(tmp_path, ["s1"]))
        with patch.object(sm.virsh, "execute", side_effect=domain):
            assert sm.validate_snapshot(VM, "s1") == (True, [])

    @pytest.mark.parametrize("running", [False, True],
                             ids=["shut-off", "running"])
    def test_restoring_it_reverts_with_no_memory_image(
            self, sm, tmp_path, running):
        """Nothing to decompress or compress again; the overlays the revert
        deleted are put back; and a VM paused for the copy is not resumed:
        the revert left it shut off, and `boxman up` starts it."""
        overlays = _overlay_files(tmp_path, ["s1", "s2"])
        domain = _Reverting(overlays, running=running)
        shell: list[str] = []
        with patch.object(sm.virsh, "execute", side_effect=domain), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs(shell)), \
             patch.object(sm, "decompress_memory_file") as decompress, \
             patch.object(sm, "compress_memory_file") as compress:
            assert sm.snapshot_restore(VM, "s1") is True

        decompress.assert_not_called()
        compress.assert_not_called()
        assert not any("zstd" in cmd for cmd in shell)
        assert domain.verbs().count("snapshot-revert") == 1
        assert ("suspend" in domain.verbs()) is running
        assert "resume" not in domain.verbs()
        for name in ("s1", "s2"):
            for path in overlays[name].values():
                assert Path(path).read_bytes().endswith(name.encode())
        assert list(tmp_path.glob("*.preserve*")) == []


needs_qemu = pytest.mark.skipif(
    not (shutil.which("qemu-img") and shutil.which("qemu-io")),
    reason="needs qemu-img and qemu-io to build real overlay chains")


def _qemu(*args: str) -> str:
    return subprocess.run(list(args), check=True, capture_output=True,
                          text=True).stdout


def _write(image: Path, era: int) -> None:
    _qemu("qemu-io", "-c", f"write -P 0x{0xb0 + era:x} {era * 64}k 64k",
          str(image))


def _reads(image: Path, era: int) -> bool:
    proc = subprocess.run(
        ["qemu-io", "-r", "-c", f"read -P 0x{0xb0 + era:x} {era * 64}k 64k",
         str(image)], capture_output=True, text=True)
    return proc.returncode == 0 and "Pattern verification failed" not in proc.stdout


def _backing_chain(image: Path) -> list[str]:
    import json

    out = _qemu("qemu-img", "info", "-U", "--backing-chain",
                "--output=json", str(image))
    return [entry["filename"] for entry in json.loads(out)]


def _takes(tmp_path: Path, names: list[str]) -> tuple[Path, dict]:
    """What disk-only takes of a shut-off VM leave: each overlay a qcow2 on
    the one before, and a pattern written into each while it is the head.
    No memory image."""
    base = tmp_path / "vm01.qcow2"
    _qemu("qemu-img", "create", "-q", "-f", "qcow2", str(base), "1M")
    _write(base, 0)
    overlays: dict[str, Path] = {}
    backing = base
    for era, name in enumerate(names, start=1):
        overlay = tmp_path / f"vm01.{name}"
        _qemu("qemu-img", "create", "-q", "-f", "qcow2", "-b", str(backing),
              "-F", "qcow2", str(overlay))
        _write(overlay, era)
        overlays[name] = overlay
        backing = overlay
    return base, overlays


class _Offline:
    """A ``virsh.execute`` double for a shut-off domain with disk-only
    snapshots, for collapse and delete. *native_delete* is whether libvirt
    deletes an external snapshot itself (it does from 9.0; measured on
    10.10); otherwise it refuses, as older libvirt did, and boxman
    collapses."""

    def __init__(self, overlays: dict, head: Path, native_delete: bool):
        self.overlays = {n: str(p) for n, p in overlays.items()}
        self.names = list(overlays)
        self.head = str(head)
        self.native_delete = native_delete
        self.calls: list[tuple] = []

    def __call__(self, *args, **_kw):
        self.calls.append(args)
        verb = args[0]
        if verb == "snapshot-info":
            return (_result() if args[2] in self.names
                    else _result(ok=False, stderr="error: not found"))
        if verb == "snapshot-list":
            return _result(stdout="".join(f"{n}\n" for n in self.names) + "\n")
        if verb == "snapshot-dumpxml":
            name = args[2]
            i = self.names.index(name)
            return _result(stdout=_disk_only_xml(
                name, {"vda": self.overlays[name]},
                self.names[i - 1] if i else None))
        if verb == "domblklist":
            return _result(stdout=(
                "Type   Device  Target  Source\n"
                f"file   disk    vda     {self.head}\n"
                "file   cdrom   sda     /iso/seed.iso\n"))
        if verb == "dumpxml":
            return _result(stdout=(
                "<domain><devices><disk type='file' device='disk'>"
                f"<source file='{self.head}'/><target dev='vda'/></disk>"
                "</devices></domain>"))
        if verb == "snapshot-delete":
            if "--metadata" in args or self.native_delete:
                self.names.remove(args[2])
                return _result()
            return _result(ok=False, stderr=(
                "error: unsupported configuration: deletion of 1 external "
                "disk snapshots not supported yet"))
        raise AssertionError(f"unexpected virsh call: {args}")


class TestDeleteAndCollapseDropIt:

    def test_libvirt_deletes_it_itself_where_it_can(self, sm, tmp_path):
        overlays = {name: tmp_path / f"vm01.{name}" for name in ("s1", "s2")}
        for path in overlays.values():
            path.write_bytes(b"overlay")
        domain = _Offline(overlays, overlays["s2"], native_delete=True)
        shell: list[str] = []
        with patch.object(sm.virsh, "execute", side_effect=domain), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs(shell)):
            assert sm.delete_snapshot(VM, "s2") is True

        assert domain.names == ["s1"]
        assert shell == []

    @needs_qemu
    def test_the_newest_is_collapsed_away_where_libvirt_cannot(
            self, sm, tmp_path):
        base, overlays = _takes(tmp_path, ["s1", "s2", "s3"])
        head = overlays["s3"]
        domain = _Offline(overlays, head, native_delete=False)
        shell: list[str] = []
        with patch.object(sm.virsh, "execute", side_effect=domain), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs(shell)):
            assert sm.delete_snapshot(VM, "s3") is True

        assert domain.names == ["s1", "s2"]
        assert head.is_file()
        assert all(_reads(head, era) for era in range(4))
        # no memory image to remove: only what the collapse itself runs
        assert not any(".raw" in cmd for cmd in shell)

    @needs_qemu
    def test_a_collapse_over_it_keeps_the_data_and_the_head(
            self, sm, tmp_path):
        base, overlays = _takes(tmp_path, ["s1", "s2", "s3"])
        head = overlays["s3"]
        domain = _Offline(overlays, head, native_delete=True)
        shell: list[str] = []
        with patch.object(sm.virsh, "execute", side_effect=domain), \
             patch.object(sm.virsh, "execute_shell",
                          side_effect=_shell_that_runs(shell)):
            assert sm.collapse_to(VM, "s1") is True

        assert domain.names == ["s1"]
        assert _backing_chain(head) == [str(head), str(overlays["s1"]),
                                        str(base)]
        assert all(_reads(head, era) for era in range(4))
        assert not overlays["s2"].exists()
        assert not any(".raw" in cmd for cmd in shell)
