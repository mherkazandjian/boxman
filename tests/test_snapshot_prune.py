"""
``boxman snapshot prune`` (#294, finding 9): which files snapshots left
behind that nothing records or uses, and why each other one is kept.

- :func:`find_candidates` and :func:`decide` on real files under
  ``tmp_path``, with the scans given (N1, N2, N3, G1-G6);
- the session's scans -- what every snapshot names (G2), what every domain
  holds (G1), one header level (G3), a memory image's save header, the
  pools of earlier ``disk_snapshot_dir``\\ s -- against :class:`FakeHost`;
- the leftovers of real snapshot sequences, driven through
  ``SnapshotManager`` on :class:`_RevertingLibvirt` (sources 1, 2, 3a, 3b),
  removed by :func:`prune`, after which every snapshot left validates and
  restores;
- :func:`prune` (G7): newest layer first, a file replaced since it was
  listed, and what a file that stays is built on;
- the removal a take runs of the overlays restores left beside an ISO, as
  prune drives it, and the warnings that point at prune.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import time
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest

from boxman.exceptions import SnapshotError
from boxman.providers.libvirt.disk_cleanup import FilesInUse
from boxman.providers.libvirt.session import LibVirtSession
from boxman.providers.libvirt.snapshot import SnapshotManager
from boxman.providers.libvirt.snapshot_prune import (
    KEPT,
    MEMORY_IMAGE,
    PRESERVE_BACKUP,
    PRESERVE_RESERVATION,
    PRESERVE_SCRATCH,
    REMOVABLE,
    RESERVATION_MARKER,
    REVERT_OVERLAY,
    SKIPPED,
    TAKE_OVERLAY,
    UNKNOWN,
    Entry,
    Held,
    References,
    Scans,
    ScopeDir,
    SnapshotRecord,
    VmFacts,
    decide,
    find_candidates,
    format_duration,
    keep_unnamed,
    owner_of,
    parse_duration,
    parse_kinds,
    prune,
    removal_order,
    save_image_owner,
    save_image_xml,
)
from conftest import domain_listing, domain_uuid
from fake_libvirt_host import FakeHost, Result
from test_libvirt_snapshot import (
    _files_in_use,
    _restore,
    _restoring,
    _result,
    _RevertingLibvirt,
    _waiting,
)

pytestmark = pytest.mark.unit

VM = "bprj__demo__bprj_c1_vm01"
UUID = domain_uuid(VM)
OTHER = "bprj__demo__bprj_c1_vm02"

unless_root = pytest.mark.skipif(os.geteuid() == 0,
                                 reason="root writes any directory")


def _qcow2(backing):
    return {"format": "qcow2", "backing": str(backing)}


class _Box:
    """One project's VMs in a workdir under ``tmp_path``, the files there,
    and the scans :func:`decide` is given."""

    def __init__(self, tmp_path: Path):
        self.wd = tmp_path / "ws" / "c1"
        self.wd.mkdir(parents=True)
        self.base = self._make(f"{VM}.qcow2")
        self.vms = {VM: VmFacts(
            name=VM, cluster="c1", uuid=UUID,
            disks=(str(self.wd / f"{VM}.qcow2"),
                   str(self.wd / f"{VM}_data.qcow2")),
            disk_names=("data",))}
        self.known = {VM}
        self.dirs = [ScopeDir(str(self.wd), "the workdir of cluster c1")]
        self.held = Held()
        self.held[self.base] = VM
        self.references = References()
        self.headers: dict[str, dict | None] = {}
        self.owners: dict[str, tuple[str, str] | None] = {}
        self.domains = [(VM, UUID)]
        self.locked: set[str] = set()
        self.mounts: set[str] | None = set()
        self.failures: list[str] = []
        self.pending = FilesInUse()

    def _make(self, name: str, data: bytes = b"data") -> str:
        path = self.wd / name
        path.write_bytes(data)
        return str(path)

    def overlay(self, name: str, backing: str | None = None) -> str:
        """A qcow2 overlay *name* on *backing* (the boot disk by default)."""
        path = self._make(name, b"QFI\xfboverlay")
        self.headers[path] = _qcow2(backing or self.base)
        return path

    def memory(self, name: str, owner: tuple[str, str] | None = None) -> str:
        path = self._make(name, b"LibvirtQemudSave")
        self.owners[path] = owner if owner is not None else (VM, UUID)
        return path

    def decide(self, min_age: float = 3600, later: float = 7200,
               kinds=None) -> dict:
        """``{file name: entry}`` of :func:`decide`, *later* seconds from
        now."""
        listing = find_candidates(self.dirs, self.vms, self.known)
        headers = {path: self.headers.get(
            path, {"format": "not qcow2", "backing": None})
            for path in listing.files}
        headers.update({path: header for path, header in self.headers.items()
                        if path in headers})
        scans = Scans(held=self.held, references=self.references,
                      headers=headers, owners=self.owners,
                      locked=self.locked, domains=self.domains,
                      mounts=self.mounts, failures=list(self.failures),
                      pending=self.pending)
        entries = decide(listing, self.vms, self.known, scans,
                         time.time() + later, min_age, kinds)
        self.listing, self.scans = listing, scans
        return {os.path.basename(entry.path): entry for entry in entries}


@pytest.fixture
def box(tmp_path) -> _Box:
    return _Box(tmp_path)


# --------------------------------------------------------------------------
# N1, N2: what is a candidate, by name
# --------------------------------------------------------------------------

class TestWhatIsListed:

    def test_an_overlay_named_for_the_vm_is_a_take_overlay(self, box):
        box.overlay(f"{VM}.s1")
        entry = box.decide()[f"{VM}.s1"]
        assert (entry.kind, entry.named, entry.status) == (
            TAKE_OVERLAY, "s1", REMOVABLE)
        assert entry.reason == ("no snapshot 's1' is left; nothing records "
                                "or uses it")

    def test_an_all_digit_suffix_is_a_revert_overlay_made_at_that_epoch(
            self, box):
        box.overlay(f"{VM}.1793626528")
        entry = box.decide()[f"{VM}.1793626528"]
        assert entry.kind == REVERT_OVERLAY
        made = time.strftime("%Y-%m-%d %H:%M", time.localtime(1793626528))
        assert entry.reason == (f"made by a revert on {made}; nothing "
                                f"records or uses it")

    def test_a_data_disks_overlay_is_named_by_the_disk(self, box):
        data = box._make(f"{VM}_data.qcow2")
        box.overlay(f"{VM}_data.s1", backing=data)
        assert box.decide()[f"{VM}_data.s1"].kind == TAKE_OVERLAY

    def test_memory_images_raw_and_zst_are_memory_images(self, box):
        box.memory(f"{VM}_snapshot_old.raw")
        box.memory(f"{VM}_snapshot_old.raw.zst")
        found = box.decide()
        assert {found[n].kind for n in (f"{VM}_snapshot_old.raw",
                                        f"{VM}_snapshot_old.raw.zst")} == {
            MEMORY_IMAGE}
        assert found[f"{VM}_snapshot_old.raw"].reason == (
            "no snapshot 'old' is left; nothing records it")

    def test_the_declared_disks_are_never_candidates(self, box):
        data = box._make(f"{VM}_data.qcow2")
        box.headers[data] = _qcow2(box.base)       # a linked clone, say
        assert box.decide() == {}

    def test_a_save_file_and_a_standalone_image_are_never_candidates(
            self, box):
        box._make(f"{VM}.save")
        box._make(f"{VM}.save.restoring-1793626528")
        standalone = box._make(f"{VM}.mine", b"QFI\xfb")
        box.headers[standalone] = {"format": "qcow2", "backing": None}
        assert box.decide() == {}

    @pytest.mark.parametrize("name, kind", [
        (".preserve", PRESERVE_BACKUP),
        (".preserve.copy", PRESERVE_SCRATCH),
        (".preserve.4242.0123456789ab", PRESERVE_RESERVATION),
        (".preserve.copy.4242.0123456789ab", PRESERVE_RESERVATION),
    ])
    def test_a_restores_leftovers_beside_an_overlay(self, box, name, kind):
        box.overlay(f"{VM}.s1")
        box._make(f"{VM}.s1{name}", b"QFI\xfba copy")
        found = box.decide()
        assert found[f"{VM}.s1{name}"].kind == kind
        assert found[f"{VM}.s1{name}"].named == f"{VM}.s1"

    def test_a_preserve_of_a_declared_disk_is_not_listed(self, box):
        box._make(f"{VM}.qcow2.preserve")
        assert box.decide() == {}

    def test_a_longer_domain_name_takes_the_file(self, box):
        """``v.x.s1`` is domain ``v.x``'s overlay of ``s1`` before it is VM
        ``v``'s of ``x.s1``: it is listed for neither."""
        box.known.add(f"{VM}.x")
        box.overlay(f"{VM}.x.s1")
        box.memory(f"{VM}_x_snapshot_s1.raw")
        assert box.decide() == {}

    def test_another_projects_files_are_not_listed(self, box):
        box.overlay("bprj__other__bprj_c1_vm01.s1")
        box.memory("bprj__other__bprj_c1_vm01_snapshot_s1.raw")
        box.memory("someone_snapshot_x.raw")
        assert box.decide() == {}

    def test_the_longest_vm_of_the_project_wins(self, box):
        longer = f"{VM}_data"
        box.vms[longer] = VmFacts(name=longer, cluster="c1",
                                  uuid=domain_uuid(longer))
        box.known.add(longer)
        box.overlay(f"{longer}.s1")
        assert box.decide()[f"{longer}.s1"].vm == longer

    def test_owner_of_needs_a_separator_after_the_name(self, box):
        assert owner_of(f"{VM}0.s1", box.vms, box.known) is None
        assert owner_of(f"{VM}.s1", box.vms, box.known)[0].name == VM

    def test_a_directory_that_is_not_there_holds_nothing(self, box, tmp_path):
        box.dirs.append(ScopeDir(str(tmp_path / "gone"), "moved"))
        assert box.decide() == {}
        assert box.listing.unreadable == {}

    def test_a_stranded_removal_directory_is_reported_and_never_listed(
            self, box):
        (box.wd / ".boxman-removing-abc123").mkdir()
        assert box.decide() == {}
        assert box.listing.stranded == [str(box.wd / ".boxman-removing-abc123")]


