"""
Keep libvirt's storage pools in step with the files boxman removes itself.

``virt-install`` and ``virt-clone`` create a directory pool for every
directory they are handed, so a VM's workdir is usually a pool. Since
teardown no longer lets libvirt delete storage (#208), a file boxman
removes stays listed in its pool until the pool is refreshed. A stale
entry does not stop a later ``virt-clone`` to the same path (checked on
libvirt 10.0), but the pool then lists files that are gone — to
``vol-list``, virt-manager and any volume lookup by path — so the pools
are refreshed after a removal.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from xml.etree import ElementTree as ET

from boxman import log


def refresh_pools_holding(virsh, paths: Iterable[str]) -> list[str]:
    """
    Refresh every active pool whose target directory held one of *paths*.

    Best effort: a pool that cannot be listed or refreshed is named in a
    warning, never raised — the files are already gone by now.

    Args:
        virsh: Command executor with an ``execute`` method (a
            :class:`VirshCommand`).
        paths: Files that were removed.

    Returns:
        The names of the pools refreshed.
    """
    directories = {os.path.realpath(os.path.dirname(path)) for path in paths}
    if not directories:
        return []
    listing = virsh.execute("pool-list", "--name", warn=True)
    if not listing.ok:
        log.warning(
            f"could not list the storage pools to refresh after removing "
            f"files in {', '.join(sorted(directories))}: "
            f"{(listing.stderr or '').strip()}")
        return []
    refreshed = []
    for pool in (line.strip() for line in listing.stdout.splitlines()):
        if not pool:
            continue
        dumped = virsh.execute("pool-dumpxml", pool, warn=True)
        try:
            target = (ET.fromstring(dumped.stdout).findtext("./target/path")
                      if dumped.ok else None)
        except ET.ParseError:
            target = None
        if target is None:
            log.warning(f"could not read the target of storage pool {pool}; "
                        f"it was not refreshed")
            continue
        if os.path.realpath(target) not in directories:
            continue
        result = virsh.execute("pool-refresh", pool, warn=True)
        if result.ok:
            refreshed.append(pool)
        else:
            log.warning(
                f"could not refresh storage pool {pool} after removing files "
                f"from it: {(result.stderr or '').strip()}")
    return refreshed
