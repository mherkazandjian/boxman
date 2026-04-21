"""
Decorators for boxman internals.

Currently just :func:`safe_execute`, which collapses the
"try/except/log/return sentinel" pattern that repeats across
``providers/libvirt/snapshot.py``, ``cloudinit.py``, and a handful of
other helper methods.

Added in Phase 2.2 of the review plan (see /home/mher/.claude/plans/).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable
from typing import Any

from boxman import log


def safe_execute(
    *,
    fallback: Any = False,
    catch: type[BaseException] | Iterable[type[BaseException]] = Exception,
    log_level: str = "error",
    message: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Wrap a method so that listed exceptions are logged and swallowed,
    and *fallback* is returned instead.

    This replaces the repetitive pattern::

        try:
            ...
            return True
        except Exception as exc:
            self.logger.error(f"error doing X: {exc}")
            return False

    with a single decorator usage::

        @safe_execute(fallback=False, message="failed to take snapshot")
        def create_snapshot(self, ...):
            ...
            return True

    Args:
        fallback: Value returned when a caught exception occurs
                  (e.g. ``False`` for success flags, ``[]`` for list
                  accumulators, ``None`` where no value is expected).
        catch:    Exception type or tuple of types to swallow. Defaults to
                  :class:`Exception` — narrow it (e.g. ``(RuntimeError,
                  OSError)``) whenever you can so programming errors still
                  surface.
        log_level: One of ``"error"``, ``"warning"``, ``"info"``,
                   ``"debug"`` — how to log the caught exception.
        message:  Optional prefix for the log message. When omitted, the
                  function's ``__qualname__`` is used.

    Returns:
        A decorator that applies the behaviour to the wrapped function.

    Notes:
        - ``BaseException`` subclasses like ``KeyboardInterrupt`` and
          ``SystemExit`` are **never** caught by default; explicitly
          include them in ``catch`` if you really mean it.
        - The wrapped function's name / docstring / module are preserved
          via :func:`functools.wraps`.
    """
    if isinstance(catch, type):
        catch_types: tuple[type[BaseException], ...] = (catch,)
    else:
        catch_types = tuple(catch)

    log_fn = getattr(log, log_level, log.error)

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except catch_types as exc:
                prefix = message or fn.__qualname__
                log_fn(f"{prefix}: {exc.__class__.__name__}: {exc}")
                return fallback
        return wrapper

    return decorator
