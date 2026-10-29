"""
Filesystem-side helpers for removing a VM's storage.

:func:`remove_vm_storage` is the one routine every VM teardown removes
storage through (#208): it decides, from a :class:`StorageInventory` taken
before the domain was undefined, which files the VM owns, and unlinks only
those, each through :func:`remove_if_unchanged`. libvirt itself deletes
nothing.

:func:`remove_vm_disks` is the older name-based sweep behind
:meth:`LibVirtSession.destroy_disks`, pinned by
``tests/test_libvirt_session.py::TestDestroyDisks``: the boot disk, the
named extra disks and the snapshot artifacts prefixed with the VM name. No
teardown uses it any more.
"""

from __future__ import annotations

import contextlib
import glob as _glob
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from boxman import log
from boxman.utils.io import mkstemp_in

from .disk import disk_path_for
from .disk_ownership import ROLE_DATA, DiskRecord


def remove_vm_disks(
    workdir: str,
    vm_name: str,
    extra_disks: Iterable[dict[str, str]] = (),
    protected: Iterable[str] = (),
) -> bool:
    """
    Delete the files on disk belonging to *vm_name* under *workdir*.

    Files removed:

    - ``<workdir>/<vm_name>.qcow2`` — the boot disk.
    - ``<workdir>/<vm_name>_<d['name']>.qcow2`` for each entry in
      *extra_disks*.
    - Snapshot artifacts matching ``<workdir>/<vm_name>.<suffix>``
      (overlay files such as ``<vm>.2026-04-21T08:00:00`` or
      ``<vm>.1772465824`` — note the literal dot separator) and
      ``<workdir>/<vm_name>_snapshot_*`` (memory snapshot ``.raw``
      files).

    Other VMs' disks in the same workdir are untouched because every
    pattern requires the VM name to be followed by a literal ``.`` or
    ``_snapshot_`` separator — a VM named ``web`` never matches files
    belonging to a VM named ``web2``.

    Args:
        workdir: Directory the VM's files live in. ``~`` is expanded.
        vm_name: Full VM name (typically ``bprj__<project>__bprj_<cluster>_<vm>``).
        extra_disks: Iterable of extra-disk config dicts; each dict is
            expected to have a ``name`` key used to build the filename.
        protected: Paths the sweep must leave alone whatever their name,
            because the caller decides them on recorded ownership. The
            ``<vm>_snapshot_*`` pattern also matches an extra disk whose
            logical name starts with ``snapshot_``.

    Returns:
        ``True`` once the sweep completes (even if there was nothing to
        delete). Always ``True`` today — mirrors the legacy method
        signature; a future revision may promote individual failures
        to exceptions.
    """
    workdir = os.path.expanduser(workdir)
    # compared resolved, so a workdir reached through a symlink still
    # matches the paths libvirt reports
    kept = {os.path.realpath(os.path.expanduser(path)) for path in protected}

    def removable(path: str) -> bool:
        return os.path.isfile(path) and os.path.realpath(path) not in kept

    boot_disk = disk_path_for(workdir, vm_name)
    if removable(boot_disk):
        os.remove(boot_disk)

    for disk in extra_disks:
        disk_path = disk_path_for(workdir, disk["name"], disk_prefix=vm_name)
        if removable(disk_path):
            os.remove(disk_path)

    for leftover in boot_family_files(workdir, vm_name):
        if removable(leftover):
            log.info(f"removing snapshot artifact: {leftover}")
            os.remove(leftover)

    return True


def boot_family_files(workdir: str, vm_name: str) -> list[str]:
    """
    The files under boxman's exclusive names for *vm_name* in *workdir*:
    its boot disk and overlays ``<vm>.<suffix>`` (``<vm>.qcow2``,
    ``<vm>.2026-04-21T08:00:00``, ``<vm>.1772465824`` — the literal dot
    separates the VM name from the suffix) and its memory-snapshot files
    ``<vm>_snapshot_*``.

    A plain ``<vm>*`` prefix glob would also match the disks of another VM
    whose name starts with this one's (destroying ``web`` would delete
    ``web2.qcow2``), so both patterns require a separator. The second one
    also matches every file of an extra disk whose logical name starts with
    ``snapshot_``; callers that know the VM's extra disks must keep those
    out (see :func:`remove_vm_storage`).
    """
    workdir = os.path.expanduser(workdir)
    found = []
    for pattern in (f'{vm_name}.*', f'{vm_name}_snapshot_*'):
        found.extend(path for path in _glob.glob(os.path.join(workdir, pattern))
                     if os.path.isfile(path) or os.path.islink(path))
    return sorted(found)


def boot_family_candidates(workdir: str, vm_name: str) -> list[str]:
    """
    :func:`boot_family_files`, for a teardown to decide with — which may not
    take "cannot tell" for "nothing there".

    ``glob`` and the ``isfile``/``islink`` filter both swallow errors, so a
    directory that exists but cannot be listed, or can be listed but not
    searched (every entry's lookup then fails), read as holding none: a
    VM-named qcow2 CD-ROM there was never a candidate, never refused and
    never kept, and the saved inventory that protected it was dropped (#208).
    Here only a directory that does not exist holds none, and an entry
    under the VM's names that cannot be looked up stays a candidate (the
    refusals, which look up every candidate, then keep it).

    The VM's names are matched literally, as every other name rule of the
    teardown is (``<vm>.``, ``<vm>_snapshot_``, ``<vm>_<name>.``): a glob
    took ``node[1]`` for a pattern, listed ``node1.qcow2`` — another VM's
    disk — and missed ``node[1].qcow2``. *workdir* is taken as the path it
    is, never as a pattern, too. A directory that lists fine gives exactly
    what :func:`boot_family_files` gives, for a vm name and a *workdir*
    without glob metacharacters.

    Raises:
        OSError: If *workdir* exists but cannot be listed.
    """
    workdir = os.path.expanduser(workdir)
    try:
        names = os.listdir(workdir)
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        if not (name.startswith(f'{vm_name}.')
                or name.startswith(f'{vm_name}_snapshot_')):
            continue
        path = os.path.join(workdir, name)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue        # gone since it was listed
        except OSError:
            found.append(path)
            continue
        # what boot_family_files keeps: a symlink, or a regular file
        if stat.S_ISLNK(st.st_mode) or stat.S_ISREG(st.st_mode):
            found.append(path)
    return sorted(found)


def is_memory_image(vm_name: str, path: str) -> bool:
    """
    Whether *path* is named as ``snapshot take`` names *vm_name*'s memory
    images: ``<vm>_snapshot_<name>.raw``, or the ``.raw.zst`` that
    compression leaves. The name only: what the file is, is not read.
    """
    name = os.path.basename(path)
    return (name.startswith(f"{vm_name}_snapshot_")
            and name.endswith((".raw", ".raw.zst")))


def memory_image_name(vm_name: str, snapshot_name: str) -> str:
    """The name ``snapshot take`` gives the memory image of *vm_name*'s
    snapshot *snapshot_name*; compression leaves it with ``.zst`` added."""
    return f"{vm_name}_snapshot_{snapshot_name}.raw"


