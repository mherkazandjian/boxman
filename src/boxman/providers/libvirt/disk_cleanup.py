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
import dataclasses
import glob as _glob
import json
import os
import stat
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from boxman import log

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
    #: (:func:`boot_family_files`)
    boot_family: list[str]
    #: config-declared extra disks by name (``<vm>_<name>.<ext>``) when the
    #: config is known, ``None`` when it is not (a VM gone from conf.yml)
    legacy_disks: list[str] | None
    #: :func:`file_identities` of every file above
    identities: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: the target (``vdb``, ...) each disk source is attached at
    targets: dict[str, str] = field(default_factory=dict)
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
    fd, staging = tempfile.mkstemp(prefix=".boxman-teardown-",
                                   suffix=".tmp", dir=os.path.dirname(path))
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
                    else [dataclasses.asdict(r) for r in inventory.records]),
        "records_state": inventory.records_state,
        "chains": inventory.chains,
        "boot_family": inventory.boot_family,
        "legacy_disks": inventory.legacy_disks,
        "identities": {path_: list(identity) for path_, identity
                       in inventory.identities.items()},
        "targets": inventory.targets,
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


_RECORD_FIELDS = frozenset(f.name for f in dataclasses.fields(DiskRecord))


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


def load_teardown_inventory(path: str, vm_name: str) -> StorageInventory:
    """
    Read back what :func:`save_teardown_inventory` saved for *vm_name*.

    Its whole layout is checked before any of it is used — every field
    present and of its type, the records complete — so a truncated,
    hand-edited or foreign file is refused as unreadable, never half-read.

    Raises:
        OSError, ValueError: If it cannot be read, is not a saved inventory
            of this layout, or names another VM.
    """
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

    Raises:
        OSError: If it cannot be written.
    """
    os.makedirs(os.path.dirname(locator), exist_ok=True)
    _write_json_atomically(locator, {
        "version": TEARDOWN_LOCATOR_VERSION,
        "vm_name": vm_name,
        "inventory": os.path.abspath(inventory_path),
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


class _Admission:
    """Which referenced files may be removed, and why each other one is
    kept. Keyed by resolved path; a refusal always wins over an
    admission."""

    def __init__(self) -> None:
        #: resolved path -> the path to unlink it by
        self.admitted: dict[str, str] = {}
        #: resolved path -> (path, why it is kept)
        self.refused: dict[str, tuple[str, str]] = {}

    def admit(self, path: str) -> None:
        resolved = os.path.realpath(path)
        if resolved not in self.refused:
            self.admitted.setdefault(resolved, path)

    def refuse(self, path: str, reason: str) -> None:
        resolved = os.path.realpath(path)
        self.admitted.pop(resolved, None)
        self.refused.setdefault(resolved, (path, reason))

    def is_admitted(self, path: str) -> bool:
        return os.path.realpath(path) in self.admitted


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
                if admission.is_admitted(layer):
                    admission.refuse(layer, problem)
                    changed = True


def remove_vm_storage(
    inventory: StorageInventory,
    workdirs: Iterable[str],
    paths_in_use: Callable[[], dict[str, str] | None],
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
       its disk directories (:func:`boot_family_files`): its boot disk,
       its overlays and its memory-snapshot files. Extra-disk files
       (attached, recorded, config-declared, or a layer under one) are
       never decided by name.
    2. **Extra disks recorded with role ``data``** at exactly their
       attached source, by the rules of :func:`_leftover_refusal`; and a
       recorded disk an external snapshot moved behind overlays, together
       with its whole chain, when :func:`_owned_snapshot_chain` proves the
       chain the VM's own.
    3. **A legacy domain** — one inventoried with no ownership record at
       all: when the config is known (*legacy_disks*), its config-declared
       disks, by name. A VM gone from conf.yml keeps them, and so does a
       domain already undefined when inventoried (a retried teardown):
       its record, if it had one, is gone.

    Every admitted file must be a regular file, not a symlink, inside one
    of the cluster *workdirs* once symlinks are resolved, and not a CD-ROM
    (or other media) source of either definition.

    Then, across the whole inventory: a backing chain is removed only as a
    whole (:func:`_refuse_incomplete_chains`), and an unreadable chain
    keeps everything; nothing another domain uses directly or as a backing
    file is removed (*paths_in_use* is asked once, and ``None`` keeps
    everything). What remains is removed heads first, each through
    :func:`remove_if_unchanged` against the identity taken before
    undefining; a layer replaced since keeps everything below it.

    Returns:
        The files removed, and ``(path, reason)`` for every file kept.
    """
    vm = inventory.vm_name
    outcome = StorageOutcome()
    admission = _Admission()
    real = os.path.realpath
    real_workdirs = {real(os.path.expanduser(w)) for w in workdirs}
    chains = inventory.chains
    legacy = list(inventory.legacy_disks or ())
    # a media source and every image under it: a qcow2 CD-ROM can be built
    # on a file the vm also attaches as a disk. Protected before anything is
    # admitted, on every route.
    media = {real(path) for path in inventory.media_sources}
    under_media = {real(layer) for source in inventory.media_sources
                   for layer in (chains or {}).get(source, ())} - media

    def media_refusal(path: str) -> str | None:
        if real(path) in media:
            return _MEDIA
        if real(path) in under_media:
            return _UNDER_MEDIA
        return None

    def unique(paths):
        seen, out = set(), []
        for path in paths:
            if real(path) not in seen:
                seen.add(real(path))
                out.append(path)
        return out

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

    attached_extras = unique(
        path for path in inventory.disk_sources
        if not os.path.basename(path).startswith(f"{vm}."))

    # extra-disk files are never decided by name
    extra_related = {real(path) for path in attached_extras}
    extra_related.update(real(r.source) for r in inventory.records or ())
    extra_related.update(real(path) for path in legacy)
    for source, layers in (chains or {}).items():
        if (source not in inventory.media_sources
                and not os.path.basename(source).startswith(f"{vm}.")):
            extra_related.update(real(layer) for layer in layers)

    # -- 1. the boot-disk family ------------------------------------------
    for path in inventory.boot_family:
        if real(path) in extra_related and media_refusal(path) is None:
            continue
        reason = static_refusal(path)
        if reason is None and inventory.records_state == RECORDS_UNKNOWN:
            reason = _boxman_artifact_refusal(vm, path)
        if reason:
            admission.refuse(path, reason)
        else:
            admission.admit(path)

    # -- 2./3. extra disks -------------------------------------------------
    state = inventory.records_state
    if state == RECORDS_PRESENT:
        records = inventory.records or []
        for record in records:
            if (record.source in inventory.disk_sources
                    or not _may_exist(record.source)):
                continue
            chain, reason = _owned_snapshot_chain(
                inventory, record, real_workdirs)
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
    if chains is None:
        for path in list(admission.admitted.values()):
            admission.refuse(path, (
                "the backing chains of the vm's disks could not be read, so "
                "what depends on it cannot be told"))
    else:
        _refuse_incomplete_chains(chains, admission)

    # -- in use by another domain ------------------------------------------
    if admission.admitted:
        in_use = paths_in_use()
        for resolved, path in list(admission.admitted.items()):
            if in_use is None:
                admission.refuse(path, "could not check whether another "
                                       "domain uses it")
            elif in_use.get(resolved):
                admission.refuse(path, f"domain {in_use[resolved]} uses it")
        if chains:
            _refuse_incomplete_chains(chains, admission)

    # -- removal, heads first ------------------------------------------------
    resolved_chains = [[real(layer) for layer in layers]
                       for layers in (chains or {}).values()]
    depth: dict[str, int] = {}
    for layers in resolved_chains:
        for index, layer in enumerate(layers):
            depth[layer] = max(depth.get(layer, 0), index)
    for resolved in sorted(admission.admitted, key=lambda r: depth.get(r, 0)):
        path = admission.admitted.get(resolved)
        if path is None:
            continue     # kept meanwhile: a layer above it was replaced
        result, where = remove_if_unchanged(
            path, inventory.identities.get(path))
        if result == "removed":
            log.info(f"removed {path} (vm {vm})")
            outcome.removed.append(path)
            del admission.admitted[resolved]
        elif result == "gone":
            del admission.admitted[resolved]
        else:
            admission.refuse(path,
                             "it was replaced after the vm was inspected"
                             if result == "restored" else _stranded(where))
            for layers in resolved_chains:
                if resolved in layers:
                    for below in layers[layers.index(resolved) + 1:]:
                        if below in admission.admitted:
                            admission.refuse(
                                admission.admitted[below],
                                "a layer above it in its backing chain was "
                                "replaced after the vm was inspected")

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
      symlink, in the base's directory, named ``<vm>_<name>.<suffix>``,
      and not a CD-ROM or other media source of the vm.

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
    media = {real(path) for path in inventory.media_sources}
    stem = f"{vm}_{record.name}."
    chain = inventory.chains[head]
    for layer in chain:
        if os.path.islink(layer):
            return None, f"{layer} in its chain is a symlink"
        if not os.path.basename(layer).startswith(stem):
            return None, (f"{layer} in its chain is not named for disk "
                          f"{record.name!r}")
        if real(os.path.dirname(layer)) != directory:
            return None, f"{layer} in its chain is in another directory"
        if real(layer) in media:
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
    """
    directory, name = os.path.split(path)
    private = tempfile.mkdtemp(prefix=".boxman-removing-", dir=directory)
    moved = os.path.join(private, name)
    try:
        os.rename(path, moved)
    except OSError as exc:
        # Nothing was moved, so the private directory is still empty: a
        # plain rmdir (never a recursive delete) removes it, and a failure
        # there must not hide why the rename failed.
        with contextlib.suppress(OSError):
            os.rmdir(private)
        if isinstance(exc, FileNotFoundError):
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
