"""
``boxman snapshot prune``: the snapshot files of a project's VMs that no
snapshot records and no domain uses any more (#294, finding 9).

Snapshots leave such files behind, and nothing removes them until
``destroy``:

- a restore puts back the overlays a revert deleted, and the ones no
  snapshot records any more stay (#266, #288): the old overlays of a
  snapshot restored with no take since, and those a revert to a leaf
  recorded new ones in place of;
- a collapse or a delete that cannot tell whether a file is still needed
  keeps it, and names it; a collapse by a boxman before #298 left the
  target's overlay, or a revert's, under the head; a delete of a leaf
  leaves the overlays an earlier restore put back (#290);
- an interrupted restore leaves its ``.preserve`` backups, their scratch
  copies and reservation scaffolds beside the overlays;
- a memory image no snapshot records: from before #295, from an
  interrupted run, or kept by a delete whose scan failed.

This module reads files, never libvirt. :func:`find_candidates` lists what
is named for one of the project's VMs in its snapshot directories (N1, N2),
and :func:`decide` gives each file a status -- ``removable``, ``kept`` with
the reason, or ``unknown`` -- from the scans ``LibVirtSession`` makes: what
every domain uses (G1), what every snapshot names (G2), and one header level
of every image in those directories (G3). :func:`prune` removes the
removable ones (G7): each only while it is still the file listed, newest
layer first, and a file that stays keeps what it is built on.
"""

from __future__ import annotations

import os
import re
import stat
import struct
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .disk_cleanup import FilesInUse, remove_if_unchanged
from .snapshot import SnapshotManager

REMOVABLE = "removable"
KEPT = "kept"
UNKNOWN = "unknown"
#: an entry of a kind ``--kind`` leaves out: not listed, and kept, so what
#: is built on it is kept too
SKIPPED = "skipped"

TAKE_OVERLAY = "take-overlay"
REVERT_OVERLAY = "revert-overlay"
MEMORY_IMAGE = "memory-image"
PRESERVE_BACKUP = "preserve-backup"
PRESERVE_SCRATCH = "preserve-scratch"
PRESERVE_RESERVATION = "preserve-reservation"

OVERLAYS = (TAKE_OVERLAY, REVERT_OVERLAY)
PRESERVES = (PRESERVE_BACKUP, PRESERVE_SCRATCH, PRESERVE_RESERVATION)

#: what ``--kind`` takes, and the kinds each one selects
KIND_GROUPS = {
    "overlay": OVERLAYS,
    "memory": (MEMORY_IMAGE,),
    "preserve": PRESERVES,
}

#: what a restore writes into a backup path it reserves, before the copy
#: replaces it (``SnapshotManager._reserve_one_path``)
RESERVATION_MARKER = SnapshotManager._RESERVATION_MARKER

#: the directory ``remove_if_unchanged`` moves a file into to unlink it;
#: one left behind holds what it could not put back
STRANDED_PREFIX = ".boxman-removing-"

#: a restore's backup of the overlay X, its scratch copy, and the scaffold
#: of either reservation (``SnapshotManager._reserve_one_path``). X is
#: greedy: a snapshot may be named ``a.preserve`` itself.
_PRESERVE = re.compile(r"(?P<x>.+)\.preserve(?P<copy>\.copy)?"
                       r"(?P<scaffold>\.[0-9]+\.[0-9a-f]{12})?")

#: the first bytes of every qcow2 image
_QCOW2_MAGIC = b"QFI\xfb"

#: why a removable file stays in a prune of named paths it is not one of
_NOT_NAMED = "it is not named"

#: a revert overlay's suffix is the epoch of the revert; outside this range
#: it is not taken for one when saying when it was made
_EPOCHS = range(10**9, 4 * 10**9)


@dataclass(frozen=True)
class SnapshotRecord:
    """What one snapshot names, as its XML does (the G2 scan): the
    overlays under ``<disks>`` and ``<revertDisks>``, by disk target; the
    source of each disk of the definition a revert to it builds on, the
    disk it was taken on; and its memory image."""

    disks: dict[str, str] = field(default_factory=dict)
    revert_disks: dict[str, str] = field(default_factory=dict)
    taken_on: dict[str, str] = field(default_factory=dict)
    memory: str | None = None


