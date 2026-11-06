"""
What libvirt says of a domain's snapshots and persistent definition, kept
for the rest of one operation, and read again only once something may have
changed it.

A ``snapshot restore`` reads the same things more than once: right after the
revert, the list of the VM's snapshots and every one's XML for the backup's
inventory, then again for the CD-ROM put-back. Each read is a ``virsh`` call
(about 30 ms through invoke). Inside :func:`keeping` (one restore), a
successful read of one of :data:`KEPT` is kept by its exact command line,
and the same command line, asked again in the same thread, is answered from
it.

It is answered only while nothing else has run since. Any command boxman
runs through :class:`~.commands.LibVirtCommandBase` that only reads
(:data:`READ_ONLY`: ``virsh list``, the live ``dumpxml`` and the like) leaves
it kept. Anything else drops every kept read: a revert, a redefine, a media
change, a suspend, a ``cp``, a ``qemu-img``, from any thread of this
process. A command boxman does not know as a read counts as one that changes
things.

What is kept describes only what libvirt changes when asked to: a domain's
snapshot metadata, and its persistent definition (``--inactive``). Never
what the guest or QEMU can change on its own: the live definition (a tray
the guest opens) or the domain's state. Another process could change a kept
read meanwhile, as it could between two reads before. A kept read is only
answered where boxman read the same thing again with nothing of its own in
between.

Off unless asked for, and only in the thread that asked. The host-wide scans
read everything afresh (:func:`fresh`), as they must, and so do their
threads, which never keep anything.
"""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

#: a read kept, and answered while nothing else has run
KEPT = "kept"
#: a read: kept by none, and it drops none
READ = "read"
#: anything else: it may change what a kept read said, so it drops them all
CHANGE = "change"

#: the ``virsh`` subcommands whose answer is kept: one domain's snapshot
#: metadata, which only libvirt changes, and only when asked to
KEPT_SUBCOMMANDS = frozenset({
    "snapshot-list", "snapshot-dumpxml", "snapshot-current", "snapshot-info",
    "snapshot-parent"})

#: these too, for the persistent definition (``--inactive``) only: the live
#: one changes with what the guest does
KEPT_INACTIVE = frozenset({"dumpxml", "domblklist"})

#: the ``virsh`` subcommands that only read, kept or not
READ_ONLY = KEPT_SUBCOMMANDS | KEPT_INACTIVE | frozenset({
    "list", "domstate", "dominfo", "domiflist", "domifaddr", "domjobinfo",
    "vol-path", "vol-pool", "vol-dumpxml", "vol-list", "pool-list",
    "pool-dumpxml", "pool-info", "net-list", "net-dumpxml",
    "managedsave-dumpxml", "backup-dumpxml", "save-image-dumpxml"})

#: guards :data:`_generation` and :data:`_changing`
_lock = threading.Lock()
#: how many times something may have changed what a kept read says: a read
#: is answered only while this is what it was when the read was made
_generation = 0
#: how many changes are running now, in any thread: a read made while one
#: runs may show it done or not, and is kept by none
_changing = 0
#: per thread: ``depth`` of :func:`keeping` blocks, ``kept`` reads by
#: command line, and ``fresh``, the depth of :func:`fresh` blocks
_local = threading.local()


def kind(subcommand: str, args: tuple, kwargs: dict) -> str:
    """:data:`KEPT`, :data:`READ` or :data:`CHANGE`: what ``virsh
    <subcommand> <args>`` does here."""
    if subcommand in KEPT_SUBCOMMANDS:
        return KEPT
    if subcommand in KEPT_INACTIVE and (
            "--inactive" in args or kwargs.get("inactive") is True):
        return KEPT
    return READ if subcommand in READ_ONLY else CHANGE


def _change(begins: bool) -> None:
    """A change begins, or ends: no read kept before it is answered once it
    has begun, and none is kept while it runs."""
    global _generation, _changing
    with _lock:
        _generation += 1
        _changing += 1 if begins else -1


def _settled() -> int | None:
    """The generation now, or ``None`` while a change runs."""
    with _lock:
        return None if _changing else _generation


def _kept() -> dict[str, tuple[int, Any]] | None:
    """This thread's kept reads, or ``None`` outside :func:`keeping` or
    inside :func:`fresh`."""
    if getattr(_local, "fresh", 0) or not getattr(_local, "depth", 0):
        return None
    return _local.kept


def through(command: str, what: str, run: Callable[[], Any]) -> Any:
    """
    Run *command*, *what* it is (:func:`kind`), by calling *run*, or answer
    it from a read kept earlier. A :data:`KEPT` read is kept when it
    succeeds and no change ran while it did, and it answers the same
    *command* until a change begins. A :data:`CHANGE` drops every kept read
    as it begins, and keeps any read made while it runs from being kept,
    however it ends.
    """
    if what == CHANGE:
        _change(begins=True)
        try:
            return run()
        finally:
            _change(begins=False)
    kept = _kept() if what == KEPT else None
    if kept is None:
        return run()
    since = _settled()
    found = kept.get(command)
    if found is not None and since is not None and found[0] == since:
        return found[1]
    result = run()
    # kept only if no change ran meanwhile, in another thread say
    if (since is not None and getattr(result, "ok", False)
            and _settled() == since):
        kept[command] = (since, result)
    return result


def changes(run_command: Callable) -> Callable:
    """*run_command*, each call taken for a :data:`CHANGE`: a shell
    command, which may do anything."""
    @functools.wraps(run_command)
    def changing(*args: Any, **kwargs: Any) -> Any:
        return through("", CHANGE, lambda: run_command(*args, **kwargs))
    return changing


@contextmanager
def kept_here() -> Iterator[None]:
    """Within it, this thread keeps its reads (see the module docstring);
    on leaving the outermost one, it forgets them."""
    depth = getattr(_local, "depth", 0)
    if not depth:
        _local.kept = {}
    _local.depth = depth + 1
    try:
        yield
    finally:
        _local.depth = depth
        if not depth:
            _local.kept = {}


def keeping(operation: Callable) -> Callable:
    """*operation*, run within :func:`kept_here`."""
    @functools.wraps(operation)
    def keeping_its_reads(*args: Any, **kwargs: Any) -> Any:
        with kept_here():
            return operation(*args, **kwargs)
    return keeping_its_reads


def fresh(scan: Callable) -> Callable:
    """*scan*, every read of it made afresh, and kept by nothing: a scan of
    what every domain uses decides what may be removed."""
    @functools.wraps(scan)
    def reading_afresh(*args: Any, **kwargs: Any) -> Any:
        _local.fresh = getattr(_local, "fresh", 0) + 1
        try:
            return scan(*args, **kwargs)
        finally:
            _local.fresh -= 1
    return reading_afresh


def _after_fork_in_child() -> None:
    """A child process answers nothing its parent kept: that was read
    before whatever the parent and its other children have done since. The
    lock is made anew, as another thread may have held it at the fork."""
    global _lock, _generation, _changing
    _lock = threading.Lock()
    _generation += 1
    _changing = 0


os.register_at_fork(after_in_child=_after_fork_in_child)
