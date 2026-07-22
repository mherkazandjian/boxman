"""
Tests for the minimal-verbosity logging behaviour.

Covers the STATUS level, the level-aware ``ColoredFormatter``, the
verbosity helpers in :mod:`boxman.loggers.logger`, and the
``resolve_verbosity`` reconciliation in :mod:`boxman.scripts.cli_parser`.

No libvirt / provider machinery is exercised — these are pure
logging + argparse unit tests.
"""

import logging
import re

import pytest

from boxman.loggers.logger import (
    DEFAULT_LEVEL,
    STATUS,
    ColoredFormatter,
    is_verbose,
    logger,
    set_quiet,
    set_verbosity,
    suppressed,
)
from boxman.scripts.cli_parser import parse_args, resolve_verbosity

pytestmark = pytest.mark.unit


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Drop SGR colour escapes so assertions compare plain text."""
    return _ANSI_RE.sub("", text)


def _record(level, msg="hello world", *, pathname="mymod.py", lineno=42,
            func="myfunc"):
    """Build a bare LogRecord for the boxman logger at *level*."""
    return logging.LogRecord(
        name="boxman", level=level, pathname=pathname, lineno=lineno,
        msg=msg, args=(), exc_info=None, func=func,
    )


@pytest.fixture(autouse=True)
def _restore_logger_level():
    """Snapshot/restore the shared 'boxman' logger level around each test
    so verbosity mutations don't leak between tests."""
    prev = logger.level
    try:
        yield
    finally:
        logger.setLevel(prev)


class TestSetVerbosity:

    def test_zero_is_status_default(self):
        assert set_verbosity(0) == STATUS
        assert STATUS == DEFAULT_LEVEL
        assert logging.getLogger("boxman").level == STATUS

    def test_one_is_info(self):
        assert set_verbosity(1) == logging.INFO
        assert logging.getLogger("boxman").level == logging.INFO

    def test_two_is_debug(self):
        assert set_verbosity(2) == logging.DEBUG
        assert logging.getLogger("boxman").level == logging.DEBUG

    def test_high_count_clamps_to_debug(self):
        assert set_verbosity(5) == logging.DEBUG
        assert logging.getLogger("boxman").level == logging.DEBUG

    def test_negative_falls_back_to_default(self):
        assert set_verbosity(-1) == DEFAULT_LEVEL


class TestSetQuiet:

    def test_quiet_is_warning(self):
        assert set_quiet() == logging.WARNING
        assert logging.getLogger("boxman").level == logging.WARNING


class TestColoredFormatter:

    def test_status_has_no_bracket_prefix(self):
        fmt = ColoredFormatter(use_color=True)
        out = _strip_ansi(fmt.format(_record(STATUS, msg="all set")))
        assert "[" not in out
        assert out == "all set"

    def test_info_is_bare_message(self):
        fmt = ColoredFormatter(use_color=True)
        out = _strip_ansi(fmt.format(_record(logging.INFO, msg="detail line")))
        assert "[" not in out
        assert out == "detail line"

    def test_debug_has_diagnostic_prefix(self):
        fmt = ColoredFormatter(use_color=True)
        out = _strip_ansi(
            fmt.format(_record(logging.DEBUG, pathname="/x/y/mymod.py",
                               lineno=99, func="do_it")))
        # rich diagnostic: [time LEVEL file{line}:func()] message
        assert out.startswith("[")
        assert "mymod.py" in out
        assert "{99}" in out
        assert "do_it()" in out

    def test_warning_starts_with_level_tag(self):
        fmt = ColoredFormatter(use_color=True)
        out = _strip_ansi(fmt.format(_record(logging.WARNING, msg="careful")))
        assert out.startswith("WARNING")
        assert "careful" in out

    def test_error_starts_with_level_tag(self):
        fmt = ColoredFormatter(use_color=True)
        out = _strip_ansi(fmt.format(_record(logging.ERROR, msg="broke")))
        assert out.startswith("ERROR")
        assert "broke" in out


class TestSuppressed:

    def test_raises_then_restores_prior_level(self):
        set_verbosity(1)  # INFO
        prior = logger.level
        with suppressed():
            assert logger.level > prior
        assert logger.level == prior

    def test_restores_prior_level_on_exception(self):
        set_verbosity(0)  # STATUS
        prior = logger.level
        with pytest.raises(ValueError):
            with suppressed():
                assert logger.level > prior
                raise ValueError("boom")
        assert logger.level == prior


class TestIsVerbose:

    def test_debug_gate(self):
        set_verbosity(2)
        assert is_verbose(logging.DEBUG) is True
        set_verbosity(0)
        assert is_verbose(logging.DEBUG) is False


class TestResolveVerbosity:

    def test_flag_after_subcommand(self):
        parser = parse_args()
        args, _ = parser.parse_known_args(["up", "-vv"])
        assert args.verbose == 2
        assert resolve_verbosity(args) == 2

    def test_flag_before_subcommand(self):
        parser = parse_args()
        args, _ = parser.parse_known_args(["-vv", "up"])
        assert args.verbose_global == 2
        assert resolve_verbosity(args) == 2

    def test_no_flag_is_zero(self, monkeypatch):
        monkeypatch.delenv("BOXMAN_VERBOSITY", raising=False)
        parser = parse_args()
        args, _ = parser.parse_known_args(["up"])
        assert resolve_verbosity(args) == 0

    def test_env_fallback_when_no_flag(self, monkeypatch):
        monkeypatch.setenv("BOXMAN_VERBOSITY", "3")
        parser = parse_args()
        args, _ = parser.parse_known_args(["up"])
        assert resolve_verbosity(args) == 3

    def test_explicit_flag_beats_env(self, monkeypatch):
        monkeypatch.setenv("BOXMAN_VERBOSITY", "3")
        parser = parse_args()
        args, _ = parser.parse_known_args(["up", "-v"])
        # a given flag takes precedence over the env fallback
        assert resolve_verbosity(args) == 1
