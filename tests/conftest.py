"""
Shared pytest fixtures and helpers for the boxman test suite.

Fixtures:

    captured_logs    — thin wrapper around pytest's ``caplog`` that attaches
                       to boxman's module-level ``log`` singleton, so tests
                       can assert on what the code logged.
    host_commands    — the fake ``docker``, ``virsh`` and ``sudo`` every test
                       outside the integration tier runs against (see
                       :class:`HostCommandGuard`), for a test that calls them
                       on purpose.

Helpers (plain importable functions, not fixtures):

    make_bare_manager — a ``BoxmanManager`` built via ``__new__`` (constructor
                       bypassed, so no config files are loaded) with an
                       in-memory config dict and a mocked logger, for unit
                       tests that exercise manager methods in isolation.
"""

from __future__ import annotations

import itertools
import logging
import os
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from boxman.manager import BoxmanManager

# ---------------------------------------------------------------------------
# Captured logs — attach caplog to boxman's module-level singleton
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """
    Configure ``caplog`` to capture records from the ``boxman`` logger.

    The boxman package exposes a module-level ``log`` (see
    ``src/boxman/__init__.py`` → ``loggers/logger.py``) with
    ``propagate = False``, which means pytest's default ``caplog`` misses
    its records. This fixture re-enables propagation for the duration of
    the test so assertions against log output work.
    """
    boxman_logger = logging.getLogger("boxman")
    previous_propagate = boxman_logger.propagate
    boxman_logger.propagate = True
    caplog.set_level(logging.DEBUG, logger="boxman")
    try:
        yield caplog
    finally:
        boxman_logger.propagate = previous_propagate


# ---------------------------------------------------------------------------
# boxman's per-user cache dir — never the real one
# ---------------------------------------------------------------------------

_cache_dirs = itertools.count()


