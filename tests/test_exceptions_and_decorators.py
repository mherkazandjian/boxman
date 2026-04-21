"""
Unit tests for boxman.exceptions + boxman.utils.decorators.

Part of Phase 2.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import pytest

from boxman.exceptions import (
    BoxmanError,
    ConfigError,
    NetworkError,
    ProvisionError,
    RuntimeUnavailable,
    SnapshotError,
    TemplateError,
)
from boxman.utils.decorators import safe_execute


pytestmark = pytest.mark.unit


class TestExceptionHierarchy:

    def test_all_subclass_boxman_error(self):
        for cls in (
            ConfigError, ProvisionError, NetworkError, TemplateError,
            SnapshotError, RuntimeUnavailable,
        ):
            assert issubclass(cls, BoxmanError)

    def test_network_and_template_errors_are_provision_errors(self):
        # Downstream code can `except ProvisionError` and catch both.
        assert issubclass(NetworkError, ProvisionError)
        assert issubclass(TemplateError, ProvisionError)

    def test_snapshot_error_is_not_provision_error(self):
        """Snapshot failures are distinct from provision failures so
        ``except ProvisionError`` doesn't silently swallow them."""
        assert not issubclass(SnapshotError, ProvisionError)

    def test_runtime_unavailable_is_not_provision_error(self):
        """Runtime outages are often retriable — keep them distinct."""
        assert not issubclass(RuntimeUnavailable, ProvisionError)

    def test_chained_exceptions_preserve_cause(self):
        """Raising with ``from exc`` must preserve the original in __cause__."""
        original = ValueError("original problem")
        try:
            try:
                raise original
            except ValueError as exc:
                raise ProvisionError("wrapping") from exc
        except ProvisionError as wrapped:
            assert wrapped.__cause__ is original


class TestSafeExecute:

    def test_returns_function_value_on_success(self):
        @safe_execute(fallback=None)
        def add(a, b):
            return a + b

        assert add(2, 3) == 5

    def test_returns_fallback_on_exception(self):
        @safe_execute(fallback=False)
        def bad():
            raise RuntimeError("nope")

        assert bad() is False

    def test_fallback_can_be_any_type(self):
        @safe_execute(fallback=[])
        def bad():
            raise RuntimeError("x")

        assert bad() == []

        @safe_execute(fallback="default")
        def worse():
            raise RuntimeError("x")

        assert worse() == "default"

    def test_narrows_catch_to_specific_types(self):
        @safe_execute(fallback=None, catch=RuntimeError)
        def raise_value_error():
            raise ValueError("wrong type")

        # ValueError is NOT in catch → should propagate
        with pytest.raises(ValueError):
            raise_value_error()

    def test_catch_accepts_a_tuple_of_types(self):
        @safe_execute(fallback=None, catch=(RuntimeError, OSError))
        def raise_os_error():
            raise OSError("disk full")

        assert raise_os_error() is None

    def test_does_not_swallow_keyboard_interrupt(self):
        @safe_execute(fallback=None)
        def user_pressed_ctrl_c():
            raise KeyboardInterrupt()

        # KeyboardInterrupt is a BaseException, not Exception → not caught
        with pytest.raises(KeyboardInterrupt):
            user_pressed_ctrl_c()

    def test_logs_with_configured_level(self, captured_logs):
        import logging

        @safe_execute(fallback=False, log_level="warning", message="bad thing")
        def bad():
            raise RuntimeError("boom")

        bad()
        records = [r for r in captured_logs.records if "bad thing" in r.message]
        assert records
        # Use levelno because the ColoredFormatter mutates levelname with ANSI codes
        assert records[0].levelno == logging.WARNING
        assert "RuntimeError" in records[0].message
        assert "boom" in records[0].message

    def test_default_message_falls_back_to_qualname(self, captured_logs):
        @safe_execute(fallback=None)
        def failing_op():
            raise RuntimeError("x")

        failing_op()
        # qualname includes the function name
        assert any("failing_op" in r.message for r in captured_logs.records)

    def test_preserves_name_and_docstring(self):
        @safe_execute(fallback=None)
        def documented():
            """Does a thing."""
            return 1

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "Does a thing."

    def test_passes_args_and_kwargs_through(self):
        @safe_execute(fallback=None)
        def concat(a, b, sep=" "):
            return f"{a}{sep}{b}"

        assert concat("hello", "world") == "hello world"
        assert concat("hello", "world", sep="-") == "hello-world"

    def test_works_on_methods_with_self(self):
        class C:
            @safe_execute(fallback="oops")
            def greet(self, name):
                if not name:
                    raise ValueError("empty")
                return f"hi {name}"

        c = C()
        assert c.greet("alice") == "hi alice"
        assert c.greet("") == "oops"


class TestVirshEditRaisesProvisionError:
    """Integration: virsh_edit.get_domain_xml now raises ProvisionError,
    not bare RuntimeError, on failure."""

    def test_runtime_error_is_wrapped_in_provision_error(self):
        from unittest.mock import patch, MagicMock
        from boxman.providers.libvirt.virsh_edit import VirshEdit

        editor = VirshEdit(provider_config={"use_sudo": False})
        with patch.object(editor.virsh, "execute",
                          side_effect=RuntimeError("virsh died")):
            with pytest.raises(ProvisionError) as excinfo:
                editor.get_domain_xml("vm01")
        # The original RuntimeError is preserved as the cause
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "vm01" in str(excinfo.value)
