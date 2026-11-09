"""
Unit tests for boxman.utils.shell.

Pins the critical invariant: every call to :func:`boxman.utils.shell.run`
passes ``in_stream=False`` unless the caller explicitly overrides it.
A regression on this would re-introduce the pytest stdin-capture
deadlock / ``OSError: reading from stdin while output is captured`` that
Phase 2.8's follow-up fixed.
"""

from __future__ import annotations

import contextlib
import os
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


#: a program that, on SIGINT, exits 3 rather than dying of it, so that only
#: the runner can tell boxman it was a Ctrl-C. It says it is up by writing
#: its PID to the file named first; that its SIGINT handler began, by
#: creating the same name with ``.stopping`` added, and a line in
#: ``.sigints`` for each SIGINT; and, a second later, that it is done, with
#: ``.sigint``: the runner must wait for that before boxman cleans up
_TRAPS_SIGINT = (
    "import os, pathlib, signal, sys, time\n"
    "up = pathlib.Path(sys.argv[1])\n"
    "def stop(*_):\n"
    "    with open(str(up) + '.sigints', 'a') as sigints:\n"
    "        sigints.write('SIGINT\\n')\n"
    "    up.with_name(up.name + '.stopping').touch()\n"
    "    time.sleep(1)\n"
    "    up.with_name(up.name + '.sigint').touch()\n"
    "    sys.exit(3)\n"
    "signal.signal(signal.SIGINT, stop)\n"
    "up.with_name(up.name + '.tmp').write_text(str(os.getpid()))\n"
    "up.with_name(up.name + '.tmp').replace(up)\n"
    "time.sleep(60)\n"
)

#: a program that ignores SIGINT altogether; it says it is up as above
_IGNORES_SIGINT = (
    "import os, pathlib, signal, sys, time\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "up = pathlib.Path(sys.argv[1])\n"
    "up.with_name(up.name + '.tmp').write_text(str(os.getpid()))\n"
    "up.with_name(up.name + '.tmp').replace(up)\n"
    "time.sleep(60)\n"
)