class References(FilesInUse):
    """
    G2: every file a snapshot of any domain names, and every image below
    each, by path and by identity (:class:`FilesInUse`), mapped to who
    names it -- ``snapshot 's1' of <domain>``. :attr:`snapshots` keeps what
    each snapshot records, by domain name, for the directories a project's
    snapshots were taken into.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        #: domain name -> {snapshot name: what it records}
        self.snapshots: dict[str, dict[str, SnapshotRecord]] = {}


class Held(FilesInUse):
    """
    G1: every file a domain uses -- ``LibVirtSession.disk_paths_in_use``,
    and the definition in a managed save, and an active backup job, each
    with their chains -- by path and by identity (:class:`FilesInUse`).
    :attr:`media` names each domain's CD-ROM and other media sources, whose
    directories no prune scans (#286).
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        #: media source -> the domain whose CD-ROM (or floppy) it is
        self.media: dict[str, str] = {}


@dataclass(frozen=True)
class ScopeDir:
    """A directory prune scans, and what makes it one of the project's
    snapshot directories."""

    path: str
    why: str


@dataclass
class VmFacts:
    """One of the project's VMs, as prune needs it."""

    #: its full domain name, ``bprj__<project>__bprj_<cluster>_<vm>``
    name: str
    cluster: str
    #: its domain's UUID; ``None`` when no domain of that name is defined
    uuid: str | None
    #: the disk paths it declares (``disk_path_for``): never candidates
    disks: tuple[str, ...] = ()
    #: the names of the disks it declares or attaches: ``<vm>_<name>.``
    disk_names: tuple[str, ...] = ()

    def prefixes(self) -> list[str]:
        """The names its files start with, literally (N1)."""
        return ([f"{self.name}.", f"{self.name}_snapshot_"]
                + [f"{self.name}_{disk}." for disk in self.disk_names])


@dataclass
class Entry:
    """A file named for one of the project's VMs (N1, N2), and its
    status."""

    path: str
    vm: str
    kind: str
    #: what its name gives: the snapshot (a take overlay, a memory image),
    #: the epoch (a revert overlay), or the overlay it is a backup of (a
    #: ``.preserve`` file, which needs that one)
    named: str
    #: its ``lstat`` when it was listed; ``None`` when it could not be read
    lstat: os.stat_result | None
    #: why it could not be read
    error: str = ""
    status: str = REMOVABLE
    reason: str = ""
    #: kept because a domain uses it or a snapshot records it: listed only
    #: with ``--all``
    recorded: bool = False
    #: the image right under it, as its header names it; None for a file
    #: built on nothing, or whose header was not read
    backing: str | None = None

    @property
    def size(self) -> int:
        """What it takes on disk (``st_blocks * 512``), 0 when unknown."""
        return self.lstat.st_blocks * 512 if self.lstat is not None else 0

    @property
    def identity(self) -> tuple[int, int] | None:
        if self.lstat is None:
            return None
        return (self.lstat.st_dev, self.lstat.st_ino)


@dataclass
class Listing:
    """What :func:`find_candidates` found in the scanned directories."""

    entries: list[Entry] = field(default_factory=list)
    #: every regular file there, and every symlink to one: whose header G3
    #: reads, candidates or not
    files: list[str] = field(default_factory=list)
    #: the ``.boxman-removing-*`` directories there, reported only
    stranded: list[str] = field(default_factory=list)
    #: a directory that could not be listed -> why
    unreadable: dict[str, str] = field(default_factory=dict)


@dataclass
class Scans:
    """What libvirt and the images said, for :func:`decide`."""

    #: G1; ``None`` when the scan failed
    held: Held | None
    #: G2; ``None`` when the scan failed
    references: References | None
    #: G3: path -> ``{"format": ..., "backing": ...}`` for every file of the
    #: listing, ``None`` when its header could not be read
    headers: dict[str, dict | None]
    #: memory image -> ``(name, uuid)`` its save header names, ``None`` when
    #: that could not be read
    owners: dict[str, tuple[str, str] | None] = field(default_factory=dict)
    #: overlays whose image lock a process holds (``qemu-img info`` without
    #: ``-U`` refused): advisory, a probe that could not run blocks nothing
    locked: set[str] = field(default_factory=set)
    #: ``(name, uuid)`` of every domain on the connection
    domains: list[tuple[str, str]] = field(default_factory=list)
    #: the mount points this process sees; ``None`` when unreadable
    mounts: set[str] | None = None
    #: why a scan failed, other than G1 and G2 being ``None``
    failures: list[str] = field(default_factory=list)
    #: the overlays of CD-ROM images restores left for later removal
    #: (``pending_overlays``), by path and by identity: never prune's to
    #: remove, but the take's, the teardown's, or prune's drain of them
    pending: FilesInUse = field(default_factory=FilesInUse)


def owner_of(name: str, vms: dict[str, VmFacts],
             known: set[str]) -> tuple[VmFacts, str] | None:
    """
    The VM a file *name* is named for (N1), and the prefix it is named by,
    or ``None``.

    Its name has to start, literally, with one of a VM's prefixes
    (:meth:`VmFacts.prefixes`), as a teardown's names are matched
    (``boot_family_candidates``). The longest VM name wins. Then any known
    name -- every domain on the connection and every VM of the project,
    *known* -- longer than that VM's that the file's name starts with, up
    to a ``.`` or a ``_``, takes the file from it: ``v.x.s1`` is domain
    ``v.x``'s overlay of snapshot ``s1`` before it is VM ``v``'s of
    ``x.s1``. Such a file is no one's here, and is not listed.
    """
    best: tuple[VmFacts, str] | None = None
    for vm in vms.values():
        for prefix in vm.prefixes():
            if (name.startswith(prefix) and len(name) > len(prefix)
                    and (best is None or len(vm.name) > len(best[0].name))):
                best = (vm, prefix)
    if best is None:
        return None
    mine = best[0].name
    for other in known:
        if (len(other) > len(mine) and name.startswith(other)
                and name[len(other):len(other) + 1] in (".", "_")):
            return None
    return best


def _named_kind(name: str, path: str, vm: VmFacts, prefix: str,
                vms: dict[str, VmFacts],
                known: set[str]) -> tuple[str, str] | None:
    """``(kind, what its name gives)`` for a file of *vm* named *name*
    after *prefix*, by its name alone (N2), or ``None`` when no snapshot
    makes a file of that name."""
    rest = name[len(prefix):]
    if prefix == f"{vm.name}_snapshot_":
        for ext in (".raw.zst", ".raw"):
            if rest.endswith(ext) and len(rest) > len(ext):
                return MEMORY_IMAGE, rest[:-len(ext)]
        return None
    preserve = _PRESERVE.fullmatch(name)
    if preserve is not None:
        stem = preserve.group("x")
        owner = owner_of(stem, vms, known)
        if (owner is None or owner[0] is not vm
                or owner[1] == f"{vm.name}_snapshot_"
                or os.path.join(os.path.dirname(path), stem) in vm.disks):
            return None
        if preserve.group("scaffold"):
            return PRESERVE_RESERVATION, stem
        if preserve.group("copy"):
            return PRESERVE_SCRATCH, stem
        return PRESERVE_BACKUP, stem
    if path in vm.disks:
        return None
    return (REVERT_OVERLAY if rest.isdigit() else TAKE_OVERLAY), rest


def find_candidates(dirs: list[ScopeDir], vms: dict[str, VmFacts],
                    known: set[str]) -> Listing:
    """
    The files in *dirs* named for one of *vms* (N1, :func:`owner_of`) that
    a snapshot makes (N2, by name): an overlay ``<vm>.<s>`` or
    ``<vm>_<disk>.<s>`` -- a revert's when *s* is all digits, its epoch --
    that is none of the VM's declared disks; a memory image
    ``<vm>_snapshot_<s>.raw`` or ``.raw.zst``; a restore's ``<X>.preserve``,
    ``<X>.preserve.copy`` or reservation scaffold, X one of the VM's
    overlay names. Whether an overlay is one -- a qcow2 image built on
    another -- is read from its header (:func:`decide`).

    Every regular file there, and every symlink to one, is listed in
    :attr:`Listing.files` too, for G3; a ``.boxman-removing-*`` directory in
    :attr:`Listing.stranded`. A directory that does not exist holds none; one
    that cannot be listed is named in :attr:`Listing.unreadable`, which
    keeps every file (:func:`decide`). An entry that cannot be looked up
    stays a candidate, with no ``lstat``.
    """
    listing = Listing()
    for scope in dirs:
        try:
            names = sorted(os.listdir(scope.path))
        except FileNotFoundError:
            continue
        except OSError as exc:
            listing.unreadable[scope.path] = str(exc)
            continue
        for name in names:
            path = os.path.join(scope.path, name)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue        # gone since it was listed
            except OSError as exc:
                st, error = None, str(exc)
            else:
                error = ""
                if stat.S_ISDIR(st.st_mode):
                    if name.startswith(STRANDED_PREFIX):
                        listing.stranded.append(path)
                    continue
                if stat.S_ISREG(st.st_mode) or (
                        stat.S_ISLNK(st.st_mode) and os.path.isfile(path)):
                    listing.files.append(path)
            owner = owner_of(name, vms, known)
            if owner is None:
                continue
            vm, prefix = owner
            named = _named_kind(name, path, vm, prefix, vms, known)
            if named is None:
                continue
            kind, what = named
            listing.entries.append(Entry(path=path, vm=vm.name, kind=kind,
                                         named=what, lstat=st, error=error))
    return listing


#: how a libvirt save image starts (``virQEMUSaveHeader``, libvirt 10.10):
#: a finished one, or one being written
_SAVE_MAGICS = (b"LibvirtQemudSave", b"LibvirtQemudPart")
#: the header's size: the magic, then version, data_len, was_running,
#: compressed, cookieOffset and unused[14], each a uint32 in the writer's
#: byte order; the domain XML (data_len bytes, NUL-padded) follows
_SAVE_HEADER_SIZE = 16 + 4 * 5 + 4 * 14
#: how much of a decompressed memory image is read for its header: the
#: domain XML of any guest fits
SAVE_HEADER_READ = 1 << 20


def save_image_xml(data: bytes) -> str | None:
    """The domain XML a libvirt save image starting with *data* holds, as
    ``virsh save-image-dumpxml`` reads it from an uncompressed one; None
    when *data* is not the start of one this layout describes."""
    if (len(data) < _SAVE_HEADER_SIZE
            or data[:len(_SAVE_MAGICS[0])] not in _SAVE_MAGICS):
        return None
    for order in ("<", ">"):
        version, length = struct.unpack(f"{order}II", data[16:24])
        if version in (1, 2):
            break
    else:
        return None
    xml = data[_SAVE_HEADER_SIZE:_SAVE_HEADER_SIZE + length]
    if len(xml) < length:
        return None
    return xml.split(b"\0", 1)[0].decode("utf-8", "replace")


def save_image_owner(xml: str | None) -> tuple[str, str] | None:
    """``(name, uuid)`` of the domain definition *xml*, which a save image
    holds; None when it names neither."""
    try:
        root = ET.fromstring(xml or "")
    except ET.ParseError:
        return None
    name = (root.findtext("name") or "").strip()
    uuid = (root.findtext("uuid") or "").strip().lower()
    if root.tag != "domain" or not name or not uuid:
        return None
    return name, uuid


def is_qcow2_file(path: str) -> bool | None:
    """Whether *path* starts as a qcow2 image does, read by this process;
    ``None`` when it cannot be read here (a 0600 file libvirt made as
    root)."""
    try:
        with open(path, "rb") as fh:
            return fh.read(len(_QCOW2_MAGIC)) == _QCOW2_MAGIC
    except OSError:
        return None


def parse_duration(text: str) -> int:
    """
    ``--min-age``: whole seconds, or a number followed by ``s``, ``m``,
    ``h`` or ``d``. 0 is allowed.

    Raises:
        ValueError: anything else.
    """
    match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(text))
    if match is None:
        raise ValueError(f"not a duration: {text!r} (say 3600, 30m, 1h or "
                         f"2d)")
    unit = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return int(match.group(1)) * unit


