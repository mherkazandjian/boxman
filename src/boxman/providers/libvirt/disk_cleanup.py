"""
Filesystem-only helpers for removing a VM's disks and leftover artifacts.

Extracted from :meth:`LibVirtSession.destroy_disks` in Phase 2.6 of the
engineering review plan so that the pure filesystem
logic lives outside the libvirt session class — both for clarity and so
it can be exercised without constructing a session.

The logic matches the contract pinned by
``tests/test_libvirt_session.py::TestDestroyDisks``: remove the boot
disk, any extra named disks, and any snapshot artifacts prefixed with
the VM name (overlay files with timestamp/hash suffixes,
``<vm>_snapshot_*.raw`` memory files).
"""

from __future__ import annotations

import glob as _glob
import os
import stat
from collections.abc import Callable, Iterable

from boxman import log

from .disk import disk_path_for
from .disk_ownership import ROLE_DATA, DiskRecord


def remove_vm_disks(
    workdir: str,
    vm_name: str,
    extra_disks: Iterable[dict[str, str]] = (),
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

    Returns:
        ``True`` once the sweep completes (even if there was nothing to
        delete). Always ``True`` today — mirrors the legacy method
        signature; a future revision may promote individual failures
        to exceptions.
    """
    workdir = os.path.expanduser(workdir)

    boot_disk = disk_path_for(workdir, vm_name)
    if os.path.isfile(boot_disk):
        os.remove(boot_disk)

    for disk in extra_disks:
        disk_path = disk_path_for(workdir, disk["name"], disk_prefix=vm_name)
        if os.path.isfile(disk_path):
            os.remove(disk_path)

    # Snapshot artifacts: overlay files named ``<vm>.<suffix>`` (the
    # literal dot separates the VM name from the timestamp/hash suffix)
    # and memory snapshot files ``<vm>_snapshot_*``. A plain
    # ``<vm>*`` prefix glob would also match disks of other VMs whose
    # name starts with this VM's name (e.g. destroying ``web`` would
    # delete ``web2.qcow2``), so both patterns require a separator.
    patterns = (
        os.path.join(workdir, f'{vm_name}.*'),
        os.path.join(workdir, f'{vm_name}_snapshot_*'),
    )
    for pattern in patterns:
        for leftover in _glob.glob(pattern):
            if os.path.isfile(leftover):
                log.info(f"removing snapshot artifact: {leftover}")
                os.remove(leftover)

    return True


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


def remove_recorded_leftovers(
    vm_name: str,
    attached: list[str],
    records: list[DiskRecord] | None,
    workdirs: Iterable[str],
    identities: dict[str, tuple[int, int]],
    paths_in_use: Callable[[], dict[str, str] | None],
) -> list[tuple[str, str]]:
    """
    Remove the data disks an undefined VM's teardown left behind — on
    recorded ownership only, never on the file name.

    ``undefine --remove-all-storage`` removes only the volumes a storage
    pool lists, so an extra disk boxman created with ``qemu-img`` survives
    it as "not managed by libvirt". Being attached and named ``<vm>_...``
    proves neither that boxman created a file nor that nothing else uses
    it: another VM's disk can carry that prefix (``web`` and ``web_2``), an
    ``attach_only`` disk was adopted rather than created, and a file can be
    another domain's backing image. A leftover is removed only when all of
    these hold:

    1. the domain's ownership record, read before undefining, lists it
       with role ``data`` at exactly this source path;
    2. its name is ``<vm>_<recorded name>.<ext>``, as the attach path
       creates it;
    3. it is a regular file, not a symlink, directly in one of the
       project's cluster *workdirs* once symlinks are resolved;
    4. it is the same file (device and inode) that was attached;
    5. no defined domain uses it, directly or as a backing file —
       *paths_in_use* is asked once, and a ``None`` answer keeps them all.

    A recorded disk that is no longer attached where it was recorded (an
    external snapshot moved it to an overlay) is kept whole: nothing
    recorded names the overlays, and removing half a chain is worse than
    leaving it.

    Args:
        vm_name: Full name of the (already undefined) VM.
        attached: Its disk files, read before undefining.
        records: Its ownership records, read before undefining; ``None``
            when it had none, which keeps every file.
        workdirs: The project's cluster workdirs.
        identities: :func:`file_identities` of *attached*, taken before
            undefining.
        paths_in_use: Returns resolved path -> name of the domain using
            it, or ``None`` when that cannot be determined.

    Returns:
        ``(path, reason)`` for every leftover file that was kept.
    """
    kept: list[tuple[str, str]] = []
    present = [path for path in attached if os.path.lexists(path)]

    if records is None:
        return [(path, "the domain carries no record of which disks "
                       "boxman created") for path in present]

    for record in records:
        if record.source not in attached and os.path.lexists(record.source):
            kept.append((record.source, (
                "boxman created it, but it was no longer attached where "
                "it was recorded (an external snapshot moves a disk to an "
                "overlay); remove it and its overlays by hand")))

    real_workdirs = {os.path.realpath(os.path.expanduser(workdir))
                     for workdir in workdirs}
    by_source = {record.source: record for record in records}
    candidates = []
    for path in present:
        reason = _leftover_refusal(vm_name, path, by_source.get(path),
                                   real_workdirs)
        if reason:
            kept.append((path, reason))
        else:
            candidates.append(path)
    if not candidates:
        return kept

    in_use = paths_in_use()
    for path in candidates:
        if in_use is None:
            kept.append((path, "could not check whether another domain "
                               "uses it"))
            continue
        user = in_use.get(os.path.realpath(path))
        if user:
            kept.append((path, f"domain {user} uses it"))
            continue
        # checked last, right before unlinking: the file must still be the
        # one (device and inode) that was attached before undefining
        if file_identities([path]).get(path) != identities.get(path):
            kept.append((path, "it was replaced after the vm was inspected"))
            continue
        log.info(f"removing leftover disk of {vm_name}: {path}")
        os.remove(path)
    return kept
