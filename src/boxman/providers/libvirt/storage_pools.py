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

A directory ``destroy`` removes takes no pool with it: the pool stays
defined, and active, on a directory that is gone, and the next workdir of
that basename gets a ``<basename>-<n>`` pool from virt-install instead,
which leaks the same way (#277). So ``destroy`` retires the pools on the
directories it removed (:func:`retire_pool`): they are stopped and
undefined, never deleted with ``pool-delete``, which removes the directory
itself.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from boxman import log

#: a UUID, as ``virsh`` prints a domain's or a pool's
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                   re.IGNORECASE)


@dataclass(frozen=True)
class Pool:
    """A storage pool, as libvirt described it when it was read
    (:func:`read_pools`)."""

    name: str
    uuid: str
    #: ``dir``, ``netfs``, ``rbd``, ...
    type: str
    #: its target directory, spelled as libvirt holds it; ``None`` for a
    #: pool with none (RBD, say)
    target: str | None
    active: bool
    persistent: bool


def _listed(virsh, *flags: str) -> list[str] | None:
    """What ``virsh pool-list *flags*`` prints, one name or UUID a line
    (padded with blanks under ``--all`` on libvirt 10), or ``None`` when it
    cannot be listed."""
    listing = virsh.execute("pool-list", *flags, warn=True)
    if not listing.ok:
        return None
    return [line.strip() for line in (listing.stdout or "").splitlines()
            if line.strip()]