def memory_image_candidates(directory: str, vm_name: str) -> list[str]:
    """
    The entries of *directory* named as *vm_name*'s memory images
    (:func:`is_memory_image`). A teardown only names them: in a cluster's
    ``memory_snapshot_dir`` it removes no file by its name, since another
    VM's full name can begin with this one's (:func:`remove_vm_storage`).

    Read as :func:`boot_family_candidates` reads a workdir: only a directory
    that does not exist holds none, a symlink or an entry that cannot be
    looked up stays a candidate (the refusals then keep it), and the name is
    matched literally, never as a pattern.

    Raises:
        OSError: If *directory* exists but cannot be listed.
    """
    return [path for path in boot_family_candidates(directory, vm_name)
            if is_memory_image(vm_name, path)]


def snapshot_only_overlays(overlays: dict[str, list[str]] | None,
                           sources: Iterable[str]) -> list[str]:
    """The overlays *overlays* records that are none of *sources* — each
    once, as recorded: those whose backing chains are read apart from the
    disks' (``StorageInventory.overlay_chains``)."""
    sources = set(sources)
    found: list[str] = []
    for paths in (overlays or {}).values():
        for path in paths:
            if path not in sources and path not in found:
                found.append(path)
    return found


def file_identities(paths: Iterable[str]) -> dict[str, tuple[int, int]]:
    """
    ``(st_dev, st_ino)`` of each path that exists, without following a
    final symlink. Paths that cannot be inspected are left out, so they can
    never match later.
    """
    identities = {}
    for path in paths:
        try:
            st = os.lstat(path)
        except OSError:
            continue
        identities[path] = (st.st_dev, st.st_ino)
    return identities


def _directory_identities(directories: Iterable[str],
                          ) -> tuple[set[tuple[int, int]], dict[str, OSError]]:
    """
    ``(st_dev, st_ino)`` of each of *directories* that exists, links followed
    by the filesystem — never a path folded by its spelling, which
    ``os.path.realpath`` does past a link it cannot read — and, apart, each
    one that could not be looked up for a reason other than not existing.
    """
    identities: set[tuple[int, int]] = set()
    unreadable: dict[str, OSError] = {}
    for directory in directories:
        try:
            st = os.stat(os.path.expanduser(directory))
        except FileNotFoundError:
            continue
        except OSError as exc:
            unreadable[directory] = exc
            continue
        identities.add((st.st_dev, st.st_ino))
    return identities, unreadable


def _directory_is_one_of(path: str,
                         identities: set[tuple[int, int]]) -> bool:
    """Whether the directory *path* is in is, by identity
    (:func:`_directory_identities`), one of those *identities* names;
    ``False`` when it cannot be looked up."""
    try:
        st = os.stat(os.path.dirname(path))
    except OSError:
        return False
    return (st.st_dev, st.st_ino) in identities


def _leftover_refusal(vm_name: str,
                      path: str,
                      record: DiskRecord | None,
                      workdirs: set[str]) -> str | None:
    """Why *path* may not be removed, or ``None`` when every static rule
    holds (the in-use and identity checks come last, right before the
    unlink)."""
    if record is None:
        return "it is not recorded as a disk boxman created for this vm"
    if record.role != ROLE_DATA:
        return (f"boxman attached it but did not create it (role "
                f"{record.role!r})")
    if not os.path.basename(path).startswith(f"{vm_name}_{record.name}."):
        # the attach path records <vm>_<name>.<type>; anything else was
        # not written by it
        return f"it is not named as boxman names disk {record.name!r}"
    try:
        st = os.lstat(path)
    except OSError as exc:
        return f"it could not be inspected ({exc})"
    if not stat.S_ISREG(st.st_mode):
        return "it is a symlink or not a regular file"
    if os.path.realpath(os.path.dirname(path)) not in workdirs:
        return "it is outside every cluster workdir of the project"
    return None


#: the domain carried ownership records, read into ``records``
RECORDS_PRESENT = "present"
#: the domain existed, was inventoried, and carried no ownership record —
#: one that predates them
RECORDS_NONE = "none"
#: the domain carried a record that could not be read
RECORDS_UNREADABLE = "unreadable"
#: the domain was already undefined when it was inventoried (an interrupted
#: teardown being retried): whatever record it carried went with it
RECORDS_UNKNOWN = "unknown"


@dataclass
class StorageInventory:
    """
    What a VM's teardown knows about its storage, read before undefining.

    Undefining drops the domain's XML, its ownership record and its
    snapshot metadata, and nothing the teardown decides afterwards may rest
    on a file's name alone, so everything is captured here first. Paths are
    kept as they were reported (lexical); decisions compare them resolved.
    """

    #: full name of the VM
    vm_name: str
    #: sources of its ``disk`` devices, live and persistent definition
    disk_sources: list[str]
    #: sources of every other device (CD-ROM, floppy, ...), live and
    #: persistent definition: never deleted
    media_sources: list[str]
    #: its ownership records, when :attr:`records_state` is
    #: :data:`RECORDS_PRESENT`
    records: list[DiskRecord] | None
    #: one of :data:`RECORDS_PRESENT`, :data:`RECORDS_NONE`,
    #: :data:`RECORDS_UNREADABLE`, :data:`RECORDS_UNKNOWN`
    records_state: str
    #: the backing chain of each disk and media source, as ``qemu-img``
    #: names the layers (the source first, the bottom image last); a
    #: source confirmed absent has none, and on a retry a disk source
    #: removed since maps to what is left of its chain. ``None`` when one
    #: could not be read
    chains: dict[str, list[str]] | None
    #: files under its exclusive names in its disk directories
    #: (:func:`boot_family_candidates`)
    boot_family: list[str]
    #: config-declared extra disks by name (``<vm>_<name>.<ext>``) when the
    #: config is known, ``None`` when it is not (a VM gone from conf.yml)
    legacy_disks: list[str] | None
    #: :func:`file_identities` of every file above
    identities: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: the target (``vdb``, ...) each disk source is attached at
    targets: dict[str, str] = field(default_factory=dict)
    #: the memory file each of its snapshots recorded, by snapshot name:
    #: only to name one the teardown leaves where it removes nothing
    memory_images: dict[str, str] = field(default_factory=dict)
    #: the external overlays each of its snapshots recorded, by snapshot
    #: name: with its disks and the layers below them, what decides its
    #: files in a disk_snapshot_dir. ``None`` when they are not known —
    #: not read (no disk_snapshot_dir was set), unreadable, or from an
    #: inventory saved before they were recorded: then nothing is
    #: removed from a disk_snapshot_dir, as before there were any
    overlays: dict[str, list[str]] | None = None
    #: the backing chain of each of :attr:`overlays` that is no disk or
    #: media source, as :attr:`chains` holds theirs — ``[]`` for one already
    #: gone — which a retry decides with only as they agree with it; never
    #: rewritten once known. ``None`` when one could not be read, until the
    #: first retry that reads them all saves them
    overlay_chains: dict[str, list[str]] | None = field(default_factory=dict)
    #: why a retry found :attr:`overlay_chains` otherwise than saved: once
    #: set, saved with it, and every retry keeps everything
    overlay_chains_changed: str | None = None
    #: where it was saved before undefining (see
    #: :func:`save_teardown_inventory`); not saved itself
    saved_at: str | None = None
    #: the locator that leads a retry to :attr:`saved_at` (see
    #: :func:`save_teardown_locator`); not saved itself
    locator_at: str | None = None


