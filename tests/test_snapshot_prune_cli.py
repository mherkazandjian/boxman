"""
``boxman snapshot prune [--cluster] [--vms] [--kind] [--min-age] [--all]
--dry-run`` (#294): the directories it scans, the table it prints, and its
exit status. This version only lists: without ``--dry-run`` it refuses.

The files are real, under ``tmp_path``; libvirt is :class:`_Session`, which
answers the scans the manager asks and removes nothing.
"""

from __future__ import annotations

import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from boxman.exceptions import ConfigError, SnapshotError
from boxman.manager import BoxmanManager
from boxman.providers.libvirt.snapshot_prune import (
    Held,
    References,
    SnapshotRecord,
    parse_kinds,
)
from boxman.scripts.app import main
from conftest import domain_uuid

pytestmark = pytest.mark.unit

VM = "bprj__demo__bprj_c1_vm01"
UUID = domain_uuid(VM)


class _Session:
    """What the manager asks libvirt, answered from attributes; every call
    is recorded."""

    def __init__(self) -> None:
        self.provider_config = {"uri": "qemu:///system", "runtime": "local"}
        self.domain_list = [(VM, UUID), ("other01", domain_uuid("other01"))]
        self.held = Held()
        self.references = References()
        self.headers: dict[str, dict | None] = {}
        self.owners: dict[str, tuple[str, str] | None] = {}
        self.pools: list[tuple[str, str]] | None = []
        self.locked: set[str] = set()
        self.calls: list[tuple] = []

    def domains(self):
        self.calls.append(("domains",))
        return self.domain_list

    def snapshot_references(self):
        self.calls.append(("snapshot_references",))
        return self.references

    def files_held(self):
        self.calls.append(("files_held",))
        return self.held

    def image_headers(self, paths):
        self.calls.append(("image_headers", tuple(paths)))
        return {path: self.headers.get(
            path, {"format": "not qcow2", "backing": None}) for path in paths}

    def memory_image_owner(self, path):
        self.calls.append(("memory_image_owner", path))
        return self.owners.get(path, (VM, UUID))

    def image_locks_held(self, paths):
        self.calls.append(("image_locks_held", tuple(paths)))
        return {path for path in paths if path in self.locked}

    def disk_snapshot_pool_dirs(self, project, cluster):
        self.calls.append(("disk_snapshot_pool_dirs", project, cluster))
        return self.pools


