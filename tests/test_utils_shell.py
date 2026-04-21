"""
Unit tests for boxman.utils.shell.

Pins the critical invariant: every call to :func:`boxman.utils.shell.run`
passes ``in_stream=False`` unless the caller explicitly overrides it.
A regression on this would re-introduce the pytest stdin-capture
deadlock / ``OSError: reading from stdin while output is captured`` that
Phase 2.8's follow-up fixed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from boxman.utils.shell import run as shell_run


pytestmark = pytest.mark.unit


class TestInStreamDefault:

    def test_in_stream_defaults_to_false(self):
        with patch("boxman.utils.shell.invoke.run") as mock_run:
            mock_run.return_value = MagicMock()
            shell_run("echo hi")
        _args, kwargs = mock_run.call_args
        assert kwargs["in_stream"] is False

    def test_caller_can_override_in_stream(self):
        with patch("boxman.utils.shell.invoke.run") as mock_run:
            mock_run.return_value = MagicMock()
            shell_run("echo hi", in_stream=None)
        _args, kwargs = mock_run.call_args
        assert kwargs["in_stream"] is None

    def test_other_kwargs_passed_through(self):
        with patch("boxman.utils.shell.invoke.run") as mock_run:
            mock_run.return_value = MagicMock()
            shell_run("echo hi", hide=True, warn=True, timeout=30)
        _args, kwargs = mock_run.call_args
        assert kwargs["hide"] is True
        assert kwargs["warn"] is True
        assert kwargs["timeout"] == 30
        assert kwargs["in_stream"] is False

    def test_command_passed_positionally(self):
        with patch("boxman.utils.shell.invoke.run") as mock_run:
            mock_run.return_value = MagicMock()
            shell_run("virsh list")
        args, _kwargs = mock_run.call_args
        assert args[0] == "virsh list"

    def test_returns_invoke_result(self):
        sentinel = object()
        with patch("boxman.utils.shell.invoke.run", return_value=sentinel):
            assert shell_run("echo x") is sentinel


class TestCompatibleUnderPytestCapture:
    """
    Sanity: calling shell_run() inside pytest's default capture mode
    must not raise. Before the wrapper, this would explode with
    ``OSError: pytest: reading from stdin while output is captured!``.
    """

    def test_does_not_raise_under_capture(self):
        # Run a no-op command; relies on /bin/true being universally present.
        result = shell_run("true")
        assert result.ok


class TestCommandsMigrationStatic:
    """
    Guard against anyone re-introducing a raw ``invoke.run(`` call in
    source modules that used to use it. The wrapper exists so the whole
    library is test-framework-safe; a fresh raw call would silently
    re-break capture-mode tests.
    """

    def test_no_raw_invoke_run_in_src(self):
        import pathlib
        src_root = pathlib.Path(__file__).resolve().parent.parent / "src"
        offenders: list[str] = []
        for py in src_root.rglob("*.py"):
            # The wrapper itself is the only allowed home of invoke.run(
            if py.name == "shell.py" and py.parent.name == "utils":
                continue
            text = py.read_text()
            if "invoke.run(" in text:
                offenders.append(str(py.relative_to(src_root)))
        assert not offenders, (
            "these modules still call invoke.run() directly — route them "
            f"through boxman.utils.shell.run: {offenders}"
        )
