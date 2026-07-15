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
        runner, _workdir, _compose_file = self._compose_context(cluster_name, cluster_cfg)
        runner.preflight()
        timeout = int(cluster_cfg.get("readiness_timeout", DEFAULT_READINESS_TIMEOUT))
        self.logger.info(
            f"[{cluster_name}] docker compose up (project '{runner.project}', "
            f"wait ≤{timeout}s)"
        )
        runner.up(timeout)
        return True

    def stop_cluster(self, cluster_name: str, cluster_cfg: dict[str, Any]) -> bool:
        """boxman down → ``docker compose stop`` (keep containers)."""
        runner, _wd, _cf = self._compose_context(cluster_name, cluster_cfg)
        self.logger.info(f"[{cluster_name}] docker compose stop")
        runner.stop()
        return True

    def start_cluster(self, cluster_name: str, cluster_cfg: dict[str, Any]) -> bool:
        """boxman up (after down) → ``docker compose start``."""
        runner, _wd, _cf = self._compose_context(cluster_name, cluster_cfg)
        self.logger.info(f"[{cluster_name}] docker compose start")
        runner.start()
        return True

    def down_cluster(self, cluster_name: str, cluster_cfg: dict[str, Any]) -> bool:
        """boxman deprovision → ``docker compose down`` (remove containers +
        networks, keep named volumes)."""
        runner, _wd, _cf = self._compose_context(cluster_name, cluster_cfg)
        self.logger.info(f"[{cluster_name}] docker compose down")
        runner.down()
        return True

    def destroy_cluster(self, cluster_name: str, cluster_cfg: dict[str, Any]) -> bool:
        """boxman destroy → ``docker compose down --volumes`` and remove the
        generated compose file."""
        runner, _wd, compose_file = self._compose_context(cluster_name, cluster_cfg)
        self.logger.info(f"[{cluster_name}] docker compose down --volumes")
        runner.down_volumes()
        try:
            if os.path.isfile(compose_file):
                os.remove(compose_file)
        except OSError as exc:
            self.logger.warning(
                f"[{cluster_name}] could not remove {compose_file}: {exc}"
            )
        return True

    # -- internals ---------------------------------------------------------

    def _compose_context(
        self, cluster_name: str, cluster_cfg: dict[str, Any]
    ) -> tuple[ComposeRunner, str, str]:
        """Regenerate the compose file (idempotent) and build a runner.

        Regenerating on every op keeps ``-f`` valid for down/stop/start
        even if the workdir was wiped, and keeps the on-disk file in sync
        with the config.
        """
        workdir = os.path.expanduser(cluster_cfg["workdir"])
        shared = set(self.config.get("shared_networks") or {})
        compose = self._generator.generate(
            cluster_name, cluster_cfg, self._conf_dir(), shared
        )
        compose_file = self._generator.write(compose, workdir)
        runner = ComposeRunner(
            project=self._compose_project(cluster_name),
            compose_file=compose_file,
            workdir=workdir,
            logger=self.logger,
        )
        return runner, workdir, compose_file

    def _conf_dir(self) -> str:
        """Directory of the project ``conf.yml`` (for build-context resolution)."""
        config_path = getattr(self.manager, "config_path", None)
        return os.path.dirname(os.path.abspath(config_path)) if config_path else os.getcwd()

    def _compose_project(self, cluster_name: str) -> str:
        """Derive the ``docker compose -p`` name — one project per cluster
        (ADR-001), so clusters never share compose state."""
        base = self._provider_config.get("project_name") or self.config.get("project") or "boxman"
        return _sanitize_project_name(f"{base}_{cluster_name}")

    def _require_local_runtime(self, cluster_name: str) -> None:
        """Defense-in-depth: the docker-compose provider requires
        ``runtime: local`` (the ``docker-compose`` *runtime* is
        libvirt-in-a-container, a different axis). app.py fails fast at
        session build; this re-checks in case the session is driven directly."""
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