def _described(xml: str) -> tuple[str, str, str | None] | None:
    """``(uuid, type, target)`` of a pool from its ``pool-dumpxml``, or
    ``None`` when *xml* does not describe a pool."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    uuid = (root.findtext("uuid") or "").strip()
    kind = root.get("type")
    if root.tag != "pool" or not uuid or not kind:
        return None
    return uuid, kind, root.findtext("./target/path") or None


def read_pools(virsh) -> list[Pool] | None:
    """
    Every storage pool libvirt has, active or not, persistent or transient:
    ``pool-list --all --name``, ``pool-list --name`` (the active ones),
    ``pool-list --all --transient --name`` and each one's ``pool-dumpxml``.

    Fails closed: ``None`` when any of it cannot be read, a pool undefined
    between the listing and its description included. A caller deciding
    which pools to retire must not decide on part of them.

    Args:
        virsh: Command executor with an ``execute`` method (a
            :class:`VirshCommand`).
    """
    every = _listed(virsh, "--all", "--name")
    active = _listed(virsh, "--name")
    transient = _listed(virsh, "--all", "--transient", "--name")
    if every is None or active is None or transient is None:
        return None
    pools = []
    for name in every:
        dumped = virsh.execute("pool-dumpxml", name, warn=True)
        described = _described(dumped.stdout or "") if dumped.ok else None
        if described is None:
            return None
        uuid, kind, target = described
        pools.append(Pool(name=name, uuid=uuid, type=kind, target=target,
                          active=name in active,
                          persistent=name not in transient))
    return pools


def _at_or_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def pools_under(pools: Iterable[Pool], trees: Iterable[str]) -> list[Pool]:
    """
    The directory pools of *pools* whose target is at or under one of
    *trees*, the directories a ``destroy`` is about to remove. Asked before
    they go: a target counts by where it resolves to
    (``os.path.realpath(..., strict=True)``), so a symlink in a tree that
    leads out of it does not bring the pool on its far end in, and a target
    elsewhere that leads into a tree does.

    A target already gone (a retried ``destroy`` removed it the first time)
    counts by its spelling, normalized, under a tree as configured or as it
    resolves. One that cannot be resolved for any other reason (a directory
    on the way that cannot be searched, a symlink loop) is not known to be
    under any; neither is one that is not absolute, nor a pool of another
    type: an RBD or a ``netfs`` pool is not a directory boxman removes.

    Args:
        pools: The pools (:func:`read_pools`).
        trees: Absolute paths of the directories to be removed.
    """
    resolved = {os.path.realpath(tree) for tree in trees}
    spelled = resolved | {os.path.normpath(tree) for tree in trees}
    found = []
    for pool in pools:
        if (pool.type != "dir" or not pool.target
                or not os.path.isabs(pool.target)):
            continue
        try:
            where, roots = os.path.realpath(pool.target, strict=True), resolved
        except FileNotFoundError:
            where, roots = os.path.normpath(pool.target), spelled
        except OSError:
            continue
        if any(_at_or_under(where, root) for root in roots):
            found.append(pool)
    return found


def domain_pool_refs(virsh) -> dict[str, list[str]] | None:
    """
    The storage pools that domain definitions name, each with the domains
    naming it: a ``<disk type='volume'>`` whose ``<source pool=...>`` is
    it, in the live or the persistent definition of any domain, running or
    not. Once such a pool is gone, that domain cannot start, or attach the
    disk again.

    Fails closed: ``None`` when the domains cannot be listed, or a
    definition cannot be read (that of a domain undefined meanwhile
    included).

    Args:
        virsh: Command executor with an ``execute`` method (a
            :class:`VirshCommand`).

    Returns:
        ``{pool name: [domain, ...]}``, or ``None``.
    """
    listing = virsh.execute("list", "--all", "--uuid", "--name", warn=True)
    running = virsh.execute("list", "--uuid", warn=True)
    if not listing.ok or not running.ok:
        return None
    domains = []
    for line in (listing.stdout or "").splitlines():
        uuid, _, name = line.strip().partition(" ")
        if not uuid:
            continue
        if not _UUID.fullmatch(uuid) or not name.strip():
            return None
        domains.append((uuid, name.strip()))
    live = {line.strip() for line in (running.stdout or "").splitlines()
            if line.strip()}
    refs: dict[str, list[str]] = {}
    for uuid, name in domains:
        # a shut-off domain's live definition is its persistent one
        for flags in ((("--inactive",), ()) if uuid in live
                      else (("--inactive",),)):
            dumped = virsh.execute("dumpxml", *flags, uuid, warn=True)
            if not dumped.ok:
                return None
            try:
                root = ET.fromstring(dumped.stdout or "")
            except ET.ParseError:
                return None
            for disk in root.iterfind("./devices/disk[@type='volume']"):
                source = disk.find("source")
                pool = None if source is None else source.get("pool")
                if pool and name not in refs.setdefault(pool, []):
                    refs[pool].append(name)
    return refs


def _gone(virsh, uuid: str) -> bool:
    """Whether no pool has *uuid* any more: ``pool-list --all --uuid``
    could be read, and does not list it."""
    listed = _listed(virsh, "--all", "--uuid")
    return listed is not None and uuid not in listed


def retire_pool(virsh, pool: Pool) -> str | None:
    """
    Stop *pool* and forget it: ``pool-destroy`` if it is active, then
    ``pool-undefine`` if it is persistent, each by its UUID, which a pool
    defined since under the same name does not share. Never
    ``pool-delete``, which removes the directory itself. The caller has
    established that the directory is gone already, and that no domain
    names the pool (:func:`domain_pool_refs`).

    The pool is described again first, and retired only if it is still
    what *pool* says: the same type, on the same target. One gone meanwhile
    (undefined by someone else, or a transient pool stopped) counts as
    retired.

    Args:
        virsh: Command executor with an ``execute`` method (a
            :class:`VirshCommand`).
        pool: The pool, as :func:`read_pools` read it.

    Returns:
        ``None`` once it is retired, else why it was not, or not wholly. A
        failed ``pool-destroy`` leaves it active and defined: no undefine
        is tried over a pool still running. A failed ``pool-undefine``
        leaves it stopped but defined.
    """
    dumped = virsh.execute("pool-dumpxml", pool.uuid, warn=True)
    if not dumped.ok:
        if _gone(virsh, pool.uuid):
            return None
        return (f"it could not be described again: "
                f"{(dumped.stderr or '').strip()}")
    now = _described(dumped.stdout or "")
    if now != (pool.uuid, pool.type, pool.target):
        return ("it is no longer what it was when the directories were "
                "read (its description changed meanwhile)")
    active = _listed(virsh, "--uuid")
    transient = _listed(virsh, "--all", "--transient", "--uuid")
    if active is None or transient is None:
        return "whether it is active or persistent could not be read"
    if pool.uuid in active:
        stopped = virsh.execute("pool-destroy", pool.uuid, warn=True)
        if not stopped.ok:
            if _gone(virsh, pool.uuid):
                return None
            return (f"pool-destroy failed, so it is still active and "
                    f"defined: {(stopped.stderr or '').strip()}")
    if pool.uuid not in transient:
        undefined = virsh.execute("pool-undefine", pool.uuid, warn=True)
        if not undefined.ok:
            if _gone(virsh, pool.uuid):
                return None
            return (f"pool-undefine failed, so it is stopped but still "
                    f"defined: {(undefined.stderr or '').strip()}")
    return None


def autostarted_pools(virsh) -> set[str] | None:
    """The names of the pools set to start with the host (``pool-list
    --all --autostart --name``), or ``None`` when they cannot be listed."""
    listed = _listed(virsh, "--all", "--autostart", "--name")
    return None if listed is None else set(listed)


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
