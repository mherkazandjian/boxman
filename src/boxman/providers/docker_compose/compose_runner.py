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

#: default interactive shell for ``boxman exec`` (POSIX-universal; override
#: with ``--shell``)
DEFAULT_EXEC_SHELL = "sh"


class ComposeRunner:
    def __init__(
        self,
        project: str,
        compose_file: str | None = None,
        workdir: str | None = None,
        logger=None,
        use_sudo: bool = False,
    ) -> None:
        self.project = project
        self.compose_file = compose_file
        self.workdir = workdir
        self.logger = logger or log
        #: ``"sudo "`` prefix when the provider config sets ``use_sudo: true``
        #: (hosts where docker needs root — no docker group / not rootless).
        self._sudo = "sudo " if use_sudo else ""

    def preflight(self) -> None:
        """Verify ``docker`` and the ``docker compose`` v2 plugin exist."""
        if shutil.which("docker") is None:
            raise RuntimeUnavailable(
                "'docker' is not on PATH — the docker-compose provider needs "
                "Docker with the Compose v2 plugin installed on this host."
            )
        if not run(f"{self._sudo}docker compose version", hide=True, warn=True).ok:
            raise RuntimeUnavailable(
                "'docker compose' (Compose v2 plugin) is not available — "
                "install it or upgrade Docker."
            )

    def up(self, timeout: int = DEFAULT_READINESS_TIMEOUT):
        """``docker compose up -d --wait`` — create+start and block on
        readiness. Raises :class:`ProvisionError` on failure/timeout."""
        if not self.compose_file:
            raise ProvisionError(
                f"cannot bring up project '{self.project}' without a compose "
                f"file (label-only runners are for teardown)."
            )
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

    def ps_json(self):
        """``docker compose ps --format json --all`` (captured).

        Compose v2 emits one JSON object per line (newline-delimited); include
        ``--all`` so stopped/paused services are reported too (for ``ps`` /
        ``connect_info`` status columns)."""
        return run(f"{self._base()} ps --all --format json", hide=True, warn=True)

    def pause(self, services: list[str] | None = None):
        """``docker compose pause`` (whole project, or the given services)."""
        return run(f"{self._base()} pause{self._svc(services)}", warn=True)

    def unpause(self, services: list[str] | None = None):
        """``docker compose unpause`` (whole project, or the given services)."""
        return run(f"{self._base()} unpause{self._svc(services)}", warn=True)

    def exec_command(
        self, box: str, cmd: list[str] | None = None,
        shell: str = DEFAULT_EXEC_SHELL,
    ) -> str:
        """Build the ``docker compose exec`` command string for *box*.

        With *cmd* → a one-shot ``exec -T <box> <cmd…>`` (``-T`` disables TTY
        allocation so it works from a non-terminal / script). Without *cmd* →
        an interactive ``exec <box> <shell>`` (TTY auto-allocated by compose).
        Returned as a string for the caller to run with inherited stdio (like
        the ssh path), so the interactive shell attaches to the real terminal.
        """
        base = self._base()
        if cmd:
            inner = " ".join(shlex.quote(c) for c in cmd)
            return f"{base} exec -T {shlex.quote(box)} {inner}"
        return f"{base} exec {shlex.quote(box)} {shlex.quote(shell)}"

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _svc(services: list[str] | None) -> str:
        return "".join(f" {shlex.quote(s)}" for s in (services or []))

    def _base(self) -> str:
        # ``-f`` / ``--project-directory`` are included only when set. A
        # teardown runner with neither operates on the project purely by its
        # compose labels (``docker compose -p <project> down`` — compose v2
        # resolves containers/networks from ``com.docker.compose.project``),
        # so containers can be removed even after the workdir/file is gone.
        parts = [f"{self._sudo}docker compose -p {shlex.quote(self.project)}"]
        if self.compose_file:
            parts.append(f"-f {shlex.quote(self.compose_file)}")
        if self.workdir:
            parts.append(f"--project-directory {shlex.quote(self.workdir)}")
        return " ".join(parts)