def format_duration(seconds: float) -> str:
    """*seconds* the way ``--min-age`` takes them, in the largest unit that
    divides them, or rounded down to minutes past an hour."""
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def parse_kinds(text: str) -> tuple[str, ...]:
    """
    ``--kind``: a comma-separated list of ``overlay``, ``memory`` and
    ``preserve``; the kinds they select.

    Raises:
        ValueError: an empty list, or another word.
    """
    words = [word.strip() for word in str(text).split(",") if word.strip()]
    unknown = [word for word in words if word not in KIND_GROUPS]
    if not words or unknown:
        raise ValueError(f"--kind takes {', '.join(KIND_GROUPS)} (comma-"
                         f"separated), not {text!r}")
    return tuple(kind for word in dict.fromkeys(words)
                 for kind in KIND_GROUPS[word])


class _Index:
    """A :class:`FilesInUse` asked by path -- as given and resolved -- and
    by identity."""

    def __init__(self, scan: FilesInUse | None) -> None:
        self.scan = scan if scan is not None else FilesInUse()

    def who(self, path: str, identity: tuple[int, int] | None) -> str | None:
        return (self.scan.get(path) or self.scan.get(os.path.realpath(path))
                or (self.scan.identities.get(identity)
                    if identity is not None else None))


def _identity_of(path: str) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of the file *path* names, links followed; None
    when it cannot be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _when(entry: Entry) -> str:
    """When a revert made *entry*: the epoch its name gives, else its
    modification time."""
    seconds = (int(entry.named) if int(entry.named) in _EPOCHS
               else entry.lstat.st_mtime if entry.lstat else 0)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))