#: one that ignores SIGTERM as well, as only a SIGKILL ends it
_IGNORES_SIGINT_AND_SIGTERM = _IGNORES_SIGINT.replace(
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n",
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n")


def _program(source: str, up) -> str:
    """The command line that runs the program *source*, up at *up*."""
    import shlex
    import sys
    return " ".join(shlex.quote(part) for part in (sys.executable, "-c", source, str(up)))


def _running(up) -> bool:
    """Whether the program that wrote its PID to *up* is still there."""
    from pathlib import Path
    try:
        stat = Path(f"/proc/{int(up.read_text())}/stat").read_text()
    except (OSError, ValueError):
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


@pytest.fixture
def no_leftovers(tmp_path):
    """Kill whatever program of this test is still running at its end: one
    a failing run left behind would otherwise sleep on for a minute."""
    import os
    import signal
    from pathlib import Path
    yield
    for up in tmp_path.glob("up*"):
        if "." in up.name or not _running(up):
            continue
        pid = int(up.read_text())
        try:
            ours = str(tmp_path).encode() in Path(f"/proc/{pid}/cmdline").read_bytes()
            if ours:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


#: a child that runs a command with run_stoppable, or with run under
#: RUN=run, and says how that ended. With a second argument, invoke is held
#: up between starting the command and waiting for it, the command's PID is
#: written to that file, and the child says whether the command is still
#: there when the interrupt reaches it. With INJECT=<runner step>:<n>, the
#: n-th call of that step once the runner is stopping the command first
#: creates the file INJECT_MARKER names and pauses, so that a SIGINT lands
#: inside it. STOP_TIMEOUT and TERM_TIMEOUT shorten the runner's graces.
#: With IN_POPEN=before-exec or after-exec, invoke is held up inside
#: ``Popen`` instead, before invoke has the command: in the forked child
#: before it execs the command, or in boxman once it has. The PID goes to
#: the same file, and the hold lasts until the file IN_POPEN_GO names
#: appears
_RUN_IN_A_CHILD = """
import os, subprocess, sys, time
from boxman.utils import shell
for name in ("STOP_TIMEOUT", "TERM_TIMEOUT"):
    if os.environ.get(name):
        setattr(shell._StoppingLocal, name, float(os.environ[name]))
def write_pid(pid):
    with open(sys.argv[2] + ".tmp", "w") as pid_file:
        pid_file.write(str(pid))
    os.replace(sys.argv[2] + ".tmp", sys.argv[2])
in_popen = os.environ.get("IN_POPEN")
if in_popen:
    def hold():
        deadline = time.monotonic() + 30
        while not os.path.exists(os.environ["IN_POPEN_GO"]) and time.monotonic() < deadline:
            time.sleep(0.01)
    if in_popen == "before-exec":
        real_init = subprocess.Popen.__init__
        def init(self, *args, **kwargs):
            def before_exec():
                write_pid(os.getpid())
                hold()
            real_init(self, *args, preexec_fn=before_exec, **kwargs)
        subprocess.Popen.__init__ = init
    else:
        real_execute = subprocess.Popen._execute_child
        def execute(self, *args, **kwargs):
            real_execute(self, *args, **kwargs)
            write_pid(self.pid)
            hold()
        subprocess.Popen._execute_child = execute
elif len(sys.argv) > 2:
    real = shell._StoppingLocal.create_io_threads
    def held_up(self):
        write_pid(self.process.pid)
        time.sleep(60)
        return real(self)
    shell._StoppingLocal.create_io_threads = held_up
if os.environ.get("INJECT"):
    name, nth = os.environ["INJECT"].split(":")
    stopping, calls = [], []
    real_stop, real_step = shell._StoppingLocal._stop, getattr(shell._StoppingLocal, name)
    def stop(self):
        stopping.append(True)
        return real_stop(self)
    def paused(self, *args, **kwargs):
        if stopping:
            calls.append(True)
            if len(calls) == int(nth):
                open(os.environ["INJECT_MARKER"], "w").close()
                time.sleep(5)
        return real_step(self, *args, **kwargs)
    shell._StoppingLocal._stop = stop
    setattr(shell._StoppingLocal, name, paused)
run = shell.run if os.environ.get("RUN") == "run" else shell.run_stoppable
try:
    result = run(sys.argv[1], hide=True, warn=os.environ.get("WARN") != "no")
    print("RETURNED", result.exited, flush=True)
except KeyboardInterrupt:
    if len(sys.argv) > 2:
        try:
            os.kill(int(open(sys.argv[2]).read()), 0)
            print("STOPPED LIVE", flush=True)
        except ProcessLookupError:
            print("STOPPED GONE", flush=True)
    else:
        print("STOPPED", flush=True)
"""

#: a child that runs a command, then a next step, as boxman does: unless
#: the run raises, it creates the file named second
_GOES_ON_AFTER_A_RUN = """
import sys
from boxman.utils import shell
shell.run(sys.argv[1], hide=True, warn=True)
open(sys.argv[2], "w").close()
"""

#: the same, with a SIGTERM, SIGHUP or SIGQUIT taken as a Ctrl-C, as
#: boxman's main() takes them
_GOES_ON_AFTER_A_RUN_UNDER_STOP_SIGNALS = """
import sys
from boxman.utils import shell
with shell.interrupt_on_signals():
    shell.run(sys.argv[1], hide=True, warn=True)
    open(sys.argv[2], "w").close()
"""

#: a child that retries a command on any error, as ssh-copy-id's attempts
#: did: the command line has the attempt's number in place of {attempt}
_RETRIES_ON_ANY_ERROR = """
import sys
from boxman.utils import shell
for attempt in (1, 2, 3):
    try:
        shell.run(sys.argv[1].format(attempt=attempt), hide=True)
    except Exception:
        continue
    break
"""


#: what each child does first: take SIGINT as Python sets it up when it
#: starts with SIGINT not ignored. A test run started in the background of a
#: script has SIGINT ignored, and so would every child, which then never
#: had a KeyboardInterrupt to stop on
_SIGINT_AS_PYTHON_SETS_IT = (
    "import signal\n"
    "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
)

#: what a child with a terminal does next: make the pty on its stdin the
#: controlling terminal of the session it leads
_THE_PTY_AS_ITS_TERMINAL = (
    "import fcntl, termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
)


def _interrupt(tmp_path, script, args, signal_at, how="alone", env=None,
               release=None, typed=None, terminal=None, timings=None):
    """Run the child *script* with *args*, and interrupt it at each point
    in *signal_at*: once the file there, or every file of a tuple there, has
    appeared, or, for a number, that many seconds on; a callable there is
    called with the child instead. Return what it printed, and how it
    exited.

    *how*, the same for every point or a list with one for each: ``alone``,
    a SIGINT to its Python alone; ``group``, to its whole process group, the
    command's too unless that has a session of its own; ``terminal``, a
    Ctrl-C typed at the pty that is its controlling terminal, which signals
    the terminal's foreground group; ``sigterm``, a SIGTERM to its Python
    alone; or (``alone`` or ``group``, a signal). *terminal*: whether it has
    such a pty, by default whether a Ctrl-C is typed at one. *typed* is
    typed at that terminal first. *timings*, a dict, gets the time of the
    first interrupt, ``first``, and of the child's end, ``ended``.

    After the last interrupt, the file *release* is created, to let a run
    held up inside ``Popen`` go on: once the child has ended, as it does
    when the interrupt goes straight through, or a couple of seconds on,
    as the runner holds it back until ``Popen`` has returned."""
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    points = [when for when in signal_at
              if not isinstance(when, (int, float)) and not callable(when)]
    hows = list(how) if isinstance(how, list) else [how] * len(points)
    if terminal is None:
        terminal = "terminal" in hows
    timings = {} if timings is None else timings
    env = dict(env or os.environ,
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    master = slave = None
    preamble = _SIGINT_AS_PYTHON_SETS_IT
    if terminal:
        master, slave = os.openpty()
        preamble += _THE_PTY_AS_ITS_TERMINAL
    child = subprocess.Popen(
        [sys.executable, "-c", preamble + script, *map(str, args)],
        env=env, stdin=slave,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)
    if slave is not None:
        os.close(slave)
    try:
        if typed:
            os.write(master, typed)
        for when in signal_at:
            if isinstance(when, (int, float)):
                time.sleep(when)
                continue
            if callable(when):
                when(child)
                continue
            files = when if isinstance(when, tuple) else (when,)
            deadline = time.monotonic() + 30
            while not all(path.exists() for path in files):
                # its stderr only once it has ended: a read would wait for that
                assert child.poll() is None and time.monotonic() < deadline, \
                    child.stderr.read() if child.poll() is not None else "not there in time"
                time.sleep(0.05)
            where = hows.pop(0)
            timings.setdefault("first", time.monotonic())
            if where == "terminal":
                os.write(master, b"\x03")
                continue
            if where == "sigterm":
                where = ("alone", signal.SIGTERM)
            elif isinstance(where, str):
                where = (where, signal.SIGINT)
            if where[0] == "group":
                os.killpg(child.pid, where[1])
            else:
                os.kill(child.pid, where[1])
        if release:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            release.touch()
        out, err = child.communicate(timeout=30)
        timings["ended"] = time.monotonic()
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        if master is not None:
            os.close(master)
    return out, err, child.returncode


@pytest.mark.usefixtures("no_leftovers")
class TestRunStoppable:
    """
    invoke answers a Ctrl-C by writing ``\\x03`` to the command's stdin and
    waiting on, so a command that does not read its stdin rode out a
    SIGINT sent to boxman alone (#227 review). ``run_stoppable`` signals
    the command and raises the KeyboardInterrupt once it has ended.
    """

    def test_does_not_raise_under_capture(self):
        from boxman.utils.shell import run_stoppable
        assert run_stoppable("true").ok

    def test_leaves_the_sigint_handler_as_it_found_it(self):
        """The runner swaps it while ``Popen`` starts the command (#278)."""
        import signal

        from boxman.utils.shell import run_stoppable

        before = signal.getsignal(signal.SIGINT)
        assert run_stoppable("true").ok
        assert signal.getsignal(signal.SIGINT) is before

    def test_runs_on_a_thread_other_than_the_main_one(self):
        """Only the main thread may set a SIGINT handler: elsewhere, the
        runner leaves it alone rather than fail (#278)."""
        import threading

        from boxman.utils.shell import run_stoppable

        results = []
        thread = threading.Thread(target=lambda: results.append(run_stoppable("true").ok))
        thread.start()
        thread.join(30)
        assert results == [True]

    @pytest.mark.parametrize("ignored", [False, True], ids=["handled", "ignored"])
    def test_the_command_takes_sigint_as_boxman_did(self, ignored):
        """Blocked and ignored just as in boxman: holding a SIGINT back
        while the command starts (#278) must not change how the command
        takes one, or the SIGINT that stops it would not reach it. Blocking
        it there would leave it blocked in the command."""
        import signal
        from pathlib import Path

        from boxman.utils.shell import run_stoppable

        def sigint_in(status):
            return {line.split(":")[0]: bool(int(line.split()[1], 16) >> (signal.SIGINT - 1) & 1)
                    for line in status.splitlines() if line.startswith(("SigBlk:", "SigIgn:"))}

        previous = signal.signal(
            signal.SIGINT, signal.SIG_IGN if ignored else signal.default_int_handler)
        try:
            boxman = sigint_in(Path("/proc/self/status").read_text())
            out = run_stoppable("grep -E '^Sig(Blk|Ign):' /proc/self/status", hide=True).stdout
        finally:
            signal.signal(signal.SIGINT, previous)
        assert boxman["SigIgn"] is ignored
        assert sigint_in(out) == boxman

    def _sigint_boxman_alone(self, tmp_path, signal_at, held_up=None, env=None,
                             program=_TRAPS_SIGINT, release=None):
        """Run the program with run_stoppable in a child, and SIGINT the
        child's Python alone at each point in *signal_at* (see
        :func:`_interrupt`).

        A held-up run's PID file appears as soon as the command is started,
        when it may not have set up its SIGINT handler yet: a SIGINT then
        ends it there and then, so a point that needs that handler to run
        waits for the command to be up too (#258)."""
        return _interrupt(
            tmp_path, _RUN_IN_A_CHILD,
            [_program(program, tmp_path / "up"), *([held_up] if held_up else [])],
            signal_at, env=env, release=release)

    @pytest.mark.parametrize("warn", [True, False], ids=["warn", "no-warn"])
    @pytest.mark.parametrize("bash_env", [None, "trap : EXIT\n"], ids=["plain", "exit-trap"])
    def test_a_sigint_to_boxman_alone_stops_the_command_and_is_raised(
            self, bash_env, warn, tmp_path):
        """With an EXIT trap from ``BASH_ENV`` to run afterwards, bash does
        not replace itself with the program, and a SIGINT to bash alone
        left the program running (#227 review). Without ``warn``, the
        program's exit 3 is a KeyboardInterrupt too, not an UnexpectedExit."""
        import os

        env = dict(os.environ, WARN="yes" if warn else "no")
        if bash_env:
            (tmp_path / "bash_env").write_text(bash_env)
            env["BASH_ENV"] = str(tmp_path / "bash_env")
        out, err, code = self._sigint_boxman_alone(tmp_path, [tmp_path / "up"], env=env)
        assert (out.split(), code) == (["STOPPED"], 0), err
        assert (tmp_path / "up.sigint").exists()

    def test_a_sigint_before_invoke_waits_stops_the_command(self, tmp_path):
        """invoke passes on only a KeyboardInterrupt that comes while it
        waits; one between starting the command and waiting for it left the
        command running on its own (#227 review)."""
        import os

        pid_file = tmp_path / "pid"
        # once invoke is held up and the command is up: a SIGINT before the
        # command had set up its handler ended it there and then, and
        # up.sigint never came (#258)
        out, err, code = self._sigint_boxman_alone(
            tmp_path, [(pid_file, tmp_path / "up")], held_up=pid_file)
        # gone by the time the interrupt reached the caller, stopped by its
        # SIGINT rather than killed, and not running on without boxman
        assert (out.split(), code) == (["STOPPED", "GONE"], 0), err
        assert (tmp_path / "up.sigint").exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    @pytest.mark.parametrize("in_popen", ["before-exec", "after-exec"],
                             ids=["before-its-exec", "after-its-exec"])
    def test_a_sigint_before_popen_returns_stops_the_command(self, in_popen, tmp_path):
        """invoke has the command only once ``Popen`` returns. A SIGINT that
        came before, while the forked child had still to exec the command or
        once it had, found no command to stop: the KeyboardInterrupt reached
        the caller, and the command ran on without boxman (#278)."""
        import os

        pid_file, go = tmp_path / "pid", tmp_path / "go"
        env = dict(os.environ, IN_POPEN=in_popen, IN_POPEN_GO=str(go))
        # held before its exec, the command cannot be up yet; held after it,
        # the SIGINT waits for it to be up, as above (#258)
        signal_at = [pid_file] if in_popen == "before-exec" else [(pid_file, tmp_path / "up")]
        out, err, code = self._sigint_boxman_alone(
            tmp_path, signal_at, held_up=pid_file, env=env, release=go)
        assert (out.split(), code) == (["STOPPED", "GONE"], 0), err
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        if in_popen == "after-exec":
            # up before its SIGINT, so stopped by it rather than killed
            assert (tmp_path / "up.sigint").exists()

    @pytest.mark.parametrize(("inject", "escalated", "killed"), [
        # the SIGINT step: another SIGINT at its very start, or in the
        # SIGINT it sends, skips it; one in its wait, once the command's
        # handler has begun, ends that. Either way the command is sent
        # SIGTERM then, and dies of it before its handler is done
        ("_ended:1", False, True),
        ("_signal:1", False, True),
        (None, True, True),
        # the SIGTERM, after another SIGINT ended the SIGINT's wait (the
        # handler may be done by then: this pauses for a while)
        ("_signal:2", True, None),
    ], ids=["at-the-first-check", "at-the-sigint", "at-the-graceful-wait", "at-the-sigterm"])
    def test_another_sigint_while_it_stops_still_waits_for_it(
            self, inject, escalated, killed, tmp_path):
        """A further Ctrl-C at any step of stopping the command used to be
        able to cut it short: the interrupt reached the caller with the
        command still running, or dead and not reaped (#227 review). Now
        the stop moves on to its next step at once, and still waits."""
        import os

        pid_file, marker = tmp_path / "pid", tmp_path / "inject"
        env = dict(os.environ, INJECT=inject or "", INJECT_MARKER=str(marker))
        # the first SIGINT once invoke is held up and the command is up: one
        # before the command had set up its handler ended it there and then,
        # and up.stopping never came (#258)
        signal_at = [(pid_file, tmp_path / "up")]
        if escalated:
            # a second SIGINT once the command is in its SIGINT handler
            signal_at.append(tmp_path / "up.stopping")
        if inject:
            signal_at.append(marker)
        out, err, code = self._sigint_boxman_alone(
            tmp_path, signal_at, held_up=pid_file, env=env)
        assert (out.split(), code) == (["STOPPED", "GONE"], 0), err
        if killed:
            assert not (tmp_path / "up.sigint").exists()

    @pytest.mark.parametrize("inject", ["_signal:3", "_reap:1"],
                             ids=["at-the-kill", "at-the-last-wait"])
    def test_another_sigint_while_it_is_killed_still_waits_for_it(self, inject, tmp_path):
        """The SIGKILL, and the wait for it, reached by a command that
        ignores both its SIGINT and its SIGTERM."""
        import os

        pid_file, marker = tmp_path / "pid", tmp_path / "inject"
        env = dict(os.environ, INJECT=inject, INJECT_MARKER=str(marker),
                   STOP_TIMEOUT="0.5", TERM_TIMEOUT="0.5")
        out, err, code = self._sigint_boxman_alone(
            tmp_path, [(pid_file, tmp_path / "up"), marker], held_up=pid_file, env=env,
            program=_IGNORES_SIGINT_AND_SIGTERM)
        assert (out.split(), code) == (["STOPPED", "GONE"], 0), err

    def test_a_command_that_ignores_its_sigint_is_killed_after_the_grace(self, tmp_path):
        """invoke waits on, without a limit, after it passes a Ctrl-C on:
        a command that ignores its SIGINT held boxman up indefinitely
        (#244 review). The runner's grace and kill apply there too."""
        import os

        env = dict(os.environ, STOP_TIMEOUT="1")
        out, err, code = self._sigint_boxman_alone(
            tmp_path, [tmp_path / "up"], env=env, program=_IGNORES_SIGINT)
        assert (out.split(), code) == (["STOPPED"], 0), err


@pytest.mark.usefixtures("no_leftovers")
class TestRunStopsEveryCommand:
    """
    ``run`` was invoke's stock runner, which answers a Ctrl-C by writing
    ``\\x03`` to the command's stdin and waiting on: virsh, qemu-img,
    virt-install, ssh-copy-id and the rest rode it out, or ended on their
    own, and boxman went on to its next step and exited 0 (#294). It takes
    compound commands, which ``run_stoppable``'s ``exec`` does not suit:
    with no terminal, the command gets a process group of its own, and the
    group is signalled; with one, it stays in boxman's, which the
    terminal's Ctrl-C reaches.
    """

    @staticmethod
    def _env(**more):
        import os
        return dict(os.environ, RUN="run", **more)

    def test_a_sigint_to_boxman_alone_stops_every_process_of_a_compound_command(
            self, tmp_path):
        """The program is below the shell, which does not pass a SIGINT
        on: it has its SIGINT from the group's. Its exit 3 ends the ``&&``
        there, and the KeyboardInterrupt is raised."""
        up, after = tmp_path / "up", tmp_path / "after"
        out, err, code = _interrupt(
            tmp_path, _RUN_IN_A_CHILD, [f"{_program(_TRAPS_SIGINT, up)} && touch {after}"],
            [up], env=self._env())
        assert (out.split(), code) == (["STOPPED"], 0), err
        assert (tmp_path / "up.sigint").exists()
        assert not after.exists()

    @pytest.mark.parametrize("program", [_IGNORES_SIGINT, _IGNORES_SIGINT_AND_SIGTERM],
                             ids=["ends-at-the-sigterm", "ends-at-the-sigkill"])
    def test_a_compound_command_that_ignores_its_sigint_is_stopped_after_the_grace(
            self, program, tmp_path):
        """SIGTERM once STOP_TIMEOUT is up, SIGKILL TERM_TIMEOUT later, both
        to the whole group: the shell's own kill left the program running."""
        up, after = tmp_path / "up", tmp_path / "after"
        out, err, code = _interrupt(
            tmp_path, _RUN_IN_A_CHILD, [f"{_program(program, up)} && touch {after}"],
            [up], env=self._env(STOP_TIMEOUT="1", TERM_TIMEOUT="1"))
        assert (out.split(), code) == (["STOPPED"], 0), err
        assert not _running(up)
        assert not after.exists()

    def test_a_cleanup_the_commands_own_handler_starts_is_left_to_finish(self, tmp_path):
        """A command whose handler of the SIGINT starts a cleanup of its own
        (a shell's ``trap ... INT``): that process, forked after the stop
        began, is not sent the SIGINT too, which would cut the cleanup short
        (#309 review)."""
        import os

        up, cleaned = tmp_path / "up", tmp_path / "cleaned"
        command = (f"trap 'sh -c \"sleep 1; touch {cleaned}\"' INT; "
                   f"{_program(_TRAPS_SIGINT, up)}")
        out, err, code = _interrupt(tmp_path, _RUN_IN_A_CHILD, [command], [up],
                                    env=dict(os.environ, RUN="run"))
        assert (out.split(), code) == (["STOPPED"], 0), err
        assert cleaned.exists()

    def test_an_interrupted_boxman_exits_by_sigint_and_takes_no_next_step(self, tmp_path):
        """The run returned the stopped command's result, and boxman went on
        with its next step, and exited 0. Now the KeyboardInterrupt ends
        boxman by SIGINT, which a shell reports as exit 130."""
        import signal

        up, next_step = tmp_path / "up", tmp_path / "next-step"
        out, err, code = _interrupt(
            tmp_path, _GOES_ON_AFTER_A_RUN, [_program(_TRAPS_SIGINT, up), next_step], [up])
        assert code == -signal.SIGINT, err
        assert (tmp_path / "up.sigint").exists()
        assert not next_step.exists()

    def test_an_except_exception_around_it_lets_a_ctrl_c_through(self, tmp_path):
        """A Ctrl-C reached the program with the rest of the group, and its
        exit 3 came back as an UnexpectedExit, which a caller retrying on
        any error took for a failed attempt: ``up`` ran on to its next
        ssh-copy-id (#294). A KeyboardInterrupt is not an Exception."""
        import signal

        out, err, code = _interrupt(
            tmp_path, _RETRIES_ON_ANY_ERROR, [_program(_TRAPS_SIGINT, tmp_path / "up-{attempt}")],
            [tmp_path / "up-1"], how="group")
        assert code == -signal.SIGINT, err
        assert (tmp_path / "up-1.sigint").exists()
        assert not (tmp_path / "up-2").exists()

    def test_a_sigterm_stops_the_command_as_a_ctrl_c_does(self, tmp_path):
        """In a session of its own, the command no longer has a SIGTERM sent
        to boxman's group, and a SIGTERM ended boxman there and then: under
        ``interrupt_on_signals``, as boxman's ``main()`` runs, boxman stops
        the command first."""
        import signal

        up, next_step = tmp_path / "up", tmp_path / "next-step"
        out, err, code = _interrupt(
            tmp_path, _GOES_ON_AFTER_A_RUN_UNDER_STOP_SIGNALS,
            [_program(_TRAPS_SIGINT, up), next_step], [up], how="sigterm")
        assert code == -signal.SIGINT, err
        assert (tmp_path / "up.sigint").exists()
        assert not next_step.exists()

    def test_a_ctrl_c_at_the_terminal_stops_a_compound_command_and_is_raised(self, tmp_path):
        """With a terminal, the command shares boxman's group, and has the
        terminal's SIGINT itself: only that one, as a second could cut its
        handler short."""
        up, after = tmp_path / "up", tmp_path / "after"
        out, err, code = _interrupt(
            tmp_path, _RUN_IN_A_CHILD, [f"{_program(_TRAPS_SIGINT, up)} && touch {after}"],
            [up], how="terminal", env=self._env())
        assert (out.split(), code) == (["STOPPED"], 0), err
        assert (tmp_path / "up.sigints").read_text().splitlines() == ["SIGINT"]
        assert (tmp_path / "up.sigint").exists()
        assert not after.exists()

    def test_a_command_that_ignores_the_terminals_ctrl_c_is_stopped_after_the_grace(
            self, tmp_path):
        """The SIGTERM and SIGKILL go to the shell and every process below
        it, and the program is waited for, and killed, after the SIGTERM has
        ended the shell above it."""
        up, after = tmp_path / "up", tmp_path / "after"
        out, err, code = _interrupt(
            tmp_path, _RUN_IN_A_CHILD,
            [f"{_program(_IGNORES_SIGINT_AND_SIGTERM, up)} && touch {after}"],
            [up], how="terminal", env=self._env(STOP_TIMEOUT="1", TERM_TIMEOUT="1"))
        assert (out.split(), code) == (["STOPPED"], 0), err
        assert not _running(up)
        assert not after.exists()

    def test_a_command_can_still_prompt_on_the_terminal(self, tmp_path):
        """In a process group of its own, a command that reads from the
        terminal, as sudo asking for a password does, would be stopped
        (SIGTTIN) and hold boxman up for good."""
        answer = tmp_path / "answer"
        out, err, code = _interrupt(
            tmp_path, _RUN_IN_A_CHILD, [f"read reply < /dev/tty && echo $reply > {answer}"],
            [], terminal=True, env=self._env(), typed=b"yes\n")
        assert (out.split(), code) == (["RETURNED", "0"], 0), err
        assert answer.read_text() == "yes\n"


def _once(path, action):
    """A step for :func:`_interrupt`: once *path* is there, *action*, called
    with the child."""
    def step(child):
        import time
        deadline = time.monotonic() + 30
        while not path.exists():
            assert child.poll() is None and time.monotonic() < deadline, "not there in time"
            time.sleep(0.05)
        action(child)
    return step


def _gone_within(up, seconds) -> bool:
    """Whether the program that wrote its PID to *up* is gone within
    *seconds*."""
    import time
    deadline = time.monotonic() + seconds
    while _running(up) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _running(up)


def _sudo_works() -> bool:
    import subprocess
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.usefixtures("no_leftovers")
class TestTheCommandDoesNotOutliveBoxman:
    """
    Without a terminal, the command runs in a process group of its own, so
    that sudo passes boxman's signals on to it. A signal to boxman's group
    that boxman could not turn into a stop then ended boxman alone, and the
    command, which went with boxman before, ran on (#294 review). The
    guardian ends it now, once boxman has gone.
    """

    @pytest.mark.parametrize("name", ["SIGKILL", "SIGTERM"])
    def test_boxman_killed_with_its_group_takes_the_command_with_it(self, name, tmp_path):
        """A SIGKILL, which nothing can handle, or a SIGTERM to boxman used
        as a library, without the handlers ``main()`` has: the program,
        below a shell that has more to run, is sent SIGTERM by the guardian."""
        import signal

        number = getattr(signal, name)
        up = tmp_path / "up"
        out, err, code = _interrupt(
            tmp_path, _GOES_ON_AFTER_A_RUN,
            [f"{_program(_TRAPS_SIGINT, up)}; true", tmp_path / "next-step"],
            [up], how=[("group", number)])
        assert code == -number, err
        assert _gone_within(up, 5)

    def test_a_sigkill_in_the_middle_of_a_stop_still_takes_the_command(self, tmp_path):
        """A Ctrl-C began the stop of a program that ignores its SIGINT, and
        a SIGKILL to the group came within the grace."""
        import signal

        up = tmp_path / "up"
        out, err, code = _interrupt(
            tmp_path, _GOES_ON_AFTER_A_RUN_UNDER_STOP_SIGNALS,
            [f"{_program(_IGNORES_SIGINT, up)}; true", tmp_path / "next-step"],
            [up, 1, up], how=["alone", ("group", signal.SIGKILL)])
        assert code == -signal.SIGKILL, err
        assert _gone_within(up, 5)

    @pytest.mark.parametrize("name", ["SIGHUP", "SIGQUIT"])
    def test_a_sighup_or_sigquit_to_the_group_stops_the_command_as_a_ctrl_c_does(
            self, name, tmp_path):
        """Under the handlers ``main()`` has, as a SIGTERM: each ended
        boxman alone."""
        import signal

        up, next_step = tmp_path / "up", tmp_path / "next-step"
        out, err, code = _interrupt(
            tmp_path, _GOES_ON_AFTER_A_RUN_UNDER_STOP_SIGNALS,
            [_program(_TRAPS_SIGINT, up), next_step], [up],
            how=[("group", getattr(signal, name))])
        assert code == -signal.SIGINT, err
        assert (tmp_path / "up.sigint").exists()
        assert not next_step.exists()


@pytest.mark.usefixtures("no_leftovers")
class TestWhatTheStopCannotEnd:
    """
    A process the stop cannot end, one sudo runs as root or one that left
    the command's group, held its output open, and invoke waited for that
    to close before boxman went on (#294 review).
    """

    def test_a_process_left_holding_the_output_does_not_hold_boxman_up(self, tmp_path):
        """The process has left for a session of its own: invoke's wait for
        the output it holds used to last until it ended."""
        import os
        import signal

        up, held = tmp_path / "up", tmp_path / "held"
        command = (f"setsid sh -c 'echo $$ > {held}; exec sleep 60' & "
                   f"{_program(_TRAPS_SIGINT, up)}")
        timings = {}
        try:
            out, err, code = _interrupt(
                tmp_path, _RUN_IN_A_CHILD, [command], [(up, held)],
                env=dict(os.environ, RUN="run"), timings=timings)
            assert (out.split(), code) == (["STOPPED"], 0), err
            assert timings["ended"] - timings["first"] < 10
        finally:
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(held.read_text()), signal.SIGKILL)

    # it runs a command as root, through sudo: the integration tier's (#214)
    @pytest.mark.integration
    def test_with_a_terminal_sudo_passes_the_sigterm_on_to_its_command(self, tmp_path):
        """sudo passes on no signal from the process group of what started
        it, boxman's, which the command shares with a terminal: the SIGTERM
        never reached the command sudo ran as root, which ignores its
        SIGINT, the SIGKILL ended sudo alone, and the command held boxman up
        until it ended. The signals come from another group now."""
        import os
        import subprocess

        if not _sudo_works():
            pytest.skip("needs sudo without a password")
        up = tmp_path / "up"
        command = f"sudo -n sh -c \"trap '' INT; echo \\$\\$ > {up}; exec sleep 60\""
        try:
            out, err, code = _interrupt(
                tmp_path, _RUN_IN_A_CHILD, [command], [up], how="terminal",
                env=dict(os.environ, RUN="run", STOP_TIMEOUT="1", TERM_TIMEOUT="1"))
            assert (out.split(), code) == (["STOPPED"], 0), err
            assert _gone_within(up, 2)
        finally:
            if _running(up):
                subprocess.run(["sudo", "-n", "kill", "-KILL", up.read_text().strip()])


@pytest.mark.usefixtures("no_leftovers")
class TestAStopTheTerminalDidNotAskFor:
    """
    With a terminal, the runner took every stop for a Ctrl-C there, which
    the command has had itself, and sent it nothing for STOP_TIMEOUT; a
    stop asked for otherwise reached the command 10 s on, as a SIGTERM
    (#294 review).
    """

    def test_with_a_terminal_a_sigterm_has_the_command_sent_its_sigint_at_once(
            self, tmp_path):
        import signal

        up, next_step = tmp_path / "up", tmp_path / "next-step"
        timings = {}
        out, err, code = _interrupt(
            tmp_path, _GOES_ON_AFTER_A_RUN_UNDER_STOP_SIGNALS,
            [_program(_TRAPS_SIGINT, up), next_step], [up], how="sigterm",
            terminal=True, timings=timings)
        assert code == -signal.SIGINT, err
        assert (tmp_path / "up.sigint").exists()
        assert not next_step.exists()
        assert timings["ended"] - timings["first"] < 5

    def test_a_sigterm_during_a_stop_moves_it_on(self, tmp_path):
        """A Ctrl-C began the stop of a program that ignores its SIGINT: a
        SIGTERM a second later was ignored, as all later ones were, where
        another Ctrl-C moves the stop on to the program's SIGTERM at once."""
        import signal

        up = tmp_path / "up"
        timings = {}
        out, err, code = _interrupt(
            tmp_path, _GOES_ON_AFTER_A_RUN_UNDER_STOP_SIGNALS,
            [_program(_IGNORES_SIGINT, up), tmp_path / "next-step"],
            [up, 1, up], how=["alone", "sigterm"], timings=timings)
        assert code == -signal.SIGINT, err
        assert not _running(up)
        assert timings["ended"] - timings["first"] < 6

    def test_an_ignored_sigint_stays_ignored_for_the_commands(self):
        """boxman started in the background of a script has SIGINT ignored,
        as its commands had too; the handler ``main()`` put in its place
        took that away from them."""
        import signal

        from boxman.utils.shell import interrupt_on_signals, run

        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            with interrupt_on_signals():
                assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
                out = run("grep '^SigIgn:' /proc/self/status", hide=True).stdout
        finally:
            signal.signal(signal.SIGINT, previous)
        assert int(out.split()[1], 16) >> (signal.SIGINT - 1) & 1


#: a child that scans as boxman does, on #304's pool: eight reads at once,
#: each running three commands one after another, through run, each of
#: which writes its PID to ``pid-<read>-<command>``, sleeps, and, unless
#: stopped, creates ``done-<read>-<command>``. A failed command does not end
#: its read (``warn``), as the reads of a scan do not all stop at one
_SCANS_ON_THE_POOL = """
import sys
from boxman.providers.libvirt.scan_pool import InOrder
from boxman.utils import shell
marks = sys.argv[1]
def read(item):
    for command in range(3):
        shell.run(f"sh -c 'echo $$ > {marks}/pid-{item}-{command}; exec sleep 5' "
                  f"&& touch {marks}/done-{item}-{command}", hide=True, warn=True)
    return item
with shell.interrupt_on_signals():
    with InOrder(read, range(8), 8) as outcomes:
        print([outcome.result() for outcome in outcomes], flush=True)
"""


#: the same scan, with the SIGINT handed to a thread other than the main
#: one, as the kernel may hand a signal sent to the process: a thread of the
#: child's own sends it to itself once the reads' first commands are up, and
#: the child says how long after that its main thread had the
#: KeyboardInterrupt
_A_THREAD_TAKES_THE_SIGINT = """
import os, signal, sys, threading, time
marks = sys.argv[1]
sent = []
def take_it():
    first = [f"{marks}/pid-{item}-0" for item in range(8)]
    while not all(os.path.exists(path) for path in first):
        time.sleep(0.01)
    sent.append(time.monotonic())
    signal.pthread_kill(threading.get_ident(), signal.SIGINT)
threading.Thread(target=take_it, daemon=True).start()
try:
""" + "".join("    " + line + "\n" if line else "\n"
              for line in _SCANS_ON_THE_POOL.strip("\n").split("\n")) + """
except KeyboardInterrupt:
    print("STOPPED", round(time.monotonic() - sent[0], 2), flush=True)
    raise
"""


#: the same scan, where read 0's first command dies of a SIGINT, as the
#: terminal's Ctrl-C ends it, and the Ctrl-C reaches boxman a little later,
#: as a busy main thread takes it: read 0 creates ``died`` first, and the
#: others wait for that before they start
_A_READS_COMMAND_DIES_OF_SIGINT = """
import os, signal, sys, threading, time
from boxman.providers.libvirt.scan_pool import InOrder
from boxman.utils import shell
marks = sys.argv[1]
def ctrl_c_reaches_boxman():
    while not os.path.exists(f"{marks}/died"):
        time.sleep(0.01)
    time.sleep(0.05)
    os.kill(os.getpid(), signal.SIGINT)
threading.Thread(target=ctrl_c_reaches_boxman, daemon=True).start()
def read(item):
    if item == 0:
        open(f"{marks}/died", "w").close()
        shell.run("sh -c 'kill -INT $$'", hide=True, warn=True)
    else:
        while not os.path.exists(f"{marks}/died"):
            time.sleep(0.01)
    for command in range(1, 3):
        shell.run(f"sh -c 'echo $$ > {marks}/pid-{item}-{command}; exec sleep 5' "
                  f"&& touch {marks}/done-{item}-{command}", hide=True, warn=True)
    return item
with shell.interrupt_on_signals():
    with InOrder(read, range(8), 8) as outcomes:
        print([outcome.result() for outcome in outcomes], flush=True)
"""


#: a scan of two reads, read 0 running the command given, read 1 none
_A_READ_RUNS_A_COMMAND = """
import sys
from boxman.providers.libvirt.scan_pool import InOrder
from boxman.utils import shell
def read(item):
    if item == 0:
        shell.run(sys.argv[1], hide=True, warn=True)
    return item
with shell.interrupt_on_signals():
    with InOrder(read, range(2), 2) as outcomes:
        print([outcome.result() for outcome in outcomes], flush=True)
"""


@pytest.mark.usefixtures("no_leftovers")
class TestCommandsOnOtherThreads:
    """
    The KeyboardInterrupt reaches the main thread alone. A scan's reads run
    their commands on #304's pool threads, which nothing stopped: without a
    terminal, in a group of their own, the commands ran to their ends, and
    the reads started more; and with one, the reads started more after the
    terminal's Ctrl-C had ended the first ones (#294 review).
    """

    @pytest.mark.parametrize("how", [("group", 2), ("group", 15), "terminal"],
                             ids=["sigint-to-the-group", "sigterm-to-the-group",
                                  "ctrl-c-at-the-terminal"])
    def test_a_stop_ends_the_commands_of_a_scan_and_starts_no_more(self, how, tmp_path):
        import signal

        marks = tmp_path / "marks"
        marks.mkdir()
        first = tuple(marks / f"pid-{item}-0" for item in range(8))
        timings = {}
        try:
            out, err, code = _interrupt(
                tmp_path, _SCANS_ON_THE_POOL, [marks], [first], how=[how], timings=timings)
            assert code == -signal.SIGINT, err
            assert timings["ended"] - timings["first"] < 4
            assert sorted(path.name for path in marks.iterdir()) == sorted(
                path.name for path in first)
            assert not [path for path in first if _running(path)]
        finally:
            for path in marks.glob("pid-*"):
                if _running(path):
                    os.kill(int(path.read_text()), signal.SIGKILL)

    @pytest.mark.parametrize("terminal", [False, True],
                             ids=["without-a-terminal", "with-a-terminal"])
    def test_a_sigint_a_reads_thread_takes_still_stops_the_scan_at_once(
            self, terminal, tmp_path):
        """The kernel may hand a signal sent to boxman to any of its
        threads, and Python handles it on the main thread alone, the next
        time that thread runs: waiting on a read's outcome without a
        timeout, it ran only once a read had ended, and the stop began
        then, after every read had started its next command (#309, a
        failure of the test above on CI). The main thread waits a slice at
        a time now. The SIGINT goes to a thread other than the main one
        here, every time."""
        import os
        import signal

        marks = tmp_path / "marks"
        marks.mkdir()
        first = [marks / f"pid-{item}-0" for item in range(8)]
        try:
            out, err, code = _interrupt(
                tmp_path, _A_THREAD_TAKES_THE_SIGINT, [marks], [], terminal=terminal)
            assert code == -signal.SIGINT, err
            stopped = out.split()
            assert stopped[:1] == ["STOPPED"] and float(stopped[1]) < 2, (out, err)
            assert sorted(path.name for path in marks.iterdir()) == sorted(
                path.name for path in first)
            assert not [path for path in first if _running(path)]
        finally:
            for path in marks.glob("pid-*"):
                if _running(path):
                    os.kill(int(path.read_text()), signal.SIGKILL)

    @pytest.mark.parametrize("how", ["alone", "terminal"],
                             ids=["sigint-to-boxman-alone", "ctrl-c-at-the-terminal"])
    def test_a_process_a_reads_command_starts_during_its_stop_is_stopped_too(
            self, how, tmp_path):
        """A read's command that started as the stop began had its SIGINT
        forwarded by the read's runner to the processes there then: one the
        shell forked after that -- the command itself, when the SIGINT
        reached the shell first, or ``b`` of ``a; b``, once ``a`` has handled
        its SIGINT -- never had it, and ran until the SIGTERM, STOP_TIMEOUT
        later (#309, a failure under load of the test above). Each that
        appears during the stop is sent the signal now."""
        import signal
        import time

        up, then = tmp_path / "up", tmp_path / "then"
        started = time.monotonic()
        out, err, code = _interrupt(
            tmp_path, _A_READ_RUNS_A_COMMAND,
            [f"{_program(_TRAPS_SIGINT, up)}; {_program(_TRAPS_SIGINT, then)}"],
            [up], how=how)
        assert code == -signal.SIGINT, (out, err)
        assert (tmp_path / "up.sigint").exists()
        assert not _running(then)
        assert time.monotonic() - started < 9

    def test_a_read_whose_command_the_ctrl_c_ended_starts_no_next_command(self, tmp_path):
        """The terminal's Ctrl-C ended a read's command before the main
        thread's handler had begun the stop, and the read went on to its next
        command, which the Ctrl-C had not reached (#309, a failure of the
        test above under load). A read whose command died of SIGINT waits a
        moment for that stop now, and starts no next command."""
        import os
        import signal
        import time

        marks = tmp_path / "marks"
        marks.mkdir()
        try:
            started = time.monotonic()
            out, err, code = _interrupt(
                tmp_path, _A_READS_COMMAND_DIES_OF_SIGINT, [marks], [])
            assert code == -signal.SIGINT, (out, err)
            assert time.monotonic() - started < 4
            # read 0 starts no next command; another read may have started
            # one before the stop began, and it was stopped: none got to a
            # second, or to the end of any
            assert not list(marks.glob("pid-0-*"))
            assert not list(marks.glob("pid-*-2"))
            assert not list(marks.glob("done-*"))
            assert not [path for path in marks.glob("pid-*") if _running(path)]
        finally:
            for path in marks.glob("pid-*"):
                if _running(path):
                    os.kill(int(path.read_text()), signal.SIGKILL)

    def test_a_read_whose_command_exits_130_with_no_ctrl_c_goes_on(self, tmp_path):
        """An exit of 130, or a death by SIGINT, with no Ctrl-C to boxman is
        no stop: the read returns its result, and the others theirs (#311
        review: it was taken for one, and the scan stopped)."""
        out, err, code = _interrupt(tmp_path, """
from boxman.providers.libvirt.scan_pool import InOrder
from boxman.utils import shell
def read(item):
    command = "sh -c 'exit 130'" if item == 0 else "sleep 0.5"
    return shell.run(command, hide=True, warn=True).exited
with shell.interrupt_on_signals():
    with InOrder(read, range(2), 2) as outcomes:
        print([outcome.result() for outcome in outcomes], flush=True)
""", [], [])
        assert (out.split(), code) == (["[130,", "0]"], 0), err

    def test_a_stop_of_reads_that_ignore_their_sigint_keeps_to_its_grace_and_its_cpu(
            self, tmp_path):
        """While a stop waits, each read's runner looks for processes of its
        command that have not had the signal: reading every process on the
        host every 20 ms, eight of them took seconds of CPU, and the stop ran
        past its grace (#311 review). Each walks its own command's tree now,
        every 100 ms."""
        marks = tmp_path / "marks"
        marks.mkdir()
        out, err, code = _interrupt(tmp_path, f"""
import os, resource, signal, sys, threading, time
from boxman.providers.libvirt.scan_pool import InOrder
from boxman.utils import shell
shell._StoppingLocal.STOP_TIMEOUT = 2
marks = {str(marks)!r}
def read(item):
    shell.run(f"sh -c 'trap \\"\\" INT; touch {{marks}}/up-{{item}}; sleep 60'",
              hide=True, warn=True)
def stop():
    while len(os.listdir(marks)) < 8:
        time.sleep(0.02)
    global used, began
    used, began = resource.getrusage(resource.RUSAGE_SELF), time.monotonic()
    os.kill(os.getpid(), signal.SIGINT)
threading.Thread(target=stop, daemon=True).start()
try:
    with shell.interrupt_on_signals():
        with InOrder(read, range(8), 8) as outcomes:
            [outcome.result() for outcome in outcomes]
except KeyboardInterrupt:
    now = resource.getrusage(resource.RUSAGE_SELF)
    cpu = now.ru_utime - used.ru_utime + now.ru_stime - used.ru_stime
    print("STOPPED", round(time.monotonic() - began, 2), round(cpu, 2), flush=True)
""", [], [])
        stopped = out.split()
        assert stopped[:1] == ["STOPPED"], (out, err)
        wall, cpu = float(stopped[1]), float(stopped[2])
        # SIGTERM after the 2 s grace ends the reads (their trap is INT's)
        assert wall < 3.5, (out, err)
        assert cpu < 1.0, (out, err)

    def test_with_a_terminal_a_stop_ends_even_when_no_helper_can_be_started(self, tmp_path):
        """The signals of a stop came from a helper process alone, and a
        stop with none to be had, at a limit on processes, never ended
        (#294 review): the SIGKILL goes straight to the command now, and the
        others do too when the helper cannot be run."""
        import os
        import resource

        up, after = tmp_path / "up", tmp_path / "after"

        def no_more_processes(child):
            resource.prlimit(child.pid, resource.RLIMIT_NPROC, (1, 1))

        timings = {}
        out, err, code = _interrupt(
            tmp_path, _RUN_IN_A_CHILD,
            [f"trap '' INT TERM; {_program(_IGNORES_SIGINT_AND_SIGTERM, up)}; touch {after}"],
            [_once(up, no_more_processes), up], how=["terminal"], timings=timings,
            env=dict(os.environ, RUN="run", STOP_TIMEOUT="1", TERM_TIMEOUT="1"))
        assert (out.split(), code) == (["STOPPED"], 0), err
        assert timings["ended"] - timings["first"] < 10
        assert not _running(up)
        assert not after.exists()


#: a stand-in for ssh: it sets the terminal as ssh does, without echo or
#: line editing, writes its PID to the file UP names, and puts the
#: terminal back when it is told to end, by SIGTERM or SIGHUP, saying so
#: with ``<UP>.ended``
_SSH = """\
import os, signal, sys, termios, time
before = termios.tcgetattr(0)
raw = termios.tcgetattr(0)
raw[3] &= ~(termios.ICANON | termios.ECHO)
termios.tcsetattr(0, termios.TCSANOW, raw)
def end(*_):
    termios.tcsetattr(0, termios.TCSANOW, before)
    open(os.environ["UP"] + ".ended", "w").close()
    sys.exit(0)
signal.signal(signal.SIGTERM, end)
signal.signal(signal.SIGHUP, end)
with open(os.environ["UP"] + ".tmp", "w") as up:
    up.write(str(os.getpid()))
os.replace(os.environ["UP"] + ".tmp", os.environ["UP"])
time.sleep(60)
end()
"""


@pytest.mark.usefixtures("no_leftovers")
class TestInteractiveSessions:
    """
    ``boxman ssh``, ``exec`` and ``run`` hand the terminal to the program.
    Under the handlers ``main()`` has, a SIGTERM to boxman raised a
    KeyboardInterrupt in ``subprocess.run``, which killed ssh a quarter of a
    second later: ssh could not put the terminal back, and left it without
    echo (#294 review).
    """

    def test_a_sigterm_to_boxman_leaves_ssh_to_put_the_terminal_back(self, tmp_path):
        import os
        import signal
        import subprocess
        import sys
        import termios
        import time
        from pathlib import Path

        bindir, workspace, up = tmp_path / "bin", tmp_path / "ws", tmp_path / "up"
        bindir.mkdir()
        workspace.mkdir()
        (bindir / "ssh").write_text(f"#!{sys.executable}\n" + _SSH)
        (bindir / "ssh").chmod(0o755)
        conf = tmp_path / "conf.yml"
        conf.write_text(
            "version: '1.0'\nproject: ssh_term\n"
            f"workspace:\n  path: {workspace}\n  gateway_host: localhost\n"
            f"clusters:\n  c1:\n    workdir: {workspace}\n    vms:\n      node01: {{}}\n")
        env = dict(os.environ, UP=str(up), HOME=str(tmp_path), TERM="xterm",
                   PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        master, slave = os.openpty()
        boxman = subprocess.Popen(
            [sys.executable, "-c", _SIGINT_AS_PYTHON_SETS_IT + _THE_PTY_AS_ITS_TERMINAL
             + "from boxman.scripts.app import main\nmain()",
             "--conf", str(conf), "ssh", "node01"],
            env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        try:
            deadline = time.monotonic() + 30
            while not up.exists():
                assert boxman.poll() is None and time.monotonic() < deadline
                time.sleep(0.05)
            boxman.send_signal(signal.SIGTERM)
            boxman.wait(timeout=10)
            # boxman has gone, at once, as before; ssh, not killed, ends on
            # its own (the terminal's hangup, here, as boxman led its
            # session) and puts the terminal back
            assert boxman.returncode == -signal.SIGTERM
            if _running(up):
                os.kill(int(up.read_text()), signal.SIGTERM)
            assert _gone_within(up, 5)
            assert (tmp_path / "up.ended").exists()
            line_editing = termios.tcgetattr(master)[3]
            assert line_editing & termios.ICANON and line_editing & termios.ECHO
        finally:
            if boxman.poll() is None:
                os.killpg(boxman.pid, signal.SIGKILL)
                boxman.wait()
            if _running(up):
                os.kill(int(up.read_text()), signal.SIGKILL)
            os.close(master)


@pytest.mark.usefixtures("no_leftovers")
class TestTheGuardianAndTheStopState:

    def test_a_guardian_that_died_is_replaced(self, tmp_path):
        """A guardian killed, or crashed, was taken for there to the end,
        and nothing ended the commands of a boxman killed after it (#294
        review)."""
        import os
        import signal

        up, go, gone = tmp_path / "up", tmp_path / "go", tmp_path / "guardian-gone"

        def kill_the_guardian(child):
            for name in os.listdir("/proc"):
                try:
                    with open(f"/proc/{name}/stat") as stat:
                        parent = int(stat.read().rsplit(")", 1)[1].split()[1])
                    with open(f"/proc/{name}/cmdline", "rb") as cmdline:
                        guardian = parent == child.pid and b"groups = set()" in cmdline.read()
                except (OSError, ValueError, IndexError):
                    continue
                if guardian:
                    os.kill(int(name), signal.SIGKILL)
                    gone.touch()
            go.touch()

        out, err, code = _interrupt(tmp_path, f"""
import os, sys, time
from boxman.utils import shell
shell.run("true", hide=True)
open({str(tmp_path / 'ran')!r}, "w").close()
while not os.path.exists({str(go)!r}):
    time.sleep(0.05)
shell.run(sys.argv[1], hide=True, warn=True)
""", [f"{_program(_TRAPS_SIGINT, up)}; true"],
            [_once(tmp_path / "ran", kill_the_guardian), up],
            how=[("group", signal.SIGKILL)])
        assert gone.exists(), err
        assert code == -signal.SIGKILL, err
        assert _gone_within(up, 5)

    def test_a_guardian_that_could_not_start_is_tried_again_and_warned_of_once(
            self, monkeypatch):
        import os

        from boxman import log
        from boxman.utils import shell

        warned, starts = [], []
        real_start = shell._start_guardian

        def start():
            starts.append(True)
            return None if len(starts) == 1 else real_start()

        monkeypatch.setattr(shell, "_guardian", None)
        monkeypatch.setattr(shell, "_guarded", set())
        monkeypatch.setattr(shell, "_guardian_warned", False)
        monkeypatch.setattr(shell, "_start_guardian", start)
        monkeypatch.setattr(log, "warning", lambda message, *a, **k: warned.append(message))
        try:
            assert shell._live_guardian() is None
            assert shell._live_guardian() is None or len(starts) == 2
            pipe = shell._live_guardian()
            assert pipe is not None and shell._guardian[0] == os.getpid()
            assert len(warned) == 1
        finally:
            shell._drop_guardian()

    def test_a_stop_signal_from_before_a_run_sends_its_command_no_second_sigint(
            self, tmp_path):
        """A SIGTERM outside any run left its mark for the next stop: with a
        terminal, a Ctrl-C there then had the command sent a second SIGINT,
        as if a signal had asked for the stop (#294 review)."""
        import os

        up, sleeping = tmp_path / "up", tmp_path / "sleeping"
        out, err, code = _interrupt(tmp_path, f"""
import sys, time
from boxman.utils import shell
with shell.interrupt_on_signals():
    try:
        open({str(sleeping)!r}, "w").close()
        time.sleep(30)
    except KeyboardInterrupt:
        pass
    try:
        shell.run(sys.argv[1], hide=True, warn=True)
    except KeyboardInterrupt:
        print("STOPPED", flush=True)
""", [_program(_TRAPS_SIGINT, up)], [sleeping, up], how=["sigterm", "terminal"],
            env=dict(os.environ))
        assert (out.split(), code) == (["STOPPED"], 0), err
        assert (tmp_path / "up.sigints").read_text().splitlines() == ["SIGINT"]


class TestWhatAStopInterrupts:

    def test_a_ledger_record_is_never_left_half_written(self, tmp_path):
        """#305's ledger of overlays to remove writes each record to a
        staging file, syncs it and renames it into place: a stop, which
        raises the KeyboardInterrupt wherever the code is, never leaves a
        record half written. A SIGTERM, at a moment of its own each time,
        while records are written one after another."""
        import random
        import signal

        from boxman.providers.libvirt.pending_overlays import PendingOverlays

        ledger, layers = tmp_path / "ledger", tmp_path / "layers"
        layers.mkdir()
        for each in range(50):
            (layers / f"nixos.{each}").write_text("overlay")
        writing = tmp_path / "writing"
        for round_ in range(8):
            out, err, code = _interrupt(tmp_path, f"""
import itertools
from boxman.providers.libvirt.pending_overlays import PendingOverlays
from boxman.utils import shell
ledger = PendingOverlays(directory={str(ledger)!r}, scope="local qemu:///system")
open({str(writing)!r}, "w").close()
with shell.interrupt_on_signals():
    for each in itertools.cycle(range(50)):
        ledger.record("vm{round_}", [([{str(layers)!r} + f"/nixos.{{each}}"], "/isos/nixos.iso")])
""", [], [_once(writing, lambda child: None), random.uniform(0.05, 0.4), writing],
                how=["sigterm"])
            assert code == -signal.SIGINT, err
            writing.unlink()
        records = PendingOverlays(directory=str(ledger), scope="local qemu:///system")
        assert records.chains()
        assert records.unreadable == {}


class TestWhatAStopReadsAndWaitsFor:

    def test_a_process_whose_name_is_not_utf8_is_read_and_skipped_without_a_crash(
            self, tmp_path, monkeypatch):
        """The kernel cuts a program's name at 15 bytes, inside a character
        as well: reading every process on the host, a stop raised
        UnicodeDecodeError on one, sent no signal, and the command outlived
        boxman (#311 review)."""
        import os
        import shutil
        import subprocess

        from boxman.utils import shell

        odd = tmp_path / ("\u00e9" * 9)
        shutil.copy("/bin/sleep", odd)
        named = subprocess.Popen([str(odd), "30"])
        try:
            with open(f"/proc/{named.pid}/comm", "rb") as comm:
                assert comm.read().rstrip(b"\n") == ("\u00e9" * 9).encode()[:15]
            assert shell._stat(named.pid)[0] in "RSD"
            # the walk through children lists, and the scan of every process
            assert os.getpid() in shell._tree(os.getpid())
            monkeypatch.setattr(shell, "_children_listed", lambda: False)
            assert os.getpid() in shell._tree(os.getpid())
            assert named.pid in shell._tree(os.getpid())
        finally:
            named.kill()
            named.wait()

    @pytest.mark.parametrize("handler", ["pythons", "interrupt_on_signals"])
    def test_a_ctrl_c_in_the_cancel_on_the_way_out_still_cancels_and_waits(
            self, tmp_path, handler):
        """InOrder's way out cancelled the items not yet started outside the
        guard that waits for the rest: a further Ctrl-C there left items in
        the queue, and returned without waiting, so the pool read on past the
        scan (#311 review). A KeyboardInterrupt is not raised inside the
        pool's workings now; the cancel goes on, the reads under way are
        waited for, and the scan ends with the first."""
        out, err, code = _interrupt(tmp_path, """
import contextlib, signal, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from boxman.providers.libvirt.scan_pool import InOrder
from boxman.utils import shell
started, lock = [], threading.Lock()
def read(item):
    with lock:
        started.append(item)
    time.sleep(0.05)
    return item
real, cut = ThreadPoolExecutor.shutdown, []
def shutdown(pool, wait=True, *, cancel_futures=False):
    if not wait and not cut:
        # a further Ctrl-C, its handler run here, inside the cancel
        cut.append(True)
        signal.raise_signal(signal.SIGINT)
    return real(pool, wait=wait, cancel_futures=cancel_futures)
ThreadPoolExecutor.shutdown = shutdown
handling = (shell.interrupt_on_signals() if sys.argv[1] == "interrupt_on_signals"
            else contextlib.nullcontext())
try:
    with handling:
        with InOrder(read, range(200), 4) as outcomes:
            outcomes[0].result()
            raise KeyboardInterrupt
except KeyboardInterrupt:
    ended = len(started)
    time.sleep(0.3)
    print("STOPPED", len(cut), ended, len(started), signal.getsignal(signal.SIGINT).__name__,
          flush=True)
""", [handler], [])
        stopped = out.split()
        assert stopped[:2] == ["STOPPED", "1"], (out, err)
        # no read began after the scan, and few before its end
        assert stopped[2] == stopped[3] and int(stopped[2]) < 200, (out, err)
        # Python's own handler is back
        assert stopped[4] == "default_int_handler", (out, err)

    def test_ctrl_cs_on_the_way_out_still_wait_for_the_reads_under_way(self, tmp_path):
        """A further Ctrl-C on the way out cut the wait for the reads under
        way short, and the scan ended while they read on (#311 review). A
        hundred Ctrl-Cs here, at the main thread, while the scan waits on its
        way out: it waits for those reads still, ends with them, and starts
        no more."""
        out, err, code = _interrupt(tmp_path, """
import random, signal, threading, time
from boxman.providers.libvirt.scan_pool import InOrder
from boxman.utils import shell
main = threading.get_ident()
started, ended, lock = [], [], threading.Lock()
def read(item):
    with lock:
        started.append(item)
    time.sleep(1.0)
    with lock:
        ended.append(item)
    return item
def ctrl_cs():
    time.sleep(0.1)
    for _ in range(100):
        signal.pthread_kill(main, signal.SIGINT)
        time.sleep(random.uniform(0.0002, 0.003))
threading.Thread(target=ctrl_cs, daemon=True).start()
began = time.monotonic()
try:
    with shell.interrupt_on_signals():
        with InOrder(read, range(2000), 8) as outcomes:
            [outcome.result() for outcome in outcomes]
except KeyboardInterrupt:
    print("STOPPED", round(time.monotonic() - began, 2), len(started), len(ended), flush=True)
""", [], [])
        stopped = out.split()
        assert stopped[:1] == ["STOPPED"], (out, err)
        # the reads under way ended before the scan did, and no more began
        assert 1.0 <= float(stopped[1]) < 3, (out, err)
        assert stopped[2] == stopped[3] and int(stopped[2]) <= 16, (out, err)


    def test_a_ctrl_c_in_the_wait_for_a_read_leaves_no_lock_of_its_held(self, tmp_path):
        """A KeyboardInterrupt raised in ``concurrent.futures.wait``, or in
        ``Future.result`` with a timeout, can leave the main thread holding
        the future's lock, or raise RuntimeError: the read that ends that
        future then waited for the lock for good, and the scan for the read,
        with eight reads under way and none to end (#311 review). A Ctrl-C
        in the wait begins the stop there and then, and is raised once the
        wait holds nothing."""
        out, err, code = _interrupt(tmp_path, """
import random, signal, threading, time
from concurrent.futures import Future
from boxman.providers.libvirt import scan_pool
# no sleep in the wait: every Ctrl-C lands in what it does to look
scan_pool.POLL_S = 0
main = threading.get_ident()
future, done = Future(), threading.Event()
def ctrl_cs():
    # once the main thread is in the loop below
    time.sleep(0.2)
    while not done.is_set():
        signal.pthread_kill(main, signal.SIGINT)
        time.sleep(random.uniform(0.0002, 0.003))
taken = held = 0
other = []
end = time.monotonic() + 2.2
def looks():
    global taken
    while time.monotonic() < end:
        try:
            scan_pool._Outcome(future).result()
        except KeyboardInterrupt:
            taken += 1
        except Exception as error:
            other.append(type(error).__name__)
        held_now()
def held_now():
    global held
    if future._condition._is_owned():
        held += 1
        while future._condition._is_owned():
            future._condition.release()
threading.Thread(target=ctrl_cs, daemon=True).start()
while time.monotonic() < end:
    try:
        looks()
    except KeyboardInterrupt:
        # one raised outside the wait, by Python's own handler
        held_now()
done.set()
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
print("TAKEN", taken, "HELD", held, "OTHER", len(other), flush=True)
""", [], [])
        counts = out.split()
        assert counts[:1] == ["TAKEN"], (out, err)
        assert int(counts[1]) > 100, (out, err)
        assert (counts[3], counts[5]) == ("0", "0"), (out, err)


class TestNoHandlerSwallowsACtrlC:
    """
    A KeyboardInterrupt is not an Exception, so ``except Exception`` lets
    it through; a bare ``except:``, or one that names BaseException or
    KeyboardInterrupt, stops it short of the top, and so does a
    ``contextlib.suppress`` of either, and boxman goes on to its next step
    (#294). Each such handler in src raises, or exits, whatever happened,
    but for the few that end a stop themselves.
    """

    #: (module, function) of the handlers that end a stop on purpose
    ENDS_A_STOP = {
        # the workers' cleanup, with SIGINT ignored
        ("boxman/manager.py", "_stop_parallel"),
        # another Ctrl-C hurries the stop on, to its end
        ("boxman/utils/shell.py", "_stop"),
    }

    BROAD = ("BaseException", "KeyboardInterrupt")

    @staticmethod
    def _names(node):
        import ast
        return [getattr(each, "id", getattr(each, "attr", None))
                for each in (node.elts if isinstance(node, ast.Tuple) else [node])]

    def _ends_the_run(self, handler):
        """A ``raise``, or a ``sys.exit(...)``, among the handler's own
        statements: not one only some paths through it reach."""
        import ast
        for statement in handler.body:
            if isinstance(statement, ast.Raise):
                return True
            call = getattr(statement, "value", None)
            if (isinstance(statement, ast.Expr) and isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute) and call.func.attr == "exit"
                    and getattr(call.func.value, "id", None) == "sys"):
                return True
        return False

    def test_every_broad_handler_in_src_raises_again(self):
        import ast
        import pathlib

        src = pathlib.Path(__file__).resolve().parent.parent / "src"
        offenders = []
        for py in sorted(src.rglob("*.py")):
            module = str(py.relative_to(src))
            tree = ast.parse(py.read_text())
            # every node, with the function it is in, "<module>" outside any
            within = {}
            for function in ast.walk(tree):
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for node in ast.walk(function):
                        within[id(node)] = function.name
            for node in ast.walk(tree):
                where = (module, within.get(id(node), "<module>"))
                if where in self.ENDS_A_STOP:
                    continue
                if isinstance(node, ast.ExceptHandler):
                    broad = node.type is None or any(
                        name in self.BROAD for name in self._names(node.type))
                    if broad and not self._ends_the_run(node):
                        offenders.append(f"{module}:{node.lineno} in {where[1]}")
                elif (isinstance(node, ast.Call)
                        and getattr(node.func, "attr", getattr(node.func, "id", None)) == "suppress"
                        and any(name in self.BROAD for arg in node.args
                                for name in self._names(arg))):
                    offenders.append(f"{module}:{node.lineno} suppress in {where[1]}")
        assert not offenders, sorted(set(offenders))

    def test_each_ctrl_c_held_back_is_raised_by_the_code_within(self):
        """Within ``ctrl_c_held_back`` the handlers raise no
        KeyboardInterrupt: the code that holds a Ctrl-C back looks at
        ``taken``, and raises it (#311 review)."""
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src"
        users = []
        for path in sorted(src.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                         and getattr(node.func, "id", getattr(node.func, "attr", None))
                         == "ctrl_c_held_back"]
                if not calls:
                    continue
                users.append(function.name)
                looks = any(isinstance(node, ast.Attribute) and node.attr == "taken"
                            for node in ast.walk(function))
                raises = any(isinstance(node, ast.Raise)
                             and "KeyboardInterrupt" in ast.unparse(node)
                             for node in ast.walk(function))
                assert looks and raises, f"{path.name}: {function.name}"
        assert sorted(users) == ["__enter__", "__exit__", "result"], users