# --------------------------------------------------------------------------
# G1-G6 and N3: why one is kept
# --------------------------------------------------------------------------

class TestWhyAFileIsKept:

    def test_a_file_a_domain_uses_is_kept_and_listed_only_with_all(
            self, box):
        overlay = box.overlay(f"{VM}.1793626528")
        box.held[overlay] = VM
        entry = box.decide()[f"{VM}.1793626528"]
        assert (entry.status, entry.reason, entry.recorded) == (
            KEPT, f"domain {VM} uses it", True)

    def test_a_file_used_under_another_name_is_kept_by_its_identity(
            self, box):
        overlay = box.overlay(f"{VM}.s1")
        st = os.stat(overlay)
        box.held.identities[(st.st_dev, st.st_ino)] = "other01"
        assert box.decide()[f"{VM}.s1"].reason == "domain other01 uses it"

    def test_a_media_source_of_a_domain_is_kept(self, box):
        overlay = box.overlay(f"{VM}.s1")
        box.held.media[overlay] = "other01"
        assert box.decide()[f"{VM}.s1"].reason == (
            "it is a media source of domain other01")

    def test_a_file_another_domains_snapshot_names_is_kept(self, box):
        """Wherever in its XML (:class:`TestWhatEverySnapshotNames`)."""
        overlay = box.overlay(f"{VM}.s1")
        box.references[overlay] = "snapshot 'x1' of other01"
        entry = box.decide()[f"{VM}.s1"]
        assert (entry.status, entry.reason, entry.recorded) == (
            KEPT, "snapshot 'x1' of other01 names it", True)

    def test_a_file_named_through_a_hard_link_is_kept(self, box):
        overlay = box.overlay(f"{VM}.s1")
        link = box.wd / "recorded-elsewhere"
        os.link(overlay, link)
        st = os.stat(overlay)
        box.references.identities[(st.st_dev, st.st_ino)] = (
            "snapshot 'x1' of other01")
        assert box.decide()[f"{VM}.s1"].reason == (
            "snapshot 'x1' of other01 names it")

    def test_a_kept_file_keeps_what_it_is_built_on_all_the_way_down(
            self, box):
        """G3, asked until nothing changes: the user's layer is no
        candidate, so it keeps s2, which keeps s1 under it."""
        s1 = box.overlay(f"{VM}.s1")
        s2 = box.overlay(f"{VM}.s2", backing=s1)
        user = box._make("userlayer.qcow2", b"QFI\xfb")
        box.headers[user] = _qcow2(s2)
        found = box.decide()
        assert found[f"{VM}.s2"].reason == f"{user} is built on it"
        assert found[f"{VM}.s1"].reason == f"{s2} (kept) is built on it"

    def test_a_too_recent_file_keeps_what_it_is_built_on(self, box):
        s1 = box.overlay(f"{VM}.s1")
        box.overlay(f"{VM}.userlayer", backing=s1)
        found = box.decide(later=0)
        assert found[f"{VM}.userlayer"].reason.startswith("too recent")
        assert found[f"{VM}.s1"].reason.startswith("too recent")
        found = box.decide(min_age=0, later=0)
        assert {e.status for e in found.values()} == {REMOVABLE}

    def test_a_removable_file_keeps_nothing(self, box):
        s1 = box.overlay(f"{VM}.s1")
        box.overlay(f"{VM}.s2", backing=s1)
        assert {e.status for e in box.decide().values()} == {REMOVABLE}

    def test_a_symlink_is_kept(self, box, tmp_path):
        target = tmp_path / "elsewhere.qcow2"
        target.write_bytes(b"QFI\xfb")
        (box.wd / f"{VM}.s1").symlink_to(target)
        box.headers[str(box.wd / f"{VM}.s1")] = _qcow2(box.base)
        assert box.decide()[f"{VM}.s1"].reason == (
            "it is a symlink or not a regular file")

    def test_a_file_with_two_names_is_kept(self, box):
        overlay = box.overlay(f"{VM}.s1")
        os.link(overlay, box.wd / "another-name")
        assert box.decide()[f"{VM}.s1"].reason == (
            "it has 2 names (hard links): removing one frees nothing")

    def test_a_mount_point_is_kept(self, box):
        overlay = box.overlay(f"{VM}.s1")
        box.mounts = {overlay}
        assert box.decide()[f"{VM}.s1"].reason == "it is a mount point"

    @unless_root
    def test_one_in_a_directory_the_user_may_not_write_is_kept(self, box):
        box.overlay(f"{VM}.s1")
        box.wd.chmod(0o555)
        try:
            assert box.decide()[f"{VM}.s1"].reason == (
                "its directory is not writable by you")
        finally:
            box.wd.chmod(0o755)

    def test_a_file_changed_within_min_age_is_too_recent(self, box):
        box.overlay(f"{VM}.s1")
        entry = box.decide(min_age=3600, later=720)[f"{VM}.s1"]
        assert (entry.status, entry.reason) == (
            KEPT, "too recent (changed 12m ago; --min-age 1h)")

    def test_a_min_age_of_0_lists_a_fresh_file(self, box):
        box.overlay(f"{VM}.s1")
        assert box.decide(min_age=0, later=0)[f"{VM}.s1"].status == REMOVABLE

    def test_a_file_whose_image_lock_a_process_holds_is_kept(self, box):
        overlay = box.overlay(f"{VM}.s1")
        box.locked = {overlay}
        assert box.decide()[f"{VM}.s1"].reason == (
            "a process holds its image lock")

    def test_a_vm_whose_domain_is_gone_has_only_unknown_files(self, box):
        box.overlay(f"{VM}.s1")
        box.vms[VM].uuid = None
        entry = box.decide()[f"{VM}.s1"]
        assert entry.status == UNKNOWN
        assert "has no domain" in entry.reason


class TestProvenance:
    """N3: what a file holds has to be its VM's."""

    def test_an_overlay_built_on_another_vms_disk_is_kept(self, box):
        other = box._make(f"{OTHER}.qcow2")
        box.overlay(f"{VM}.s1", backing=other)
        assert box.decide()[f"{VM}.s1"].reason == (
            f"it is built on {other}, which is not named for {VM}")

    def test_an_overlay_built_on_an_image_that_is_gone_counts_by_name(
            self, box):
        """#289: libvirt's delete of a branch unlinks what it was taken
        on."""
        box.overlay(f"{VM}.t2", backing=str(box.wd / f"{VM}.t1"))
        assert box.decide()[f"{VM}.t2"].status == REMOVABLE

    def test_an_overlay_built_on_a_media_source_is_kept(self, box, tmp_path):
        iso = tmp_path / "nixos.iso"
        iso.write_bytes(b"iso")
        box.held.media[str(iso)] = "other01"
        box.overlay(f"{VM}.1793626528", backing=str(iso))
        assert box.decide()[f"{VM}.1793626528"].reason == (
            f"it is built on {iso}, a media source of domain other01")

    def test_an_overlay_built_on_a_layer_of_its_vm_named_otherwise_counts(
            self, box, tmp_path):
        """Its disk under another name: a symlinked directory."""
        link = tmp_path / "link"
        link.symlink_to(box.wd)
        box.overlay(f"{VM}.s1", backing=str(link / "renamed.qcow2"))
        (box.wd / "renamed.qcow2").symlink_to(box.base)
        assert box.decide()[f"{VM}.s1"].status == REMOVABLE

    def test_a_memory_image_another_domain_wrote_is_kept(self, box):
        box.memory(f"{VM}_snapshot_x.raw", owner=("other01", "u"))
        assert box.decide()[f"{VM}_snapshot_x.raw"].reason == (
            "its save header names domain other01: written by that domain")

    def test_a_memory_image_naming_the_uuid_of_another_domain_is_unknown(
            self, box):
        box.domains.append((OTHER, domain_uuid(OTHER)))
        box.memory(f"{VM}_snapshot_x.raw", owner=(VM, domain_uuid(OTHER)))
        entry = box.decide()[f"{VM}_snapshot_x.raw"]
        assert entry.status == UNKNOWN
        assert entry.reason == (f"its save header names {VM} with the uuid "
                                f"of domain {OTHER}")

    def test_a_memory_image_of_an_earlier_domain_of_that_name_is_removable(
            self, box):
        box.memory(f"{VM}_snapshot_x.raw", owner=(VM, domain_uuid("gone")))
        assert box.decide()[f"{VM}_snapshot_x.raw"].status == REMOVABLE

    def test_a_memory_image_whose_header_cannot_be_read_is_unknown(
            self, box):
        path = box.memory(f"{VM}_snapshot_fake.raw")
        box.owners[path] = None
        entry = box.decide()[f"{VM}_snapshot_fake.raw"]
        assert (entry.status, entry.reason) == (
            UNKNOWN, "its save header could not be read")

    def test_one_a_snapshot_of_that_name_records_otherwise_says_so(self, box):
        box.references.snapshots[VM] = {"s1": SnapshotRecord(
            memory=str(box.wd / f"{VM}_snapshot_s1.raw.zst.old"))}
        box.overlay(f"{VM}.s1")
        box.memory(f"{VM}_snapshot_s1.raw")
        found = box.decide()
        assert found[f"{VM}.s1"].reason == ("snapshot 's1' no longer "
                                           "records it")
        assert found[f"{VM}_snapshot_s1.raw"].reason == (
            "snapshot 's1' records another memory image")