class _Decision:
    """The reasons one entry gathers: the first ``kept`` one decides,
    else the first ``unknown`` one, else it is removable."""

    def __init__(self) -> None:
        self.kept: tuple[str, bool] | None = None
        self.unknown: str | None = None
        self.removable = ""

    def keep(self, reason: str, recorded: bool = False) -> None:
        if self.kept is None:
            self.kept = (reason, recorded)

    def doubt(self, reason: str) -> None:
        if self.unknown is None:
            self.unknown = reason

    def apply(self, entry: Entry) -> None:
        if self.kept is not None:
            entry.status, (entry.reason, entry.recorded) = KEPT, self.kept
        elif self.unknown is not None:
            entry.status, entry.reason = UNKNOWN, self.unknown
        else:
            entry.status, entry.reason = REMOVABLE, self.removable


def _read_marker(path: str) -> bool | None:
    """Whether *path* holds just the reservation marker; None when it
    cannot be read."""
    try:
        with open(path, "rb") as fh:
            return fh.read(len(RESERVATION_MARKER) + 1) == RESERVATION_MARKER
    except OSError:
        return None


def _preserve_verdict(entry: Entry, decision: _Decision) -> None:
    """G6 for a restore's leftover beside the overlay X it backs up."""
    st = entry.lstat
    stem = os.path.join(os.path.dirname(entry.path), entry.named)
    marker = None
    if st.st_size == len(RESERVATION_MARKER):
        marker = _read_marker(entry.path)
        if marker is None:
            decision.doubt("it could not be read, to tell whether it is an "
                           "unfilled backup reservation")
            return
    named = _PRESERVE.fullmatch(os.path.basename(entry.path))
    if named is not None and named.group("scaffold"):
        if marker:
            decision.removable = ("the scaffold of a backup reservation, "
                                  "left by an interrupted restore")
        else:
            decision.doubt("it is named as a reservation scaffold, but does "
                           "not hold the reservation marker")
        return
    if marker:
        # a .preserve, or its scratch .preserve.copy, the copy never filled:
        # a restore reserves both before it copies
        entry.kind = PRESERVE_RESERVATION
        decision.removable = "an unfilled backup reservation"
        return
    try:
        original = os.lstat(stem)
    except FileNotFoundError:
        original = None
    except OSError as exc:
        decision.doubt(f"whether {stem}, which it backs up, is in place "
                       f"cannot be told ({exc})")
        return
    if original is None:
        if entry.kind == PRESERVE_BACKUP:
            decision.keep(f"the only copy of {stem}, which is missing: "
                          f"mv -fT {entry.path} {stem}")
        else:
            decision.keep(f"{stem} is missing, and this copy of it may be "
                          f"what is left of it: check it")
        return
    if not stat.S_ISREG(original.st_mode):
        decision.doubt(f"{stem}, which it backs up, is not a regular file")
        return
    if entry.kind == PRESERVE_SCRATCH:
        decision.removable = (f"the scratch copy of an interrupted restore; "
                              f"{entry.named} is in place")
    elif (original.st_size, original.st_mtime_ns) == (st.st_size,
                                                       st.st_mtime_ns):
        decision.removable = (f"a spare copy of {entry.named}, which is in "
                              f"place")
    else:
        decision.doubt(f"it differs from {entry.named}, which is in place: "
                       f"which one holds the overlay cannot be told")


