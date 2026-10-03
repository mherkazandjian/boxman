"""
The guard in ``tests/conftest.py`` (#214): outside the integration tier,
``docker``, ``virsh`` and ``sudo`` are fakes that record each call and run
nothing, and a test that calls one fails.
"""

import os
import shutil
import subprocess

import invoke
import pytest
from tests import conftest

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", conftest.HostCommandGuard.COMMANDS)
def test_a_call_reaches_the_fake_and_runs_nothing(name, host_commands):
    result = subprocess.run([name, "ps", "-a"], capture_output=True,
                            text=True)
    assert result.returncode == 1
    assert f"{name} is for the integration tier only" in result.stderr
    assert host_commands.take() == [f"{name} ps -a"]


def test_a_command_run_through_a_shell_reaches_it_too(host_commands):
    """boxman runs its commands through invoke, whose shell searches
    ``PATH`` itself."""
    result = invoke.run("docker inspect -f '{{json .Mounts}}' c",
                        hide=True, warn=True, in_stream=False)
    assert not result.ok
    assert host_commands.take() == ["docker inspect -f {{json .Mounts}} c"]


#: A session run under the guard. OUTER is what ``docker`` resolves to in
#: the test running it, which is itself guarded.
_SESSION = """
import shutil
import subprocess

import pytest

OUTER = {outer!r}


def test_a_default_tier_test_gets_the_fakes():
    assert shutil.which("docker") != OUTER


@pytest.fixture(scope="module")
def listing():
    return subprocess.run(["docker", "ps", "-q"]).returncode


def test_a_module_scoped_fixture_is_caught(listing):
    pass


def test_a_call_fails_the_test():
    subprocess.run(["virsh", "list", "--all"])


@pytest.mark.integration
def test_an_integration_test_keeps_the_path_it_had():
    assert shutil.which("docker") == OUTER
"""


def test_a_test_that_calls_one_fails_at_teardown(pytester):
    pytester.makeini("[pytest]\nmarkers = integration: the real tools\n")
    pytester.makepyfile(_SESSION.format(outer=shutil.which("docker")))

    result = pytester.runpytest_inprocess(plugins=[conftest])

    result.assert_outcomes(passed=4, errors=2)
    result.stdout.fnmatch_lines([
        "*ERROR at teardown of test_a_module_scoped_fixture_is_caught*",
        "*    docker ps -q",
        "*ERROR at teardown of test_a_call_fails_the_test*",
        "*    virsh list --all",
    ])


#: Sessions that abort while a guarded test runs, which skips that test's
#: ``pytest_runtest_teardown``, and the exit code each ends with.
_ABORTED = {
    "pytest.exit": ("""
import pytest


def test_aborts_the_session():
    pytest.exit("abort", returncode=3)
""", 3),
    "ctrl-c": ("""
import pytest


@pytest.fixture
def interrupted():
    raise KeyboardInterrupt


def test_aborts_the_session(interrupted):
    pass
""", pytest.ExitCode.INTERRUPTED),
}


@pytest.mark.parametrize("abort", _ABORTED)
def test_a_session_aborted_mid_test_restores_path(pytester, abort):
    """The guard is still installed when such a session ends; ``PATH``
    must come back before its fakes are deleted, or whoever called
    ``pytest.main()`` is left with a ``PATH`` led by a missing directory."""
    session, exit_code = _ABORTED[abort]
    pytester.makepyfile(session)
    before = os.environ["PATH"]

    result = pytester.runpytest_inprocess(plugins=[conftest],
                                          no_reraise_ctrlc=True)

    assert result.ret == exit_code
    assert os.environ["PATH"] == before
