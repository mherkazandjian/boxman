"""
Run the commands boxman starts: :func:`invoke.run`, with two defaults
changed.

``in_stream=False``
-------------------
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

A Ctrl-C stops the command
--------------------------
invoke answers a Ctrl-C by writing ``\\x03`` to the command's stdin, waits
on, and returns the command's result as if nothing had happened: boxman
went on to its next step, and exited 0 (#294). Every command runs on
:class:`_StoppingLocal` instead, which stops it, and raises the
KeyboardInterrupt once it has ended.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Iterable, Iterator
from typing import Any

import invoke


def _has_terminal() -> bool:
    """Whether this process has a controlling terminal."""
    try:
        os.close(os.open("/dev/tty", os.O_RDONLY))
    except OSError:
        return False
    return True


def _stat(pid: int) -> list[str] | None:
    """The fields of ``/proc/<pid>/stat`` after the program's name: the
    state first, the parent's PID second and the start time 20th; or None,
    for a process that is not there."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            # the name, in parentheses, may hold either itself
            fields = stat.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return fields if len(fields) > 19 else None


def _tree(pid: int) -> dict[int, str]:
    """*pid*, and every process below it, each with its start time, which
    tells it from a process given its PID later."""
    try:
        names = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        names = [pid]
    stats = {each: fields for each in names if (fields := _stat(each))}
    children: dict[int, list[int]] = {}
    for each, fields in stats.items():
        children.setdefault(int(fields[1]), []).append(each)
    found = [pid] if pid in stats else []
    for each in found:
        found.extend(children.get(each, ()))
    return {each: stats[each][19] for each in found}


def _alive(pid: int, start: str) -> bool:
    """Whether the process that had *pid* at *start* is there, unreaped
    zombies aside."""
    fields = _stat(pid)
    return fields is not None and fields[19] == start and fields[0] != "Z"


