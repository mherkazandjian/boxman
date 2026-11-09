"""
The thread pool the host-wide scans read their domains with: what every
domain uses (``LibVirtSession.disk_paths_in_use``), and what every domain's
snapshots record or name (``snapshot_files_in_use``,
``snapshot_references``).

A scan spends nearly all of its time waiting: on ``virsh`` and ``qemu-img``,
about 30 ms a command through invoke, two to six commands a domain. Each
domain is still read exactly as one read after another reads it -- the same
commands, through the same runner, each domain's own reads in their own
order -- only several domains at once, so that those waits overlap.

What a scan answers does not change: the caller takes the outcomes in the
order the domains were listed (:class:`InOrder`), so a file two domains
use is credited to the one listed first, as before, and the first domain
whose read failed fails the scan, whatever the domains after it would have
said. Reads only run here; nothing a scan does changes what it reads.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for
from typing import Any

from boxman.utils.shell import HeldBack, ctrl_c_held_back, stop_commands

#: the provider setting -- ``provider.libvirt`` in conf.yml, or
#: ``providers.libvirt`` in boxman.yml -- for how many domains a scan reads
#: at once; 1 reads them one after another, as boxman did before
WORKERS_KEY = "in_use_scan_workers"

#: how many domains a scan reads at once by default, and how many disks of
#: one domain: on a host with 71 domains, a scan took 7.8 s one at a time,
#: 2.0 s four at a time, 1.1 s eight at a time and 0.7 s sixteen at a time.
#: Eight leaves room for several scans at once (three took 1.4 s each)
DEFAULT_WORKERS = 8


def scan_workers(provider_config: dict[str, Any], logger) -> int:
    """
    How many domains a scan reads at once: :data:`WORKERS_KEY` from
    *provider_config*, a whole number of 1 or more, else
    :data:`DEFAULT_WORKERS`. A value that is not one is warned about and the
    default used: how fast a scan reads never decides what it answers.
    """
    value = provider_config.get(WORKERS_KEY, DEFAULT_WORKERS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        logger.warning(
            f"{WORKERS_KEY} is {value!r}, not a whole number of 1 or more; "
            f"the in-use scans read {DEFAULT_WORKERS} domains at once")
        return DEFAULT_WORKERS
    return value


#: how long the main thread waits on the reads at a time. A signal -- a
#: Ctrl-C -- that the kernel hands one of the reads' threads is handled by
#: the main thread's Python handler only once that thread runs again, and a
#: wait without a timeout never lets it: the stop began only when a read
#: ended, after the reads had started their next commands (#309)
POLL_S = 0.05


def _wait_for_all(futures: list[Future], ctrl_c: HeldBack | None = None) -> None:
    """Wait for every one of *futures*, a slice at a time (:data:`POLL_S`),
    so that a signal is handled meanwhile; given *ctrl_c*, what
    :func:`~boxman.utils.shell.ctrl_c_held_back` gave, only until a Ctrl-C
    is taken. A future the pool cancelled counts as ended, as ``done()``
    says, though ``concurrent.futures.wait`` would not count it."""
    pending = [future for future in futures if not future.done()]
    while pending and not (ctrl_c is not None and ctrl_c.taken):
        wait_for(pending, timeout=POLL_S)
        pending = [future for future in pending if not future.done()]


class _Outcome:
    """A read's outcome, waited for a slice at a time (:data:`POLL_S`)."""

    def __init__(self, future: Future) -> None:
        self._future = future

    def result(self) -> Any:
        # a Ctrl-C meanwhile begins the stop there and then, and is raised
        # here, once the wait holds nothing (#311 review)
        with ctrl_c_held_back() as ctrl_c:
            _wait_for_all([self._future], ctrl_c)
        if ctrl_c.taken:
            raise KeyboardInterrupt
        return self._future.result()


class _WhenAsked:
    """One read, run when its outcome is asked for: what :class:`InOrder`
    hands out when it reads one item at a time, so that nothing past the
    first failure is read, as before the pool."""

    def __init__(self, read: Callable[[Any], Any], item: Any) -> None:
        self._read = read
        self._item = item

    def result(self) -> Any:
        return self._read(self._item)