class TestPreserveLeftovers:
    """G6: an interrupted restore's backup is removable only when it is
    provably not the only copy."""

    def test_an_unfilled_reservation_is_removable(self, box):
        box.overlay(f"{VM}.s1")
        box._make(f"{VM}.s1.preserve", RESERVATION_MARKER)
        entry = box.decide()[f"{VM}.s1.preserve"]
        assert (entry.status, entry.kind, entry.reason) == (
            REMOVABLE, PRESERVE_RESERVATION, "an unfilled backup reservation")

    def test_it_reads_the_same_on_a_second_pass(self, box):
        box.overlay(f"{VM}.s1")
        box._make(f"{VM}.s1.preserve", RESERVATION_MARKER)
        box.decide()
        entry = box.decide()[f"{VM}.s1.preserve"]
        assert entry.reason == "an unfilled backup reservation"

    def test_a_copy_of_an_overlay_in_place_is_removable(self, box):
        overlay = box.overlay(f"{VM}.s1")
        copy = box._make(f"{VM}.s1.preserve", b"QFI\xfboverlay")
        st = os.stat(overlay)
        os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns))
        box.headers[copy] = _qcow2(box.base)
        entry = box.decide()[f"{VM}.s1.preserve"]
        assert (entry.status, entry.reason) == (
            REMOVABLE, f"a spare copy of {VM}.s1, which is in place")

    def test_the_only_copy_is_kept_with_the_move_that_puts_it_back(self, box):
        box._make(f"{VM}.s1.preserve", b"QFI\xfboverlay")
        overlay = box.wd / f"{VM}.s1"
        entry = box.decide()[f"{VM}.s1.preserve"]
        assert entry.status == KEPT
        assert entry.reason == (f"the only copy of {overlay}, which is "
                                f"missing: mv -fT {overlay}.preserve "
                                f"{overlay}")

    def test_a_copy_that_differs_from_its_overlay_is_unknown(self, box):
        box.overlay(f"{VM}.s1")
        box._make(f"{VM}.s1.preserve", b"QFI\xfbsomething else")
        entry = box.decide()[f"{VM}.s1.preserve"]
        assert entry.status == UNKNOWN
        assert entry.reason.startswith(f"it differs from {VM}.s1")

    def test_a_scratch_copy_goes_while_its_overlay_is_in_place(self, box):
        box.overlay(f"{VM}.s1")
        box._make(f"{VM}.s1.preserve.copy", b"partial")
        assert box.decide()[f"{VM}.s1.preserve.copy"].status == REMOVABLE

    def test_a_scratch_copy_stays_while_its_overlay_is_missing(self, box):
        box._make(f"{VM}.s1.preserve.copy", b"partial")
        assert box.decide()[f"{VM}.s1.preserve.copy"].status == KEPT

    def test_a_scaffold_is_removable_only_as_the_marker(self, box):
        box.overlay(f"{VM}.s1")
        box._make(f"{VM}.s1.preserve.4242.0123456789ab", RESERVATION_MARKER)
        box._make(f"{VM}.s1.preserve.4243.0123456789ab", b"else")
        found = box.decide()
        assert found[f"{VM}.s1.preserve.4242.0123456789ab"].status == (
            REMOVABLE)
        assert found[f"{VM}.s1.preserve.4243.0123456789ab"].status == UNKNOWN

    def test_a_hard_link_an_interrupted_restore_left_is_kept_with_its_overlay(
            self, box):
        """A restore backs an overlay up as a hard link of it: until the
        put-back renames it over the original's name, or drops it, the two
        names are one file. G4 keeps both -- removing either frees nothing
        -- and a restore that ends drops the second name either way."""
        overlay = box.overlay(f"{VM}.s1")
        os.link(overlay, box.wd / f"{VM}.s1.preserve")
        box.headers[str(box.wd / f"{VM}.s1.preserve")] = _qcow2(box.base)
        found = box.decide()
        for name in (f"{VM}.s1", f"{VM}.s1.preserve"):
            assert (found[name].status, found[name].reason) == (
                KEPT, "it has 2 names (hard links): removing one frees "
                      "nothing")

    def test_a_restore_in_progress_is_left_alone(self, box):
        box.overlay(f"{VM}.s1")
        box._make(f"{VM}.s1.preserve", b"QFI\xfbbeing copied")
        assert box.decide(later=5)[f"{VM}.s1.preserve"].reason.startswith(
            "too recent")


class TestFailsClosed:
    """A scan that fails makes every file unknown: nothing can be
    removed."""

    @pytest.mark.parametrize("broken, why", [
        ("held", "what the domains use could not be told"),
        ("references", "what the snapshots of every domain name could not"),
        ("mounts", "the mount table could not be read"),
    ])
    def test_a_scan_that_fails_makes_everything_unknown(self, box, broken,
                                                        why):
        box.overlay(f"{VM}.s1")
        setattr(box, broken, None)
        entry = box.decide()[f"{VM}.s1"]
        assert entry.status == UNKNOWN
        assert why in entry.reason

    def test_a_header_that_cannot_be_read_makes_everything_unknown(
            self, box):
        box.overlay(f"{VM}.s1")
        unread = box._make("whatever.qcow2")
        box.headers[unread] = None
        entry = box.decide()[f"{VM}.s1"]
        assert entry.status == UNKNOWN
        assert f"the header of {unread} could not be read" in entry.reason

    def test_a_memory_image_read_as_a_save_image_needs_no_header(self, box):
        box.overlay(f"{VM}.s1")
        path = box.memory(f"{VM}_snapshot_s0.raw")
        box.headers[path] = None
        assert box.decide()[f"{VM}.s1"].status == REMOVABLE

    @unless_root
    def test_a_directory_that_cannot_be_listed_makes_everything_unknown(
            self, box, tmp_path):
        box.overlay(f"{VM}.s1")
        locked = tmp_path / "locked"
        locked.mkdir()
        box.dirs.append(ScopeDir(str(locked), "a memory_snapshot_dir"))
        locked.chmod(0)
        try:
            entry = box.decide()[f"{VM}.s1"]
        finally:
            locked.chmod(0o755)
        assert entry.status == UNKNOWN
        assert f"{locked} could not be listed" in entry.reason

    def test_a_failure_the_manager_found_makes_everything_unknown(self, box):
        box.overlay(f"{VM}.s1")
        box.failures = ["the storage pools could not be read"]
        assert box.decide()[f"{VM}.s1"].reason == (
            "nothing can be removed: the storage pools could not be read")


class TestKinds:

    def test_a_kind_left_out_is_skipped_and_keeps_what_is_under_it(
            self, box):
        s1 = box.overlay(f"{VM}.s1")
        copy = box._make(f"{VM}.s9.preserve", b"QFI\xfbcopy")
        box.headers[copy] = _qcow2(s1)
        found = box.decide(kinds=parse_kinds("overlay"))
        assert found[f"{VM}.s9.preserve"].status == SKIPPED
        assert found[f"{VM}.s1"].reason == (
            f"{copy} (skipped) is built on it")

    def test_kinds_parse(self):
        assert parse_kinds("memory,preserve") == (
            MEMORY_IMAGE, PRESERVE_BACKUP, PRESERVE_SCRATCH,
            PRESERVE_RESERVATION)
        for bad in ("", "disk", "overlay,,bogus"):
            with pytest.raises(ValueError):
                parse_kinds(bad)


class TestDurations:

    @pytest.mark.parametrize("text, seconds", [
        ("0", 0), ("3600", 3600), ("90s", 90), ("30m", 1800), ("1h", 3600),
        ("2d", 172800), (" 5m ", 300)])
    def test_min_age_takes_seconds_or_a_unit(self, text, seconds):
        assert parse_duration(text) == seconds

    @pytest.mark.parametrize("text", ["", "-1", "1.5h", "1w", "h"])
    def test_anything_else_is_refused(self, text):
        with pytest.raises(ValueError):
            parse_duration(text)

    @pytest.mark.parametrize("seconds, text", [
        (0, "0s"), (59, "59s"), (720, "12m"), (3600, "1h"), (5400, "1h30m"),
        (86400, "1d")])
    def test_durations_read_as_the_flag_takes_them(self, seconds, text):
        assert format_duration(seconds) == text