@pytest.fixture(autouse=True)
def _private_boxman_cache_dir(tmp_path_factory: pytest.TempPathFactory,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Point boxman's per-user cache dir (``~/.config/boxman/cache``) at a
    directory of this test's own, not created until something writes there.

    A VM teardown records there where it saved its inventory (#208), and a
    registered project lands there too; neither may reach the real one, nor
    leak from one test into the next. Tests that patch
    ``DEFAULT_CACHE_DIR`` themselves still win.
    """
    monkeypatch.setattr(
        "boxman.config_cache.DEFAULT_CACHE_DIR",
        str(tmp_path_factory.getbasetemp() / "boxman-cache"
            / str(next(_cache_dirs))))


# ---------------------------------------------------------------------------
# docker, virsh and sudo — for the integration tier only
# ---------------------------------------------------------------------------

class HostCommandGuard:
    """
    Fakes of the commands that act on this machine, first on ``PATH`` around
    every test that is not marked ``integration``.

    ``docker`` and ``virsh`` reach its container runtime and its libvirt.
    Default-tier tests used to run both unmocked, so they passed or failed
    depending on the host. Some went further: on a machine with a boxman
    container under the default name, four would have stopped it and copied
    state out of it, and one would have written disk records into any domain
    named ``vm01`` (#214). ``sudo`` is here because it resets ``PATH``, so
    ``sudo docker`` or ``sudo virsh`` would bypass the other two.

    A fake runs nothing: it records the call and exits 1. A test that made a
    call fails at teardown, listing the calls. The fakes go on ``PATH``
    before any of the test's fixtures are set up, so a call from a module-
    or class-scoped fixture is caught too, and every subprocess the test
    starts inherits them. Collection happens before any test and is not
    guarded: two integration modules ask ``docker compose version`` there
    to decide their skips, which only reads the client's version.
    """

    COMMANDS = ("docker", "virsh", "sudo")

    #: A fake. ``{name}`` is the command, ``{log}`` the file it records to.
    FAKE = (
        "#!/bin/sh\n"
        "printf '{name} %s\\n' \"$*\" >>{log}\n"
        "echo 'boxman tests: {name} is for the integration tier only; this "
        "call was not run (tests/conftest.py)' >&2\n"
        "exit 1\n"
    )

    def __init__(self) -> None:
        self.bindir = Path(tempfile.mkdtemp(prefix="boxman-tests-bin-"))
        self.log = self.bindir / "calls.log"
        for name in self.COMMANDS:
            fake = self.bindir / name
            fake.write_text(self.FAKE.format(
                name=name, log=shlex.quote(str(self.log))))
            fake.chmod(0o755)
        self._saved_path: str | None = None
        self._installed = False

    def install(self) -> None:
        """Put the fakes first on ``PATH``, with no calls recorded yet."""
        self.take()
        self._saved_path = os.environ.get("PATH")
        os.environ["PATH"] = os.pathsep.join(
            [str(self.bindir), self._saved_path or os.defpath])
        self._installed = True

    def uninstall(self) -> list[str]:
        """Restore ``PATH``, and return the calls made since :meth:`install`."""
        if not self._installed:
            return []
        self._installed = False
        if self._saved_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self._saved_path
        return self.take()

    def take(self) -> list[str]:
        """The calls recorded so far, which are then forgotten."""
        try:
            calls = self.log.read_text().splitlines()
        except FileNotFoundError:
            return []
        self.log.unlink()
        return calls


_HOST_COMMANDS = pytest.StashKey[HostCommandGuard]()


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_HOST_COMMANDS] = HostCommandGuard()


def pytest_unconfigure(config: pytest.Config) -> None:
    guard = config.stash.get(_HOST_COMMANDS, None)
    if guard is not None:
        shutil.rmtree(guard.bindir, ignore_errors=True)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item):
    # before the item's fixtures, which the inner hooks set up
    if item.get_closest_marker("integration") is None:
        item.config.stash[_HOST_COMMANDS].install()
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    try:
        return (yield)
    finally:
        # after the item's fixtures are torn down, which may run them too
        calls = item.config.stash[_HOST_COMMANDS].uninstall()
        if calls:
            pytest.fail(
                "this test ran docker, virsh or sudo outside the integration "
                "tier. Each call below reached a fake and ran nothing; the "
                "real command acts on this machine's containers, its "
                "libvirt, or as root (#214). Mock it, or mark the test "
                "'integration':\n    " + "\n    ".join(calls),
                pytrace=False)


@pytest.fixture
def host_commands(request: pytest.FixtureRequest) -> HostCommandGuard:
    """
    The guard, for a test that runs a fake on purpose: it takes its calls
    back with :meth:`HostCommandGuard.take` before teardown.
    """
    return request.config.stash[_HOST_COMMANDS]


# ---------------------------------------------------------------------------
# Bare manager — the BoxmanManager.__new__ bypass shared by unit tests
# ---------------------------------------------------------------------------

def make_bare_manager(config: dict[str, Any] | None = None) -> BoxmanManager:
    """
    Return a bare ``BoxmanManager`` with an in-memory config dict.

    The constructor is bypassed (``__new__`` only), so no config files are
    loaded and no provider/runtime is created; only the attributes unit
    tests rely on are populated. Callers set any further attributes
    (``provider``, …) on the returned instance as needed.
    """
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = config
    mgr.config_path = None
    mgr.logger = MagicMock()
    mgr._netlab = None
    return mgr


# ---------------------------------------------------------------------------
# What `virsh list` prints — for mocked virsh calls
# ---------------------------------------------------------------------------

def domain_uuid(name: str) -> str:
    """A stable UUID for the test domain *name*."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"libvirt-domain:{name}"))


def domain_listing(args: tuple, *domains: str | tuple[str, str]) -> str:
    """
    What ``virsh list`` prints for *domains* when called with *args*: a
    ``<uuid> <name>`` line each when they ask for ``--uuid`` (with
    ``--name``), one name per line otherwise. A domain is its name, whose
    UUID is :func:`domain_uuid`'s, or a ``(uuid, name)`` pair — a domain
    listed under a name that is not its first, or a new domain under an
    old name.
    """
    lines = []
    for domain in domains:
        uid, name = (domain if isinstance(domain, tuple)
                     else (domain_uuid(domain), domain))
        lines.append(f"{uid} {name}" if "--uuid" in args else name)
    return "".join(f"{line}\n" for line in lines)
