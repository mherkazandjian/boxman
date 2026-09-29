"""
Shared pytest fixtures and helpers for the boxman test suite.

Fixtures:

    private_tempdir  — points :mod:`tempfile` at a directory of the test's
                       own, for a test that checks what it left behind there.
    captured_logs    — thin wrapper around pytest's ``caplog`` that attaches
                       to boxman's module-level ``log`` singleton, so tests
                       can assert on what the code logged.

Helpers (plain importable functions, not fixtures):

    make_bare_manager — a ``BoxmanManager`` built via ``__new__`` (constructor
                       bypassed, so no config files are loaded) with an
                       in-memory config dict and a mocked logger, for unit
                       tests that exercise manager methods in isolation.
"""

from __future__ import annotations

import itertools
import logging
import uuid
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


# ---------------------------------------------------------------------------
# tempfile's directory — one of the test's own
# ---------------------------------------------------------------------------

@pytest.fixture
def private_tempdir(tmp_path_factory: pytest.TempPathFactory,
                    monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Point :mod:`tempfile` at a directory of this test's own, so a check for
    what the test left under ``tempfile.gettempdir()`` sees only its own.

    Against the machine's temp dir, such a check also saw another suite
    running on the same machine at the same moment, which made the same
    staging directories and removed them a moment later (#231).
    """
    monkeypatch.setattr("tempfile.tempdir",
                        str(tmp_path_factory.mktemp("tempdir")))
