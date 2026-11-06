"""
One revert at a time of the VMs whose disks read one image (#303): the
lock per overlay name a revert can compute, and the second it waits for
(:mod:`boxman.providers.libvirt.revert_locks`).
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from boxman.providers.libvirt import revert_locks
from boxman.providers.libvirt.revert_locks import (
    RevertLock,
    RevertLockTimeoutError,
    overlay_key,
)

pytestmark = pytest.mark.unit


class _Clock:
    """The host's clock, moved only by what waits on it."""

    def __init__(self, now: float) -> None:
        self.now = now
        self.waits: list[float] = []
        self._lock = threading.Lock()

    def time(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.waits.append(seconds)
            self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    clock = _Clock(1793626526.25)
    monkeypatch.setattr(revert_locks, "_now", clock.time)
    monkeypatch.setattr(revert_locks, "_wait", clock.sleep)
    return clock


class TestTheOverlayNameARevertGives:
    """libvirt names a revert's overlay of a disk ``<stem>.<epoch>``
    beside the image the disk reads: the key is that directory and stem."""

    def test_the_last_suffix_is_dropped(self, tmp_path):
        assert overlay_key(str(tmp_path / "nixos.iso")) == str(
            tmp_path / "nixos")

    def test_two_images_with_one_stem_in_one_directory_share_it(
            self, tmp_path):
        assert overlay_key(str(tmp_path / "nixos.iso")) == overlay_key(
            str(tmp_path / "nixos.img"))

    def test_an_overlay_a_revert_made_of_the_image_shares_it(self, tmp_path):
        assert overlay_key(str(tmp_path / "nixos.iso")) == overlay_key(
            str(tmp_path / "nixos.1793626526"))

    def test_one_directory_under_two_names_is_one(self, tmp_path):
        (tmp_path / "cache").mkdir()
        (tmp_path / "alias").symlink_to(tmp_path / "cache")
        assert overlay_key(str(tmp_path / "alias" / "nixos.iso")) == (
            overlay_key(str(tmp_path / "cache" / "nixos.iso")))

    def test_images_in_two_directories_do_not(self, tmp_path):
        assert overlay_key(str(tmp_path / "a" / "nixos.iso")) != overlay_key(
            str(tmp_path / "b" / "nixos.iso"))


class TestTheLock:

    def test_no_keys_take_no_lock_and_make_no_file(self, clock):
        with RevertLock([]):
            pass
        assert not os.path.exists(revert_locks.lock_dir())

    def test_a_new_lock_waits_for_nothing(self, clock):
        with RevertLock(["/cache/nixos"]):
            pass
        assert clock.waits == []

    def test_the_next_holder_waits_for_the_second_after_the_last_left(
            self, clock):
        with RevertLock(["/cache/nixos"]):
            pass
        # left at .25 past the second: the next may start at the next one
        with RevertLock(["/cache/nixos"]):
            assert clock.time() == 1793626527.0
        assert clock.waits == [0.75]

    def test_a_holder_a_second_later_waits_for_nothing(self, clock):
        with RevertLock(["/cache/nixos"]):
            pass
        clock.now += 1.0
        with RevertLock(["/cache/nixos"]):
            pass
        assert clock.waits == []

    def test_another_key_does_not_wait(self, clock):
        with RevertLock(["/cache/nixos"]):
            pass
        with RevertLock(["/cache/guix"]):
            pass
        assert clock.waits == []

    def test_a_stamp_that_does_not_read_as_one_waits_a_second(self, clock):
        os.makedirs(revert_locks.lock_dir())
        with open(RevertLock.path_for("/cache/nixos"), "w") as fh:
            fh.write("garbled")
        with RevertLock(["/cache/nixos"]):
            pass
        assert clock.waits == [0.75]

    def test_a_clock_set_back_waits_a_second_at_most(self, clock):
        with RevertLock(["/cache/nixos"]):
            pass
        clock.now -= 3600
        with RevertLock(["/cache/nixos"]):
            pass
        assert clock.waits == [1.0]

    def test_a_lock_held_elsewhere_is_waited_for(self, clock):
        """Two holders never hold one key at once: the restore workers of
        one command are processes of their own, and each opens the lock
        file itself, as these threads do."""
        inside = threading.Event()
        leave = threading.Event()
        waited: list[str] = []

        def first():
            with RevertLock(["/cache/nixos"]):
                inside.set()
                leave.wait(5)
        thread = threading.Thread(target=first)
        thread.start()
        inside.wait(5)
        threading.Timer(0.2, leave.set).start()
        started = time.monotonic()
        with RevertLock(["/cache/nixos"], on_wait=waited.append):
            assert leave.is_set()
        thread.join(5)
        assert waited == ["/cache/nixos"]
        assert time.monotonic() - started >= 0.15

    def test_one_held_too_long_times_out_holding_nothing(self, clock):
        holder = RevertLock(["/cache/a", "/cache/nixos"]).__enter__()
        try:
            with pytest.raises(RevertLockTimeoutError, match="cache/nixos"):
                with RevertLock(["/cache/guix", "/cache/nixos"],
                                timeout=0.1):
                    pass
            # what it took before giving up, it let go of
            with RevertLock(["/cache/guix"], timeout=0.1):
                pass
        finally:
            holder.__exit__(None, None, None)

    def test_keys_are_taken_in_order_so_two_holders_never_wait_in_a_circle(
            self, clock):
        errors: list[BaseException] = []

        def take(keys):
            try:
                for _ in range(20):
                    with RevertLock(keys, timeout=10):
                        pass
            except BaseException as exc:  # pragma: no cover - a failure
                errors.append(exc)
        threads = [threading.Thread(target=take, args=(keys,)) for keys in
                   (["/cache/a", "/cache/b"], ["/cache/b", "/cache/a"])]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert errors == [] and not any(t.is_alive() for t in threads)

    def test_a_stamp_that_cannot_be_written_is_waited_out_by_the_holder(
            self, clock, monkeypatch):
        def no_space(*_a):
            raise OSError(28, "No space left on device")
        lock = RevertLock(["/cache/nixos"]).__enter__()
        monkeypatch.setattr(revert_locks.os, "pwrite", no_space)
        lock.__exit__(None, None, None)
        assert clock.waits == [0.75]