#: version of the saved teardown inventory's layout
TEARDOWN_INVENTORY_VERSION = 1
#: version of a teardown locator's layout
TEARDOWN_LOCATOR_VERSION = 1


def _inventory_name(vm_name: str) -> str:
    return f".boxman-teardown-{vm_name}.json"


def teardown_inventory_path(directory: str, vm_name: str) -> str:
    """Where a VM's teardown inventory is saved, beside its boot disk."""
    return os.path.join(os.path.expanduser(directory),
                        _inventory_name(vm_name))


def _write_json_atomically(path: str, data: object) -> None:
    """Write *data* to *path* as JSON: into a staging file beside it,
    flushed and synced, then renamed over it."""
    fd, staging = mkstemp_in(os.path.dirname(path),
                             prefix=".boxman-teardown-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(staging, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staging)
        raise


def save_teardown_inventory(inventory: StorageInventory,
                            directory: str) -> str:
    """
    Save *inventory* beside the VM's disks, atomically, and return the path.

    Undefining drops everything the inventory was read from — the
    domain's devices, its ownership record, its chains' context — so a
    teardown interrupted after the undefine would otherwise be retried
    blind: a CD-ROM file under the VM's own names, protected by the first
    attempt, would be taken for a boot-disk file and deleted by the second.
    Saved first, the retry decides with what the first attempt saw — its
    provenance; what the files depend on is read again (the caller's
    part) — and identity checks still guard every unlink. A retry finds it
    through its locator (:func:`save_teardown_locator`).

    Raises:
        OSError: If the file cannot be written.
    """
    path = teardown_inventory_path(directory, inventory.vm_name)
    data = {
        "version": TEARDOWN_INVENTORY_VERSION,
        "vm_name": inventory.vm_name,
        "disk_sources": inventory.disk_sources,
        "media_sources": inventory.media_sources,
        "records": (None if inventory.records is None
                    else [{key: getattr(r, key)
                           for key in sorted(_RECORD_FIELDS)}
                          for r in inventory.records]),
        "records_state": inventory.records_state,
        "chains": inventory.chains,
        "boot_family": inventory.boot_family,
        "legacy_disks": inventory.legacy_disks,
        "identities": {path_: list(identity) for path_, identity
                       in inventory.identities.items()},
        "targets": inventory.targets,
        "memory_images": inventory.memory_images,
        "overlays": inventory.overlays,
        "overlay_chains": inventory.overlay_chains,
        "overlay_chains_changed": inventory.overlay_chains_changed,
    }
    _write_json_atomically(path, data)
    return path


def _is_int(value: object) -> bool:
    # a JSON true would pass isinstance(value, int)
    return isinstance(value, int) and not isinstance(value, bool)


def _is_paths(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _is_map(value: object, valid: Callable[[object], bool]) -> bool:
    # JSON object keys are always strings
    return isinstance(value, dict) and all(valid(v) for v in value.values())


#: what an inventory keeps of each ownership record: what the teardown
#: decides on. A record's creation token and inode number (#215) are left
#: out, so inventories saved by this version and by the ones before it read
#: the same.
_RECORD_FIELDS = frozenset({"name", "target", "role", "source"})


def _is_records(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(record, dict) and set(record) == _RECORD_FIELDS
        and all(isinstance(v, str) for v in record.values())
        for record in value)


def _is_identity(value: object) -> bool:
    return (isinstance(value, list) and len(value) == 2
            and all(_is_int(v) for v in value))


def _field(data: dict, key: str, valid: Callable[[object], bool]) -> object:
    if key not in data or not valid(data[key]):
        raise ValueError(f"its {key!r} is missing or malformed")
    return data[key]


def _json_object(path: str) -> dict:
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"it holds a JSON {type(data).__name__}, not an "
                         f"object")
    return data


def _version(data: dict, expected: int) -> None:
    version = data.get("version")
    if not _is_int(version) or version != expected:
        raise ValueError(f"unknown layout {version!r}")


#: the kernel's table of the mounts this process sees
MOUNTINFO = "/proc/self/mountinfo"

_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def mount_points(mountinfo: str) -> set[str] | None:
    """
    Every mount point the *mountinfo* table lists, unescaped as the kernel
    escapes them (a space as ``\\040``), or None when it cannot be read.
    """
    try:
        # undecodable bytes as os.fsdecode() gives them in a path; only a
        # line feed ends a line, which the kernel escapes in a mount point,
        # while a carriage return and the like are left as they are
        with open(mountinfo, encoding="utf-8", errors="surrogateescape",
                  newline="") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return None
    return {_OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)),
                              line.split(" ")[4])
            for line in lines if line.count(" ") >= 4}


def load_teardown_inventory(path: str, vm_name: str) -> StorageInventory:
    """
    Read back what :func:`save_teardown_inventory` saved for *vm_name*.

    Its whole layout is checked before any of it is used — every field
    present and of its type, the records complete — so a truncated,
    hand-edited or foreign file is refused as unreadable, never half-read.

    One that is a symbolic link is refused too: boxman saves its inventory
    as a file of its own, and a retry saving what it found through a link
    would replace the link, leaving the file it names as it was. So is one
    with another name -- a hard link, or a file mounted on it: the save
    puts a new file under the one name it loaded, and the other name keeps
    the old one, for a search without the locator to find. A mount table
    that cannot be read refuses nothing.

    Raises:
        OSError, ValueError: If it cannot be read, is not a saved inventory
            of this layout, is a symbolic link, has another name, or names
            another VM.
    """
    if os.path.islink(path):
        raise ValueError("it is a symbolic link, not the file boxman saved")
    if os.stat(path).st_nlink > 1:
        raise ValueError("it has another name, a hard link, which a save "
                         "under this one would leave as it was")
    mounts = mount_points(MOUNTINFO)
    if mounts is not None and os.path.realpath(path) in mounts:
        raise ValueError("a file is mounted on it, which a save under "
                         "another name of that file would leave as it was")
    data = _json_object(path)
    _version(data, TEARDOWN_INVENTORY_VERSION)
    name = _field(data, "vm_name", lambda v: isinstance(v, str))
    if name != vm_name:
        raise ValueError(f"it is the inventory of {name!r}")
    state = data.get("records_state")
    if state not in (RECORDS_PRESENT, RECORDS_NONE, RECORDS_UNREADABLE):
        raise ValueError(f"unknown records state {state!r}")
    # records are read exactly when the domain carried them
    records = _field(data, "records", (
        _is_records if state == RECORDS_PRESENT else (lambda v: v is None)))
    chains = _field(data, "chains",
                    lambda v: v is None or _is_map(v, _is_paths))
    legacy = _field(data, "legacy_disks", lambda v: v is None or _is_paths(v))
    identities = _field(data, "identities",
                        lambda v: _is_map(v, _is_identity))
    # absent from an inventory saved before they were recorded
    memory_images = (
        _field(data, "memory_images",
               lambda v: _is_map(v, lambda p: isinstance(p, str)))
        if "memory_images" in data else {})
    # not known in one saved before they were recorded
    overlays = (_field(data, "overlays",
                       lambda v: v is None or _is_map(v, _is_paths))
                if "overlays" in data else None)
    overlay_chains = (
        _field(data, "overlay_chains",
               lambda v: v is None or _is_map(v, _is_paths))
        if "overlay_chains" in data else {})
    changed = (_field(data, "overlay_chains_changed",
                      lambda v: v is None or isinstance(v, str))
               if "overlay_chains_changed" in data else None)
    return StorageInventory(
        vm_name=vm_name,
        disk_sources=list(_field(data, "disk_sources", _is_paths)),
        media_sources=list(_field(data, "media_sources", _is_paths)),
        records=(None if records is None
                 else [DiskRecord(**record) for record in records]),
        records_state=state,
        chains=(None if chains is None
                else {k: list(v) for k, v in chains.items()}),
        boot_family=list(_field(data, "boot_family", _is_paths)),
        legacy_disks=None if legacy is None else list(legacy),
        identities={k: (v[0], v[1]) for k, v in identities.items()},
        targets=dict(_field(data, "targets",
                            lambda v: _is_map(v, lambda t: isinstance(t, str)))),
        memory_images=dict(memory_images),
        overlays=(None if overlays is None
                  else {k: list(v) for k, v in overlays.items()}),
        overlay_chains=(None if overlay_chains is None
                        else {k: list(v) for k, v in overlay_chains.items()}),
        overlay_chains_changed=changed,
        saved_at=path,
    )


