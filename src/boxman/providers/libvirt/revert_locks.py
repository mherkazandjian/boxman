"""
One revert at a time of the VMs whose disks read one image (#303).

Reverting to an external snapshot, libvirt builds a qcow2 overlay of every
disk with media, beside the image the disk reads in the snapshot's saved
definition, and names it after that image, its last suffix dropped, and the
time in whole seconds: ``<stem>.<epoch>``. VMs whose CD-ROMs share an ISO --
or that share any image -- reverted in the same second compute the same
overlay: one revert fails ("Image is not in qcow2 format") and its domain is
left on the other's overlay.

So a revert holds a lock per overlay name it can compute -- the image's
directory, resolved, and its stem (:func:`overlay_key`) -- from just before
``snapshot-revert`` until it returns, and stamps the lock with the next whole
second; the next holder waits for that second before it reverts. Reverts of
VMs that share no image take no common lock and still run in parallel.

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
    never wait for each other in a circle; then wait for the second the last
    holder stamped. On leaving, stamp each with the next whole second and
    release it. No keys, no lock.

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
        self._held: list[tuple[str, int]] = []

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
                self._held.append((key, fd))
                self._lock(key, fd, deadline)
            # a stamp is at most a second ahead of the release that wrote
            # it, which came before this: one further ahead is a clock set
            # back since, and the second it names is not this one
            delay = min(max(self._stamp_in(fd) for _key, fd in self._held)
                        - _now(), 1.0)
            if delay > 0:
                _wait(delay)
        except BaseException:
            self._release(stamp=False)
            raise
        return self

    def __exit__(self, *_exc) -> None:
        self._release(stamp=True)

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
        not_before = math.floor(_now()) + 1
        held, self._held = self._held, []
        for _key, fd in held:
            try:
                if stamp:
                    try:
                        data = f"{not_before}\n".encode()
                        os.pwrite(fd, data, 0)
                        os.ftruncate(fd, len(data))
                    except OSError:
                        # unstamped, the next holder could revert within
                        # this second: wait it out here instead
                        delay = not_before - _now()
                        if delay > 0:
                            _wait(delay)
            finally:
                os.close(fd)