# --------------------------------------------------------------------------
# a libvirt save image's header
# --------------------------------------------------------------------------

def _save_image(xml: str, order: str = "<", magic=b"LibvirtQemudSave",
                version: int = 2) -> bytes:
    data = xml.encode() + b"\0" * 7
    header = magic + struct.pack(f"{order}5I", version, len(data), 1, 0, 0)
    return header + b"\0" * 56 + data + b"memory pages follow"


DOMAIN_XML = f"<domain type='kvm'><name>{VM}</name><uuid>{UUID.upper()}</uuid></domain>"


class TestSaveImageHeader:

    @pytest.mark.parametrize("order", ["<", ">"])
    def test_its_domain_is_read_in_either_byte_order(self, order):
        xml = save_image_xml(_save_image(DOMAIN_XML, order))
        assert save_image_owner(xml) == (VM, UUID)

    def test_a_partial_save_image_is_read_too(self):
        assert save_image_owner(save_image_xml(
            _save_image(DOMAIN_XML, magic=b"LibvirtQemudPart"))) == (VM, UUID)

    @pytest.mark.parametrize("data", [
        b"random bytes" * 20,
        _save_image(DOMAIN_XML)[:100],
        _save_image(DOMAIN_XML, version=7),
    ])
    def test_anything_else_names_no_domain(self, data):
        assert save_image_owner(save_image_xml(data)) is None


# --------------------------------------------------------------------------
# the session's scans, against a fake host
# --------------------------------------------------------------------------

class _Host(FakeHost):
    """:class:`FakeHost` with what ``snapshot prune``'s scans ask: each
    domain's snapshots (by UUID), its managed save and backup job, one
    header level and a lock probe from ``qemu-img``, a save image's header
    from ``virsh`` and through ``zstd``."""

    def __init__(self) -> None:
        super().__init__()
        #: domain -> {snapshot: its XML}
        self.snapshots: dict[str, dict[str, str]] = {}
        #: domain -> the definition its managed save holds
        self.managed: dict[str, str] = {}
        #: domain -> what ``domjobinfo --rawstats`` prints
        self.jobs: dict[str, str] = {}
        #: domain -> what ``backup-dumpxml`` prints
        self.backups: dict[str, str] = {}
        #: memory image -> the domain XML its save header holds
        self.saves: dict[str, str] = {}
        #: (pool, volume) -> its path
        self.volumes: dict[tuple[str, str], str] = {}
        #: images whose lock a process holds
        self.locked: set[str] = set()
        #: domains whose snapshots cannot be read
        self.broken: set[str] = set()
        #: domain -> how many more ``domjobinfo`` calls fail, as one busy
        #: with another job can (libvirt 10.10)
        self.busy: dict[str, int] = {}

    def _named(self, ident: str) -> str | None:
        return next((name for name in self.domains
                     if ident in (name, domain_uuid(name))), None)

    def run(self, command: str, **kwargs) -> Result:
        bare = command[len("sudo "):] if command.startswith("sudo ") else command
        if bare.startswith(("zstd ", "qemu-img info")):
            self.commands.append(command)
        if bare.startswith("zstd "):
            path = bare.split(" -- ", 1)[1].split(" | ", 1)[0].strip("'")
            self.log.append(("user", "zstd", (path,)))
            if path not in self.saves:
                return Result(stderr="zstd: not a zstd frame\n", code=1)
            data = _save_image(self.saves[path])
            return Result(stdout=base64.b64encode(data).decode())
        if bare.startswith("qemu-img info") and "--backing-chain" not in bare:
            path = bare.split()[-1].strip("'")
            self.log.append(("user", "qemu-img-header", (path,)))
            image = self.files.get(path)
            if "-U" not in bare.split() and path in self.locked:
                return Result(stderr=f"qemu-img: Could not open '{path}': "
                                     f"Failed to get shared \"write\" lock\n",
                              code=1)
            if image is None or not (image.readable or command.startswith(
                    "sudo ")):
                return Result(stderr=f"qemu-img: Could not open '{path}'\n",
                              code=1)
            info = {"filename": path, "format": image.format}
            if image.backing:
                info["full-backing-filename"] = image.backing
            return Result(stdout=json.dumps(info))
        return super().run(command, **kwargs)

    def _list(self, args):
        if "--with-managed-save" in args:
            names = sorted(self.managed)
            return Result(stdout=domain_listing(args, *names) + "\n")
        return super()._list(args)

    def _snapshot_list(self, args):
        domain = self._named(args[0])
        if domain is None or domain in self.broken:
            return Result(stderr="error: failed to get domain\n", code=1)
        names = "".join(f"{n}\n" for n in self.snapshots.get(domain, {}))
        return Result(stdout=names + "\n")

    def _snapshot_dumpxml(self, args):
        domain = self._named(args[0])
        xml = self.snapshots.get(domain, {}).get(args[1])
        if xml is None:
            return Result(stderr="error: snapshot not found\n", code=1)
        return Result(stdout=xml)

    def _managedsave_dumpxml(self, args):
        domain = self._named(args[0])
        if domain not in self.managed:
            return Result(stderr="error: no managed save\n", code=1)
        return Result(stdout=self.managed[domain])

    def _domjobinfo(self, args):
        domain = self._named(args[0])
        if domain not in self.running:
            return Result(stderr="error: domain is not running\n", code=1)
        if self.busy.get(domain):
            self.busy[domain] -= 1
            return Result(stderr="error: internal error: invalid job "
                                 "statistics type\n", code=1)
        return Result(stdout=self.jobs.get(domain, "Job type: 0\n\n"))

    def _backup_dumpxml(self, args):
        domain = self._named(args[0])
        if domain not in self.backups:
            return Result(stderr="error: no domain backup job present\n",
                          code=1)
        return Result(stdout=self.backups[domain])

    def _save_image_dumpxml(self, args):
        if args[0] not in self.saves:
            return Result(stderr="error: failed to read save image\n", code=1)
        return Result(stdout=self.saves[args[0]])

    def _vol_path(self, args):
        pool = next(a.split("=", 1)[1] for a in args
                    if a.startswith("--pool="))
        path = self.volumes.get((pool, args[0]))
        if path is None:
            return Result(stderr="error: no storage vol\n", code=1)
        return Result(stdout=f"{path}\n\n")


WS = "/ws/c1"
OV = "/fast/ov"


def _snapshot(name: str, *, disks=(), revert=(), domain=(), inactive=(),
              memory: str | None = None, state: str = "running") -> str:
    """A snapshot's XML: ``(target, source)`` pairs per section."""
    def recorded(pairs):
        return "".join(
            f"<disk name='{t}' snapshot='external' type='file'>"
            f"<driver type='qcow2'/><source file='{p}'/></disk>"
            for t, p in pairs)

    def definition(tag, pairs):
        return (f"<{tag} type='kvm'><devices>" + "".join(
            f"<disk type='file' device='disk'><source file='{p}'/>"
            f"<backingStore type='file'><source file='{p}.below'/>"
            f"<backingStore/></backingStore><target dev='{t}'/></disk>"
            for t, p in pairs) + f"</devices></{tag}>")

    return (f"<domainsnapshot><name>{name}</name><state>{state}</state>"
            + (f"<memory snapshot='external' file='{memory}'/>" if memory
               else "<memory snapshot='no'/>")
            + f"<disks>{recorded(disks)}</disks>"
            + (f"<revertDisks>{recorded(revert)}</revertDisks>"
               if revert else "")
            + definition("domain", domain)
            + (definition("inactiveDomain", inactive) if inactive else "")
            + "</domainsnapshot>")


def _scan(host: _Host, method: str, *args, provider=None):
    with patch("boxman.providers.libvirt.commands._shell_run",
               side_effect=host.run):
        session = LibVirtSession(config={"provider": {
            "libvirt": provider or {}}})
        return getattr(session, method)(*args)


