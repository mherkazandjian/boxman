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


@dataclass
class StorageInventory:
    """
    What a VM's teardown knows about its storage, read before undefining.

    Undefining drops the domain's XML, its ownership record and its
    snapshot metadata, and nothing the teardown decides afterwards may rest
    on a file's name alone, so everything is captured here first.
    """

    #: full name of the VM
    vm_name: str
    #: sources of its ``disk`` devices, live and persistent definition
    disk_sources: list[str]
    #: sources of every other device (CD-ROM, floppy, ...), live and
    #: persistent definition: never deleted
    media_sources: list[str]
    #: its ownership records; ``None`` when it carried none
    records: list[DiskRecord] | None
    #: whether the records could not be read (then ``records`` is None and
    #: nothing recorded-or-not is removed as an extra disk)
    records_unreadable: bool
    #: the backing chain of each attached extra disk (resolved paths, the
    #: disk first, the bottom image last); ``None`` when one could not be
    #: read
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

    @property
    def chain_layers(self) -> list[str] | None:
        """Every layer of every chain in :attr:`chains`, or ``None``."""
        if self.chains is None:
            return None
        return sorted({layer for chain in self.chains.values()
                       for layer in chain})


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

    Candidates, from the *inventory* taken before undefining:

    1. **The boot-disk family** — files under the VM's exclusive names in
       its disk directories (:func:`boot_family_files`): its boot disk, its
       overlays and its memory-snapshot files. Any of them that is a
       source of one of its CD-ROMs, or an extra disk of it, a layer of
       one or a recorded source, is left to the rules below. When an extra
       disk's chain cannot be read, a ``<vm>_snapshot_*`` file cannot be
       told from a layer of one, so all of them are kept.
    2. **Extra disks recorded with role ``data``** at exactly their
       attached source, by the rules of :func:`_leftover_refusal`. A
       recorded disk no longer attached where it was recorded (an external
       snapshot moved it to an overlay) goes with its whole snapshot chain,
       head first, when the chain is provably the VM's own
       (:func:`_owned_snapshot_chain`); otherwise the chain is kept whole.
    3. **A legacy domain** — one with no ownership record at all: when the
       config is known (*legacy_disks*), its config-declared disks
       ``<vm>_<name>.<ext>``, by name as before records existed; otherwise
       (a VM gone from conf.yml) its extra disks are kept.

    Never removed: a CD-ROM (or other media) source, an adopted or other
    non-``data`` recorded disk, an extra disk outside every cluster
    *workdir*, a symlink, or anything another domain uses directly or as a
    backing file. *paths_in_use* is asked once; if it cannot answer
    (``None``), every candidate is kept. Each unlink goes through
    :func:`remove_if_unchanged` against the identity taken before
    undefining.

    Returns:
        The files removed, and ``(path, reason)`` for every file kept.
    """
    vm = inventory.vm_name
    outcome = StorageOutcome()
    kept = outcome.kept

    def real(path: str) -> str:
        return os.path.realpath(path)

    media = {real(path) for path in inventory.media_sources}
    real_workdirs = {real(os.path.expanduser(w)) for w in workdirs}
    attached_extras = []
    for path in inventory.disk_sources:
        if (not os.path.basename(path).startswith(f"{vm}.")
                and path not in attached_extras):
            attached_extras.append(path)
    records = inventory.records
    legacy = (inventory.legacy_disks
              if records is None and not inventory.records_unreadable
              else None)

    # everything decided as an extra disk, not as the boot family
    extra_related = {real(path) for path in attached_extras}
    extra_related.update(real(r.source) for r in records or ())
    extra_related.update(real(path) for path in legacy or ())
    extra_related.update(real(path) for path in inventory.chain_layers or ())

    candidates: list[str] = []
    chains: list[list[str]] = []

    # -- 1. the boot-disk family --------------------------------------------
    for path in inventory.boot_family:
        if real(path) in media:
            kept.append((path, "it is a CD-ROM or other media source of "
                               "the vm"))
            continue
        if real(path) in extra_related:
            continue
        if (inventory.chain_layers is None
                and os.path.basename(path).startswith(f"{vm}_snapshot_")):
            kept.append((path, (
                "the backing chain of the vm's extra disks could not be "
                "read, so a layer of one cannot be told apart from a "
                "memory-snapshot file")))
            continue
        reason = _regular_file_refusal(path)
        if reason:
            kept.append((path, reason))
        else:
            candidates.append(path)

    # -- 2./3. extra disks ----------------------------------------------------
    present_extras = [p for p in attached_extras if os.path.lexists(p)]
    if records is None:
        if legacy is not None:
            for path in legacy:
                if not os.path.lexists(path) or path in candidates:
                    continue
                reason = ("it is a CD-ROM or other media source of the vm"
                          if real(path) in media
                          else _regular_file_refusal(path))
                if (reason is None and real(os.path.dirname(path))
                        not in real_workdirs):
                    reason = "it is outside every cluster workdir of the project"
                if reason:
                    kept.append((path, reason))
                else:
                    candidates.append(path)
            legacy_real = {real(path) for path in legacy}
            present_extras = [p for p in present_extras
                              if real(p) not in legacy_real]
        why = ("its disk ownership record could not be read"
               if inventory.records_unreadable
               else "the domain carries no record of which disks boxman "
                    "created")
        kept.extend((path, why) for path in present_extras)
    else:
        for record in records:
            if (record.source in inventory.disk_sources
                    or not os.path.lexists(record.source)):
                continue
            chain, reason = _owned_snapshot_chain(
                inventory, record, real_workdirs)
            if chain is None:
                kept.append((record.source, (
                    f"boxman created it, but it was no longer attached "
                    f"where it was recorded (an external snapshot moves a "
                    f"disk to an overlay) and {reason}; remove it and its "
                    f"overlays by hand")))
            else:
                chains.append(chain)
        in_chains = {layer for chain in chains for layer in chain}
        by_source = {record.source: record for record in records}
        for path in present_extras:
            if real(path) in in_chains:
                continue
            reason = _leftover_refusal(vm, path, by_source.get(path),
                                       real_workdirs)
            if reason:
                kept.append((path, reason))
            else:
                candidates.append(path)

    if not candidates and not chains:
        return outcome

    # -- in use by another domain, then the identity-checked unlink ----------
    in_use = paths_in_use()
    for path in candidates:
        if in_use is None:
            kept.append((path, "could not check whether another domain "
                               "uses it"))
            continue
        user = in_use.get(real(path))
        if user:
            kept.append((path, f"domain {user} uses it"))
            continue
        result, where = remove_if_unchanged(
            path, inventory.identities.get(path))
        if result == "removed":
            log.info(f"removed {path} (vm {vm})")
            outcome.removed.append(path)
        elif result == "restored":
            kept.append((path, "it was replaced after the vm was inspected"))
        elif result == "stranded":
            kept.append((path, _stranded(where)))

    for chain in chains:
        _remove_chain(chain, vm, inventory, in_use, outcome)
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
    - every layer is a regular file in the base's directory named
      ``<vm>_<name>.<suffix>``.

    Whether another domain uses a layer is checked at removal time.

    Returns:
        ``(chain, None)`` — head first, base last — or ``(None, reason)``.
    """
    vm = inventory.vm_name
    if record.role != ROLE_DATA:
        return None, (f"boxman attached it but did not create it (role "
                      f"{record.role!r})")
    if inventory.chains is None:
        return None, ("the backing chains of the vm's extra disks could not "
                      "be read")
    base = os.path.realpath(record.source)
    heads = [source for source, chain in inventory.chains.items()
             if chain and chain[-1] == base]
    if len(heads) != 1:
        return None, ("no single attached disk has it at the bottom of its "
                      "backing chain")
    head = heads[0]
    if inventory.targets.get(head) != record.target:
        return None, (f"the disk built on it is attached at "
                      f"{inventory.targets.get(head)}, not at "
                      f"{record.target} where it was recorded")
    directory = os.path.dirname(base)
    if directory not in workdirs:
        return None, "it is outside every cluster workdir of the project"
    stem = f"{vm}_{record.name}."
    chain = inventory.chains[head]
    for layer in chain:
        if not os.path.basename(layer).startswith(stem):
            return None, (f"{layer} in its chain is not named for disk "
                          f"{record.name!r}")
        if os.path.dirname(layer) != directory:
            return None, f"{layer} in its chain is in another directory"
        reason = _regular_file_refusal(layer)
        if reason:
            return None, f"{layer} in its chain: {reason}"
    return chain, None


