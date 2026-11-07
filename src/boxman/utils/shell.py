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
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from typing import Any

import invoke

#: the signals :func:`interrupt_on_signals` takes as a Ctrl-C
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)

#: set once a stop has begun in this process: by a Ctrl-C, by one of
#: STOP_SIGNALS, or by code that leaves on a KeyboardInterrupt while threads
#: of its own run commands (stop_commands). The KeyboardInterrupt reaches the
#: main thread alone: a runner on any other thread stops its command when
#: this is set, and starts no other, where a scan's reads, on threads of
#: their own, ran their commands to the end (#294 review). The main thread
#: clears it when it starts a command again, the stop being over
_STOPPING = threading.Event()

#: whether that stop was asked for by one of STOP_SIGNALS, not by a Ctrl-C
#: at the terminal, which has reached a command in boxman's group itself
_stop_by_a_signal = False


def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def _begin_stop(by_a_signal: bool = False) -> None:
    global _stop_by_a_signal
    if by_a_signal:
        _stop_by_a_signal = True
    _STOPPING.set()


def _end_stop() -> None:
    global _stop_by_a_signal
    _stop_by_a_signal = False
    _STOPPING.clear()


def stop_commands() -> None:
    """Stop the commands that threads of this process are running, and have
    them start no more (#294 review).

    For code that leaves on a KeyboardInterrupt while threads of its own
    run commands (a scan's pool): the KeyboardInterrupt reaches the main
    thread alone. A Ctrl-C under :func:`interrupt_on_signals` does this
    already.
    """
    _begin_stop()


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