class _Project:
    """A project ``demo`` with cluster ``c1`` (vm01, a data disk) in a
    workdir under ``tmp_path``, and the manager that runs prune on it."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.wd = tmp_path / "ws" / "c1"
        self.wd.mkdir(parents=True)
        self.base = self.file(f"{VM}.qcow2", b"QFI\xfb")
        self.session = _Session()
        self.session.held[self.base] = VM
        self.cache = tmp_path / "cache"
        self.cache.mkdir()
        self.config = {
            "project": "demo",
            "clusters": {"c1": {
                "workdir": str(self.wd),
                "vms": {"vm01": {"disks": [{"name": "data"}]}}}}}

    def file(self, name, data=b"data", where=None) -> str:
        path = (where or self.wd) / name
        path.write_bytes(data)
        return str(path)

    def overlay(self, name, backing=None, where=None) -> str:
        path = self.file(name, b"QFI\xfb", where)
        self.session.headers[path] = {"format": "qcow2",
                                      "backing": backing or self.base}
        return path

    def manager(self) -> BoxmanManager:
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = self.config
        mgr.app_config = {"cache": {"cache_dir": str(self.cache)}}
        mgr._runtime_name = "local"
        mgr._sessions = {"libvirt": self.session}
        mgr._provider = self.session
        return mgr

    def prune(self, **kwargs):
        defaults = {"dry_run": True, "kinds": None, "min_age": 0,
                    "show_all": False, "cluster": None, "vms": "all"}
        return self.manager().snapshot_prune(
            SimpleNamespace(**{**defaults, **kwargs}))


@pytest.fixture
def project(tmp_path) -> _Project:
    return _Project(tmp_path)


def _rows(out: str) -> dict[str, list[str]]:
    """``{path: [status, size, kind, reason]}`` of the table."""
    rows = {}
    for line in out.splitlines():
        found = re.match(r"^(removable|kept|unknown)\s+(\S+)\s+(\S+)\s+"
                         r"(\S+)\s*(.*)$", line)
        if found:
            status, size, kind, path, reason = found.groups()
            rows[path] = [status, size, kind, reason]
    return rows


class TestOnlyListsInThisVersion:

    def test_without_dry_run_it_refuses_before_reading_anything(
            self, project):
        with pytest.raises(ConfigError, match="run it with --dry-run"):
            project.prune(dry_run=False)
        assert project.session.calls == []

    def test_dry_run_says_nothing_was_changed(self, project, capsys):
        project.prune()
        assert capsys.readouterr().out.rstrip().endswith(
            "dry run: nothing was changed")


class TestTheTable:

    def test_each_file_with_its_size_kind_and_reason_then_the_totals(
            self, project, capsys):
        orphan = project.overlay(f"{VM}.1793626528")
        recorded = project.overlay(f"{VM}.s1")
        project.session.references[recorded] = f"snapshot 's1' of {VM}"
        copy = project.file(f"{VM}.s9.preserve", b"QFI\xfbonly copy")

        project.prune()

        out = capsys.readouterr().out
        assert out.startswith(f"scanned 2 domains (qemu:///system), 1 "
                              f"directory: {project.wd}\n")
        assert re.search(r"^STATUS\s+SIZE\s+KIND\s+PATH\s+REASON$", out, re.M)
        rows = _rows(out)
        assert rows[orphan][0::2] == ["removable", "revert-overlay"]
        assert rows[orphan][3].startswith("made by a revert on ")
        assert rows[copy][:1] == ["kept"]
        assert "the only copy" in rows[copy][3]
        assert recorded not in rows
        assert re.search(r"^would reclaim \S+ \(1 file\); kept 1 \(\S+\); "
                         r"unknown 0 \(0\.0B\); 1 recorded or in use not "
                         r"shown \(--all\)$", out, re.M)

    def test_size_is_what_the_file_takes_on_disk(self, project, capsys):
        sparse = project.overlay(f"{VM}.s1")
        with open(sparse, "r+b") as fh:
            fh.truncate(1 << 30)            # 1 GiB long, a few blocks used
        project.prune()
        assert _rows(capsys.readouterr().out)[sparse][1] != "1.0G"

    def test_all_lists_the_recorded_and_in_use_files_too(self, project,
                                                         capsys):
        recorded = project.overlay(f"{VM}.s1")
        project.session.references[recorded] = f"snapshot 's1' of {VM}"
        project.prune(show_all=True)
        out = capsys.readouterr().out
        assert _rows(out)[recorded] [0::3] == [
            "kept", f"snapshot 's1' of {VM} names it"]
        assert "not shown" not in out

    def test_kind_lists_only_those_kinds(self, project, capsys):
        project.overlay(f"{VM}.s1")
        memory = project.file(f"{VM}_snapshot_s0.raw")
        project.prune(kinds=parse_kinds("memory"))
        assert list(_rows(capsys.readouterr().out)) == [memory]

    def test_the_default_min_age_keeps_a_fresh_file(self, project, capsys):
        orphan = project.overlay(f"{VM}.s1")
        project.prune(min_age=3600)
        status, _size, _kind, reason = _rows(capsys.readouterr().out)[orphan]
        assert (status, reason) == (
            "kept", "too recent (changed 0s ago; --min-age 1h)")

    def test_with_nothing_left_over_it_says_so(self, project, capsys):
        project.prune()
        out = capsys.readouterr().out
        assert "no snapshot file of these VMs is left over" in out
        assert "would reclaim 0.0B (0 files)" in out

    def test_notes_name_stranded_directories_shared_copies_and_both_forms(
            self, project, capsys):
        (project.wd / ".boxman-removing-x1").mkdir()
        overlay = project.overlay(f"{VM}.s1")
        copy = project.file(f"{VM}.s1.preserve", b"QFI\xfb")
        st = os.stat(overlay)
        os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns))
        project.session.headers[copy] = project.session.headers[overlay]
        raw = project.file(f"{VM}_snapshot_s2.raw")
        project.file(f"{VM}_snapshot_s2.raw.zst")
        project.session.references.snapshots[VM] = {
            "s2": SnapshotRecord(memory=raw)}
        project.session.references[raw] = f"snapshot 's2' of {VM}"
        project.session.references[f"{raw}.zst"] = f"snapshot 's2' of {VM}"

        project.prune()

        out = capsys.readouterr().out
        assert (f"note: {project.wd / '.boxman-removing-x1'} is a directory "
                f"a removal left behind") in out
        assert "removing it frees only those it does not share" in out
        assert (f"note: both {raw} and {raw}.zst are there; `boxman storage "
                f"compress-snapshots` keeps one of them") in out


class TestTheLockProbe:

    def test_only_overlays_it_would_list_as_removable_are_probed(
            self, project):
        orphan = project.overlay(f"{VM}.s1")
        used = project.overlay(f"{VM}.1793626528")
        project.session.held[used] = VM
        project.file(f"{VM}_snapshot_s0.raw")
        project.prune()
        assert ("image_locks_held", (orphan,)) in project.session.calls

    def test_a_held_lock_keeps_the_file_and_what_it_is_built_on(
            self, project, capsys):
        s1 = project.overlay(f"{VM}.s1")
        s2 = project.overlay(f"{VM}.s2", backing=s1)
        project.session.locked = {s2}
        project.prune()
        rows = _rows(capsys.readouterr().out)
        assert rows[s2][3] == "a process holds its image lock"
        assert rows[s1][3] == f"{s2} (kept) is built on it"


class TestAScanThatFails:

    @pytest.mark.parametrize("scan", ["held", "references"])
    def test_lists_every_file_as_unknown_and_exits_2(self, project, scan,
                                                     capsys):
        orphan = project.overlay(f"{VM}.s1")
        setattr(project.session, scan, None)
        with pytest.raises(SnapshotError, match="^nothing was pruned: "):
            project.prune()
        assert _rows(capsys.readouterr().out)[orphan][0] == "unknown"

    def test_pools_that_cannot_be_read_fail_it_too(self, project, capsys):
        orphan = project.overlay(f"{VM}.s1")
        project.session.pools = None
        with pytest.raises(SnapshotError, match="storage pools could not"):
            project.prune()
        assert _rows(capsys.readouterr().out)[orphan][0] == "unknown"

    def test_a_domain_list_that_cannot_be_read_fails_it(self, project):
        project.overlay(f"{VM}.s1")
        project.session.domain_list = None
        with pytest.raises(SnapshotError, match="domains could not be listed"):
            project.prune()


class TestTheDirectories:

    def _scanned(self, capsys) -> str:
        return capsys.readouterr().out.splitlines()[0]

    def test_the_snapshot_dirs_set_now_are_scanned(self, project, capsys):
        fast, mem = project.tmp / "fast", project.tmp / "mem"
        fast.mkdir()
        mem.mkdir()
        project.config["clusters"]["c1"].update(
            disk_snapshot_dir=str(fast), memory_snapshot_dir=str(mem))
        orphan = project.overlay(f"{VM}.s1", where=fast)
        memory = project.file(f"{VM}_snapshot_s0.raw", where=mem)
        project.prune()
        out = capsys.readouterr().out
        assert out.splitlines()[0].endswith(
            f"3 directories: {project.wd}, {fast}, {mem}")
        assert {orphan, memory} <= set(_rows(out))

    def test_a_disk_snapshot_dir_set_before_is_found_by_its_pool(
            self, project, capsys):
        old = project.tmp / "old-fast"
        old.mkdir()
        project.session.pools = [("bprj__demo__bprj_c1_disk_snapshots",
                                  str(old))]
        orphan = project.overlay(f"{VM}.s1", where=old)
        project.prune()
        assert orphan in _rows(capsys.readouterr().out)
        assert ("disk_snapshot_pool_dirs", "demo", "c1") in (
            project.session.calls)

    def test_and_by_an_overlay_a_snapshot_records_there_under_a_takes_name(
            self, project, capsys):
        old, odd = project.tmp / "old-fast", project.tmp / "odd"
        old.mkdir()
        odd.mkdir()
        project.session.references.snapshots[VM] = {
            "s1": SnapshotRecord(disks={"vda": str(old / f"{VM}.s1")},
                                 taken_on={"vda": project.base}),
            # a revert's overlay, or a file the user named: not a take's
            "s2": SnapshotRecord(disks={"vda": str(odd / "mine.qcow2")},
                                 taken_on={"vda": project.base})}
        project.prune()
        first = capsys.readouterr().out.splitlines()[0]
        assert str(old) in first and str(odd) not in first

    def test_a_memory_image_recorded_elsewhere_brings_its_directory_in(
            self, project, capsys):
        old = project.tmp / "old-mem"
        old.mkdir()
        project.session.references.snapshots[VM] = {
            "s1": SnapshotRecord(memory=str(old / f"{VM}_snapshot_s1.raw"))}
        orphan = project.file(f"{VM}_snapshot_s0.raw", where=old)
        project.prune()
        assert orphan in _rows(capsys.readouterr().out)

    def test_the_image_cache_and_a_media_directory_are_never_scanned(
            self, project, capsys):
        media = project.tmp / "isos"
        media.mkdir()
        project.session.held.media[str(media / "tools.iso")] = "other01"
        project.session.references.snapshots[VM] = {
            "s1": SnapshotRecord(disks={"vda": str(media / f"{VM}.s1")},
                                 taken_on={"vda": project.base}),
            "s2": SnapshotRecord(memory=str(
                project.cache / f"{VM}_snapshot_s2.raw"))}
        project.overlay(f"{VM}.s1", where=media)
        project.prune()
        out = capsys.readouterr().out
        assert out.splitlines()[0].endswith(f"1 directory: {project.wd}")
        assert (f"note: not scanned: {media}, which holds "
                f"{media / 'tools.iso'}, a media source of domain other01"
                in out)
        assert f"note: not scanned: {project.cache}, the image cache" in out

    def test_a_directory_reached_twice_is_scanned_once(self, project,
                                                       capsys):
        project.config["clusters"]["c1"]["memory_snapshot_dir"] = str(
            project.tmp / "link")
        (project.tmp / "link").symlink_to(project.wd)
        project.prune()
        assert self._scanned(capsys).endswith(f"1 directory: {project.wd}")

    def test_a_compose_cluster_is_skipped_with_a_note(self, project,
                                                      capsys):
        project.config["clusters"]["web"] = {"provider": "docker-compose",
                                             "boxes": {}}
        project.prune()
        assert ("note: skipped docker-compose cluster web: its snapshots are "
                "container images") in capsys.readouterr().out

    def test_an_unknown_cluster_is_a_config_error(self, project):
        with pytest.raises(ConfigError, match="cluster 'nope' not found"):
            project.prune(cluster="nope")

    def test_vms_selects_whose_files_are_listed(self, project, capsys):
        project.config["clusters"]["c1"]["vms"]["vm02"] = {}
        vm02 = "bprj__demo__bprj_c1_vm02"
        project.session.domain_list.append((vm02, domain_uuid(vm02)))
        project.overlay(f"{vm02}.s1", backing=project.file(f"{vm02}.qcow2"))
        mine = project.overlay(f"{VM}.s1")
        project.prune(vms="vm01")
        assert list(_rows(capsys.readouterr().out)) == [mine]

    def test_under_the_docker_runtime_only_the_workdirs_are_scanned(
            self, project, capsys):
        mgr = project.manager()
        mgr._runtime_name = "docker-compose"
        project.overlay(f"{VM}.s1")
        mgr.snapshot_prune(SimpleNamespace(
            dry_run=True, kinds=None, min_age=0, show_all=False,
            cluster=None, vms="all"))
        assert self._scanned(capsys).endswith(f"1 directory: {project.wd}")

    def test_a_snapshot_dir_under_the_docker_runtime_is_refused(
            self, project):
        mgr = project.manager()
        mgr._runtime_name = "docker-compose"
        project.config["clusters"]["c1"]["memory_snapshot_dir"] = str(
            project.tmp / "mem")
        with pytest.raises(ConfigError, match="not supported under"):
            mgr.snapshot_prune(SimpleNamespace(
                dry_run=True, kinds=None, min_age=0, show_all=False,
                cluster=None, vms="all"))


class TestDispatch:

    @staticmethod
    def _run(argv, tmp_path):
        with patch.object(sys, "argv", ["boxman", *argv]), \
                patch("boxman.config_cache.DEFAULT_CACHE_DIR",
                      str(tmp_path / "cache")):
            try:
                main()
                return 0
            except SystemExit as exc:
                return exc.code if isinstance(exc.code, int) else 0

    @staticmethod
    def _files(tmp_path):
        conf = tmp_path / "conf.yml"
        conf.write_text(
            "version: '1.0'\nproject: demo\n"
            f"workspace:\n  path: {tmp_path / 'ws'}\n"
            "clusters:\n  c1:\n"
            f"    workdir: {tmp_path / 'ws' / 'c1'}\n    vms: {{}}\n")
        boxman_yml = tmp_path / "boxman.yml"
        boxman_yml.write_text("runtime: local\nproviders:\n  libvirt:\n"
                              "    uri: qemu:///system\n")
        return ["--conf", str(conf), "--boxman-conf", str(boxman_yml)]

    def test_it_reaches_the_handler_with_its_flags(self, tmp_path):
        seen = []
        with patch.object(BoxmanManager, "snapshot_prune",
                          lambda self, args: seen.append(args)):
            code = self._run([*self._files(tmp_path), "snapshot", "prune",
                              "--dry-run", "--kind", "overlay,memory",
                              "--min-age", "30m", "--all"], tmp_path)
        assert code in (0, None)
        [args] = seen
        assert (args.dry_run, args.min_age, args.show_all) == (True, 1800,
                                                               True)
        assert args.kinds == parse_kinds("overlay,memory")

    def test_without_dry_run_it_exits_2(self, tmp_path):
        with patch("boxman.scripts.app.log") as log:
            code = self._run([*self._files(tmp_path), "snapshot", "prune"],
                             tmp_path)
        assert code == 2
        [said] = [call.args[0] for call in log.error.call_args_list]
        assert "run it with --dry-run" in said
        assert "Removing those files comes in a later version" in said