class _StoppingLocal(invoke.runners.Local):
    """invoke's local runner, made to stop a command on a Ctrl-C.

    invoke answers a KeyboardInterrupt by writing ``\\x03`` to the command's
    stdin and waiting on: right for an editor or a REPL, but a command that
    does not read its stdin, as none boxman runs does, runs to its end, and
    the interrupt is lost (#227, #294). This runner stops the command: a
    SIGINT, then a SIGTERM and a SIGKILL for one still running after its
    grace; and raises the KeyboardInterrupt once it has ended.

    What is signalled depends on where the command runs. With no controlling
    terminal, it runs in a session, and so a process group, of its own, and
    every signal goes to that group: to each process of a compound command
    or a pipeline, not only to the shell, which does not pass a signal on.
    With a terminal, it stays in boxman's group: there it can prompt on the
    terminal, as sudo asking for a password does, where in a group of its
    own the prompt would stop it (SIGTTOU); and a Ctrl-C at the terminal
    reaches it directly. It is not sent that SIGINT a second time, which
    could cut its handling of the first short; the SIGTERM and SIGKILL go to
    it and every process below it. A command run with ``pty=True`` leads a
    session of its own, as invoke starts it, and is stopped as a group.
    """

    #: how long a command has to end after its SIGINT, before its SIGTERM
    STOP_TIMEOUT = 10
    #: how long it then has to end, before it is killed. sudo passes a
    #: SIGTERM on to the command it runs as root, which boxman cannot kill
    TERM_TIMEOUT = 2
    #: how often a command being stopped is looked at
    POLL = 0.02

    def start(self, command: str, shell: str, env: dict[str, Any]) -> None:
        # invoke has the command only once Popen returns, and Popen forks,
        # then waits for the command's exec: a KeyboardInterrupt in between
        # left _stop() no command to stop, and the command ran on without
        # boxman (#278). So a SIGINT is held back until invoke has the
        # command, and handled then. Blocking SIGINT would not do: the
        # command inherits the mask, so the SIGINT _stop() sends it stays
        # pending, and another thread takes a SIGINT blocked here and has it
        # raised here all the same. Only a Python handler is swapped, for
        # another, which leaves the command's disposition as it was; an
        # ignored SIGINT, or the default one, raises nothing to hold back
        previous = signal.getsignal(signal.SIGINT)
        held_back: list[tuple[Any, ...]] = []
        swapped = False
        if callable(previous):
            try:
                signal.signal(signal.SIGINT, lambda *received: held_back.append(received))
                swapped = True
            except ValueError:
                # not the main thread, which is where SIGINT is handled anyway
                pass
        try:
            self._start(command, shell, env)
        finally:
            if swapped:
                signal.signal(signal.SIGINT, previous)
                if held_back:
                    # as if it came now, once however many came: for the
                    # default handler, a KeyboardInterrupt
                    previous(*held_back[0])

    def _start(self, command: str, shell: str, env: dict[str, Any]) -> None:
        #: the process group the command leads, or None: it is in boxman's
        self.group: int | None = None
        #: in boxman's group: each process signalled, by PID, with its start
        #: time; one below the shell is passed on to init when the shell dies
        self.signalled: dict[int, str] = {}
        if self.using_pty:
            # pty.fork() makes it a session leader, and so a group leader
            super().start(command, shell, env)
            self.group = self.pid
        elif _has_terminal():
            super().start(command, shell, env)
        else:
            # invoke's own Popen, in a session of its own: with no terminal,
            # there is none for the command to lose, or to prompt on
            self.process = subprocess.Popen(
                command, shell=True, executable=shell, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.PIPE, start_new_session=True)
            self.group = self.process.pid

    @property
    def process_is_finished(self) -> bool:
        # with a pty, invoke reaps the command with a waitpid of its own, and
        # another once it has been reaped raises: the stop and invoke both ask
        if self.using_pty and getattr(self, "_reaped", False):
            return True
        finished = super().process_is_finished
        if self.using_pty:
            self._reaped = finished
        return finished

    def send_interrupt(self, interrupt: KeyboardInterrupt) -> None:
        # invoke calls this from its wait for the command, and waits on
        # afterwards without a limit: stopping it here is what gives a
        # command that ignores its SIGINT a deadline (#244 review)
        self.interrupted = True
        self._stop()

    def _started(self) -> bool:
        if self.using_pty:
            return getattr(self, "pid", None) is not None
        return getattr(self, "process", None) is not None

    def _ended(self) -> bool:
        """Whether the command has ended, and been reaped: its shell, and
        every other process of its group, or in boxman's, every process it
        was sent a signal in.

        Processes left in its group that boxman cannot signal are ones sudo
        ran as root, once sudo itself has gone: nothing more can reach them.
        """
        if not self.process_is_finished:
            return False
        if self.group is None:
            return not any(_alive(pid, start) for pid, start in self.signalled.items())
        try:
            os.killpg(self.group, 0)
        except (ProcessLookupError, PermissionError):
            return True
        return False

    def _signal(self, number: int) -> None:
        if self.group is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.group, number)
            return
        # the shell, and every process below it, while it is there to find
        # them by: once reaped, its PID may be another process's
        if not self.process_is_finished:
            self.signalled.update(_tree(self.process.pid))
        for pid, start in self.signalled.items():
            if _alive(pid, start):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, number)

    def _wait(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not self._ended() and time.monotonic() < deadline:
            time.sleep(self.POLL)

    def _reap(self) -> None:
        """Wait for the command's shell, killed, to end."""
        while not self.process_is_finished:
            time.sleep(self.POLL)

    def _stop(self) -> None:
        """Stop the command, and wait for it to end (#227 review, #294).

        It is sent SIGINT; SIGTERM if it has not ended STOP_TIMEOUT later;
        and SIGKILL if it still has not TERM_TIMEOUT after that. One in
        boxman's group had the SIGINT from the terminal already, and is not
        sent another. Another Ctrl-C, at any step, moves on to the next at
        once. Either way, the command has ended, and been reaped, when this
        returns.
        """
        if not self._started():
            return
        steps = [(signal.SIGINT if self.group is not None else None, self.STOP_TIMEOUT),
                 (signal.SIGTERM, self.TERM_TIMEOUT)]
        step = 0
        while True:
            try:
                if self._ended():
                    return
                if step < len(steps):
                    number, timeout = steps[step]
                    step += 1
                    if number is not None:
                        self._signal(number)
                    self._wait(timeout)
                    continue
                self._signal(signal.SIGKILL)
                self._reap()
                return
            except KeyboardInterrupt:
                # another Ctrl-C: no more of the SIGINT's grace, but still
                # to the end
                step = max(step, 1)

    def run(self, command: str, **kwargs: Any) -> invoke.runners.Result:
        self.interrupted = False
        try:
            result = super().run(command, **kwargs)
        except invoke.exceptions.Failure:
            # an UnexpectedExit, for one: a stopped command did not exit 0
            if self.interrupted:
                raise KeyboardInterrupt from None
            raise
        except KeyboardInterrupt:
            # one invoke did not pass on: it came after the command started,
            # or start() held it back until then, and before invoke began to
            # wait for it, so the command would run on without boxman
            self._stop()
            raise
        if self.interrupted:
            raise KeyboardInterrupt
        return result


def stop_timeout() -> float:
    """The longest a command's stop waits on it, before it is killed."""
    return _StoppingLocal.STOP_TIMEOUT + _StoppingLocal.TERM_TIMEOUT


@contextlib.contextmanager
def interrupt_on_sigterm() -> Iterator[None]:
    """Within it, a SIGTERM stops this process as a first Ctrl-C does
    (#294).

    By default a SIGTERM ended boxman, or one of its worker processes, there
    and then, and the command it was running ran on alone: in a session of
    its own, a SIGTERM to boxman's group no longer reaches it either. Now
    the command is stopped, as on a Ctrl-C, and the KeyboardInterrupt
    raised. A Ctrl-C at the terminal reaches a worker as well as its parent,
    so the parent's SIGTERM finds the worker stopping its command already:
    it changes nothing then, where a second Ctrl-C would cut that stop
    short. Both are Python handlers, which the commands started do not
    inherit, and the ones it found are put back on the way out. Off the main
    thread, where no signal is handled, it changes nothing.
    """
    interrupted = False

    def on_sigint(number: int, frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        signal.default_int_handler(number, frame)

    def on_sigterm(number: int, frame: Any) -> None:
        # whatever handles SIGINT now: on_sigint, or, while a command
        # starts, the runner, which holds it back; and nothing once the
        # SIGINT is ignored
        handler = signal.getsignal(signal.SIGINT)
        if not interrupted and callable(handler):
            handler(signal.SIGINT, frame)

    try:
        previous = (signal.signal(signal.SIGINT, on_sigint),
                    signal.signal(signal.SIGTERM, on_sigterm))
    except ValueError:
        yield
        return
    try:
        yield
    finally:
        for number, handler in zip((signal.SIGINT, signal.SIGTERM), previous, strict=True):
            signal.signal(number, signal.SIG_DFL if handler is None else handler)


#: stop_workers() could not change SIGINT (not the main thread)
_NOT_SWAPPED = object()


def stop_workers(processes: Iterable[Any], slack: float = 5) -> None:
    """Stop worker processes, which run :func:`interrupt_on_sigterm`, and
    reap them (#294).

    Each is sent SIGTERM, a Ctrl-C to it: the command it is running is
    stopped before it exits. One still there once a command's whole stop
    could have run, and *slack* seconds more, is killed. Another Ctrl-C
    meanwhile is ignored here, as the workers are to be stopped either way;
    a terminal's reaches them too, and hurries their stops.
    """
    started = [process for process in processes if process.pid is not None]
    try:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:
        previous = _NOT_SWAPPED
    try:
        for process in started:
            if process.is_alive():
                process.terminate()
        deadline = time.monotonic() + stop_timeout() + slack
        for process in started:
            process.join(max(0.0, deadline - time.monotonic()))
            if process.is_alive():
                process.kill()
                process.join()
    finally:
        if previous is not _NOT_SWAPPED:
            signal.signal(signal.SIGINT, signal.SIG_DFL if previous is None else previous)


#: invoke's own run(). The test suite stubs the commands boxman runs by
#: putting a stand-in in its place, as run() called it before it had a
#: runner of its own: a stand-in there still has every command
_INVOKE_RUN = invoke.run


def _run(command: str, **kwargs: Any) -> invoke.runners.Result:
    config = invoke.Config(overrides={"runners": {"local": _StoppingLocal}})
    return invoke.Context(config).run(command, **kwargs)


def run_stoppable(command: str, **kwargs: Any) -> invoke.runners.Result:
    """
    :func:`run`, with *command* a single program and its arguments, which
    the shell is told to ``exec``.

    Every command boxman runs is stopped by a Ctrl-C now (#294); this was
    the one that was, before (#227). The ``exec`` keeps the shell from
    staying between boxman and the program, as a shell does whenever it has
    something to do afterwards, an EXIT trap from a ``BASH_ENV`` file for
    one: the result is the program's own.
    """
    kwargs.setdefault("in_stream", False)
    return _run(f"exec {command}", **kwargs)


def run(command: str, **kwargs: Any) -> invoke.runners.Result:
    """
    Run *command* via :func:`invoke.run`, defaulting ``in_stream=False``,
    on a runner that stops *command* on a Ctrl-C and raises the
    KeyboardInterrupt (see :class:`_StoppingLocal`).

    The signature is intentionally narrower than ``invoke.run`` — it
    accepts the same kwargs and passes them straight through; ``in_stream``
    defaults to ``False`` and can be overridden by passing a different
    value explicitly.

    Prefer this over ``invoke.run`` and ``from invoke import run`` in
    new code so the library stays test-framework- and CI-friendly.
    """
    kwargs.setdefault("in_stream", False)
    if invoke.run is not _INVOKE_RUN:
        return invoke.run(command, **kwargs)
    return _run(command, **kwargs)