def _memory_verdict(entry: Entry, vm: VmFacts, scans: Scans,
                    decision: _Decision) -> None:
    """N3 for a memory image: its save header must name this VM."""
    owner = scans.owners.get(entry.path)
    if owner is None:
        decision.doubt("its save header could not be read")
        return
    name, uuid = owner
    if name != vm.name:
        decision.keep(f"its save header names domain {name}: written by "
                      f"that domain")
        return
    if uuid != vm.uuid:
        other = next((n for n, u in scans.domains if u == uuid), None)
        if other is not None:
            decision.doubt(f"its save header names {name} with the uuid of "
                           f"domain {other}")
            return
    snapshots = (scans.references.snapshots.get(vm.name, {})
                 if scans.references is not None else {})
    decision.removable = (
        f"snapshot '{entry.named}' records another memory image"
        if entry.named in snapshots
        else f"no snapshot '{entry.named}' is left; nothing records it")


def _overlay_verdict(entry: Entry, vm: VmFacts, vms: dict[str, VmFacts],
                     known: set[str], header: dict, media: _Index,
                     layers: set[tuple[int, int]], scans: Scans,
                     decision: _Decision) -> None:
    """N3 for an overlay: the image right under it must be this VM's -- by
    its name (a gone one too, #289), or by identity one of *layers*, this
    VM's disk layers named for it -- and no domain's media source (#286). A
    shared template under a linked clone never counts: only one level is
    read."""
    backing = header.get("backing")
    identity = _identity_of(backing)
    domain = media.who(backing, identity)
    if domain:
        decision.keep(f"it is built on {backing}, a media source of domain "
                      f"{domain}")
        return
    owner = owner_of(os.path.basename(backing), vms, known)
    ours = (owner is not None and owner[0] is vm) or (
        identity is not None and identity in layers)
    if not ours:
        decision.keep(f"it is built on {backing}, which is not named for "
                      f"{vm.name}")
        return
    snapshots = (scans.references.snapshots.get(vm.name, {})
                 if scans.references is not None else {})
    if entry.kind == REVERT_OVERLAY:
        decision.removable = (f"made by a revert on {_when(entry)}; nothing "
                              f"records or uses it")
    elif entry.named in snapshots:
        decision.removable = (f"snapshot '{entry.named}' no longer records "
                              f"it")
    else:
        decision.removable = (f"no snapshot '{entry.named}' is left; nothing "
                              f"records or uses it")


