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

import os
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

#: Compose falls back to ``COMPOSE_PROJECT_NAME`` *silently* when it cannot
#: load a file, rather than failing, so a broken file plus that variable
#: redirects the command at whatever project it names -- measured on 2.40.3,
#: where ``down --dry-run`` with an explicit ``-p ourproj`` reported
#: "Container victimproj-v-1  Stopping".
#:
#: Unsetting it is not enough: compose then reads ``COMPOSE_*`` from the
#: project directory's ``.env`` (and from ``COMPOSE_ENV_FILES``), which
#: reintroduces it. The shell environment takes precedence over both, so the
#: variable is set -- to the **empty string**, not to the project name.
#:
#: Empty is what matters. Present, so ``.env`` cannot supply one; empty, so
#: the fallback has nothing to fall back *to* and compose fails on a file it
#: cannot load instead of silently proceeding without it. Pinning it to the
#: real project name stopped the redirection but re-enabled that silent
#: path, and ``down --volumes`` then deleted a volume declared
#: ``external: true``. ``-p`` still carries the real name (#164 NET-C1).


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

    def available(self) -> bool:
        """Whether ``docker`` and the Compose v2 plugin can be invoked."""
        if shutil.which("docker") is None:
            return False
        return run(f"{self._sudo}docker compose version",
                   hide=True, warn=True).ok

    def validate(self, compose_file: str):
        """``docker compose -f <file> config --quiet`` — resolve and check.

        Compose is the authority on whether a file resolves: it performs
        ``include:``, ``extends:`` and ``${VAR}`` interpolation first and then
        refuses any service reference to an undeclared network. Running it on
        a *candidate* is what lets boxman defer those cases safely instead of
        guessing at them (#164 NET-C1).
        """
        cmd = (f"{self._sudo}env COMPOSE_PROJECT_NAME= docker compose "
               f"-p {self.project} "
               f"-f {shlex.quote(compose_file)} config --quiet")
        return run(cmd, hide=True, warn=True)

    def up(self, timeout: int = DEFAULT_READINESS_TIMEOUT,
           force_recreate: bool = False):
        """``docker compose up -d --wait`` — create+start and block on
        readiness. ``force_recreate`` recreates containers even if their config
        is unchanged (used by snapshot restore, which swaps in snapshot image
        tags). Raises :class:`ProvisionError` on failure/timeout."""
        if not self.compose_file:
            raise ProvisionError(
                f"cannot bring up project '{self.project}' without a compose "
                f"file (label-only runners are for teardown)."
            )
        recreate = " --force-recreate" if force_recreate else ""
        cmd = f"{self._base()} up -d --wait{recreate} --wait-timeout {int(timeout)}"
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
    ) -> list[str]:
        """Build the ``docker compose exec`` **argv list** for *box*.

        With *cmd* → a one-shot ``exec -T <box> <cmd…>`` (``-T`` disables TTY
        allocation so it works from a non-terminal / script). Without *cmd* →
        an interactive ``exec <box> <shell>`` (TTY auto-allocated by compose).
        Returned as an argv list so the caller runs it with ``shell=False``
        (no shell injection surface) and inherited stdio, so an interactive
        shell attaches to the real terminal.
        """
        argv = self._base_argv() + ["exec"]
        if cmd:
            return argv + ["-T", box, *cmd]
        return argv + [box, shell]

    # -- docker image ops (snapshots, D3) — plain `docker`, not compose -----

    def commit(self, container: str, tag: str):
        """``docker commit <container> <tag>`` — snapshot a container's
        filesystem to an image (captures the writable layer only, not volumes)."""
        return run(
            f"{self._sudo}docker commit {shlex.quote(container)} {shlex.quote(tag)}",
            warn=True, hide=True)

    def image_rm(self, tag: str):
        """``docker image rm -f <tag>``."""
        return run(f"{self._sudo}docker image rm -f {shlex.quote(tag)}",
                   warn=True, hide=True)

    def image_exists(self, tag: str) -> bool:
        """True iff *tag* resolves to a local image."""
        return run(f"{self._sudo}docker image inspect {shlex.quote(tag)}",
                   warn=True, hide=True).ok

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _svc(services: list[str] | None) -> str:
        return "".join(f" {shlex.quote(s)}" for s in (services or []))

    def _base_argv(self) -> list[str]:
        """The ``docker compose -p <project> …`` prefix as an argv list (for
        commands run with ``shell=False``)."""
        argv = (["sudo"] if self._sudo else []) + [
            "env", "COMPOSE_PROJECT_NAME=", "docker", "compose"]
        if not self.compose_file:
            # label-only: no file is wanted, and a `.env` COMPOSE_FILE would
            # supply one anyway. `-f` beats `.env`, but here there is no `-f`.
            argv += ["--env-file", os.devnull]
        argv += ["-p", self.project]
        if self.compose_file:
            argv += ["-f", self.compose_file]
        if self.workdir:
            argv += ["--project-directory", self.workdir]
        return argv

    def _base(self) -> str:
        # ``-f`` / ``--project-directory`` are included only when set. A
        # teardown runner with neither operates on the project purely by its
        # compose labels (``docker compose -p <project> down`` — compose v2
        # resolves containers/networks from ``com.docker.compose.project``),
        # so containers can be removed even after the workdir/file is gone.
        parts = [f"{self._sudo}env COMPOSE_PROJECT_NAME= docker compose"]
        if not self.compose_file:
            # label-only: a `.env` COMPOSE_FILE would supply a file we did
            # not ask for, and there is no `-f` here to beat it.
            parts.append(f"--env-file {os.devnull}")
        parts.append(f"-p {shlex.quote(self.project)}")
        if self.compose_file:
            parts.append(f"-f {shlex.quote(self.compose_file)}")
        if self.workdir:
            parts.append(f"--project-directory {shlex.quote(self.workdir)}")
        return " ".join(parts)
