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
#: the runner can tell boxman it was a Ctrl-C; it says it is up by creating
#: the file named first, and, a second after its SIGINT (not a kill), that
#: it is done, by creating the same name with ``.sigint`` added: the runner
#: must wait for that before boxman goes on to clean up after it
_TRAPS_SIGINT = (
    "import pathlib, signal, sys, time\n"
    "up = pathlib.Path(sys.argv[1])\n"
    "def stop(*_):\n"
    "    up.with_name(up.name + '.stopping').touch()\n"
    "    time.sleep(1)\n"
    "    up.with_name(up.name + '.sigint').touch()\n"
    "    sys.exit(3)\n"
    "signal.signal(signal.SIGINT, stop)\n"
    "up.touch()\n"
    "time.sleep(60)\n"
)

#: a child that runs a command with run_stoppable and says how that ended.
#: With a second argument, invoke is held up between starting the command
#: and waiting for it, the command's PID is written to that file, and the
#: child says whether the command is still there when the interrupt
#: reaches it
_RUN_STOPPABLE_IN_A_CHILD = """
import os, sys, time
from boxman.utils import shell
if len(sys.argv) > 2:
    real = shell._StoppingLocal.create_io_threads
    def held_up(self):
        with open(sys.argv[2] + ".tmp", "w") as pid_file:
            pid_file.write(str(self.process.pid))
        os.replace(sys.argv[2] + ".tmp", sys.argv[2])
        time.sleep(60)
        return real(self)
    shell._StoppingLocal.create_io_threads = held_up
try:
    result = shell.run_stoppable(sys.argv[1], hide=True, warn=os.environ.get("WARN") != "no")
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

    def _sigint_boxman_alone(self, tmp_path, ready, held_up=None, env=None, again_when=None):
        """Run the child, SIGINT its Python alone once *ready* exists (and
        again once *again_when* does), and return what the child printed."""
        import os
        import shlex
        import signal
        import subprocess
        import sys
        import time
        from pathlib import Path

        command = " ".join(shlex.quote(part) for part in (
            sys.executable, "-c", _TRAPS_SIGINT, str(tmp_path / "up")))
        env = dict(env or os.environ,
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        child = subprocess.Popen(
            [sys.executable, "-c", _RUN_STOPPABLE_IN_A_CHILD, command,
             *([str(held_up)] if held_up else [])],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        try:
            for when in (ready, again_when) if again_when else (ready,):
                deadline = time.monotonic() + 30
                while not when.exists():
                    assert child.poll() is None and time.monotonic() < deadline, \
                        child.stderr.read()
                    time.sleep(0.05)
                os.kill(child.pid, signal.SIGINT)
            out, err = child.communicate(timeout=20)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        return out, err

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
        out, err = self._sigint_boxman_alone(tmp_path, tmp_path / "up", env=env)
        assert out.split() == ["STOPPED"], err
        assert (tmp_path / "up.sigint").exists()

    def test_a_sigint_before_invoke_waits_stops_the_command(self, tmp_path):
        """invoke passes on only a KeyboardInterrupt that comes while it
        waits; one between starting the command and waiting for it left the
        command running on its own (#227 review)."""
        import os

        pid_file = tmp_path / "pid"
        out, err = self._sigint_boxman_alone(tmp_path, pid_file, held_up=pid_file)
        # gone by the time the interrupt reached the caller, stopped by its
        # SIGINT rather than killed, and not running on without boxman
        assert out.split() == ["STOPPED", "GONE"], err
        assert (tmp_path / "up.sigint").exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_a_second_sigint_while_it_stops_still_waits_for_it(self, tmp_path):
        """A second Ctrl-C while the command ends after the first one cut
        that wait short, and the interrupt reached the caller with the
        command still running (#227 review). Now it is killed, and still
        waited for."""
        pid_file = tmp_path / "pid"
        out, err = self._sigint_boxman_alone(
            tmp_path, pid_file, held_up=pid_file, again_when=tmp_path / "up.stopping")
        assert out.split() == ["STOPPED", "GONE"], err
