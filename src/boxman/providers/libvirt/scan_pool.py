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

import queue
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from concurrent.futures import wait as wait_for
from typing import Any

from boxman.utils.shell import CtrlCsHeldBack, stop_commands

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


def _wait_for_all(futures: list[Future], held: CtrlCsHeldBack, enough: int) -> None:
    """Wait for every one of *futures*, a slice at a time (:data:`POLL_S`),
    so that a signal is handled meanwhile, or until *held* has taken
    *enough* Ctrl-Cs. A future the pool cancelled counts as ended, as
    ``done()`` says, though ``concurrent.futures.wait`` would not count it."""
    pending = [future for future in futures if not future.done()]
    while pending and held.taken < enough:
        wait_for(pending, timeout=POLL_S)
        pending = [future for future in pending if not future.done()]


class _Outcome:
    """A read's outcome, waited for a slice at a time (:data:`POLL_S`)."""

    def __init__(self, future: Future, held: CtrlCsHeldBack) -> None:
        self._future = future
        self._held = held

    def result(self) -> Any:
        # a Ctrl-C meanwhile begins the stop there and then, and is raised
        # here, once the wait holds nothing (#311 review)
        before = self._held.taken
        with self._held:
            _wait_for_all([self._future], self._held, before + 1)
        if self._held.taken > before:
            raise KeyboardInterrupt
        return self._future.result()


class _Reads:
    """
    The threads a scan reads its items on: each takes the next item, and
    reads it unless it was cancelled. Daemon threads: a read that a second
    Ctrl-C leaves running (:meth:`InOrder.__exit__`) does not hold boxman's
    end up, its commands left to the stop, and to the guardian (#311
    review). ThreadPoolExecutor's threads are all waited for as Python
    exits.
    """

    def __init__(self, read: Callable[[Any], Any], items: list[Any]) -> None:
        self._read = read
        self.futures: list[Future] = [Future() for _ in items]
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        for job in zip(self.futures, items, strict=True):
            self._queue.put(job)
        self._threads: list[threading.Thread] = []

    def start(self, workers: int) -> None:
        for number in range(workers):
            thread = threading.Thread(target=self._work, name=f"boxman-scan_{number}",
                                      daemon=True)
            thread.start()
            self._threads.append(thread)

    def _work(self) -> None:
        while True:
            try:
                future, item = self._queue.get_nowait()
            except queue.Empty:
                return
            if not future.set_running_or_notify_cancel():
                continue
            try:
                value = self._read(item)
            except BaseException as error:
                future.set_exception(error)
                del error
            else:
                future.set_result(value)

    def cancel(self) -> None:
        """Cancel each item not yet started: one taken from the queue too."""
        for future in self.futures:
            future.cancel()

    def join(self) -> None:
        for thread in self._threads:
            thread.join()


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
    way to end: no read outlives the scan that asked for it, unless a second
    Ctrl-C asks boxman to leave them (below). Reads are short
    (one domain's commands), so the wait is too. On a KeyboardInterrupt,
    which reaches the main thread alone, the commands the reads are running
    are stopped first, and the reads start no more (#294). The main thread
    waits a slice at a time (:data:`POLL_S`), for an outcome or on the way
    out, so that a Ctrl-C the kernel handed a read's thread is not left
    until a read ends (#309).

    While it waits, or hands out or cancels the items, a Ctrl-C (under
    :func:`~boxman.utils.shell.interrupt_on_signals`) begins the stop there
    and then, and its KeyboardInterrupt is raised where nothing of the pool
    is held: raised inside the pool's own workings, it can leave an item
    neither read nor cancelled, or a lock held that a read needs to end
    (#311 review). The first Ctrl-C is raised from the outcome waited for,
    and the way out then cancels the items not yet started and waits for
    the reads under way. A second, during that wait, ends it once the
    items are cancelled: the KeyboardInterrupt is raised at once, and the
    reads still running are left to the stop, which ends their commands.

    With one worker, or one item, nothing runs on a thread: each item is
    read when its outcome is asked for, as before the pool.
    """

    def __init__(self, read: Callable[[Any], Any], items: Iterable[Any],
                 workers: int) -> None:
        self._read = read
        self._items = list(items)
        self._workers = workers
        self._pool: _Reads | None = None

    def __enter__(self) -> list:
        if self._workers <= 1 or len(self._items) <= 1:
            return [_WhenAsked(self._read, item) for item in self._items]
        self._held = CtrlCsHeldBack()
        self._pool = _Reads(self._read, self._items)
        try:
            with self._held:
                self._pool.start(min(self._workers, len(self._items)))
                outcomes = [_Outcome(future, self._held) for future in self._pool.futures]
            if self._held.taken:
                raise KeyboardInterrupt
            return outcomes
        except BaseException as error:
            # the block is not entered, so not left: the items are
            # cancelled, and the reads under way waited for, here (#311
            # review: a Ctrl-C as the outcomes were made left them reading)
            self.__exit__(type(error), error, error.__traceback__)
            raise

    def __exit__(self, *exc_info: Any) -> None:
        if self._pool is None:
            return
        with self._held:
            stopping = exc_info[0] is not None and issubclass(exc_info[0], KeyboardInterrupt)
            if stopping:
                # the first Ctrl-C, however it reached the block
                self._held.taken = max(self._held.taken, 1)
                stop_commands()
            before = self._held.taken
            self._pool.cancel()
            # until the reads under way end, or a second Ctrl-C: those still
            # running are then left to the stop
            _wait_for_all(self._pool.futures, self._held, 2)
            if all(future.done() for future in self._pool.futures):
                self._pool.join()
        if self._held.taken > before and not stopping:
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
