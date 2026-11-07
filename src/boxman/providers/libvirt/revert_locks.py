"""
One revert at a time of the VMs whose disks read one image (#303).

Reverting to an external snapshot, libvirt builds a qcow2 overlay of every
disk with media, beside the image the disk reads in the snapshot's saved
definition, and names it after that image, its last suffix dropped, and the
time in whole seconds: ``<stem>.<epoch>``. VMs whose CD-ROMs share an ISO --
or that share any image -- reverted in the same second compute the same
overlay: one revert fails ("Image is not in qcow2 format") and its domain is
left on the other's overlay.

So a restore holds a lock per overlay name its revert can compute -- the
image's directory, resolved, and its stem (:func:`overlay_key`) -- until its
revert returns, and stamps the lock with the next whole second; the next
holder waits for that second just before it reverts, its preparation done
meanwhile (:meth:`RevertLock.reverting`). As the revert starts the lock is
stamped two seconds ahead: a holder killed during the revert, which nothing
it runs outlives, still leaves a stamp that keeps the next holder off the
seconds libvirt may yet name an overlay in. Reverts of VMs that share no
image take no common lock and still run in parallel.

The locks are ``flock`` locks on files in boxman's per-user cache dir: they
hold between the restore workers of one command, which are processes of
their own, and between boxman processes of the same user -- another project
booting from the same cached ISO. They do not hold against another user's
boxman, nor against a ``virsh snapshot-revert`` run by hand. The second is
the host's clock, which libvirt reads too.
"""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import time
from collections.abc import Callable, Iterable

from boxman import config_cache

#: the clock the stamps are read and written by, and how a holder waits for
#: one -- the host's, as libvirt's; tests replace them
_now: Callable[[], float] = time.time
_wait: Callable[[float], None] = time.sleep

#: how often a lock held elsewhere is asked for again, and for how long
POLL_S = 0.05
TIMEOUT_S = 600.0


def lock_dir() -> str:
    """Where the lock files are: under boxman's per-user cache dir, read
    when called."""
    return os.path.join(os.path.expanduser(config_cache.DEFAULT_CACHE_DIR),
                        "revert-locks")


def overlay_key(source: str) -> str:
    """
    What a revert's overlay of a disk reading *source* is named after: its
    directory, resolved -- one directory under two names is one -- and its
    name with the last suffix dropped. ``nixos.iso`` and ``nixos.img`` in
    one directory give one key, as libvirt gives them one overlay name, and
    so does ``nixos.1793626526``, an overlay a revert made of either.
    """
    return os.path.join(os.path.realpath(os.path.dirname(source)),
                        os.path.splitext(os.path.basename(source))[0])


class RevertLockTimeoutError(TimeoutError):
    """A lock stayed held elsewhere for longer than the timeout."""

    def __init__(self, key: str, timeout: float) -> None:
        super().__init__(
            f"another revert of a VM whose disk reads an image named "
            f"{key}.* held its lock for more than {timeout:g} s")
        self.key = key


class RevertLock:
    """
    ``with RevertLock(keys):`` -- hold the lock of every key in *keys* (from
    :func:`overlay_key`), taken in sorted order, so two holders of several
    never wait for each other in a circle. Call :meth:`reverting` just
    before each revert: it waits for the second the last holder stamped. On
    leaving, however that happens, stamp each with the next whole second --
    or with the stamp :meth:`reverting` wrote, if later -- and release it;
    a holder that never reverted puts back the stamp it found. No keys, no
    lock.

    *on_wait* is called once with a key whose lock is held elsewhere, before
    waiting for it.

    Raises:
        RevertLockTimeoutError: a lock stayed held elsewhere longer than
            *timeout* seconds; nothing is held then.
        OSError: a lock file could not be opened; nothing is held then.
    """

    def __init__(self, keys: Iterable[str], timeout: float = TIMEOUT_S,
                 on_wait: Callable[[str], None] | None = None) -> None:
        self.keys = sorted(set(keys))
        self.timeout = timeout
        self.on_wait = on_wait
        #: (key, descriptor, the stamp it held when taken)
        self._held: list[tuple[str, int, bytes]] = []
        #: the stamp :meth:`reverting` wrote last, if it ran
        self._ahead: int | None = None

    @staticmethod
    def path_for(key: str) -> str:
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        return os.path.join(lock_dir(), f"{digest}.lock")

    def __enter__(self) -> RevertLock:
        if not self.keys:
            return self
        try:
            os.makedirs(lock_dir(), exist_ok=True)
            deadline = time.monotonic() + self.timeout
            for key in self.keys:
                fd = os.open(self.path_for(key),
                             os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
                try:
                    self._lock(key, fd, deadline)
                except BaseException:
                    os.close(fd)
                    raise
                self._held.append((key, fd, os.pread(fd, 64, 0)))
        except BaseException:
            self._release(stamp=False)
            raise
        return self

    def __exit__(self, *_exc) -> None:
        self._release(stamp=True)

    def reverting(self) -> None:
        """
        Just before ``snapshot-revert``: wait for the second the last holder
        stamped -- a revert of this holder's own included -- then stamp each
        lock with the second after next. A holder killed during the revert
        runs no release, and libvirtd may carry on with the revert without
        it, naming its overlays in the second it started or the next: the
        stamp keeps the next holder off both. Best effort: a release that
        runs stamps again.
        """
        if self._held:
            # a stamp is at most two seconds ahead of the revert that wrote
            # it, which came before this: one further ahead is a clock set
            # back since, and the second it names is not this one
            delay = min(max(self._stamp_in(fd) for _key, fd, _s in self._held)
                        - _now(), 2.0)
            if delay > 0:
                _wait(delay)
        self._ahead = math.floor(_now()) + 2
        data = f"{self._ahead}\n".encode()
        for _key, fd, _found in self._held:
            try:
                os.pwrite(fd, data, 0)
                os.ftruncate(fd, len(data))
            except OSError:
                pass

    def _lock(self, key: str, fd: int, deadline: float) -> None:
        waited = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                pass
            if not waited and self.on_wait is not None:
                self.on_wait(key)
            waited = True
            if time.monotonic() >= deadline:
                raise RevertLockTimeoutError(key, self.timeout)
            time.sleep(POLL_S)

    @staticmethod
    def _stamp_in(fd: int) -> float:
        """The second the last holder stamped: 0 for a new lock file, and
        the next second from now for one that does not read as a stamp."""
        text = os.pread(fd, 64, 0).decode(errors="replace").strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return math.floor(_now()) + 1.0

    def _release(self, stamp: bool) -> None:
        not_before = max(math.floor(_now()) + 1, self._ahead or 0)
        held, self._held = self._held, []
        for _key, fd, found in held:
            try:
                if stamp:
                    # no revert, no second to keep anyone off: what was
                    # there goes back
                    data = (f"{not_before}\n".encode()
                            if self._ahead is not None else found)
                    try:
                        os.pwrite(fd, data, 0)
                        os.ftruncate(fd, len(data))
                    except OSError:
                        # unstamped, the next holder could revert within
                        # this second: wait it out here instead
                        delay = not_before - _now()
                        if self._ahead is not None and delay > 0:
                            _wait(delay)
            finally:
                os.close(fd)