class TestWhatEverySnapshotNames:
    """G2: wider than ``recorded_files``, and read by UUID."""

    @pytest.fixture
    def host(self) -> _Host:
        host = _Host()
        host.define_domain(VM, ("file", "disk", "vda", f"{OV}/{VM}.s2"))
        host.define_domain("other01", ("file", "disk", "vda", "/o/o.qcow2"))
        host.snapshots[VM] = {
            "s1": _snapshot("s1", disks=[("vda", f"{OV}/{VM}.s1")],
                            revert=[("vda", f"{WS}/{VM}.1793626528")],
                            domain=[("vda", f"{WS}/{VM}.qcow2")],
                            memory=f"/mem/{VM}_snapshot_s1.raw")}
        host.snapshots["other01"] = {
            "x1": _snapshot("x1", disks=[("vda", "/o/o.x1")],
                            domain=[("vda", "/o/o.qcow2")],
                            inactive=[("vda", f"{WS}/{VM}.inactive")],
                            state="shutoff")}
        return host

    def test_every_section_and_the_memory_image_are_named(self, host):
        references = _scan(host, "snapshot_references")

        assert references[f"{OV}/{VM}.s1"] == f"snapshot 's1' of {VM}"
        for path in (f"{WS}/{VM}.1793626528", f"{WS}/{VM}.qcow2",
                     f"{WS}/{VM}.qcow2.below", f"/mem/{VM}_snapshot_s1.raw",
                     f"/mem/{VM}_snapshot_s1.raw.zst"):
            assert references[path] == f"snapshot 's1' of {VM}", path
        assert references[f"{WS}/{VM}.inactive"] == "snapshot 'x1' of other01"

    def test_what_each_snapshot_records_is_kept_for_the_directories(
            self, host):
        record = _scan(host, "snapshot_references").snapshots[VM]["s1"]
        assert record == SnapshotRecord(
            disks={"vda": f"{OV}/{VM}.s1"},
            revert_disks={"vda": f"{WS}/{VM}.1793626528"},
            taken_on={"vda": f"{WS}/{VM}.qcow2"},
            memory=f"/mem/{VM}_snapshot_s1.raw")

    def test_the_chain_of_every_file_named_is_named_too(self, host):
        host.add(f"{OV}/{VM}.s1", backing=f"{WS}/{VM}.qcow2", readable=True)
        host.add(f"{WS}/{VM}.qcow2", backing="/tpl/base.qcow2",
                 readable=True)
        host.add("/tpl/base.qcow2", readable=True)
        assert _scan(host, "snapshot_references")["/tpl/base.qcow2"] == (
            f"snapshot 's1' of {VM}")

    def test_each_domain_is_read_by_its_uuid(self, host):
        _scan(host, "snapshot_references")
        assert host.asked("snapshot-list") == [
            (domain_uuid(VM), "--name"), (domain_uuid("other01"), "--name")]

    def test_a_volume_is_named_by_its_path(self, host):
        host.volumes[("default", "x.qcow2")] = "/pool/x.qcow2"
        host.snapshots["other01"]["x2"] = (
            "<domainsnapshot><name>x2</name><disks><disk name='vda' "
            "snapshot='external' type='volume'><source pool='default' "
            "volume='x.qcow2'/></disk></disks></domainsnapshot>")
        references = _scan(host, "snapshot_references")
        assert references["/pool/x.qcow2"] == "snapshot 'x2' of other01"

    @pytest.mark.parametrize("breakage", [
        "the list", "a snapshot", "an external overlay naming nothing",
        "a volume", "a chain"])
    def test_what_cannot_be_read_fails_the_scan(self, host, breakage):
        if breakage == "the list":
            host.broken.add("other01")
        elif breakage == "a snapshot":
            host.snapshots["other01"]["x2"] = None
        elif breakage == "an external overlay naming nothing":
            host.snapshots["other01"]["x2"] = (
                "<domainsnapshot><name>x2</name><disks><disk name='vda' "
                "snapshot='external' type='network'><source protocol='rbd' "
                "name='pool/x'/></disk></disks></domainsnapshot>")
        elif breakage == "a volume":
            host.snapshots["other01"]["x2"] = (
                "<domainsnapshot><name>x2</name><disks><disk name='vda' "
                "snapshot='external' type='volume'><source pool='gone' "
                "volume='x'/></disk></disks></domainsnapshot>")
        else:
            host.add("/o/o.x1", corrupt=True)
        assert _scan(host, "snapshot_references") is None

    def test_a_domain_gone_meanwhile_names_nothing(self, host):
        host.broken.add("other01")
        host.before[("snapshot-list", domain_uuid("other01"))] = (
            lambda: host.domains.pop("other01"))
        references = _scan(host, "snapshot_references")
        assert references is not None
        assert "other01" not in references.snapshots


class TestWhatEveryDomainHolds:
    """G1: what the in-use scan sees, with each domain's media, the
    definition a managed save holds, and an active backup job."""

    @pytest.fixture
    def host(self) -> _Host:
        host = _Host()
        for path in ("/o/o.qcow2", "/iso/tools.iso"):
            host.add(path, format="raw" if path.endswith("iso") else "qcow2",
                     readable=True)
        host.define_domain("other01", ("file", "disk", "vda", "/o/o.qcow2"),
                           ("file", "cdrom", "sda", "/iso/tools.iso"),
                           running=True)
        return host

    def test_the_disks_and_the_media_of_every_domain(self, host):
        held = _scan(host, "files_held")
        assert held["/o/o.qcow2"] == "other01"
        assert held.media == {"/iso/tools.iso": "other01"}

    def test_a_managed_saves_disks_are_held_with_their_chains(self, host):
        host.add("/o/saved.qcow2", backing="/o/saved-base.qcow2",
                 readable=True)
        host.add("/o/saved-base.qcow2", readable=True)
        host.managed["other01"] = (
            "<domain><devices><disk type='file' device='disk'>"
            "<source file='/o/saved.qcow2'/><target dev='vda'/></disk>"
            "</devices></domain>")
        held = _scan(host, "files_held")
        assert held["/o/saved.qcow2"] == "the managed save of domain other01"
        assert held["/o/saved-base.qcow2"] == (
            "the managed save of domain other01")

    def test_a_managed_save_gone_meanwhile_holds_nothing(self, host):
        host.managed["other01"] = "not xml"
        host.before[("managedsave-dumpxml", domain_uuid("other01"))] = (
            lambda: host.managed.pop("other01"))
        assert _scan(host, "files_held") is not None

    def test_a_managed_save_that_cannot_be_read_fails_the_scan(self, host):
        host.managed["other01"] = "not xml"
        assert _scan(host, "files_held") is None

    def test_a_backup_jobs_scratch_file_is_held(self, host):
        host.add("/scratch/vda.qcow2", backing="/o/o.qcow2", readable=True)
        host.jobs["other01"] = ("Job type: 2\n\n  operation: 9\n"
                                "  time_elapsed: 1200\n")
        host.backups["other01"] = (
            "<domainbackup mode='pull'><disks><disk name='vda' backup='yes' "
            "type='file'><scratch file='/scratch/vda.qcow2'/></disk>"
            "</disks></domainbackup>")
        held = _scan(host, "files_held")
        assert held["/scratch/vda.qcow2"] == "a backup job of domain other01"

    def test_another_kind_of_job_is_no_backup(self, host):
        host.jobs["other01"] = "Job type: 2\n\n  operation: 5\n"
        assert _scan(host, "files_held") is not None
        assert host.asked("backup-dumpxml") == []

    def test_a_backup_whose_files_cannot_be_read_fails_the_scan(self, host):
        host.jobs["other01"] = "Job type: 2\n\n  operation: 9\n"
        assert _scan(host, "files_held") is None

    @pytest.fixture(autouse=True)
    def _no_wait(self, monkeypatch):
        monkeypatch.setattr(
            "boxman.providers.libvirt.session._JOB_RETRY_DELAY", 0)

    def test_a_domain_busy_with_another_job_is_asked_again(self, host):
        """Seen on a real host: a domain in the middle of a snapshot fails
        ``domjobinfo``. Its jobs exclude each other, so once that one ends
        it says what it runs."""
        host.busy["other01"] = 2
        assert _scan(host, "files_held") is not None
        assert len(host.asked("domjobinfo")) == 3

    def test_one_that_never_says_fails_the_scan(self, host):
        host.busy["other01"] = 3
        assert _scan(host, "files_held") is None

    def test_a_domain_that_stopped_meanwhile_runs_no_job(self, host):
        host.before[("domjobinfo", domain_uuid("other01"))] = (
            lambda: host.running.discard("other01"))
        assert _scan(host, "files_held") is not None

    def test_the_in_use_scan_failing_fails_it(self, host):
        host.fail.add("domblklist")
        assert _scan(host, "files_held") is None


class TestOneHeaderLevel:
    """G3's reads, and the advisory lock probe."""

    def test_an_overlay_is_read_one_level(self):
        host = _Host()
        host.add(f"{OV}/{VM}.s2", backing=f"{OV}/{VM}.s1", readable=True)
        assert _scan(host, "image_headers", [f"{OV}/{VM}.s2"]) == {
            f"{OV}/{VM}.s2": _qcow2(f"{OV}/{VM}.s1")}
        assert host.asked("qemu-img") == []      # no --backing-chain

    def test_a_file_this_process_reads_that_is_no_qcow2_is_not_handed_on(
            self, tmp_path):
        host = _Host()
        notes = tmp_path / "conf.rendered.yml"
        notes.write_text("project: demo\n")
        assert _scan(host, "image_headers", [str(notes)]) == {
            str(notes): {"format": "not qcow2", "backing": None}}
        assert host.log == []

    def test_one_the_user_may_not_read_is_read_through_its_pool(self):
        host = _Host()
        host.add(f"{WS}/{VM}.1793626528", backing=f"{WS}/{VM}.qcow2")
        host.define_pool("c1", WS)
        assert _scan(host, "image_headers", [f"{WS}/{VM}.1793626528"]) == {
            f"{WS}/{VM}.1793626528": _qcow2(f"{WS}/{VM}.qcow2")}
        assert host.asked("vol-dumpxml") == [(f"{WS}/{VM}.1793626528",)]

    def test_one_nobody_reads_has_no_header(self):
        host = _Host()
        host.add(f"{OV}/{VM}.s1", backing=f"{OV}/{VM}.qcow2")
        assert _scan(host, "image_headers", [f"{OV}/{VM}.s1"]) == {
            f"{OV}/{VM}.s1": None}

    def test_a_held_lock_is_reported_and_any_other_failure_is_not(self):
        host = _Host()
        host.add(f"{OV}/{VM}.s1", readable=True)
        host.add(f"{OV}/{VM}.s2", readable=True)
        host.locked.add(f"{OV}/{VM}.s1")
        assert _scan(host, "image_locks_held",
                     [f"{OV}/{VM}.s1", f"{OV}/{VM}.s2", "/missing"]) == {
            f"{OV}/{VM}.s1"}