def _remove_chain(chain: list[str], vm: str, inventory: StorageInventory,
                  in_use: dict[str, str] | None,
                  outcome: StorageOutcome) -> None:
    """
    Remove an owned snapshot chain from the head down to the base, so a
    failure part-way never leaves an overlay whose backing file is gone.
    Any layer another domain uses — or an unanswered scan — keeps the whole
    chain; a layer replaced since the inspection keeps it and everything
    below it.
    """
    kept = outcome.kept
    if in_use is None:
        kept.extend((layer, "could not check whether another domain uses "
                            "it") for layer in chain)
        return
    users = {in_use[layer] for layer in chain if in_use.get(layer)}
    if users:
        kept.extend((layer, (f"its snapshot chain is used by domain "
                             f"{', '.join(sorted(users))}"))
                    for layer in chain)
        return
    for position, layer in enumerate(chain):
        result, where = remove_if_unchanged(
            layer, inventory.identities.get(layer))
        if result == "removed":
            log.info(f"removed {layer} (vm {vm})")
            outcome.removed.append(layer)
        elif result == "gone":
            continue
        else:
            kept.append((layer,
                         "it was replaced after the vm was inspected"
                         if result == "restored" else _stranded(where)))
            kept.extend((below, ("a layer above it in its snapshot chain "
                                 "was replaced after the vm was inspected"))
                        for below in chain[position + 1:])
            return


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