def scan_failures(listing: Listing, scans: Scans) -> list[str]:
    """Why nothing at all can be removed, if anything says so: a directory
    that cannot be listed, a G1, G2 or G3 scan that failed, or the mount
    table that cannot be read."""
    failures = list(scans.failures)
    failures += [f"{path} could not be listed ({why})"
                 for path, why in sorted(listing.unreadable.items())]
    if scans.held is None:
        failures.append("what the domains use could not be told (see the "
                        "warnings above)")
    if scans.references is None:
        failures.append("what the snapshots of every domain name could not "
                        "be told (see the warnings above)")
    if scans.mounts is None:
        failures.append("the mount table could not be read")
    for path in listing.files:
        if (scans.headers.get(path) is None and scans.owners.get(path) is None
                and os.path.lexists(path)):
            failures.append(f"the header of {path} could not be read, so "
                            f"whether it is built on a file listed here "
                            f"cannot be told")
            break
    return failures


def decide(listing: Listing, vms: dict[str, VmFacts], known: set[str],
           scans: Scans, now: float, min_age: float,
           kinds: tuple[str, ...] | None = None) -> list[Entry]:
    """
    Give every entry of *listing* its status, and return the entries --
    an overlay whose header says it is no image built on another left out:
    no snapshot makes it.

    A file is ``removable`` only when everything below holds, each by path
    -- as named and resolved -- and by identity:

    - N3, provenance: an overlay is built right on an image of its VM
      (:func:`_overlay_verdict`); a memory image's save header names its VM
      (:func:`_memory_verdict`); and the VM has a domain (a VM whose
      domain is gone is ``destroy``'s to clean up);
    - G1: no domain uses it, or holds it in a managed save or a backup job,
      and it is no domain's media source;
    - G2: no snapshot of any domain names it;
    - G3: no file in the scanned directories that is not removable --
      candidate or not -- is built on it, asked until nothing changes: a
      kept file keeps everything below it;
    - G4: a regular file with one name, no mount point, in a directory the
      user may write to;
    - G5: its status changed longer than *min_age* seconds before *now*
      (``st_ctime``: a rename sets it, so a put-back overlay counts from
      its restore, and a restore that is running is left alone);
    - G6: a ``.preserve`` backup is an unfilled reservation, or a copy of
      an overlay that is in place (:func:`_preserve_verdict`);
    - no process holds its image lock (advisory).

    A failed scan (:func:`scan_failures`) makes every entry ``unknown``. An
    entry of a kind not in *kinds* is ``skipped``: kept, and not listed.
    """
    failures = scan_failures(listing, scans)
    held = _Index(scans.held)
    references = _Index(scans.references)
    media_scan = FilesInUse()
    for path, domain in (scans.held.media.items() if scans.held else ()):
        media_scan.setdefault(path, domain)
        media_scan.setdefault(os.path.realpath(path), domain)
        identity = _identity_of(path)
        if identity is not None:
            media_scan.identities.setdefault(identity, domain)
    media = _Index(media_scan)
    # each VM's disk layers named for it, by identity: what N3 takes an
    # overlay to be built on under a name that is not its VM's
    layers: dict[str, set[tuple[int, int]]] = {}
    for path, domain in (scans.held.items() if scans.held else ()):
        found = owner_of(os.path.basename(path), vms, known)
        identity = _identity_of(path) if found else None
        if found and found[0].name == domain and identity is not None:
            layers.setdefault(domain, set()).add(identity)
    entries = []
    for entry in listing.entries:
        header = scans.headers.get(entry.path)
        if (entry.kind in OVERLAYS and header is not None
                and (header.get("format") != "qcow2"
                     or not header.get("backing"))):
            continue        # no overlay: <vm>.save, a standalone image
        entries.append(entry)
        entry.backing = (header or {}).get("backing") or None
        decision = _Decision()
        vm = vms[entry.vm]
        if failures:
            decision.doubt(f"nothing can be removed: {failures[0]}")
        elif vm.uuid is None:
            decision.doubt(f"{vm.name} has no domain: what is left of it is "
                           f"destroy's to remove")
        elif entry.lstat is None:
            decision.doubt(f"it could not be inspected ({entry.error})")
        else:
            _gates(entry, vm, vms, known, header, held, references, media,
                   layers.get(vm.name, set()), scans, now, min_age,
                   decision)
        decision.apply(entry)
        if kinds is not None and entry.kind not in kinds:
            entry.status = SKIPPED
    _keep_what_is_built_on(entries, listing, scans)
    return entries


