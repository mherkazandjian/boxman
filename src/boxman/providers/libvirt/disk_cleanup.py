"""
Filesystem-only helpers for removing a VM's disks and leftover artifacts.

Extracted from :meth:`LibVirtSession.destroy_disks` in Phase 2.6 of the
review plan (see /home/mher/.claude/plans/) so that the pure filesystem
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
from collections.abc import Iterable

from boxman import log


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
    - Every remaining path matching ``<workdir>/<vm_name>*`` that is a
      regular file — this sweeps up snapshot overlay files
      (``<vm>.2026-04-21T08:00:00``, ``<vm>.1772465824``) and memory
      snapshot files (``<vm>_snapshot_<name>.raw``).

    Other VMs' disks in the same workdir are untouched because the glob
    is anchored at the VM name prefix.

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

    boot_disk = os.path.join(workdir, f'{vm_name}.qcow2')
    if os.path.isfile(boot_disk):
        os.remove(boot_disk)

    for disk in extra_disks:
        disk_path = os.path.join(workdir, f'{vm_name}_{disk["name"]}.qcow2')
        if os.path.isfile(disk_path):
            os.remove(disk_path)

    # Snapshot artifacts: overlay files with timestamp/hash suffixes and
    # memory snapshot .raw files — anything prefixed with vm_name that is
    # still on disk after the named qcow2 files were removed above.
    for leftover in _glob.glob(os.path.join(workdir, f'{vm_name}*')):
        if os.path.isfile(leftover):
            log.info(f"removing snapshot artifact: {leftover}")
            os.remove(leftover)

    return True
