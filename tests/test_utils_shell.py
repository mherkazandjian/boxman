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
#: appears. With TERMINAL set, the pty on its stdin is its terminal
_RUN_IN_A_CHILD = """
import os, subprocess, sys, time
from boxman.utils import shell
if os.environ.get("TERMINAL"):
    import fcntl, termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
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

#: the same, with a SIGTERM taken as a Ctrl-C, as boxman's main() takes it
_GOES_ON_AFTER_A_RUN_UNDER_SIGTERM = """
import sys
from boxman.utils import shell
with shell.interrupt_on_sigterm():
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


def _interrupt(tmp_path, script, args, signal_at, how="alone", env=None,
               release=None, typed=None):
    """Run the child *script* with *args*, and interrupt it at each point
    in *signal_at*: once the file there, or every file of a tuple there, has
    appeared; return what it printed, and how it exited.

    *how*: ``alone``, a SIGINT to its Python alone; ``group``, to its whole
    process group, the command's too unless that has a session of its own;
    ``terminal``, a Ctrl-C typed at a pty that is its controlling terminal,
    which signals the terminal's foreground group; ``sigterm``, a SIGTERM to
    its Python alone. *typed* is typed at that terminal first.

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

    env = dict(env or os.environ,
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    master = slave = None
    if how == "terminal":
        master, slave = os.openpty()
        env["TERMINAL"] = "1"
    child = subprocess.Popen(
        [sys.executable, "-c", script, *map(str, args)], env=env, stdin=slave,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)
    if slave is not None:
        os.close(slave)
    try:
        if typed:
            os.write(master, typed)
        for when in signal_at:
            files = when if isinstance(when, tuple) else (when,)
            deadline = time.monotonic() + 30
            while not all(path.exists() for path in files):
                assert child.poll() is None and time.monotonic() < deadline, \
                    child.stderr.read()
                time.sleep(0.05)
            if how == "terminal":
                os.write(master, b"\x03")
            elif how == "group":
                os.killpg(child.pid, signal.SIGINT)
            else:
                os.kill(child.pid, signal.SIGTERM if how == "sigterm" else signal.SIGINT)
        if release:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            release.touch()
        out, err = child.communicate(timeout=30)
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
        ``interrupt_on_sigterm``, as boxman's ``main()`` runs, boxman stops
        the command first."""
        import signal

        up, next_step = tmp_path / "up", tmp_path / "next-step"
        out, err, code = _interrupt(
            tmp_path, _GOES_ON_AFTER_A_RUN_UNDER_SIGTERM,
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
            [], how="terminal", env=self._env(), typed=b"yes\n")
        assert (out.split(), code) == (["RETURNED", "0"], 0), err
        assert answer.read_text() == "yes\n"


class TestNoHandlerSwallowsACtrlC:
    """
    A KeyboardInterrupt is not an Exception, so ``except Exception`` lets
    it through; a bare ``except:``, or one that names BaseException or
    KeyboardInterrupt, stops it short of the top, and boxman goes on to its
    next step (#294). Each in src raises again, but for the few that end a
    stop themselves.
    """

    #: (module, function) of the handlers that end a stop on purpose
    ENDS_A_STOP = {
        # a worker stopped with its command exits 130
        ("boxman/manager.py", "_parallel_worker"),
        # the workers' cleanup, with SIGINT ignored
        ("boxman/manager.py", "_stop_parallel"),
        # another Ctrl-C hurries the stop on, to its end
        ("boxman/utils/shell.py", "_stop"),
    }

    def test_every_broad_handler_in_src_raises_again(self):
        import ast
        import pathlib

        def broad(handler):
            names = (handler.type.elts if isinstance(handler.type, ast.Tuple)
                     else [handler.type])
            return any(name is None or getattr(name, "id", getattr(name, "attr", None))
                       in ("BaseException", "KeyboardInterrupt") for name in names)

        src = pathlib.Path(__file__).resolve().parent.parent / "src"
        offenders = []
        for py in sorted(src.rglob("*.py")):
            module = str(py.relative_to(src))
            for function in ast.walk(ast.parse(py.read_text())):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    if (isinstance(node, ast.ExceptHandler) and broad(node)
                            and not any(isinstance(each, ast.Raise) for each in ast.walk(node))
                            and (module, function.name) not in self.ENDS_A_STOP):
                        offenders.append(f"{module}:{node.lineno} in {function.name}")
        assert not offenders, sorted(set(offenders))