class TestWhoWroteAMemoryImage:

    def test_a_raw_image_is_read_by_libvirt(self):
        host = _Host()
        host.saves["/mem/x.raw"] = DOMAIN_XML
        assert _scan(host, "memory_image_owner", "/mem/x.raw") == (VM, UUID)

    def test_a_compressed_one_is_read_through_zstd_as_far_as_its_header(
            self):
        host = _Host()
        host.saves["/mem/x.raw.zst"] = DOMAIN_XML
        assert _scan(host, "memory_image_owner", "/mem/x.raw.zst",
                     provider={"use_sudo": True}) == (VM, UUID)
        assert any(c.startswith("sudo zstd -dcq -- ") and "| head -c" in c
                   for c in host.commands)

    @pytest.mark.parametrize("path", ["/mem/x.raw", "/mem/x.raw.zst"])
    def test_one_that_is_no_save_image_names_nobody(self, path):
        assert _scan(_Host(), "memory_image_owner", path) is None


class TestEarlierDiskSnapshotDirs:
    """``ensure_pool_on`` leaves the pool on the old directory when a
    cluster's ``disk_snapshot_dir`` moves, and names the next one
    ``…_disk_snapshots_<n>``."""

    def test_its_pools_name_where_it_was(self):
        host = _Host()
        name = "bprj__demo__bprj_c1_disk_snapshots"
        host.define_pool(name, "/fast/old")
        host.define_pool(f"{name}_2", "/fast/new")
        host.define_pool("bprj__demo__bprj_c10_disk_snapshots", "/x")
        host.define_pool("bprj__demo__bprj_c1_workdir", WS)
        assert sorted(_scan(host, "disk_snapshot_pool_dirs", "demo", "c1")) == [
            (name, "/fast/old"), (f"{name}_2", "/fast/new")]

    def test_pools_it_cannot_read_are_not_known(self):
        host = _Host()
        host.fail.add("pool-list")
        assert _scan(host, "disk_snapshot_pool_dirs", "demo", "c1") is None

    def test_only_the_pools_of_those_names_are_described(self):
        host = _Host()
        host.define_pool("bprj__demo__bprj_c1_disk_snapshots", "/fast/old")
        host.define_pool("someone-elses", "/data")
        _scan(host, "disk_snapshot_pool_dirs", "demo", "c1")
        assert host.asked("pool-dumpxml") == [
            ("bprj__demo__bprj_c1_disk_snapshots",)]

    def test_one_undefined_meanwhile_is_passed_over(self):
        host = _Host()
        name = "bprj__demo__bprj_c1_disk_snapshots"
        host.define_pool(name, "/fast/old")
        host.before[("pool-dumpxml", name)] = lambda: host.pools.pop(name)
        assert _scan(host, "disk_snapshot_pool_dirs", "demo", "c1") == []


# --------------------------------------------------------------------------
# what snapshot sequences leave behind (design §1, sources 1, 2, 3a, 3b)
# --------------------------------------------------------------------------

def _unaccounted(vm: _RevertingLibvirt) -> list[str]:
    """The disk images of *vm* nothing accounts for: in no chain of a disk
    it runs on, recorded and taken on by no snapshot."""
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


def _entries(vm: _RevertingLibvirt) -> list:
    """The entries of a prune of *vm*'s directories, every scan read from
    *vm* as libvirt would answer it: what it uses, what its snapshots name
    (parsed as :meth:`LibVirtSession.snapshot_references` parses them), one
    header level of each file."""
    uuid = domain_uuid(vm.VM)
    facts = VmFacts(name=vm.VM, cluster="c", uuid=uuid,
                    disks=(str(vm.workdir / "vm01.qcow2"),
                           str(vm.workdir / "vm01_data.qcow2")),
                    disk_names=("data",))
    dirs = [ScopeDir(str(vm.workdir), "the workdir")]
    if vm.disk_dir is not None:
        dirs.append(ScopeDir(str(vm.disk_dir), "the disk_snapshot_dir"))
    held = Held()
    held.update(vm.in_use())
    held.media[vm.iso] = vm.VM
    references = References()
    session = LibVirtSession.__new__(LibVirtSession)
    for name in vm.snapshots:
        record, files = session._snapshot_files(
            None, ET.fromstring(vm.snapshot_xml(name)))
        references.snapshots.setdefault(vm.VM, {})[name] = record
        for path in files:
            for layer in vm._chain(path):
                references.setdefault(layer, f"snapshot '{name}' of vm01")
    listing = find_candidates(dirs, {vm.VM: facts}, {vm.VM})
    headers = {path: ({"format": vm.backing[path][0],
                       "backing": vm.backing[path][1]}
                      if path in vm.backing
                      else {"format": "not qcow2", "backing": None})
               for path in listing.files}
    owners = {entry.path: (vm.VM, uuid) for entry in listing.entries
              if entry.kind == MEMORY_IMAGE}
    scans = Scans(held=held, references=references, headers=headers,
                  owners=owners, domains=[(vm.VM, uuid)], mounts=set())
    return decide(listing, {vm.VM: facts}, {vm.VM}, scans,
                  time.time() + 7200, 3600)


def _prune(vm: _RevertingLibvirt) -> dict[str, list[str]]:
    """``{status: [path, ...]}`` of a prune of *vm*'s directories
    (:func:`_entries`)."""
    found: dict[str, list[str]] = {}
    for entry in _entries(vm):
        found.setdefault(entry.status, []).append(entry.path)
    return {status: sorted(paths) for status, paths in found.items()}


def _prune_and_restore_each(sm, vm: _RevertingLibvirt) -> list[str]:
    """Remove what a prune of *vm* lists as removable (:func:`prune`), then
    validate and restore every snapshot left, newest name first, the first
    again last; what was removed. Each must validate and restore."""
    removable = [e for e in _entries(vm) if e.status == REMOVABLE]
    result = prune(removable)
    assert (result.failed, result.kept, result.gone) == ([], [], [])
    removed = sorted(entry.path for entry in result.removed)
    assert REMOVABLE not in _prune(vm)      # a second prune finds nothing
    names = sorted(vm.snapshots, reverse=True)
    for name in [*names, names[0]]:
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            assert sm.validate_snapshot(vm.VM, name) == (True, []), name
        assert _restore(sm, vm, name) is True, name
    return removed


@pytest.fixture
def sm() -> SnapshotManager:
    return SnapshotManager(provider_config={"use_sudo": False,
                                            "uri": "qemu:///system"})


def _delete(sm, vm, name):
    execute, shell = _restoring(sm, vm)
    with execute, shell:
        return sm.delete_snapshot(vm.VM, name, recorded_in_use=vm.recorded)


def _collapse(sm, vm, target):
    running, vm.active = vm.active, False
    execute, shell = _restoring(sm, vm)
    with execute, shell:
        done = sm.collapse_to(vm.VM, target, recorded_in_use=vm.recorded)
    if running:
        vm.active, vm.live = True, dict(vm.config)
    return done


