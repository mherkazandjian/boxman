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

    boot_disk = os.path.join(workdir, f'{vm_name}.qcow2')
    if os.path.isfile(boot_disk):
        os.remove(boot_disk)

    for disk in extra_disks:
        disk_path = os.path.join(workdir, f'{vm_name}_{disk["name"]}.qcow2')
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