def _gates(entry: Entry, vm: VmFacts, vms: dict[str, VmFacts],
           known: set[str], header: dict | None, held: _Index,
           references: _Index, media: _Index,
           layers: set[tuple[int, int]], scans: Scans, now: float,
           min_age: float, decision: _Decision) -> None:
    """Every gate of :func:`decide` but G3, for one entry whose VM has a
    domain and whose ``lstat`` was read."""
    st = entry.lstat
    identity = entry.identity
    domain = held.who(entry.path, identity)
    if domain:
        decision.keep(f"domain {domain} uses it", recorded=True)
    domain = media.who(entry.path, identity)
    if domain:
        decision.keep(f"it is a media source of domain {domain}",
                      recorded=True)
    who = references.who(entry.path, identity)
    if who:
        decision.keep(f"{who} names it", recorded=True)
    who = _Index(scans.pending).who(entry.path, identity)
    if who:
        decision.keep(who)
    if not stat.S_ISREG(st.st_mode):
        decision.keep("it is a symlink or not a regular file")
    elif st.st_nlink > 1:
        decision.keep(f"it has {st.st_nlink} names (hard links): removing "
                      f"one frees nothing")
    if entry.path in (scans.mounts or ()):
        decision.keep("it is a mount point")
    if not os.access(os.path.dirname(entry.path), os.W_OK):
        decision.keep("its directory is not writable by you")
    if entry.path in scans.locked:
        decision.keep("a process holds its image lock")
    if entry.kind in OVERLAYS:
        if header is None:
            decision.doubt("its header could not be read")
        else:
            _overlay_verdict(entry, vm, vms, known, header, media, layers,
                             scans, decision)
    elif entry.kind == MEMORY_IMAGE:
        _memory_verdict(entry, vm, scans, decision)
    elif stat.S_ISREG(st.st_mode):
        _preserve_verdict(entry, decision)
    age = now - st.st_ctime
    if age < min_age:
        decision.keep(f"too recent (changed {format_duration(age)} ago; "
                      f"--min-age {format_duration(min_age)})")


def _keep_what_is_built_on(entries: list[Entry], listing: Listing,
                           scans: Scans) -> None:
    """G3: an entry that a file in the scanned directories is built on --
    one that is not removable itself -- is kept, and so, in turn, is what
    that one is built on, until nothing changes."""
    removable = {entry.path: entry for entry in entries
                 if entry.status == REMOVABLE}
    by_identity = {entry.identity: entry for entry in removable.values()
                   if entry.identity is not None}
    # what each entry is, said in brackets after its name
    status = {entry.path: ("too recent" if entry.reason.startswith(
                               "too recent")
                           else "not named" if entry.reason == _NOT_NAMED
                           else entry.status)
              for entry in entries}
    below: dict[str, tuple[str, tuple[int, int] | None]] = {}
    for path in listing.files:
        backing = (scans.headers.get(path) or {}).get("backing")
        if backing:
            below[path] = (backing, _identity_of(backing))
    changed = True
    while changed:
        changed = False
        for path, (backing, identity) in below.items():
            if path in removable:
                continue
            under = (removable.get(backing)
                     or removable.get(os.path.realpath(backing))
                     or (by_identity.get(identity) if identity else None))
            if under is None or under.status != REMOVABLE:
                continue
            what = status.get(path)
            under.status = KEPT
            under.reason = (f"{path}{f' ({what})' if what else ''} is built "
                            f"on it")
            del removable[under.path]
            status[under.path] = KEPT
            changed = True


