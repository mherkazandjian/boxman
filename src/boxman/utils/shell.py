"""
Thin wrapper around :func:`invoke.run` that defaults to
``in_stream=False``.

Why
---
Boxman's subprocess calls are all non-interactive — virsh, virt-clone,
qemu-img, docker compose, rsync, etc. Without ``in_stream=False``,
:class:`invoke.runners.Runner` tries to attach a stdin pump to
``sys.stdin``, which fails under any caller that captures stdin
(pytest's default output capture is the most common offender; CI
environments without a TTY are another). The failure modes vary:

- pytest runs the command successfully but crashes during teardown
  with ``OSError: pytest: reading from stdin while output is
  captured!``.
- CI jobs without a TTY deadlock waiting for stdin that never closes.

Defaulting ``in_stream=False`` disables the stdin pump. Interactive
callers (there are none today, but keep the override path open) can
still pass a non-False stream explicitly.

Added in Phase 2.8 follow-up (see /home/mher/.claude/plans/).
"""

from __future__ import annotations

from typing import Any

import invoke


def run(command: str, **kwargs: Any) -> invoke.runners.Result:
    """
    Run *command* via :func:`invoke.run`, defaulting ``in_stream=False``.

    The signature is intentionally narrower than ``invoke.run`` — it
    accepts the same kwargs and passes them straight through; ``in_stream``
    defaults to ``False`` and can be overridden by passing a different
    value explicitly.

    Prefer this over ``invoke.run`` and ``from invoke import run`` in
    new code so the library stays test-framework- and CI-friendly.
    """
    kwargs.setdefault("in_stream", False)
    return invoke.run(command, **kwargs)