def teardown_locator_path(locator_dir: str, vm_name: str) -> str:
    """Where the locator of *vm_name*'s saved teardown inventory is kept:
    in *locator_dir* (under boxman's per-user state dir), keyed by the full
    vm name alone."""
    return os.path.join(os.path.expanduser(locator_dir), f"{vm_name}.json")


def save_teardown_locator(locator: str, vm_name: str,
                          inventory_path: str) -> None:
    """
    Record at *locator*, atomically, that *vm_name*'s teardown inventory is
    saved at *inventory_path*.

    The inventory stays beside the VM's disks, which it describes, and that
    can be any directory the boot disk was in. The locator is what a retry
    looks up — by the vm name alone — so it finds the inventory wherever
    that was, whatever directories the retry itself would search.

    *inventory_path* is recorded as given, joined onto the working directory
    when relative, with nothing folded: ``os.path.abspath`` folds
    ``link/..`` as text, into what can be another directory than the one
    the inventory was written to (#260).

    Raises:
        OSError: If it cannot be written.
    """
    os.makedirs(os.path.dirname(locator), exist_ok=True)
    _write_json_atomically(locator, {
        "version": TEARDOWN_LOCATOR_VERSION,
        "vm_name": vm_name,
        "inventory": (inventory_path if os.path.isabs(inventory_path)
                      else os.path.join(os.getcwd(), inventory_path)),
    })


def read_teardown_locator(locator: str, vm_name: str) -> str:
    """
    The inventory path *locator* records for *vm_name*.

    Only an absolute path to a file named as *vm_name*'s teardown inventory
    is accepted: what a locator names is read, and removed once it has
    done its job.

    Raises:
        OSError, ValueError: If it cannot be read or is malformed.
    """
    data = _json_object(locator)
    _version(data, TEARDOWN_LOCATOR_VERSION)
    name = _field(data, "vm_name", lambda v: isinstance(v, str))
    if name != vm_name:
        raise ValueError(f"it is the locator of {name!r}")
    inventory = _field(data, "inventory", lambda v: isinstance(v, str))
    if (not os.path.isabs(inventory)
            or os.path.basename(inventory) != _inventory_name(vm_name)):
        raise ValueError(f"it does not name a teardown inventory of "
                         f"{vm_name}: {inventory!r}")
    return inventory


def entry_exists(path: str) -> bool:
    """
    Whether *path* has a directory entry (a dangling symlink counts).

    Only ``FileNotFoundError`` is absence. ``os.path.lexists`` answers
    ``False`` for an entry that cannot be looked up at all — a directory on
    the way that cannot be searched, a component that is not a directory, a
    symlink loop — and a retried teardown that took an unsearchable locator
    directory for "no locator" fell back to deleting by name what the
    inventory protected (#208).

    Raises:
        OSError: If it cannot be looked up for any other reason, for the
            caller to fail closed on.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _may_exist(path: str) -> bool:
    """``False`` only when *path* is confirmed absent (:func:`entry_exists`):
    one that cannot be looked up may still be there, so it is kept."""
    try:
        return entry_exists(path)
    except OSError:
        return True


@dataclass
class StorageOutcome:
    """What :func:`remove_vm_storage` removed and what it kept, and why."""

    removed: list[str] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)


def _regular_file_refusal(path: str) -> str | None:
    """Why *path* is not a regular file that may be unlinked, if it is not."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        return f"it could not be inspected ({exc})"
    if not stat.S_ISREG(st.st_mode):
        return "it is a symlink or not a regular file"
    return None


_MEDIA = "it is a CD-ROM or other media source of the vm"
_UNDER_MEDIA = "it backs a CD-ROM or other media source of the vm"
_OUTSIDE = "it is outside every cluster workdir of the project"
_CHAINS_UNREAD = ("the backing chains of the vm's disks could not be read, "
                  "so whether a CD-ROM is built on it cannot be told")
_NOT_AN_ARTIFACT = (
    "the vm was already undefined and no saved teardown inventory was "
    "found, so only boxman's own qcow2 images and memory files are removed "
    "by name")

#: the first bytes of every qcow2 image
_QCOW2_MAGIC = b"QFI\xfb"


def _boxman_artifact_refusal(vm_name: str, path: str) -> str | None:
    """
    Why *path* is not one of the artifact types boxman writes under the VM's
    names — a qcow2 image ``<vm>.*`` (told by its magic, not its name) or a
    memory file ``<vm>_snapshot_*.raw`` / ``.raw.zst`` — if it is not.
    The rule for a domain undefined without a saved inventory: nothing is
    left to say what else a file under those names might be.
    """
    name = os.path.basename(path)
    if name.startswith(f"{vm_name}_snapshot_"):
        return None if name.endswith((".raw", ".raw.zst")) else _NOT_AN_ARTIFACT
    try:
        with open(path, "rb") as fh:
            magic = fh.read(len(_QCOW2_MAGIC))
    except OSError as exc:
        return (f"it could not be read to tell whether it is a qcow2 image "
                f"({exc})")
    return None if magic == _QCOW2_MAGIC else _NOT_AN_ARTIFACT

_WHY_NO_RECORD = {
    RECORDS_NONE: "the domain carries no record of which disks boxman "
                  "created",
    RECORDS_UNREADABLE: "its disk ownership record could not be read",
    RECORDS_UNKNOWN: "the vm was already undefined when this teardown "
                     "inventoried it, so whether boxman created it can no "
                     "longer be told",
}