def _built_on(upper: Entry, lower: Entry) -> bool:
    """Whether *upper*'s header names *lower* as the image right under it,
    by path -- as named and resolved -- or by identity."""
    if not upper.backing:
        return False
    return (upper.backing == lower.path
            or os.path.realpath(upper.backing) == os.path.realpath(lower.path)
            or (lower.identity is not None
                and _identity_of(upper.backing) == lower.identity))


def removal_order(entries: list[Entry]) -> list[Entry]:
    """
    *entries* newest layer first: each before every one of them it is built
    on, by path otherwise. Entries in a loop of backing files, which no
    qcow2 chain can be, come last, by path.
    """
    above = {id(entry): [other for other in entries
                         if other is not entry and _built_on(other, entry)]
             for entry in entries}
    order: list[Entry] = []
    placed: set[int] = set()
    left = sorted(entries, key=lambda e: e.path)
    while left:
        ready = [entry for entry in left
                 if all(id(up) in placed for up in above[id(entry)])]
        if not ready:
            order += left
            break
        order += ready
        placed.update(id(entry) for entry in ready)
        left = [entry for entry in left if id(entry) not in placed]
    return order


@dataclass
class Removal:
    """What :func:`prune` did with each entry it was given."""

    removed: list[Entry] = field(default_factory=list)
    #: removed by something else before it got to them
    gone: list[Entry] = field(default_factory=list)
    #: ``(entry, why)``: replaced since it was listed, or its removal failed
    failed: list[tuple[Entry, str]] = field(default_factory=list)
    #: ``(entry, why)``: a file built on it stays
    kept: list[tuple[Entry, str]] = field(default_factory=list)

    @property
    def reclaimed(self) -> int:
        return sum(entry.size for entry in self.removed)


def prune(entries: list[Entry], remove=None) -> Removal:
    """
    G7: remove each of *entries*, removable ones, newest layer first
    (:func:`removal_order`), each only while it is still the file that was
    listed -- *remove*, by default ``remove_if_unchanged``, given its
    identity when it was listed. A file replaced since is put back under
    its name, and stays.

    A file that stays -- replaced since, or whose removal failed -- keeps
    every entry it is built on, all the way down, as a teardown keeps what
    is below a layer it keeps: removed, they would leave it on nothing.
    One that something else removed first holds nothing up.
    """
    remove = remove or remove_if_unchanged
    result = Removal()
    held: dict[str, str] = {}

    def keep_below(entry: Entry) -> None:
        for lower in entries:
            if lower is not entry and _built_on(entry, lower):
                held.setdefault(lower.path, f"{entry.path}, which stays, is "
                                            f"built on it")

    for entry in removal_order(entries):
        if entry.path in held:
            result.kept.append((entry, held[entry.path]))
            keep_below(entry)
            continue
        try:
            outcome, where = remove(entry.path, entry.identity)
        except OSError as exc:
            result.failed.append(
                (entry, f"it could not be removed "
                        f"({exc.strerror or exc})"))
            keep_below(entry)
            continue
        if outcome == "removed":
            result.removed.append(entry)
        elif outcome == "gone":
            result.gone.append(entry)
        else:
            result.failed.append((entry, (
                "it was replaced after it was listed, and stays"
                if outcome == "restored" else
                f"it was replaced after it was listed, and its name was "
                f"taken again before the replacement could be put back; the "
                f"replacement is at {where}")))
            keep_below(entry)
    return result


def keep_unnamed(entries: list[Entry], listing: Listing, scans: Scans,
                 named: set[str]) -> None:
    """
    For a prune of the paths *named* alone: every removable entry not
    named stays (``kept``), and so does what it is built on (G3, asked
    again), so that no file left is built on one removed.
    """
    for entry in entries:
        if entry.status == REMOVABLE and entry.path not in named:
            entry.status, entry.reason = KEPT, _NOT_NAMED
    _keep_what_is_built_on(entries, listing, scans)
