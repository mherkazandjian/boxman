"""
Integration test for the docker-compose based libvirt environment.

Requires Docker with compose v2 and /dev/kvm on the host.
"""

import os
import time
import invoke
import pytest

DOCKER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "containers", "docker",
)


def _run(cmd, warn=False):
    """Run a shell command in the docker dir and return the invoke Result."""
    ctx = invoke.context.Context()
    with ctx.cd(DOCKER_DIR):
        return ctx.run(cmd, hide=True, warn=warn, in_stream=False)


@pytest.fixture(scope="module")
def docker_compose_env():
    """Bring up the docker-compose environment for the test module, tear down after."""
    _run("make up")
    # give services a moment to fully start (sshd, libvirtd, etc.)
    time.sleep(5)
    yield
    _run("make down", warn=True)


@pytest.mark.integration
class TestDockerCompose:

    def test_container_is_running(self, docker_compose_env):
        """Verify the container is in 'running' state."""
        result = _run("docker compose ps --status running -q")
        assert result.stdout.strip(), "no running containers found"

    def test_ssh_into_container(self, docker_compose_env):
        """SSH into the container and run a simple command."""
        max_attempts = 5
        last_err = None
        for attempt in range(1, max_attempts + 1):
            result = _run('make ssh cmd="echo ok"', warn=True)
            if result.ok and "ok" in result.stdout:
                return  # success
            last_err = result.stderr
            time.sleep(2)

        pytest.fail(
            f"SSH into container failed after {max_attempts} attempts. "
            f"Last stderr: {last_err}"
        )
