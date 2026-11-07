"""
``boxman snapshot prune [--cluster] [--vms] [--kind] [--min-age] [--all]
[--dry-run] [-y] [PATH ...]`` (#294): the directories it scans, the table it
prints, the prompt, what it removes, and its exit status.

The files are real, under ``tmp_path``; libvirt is :class:`_Session`, which
answers the scans the manager asks. Files go through the real
``remove_if_unchanged``; the overlays of CD-ROM images left for later go
through :meth:`_Session.remove_pending_iso_overlays`, which stands for the
removal a take runs (tested on its own in ``test_snapshot_prune.py``).
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
from boxman.providers.libvirt import snapshot_prune
from boxman.providers.libvirt.disk_cleanup import FilesInUse
from boxman.providers.libvirt.pending_overlays import PendingOverlays
from boxman.providers.libvirt.snapshot import IsoOverlayRemoval
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
        #: what the removal of the overlays left for later keeps, and why;
        #: those it fails to remove; whether it can tell nothing
        self.drain_keeps: dict[str, str] = {}
        self.drain_fails: set[str] = set()
        self.drain_undecided = False

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

    def refresh_pools_holding(self, paths):
        self.calls.append(("refresh_pools_holding", tuple(paths)))
        return []

    def disk_paths_in_use(self):
        self.calls.append(("disk_paths_in_use",))
        return FilesInUse(self.held)

    def remove_pending_iso_overlays(self, vm, scan=None, gone=False,
                                    only=None):
        """The removal a take runs of the overlays a VM's restores left for
        later, of the chains topped by *only*: each removed but those it
        keeps, asking *scan* once."""
        self.calls.append(("remove_pending_iso_overlays", vm, gone,
                           frozenset(only or ())))
        outcome = IsoOverlayRemoval()
        in_use = scan() if scan is not None else None
        if self.drain_undecided:
            outcome.decided = False
            outcome.kept = dict.fromkeys(only or (), "cannot be told")
            return outcome
        for layer in sorted(only or ()):
            why = self.drain_keeps.get(layer) or (in_use or {}).get(layer)
            if why or layer in self.drain_fails:
                outcome.kept[layer] = why or "it could not be removed"
                if layer in self.drain_fails:
                    outcome.failed.append(layer)
                continue
            os.unlink(layer)
            outcome.removed.append(layer)
        return outcome


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
                    "show_all": False, "cluster": None, "vms": "all",
                    "yes": False, "paths": []}
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


class TestADryRun:

    def test_says_nothing_was_changed(self, project, capsys):
        project.prune()
        assert capsys.readouterr().out.rstrip().endswith(
            "dry run: nothing was changed")

    def test_never_removes_and_never_asks(self, project, capsys):
        orphan = project.overlay(f"{VM}.1793626528")
        with patch("builtins.input", side_effect=AssertionError("asked")), \
                patch.object(snapshot_prune, "remove_if_unchanged",
                             side_effect=AssertionError("removed")):
            project.prune(dry_run=True, yes=True)
        assert os.path.exists(orphan)
        assert "These files will be removed" not in capsys.readouterr().out


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


class TestTheOverlaysOfCdromImagesLeftForLater:
    """A restore leaves the overlay libvirt's revert made of a CD-ROM's ISO
    beside the ISO, recorded for removal by the VM's next take or its
    destroy (#273). The ISO's directory is no snapshot directory, so they
    are not in the table: a note names each, and what keeps it now."""

    @staticmethod
    def _left(project, vm=VM):
        isodir = project.tmp / "image-cache"
        isodir.mkdir(exist_ok=True)
        iso = project.file("nixos.iso", b"the ISO", where=isodir)
        overlay = project.file(f"nixos.{len(vm)}", b"QFI\xfb", where=isodir)
        PendingOverlays().record(vm, [([overlay], iso)])
        return iso, overlay

    def test_each_is_named_with_what_removes_it(self, project, capsys):
        iso, overlay = self._left(project)

        project.prune()

        out = capsys.readouterr().out
        assert (f"note: {overlay} waits for removal: an overlay libvirt made "
                f"of {iso} for a CD-ROM of {VM}; a prune with no path, its "
                f"next `boxman snapshot take`, or its destroy, removes it once "
                f"nothing uses it\n") in out
        assert overlay not in _rows(out)

    def test_what_keeps_one_now_is_said(self, project, capsys):
        _iso, overlay = self._left(project)
        project.session.held[overlay] = "other01"

        project.prune()

        assert "(domain other01 uses it)" in capsys.readouterr().out

    def test_another_projects_are_not_listed(self, project, capsys):
        _iso, overlay = self._left(project, vm="other01")

        project.prune()

        assert overlay not in capsys.readouterr().out

    def test_one_left_for_a_vm_with_no_domain_any_more_is_named(
            self, project, capsys):
        """A VM removed outside boxman has no take or destroy to come:
        nothing removes what its restores left, so whichever project's
        prune runs names it."""
        gone = "bprj__else__bprj_c1_vm01"
        _iso, overlay = self._left(project, vm=gone)

        project.prune()

        out = capsys.readouterr().out
        assert (f"note: {overlay} was left for removal by a restore of "
                f"{gone}, which has no domain any more: nothing removes it"
                ) in out

    def test_another_libvirts_records_are_not_this_ones(self, project,
                                                        capsys):
        isodir = project.tmp / "image-cache"
        isodir.mkdir()
        iso = project.file("nixos.iso", b"the ISO", where=isodir)
        overlay = project.file("nixos.7", b"QFI\xfb", where=isodir)
        PendingOverlays(scope="local qemu:///session").record(
            VM, [([overlay], iso)])

        project.prune()

        assert overlay not in capsys.readouterr().out

    def test_an_interrupted_restores_backup_beside_the_iso_is_named(
            self, project, capsys):
        """Prune scans no image's directory; a restore left a .preserve
        there, of a CD-ROM overlay it backed up, and refuses while it is
        there."""
        iso, overlay = self._left(project)
        backup = project.file(f"{os.path.basename(overlay)}.preserve",
                              b"QFI\xfb", where=project.tmp / "image-cache")

        project.prune()

        out = capsys.readouterr().out
        assert (f"note: {backup} is an interrupted restore's backup of "
                f"{overlay}, an overlay of {iso} for a CD-ROM of {VM}, which "
                f"is in place") in out


    def test_a_prune_removes_them_through_the_removal_a_take_runs(
            self, project, capsys):
        """Prune is the one cleanup command: without a path it removes them
        too, as the VM's next take would, behind that removal's own checks
        and on one scan of every domain taken then; the prompt lists them,
        apart from what the table reclaims."""
        _iso, overlay = self._left(project)
        orphan = project.overlay(f"{VM}.1793626528")

        project.prune(dry_run=False, yes=True)

        out = capsys.readouterr().out
        assert (f"And these overlays of CD-ROM images, which restores left "
                f"for later removal, each once a scan of every domain finds "
                f"nothing uses it, as `boxman snapshot take` removes them:\n"
                f"\n  {overlay}\n") in out
        assert ("remove_pending_iso_overlays", VM, False,
                frozenset({overlay})) in project.session.calls
        assert project.session.calls.count(("disk_paths_in_use",)) == 1
        assert not os.path.exists(overlay) and not os.path.exists(orphan)
        assert f"removed {overlay} (" in out
        assert re.search(r"^reclaimed \S+ \(2 files\)$", out, re.M)

    def test_alone_they_are_still_asked_about(self, project, capsys):
        _iso, overlay = self._left(project)
        with patch("builtins.input", return_value="y") as asked:
            project.prune(dry_run=False)
        asked.assert_called_once()
        assert capsys.readouterr().out.count("These overlays of CD-ROM") == 1
        assert not os.path.exists(overlay)

    def test_one_scan_serves_every_vm(self, project):
        project.config["clusters"]["c1"]["vms"]["vm02"] = {}
        vm02 = "bprj__demo__bprj_c1_vm02"
        project.session.domain_list.append((vm02, domain_uuid(vm02)))
        self._left(project)
        isodir = project.tmp / "image-cache"
        iso2 = project.file("tools.iso", b"another ISO", where=isodir)
        other = project.file("tools.9", b"QFI\xfb", where=isodir)
        PendingOverlays().record(vm02, [([other], iso2)])

        project.prune(dry_run=False, yes=True)

        drained = [call[1] for call in project.session.calls
                   if call[0] == "remove_pending_iso_overlays"]
        assert drained == [VM, vm02]
        assert project.session.calls.count(("disk_paths_in_use",)) == 1

    @pytest.mark.parametrize("how", [
        {"dry_run": True}, {"kinds": parse_kinds("memory")},
        {"kinds": parse_kinds("preserve")}])
    def test_a_dry_run_or_a_kind_that_leaves_overlays_out_removes_none(
            self, project, how):
        _iso, overlay = self._left(project)
        project.prune(**{"dry_run": False, "yes": True, **how})
        assert os.path.exists(overlay)
        assert not any(call[0] == "remove_pending_iso_overlays"
                       for call in project.session.calls)

    def test_a_prune_of_named_paths_removes_none(self, project):
        _iso, overlay = self._left(project)
        orphan = project.overlay(f"{VM}.1793626528")
        project.prune(dry_run=False, yes=True, paths=[orphan])
        assert os.path.exists(overlay) and not os.path.exists(orphan)

    def test_one_a_domain_uses_stays_and_is_not_a_failure(self, project,
                                                          capsys):
        _iso, overlay = self._left(project)
        project.session.held[overlay] = "other01"
        project.prune(dry_run=False, yes=True)
        assert os.path.exists(overlay)
        assert "reclaimed 0.0B (0 files)" in capsys.readouterr().out

    def test_one_it_could_not_remove_exits_2_after_the_rest(self, project,
                                                            capsys):
        _iso, overlay = self._left(project)
        orphan = project.overlay(f"{VM}.1793626528")
        project.session.drain_fails = {overlay}
        with pytest.raises(SnapshotError,
                           match=f"{re.escape(overlay)}: it could not be "
                                 f"removed"):
            project.prune(dry_run=False, yes=True)
        assert not os.path.exists(orphan)
        assert "reclaimed " in capsys.readouterr().out

    def test_a_scan_that_cannot_tell_exits_2(self, project):
        _iso, overlay = self._left(project)
        project.session.drain_undecided = True
        with pytest.raises(SnapshotError, match="could not be told"):
            project.prune(dry_run=False, yes=True)
        assert os.path.exists(overlay)

    def test_a_vms_with_no_domain_any_more_are_left_to_destroy(
            self, project):
        _iso, overlay = self._left(project, vm="bprj__else__bprj_c1_vm01")
        project.prune(dry_run=False, yes=True)
        assert os.path.exists(overlay)
        assert not any(call[0] == "remove_pending_iso_overlays"
                       for call in project.session.calls)


class TestTheRemoval:
    """Without ``--dry-run`` the removable files go, after a prompt that
    lists them; ``-y`` skips it (design §5)."""

    def test_the_prompt_lists_them_and_yes_removes_them(self, project,
                                                        capsys):
        orphan = project.overlay(f"{VM}.1793626528")
        memory = project.file(f"{VM}_snapshot_s0.raw")
        with patch("builtins.input", return_value="y") as asked:
            project.prune(dry_run=False)
        out = capsys.readouterr().out
        asked.assert_called_once_with("Proceed? [y/N] ")
        assert re.search(r"^These files will be removed \(reclaiming \S+\):\n"
                         rf"\n  {re.escape(orphan)}\n  {re.escape(memory)}\n",
                         out, re.M)
        assert not os.path.exists(orphan) and not os.path.exists(memory)
        assert re.search(rf"^removed {re.escape(orphan)} \(\S+\)$", out, re.M)
        assert re.search(r"^reclaimed \S+ \(2 files\)$", out, re.M)
        assert ("refresh_pools_holding", (orphan, memory)) in (
            project.session.calls)

    @pytest.mark.parametrize("answer", ["n", "", "no", "maybe"])
    def test_anything_but_yes_aborts_and_removes_nothing(self, project,
                                                         capsys, answer):
        orphan = project.overlay(f"{VM}.1793626528")
        with patch("builtins.input", return_value=answer):
            project.prune(dry_run=False)
        assert capsys.readouterr().out.rstrip().endswith("Aborted.")
        assert os.path.exists(orphan)

    def test_the_end_of_input_aborts_and_removes_nothing(self, project,
                                                         capsys):
        orphan = project.overlay(f"{VM}.1793626528")
        with patch("builtins.input", side_effect=EOFError):
            project.prune(dry_run=False)
        assert capsys.readouterr().out.rstrip().endswith(
            "No input available, aborted.")
        assert os.path.exists(orphan)

    def test_y_skips_the_prompt(self, project):
        orphan = project.overlay(f"{VM}.1793626528")
        with patch("builtins.input", side_effect=AssertionError("asked")):
            project.prune(dry_run=False, yes=True)
        assert not os.path.exists(orphan)

    def test_with_nothing_to_remove_it_asks_nothing(self, project, capsys):
        with patch("builtins.input", side_effect=AssertionError("asked")):
            project.prune(dry_run=False)
        assert capsys.readouterr().out.rstrip().endswith("nothing to prune")

    def test_kept_and_unknown_files_are_never_pruned(self, project):
        orphan = project.overlay(f"{VM}.1793626528")
        doubtful = project.file(f"{VM}_snapshot_x.raw")
        project.session.owners[doubtful] = None        # unreadable header
        only = project.file(f"{VM}.s9.preserve", b"QFI\xfbonly copy")
        used = project.overlay(f"{VM}.s1")
        project.session.held[used] = "other01"

        project.prune(dry_run=False, yes=True)

        assert not os.path.exists(orphan)
        assert all(os.path.exists(p) for p in (doubtful, only, used))

    def test_the_newest_layer_goes_first(self, project):
        s1 = project.overlay(f"{VM}.s1")
        s2 = project.overlay(f"{VM}.s2", backing=s1)
        s3 = project.overlay(f"{VM}.s3", backing=s2)
        order = []
        real = snapshot_prune.remove_if_unchanged

        def remove(path, identity):
            order.append(path)
            return real(path, identity)

        with patch.object(snapshot_prune, "remove_if_unchanged",
                          side_effect=remove):
            project.prune(dry_run=False, yes=True)
        assert order == [s3, s2, s1]

    def test_a_failed_removal_exits_2_after_the_rest(self, project, capsys):
        stuck = project.overlay(f"{VM}.1793626528")
        orphan = project.overlay(f"{VM}.s1")
        memory = project.file(f"{VM}_snapshot_s0.raw")
        real = snapshot_prune.remove_if_unchanged

        def remove(path, identity):
            if path == stuck:
                raise PermissionError(13, "Permission denied")
            return real(path, identity)

        with patch.object(snapshot_prune, "remove_if_unchanged",
                          side_effect=remove), \
                pytest.raises(SnapshotError) as raised:
            project.prune(dry_run=False, yes=True)
        assert str(raised.value) == (
            f"could not prune 1 file(s): {stuck}: it could not be removed "
            f"(Permission denied)")
        assert os.path.exists(stuck)
        assert not os.path.exists(orphan) and not os.path.exists(memory)
        out = capsys.readouterr().out
        assert (f"could not remove {stuck}: it could not be removed "
                f"(Permission denied)") in out
        assert re.search(r"^reclaimed \S+ \(2 files\)$", out, re.M)

    def test_what_a_file_that_stays_is_built_on_stays_too(self, project,
                                                          capsys):
        s1 = project.overlay(f"{VM}.s1")
        s2 = project.overlay(f"{VM}.s2", backing=s1)

        def remove(path, identity):
            raise PermissionError(13, "Permission denied")

        with patch.object(snapshot_prune, "remove_if_unchanged",
                          side_effect=remove) as tried, \
                pytest.raises(SnapshotError, match="could not prune 2 file"):
            project.prune(dry_run=False, yes=True)
        assert [call.args[0] for call in tried.call_args_list] == [s2]
        assert os.path.exists(s1) and os.path.exists(s2)
        assert (f"could not remove {s1}: {s2}, which stays, is built on it"
                in capsys.readouterr().out)

    def test_a_file_replaced_after_the_listing_stays(self, project):
        """The prompt is where a listing can go stale: what took the name
        meanwhile is not the file listed, and is put back."""
        orphan = project.overlay(f"{VM}.1793626528")

        def replace_it(_prompt):
            # renamed over it, as a writer replaces a file: another inode
            with open(f"{orphan}.new", "wb") as fh:
                fh.write(b"someone else's")
            os.replace(f"{orphan}.new", orphan)
            return "y"

        with patch("builtins.input", side_effect=replace_it), \
                pytest.raises(SnapshotError,
                              match="replaced after it was listed"):
            project.prune(dry_run=False)
        with open(orphan, "rb") as fh:
            assert fh.read() == b"someone else's"
        assert not [n for n in os.listdir(project.wd)
                    if n.startswith(".boxman-removing-")]

    def test_a_scan_that_fails_prunes_nothing(self, project):
        orphan = project.overlay(f"{VM}.1793626528")
        project.session.references = None
        with patch("builtins.input", side_effect=AssertionError("asked")), \
                pytest.raises(SnapshotError, match="^nothing was pruned: "):
            project.prune(dry_run=False, yes=True)
        assert os.path.exists(orphan)


class TestNamedPaths:
    """``PATH ...``: only those, all or nothing (design §5)."""

    def test_only_the_paths_named_go(self, project, capsys):
        named = project.overlay(f"{VM}.1793626528")
        other = project.overlay(f"{VM}.s1")
        project.prune(dry_run=False, yes=True, paths=[named])
        assert not os.path.exists(named) and os.path.exists(other)
        assert list(_rows(capsys.readouterr().out)) == [named]

    def test_a_path_that_is_not_removable_stops_everything(self, project):
        named = project.overlay(f"{VM}.1793626528")
        recorded = project.overlay(f"{VM}.s1")
        project.session.references[recorded] = f"snapshot 's1' of {VM}"
        with pytest.raises(SnapshotError) as raised:
            project.prune(dry_run=False, yes=True, paths=[named, recorded])
        assert str(raised.value) == (
            f"nothing was pruned: {recorded}: kept: snapshot 's1' of {VM} "
            f"names it")
        assert os.path.exists(named) and os.path.exists(recorded)

    def test_an_unknown_one_is_refused(self, project):
        doubtful = project.file(f"{VM}_snapshot_x.raw")
        project.session.owners[doubtful] = None
        with pytest.raises(SnapshotError,
                           match="unknown: its save header could not be "
                                 "read"):
            project.prune(dry_run=False, yes=True, paths=[doubtful])
        assert os.path.exists(doubtful)

    @pytest.mark.parametrize("name", ["nothing-there", f"{VM}.qcow2"])
    def test_one_prune_does_not_list_is_refused(self, project, name):
        orphan = project.overlay(f"{VM}.1793626528")
        path = str(project.wd / name)
        with pytest.raises(SnapshotError,
                           match=f"{re.escape(path)}: it is no snapshot file "
                                 f"of the selected VMs"):
            project.prune(dry_run=False, yes=True, paths=[orphan, path])
        assert os.path.exists(orphan)

    def test_one_an_unnamed_removable_file_is_built_on_is_refused(
            self, project):
        """Removing it alone would leave the other on nothing."""
        s1 = project.overlay(f"{VM}.s1")
        s2 = project.overlay(f"{VM}.s2", backing=s1)
        with pytest.raises(SnapshotError,
                           match=f"{re.escape(s1)}: kept: {re.escape(s2)} "
                                 f"\\(not named\\) is built on it"):
            project.prune(dry_run=False, yes=True, paths=[s1])
        assert os.path.exists(s1) and os.path.exists(s2)

    def test_both_named_go_newest_first(self, project):
        s1 = project.overlay(f"{VM}.s1")
        s2 = project.overlay(f"{VM}.s2", backing=s1)
        project.prune(dry_run=False, yes=True, paths=[s1, s2])
        assert not os.path.exists(s1) and not os.path.exists(s2)

    def test_a_relative_path_names_the_same_file(self, project,
                                                 monkeypatch):
        orphan = project.overlay(f"{VM}.1793626528")
        monkeypatch.chdir(project.wd)
        project.prune(dry_run=False, yes=True,
                      paths=[os.path.basename(orphan)])
        assert not os.path.exists(orphan)

    def test_a_dry_run_refuses_them_as_the_real_run_would(self, project):
        recorded = project.overlay(f"{VM}.s1")
        project.session.references[recorded] = f"snapshot 's1' of {VM}"
        with pytest.raises(SnapshotError, match="^nothing was pruned: "):
            project.prune(dry_run=True, paths=[recorded])

    def test_a_kind_left_out_is_refused(self, project):
        orphan = project.overlay(f"{VM}.1793626528")
        with pytest.raises(SnapshotError,
                           match="--kind leaves its kind, revert-overlay, "
                                 "out"):
            project.prune(dry_run=False, yes=True, paths=[orphan],
                          kinds=parse_kinds("memory"))
        assert os.path.exists(orphan)


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

    def test_the_paths_and_y_reach_it(self, tmp_path):
        seen = []
        with patch.object(BoxmanManager, "snapshot_prune",
                          lambda self, args: seen.append(args)):
            self._run([*self._files(tmp_path), "snapshot", "prune", "-y",
                       "/a/one", "/a/two"], tmp_path)
        [args] = seen
        assert (args.yes, args.paths, args.dry_run) == (
            True, ["/a/one", "/a/two"], False)

    def test_a_refusal_exits_2(self, tmp_path):
        def refuse(self, args):
            raise SnapshotError("nothing was pruned: /a/one: kept: ...")

        with patch.object(BoxmanManager, "snapshot_prune", refuse), \
                patch("boxman.scripts.app.log") as log:
            code = self._run([*self._files(tmp_path), "snapshot", "prune",
                              "/a/one"], tmp_path)
        assert code == 2
        [said] = [call.args[0] for call in log.error.call_args_list]
        assert "nothing was pruned: /a/one" in said