def _kill_from_another_group(number: int, pids: list[int]) -> bool:
    """Send signal *number* to *pids*, from a short-lived process in a
    process group of its own; whether that process could be run.

    sudo passes on no signal that comes from the process group of whatever
    started it, taking it for one the terminal sent the whole group: sent
    from boxman's, which a command run with a terminal shares, a SIGTERM
    never reached the command sudo runs as root (#294 review).
    """
    try:
        subprocess.run(
            ["/bin/sh", "-c", 'kill -"$0" "$@"', str(int(number)), *map(str, pids)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


#: the guardian: a process in a group of its own that outlives boxman only
#: to end the commands boxman leaves behind. It reads "+<group>" for each
#: command group started and "-<group>" for each that ended. When the pipe
#: closes, boxman has gone, however it was killed, and each group still
#: there is sent SIGTERM, then SIGKILL the grace after
_GUARDIAN = r"""
import os, signal, sys, time
for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
    signal.signal(number, signal.SIG_IGN)
groups = set()
for line in sys.stdin.buffer:
    if line[:1] == b"+":
        groups.add(int(line[1:]))
    elif line[:1] == b"-":
        groups.discard(int(line[1:]))
def there(group):
    try:
        os.killpg(group, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True
for number in (signal.SIGTERM, signal.SIGKILL):
    groups = set(filter(there, groups))
    for group in groups:
        try:
            os.killpg(group, number)
        except OSError:
            pass
    deadline = time.monotonic() + float(sys.argv[1])
    while number == signal.SIGTERM and any(map(there, groups)) and time.monotonic() < deadline:
        time.sleep(0.05)
"""

#: (the PID of the process it guards, the guardian's PID, the pipe to it),
#: or None
_guardian: tuple[int, int, int] | None = None
#: the groups this process has the guardian watch: a new guardian, started
#: in place of one that died, is told of each
_guarded: set[int] = set()
_guardian_lock = threading.Lock()
_guardian_warned = False


def _guard(group: int, on: bool) -> None:
    """Have this process's guardian watch *group*, or stop watching it.

    Without a terminal, a command runs in a process group of its own, so
    that sudo passes boxman's signals on to it: a SIGKILL, SIGHUP or SIGQUIT
    to boxman's group, which ended the command with boxman, no longer
    reached it, and it ran on alone (#294 review). A parent-death signal
    would reach the shell alone, not the rest of a compound command.
    """
    with _guardian_lock:
        if on:
            _guarded.add(group)
        else:
            _guarded.discard(group)
        line = b"%s%d\n" % (b"+" if on else b"-", group)
        for _attempt in range(2):
            pipe = _live_guardian()
            if pipe is None:
                return
            try:
                os.write(pipe, line)
                return
            except OSError:
                # it died: the next attempt starts another, told of every group
                _drop_guardian()


def _live_guardian() -> int | None:
    """The pipe to this process's guardian, started, or started again if it
    has died, on first use; None if it cannot be started now."""
    global _guardian, _guardian_warned
    if _guardian is not None:
        owner, pid, pipe = _guardian
        if owner == os.getpid():
            try:
                done = os.waitpid(pid, os.WNOHANG)[0]
            except ChildProcessError:
                done = pid
            if not done:
                return pipe
        _drop_guardian()
    started = _start_guardian()
    if started is None:
        if not _guardian_warned:
            _guardian_warned = True
            from boxman import log
            log.warning(
                "boxman could not start the helper that ends its commands if it is "
                "killed: a command run without a terminal may outlive a killed boxman")
        return None
    _guardian = (os.getpid(), *started)
    with contextlib.suppress(OSError):
        os.write(_guardian[2], b"".join(b"+%d\n" % group for group in _guarded))
    return _guardian[2]


def _drop_guardian() -> None:
    global _guardian
    if _guardian is not None:
        with contextlib.suppress(OSError):
            os.close(_guardian[2])
    _guardian = None


def _start_guardian() -> tuple[int, int] | None:
    """Start a guardian; return its PID and the pipe to it, or None."""
    if not sys.executable:
        return None
    read, write = os.pipe()
    try:
        pid = os.posix_spawn(
            sys.executable,
            [sys.executable, "-I", "-S", "-c", _GUARDIAN, str(_StoppingLocal.TERM_TIMEOUT)],
            os.environ,
            file_actions=[(os.POSIX_SPAWN_DUP2, read, 0),
                          (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                          (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0)],
            setpgroup=0)
    except (OSError, NotImplementedError):
        os.close(write)
        return None
    finally:
        os.close(read)
    return pid, write


def _forget_guardian() -> None:
    """In a forked child, as a worker is: its parent's guardian is not its
    own, and holding the pipe to it would keep it from seeing the parent
    go; nor are its parent's groups. The child starts a guardian of its own
    when it needs one. The lock is a new one, as another thread of the
    parent may have held it at the fork."""
    global _guardian, _guarded, _guardian_lock
    _guardian_lock = threading.Lock()
    _drop_guardian()
    _guarded = set()


os.register_at_fork(after_in_child=_forget_guardian)


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
    The guardian ends the group if boxman goes without stopping it. With a
    terminal, it stays in boxman's group: there it can prompt on the
    terminal, as sudo asking for a password does, where in a group of its
    own the prompt would stop it (SIGTTOU); and a Ctrl-C at the terminal
    reaches it directly. It is not sent that SIGINT a second time, which
    could cut its handling of the first short, unless the stop was asked for
    otherwise; the signals go to it and every process below it, from a
    process in another group, as sudo would pass on none from boxman's. A
    command run with ``pty=True`` leads a session of its own, as invoke
    starts it, and is stopped as a group.

    On a thread other than the main one, which no KeyboardInterrupt
    reaches, the runner stops its command once a stop has begun in the
    process (_STOPPING), and starts none after it.
    """

    #: how long a command has to end after its SIGINT, before its SIGTERM
    STOP_TIMEOUT = 10
    #: how long it then has to end, before it is killed. sudo passes a
    #: SIGTERM on to the command it runs as root, which boxman cannot kill
    TERM_TIMEOUT = 2
    #: how long a killed command's shell has to be reaped, before boxman
    #: leaves it (a process in an uninterruptible wait ends only with it)
    KILL_TIMEOUT = 5
    #: how often a command being stopped is looked at
    POLL = 0.02

    def start(self, command: str, shell: str, env: dict[str, Any]) -> None:
        # invoke has the command only once Popen returns, and Popen forks,
        # then waits for the command's exec: a KeyboardInterrupt in between
        # left _stop() no command to stop, and the command ran on without
        # boxman (#278). So a SIGINT is held back until invoke has the
        # command, and handled then, and so is a signal interrupt_on_signals
        # takes as one. Blocking it would not do: the command inherits the
        # mask, so the SIGINT _stop() sends it stays pending, and another
        # thread takes a SIGINT blocked here and has it raised here all the
        # same. Only a Python handler is swapped, for another, which leaves
        # the command's disposition as it was; an ignored signal, or one
        # left to its default, raises nothing to hold back
        if _STOPPING.is_set() and not _on_main_thread():
            # a stop has begun: no new command on a thread the
            # KeyboardInterrupt does not reach (#294 review)
            raise KeyboardInterrupt
        held_back: list[tuple[Any, ...]] = []
        swapped: dict[int, Any] = {}
        for number in (signal.SIGINT, *STOP_SIGNALS):
            previous = signal.getsignal(number)
            if not callable(previous):
                continue
            try:
                signal.signal(number, lambda *received: held_back.append(received))
            except ValueError:
                # not the main thread, which is where signals are handled anyway
                break
            swapped[number] = previous
        try:
            self._start(command, shell, env)
        finally:
            for number, previous in swapped.items():
                signal.signal(number, previous)
            if held_back:
                # as if it came now, once however many came: for the
                # default SIGINT handler, a KeyboardInterrupt
                number, frame = held_back[0]
                swapped[number](number, frame)

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
        if self.group is not None:
            _guard(self.group, True)

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
        _begin_stop()
        self.interrupted = True
        self._stop()

    def wait(self) -> None:
        # invoke's wait for the command, which on a thread other than the
        # main one also looks for a stop that has begun: the
        # KeyboardInterrupt reaches the main thread alone (#294 review)
        watch = not _on_main_thread()
        while not (self.process_is_finished or self.has_dead_threads
                   or getattr(self, "abandoned", False)):
            if watch and _STOPPING.is_set() and not self.interrupted:
                self.interrupted = True
                # it may have started after the terminal's Ctrl-C, before
                # the main thread's handler set _STOPPING: it is sent the
                # SIGINT itself (a scan's reads only read, and take a second
                # one with no harm)
                self.from_another_thread = True
                self._stop()
                continue
            time.sleep(self.input_sleep)

    def _thread_join_timeout(self, target: Any) -> int | None:
        # once stopped, the command may have left a process holding its
        # output open, one sudo ran as root or one that left its group, and
        # invoke waited for it to end before it returned (#294 review)
        if getattr(self, "interrupted", False):
            return 1
        return super()._thread_join_timeout(target)

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
        pids = [pid for pid, start in self.signalled.items() if _alive(pid, start)]
        # from another group, for sudo; but a SIGKILL sudo cannot pass on,
        # and a helper that cannot be run must not leave a stop unended
        # (#294 review)
        if not pids or (number != signal.SIGKILL and _kill_from_another_group(number, pids)):
            return
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, number)

    def _wait(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not self._ended() and time.monotonic() < deadline:
            time.sleep(self.POLL)

    def _reap(self) -> None:
        """Wait for the command's shell, killed, to end; or KILL_TIMEOUT,
        and leave it, abandoned, which ends invoke's wait for it too."""
        deadline = time.monotonic() + self.KILL_TIMEOUT
        while not self.process_is_finished:
            if time.monotonic() >= deadline:
                self.abandoned = True
                return
            time.sleep(self.POLL)

    def _stop(self) -> None:
        """Stop the command, and wait for it to end (#227 review, #294).

        It is sent SIGINT; SIGTERM if it has not ended STOP_TIMEOUT later;
        and SIGKILL if it still has not TERM_TIMEOUT after that. One in
        boxman's group had the SIGINT from the terminal already, and is not
        sent another, unless one of STOP_SIGNALS asked for the stop, a
        worker's parent did, or the runner is on another thread. Another Ctrl-C, at any step, moves on to the
        next at once. Either way, the command has ended, and been reaped,
        when this returns.
        """
        if not self._started():
            return
        forward = (self.group is not None or _stop_by_a_signal
                   or getattr(self, "from_another_thread", False))
        steps = [(signal.SIGINT if forward else None, self.STOP_TIMEOUT),
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
        if _on_main_thread():
            # a command started again: the stop, if there was one, is over
            _end_stop()
        try:
            result = super().run(command, **kwargs)
        except invoke.exceptions.Failure:
            # an UnexpectedExit, for one: a stopped command did not exit 0;
            # on another thread, one the terminal's Ctrl-C ended first
            if self.interrupted or (_STOPPING.is_set() and not _on_main_thread()):
                raise KeyboardInterrupt from None
            raise
        except KeyboardInterrupt:
            # one invoke did not pass on: it came after the command started,
            # or start() held it back until then, and before invoke began to
            # wait for it, so the command would run on without boxman
            if _on_main_thread():
                _begin_stop()
            self._stop()
            raise
        finally:
            # ended, or let go of (disown, asynchronous): not the guardian's
            if getattr(self, "group", None) is not None:
                _guard(self.group, False)
        if self.interrupted:
            raise KeyboardInterrupt
        return result


def stop_timeout() -> float:
    """The longest a command's stop waits on it, before it is killed."""
    return _StoppingLocal.STOP_TIMEOUT + _StoppingLocal.TERM_TIMEOUT


@contextlib.contextmanager
def interrupt_on_signals(worker: bool = False) -> Iterator[None]:
    """Within it, a SIGTERM, SIGHUP or SIGQUIT stops this process as a
    Ctrl-C does (#294).

    By default each ended boxman, or one of its worker processes, there and
    then, and the command it was running ran on alone: in a session of its
    own, a signal to boxman's group no longer reaches it either. Now the
    command is stopped, as on a Ctrl-C, and the KeyboardInterrupt raised;
    with a terminal, the command is sent the SIGINT the terminal did not
    send it. Another, during the stop, moves it on, as another Ctrl-C does.
    A *worker* is the exception: a Ctrl-C at the terminal reaches it as well
    as its parent, so the parent's SIGTERM finds it stopping its command
    already, and changes nothing then, where a second Ctrl-C would cut that
    stop short. A signal ignored on the way in, as SIGINT is in a script's
    background job, stays ignored, and the commands started inherit that, as
    they did. The handlers are Python ones, which commands do not inherit,
    and the ones found are put back on the way out. Off the main thread,
    where no signal is handled, it changes nothing.
    """
    stopping = False

    def on_sigint(number: int, frame: Any) -> None:
        nonlocal stopping
        stopping = True
        _begin_stop()
        signal.default_int_handler(number, frame)

    def on_stop_signal(number: int, frame: Any) -> None:
        nonlocal stopping
        if worker and stopping:
            return
        stopping = True
        _begin_stop(by_a_signal=True)
        # whatever handles SIGINT now: on_sigint, or, while a command starts,
        # the runner, which holds it back; with SIGINT ignored, this raises
        handler = signal.getsignal(signal.SIGINT)
        if callable(handler):
            handler(signal.SIGINT, frame)
        else:
            raise KeyboardInterrupt

    handlers = {signal.SIGINT: on_sigint, **{number: on_stop_signal for number in STOP_SIGNALS}}
    previous: dict[int, Any] = {}
    try:
        for number, handler in handlers.items():
            if signal.getsignal(number) is not signal.SIG_IGN:
                previous[number] = signal.signal(number, handler)
    except ValueError:
        # not the main thread: nothing was changed
        yield
        return
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, signal.SIG_DFL if handler is None else handler)


@contextlib.contextmanager
def default_stop_signals() -> Iterator[None]:
    """Within it, SIGTERM, SIGHUP and SIGQUIT are left to their defaults:
    for a program run in the foreground, attached to the terminal, whose
    session is the user's (``boxman ssh``, ``exec``, ``run``).

    Under :func:`interrupt_on_signals`, a SIGTERM to boxman raised a
    KeyboardInterrupt in ``subprocess.run``, which killed ssh a quarter of a
    second later, and left the terminal as ssh had set it, without echo
    (#294 review). Now boxman ends at once, as it did, and ssh, left to end
    on its own, puts the terminal back. One ignored on the way in stays
    ignored; the handlers found are put back on the way out.
    """
    previous: dict[int, Any] = {}
    try:
        for number in STOP_SIGNALS:
            if signal.getsignal(number) is not signal.SIG_IGN:
                previous[number] = signal.signal(number, signal.SIG_DFL)
    except ValueError:
        # not the main thread: nothing was changed
        yield
        return
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, signal.SIG_DFL if handler is None else handler)


#: stop_workers() could not change SIGINT (not the main thread)
_NOT_SWAPPED = object()


def stop_workers(processes: Iterable[Any], slack: float = 5) -> None:
    """Stop worker processes, which run :func:`interrupt_on_signals`, and
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
