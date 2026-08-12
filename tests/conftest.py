"""
Shared pytest fixtures and helpers for the boxman test suite.

Fixtures:

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

import logging
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
