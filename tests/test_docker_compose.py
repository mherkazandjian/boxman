"""
Integration test for the docker-compose based libvirt environment.

Requires Docker with compose v2 and /dev/kvm on the host.
"""

import os
import re
import time

import invoke
import pytest

DOCKER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "containers", "docker",
)

#: How long the entrypoint gets to reach "libvirt is ready." after `make up`.
SETTLE_TIMEOUT = 120


def _run(cmd, warn=False):
    """Run a shell command in the docker dir and return the invoke Result."""
    ctx = invoke.context.Context()
    with ctx.cd(DOCKER_DIR):
        return ctx.run(cmd, hide=True, warn=warn, in_stream=False)


def _container_id():
    """The compose service's container, running or not ("" if there is none)."""
    return _run("docker compose ps -a -q", warn=True).stdout.strip()


def _container_state(cid):
    """``(status, restart count)`` as docker reports them."""
    result = _run(
        f"docker inspect -f '{{{{.State.Status}}}} {{{{.RestartCount}}}}' {cid}",
        warn=True)
    status, _, restarts = result.stdout.strip().partition(" ")
    return status or "unknown", int(restarts) if restarts.isdigit() else 0


def _container_logs(cid, tail=None):
    """The container's stdout and stderr, interleaved as it wrote them."""
    tail_arg = f"--tail {tail} " if tail else ""
    return _run(f"docker logs {tail_arg}{cid} 2>&1", warn=True).stdout


def _not_staying_up():
    """
    Why the container cannot be serving anything, or None if it is up.

    `docker compose ps --status running` is not enough: a container in a
    restart loop is "running" for a few seconds each time round, so a check
    that happens to land in one of those windows passes (#205).
    """
    cid = _container_id()
    if not cid:
        return "there is no container"
    status, restarts = _container_state(cid)
    if status == "running" and restarts == 0:
        return None
    return (
        f"the container is not staying up (status '{status}', restarted "
        f"{restarts} time(s)). Last 30 lines of docker logs:\n"
        f"{_container_logs(cid, tail=30)}")


def _require_staying_up(what="the test"):
    """Fail, saying why, unless the container is up and has stayed up."""
    problem = _not_staying_up()
    if problem is not None:
        pytest.fail(f"{what} cannot work: {problem}")


def _wait_until_settled():
    """
    Wait until the entrypoint reports libvirt ready, or the container shows
    it never will. Failing is left to the tests, which say why.
    """
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while time.monotonic() < deadline:
        cid = _container_id()
        if cid and "libvirt is ready." in _container_logs(cid):
            return
        if _not_staying_up() is not None:
            return
        time.sleep(2)


@pytest.fixture(scope="module")
def docker_compose_env():
    """Bring up the docker-compose environment for the test module, tear down after."""
    _run("make up")
    _wait_until_settled()
    yield
    _run("make down", warn=True)


@pytest.mark.integration
class TestDockerCompose:

    def test_container_is_running(self, docker_compose_env):
        """Verify the container is running, and not in a restart loop."""
        result = _run("docker compose ps --status running -q")
        assert result.stdout.strip(), "no running containers found"
        _require_staying_up("the container")

    def test_default_network_is_active(self, docker_compose_env):
        """The entrypoint defines and starts libvirt's default network from
        the seeded /etc/libvirt — the step a partially seeded volume broke."""
        _require_staying_up("the default network")
        cid = _container_id()
        result = _run(f"docker exec {cid} virsh net-info default", warn=True)
        active = re.search(r"^Active:\s+yes$", result.stdout, re.M)
        assert result.ok and active, (
            f"the default network is not active:\n{result.stdout}"
            f"{result.stderr}\nLast 30 lines of docker logs:\n"
            f"{_container_logs(cid, tail=30)}")

    def test_ssh_into_container(self, docker_compose_env):
        """SSH into the container and run a simple command."""
        max_attempts = 5
        last_err = None
        for _attempt in range(1, max_attempts + 1):
            # a container that is not staying up drops every connection,
            # which reads as an ssh problem when it is not one
            _require_staying_up("SSH")
            result = _run('make ssh cmd="echo ok"', warn=True)
            if result.ok and "ok" in result.stdout:
                return  # success
            last_err = result.stderr
            time.sleep(2)

        cid = _container_id()
        pytest.fail(
            f"SSH into container failed after {max_attempts} attempts. "
            f"Last stderr: {last_err}\nLast 30 lines of docker logs:\n"
            f"{_container_logs(cid, tail=30) if cid else '(no container)'}"
        )
