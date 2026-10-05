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

Added in a Phase 2.8 follow-up of the engineering review plan.
"""

from __future__ import annotations

import signal
import subprocess
from typing import Any

import invoke


class _StoppingLocal(invoke.runners.Local):
    """invoke's local runner, made to stop a command on a Ctrl-C.

    invoke answers a KeyboardInterrupt by writing ``\\x03`` to the command's
    stdin and waiting on: right for an editor or a REPL, but a command that
    does not read its stdin, as a download does not, runs to its end, and
    the interrupt is lost. Only a SIGINT to the whole process group, as from
    a terminal's Ctrl-C, reached the command itself (#227). This runner
    sends the command SIGINT, and raises the KeyboardInterrupt once the
    command has ended.
    """

    #: how long a command has to end after its SIGINT, before it is killed
    STOP_TIMEOUT = 10

    def send_interrupt(self, interrupt: KeyboardInterrupt) -> None:
        self.interrupted = True
        process = getattr(self, "process", None)
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)

    def _stop(self) -> None:
        """Send the command SIGINT, and wait for it to end (#227 review).

        It is killed if it has not ended in time, or if another Ctrl-C
        comes while it ends; either way, it has ended when this returns.
        """
        process = getattr(self, "process", None)
        if process is None or process.poll() is not None:
            return
        try:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=self.STOP_TIMEOUT)
            return
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            pass
        while True:
            try:
                process.kill()
                process.wait()
                return
            except KeyboardInterrupt:
                continue  # it is being killed: gone in a moment

    def run(self, command: str, **kwargs: Any) -> invoke.runners.Result:
        self.interrupted = False
        try:
            result = super().run(command, **kwargs)
        except invoke.exceptions.UnexpectedExit:
            if self.interrupted:
                raise KeyboardInterrupt from None
            raise
        except KeyboardInterrupt:
            # one invoke did not pass on: it came after the command started
            # and before invoke began to wait for it, so the command would
            # run on without boxman
            self._stop()
            raise
        if self.interrupted:
            raise KeyboardInterrupt
        return result


def run_stoppable(command: str, **kwargs: Any) -> invoke.runners.Result:
    """
    :func:`run`, except that a Ctrl-C stops *command* and is raised.

    For a command that would otherwise ride out a Ctrl-C (see
    :class:`_StoppingLocal`). *command* is a single program and its
    arguments: the shell is told to ``exec`` it, so that the SIGINT reaches
    the program itself. Left to itself, a shell keeps itself between them
    whenever it has something to do afterwards, an EXIT trap from a
    ``BASH_ENV`` file for one, and the program rides the SIGINT out.
    """
    kwargs.setdefault("in_stream", False)
    config = invoke.Config(overrides={"runners": {"local": _StoppingLocal}})
    return invoke.Context(config).run(f"exec {command}", **kwargs)


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
