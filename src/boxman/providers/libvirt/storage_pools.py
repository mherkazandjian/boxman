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


def ensure_pool_on(virsh, directory: str, name: str) -> str | None:
    """
    An active storage pool on *directory*, for the chain reads that find a
    file there through libvirt (#221): the one already on it, under
    whatever name — started if it is not — else a new directory pool
    *name*, started and set to autostart.

    A pool named *name* on another directory is never taken for it: a
    workdir ``…/ws/web`` and an overlay directory ``…/ov/web`` share a
    basename. One is on it when its target is spelled as *directory* is,
    once normalized — the chain reads find the pool on a file's directory
    by that spelling, and libvirt a volume by its path (#221), so a pool on
    the same directory under another name, a symlink or a bind mount,
    serves neither — and is that directory by identity, ``(st_dev,
    st_ino)``: spelled alike, ``hidden/link/../../ov`` can be another
    directory than ``ov``, and one that cannot be looked up is not taken
    for it. With *name* taken -- by a pool on this directory under another
    path, or on another directory, as when a cluster's disk_snapshot_dir
    moved with a pool still on the old one, which is left as it is -- the
    first free ``<name>_<n>`` is defined instead.

    Best effort: what stops it is named in a warning, never raised.

    Args:
        virsh: Command executor with an ``execute`` method (a
            :class:`VirshCommand`).
        directory: The directory the pool must be on.
        name: The name of the pool to define when none is on it.

    Returns:
        The pool's name, or ``None`` when there is none on *directory*.
    """
    where = f"could not see to a storage pool on {directory}"
    try:
        st = os.stat(directory)
    except OSError as exc:
        log.warning(f"{where}: it could not be looked up ({exc})")
        return None
    target = (st.st_dev, st.st_ino)
    listing = virsh.execute("pool-list", "--all", "--name", warn=True)
    if not listing.ok:
        log.warning(f"{where}: the pools could not be listed: "
                    f"{(listing.stderr or '').strip()}")
        return None
    names = [line.strip() for line in listing.stdout.splitlines()
             if line.strip()]
    for pool in names:
        dumped = virsh.execute("pool-dumpxml", pool, warn=True)
        try:
            path = (ET.fromstring(dumped.stdout).findtext("./target/path")
                    if dumped.ok else None)
        except ET.ParseError:
            path = None
        # one with no local directory (RBD, say) is not on it; one that
        # cannot be read makes the definition below fail, if it is
        if path is None:
            continue
        try:
            on = os.stat(path)
        except OSError:
            continue      # not known to be on it
        if (on.st_dev, on.st_ino) != target:
            continue
        if os.path.normpath(path) != os.path.normpath(directory):
            continue      # on it under another path: no lookup finds it
        active = virsh.execute("pool-list", "--name", warn=True)
        if active.ok and pool in (line.strip()
                                  for line in active.stdout.splitlines()):
            return pool
        started = virsh.execute("pool-start", pool, warn=True)
        if not started.ok:
            log.warning(f"{where}: pool {pool} is on it but could not be "
                        f"started: {(started.stderr or '').strip()}")
            return None
        return pool
    if name in names:
        # taken by a pool not on it as spelled, which stays as it is
        taken, n = name, 2
        while f"{taken}_{n}" in names:
            n += 1
        name = f"{taken}_{n}"
    for verb in ("pool-define-as", "pool-start"):
        args = ((name, "dir", "--target", directory)
                if verb == "pool-define-as" else (name,))
        result = virsh.execute(verb, *args, warn=True)
        if not result.ok:
            log.warning(f"{where}: {verb} {name} failed: "
                        f"{(result.stderr or '').strip()}")
            return None
    autostart = virsh.execute("pool-autostart", name, warn=True)
    if not autostart.ok:
        log.warning(f"storage pool {name} on {directory} will not start with "
                    f"the host: {(autostart.stderr or '').strip()}")
    return name
