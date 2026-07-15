"""
ComposeRunner — drive ``docker compose`` for one cluster's generated
``docker-compose.yml``.

Stateless and constructed per operation; shells out **directly on the
host** via ``boxman.utils.shell.run`` (the same pattern
``ContainerlabManager`` and ``runtime/docker_compose.py`` use for their
host-side ``docker compose`` calls — the docker-compose *provider* requires
``runtime: local``, so there is nothing to wrap). Readiness uses
``docker compose up --wait`` (design decision D1): it blocks until every
service is ``healthy`` (when a healthcheck exists) or ``running``.
"""

from __future__ import annotations

import shlex
import shutil

from boxman import log
from boxman.exceptions import ProvisionError, RuntimeUnavailable
from boxman.utils.shell import run

#: default per-cluster readiness timeout, seconds (D1)
DEFAULT_READINESS_TIMEOUT = 120


class ComposeRunner:
    def __init__(self, project: str, compose_file: str, workdir: str, logger=None) -> None:
        self.project = project
        self.compose_file = compose_file
        self.workdir = workdir
        self.logger = logger or log

    def preflight(self) -> None:
        """Verify ``docker`` and the ``docker compose`` v2 plugin exist."""
        if shutil.which("docker") is None:
            raise RuntimeUnavailable(
                "'docker' is not on PATH — the docker-compose provider needs "
                "Docker with the Compose v2 plugin installed on this host."
            )
        if not run("docker compose version", hide=True, warn=True).ok:
            raise RuntimeUnavailable(
                "'docker compose' (Compose v2 plugin) is not available — "
                "install it or upgrade Docker."
            )

    def up(self, timeout: int = DEFAULT_READINESS_TIMEOUT):
        """``docker compose up -d --wait`` — create+start and block on
        readiness. Raises :class:`ProvisionError` on failure/timeout."""
        cmd = f"{self._base()} up -d --wait --wait-timeout {int(timeout)}"
        result = run(cmd, warn=True)
        if not result.ok:
            raise ProvisionError(
                f"'docker compose up' failed for project '{self.project}' "
                f"(timeout {timeout}s): {(result.stderr or result.stdout).strip()}"
            )
        return result

    def down(self):
        """``docker compose down`` — remove containers + networks, keep named volumes."""
        return run(f"{self._base()} down --remove-orphans", warn=True)

    def down_volumes(self):
        """``docker compose down --volumes`` — remove everything including named volumes."""
        return run(f"{self._base()} down --volumes --remove-orphans", warn=True)

    def stop(self):
        """``docker compose stop`` — stop containers, keep them (reversible by :meth:`start`)."""
        return run(f"{self._base()} stop", warn=True)

    def start(self):
        """``docker compose start`` — start previously-stopped containers."""
        return run(f"{self._base()} start", warn=True)

    def ps(self):
        """``docker compose ps`` (captured)."""
        return run(f"{self._base()} ps", hide=True, warn=True)

    # -- internals ---------------------------------------------------------

    def _base(self) -> str:
        return (
            f"docker compose -p {shlex.quote(self.project)} "
            f"-f {shlex.quote(self.compose_file)} "
            f"--project-directory {shlex.quote(self.workdir)}"
        )
