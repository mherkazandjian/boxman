"""
DockerComposeSession — the docker-compose provider session.

One compose project per cluster (ADR-001). Unlike the libvirt session
(whose per-VM methods the manager calls inside per-box subprocess loops),
this session exposes **coarse per-cluster** lifecycle methods that the
manager dispatches a whole cluster to — because docker-compose is
declarative and cluster-scoped (one ``docker compose up --wait`` per
cluster, D1/D5). The libvirt-shaped per-VM/network protocol methods exist
only to satisfy the ``ProviderSession`` protocol; they are never reached
for a docker-compose cluster and raise a clear error if called.
"""

from __future__ import annotations

import os
import re
from typing import Any

from boxman import log
from boxman.exceptions import ConfigError, ProvisionError
from boxman.providers.docker_compose.compose_generator import ComposeGenerator
from boxman.providers.docker_compose.compose_runner import (
    DEFAULT_READINESS_TIMEOUT,
    ComposeRunner,
)


class DockerComposeSession:
    """Per-cluster docker-compose provider session."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.logger = log
        #: set externally by scripts/app.py right after construction
        self.manager = None
        self._provider_config = (
            (self.config.get("provider") or {}).get("docker-compose") or {}
        )
        self._generator = ComposeGenerator(logger=self.logger)

    # -- ProviderSession protocol: config surface --------------------------

    @property
    def provider_config(self) -> dict[str, Any]:
        return self._provider_config

    @provider_config.setter
    def provider_config(self, value: dict[str, Any]) -> None:
        self._provider_config = value or {}

    @property
    def uri(self) -> str:
        # docker-compose has no libvirt-style URI; the docker host is implicit.
        return self._provider_config.get("uri", "")

    @property
    def use_sudo(self) -> bool:
        return bool(self._provider_config.get("use_sudo", False))

    def update_provider_config(self, new_config: dict[str, Any]) -> None:
        self._provider_config = {**self._provider_config, **(new_config or {})}

    def update_provider_config_with_runtime(self) -> None:
        """No-op: the docker-compose provider requires ``runtime: local``,
        so there is no runtime enrichment to apply. Present because the
        manager calls it over every registered session."""
        return None

    # -- coarse per-cluster lifecycle (the real work) ----------------------

    def up_cluster(self, cluster_name: str, cluster_cfg: dict[str, Any]) -> bool:
        """boxman provision/up → generate the compose file and
        ``docker compose up -d --wait`` (idempotent)."""
        self._require_local_runtime(cluster_name)
        timeout = self._readiness_timeout(cluster_cfg, cluster_name)
        runner, _workdir, _compose_file = self._compose_context(cluster_name, cluster_cfg)
        self._ensure_bind_dirs(cluster_name, cluster_cfg)
        runner.preflight()
        self.logger.info(
            f"[{cluster_name}] docker compose up (project '{runner.project}', "
            f"wait ≤{timeout}s)"
        )
        runner.up(timeout)
        return True

    def stop_cluster(self, cluster_name: str, cluster_cfg: dict[str, Any]) -> bool:
        """boxman down → ``docker compose stop`` (keep containers)."""
        runner, _wd, _cf = self._teardown_runner(cluster_name, cluster_cfg)
        self.logger.info(f"[{cluster_name}] docker compose stop")
        return self._check(cluster_name, "stop", runner.stop())

    def start_cluster(self, cluster_name: str, cluster_cfg: dict[str, Any]) -> bool:
        """``docker compose start`` — reserved API surface for the later
        control-verb phase; not driven by a flow yet (``up``-after-``down``
        reconciles via :meth:`up_cluster`). See
        ``BoxmanManager.start_compose_clusters``."""
        runner, _wd, _cf = self._teardown_runner(cluster_name, cluster_cfg)
        self.logger.info(f"[{cluster_name}] docker compose start")
        return self._check(cluster_name, "start", runner.start())

    def down_cluster(self, cluster_name: str, cluster_cfg: dict[str, Any]) -> bool:
        """boxman deprovision → ``docker compose down`` (remove containers +
        networks, keep named volumes)."""
        runner, _wd, _cf = self._teardown_runner(cluster_name, cluster_cfg)
        self.logger.info(f"[{cluster_name}] docker compose down")
        return self._check(cluster_name, "down", runner.down())

    def destroy_cluster(self, cluster_name: str, cluster_cfg: dict[str, Any]) -> bool:
        """boxman destroy → ``docker compose down --volumes`` and remove the
        generated compose file (only when the teardown actually succeeded)."""
        runner, _wd, compose_file = self._teardown_runner(cluster_name, cluster_cfg)
        self.logger.info(f"[{cluster_name}] docker compose down --volumes")
        ok = self._check(cluster_name, "down --volumes", runner.down_volumes())
        if not ok:
            # keep the on-disk file so a retry can still resolve the project
            self.logger.warning(
                f"[{cluster_name}] keeping {compose_file} for retry "
                f"(teardown did not report success)."
            )
            return False
        try:
            if os.path.isfile(compose_file):
                os.remove(compose_file)
        except OSError as exc:
            self.logger.warning(
                f"[{cluster_name}] could not remove {compose_file}: {exc}"
            )
        return True

    def _check(self, cluster_name: str, op: str, result: Any) -> bool:
        """Warn (don't raise) when a best-effort teardown op did not succeed.

        The teardown runner methods shell out with ``warn=True`` (no raise);
        their ``Result.ok`` is inspected here so a failed ``stop``/``down`` is
        surfaced instead of being silently reported as success. A ``result``
        without an ``ok`` attribute (e.g. a test double returning ``None``) is
        treated as success.
        """
        if not getattr(result, "ok", True):
            detail = (
                getattr(result, "stderr", "") or getattr(result, "stdout", "") or ""
            ).strip()
            self.logger.warning(
                f"[{cluster_name}] 'docker compose {op}' reported failure: {detail}"
            )
            return False
        return True

    # -- internals ---------------------------------------------------------

    def _compose_context(
        self, cluster_name: str, cluster_cfg: dict[str, Any]
    ) -> tuple[ComposeRunner, str, str]:
        """Regenerate the compose file (idempotent) and build a runner.

        Regeneration is for **bring-up only** (``up_cluster``); teardown uses
        :meth:`_teardown_runner`, which never regenerates.
        """
        workdir = self._workdir(cluster_cfg, cluster_name)
        shared_networks = self.config.get("shared_networks") or {}
        project = self._compose_project(cluster_name)
        compose = self._generator.generate(
            cluster_name, cluster_cfg, self._conf_dir(), shared_networks,
            project_name=project,
        )
        compose_file = self._generator.write(compose, workdir)
        runner = ComposeRunner(
            project=project,
            compose_file=compose_file,
            workdir=workdir,
            logger=self.logger,
            use_sudo=self.use_sudo,
        )
        return runner, workdir, compose_file

    def _teardown_runner(
        self, cluster_name: str, cluster_cfg: dict[str, Any]
    ) -> tuple[ComposeRunner, str, str]:
        """Build a runner for a teardown op **without regenerating** the file.

        Uses the on-disk ``<workdir>/docker-compose.yml`` when present (so a
        hand-edited file is respected and never overwritten); otherwise falls
        back to a **label-only** runner (``docker compose -p <project> …``) so
        containers can still be removed after the workdir/file was wiped.

        This deliberately avoids :meth:`_compose_context`'s ``generate()``:
        regenerating on teardown would make ``down``/``deprovision``/``destroy``
        fail on a config that no longer generates cleanly (e.g. a box that lost
        its ``image:``), and would recreate the workdir + compose file on a
        ``down`` of a never-provisioned project.
        """
        workdir = self._workdir(cluster_cfg, cluster_name)
        compose_file = os.path.join(workdir, "docker-compose.yml")
        project = self._compose_project(cluster_name)
        if os.path.isfile(compose_file):
            runner = ComposeRunner(
                project=project,
                compose_file=compose_file,
                workdir=workdir,
                logger=self.logger,
                use_sudo=self.use_sudo,
            )
        else:
            # label-only: resolve the project from compose labels
            runner = ComposeRunner(
                project=project, logger=self.logger, use_sudo=self.use_sudo)
        return runner, workdir, compose_file

    def _readiness_timeout(self, cluster_cfg: dict[str, Any], cluster_name: str) -> int:
        """Validate the cluster ``readiness_timeout:`` → positive int seconds.

        Raises a clear :class:`ConfigError` instead of a bare ``ValueError`` /
        ``TypeError`` traceback for non-integer or non-positive input.
        """
        raw = cluster_cfg.get("readiness_timeout", DEFAULT_READINESS_TIMEOUT)
        try:
            timeout = int(raw)
        except (TypeError, ValueError):
            raise ConfigError(
                f"docker-compose cluster '{cluster_name}': readiness_timeout "
                f"must be an integer number of seconds (got {raw!r})."
            )
        if timeout <= 0:
            raise ConfigError(
                f"docker-compose cluster '{cluster_name}': readiness_timeout "
                f"must be a positive integer (got {timeout})."
            )
        return timeout

    def _workdir(self, cluster_cfg: dict[str, Any], cluster_name: str) -> str:
        """Resolve the cluster ``workdir:`` (required) to an absolute-ish path.

        Raises a clear :class:`ConfigError` instead of a bare ``KeyError`` when
        ``workdir:`` is missing — it is the first thing every lifecycle op needs
        (the generated ``docker-compose.yml`` lives at ``<workdir>/``).
        """
        workdir = cluster_cfg.get("workdir")
        if not workdir:
            raise ConfigError(
                f"docker-compose cluster '{cluster_name}' has no 'workdir:' — "
                f"it is required (the generated docker-compose.yml is written "
                f"to <workdir>/docker-compose.yml)."
            )
        return os.path.expanduser(workdir)

    def _conf_dir(self) -> str:
        """Directory of the project ``conf.yml`` (for build-context resolution)."""
        config_path = getattr(self.manager, "config_path", None)
        return os.path.dirname(os.path.abspath(config_path)) if config_path else os.getcwd()

    def _ensure_bind_dirs(
        self, cluster_name: str, cluster_cfg: dict[str, Any]
    ) -> None:
        """``mkdir -p`` each bind-mount host directory before ``compose up``.

        A bind mount whose host path doesn't exist yet would otherwise be
        created by the docker daemon as ``root:root``; pre-creating it here (as
        the boxman user, the ``_ensure_writable_dir`` intent) gives saner
        ownership. Named volumes need no pre-creation (docker-managed), and
        bind dirs are deliberately **never removed** on teardown — they are
        user paths (``./configs``, ``.``). A host path that already exists (or
        is a file) is left untouched.
        """
        conf_dir = self._conf_dir()
        for box_name, box in (cluster_cfg.get("boxes") or {}).items():
            for entry in (box or {}).get("volumes") or []:
                if not isinstance(entry, dict):
                    continue  # malformed — the generator raises on it
                host_path = entry.get("host_path")
                if not host_path:
                    continue  # named volume — docker-managed
                abs_host = os.path.abspath(
                    os.path.join(conf_dir, os.path.expanduser(str(host_path)))
                )
                if os.path.exists(abs_host):
                    continue
                try:
                    os.makedirs(abs_host, exist_ok=True)
                    self.logger.info(
                        f"[{cluster_name}] created bind-mount dir {abs_host} "
                        f"(box '{box_name}')"
                    )
                except OSError as exc:
                    self.logger.warning(
                        f"[{cluster_name}] could not create bind-mount dir "
                        f"{abs_host}: {exc} — docker will create it as root."
                    )

    def compose_project_name(self, cluster_name: str) -> str:
        """Public accessor for the ``docker compose -p`` name of *cluster_name*.

        The manager uses this to detect cross-cluster project-name collisions
        (distinct cluster names that sanitize to the same project) before
        creating any compose state — see
        ``BoxmanManager._reject_compose_project_collisions``.
        """
        return self._compose_project(cluster_name)

    def _compose_project(self, cluster_name: str) -> str:
        """Derive the ``docker compose -p`` name — one project per cluster
        (ADR-001), so clusters never share compose state."""
        base = self._provider_config.get("project_name") or self.config.get("project") or "boxman"
        return _sanitize_project_name(f"{base}_{cluster_name}")

    def _require_local_runtime(self, cluster_name: str) -> None:
        """Defense-in-depth: the docker-compose provider requires
        ``runtime: local`` (the ``docker-compose`` *runtime* is
        libvirt-in-a-container, a different axis). app.py fails fast at
        session build; this re-checks in case the session is driven directly.

        With no manager attached (a bare direct-drive, e.g. in a unit test)
        there is no runtime axis to enforce, so the ``getattr`` default of
        ``"local"`` intentionally passes — the authoritative fail-fast is
        app.py's session-build guardrail, which always has the manager.
        """
        runtime = getattr(self.manager, "runtime", "local")
        if runtime != "local":
            raise ConfigError(
                f"cluster '{cluster_name}' uses the docker-compose provider, "
                f"which requires runtime 'local' (got '{runtime}'). The "
                f"'docker-compose' runtime is libvirt-in-a-container and is a "
                f"different setting — see doc/docker-compose-provider/config-schema.md."
            )

    # -- ProviderSession protocol: libvirt-shaped methods (never reached) --

    def _cluster_scoped(self, method: str):
        raise ProvisionError(
            f"DockerComposeSession.{method}() is not supported — the "
            f"docker-compose provider is cluster-scoped; the manager drives "
            f"it through the per-cluster lifecycle (up_cluster/down_cluster/"
            f"destroy_cluster), not per-box VM methods."
        )

    def start_vm(self, vm_name: str) -> bool:
        self._cluster_scoped("start_vm")

    def destroy_vm(self, name: str, force: bool = False, remove_storage: bool = True, **kwargs) -> bool:
        self._cluster_scoped("destroy_vm")

    def clone_vm(self, new_vm_name: str, src_vm_name: str, info: dict[str, Any], workdir: str) -> bool:
        self._cluster_scoped("clone_vm")

    def define_network(self, name=None, info=None, workdir=None) -> bool:
        self._cluster_scoped("define_network")

    def destroy_network(self, name=None, info=None) -> bool:
        self._cluster_scoped("destroy_network")

    def remove_network(self, name=None, info=None) -> bool:
        self._cluster_scoped("remove_network")

    def snapshot_take(self, *args, **kwargs) -> bool:
        self._cluster_scoped("snapshot_take")

    def snapshot_restore(self, vm_name: str, snapshot_name: str | None = None) -> bool:
        self._cluster_scoped("snapshot_restore")

    def snapshot_delete(self, vm_name: str, snapshot_name: str) -> bool:
        self._cluster_scoped("snapshot_delete")

    def snapshot_list(self, vm_name: str | None = None) -> list[dict[str, str]]:
        self._cluster_scoped("snapshot_list")


def _sanitize_project_name(name: str) -> str:
    """Coerce *name* to a valid compose project name (``[a-z0-9][a-z0-9_-]*``)."""
    slug = re.sub(r"[^a-z0-9_-]", "_", name.lower())
    slug = slug.lstrip("_-") or "boxman"
    return slug