class FilesInUse(dict):
    """
    What other domains use (``LibVirtSession.disk_paths_in_use``): the
    mapping itself is path -> domain — resolved where that could be done
    strictly, else as the domain named it — and :attr:`identities` maps
    each of those files' identity, ``(st_dev, st_ino)``, to its domain — so
    a file used under a name no resolved path unifies with the teardown's
    (a bind mount, a hard link, a path boxman cannot resolve) is still
    recognised. A consumer consults both.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        #: (st_dev, st_ino) -> the domain that uses that file
        self.identities: dict[tuple[int, int], str] = {}


class _Identities:
    """
    Which file each path names, as ``(st_dev, st_ino)``: one file under two
    names a resolved path cannot unify — a bind mount, a hard link, a
    symlink boxman cannot resolve — is still one file (#208).

    A candidate's identity is the one captured before undefining
    (``StorageInventory.identities``, lstat: candidates are regular files,
    and a symlink is refused anyway) — the identity its removal checks. A
    protected path is read now, host-side, with ``os.stat`` (links
    followed): one that names no file matches nothing by identity, and one
    that cannot be read at all is kept in :attr:`unreadable`, which keeps
    every candidate (:func:`remove_vm_storage`).
    """

    def __init__(self, captured: dict[str, tuple[int, int]],
                 protected: Iterable[str]) -> None:
        self._captured = captured
        self._current: dict[str, tuple[int, int]] = {}
        #: protected path -> why its identity could not be read
        self.unreadable: dict[str, OSError] = {}
        seen: set[str] = set()
        for path in protected:
            if path in seen:
                continue
            seen.add(path)
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                self.unreadable[path] = exc
                continue
            self._current[path] = (st.st_dev, st.st_ino)

    def of(self, path: str) -> set[tuple[int, int]]:
        """Every identity known for *path*: captured, read now, or both."""
        return {identity for identity in (self._captured.get(path),
                                          self._current.get(path))
                if identity is not None}


class _FileSet:
    """Files, matched by resolved path or by identity
    (:class:`_Identities`)."""

    def __init__(self, identities: _Identities, paths: Iterable[str]) -> None:
        self._identities = identities
        self._resolved: set[str] = set()
        self._ids: set[tuple[int, int]] = set()
        for path in paths:
            self._resolved.add(os.path.realpath(path))
            self._ids.update(identities.of(path))

    def __contains__(self, path: object) -> bool:
        return (os.path.realpath(path) in self._resolved
                or not self._ids.isdisjoint(self._identities.of(path)))


class _Admission:
    """Which referenced files may be removed, and why each other one is
    kept. Keyed by resolved path, and matched by identity too
    (:class:`_Identities`): the refusal of a file under one name blocks,
    and withdraws, its admission under another. A refusal always wins over
    an admission, and identity only ever adds refusals."""

    def __init__(self, identities: _Identities | None = None) -> None:
        self._identities = identities or _Identities({}, ())
        #: resolved path -> the path to unlink it by
        self.admitted: dict[str, str] = {}
        #: resolved path -> (path, why it is kept)
        self.refused: dict[str, tuple[str, str]] = {}
        self._refused_ids: set[tuple[int, int]] = set()

    def _same_file(self, path: str) -> list[str]:
        """The admitted entries (resolved keys) that are the file *path*
        names, by resolved path or identity."""
        resolved = os.path.realpath(path)
        ids = self._identities.of(path)
        return [key for key, admitted in self.admitted.items()
                if key == resolved
                or not ids.isdisjoint(self._identities.of(admitted))]

    def admit(self, path: str) -> None:
        resolved = os.path.realpath(path)
        if (resolved not in self.refused
                and self._refused_ids.isdisjoint(self._identities.of(path))):
            self.admitted.setdefault(resolved, path)

    def refuse(self, path: str, reason: str) -> list[str]:
        """Keep the file *path* names, under every name it was admitted by;
        returns those names (withdrawn from the admitted)."""
        withdrawn = [self.admitted.pop(key) for key in self._same_file(path)]
        self._refused_ids.update(self._identities.of(path))
        self.refused.setdefault(os.path.realpath(path), (path, reason))
        return withdrawn

    def is_admitted(self, path: str) -> bool:
        """Admitted under this resolved path. A chain counts as whole only
        by the names ``qemu-img`` gave its layers, as before identities."""
        return os.path.realpath(path) in self.admitted

    def is_admitted_as(self, path: str) -> bool:
        """Admitted under this name, or under another name of its file."""
        return bool(self._same_file(path))


def _refuse_incomplete_chains(chains: dict[str, list[str]],
                              admission: _Admission) -> None:
    """
    Keep every layer of every chain that is not removable as a whole.

    A chain is removed only when each of its layers is admitted and none is
    a symlink — checked on the path ``qemu-img`` named, before resolving
    it, since a symlinked layer resolves to a file that looks removable.
    Otherwise every layer of it is kept: removing a layer that a kept disk
    above it (or a kept image beside it in the same chain) still needs would
    leave that disk broken. Repeated until stable, as chains share layers
    and a live and a persistent definition can each hold a different head
    over the same base.
    """
    changed = True
    while changed:
        changed = False
        for head, layers in chains.items():
            problem = None
            for layer in layers:
                if os.path.islink(layer):
                    problem = f"its backing chain goes through the symlink {layer}"
                    break
                if not admission.is_admitted(layer):
                    problem = (
                        f"it is in the backing chain of {head}, which is kept"
                        if layer == head else
                        f"it is in the backing chain of {head}, and {layer} "
                        f"in that chain is kept")
                    break
            if problem is None:
                continue
            for layer in layers:
                # the layer's file under any name
                if admission.is_admitted_as(layer):
                    admission.refuse(layer, problem)
                    changed = True


def remove_vm_storage(
    inventory: StorageInventory,
    workdirs: Iterable[str],
    paths_in_use: Callable[[], dict[str, str] | None],
    memory_dirs: Iterable[str] = (),
    recorded_in_use: Callable[[], dict[str, str] | None] | None = None,
    overlay_dirs: Iterable[str] = (),
) -> StorageOutcome:
    """
    Remove the storage an undefined VM owns — and nothing else.

    The one storage-removal routine of every VM teardown (``deprovision``,
    ``destroy``, ``provision --force``, ``up --force`` and ``update``
    removing a VM). libvirt deletes nothing: ``undefine
    --remove-all-storage`` zero-filled and deleted every pool-listed source
    of the domain, CD-ROM media and adopted disks included (#208).

    Files are first admitted one by one, from the *inventory* taken before
    undefining; anything a rule does not admit is kept:

    1. **The boot-disk family** — files under the VM's exclusive names in
       its disk directories (:func:`boot_family_candidates`): its boot disk,
       its overlays and its memory-snapshot files. Extra-disk files
       (attached, recorded, config-declared, or a layer under one) are
       never decided by name. A file found in a cluster's
       ``disk_snapshot_dir`` (*overlay_dirs*) is left to 1c, a memory
       image found in a memory_snapshot_dir to 1b.
    1b. **The memory images its snapshots recorded**
       (``StorageInventory.memory_images``), each named exactly as
       :func:`memory_image_name` names that snapshot's (or with the
       ``.zst`` compression adds), in a cluster workdir or one of the
       clusters' ``memory_snapshot_dir`` (*memory_dirs*). Nothing in a
       memory_snapshot_dir is decided by name: every cluster's is one, and
       another VM's full name can begin with this one's. Its directory is
       told by identity, links followed by the filesystem; when it, or a
       directory it might be, cannot be looked up, the image is kept. One
       recorded anywhere else is not boxman's to remove (the caller names
       it).
    1c. **The boot disk's overlays in a disk_snapshot_dir** — what libvirt
       names as the VM's there: its disk sources, the layers of their
       chains, and the overlays its snapshots recorded
       (``StorageInventory.overlays``), under ``<vm>.``. Nothing there is
       decided by name either, for the same reason, and a listing of it
       admits nothing. Its directory is told by identity, as in 1b, which
       sees a bind mount for the directory it is. With those overlays not
       known (``None``) nothing there is admitted, a data disk's chain (2)
       included: one only a snapshot records can be built on any of them.
    2. **Extra disks recorded with role ``data``** at exactly their
       attached source, by the rules of :func:`_leftover_refusal`; and a
       recorded disk an external snapshot moved behind overlays, together
       with its whole chain, when :func:`_owned_snapshot_chain` proves the
       chain the VM's own — its overlays beside it or in one of
       *overlay_dirs*.
    3. **A legacy domain** — one inventoried with no ownership record at
       all: when the config is known (*legacy_disks*), its config-declared
       disks, by name. A VM gone from conf.yml keeps them, and so does a
       domain already undefined when inventoried (a retried teardown):
       its record, if it had one, is gone.

    Every admitted file must be a regular file, not a symlink, inside one
    of the cluster *workdirs* once symlinks are resolved (a recorded memory
    image, see 1b, one of *memory_dirs* too, and an overlay, see 1c and 2,
    one of *overlay_dirs*), and not a CD-ROM (or other media) source of
    either definition.

    Every protection that compares files matches them by resolved path and
    by identity (:class:`_Identities`): the media and what they are built
    on, the extra disks and their chains, the chain-whole rule, what other
    domains use (:class:`FilesInUse`), and a refusal under one name against
    an admission under another. A protected path whose identity cannot be
    read, other than for naming no file, keeps every candidate.

    Then, across the whole inventory: a backing chain is removed only as a
    whole (:func:`_refuse_incomplete_chains`), and an unreadable chain
    keeps everything; nothing another domain uses directly or as a backing
    file is removed (*paths_in_use* is asked once, and ``None`` keeps
    everything); and no memory image (:func:`is_memory_image`) — with
    *overlay_dirs*, no file at all — that a snapshot of another domain
    records: neither its name nor this VM's own record makes it this VM's
    alone, as two VMs' names can give one file name (*recorded_in_use*,
    asked once, only when one would go; ``None`` keeps them all). What remains is
    removed heads first, each through
    :func:`remove_if_unchanged` against the identity taken before
    undefining; a layer replaced since keeps everything below it.

    Returns:
        The files removed, and ``(path, reason)`` for every file kept.
    """
    vm = inventory.vm_name
    outcome = StorageOutcome()
    real = os.path.realpath
    workdirs = list(workdirs)
    real_workdirs = {real(os.path.expanduser(w)) for w in workdirs}
    chains = inventory.chains
    legacy = list(inventory.legacy_disks or ())
    records = inventory.records or []
    # every path a protection compares candidates with, read now
    identities = _Identities(inventory.identities, [
        *inventory.media_sources, *inventory.disk_sources,
        *(record.source for record in records), *legacy,
        *(layer for layers in (chains or {}).values() for layer in layers),
        *(layer for layers in (inventory.overlay_chains or {}).values()
          for layer in layers)])
    admission = _Admission(identities)
    # a media source and every image under it: a qcow2 CD-ROM can be built
    # on a file the vm also attaches as a disk. Protected before anything is
    # admitted, on every route.
    media = _FileSet(identities, inventory.media_sources)
    under_media = _FileSet(identities, [
        layer for source in inventory.media_sources
        for layer in (chains or {}).get(source, ())])

    def media_refusal(path: str) -> str | None:
        if path in media:
            return _MEDIA
        if path in under_media:
            return _UNDER_MEDIA
        return None

    def unique(paths):
        seen, out = set(), []
        for path in paths:
            if real(path) not in seen:
                seen.add(real(path))
                out.append(path)
        return out

    overlay_dirs = list(overlay_dirs)
    overlay_ids, unreadable_overlay_dirs = _directory_identities(overlay_dirs)
    # what its snapshots recorded is not known: nothing in a
    # disk_snapshot_dir is admitted, as before there were any (1c, 2)
    overlays_known = inventory.overlays is not None
    memory_ids, _unreadable = _directory_identities(memory_dirs)

    def static_refusal(path: str) -> str | None:
        reason = media_refusal(path)
        if reason:
            return reason
        reason = _regular_file_refusal(path)
        if reason:
            return reason
        if real(os.path.dirname(path)) not in real_workdirs:
            return _OUTSIDE
        return None

    def in_overlay_dir(path: str) -> tuple[bool, str | None]:
        """Whether *path* is in a disk_snapshot_dir, by identity — or why
        that cannot be told."""
        try:
            st = os.stat(os.path.dirname(path))
        except OSError as exc:
            return False, (f"its directory could not be looked up ({exc}), "
                           f"so whether it is in a cluster workdir or "
                           f"disk_snapshot_dir cannot be told")
        if (st.st_dev, st.st_ino) in overlay_ids:
            return True, None
        if unreadable_overlay_dirs:
            directory, exc = min(unreadable_overlay_dirs.items())
            return False, (f"whether it is in a cluster workdir or "
                           f"disk_snapshot_dir cannot be told: {directory} "
                           f"could not be looked up ({exc})")
        return False, None

    attached_extras = unique(
        path for path in inventory.disk_sources
        if not os.path.basename(path).startswith(f"{vm}."))

    # extra-disk files are never decided by name
    extra_related = _FileSet(identities, [
        *attached_extras, *(record.source for record in records), *legacy,
        *(layer for source, layers in (chains or {}).items()
          if source not in inventory.media_sources
          and not os.path.basename(source).startswith(f"{vm}.")
          for layer in layers)])

    # -- 1. the boot-disk family ------------------------------------------
    deferred = []         # left to what the snapshots recorded (1b)
    for path in inventory.boot_family:
        if path in extra_related and media_refusal(path) is None:
            continue
        if (is_memory_image(vm, path)
                and real(os.path.dirname(path)) not in real_workdirs
                and _directory_is_one_of(path, memory_ids)):
            # what its snapshots recorded decides it (1b), but every refusal
            # other than being outside the workdirs comes first, as on
            # every route: a CD-ROM is kept, a symlink keeps the file it
            # names, unread chains keep what a CD-ROM may be built on
            reason = (media_refusal(path) or _regular_file_refusal(path)
                      or (_CHAINS_UNREAD if chains is None else None))
            if reason:
                admission.refuse(path, reason)
            else:
                deferred.append(path)
            continue
        if (overlay_dirs and overlays_known
                and real(os.path.dirname(path)) not in real_workdirs
                and any(in_overlay_dir(path))):
            # what libvirt names as the vm's decides it (1c), after every
            # refusal other than being outside the workdirs, as for a
            # memory image above
            reason = (media_refusal(path) or _regular_file_refusal(path)
                      or (_CHAINS_UNREAD if chains is None else None))
            if reason:
                admission.refuse(path, reason)
            continue
        reason = static_refusal(path)
        if reason is None and inventory.records_state == RECORDS_UNKNOWN:
            reason = _boxman_artifact_refusal(vm, path)
        if reason:
            admission.refuse(path, reason)
        else:
            admission.admit(path)

    # -- 1b. the memory images its snapshots recorded --------------------
    managed, unreadable_dirs = _directory_identities([*workdirs,
                                                      *memory_dirs])
    for snapshot, recorded in sorted(inventory.memory_images.items()):
        if os.path.basename(recorded) != memory_image_name(vm, snapshot):
            continue      # not boxman's (named by the caller)
        for path in (recorded, f"{recorded}.zst"):
            if not _may_exist(path) or path in extra_related:
                continue
            try:
                st = os.stat(os.path.dirname(path))
            except OSError as exc:
                admission.refuse(path, (
                    f"its directory could not be looked up ({exc}), so "
                    f"whether it is in a cluster workdir or "
                    f"memory_snapshot_dir cannot be told"))
                continue
            if (st.st_dev, st.st_ino) not in managed:
                if unreadable_dirs:
                    directory, exc = min(unreadable_dirs.items())
                    admission.refuse(path, (
                        f"whether it is in a cluster workdir or "
                        f"memory_snapshot_dir cannot be told: {directory} "
                        f"could not be looked up ({exc})"))
                continue  # outside all of them (named by the caller)
            reason = media_refusal(path) or _regular_file_refusal(path)
            if reason:
                admission.refuse(path, reason)
            else:
                admission.admit(path)
    # one left to them that they did not record is kept, as outside the
    # workdirs, as it was before it was left to them: refused, so that
    # every protection applied to what is admitted still holds it, and the
    # saved inventory stays
    for path in deferred:
        if not admission.is_admitted_as(path):
            admission.refuse(path, _OUTSIDE)

    # -- 1c. the boot disk's overlays in a disk_snapshot_dir --------------
    named_by_libvirt = unique([
        *inventory.disk_sources,
        *(layer for layers in (chains or {}).values() for layer in layers),
        *(path for paths in (inventory.overlays or {}).values()
          for path in paths)])
    for path in named_by_libvirt if overlay_dirs and overlays_known else ():
        if (not os.path.basename(path).startswith(f"{vm}.")
                or real(os.path.dirname(path)) in real_workdirs
                or not _may_exist(path)
                or (path in extra_related and media_refusal(path) is None)):
            continue      # decided by another rule, or nothing to decide
        inside, reason = in_overlay_dir(path)
        if not (inside or reason):
            continue      # outside all of them, as in 1
        reason = reason or media_refusal(path) or _regular_file_refusal(path)
        if reason:
            admission.refuse(path, reason)
        else:
            admission.admit(path)

    # -- 2./3. extra disks -------------------------------------------------
    state = inventory.records_state
    if state == RECORDS_PRESENT:
        for record in records:
            if (record.source in inventory.disk_sources
                    or not _may_exist(record.source)):
                continue
            chain, reason = _owned_snapshot_chain(
                inventory, record, real_workdirs, media,
                overlay_ids if overlays_known else frozenset())
            if chain is None:
                admission.refuse(record.source, (
                    f"boxman created it, but it was no longer attached "
                    f"where it was recorded (an external snapshot moves a "
                    f"disk to an overlay) and {reason}; remove it and its "
                    f"overlays by hand"))
            else:
                for layer in chain:
                    admission.admit(layer)
        by_source = {record.source: record for record in records}
        for path in attached_extras:
            if not _may_exist(path) or admission.is_admitted(path):
                continue
            reason = (media_refusal(path)
                      or _leftover_refusal(vm, path, by_source.get(path),
                                           real_workdirs))
            if reason:
                admission.refuse(path, reason)
            else:
                admission.admit(path)
    elif state == RECORDS_NONE and inventory.legacy_disks is not None:
        for path in legacy:
            if not _may_exist(path):
                continue
            reason = static_refusal(path)
            if reason:
                admission.refuse(path, reason)
            else:
                admission.admit(path)
        declared = {real(path) for path in legacy}
        for path in attached_extras:
            if _may_exist(path) and real(path) not in declared:
                admission.refuse(path, _WHY_NO_RECORD[RECORDS_NONE])
    else:
        for path in unique([*attached_extras, *legacy]):
            if _may_exist(path):
                admission.refuse(path, _WHY_NO_RECORD[state])

    # -- dependencies across the whole inventory ---------------------------
    # an overlay only a snapshot records is held up by its chain as a disk
    # is by its own: kept — never admitted, as a data disk's, or refused —
    # it keeps what it is built on, and so does what is left of one a first
    # attempt removed
    overlay_chains = inventory.overlay_chains
    if chains is not None and overlay_chains is not None:
        chains = {**overlay_chains, **chains}
    if chains is None or overlay_chains is None:
        unread = ("of the vm's disks could not be read" if chains is None
                  else "of the overlays its snapshots recorded could not be "
                       "read, or changed since the teardown began")
        for path in list(admission.admitted.values()):
            admission.refuse(path, (
                f"the backing chains {unread}, so what depends on it cannot "
                f"be told"))
    else:
        _refuse_incomplete_chains(chains, admission)
    if identities.unreadable:
        unreadable, exc = min(identities.unreadable.items())
        for path in list(admission.admitted.values()):
            admission.refuse(path, (
                f"the identity of {unreadable} could not be read ({exc}), so "
                f"whether that is this file cannot be told — make it "
                f"accessible"))

    # -- in use by another domain ------------------------------------------
    if admission.admitted:
        in_use = paths_in_use()
        used_ids = getattr(in_use, "identities", {})
        for resolved, path in list(admission.admitted.items()):
            if resolved not in admission.admitted:
                continue     # withdrawn meanwhile, under another name
            if in_use is None:
                admission.refuse(path, "could not check whether another "
                                       "domain uses it")
                continue
            domain = in_use.get(resolved) or next(
                (used_ids[identity]
                 for identity in sorted(identities.of(path))
                 if identity in used_ids), None)
            if domain:
                admission.refuse(path, f"domain {domain} uses it")
        if chains:
            _refuse_incomplete_chains(chains, admission)

    # -- recorded by another domain's snapshot -----------------------------
    # with a disk_snapshot_dir anywhere, everything: overlays are named
    # libvirt's way, after the source, so a layer under this VM's name — in
    # its chain, in another cluster's workdir — can be another's snapshot's
    snapshot_named = [(resolved, path)
                      for resolved, path in admission.admitted.items()
                      if overlay_dirs or is_memory_image(vm, path)]
    if snapshot_named and recorded_in_use is not None:
        recorded = recorded_in_use()
        recorded_ids = getattr(recorded, "identities", {})
        for resolved, path in snapshot_named:
            if resolved not in admission.admitted:
                continue     # withdrawn meanwhile, under another name
            if recorded is None:
                admission.refuse(path, (
                    "whether a snapshot of another domain records it could "
                    "not be checked"))
                continue
            domain = recorded.get(resolved) or recorded.get(path) or next(
                (recorded_ids[identity]
                 for identity in sorted(identities.of(path))
                 if identity in recorded_ids), None)
            if domain:
                admission.refuse(path, (
                    f"a snapshot of domain {domain} records it"))
        if chains:
            _refuse_incomplete_chains(chains, admission)

    # -- removal, heads first ------------------------------------------------
    # (ordered by resolved path: a layer named otherwise than the file
    # admitted is never admitted, so the chain-whole rule kept that chain)
    resolved_chains = [[real(layer) for layer in layers]
                       for layers in (chains or {}).values()]
    depth: dict[str, int] = {}
    for layers in resolved_chains:
        for index, layer in enumerate(layers):
            depth[layer] = max(depth.get(layer, 0), index)
    unlinked: set[str] = set()

    def keep_below(kept: list[str]) -> None:
        """
        A kept layer keeps everything below it, under every name — applied
        before anything else is unlinked. For each chain holding a file
        just kept (by resolved path or identity; never a layer already
        unlinked, which holds nothing up), every admitted layer below it is
        refused; each refusal withdraws the names that file was admitted
        by, which are followed in turn.
        """
        work, seen = list(kept), set()
        while work:
            path = work.pop()
            if path in seen:
                continue
            seen.add(path)
            resolved, ids = real(path), identities.of(path)
            for layers in (chains or {}).values():
                for index, layer in enumerate(layers):
                    if real(layer) in unlinked or not (
                            real(layer) == resolved
                            or not ids.isdisjoint(identities.of(layer))):
                        continue
                    for below in layers[index + 1:]:
                        if admission.is_admitted_as(below):
                            work.extend(admission.refuse(
                                below, "a layer above it in its backing "
                                       "chain is kept"))

    for resolved in sorted(admission.admitted, key=lambda r: depth.get(r, 0)):
        path = admission.admitted.get(resolved)
        if path is None:
            continue     # kept meanwhile, with a layer above it
        result, where = remove_if_unchanged(
            path, inventory.identities.get(path))
        if result == "removed":
            log.info(f"removed {path} (vm {vm})")
            outcome.removed.append(path)
            del admission.admitted[resolved]
            unlinked.add(resolved)
        elif result == "gone":
            del admission.admitted[resolved]
            unlinked.add(resolved)
        else:
            withdrawn = admission.refuse(
                path, "it was replaced after the vm was inspected"
                if result == "restored" else _stranded(where))
            keep_below([path, *withdrawn])

    # the kept list decides whether the saved inventory and its locator go
    # (_teardown_vm), so only a file confirmed gone leaves it
    outcome.kept = [(path, reason)
                    for path, reason in admission.refused.values()
                    if _may_exist(path)]
    return outcome


def _stranded(where: str | None) -> str:
    return (f"it was replaced after the vm was inspected, and the name was "
            f"taken again before the replacement could be put back; the "
            f"replacement is at {where}")


def _owned_snapshot_chain(inventory: StorageInventory,
                          record: DiskRecord,
                          workdirs: set[str],
                          media: _FileSet | None = None,
                          overlay_dirs: set[tuple[int, int]] = frozenset(),
                          ) -> tuple[list[str] | None, str | None]:
    """
    The snapshot chain of a ``data`` disk whose recorded source an external
    snapshot moved behind overlays — when it is provably this VM's own.

    The record names only the base; libvirt names each overlay after the
    source it was taken of (``<vm>_<name>.qcow2`` gives
    ``<vm>_<name>.<snapshot>``), and the snapshot metadata that listed the
    overlays goes with the domain. The chain is taken as the VM's own only
    when all of these hold, each checked against the inventory read before
    undefining:

    - the record's role is ``data`` and its base is in a cluster workdir;
    - exactly one attached disk has the base at the *bottom* of its
      backing chain (nothing below it: boxman creates data disks
      standalone), and that disk is attached at the record's target;
    - every layer, as ``qemu-img`` named it, is a regular file and not a
      symlink, in the base's directory or in a cluster's
      ``disk_snapshot_dir`` (*overlay_dirs*, their identities), named
      ``<vm>_<name>.<suffix>``, and not a CD-ROM or other media source of
      the vm.

    Whether it may go as a whole, and whether another domain uses a layer,
    is decided by :func:`remove_vm_storage` across the whole inventory.

    Returns:
        ``(chain, None)`` — head first, base last — or ``(None, reason)``.
    """
    vm = inventory.vm_name
    real = os.path.realpath
    if record.role != ROLE_DATA:
        return None, (f"boxman attached it but did not create it (role "
                      f"{record.role!r})")
    if inventory.chains is None:
        return None, ("the backing chains of the vm's disks could not be "
                      "read")
    base = real(record.source)
    heads = [source for source, chain in inventory.chains.items()
             if chain and real(chain[-1]) == base]
    if len(heads) != 1:
        return None, ("no single attached disk has it at the bottom of its "
                      "backing chain")
    head = heads[0]
    if inventory.targets.get(head) != record.target:
        return None, (f"the disk built on it is attached at "
                      f"{inventory.targets.get(head)}, not at "
                      f"{record.target} where it was recorded")
    directory = real(os.path.dirname(record.source))
    if directory not in workdirs:
        return None, _OUTSIDE
    if media is None:
        media = _FileSet(_Identities({}, ()), inventory.media_sources)
    stem = f"{vm}_{record.name}."
    chain = inventory.chains[head]
    for layer in chain:
        if os.path.islink(layer):
            return None, f"{layer} in its chain is a symlink"
        if not os.path.basename(layer).startswith(stem):
            return None, (f"{layer} in its chain is not named for disk "
                          f"{record.name!r}")
        if (real(os.path.dirname(layer)) != directory
                and not _directory_is_one_of(layer, overlay_dirs)):
            return None, f"{layer} in its chain is in another directory"
        if layer in media:
            return None, f"{layer} in its chain is a media source of the vm"
        reason = _regular_file_refusal(layer)
        if reason:
            return None, f"{layer} in its chain: {reason}"
    return chain, None


def remove_if_unchanged(path: str,
                        identity: tuple[int, int] | None,
                        ) -> tuple[str, str | None]:
    """
    Unlink *path* only if it is still the file *identity* names.

    Comparing a stat of the pathname and then unlinking the pathname
    deletes whatever replaced the file in between. Instead the entry is
    moved into a private directory beside it — the same filesystem, so the
    rename is atomic — and only that moved entry is checked and unlinked; a
    writer that recreates the name meanwhile is never touched. A moved
    entry that is not the attached file goes back under its name with a
    link, which fails rather than replaces, so a name taken again in the
    meantime is never overwritten.

    Returns:
        ``(outcome, where)``: ``"removed"``; ``"restored"`` (it was not the
        attached file and is back under its name); ``"stranded"`` (it was
        not the attached file and the name was taken again, so it stays at
        *where*, inside the private directory); or ``"gone"`` (someone
        removed it first).

    Raises:
        OSError: If it could not be moved aside for any other reason, the
            private directory having gone too among them; it is left as it
            was.
    """
    directory, name = os.path.split(path)
    # mkdtemp makes it under *directory* as given, but since Python 3.12
    # returns its name made absolute, and that folds ``link/..`` as text
    # into what can be another directory: only the last component is kept
    # (#257)
    private = os.path.join(directory, os.path.basename(
        tempfile.mkdtemp(prefix=".boxman-removing-", dir=directory)))
    moved = os.path.join(private, name)
    try:
        os.rename(path, moved)
    except OSError as exc:
        # Nothing was moved, so the private directory is still empty: a
        # plain rmdir (never a recursive delete) removes it, and a failure
        # there must not hide why the rename failed.
        with contextlib.suppress(OSError):
            os.rmdir(private)
        # what is missing can be the private directory: only a file
        # confirmed absent is gone
        if isinstance(exc, FileNotFoundError) and not _may_exist(path):
            return "gone", None
        raise
    st = os.lstat(moved)
    if (st.st_dev, st.st_ino) == identity:
        os.unlink(moved)
        os.rmdir(private)
        return "removed", None
    try:
        os.link(moved, path, follow_symlinks=False)
    except OSError:
        return "stranded", moved
    os.unlink(moved)
    os.rmdir(private)
    return "restored", None