class TestWhatSnapshotsLeaveBehind:
    """Each leftover the design names, made by driving real sequences
    through ``SnapshotManager``: prune lists exactly what nothing accounts
    for as removable, and every other file of the VM as kept."""

    def test_a_restore_of_a_leaf_leaves_its_old_overlays(self, sm, tmp_path):
        """Source 2 (#266): the revert records new overlays in place of the
        snapshot's, and the old ones are put back recorded by nothing."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        assert _restore(sm, vm, "s1") is True
        assert _restore(sm, vm, "s1") is True
        left = _unaccounted(vm)
        assert len(left) == 2       # vm01.s1, and the first revert's overlay

        found = _prune(vm)

        assert found[REMOVABLE] == left
        assert UNKNOWN not in found

    def test_a_restore_away_from_one_restored_leaves_its_revert_overlay(
            self, sm, tmp_path):
        """Source 1 (#288): a snapshot restored with no take since records
        its revert's overlay in ``<revertDisks>``; the next revert deletes
        it and clears the record, and the backup puts it back."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        vm.take("s2")
        assert _restore(sm, vm, "s1") is True
        reverted = vm.snapshots["s1"]["revert_disks"]["vda"]
        assert _restore(sm, vm, "s2") is True

        found = _prune(vm)

        assert reverted in found[REMOVABLE]
        assert found[REMOVABLE] == _unaccounted(vm)

    def test_a_deleted_leaf_leaves_the_overlays_a_restore_put_back(
            self, sm, tmp_path):
        """Source 3b (#290): libvirt's delete of a leaf that is not current
        unlinks the overlay its parent's take made, not its own (#289)."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("t1")
        vm.take("t2")
        assert _restore(sm, vm, "t1") is True
        vm.take("t3")
        t2 = vm.snapshots["t2"]["disks"]["vda"]
        assert _delete(sm, vm, "t2") is True

        found = _prune(vm)

        assert found[REMOVABLE] == [t2] == _unaccounted(vm)

    def test_a_collapse_now_takes_the_revert_overlay_under_the_head_with_it(
            self, sm, tmp_path):
        """Source 3a (#292) is gone since #298: the collapse merges the
        revert's overlay into the head and removes it. What is left is the
        deleted leaf's own overlay."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("t1")
        vm.take("t2")
        assert _restore(sm, vm, "t1") is True
        reverted = vm.live["vda"]
        vm.take("t3")
        t2 = vm.snapshots["t2"]["disks"]["vda"]
        assert _delete(sm, vm, "t2") is True

        assert _collapse(sm, vm, "t1") is True

        assert not os.path.exists(reverted)
        assert _prune(vm)[REMOVABLE] == [t2] == _unaccounted(vm)

    def test_overlays_in_a_disk_snapshot_dir_and_of_a_data_disk(
            self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path, data_disk=True,
                               disk_dir=tmp_path / "fast")
        vm.take("s1")
        assert _restore(sm, vm, "s1") is True
        assert _restore(sm, vm, "s1") is True

        found = _prune(vm)

        assert found[REMOVABLE] == _unaccounted(vm)
        assert {os.path.dirname(p) for p in found[REMOVABLE]} == {
            str(vm.workdir), str(vm.disk_dir)}
        assert any("vm01_data." in p for p in found[REMOVABLE])

    def test_nothing_a_snapshot_records_or_the_vm_runs_on_is_removable(
            self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        for name in ("s1", "s2", "s3"):
            vm.take(name)
        found = _prune(vm)
        assert REMOVABLE not in found
        assert sorted(found[KEPT]) == sorted(
            [*(p for s in vm.snapshots.values() for p in s["disks"].values()
               if p),
             *(s["memory"] for s in vm.snapshots.values())])


class TestWhatPruneRemovesLeavesEverySnapshotWhole:
    """Design §8: drive each source, remove what prune lists, then every
    snapshot left validates and restores."""

    def test_after_the_restores_of_a_leaf(self, sm, tmp_path):
        """Sources 2 (#266) and 1 (#288)."""
        vm = _RevertingLibvirt(tmp_path, data_disk=True,
                               disk_dir=tmp_path / "fast")
        vm.take("s1")
        assert _restore(sm, vm, "s1") is True
        assert _restore(sm, vm, "s1") is True
        vm.take("s2")
        assert _restore(sm, vm, "s1") is True
        assert _restore(sm, vm, "s2") is True
        left = _unaccounted(vm)
        assert left

        removed = _prune_and_restore_each(sm, vm)

        assert removed == left
        assert not any(os.path.exists(path) for path in removed)

    def test_after_a_deleted_leaf_and_a_collapse(self, sm, tmp_path):
        """Source 3b (#290), after #298's collapse."""
        vm = _RevertingLibvirt(tmp_path, data_disk=True)
        vm.take("t1")
        vm.take("t2")
        assert _restore(sm, vm, "t1") is True
        vm.take("t3")
        t2 = vm.snapshots["t2"]["disks"]["vda"]
        assert _delete(sm, vm, "t2") is True
        assert _collapse(sm, vm, "t1") is True

        removed = _prune_and_restore_each(sm, vm)

        assert t2 in removed


def _entry(path: str, backing: str | None = None):
    """An entry for :func:`prune` of the file *path*, as listed now."""
    return Entry(path=path, vm=VM, kind=TAKE_OVERLAY, named="s",
                 lstat=os.lstat(path), backing=backing)


class TestTheRemoval:
    """G7 (:func:`prune`)."""

    def test_each_goes_and_what_it_took_is_reclaimed(self, box):
        a, b = box.overlay(f"{VM}.s1"), box.memory(f"{VM}_snapshot_s0.raw")
        entries = [_entry(a), _entry(b)]
        result = prune(entries)
        assert [e.path for e in result.removed] == [a, b]
        assert result.reclaimed == sum(e.size for e in entries) > 0
        assert not os.path.exists(a) and not os.path.exists(b)

    def test_the_newest_layer_comes_first(self, box):
        s1 = box.overlay(f"{VM}.s1")
        s2 = box.overlay(f"{VM}.s2", backing=s1)
        s3 = box.overlay(f"{VM}.s3", backing=s2)
        loose = box.overlay(f"{VM}.s0")
        entries = [_entry(s1), _entry(s2, s1), _entry(loose), _entry(s3, s2)]
        assert [e.path for e in removal_order(entries)] == [loose, s3, s2, s1]

    def test_built_on_is_told_by_resolved_path_and_by_identity(self, box,
                                                               tmp_path):
        s1 = box.overlay(f"{VM}.s1")
        (tmp_path / "link").symlink_to(box.wd)
        s2 = box.overlay(f"{VM}.s2")
        named_otherwise = str(tmp_path / "link" / f"{VM}.s1")
        entries = [_entry(s1), _entry(s2, named_otherwise)]
        assert [e.path for e in removal_order(entries)] == [s2, s1]

    def test_a_loop_of_backing_files_comes_last(self, box):
        a, b = box.overlay(f"{VM}.a"), box.overlay(f"{VM}.b")
        c = box.overlay(f"{VM}.c")
        order = removal_order([_entry(a, b), _entry(b, a), _entry(c)])
        assert [e.path for e in order] == [c, a, b]

    def test_a_failed_removal_keeps_everything_below_it(self, box):
        s1 = box.overlay(f"{VM}.s1")
        s2 = box.overlay(f"{VM}.s2", backing=s1)
        s3 = box.overlay(f"{VM}.s3", backing=s2)
        tried = []

        def remove(path, identity):
            tried.append(path)
            raise PermissionError(13, "Permission denied")

        result = prune([_entry(s1), _entry(s2, s1), _entry(s3, s2)],
                       remove=remove)
        assert tried == [s3]
        assert [(e.path, why) for e, why in result.failed] == [
            (s3, "it could not be removed (Permission denied)")]
        assert [(e.path, why) for e, why in result.kept] == [
            (s2, f"{s3}, which stays, is built on it"),
            (s1, f"{s2}, which stays, is built on it")]
        assert all(os.path.exists(p) for p in (s1, s2, s3))

    def test_a_file_replaced_since_it_was_listed_stays_with_what_is_below(
            self, box):
        s1 = box.overlay(f"{VM}.s1")
        s2 = box.overlay(f"{VM}.s2", backing=s1)
        entries = [_entry(s1), _entry(s2, s1)]
        replacement = box.wd / "replacement"
        replacement.write_bytes(b"another file")
        os.replace(replacement, s2)

        result = prune(entries)

        assert [(e.path, why) for e, why in result.failed] == [
            (s2, "it was replaced after it was listed, and stays")]
        assert [e.path for e, _why in result.kept] == [s1]
        assert Path(s2).read_bytes() == b"another file"
        assert os.path.exists(s1)

    def test_one_gone_meanwhile_holds_nothing_up(self, box):
        s1 = box.overlay(f"{VM}.s1")
        s2 = box.overlay(f"{VM}.s2", backing=s1)
        entries = [_entry(s1), _entry(s2, s1)]
        os.unlink(s2)
        result = prune(entries)
        assert [e.path for e in result.gone] == [s2]
        assert [e.path for e in result.removed] == [s1]

    def test_decide_says_what_each_entry_is_built_on(self, box):
        s1 = box.overlay(f"{VM}.s1")
        box.overlay(f"{VM}.s2", backing=s1)
        box.memory(f"{VM}_snapshot_s0.raw")
        found = box.decide()
        assert found[f"{VM}.s2"].backing == s1
        assert found[f"{VM}.s1"].backing == box.base
        assert found[f"{VM}_snapshot_s0.raw"].backing is None


class TestAPruneOfNamedPaths:
    """:func:`keep_unnamed`: a removable file not named stays, and keeps
    what it is built on."""

    def test_one_an_unnamed_file_is_built_on_is_kept(self, box):
        s1 = box.overlay(f"{VM}.s1")
        s2 = box.overlay(f"{VM}.s2", backing=s1)
        box.overlay(f"{VM}.s0")
        found = box.decide()
        keep_unnamed(list(found.values()), box.listing, box.scans,
                     {s1, str(box.wd / f"{VM}.s0")})
        assert (found[f"{VM}.s1"].status, found[f"{VM}.s1"].reason) == (
            KEPT, f"{s2} (not named) is built on it")
        assert found[f"{VM}.s2"].status == KEPT
        assert found[f"{VM}.s0"].status == REMOVABLE


class TestTheOverlaysRestoresLeftForLater:
    """#305's records: what a restore left beside an ISO for later removal
    is never prune's own to remove, whatever its name; prune has the
    removal a take runs remove it."""

    def test_a_file_one_records_is_kept_by_its_path(self, box):
        overlay = box.overlay(f"{VM}.1793626528")
        box.pending[overlay] = "a restore of vm01 left it for later removal"
        found = box.decide()[f"{VM}.1793626528"]
        assert (found.status, found.reason) == (
            KEPT, "a restore of vm01 left it for later removal")

    def test_and_by_its_identity(self, box):
        overlay = box.overlay(f"{VM}.1793626528")
        st = os.stat(overlay)
        box.pending.identities[(st.st_dev, st.st_ino)] = "recorded"
        assert box.decide()[f"{VM}.1793626528"].status == KEPT

    def test_only_the_chains_prune_listed_go(self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        assert _restore(sm, vm, "s1") is True
        listed = _waiting(vm)
        assert _restore(sm, vm, "s1") is True
        later = sorted(set(_waiting(vm)) - set(listed))
        assert listed and later
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            outcome = sm.remove_pending_iso_overlays(
                vm.VM, files_in_use=_files_in_use(vm), only=set(listed))
        assert (outcome.removed, outcome.failed, outcome.decided) == (
            listed, [], True)
        assert not any(os.path.exists(p) for p in listed)
        assert all(os.path.exists(p) for p in later)
        assert _waiting(vm) == later

    def test_what_a_domain_uses_is_kept_and_said_to_be(self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        assert _restore(sm, vm, "s1") is True
        [overlay] = _waiting(vm)
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            outcome = sm.remove_pending_iso_overlays(
                vm.VM, files_in_use=_files_in_use(vm, others=[overlay]))
        assert outcome.removed == [] and overlay in outcome.kept
        assert os.path.exists(overlay) and _waiting(vm) == [overlay]

    def test_a_scan_that_fails_decides_nothing(self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        assert _restore(sm, vm, "s1") is True
        [overlay] = _waiting(vm)
        execute, shell = _restoring(sm, vm)
        with execute, shell:
            outcome = sm.remove_pending_iso_overlays(
                vm.VM, files_in_use=lambda paths: None)
        assert outcome.decided is False and list(outcome.kept) == [overlay]

    def test_a_removal_that_fails_is_named(self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        assert _restore(sm, vm, "s1") is True
        [overlay] = _waiting(vm)
        execute, shell = _restoring(sm, vm)
        with execute, shell, patch.object(sm, "_remove_file",
                                          return_value=False):
            outcome = sm.remove_pending_iso_overlays(
                vm.VM, files_in_use=_files_in_use(vm))
        assert outcome.failed == [overlay]
        assert outcome.kept == {overlay: "it could not be removed"}

    def test_the_session_hands_only_and_its_scan_on(self):
        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {}
        in_use = FilesInUse({"/cache/nixos.1": "other01"})
        with patch.object(SnapshotManager, "remove_pending_iso_overlays",
                          autospec=True, return_value="outcome") as remove:
            assert session.remove_pending_iso_overlays(
                "vm01", scan=lambda: in_use, only={"/cache/nixos.1"}) == (
                "outcome")
        _sm, vm_name, asking = remove.call_args.args
        assert remove.call_args.kwargs == {"gone": False,
                                           "only": {"/cache/nixos.1"}}
        assert asking(["/cache/nixos.1"]) == {"/cache/nixos.1": (
            "domain other01 uses it")}


def _warnings(logger) -> str:
    return "\n".join(str(call.args[0])
                     for call in logger.warning.call_args_list)


class TestTheWarningsPointAtPrune:
    """Design §5: what snapshots leave behind is named, with the command
    that removes it; a ``.preserve`` that may be the only copy never gets
    "remove it"."""

    def test_a_leftover_backup_path_sends_to_a_dry_run_never_to_rm(
            self, sm, tmp_path):
        left = tmp_path / "vm01.s1.preserve"
        left.write_bytes(b"QFI\xfbmaybe the only copy")
        with pytest.raises(SnapshotError) as refused:
            sm._claim_backup_paths(
                [(str(tmp_path / "vm01.s1"), str(left))], {})
        said = str(refused.value)
        assert said.endswith(
            "It is most likely left over from an interrupted restore: run "
            "`boxman snapshot prune --dry-run`: it says whether this is a "
            "redundant copy or the only one.")
        assert "remove it" not in said

    def test_the_overlays_a_revert_put_back_are_prunes(self, sm, tmp_path):
        """Sources 1 and 2."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        with patch.object(sm, "logger") as logger:
            assert _restore(sm, vm, "s1") is True
        warned = _warnings(logger)
        assert "no snapshot of it records them any more. `boxman snapshot " \
               "prune` removes them once nothing uses them, an hour from " \
               "now (or sooner with --min-age)." in warned
        assert "by hand" not in warned

    def test_backups_a_cleanup_left_say_who_removes_which(self, sm,
                                                          tmp_path):
        overlay = tmp_path / "vm01.s1"
        overlay.write_bytes(b"QFI\xfb")
        copy, link = tmp_path / "vm01.s2.preserve", tmp_path / "vm01.s1.preserve"
        copy.write_bytes(b"QFI\xfb")
        os.link(overlay, link)
        with patch.object(sm.virsh, "execute_shell",
                          return_value=_result(ok=False, stderr="busy")), \
                patch.object(sm, "logger") as logger:
            sm._reap_preserve_files([str(copy), str(link),
                                     str(tmp_path / "gone.preserve")])
        assert _warnings(logger) == (
            f"could not remove the overlay backups (busy); `boxman snapshot "
            f"prune` removes each of these that is a spare copy or an "
            f"unfilled reservation: {copy}; these are second names of their "
            f"overlays (hard links), which prune keeps: remove them by hand: "
            f"{link}")

    def test_a_memory_image_a_delete_kept_is_prunes(self, sm, tmp_path):
        image = tmp_path / "vm01_snapshot_s1.raw"
        image.write_bytes(b"memory")
        with patch.object(sm, "logger") as logger:
            sm._remove_memory_images("vm01", {"s1": str(image)},
                                     recorded_in_use=lambda: None)
        assert ("whether another snapshot records it could not be checked. "
                "`boxman snapshot prune` removes it once no snapshot records "
                "it.") in _warnings(logger)
        assert image.exists()

    def test_a_deleted_leafs_own_overlays_are_named(self, sm, tmp_path):
        """Source 3b, new: libvirt's delete of a leaf that is not current
        leaves the overlays it recorded."""
        vm = _RevertingLibvirt(tmp_path, data_disk=True)
        vm.take("t1")
        vm.take("t2")
        assert _restore(sm, vm, "t1") is True
        vm.take("t3")
        t2 = vm.snapshots["t2"]["disks"]
        with patch.object(sm, "logger") as logger:
            assert _delete(sm, vm, "t2") is True
        assert (f"{vm.VM}: libvirt's delete of snapshot 't2' left "
                f"{t2['vda']}, {t2['vdb']}, which it recorded: no snapshot "
                f"of the VM records them now, and it runs on none of them. "
                f"`boxman snapshot prune` removes them once nothing uses "
                f"them.") in _warnings(logger)

    def test_a_delete_that_leaves_nothing_of_its_own_says_nothing(
            self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("s1")
        vm.take("s2")
        with patch.object(sm, "logger") as logger:
            assert _delete(sm, vm, "s2") is True
        assert "libvirt's delete of snapshot" not in _warnings(logger)

    def test_a_collapse_that_cannot_read_the_heads_chain_says_so(
            self, sm, tmp_path):
        """Source 3a, where it still applies after #298: what the rebase
        merges into the head cannot be told, so it is not removed."""
        vm = _RevertingLibvirt(tmp_path)
        vm.take("t1")
        vm.take("t2")
        assert _restore(sm, vm, "t1") is True
        vm.take("t3")
        assert _delete(sm, vm, "t2") is True
        with patch.object(sm, "_merged_layers", return_value=None), \
                patch.object(sm, "logger") as logger:
            assert _collapse(sm, vm, "t1") is True
        warned = _warnings(logger)
        assert "could not be read down to" in warned
        assert "`boxman snapshot prune` lists those nothing records or " \
               "uses." in warned

    def test_a_dropped_overlay_a_collapse_could_not_remove_is_named(
            self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("t1")
        vm.take("t2")
        vm.take("t3")
        dropped = vm.snapshots["t2"]["disks"]["vda"]
        run = vm.qemu_img

        def refusing_rm(command, *args, **kwargs):
            if command == f"rm -f '{dropped}'":
                return _result(ok=False, stderr="Operation not permitted")
            return run(command, *args, **kwargs)

        vm.qemu_img = refusing_rm
        with patch.object(sm, "logger") as logger:
            assert _collapse(sm, vm, "t1") is True
        assert (f"could not remove {dropped} of dropped snapshot 't2' on "
                f"{vm.VM}: Operation not permitted. `boxman snapshot prune` "
                f"removes it once nothing uses it.") in _warnings(logger)

    def test_what_a_collapse_could_not_check_is_prunes(self, sm, tmp_path):
        vm = _RevertingLibvirt(tmp_path)
        vm.take("t1")
        vm.take("t2")
        vm.take("t3")
        with patch.object(sm, "_collapse_keeps", return_value=None), \
                patch.object(sm, "logger") as logger:
            assert _collapse(sm, vm, "t1") is True
        warned = _warnings(logger)
        assert "of dropped snapshot 't2'" in warned
        assert "`boxman snapshot prune` removes it once nothing uses it." \
               in warned
        assert "by hand" not in warned