class InOrder:
    """
    ``read(item)`` for each of *items*, run on a pool of at most *workers*
    threads, its outcomes handed back in the order of *items*.

    Used as ``with InOrder(read, items, workers) as outcomes:``, where
    ``outcomes[i].result()`` gives what ``read(items[i])`` returned, or
    raises what it raised. Taken in order, they tell what reading the items
    one after another would have told: the caller stops at the first that
    decides, as it did then.

    Leaving the block -- at the end, early, or on an exception, a Ctrl-C
    included -- reads no item not yet started, and waits for the reads under
    way to end: no read outlives the scan that asked for it. Reads are short
    (one domain's commands), so the wait is too. On a KeyboardInterrupt,
    which reaches the main thread alone, the commands the reads are running
    are stopped first, and the reads start no more (#294). The main thread
    waits a slice at a time (:data:`POLL_S`), for an outcome or on the way
    out, so that a Ctrl-C the kernel handed a read's thread is not left
    until a read ends (#309). While it waits, or hands out or cancels the
    items, a Ctrl-C begins the stop there and then, and its
    KeyboardInterrupt is raised only once the wait is over, or the items
    cancelled and the reads under way ended -- the stop of their commands
    bounds that -- so that it is never raised inside the pool's own
    workings, where it can leave an item neither read nor cancelled, or a
    lock held that a read needs to end (#311 review). A further Ctrl-C on
    the way out is the same stop: the KeyboardInterrupt raised is the first.

    With one worker, or one item, nothing runs on a thread: each item is
    read when its outcome is asked for, as before the pool.
    """

    def __init__(self, read: Callable[[Any], Any], items: Iterable[Any],
                 workers: int) -> None:
        self._read = read
        self._items = list(items)
        self._workers = workers
        self._pool: ThreadPoolExecutor | None = None

    def __enter__(self) -> list:
        if self._workers <= 1 or len(self._items) <= 1:
            return [_WhenAsked(self._read, item) for item in self._items]
        self._pool = ThreadPoolExecutor(
            max_workers=min(self._workers, len(self._items)),
            thread_name_prefix="boxman-scan")
        with ctrl_c_held_back() as ctrl_c:
            try:
                self._futures = [self._pool.submit(self._read, item)
                                 for item in self._items]
            except BaseException:
                self._pool.shutdown(wait=True, cancel_futures=True)
                raise
        if ctrl_c.taken:
            # the block is not entered, so not left: the reads are ended here
            self.__exit__(KeyboardInterrupt, KeyboardInterrupt(), None)
            raise KeyboardInterrupt
        return [_Outcome(future) for future in self._futures]

    def __exit__(self, *exc_info: Any) -> None:
        if self._pool is None:
            return
        stopping = exc_info[0] is not None and issubclass(exc_info[0], KeyboardInterrupt)
        with ctrl_c_held_back() as ctrl_c:
            if stopping:
                stop_commands()
            self._pool.shutdown(wait=False, cancel_futures=True)
            _wait_for_all(self._futures)
            # every read has ended: their threads' own ends are left
            self._pool.shutdown(wait=True)
        if ctrl_c.taken and not stopping:
            raise KeyboardInterrupt


class OnceEach:
    """
    A value per key, computed once however many threads ask for it at the
    same time: the first to ask computes it, the others wait for that one,
    and all get what it returned, or what it raised. A scan shares one
    between its domains where it read each thing once before the pool (the
    backing chain of a file several snapshots name).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[Any, Future] = {}

    def get(self, key: Any, compute: Callable[[], Any]) -> Any:
        """The value for *key*, computing it with *compute* if no thread has
        asked for it yet."""
        with self._lock:
            future = self._values.get(key)
            mine = future is None
            if mine:
                future = self._values[key] = Future()
        if not mine:
            return future.result()
        try:
            value = compute()
        except BaseException as exc:
            future.set_exception(exc)
            raise
        future.set_result(value)
        return value
