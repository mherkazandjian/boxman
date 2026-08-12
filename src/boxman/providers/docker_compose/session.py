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

import copy
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from boxman.exceptions import ConfigError, ProvisionError
from boxman.providers.docker_compose.compose_generator import (
    ComposeGenerator,
    resolve_local_path,
)
from boxman.providers.docker_compose.compose_runner import (
    DEFAULT_EXEC_SHELL,
    DEFAULT_READINESS_TIMEOUT,
    ComposeRunner,
)
from boxman.providers.session_base import SessionConfigMixin
from boxman.utils.compose_names import sanitize_project_name


class DockerComposeSession(SessionConfigMixin):
    """Per-cluster docker-compose provider session."""

    provider_key = "docker-compose"
    #: docker-compose has no libvirt-style URI; the docker host is implicit.
    default_uri = ""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._generator = ComposeGenerator(logger=self.logger)

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

    # -- control / access (Phase 6) ----------------------------------------

    def container_status(
        self, cluster_name: str, cluster_cfg: dict[str, Any]
    ) -> list[dict[str, str]]:
        """Per-service status rows for a dc cluster via ``docker compose ps``.

        Each row: ``{service, name, state, health, ports}``. Empty list when the
        project has no containers (never provisioned / fully removed) — or when
        the cluster is misconfigured (e.g. no ``workdir``). Never raises, so the
        display verbs (``ps``/``connect_info``) can't be broken by a status
        probe (uniform with a VM-state query on an unprovisioned VM).
        """
        rows: list[dict[str, str]] = []
        try:
            runner, _wd, _cf = self._teardown_runner(cluster_name, cluster_cfg)
        except ConfigError as exc:
            self.logger.warning(
                f"[{cluster_name}] cannot query container status: {exc}")
            return rows
        result = runner.ps_json()
        if not getattr(result, "ok", False):
            return rows
        for line in (getattr(result, "stdout", "") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            # compose v2 emits NDJSON objects; some builds emit a JSON array
            for obj in (parsed if isinstance(parsed, list) else [parsed]):
                rows.append({
                    "service": obj.get("Service", ""),
                    "name": obj.get("Name", ""),
                    "state": obj.get("State", ""),
                    "health": obj.get("Health", ""),
                    "ports": _format_ports(obj.get("Publishers")),
                })
        return rows

    def exec_command_for(
        self, cluster_name: str, cluster_cfg: dict[str, Any], box: str,
        cmd: list[str] | None = None, shell: str = DEFAULT_EXEC_SHELL,
    ) -> str:
        """Validate *box* and return the ``docker compose exec`` command string.

        The caller runs it with inherited stdio (interactive shell) — so this
        only builds and validates, it does not execute. ``ConfigError`` if
        *box* is not a service of this cluster.
        """
        boxes = cluster_cfg.get("boxes") or {}
        if box not in boxes:
            raise ConfigError(
                f"box '{box}' is not a service in docker-compose cluster "
                f"'{cluster_name}' (services: {', '.join(boxes) or 'none'})."
            )
        runner, _wd, _cf = self._teardown_runner(cluster_name, cluster_cfg)
        return runner.exec_command(box, cmd=cmd, shell=shell)

    def pause_cluster(
        self, cluster_name: str, cluster_cfg: dict[str, Any]
    ) -> bool:
        """boxman control suspend → ``docker compose pause`` (whole cluster)."""
        runner, _wd, _cf = self._teardown_runner(cluster_name, cluster_cfg)
        self.logger.info(f"[{cluster_name}] docker compose pause")
        return self._check(cluster_name, "pause", runner.pause())

    def unpause_cluster(
        self, cluster_name: str, cluster_cfg: dict[str, Any]
    ) -> bool:
        """boxman control resume → ``docker compose unpause`` (whole cluster)."""
        runner, _wd, _cf = self._teardown_runner(cluster_name, cluster_cfg)
        self.logger.info(f"[{cluster_name}] docker compose unpause")
        return self._check(cluster_name, "unpause", runner.unpause())

    # -- snapshots (Phase 7, decision D3) — docker commit-backed -----------

    def _container_names(
        self, cluster_name: str, cluster_cfg: dict[str, Any]
    ) -> dict[str, str]:
        """Map each running/existing box → its container name via
        ``docker compose ps``. Boxes without a container are absent.

        Raises :class:`ProvisionError` when the ``ps`` itself fails (a broken
        docker/compose, a bad project) — swallowing it here would surface
        later as a misleading "no containers to snapshot", hiding the real
        cause. An *empty* mapping from a successful ``ps`` is a legitimate
        answer (nothing is up) and is returned as such.
        """
        runner, _wd, _cf = self._teardown_runner(cluster_name, cluster_cfg)
        result = runner.ps_json()
        names: dict[str, str] = {}
        if not getattr(result, "ok", False):
            raise ProvisionError(
                f"[{cluster_name}] 'docker compose ps' failed — cannot "
                f"determine container names: "
                f"{_result_error(result) or 'no error output'}"
            )
        for line in (getattr(result, "stdout", "") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            for obj in (parsed if isinstance(parsed, list) else [parsed]):
                if obj.get("Service") and obj.get("Name"):
                    names[obj["Service"]] = obj["Name"]
        return names

    def snapshot_take_cluster(
        self, cluster_name: str, cluster_cfg: dict[str, Any],
        snapshot_name: str, description: str = "",
    ) -> bool:
        """``docker commit`` every container in the cluster to
        ``boxman/<compose-project>_<box>:<snap>`` (the compose project is
        ``<project>_<cluster>``, so tags can't collide across clusters) and
        record it in the workdir metadata. **Named volumes are NOT captured**
        — a commit only snapshots the container's writable layer (D3).

        The snapshot name must be unused: a repeat ``take`` would re-point the
        existing tags one box at a time, so a failure midway would leave the
        recorded snapshot a mix of old and new box states. Delete first
        instead (libvirt rejects a duplicate domain-snapshot name too). If a
        commit fails partway, the images already written for *this* take are
        removed again, so a failed take leaves nothing behind.
        """
        self._require_local_runtime(cluster_name)
        project = self._compose_project(cluster_name)
        self._reject_existing_snapshot(
            cluster_name, cluster_cfg, project, snapshot_name)
        names = self._container_names(cluster_name, cluster_cfg)
        runner, _wd, _cf = self._teardown_runner(cluster_name, cluster_cfg)
        self.logger.warning(
            f"[{cluster_name}] snapshot '{snapshot_name}' commits container "
            f"filesystems only — **named volumes are NOT captured** (docker "
            f"commit cannot include them). Back volumes up separately."
        )
        tags: dict[str, str] = {}
        for box in (cluster_cfg.get("boxes") or {}):
            container = names.get(box)
            if not container:
                self.logger.warning(
                    f"[{cluster_name}] box '{box}' has no container — skipping "
                    f"in snapshot '{snapshot_name}'."
                )
                continue
            tag = _snapshot_tag(project, box, snapshot_name)
            result = runner.commit(container, tag)
            if not getattr(result, "ok", False):
                self._rollback_commits(cluster_name, runner, tags, snapshot_name)
                raise ProvisionError(
                    f"[{cluster_name}] docker commit failed for box '{box}': "
                    f"{_result_error(result)}"
                )
            self.logger.info(f"[{cluster_name}] snapshot '{snapshot_name}': {box} -> {tag}")
            tags[box] = tag
        if not tags:
            raise ProvisionError(
                f"[{cluster_name}] no containers to snapshot for "
                f"'{snapshot_name}' — is the cluster up?"
            )
        meta = self._read_snapshots(cluster_name, cluster_cfg)
        meta[snapshot_name] = {
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "description": description or "",
            "boxes": tags,
        }
        self._save_snapshots(cluster_name, cluster_cfg, meta)
        return True

    def snapshot_list_cluster(
        self, cluster_name: str, cluster_cfg: dict[str, Any]
    ) -> dict[str, Any]:
        """Recorded snapshots for the cluster (name → metadata)."""
        return self._read_snapshots(cluster_name, cluster_cfg)

    def snapshot_delete_cluster(
        self, cluster_name: str, cluster_cfg: dict[str, Any], snapshot_name: str
    ) -> bool:
        """Remove a snapshot's images (``docker image rm``) and metadata.

        The metadata entry is dropped **only when every image was removed**.
        ``docker image rm -f`` legitimately fails while a container is still
        running from the image — e.g. ``snapshot delete v1`` right after
        ``snapshot restore v1``, where the restored containers hold it — and
        dropping the entry anyway would leak the image with nothing left to
        reclaim it by. Failures are reported and the entry is kept so the
        delete can be retried (after a ``down``/``up``).
        """
        meta = self._read_snapshots(cluster_name, cluster_cfg)
        snap = meta.get(snapshot_name)
        if not snap:
            self.logger.warning(
                f"[{cluster_name}] no snapshot '{snapshot_name}' to delete.")
            return False
        runner, _wd, _cf = self._teardown_runner(cluster_name, cluster_cfg)
        failed: list[str] = []
        for _box, tag in (snap.get("boxes") or {}).items():
            if self._check(cluster_name, f"image rm {tag}",
                           runner.image_rm(tag), tool="docker"):
                self.logger.info(f"[{cluster_name}] removed snapshot image {tag}")
            else:
                failed.append(tag)
        if failed:
            self.logger.error(
                f"[{cluster_name}] snapshot '{snapshot_name}' NOT deleted — "
                f"could not remove {len(failed)} image(s): {', '.join(failed)}. "
                f"An image still in use by a running container cannot be "
                f"removed; 'boxman down' the cluster and retry. The snapshot "
                f"metadata was kept so the delete can be retried."
            )
            return False
        meta.pop(snapshot_name, None)
        self._save_snapshots(cluster_name, cluster_cfg, meta)
        return True

    def snapshot_resolve_cluster(
        self, cluster_name: str, cluster_cfg: dict[str, Any],
        snapshot_name: str | None,
    ) -> str | None:
        """Resolve *snapshot_name* for a cluster, falling back to the newest
        recorded snapshot when it is empty/None. ``None`` when the cluster has
        no snapshots at all (the caller warns and skips)."""
        if snapshot_name:
            return snapshot_name
        snaps = self._read_snapshots(cluster_name, cluster_cfg)
        if not snaps:
            return None
        return max(snaps, key=lambda k: snaps[k].get("created", ""))

    def validate_snapshot_cluster(
        self, cluster_name: str, cluster_cfg: dict[str, Any], snapshot_name: str
    ) -> tuple[bool, list[str]]:
        """``(valid, errors)`` for a recorded cluster snapshot — mirrors the
        libvirt session's ``validate_snapshot`` so the manager can pre-validate
        both provider types before mutating either.

        Checks the snapshot is recorded and that **every** image it references
        still exists locally: metadata outlives a pruned image, and without
        this compose would try to *pull* ``boxman/…`` from a registry (or fail
        only after the destructive ``--force-recreate`` had begun).
        """
        meta = self._read_snapshots(cluster_name, cluster_cfg)
        snap = meta.get(snapshot_name)
        if not snap:
            return False, [
                f"no snapshot '{snapshot_name}' recorded for cluster "
                f"'{cluster_name}' (have: {', '.join(meta) or 'none'})"
            ]
        runner, _wd, _cf = self._teardown_runner(cluster_name, cluster_cfg)
        errors = [
            f"snapshot image missing for box '{box}': {tag} "
            f"(removed outside boxman?)"
            for box, tag in (snap.get("boxes") or {}).items()
            if not runner.image_exists(tag)
        ]
        return (not errors), errors

    def snapshot_restore_cluster(
        self, cluster_name: str, cluster_cfg: dict[str, Any], snapshot_name: str
    ) -> bool:
        """Regenerate the compose file with the snapshot's per-box image tags
        and ``up --force-recreate``.

        Validated before anything is touched (:meth:`validate_snapshot_cluster`)
        so a missing/pruned image aborts cleanly rather than part-way through
        the destructive recreate.

        Note: a subsequent ``boxman up`` regenerates from ``conf.yml`` and
        reverts to the declared images — restore is a point-in-time recreate,
        not a permanent pin. **Volume data is unchanged** (not part of the
        snapshot)."""
        self._require_local_runtime(cluster_name)
        valid, errors = self.validate_snapshot_cluster(
            cluster_name, cluster_cfg, snapshot_name)
        if not valid:
            raise ConfigError(
                f"[{cluster_name}] cannot restore snapshot '{snapshot_name}': "
                + "; ".join(errors)
            )
        snap = self._read_snapshots(cluster_name, cluster_cfg)[snapshot_name]
        restore_cfg = copy.deepcopy(cluster_cfg)
        boxes = restore_cfg.get("boxes") or {}
        for box, tag in (snap.get("boxes") or {}).items():
            if box in boxes:
                boxes[box] = {**(boxes[box] or {}), "image": tag}
                boxes[box].pop("build", None)  # the snapshot image replaces build
        # restore is a bring-up: same context/bind-dir/preflight path as
        # up_cluster, so a bind host dir removed between take and restore is
        # re-created as *us* rather than root-owned by the docker daemon.
        runner, _workdir, _compose_file = self._compose_context(
            cluster_name, restore_cfg)
        self._ensure_bind_dirs(cluster_name, restore_cfg)
        runner.preflight()
        timeout = self._readiness_timeout(cluster_cfg, cluster_name)
        self.logger.info(
            f"[{cluster_name}] restoring snapshot '{snapshot_name}' "
            f"(up --force-recreate)")
        runner.up(timeout, force_recreate=True)
        return True

    def _reject_existing_snapshot(
        self, cluster_name: str, cluster_cfg: dict[str, Any],
        project: str, snapshot_name: str,
    ) -> None:
        """Refuse a ``take`` whose name (or resulting image tag) is taken.

        The tag is checked too, not just the name: distinct names can sanitize
        to the same tag (``v:1`` and ``v-1`` both → ``v-1``), which would
        silently overwrite the earlier snapshot's image while both metadata
        entries survived.
        """
        meta = self._read_snapshots(cluster_name, cluster_cfg)
        existing = meta.get(snapshot_name)
        if existing:
            raise ConfigError(
                f"[{cluster_name}] snapshot '{snapshot_name}' already exists "
                f"(created {existing.get('created', '?')}). Delete it first: "
                f"boxman snapshot delete --name {snapshot_name}"
            )
        boxes = list(cluster_cfg.get("boxes") or {})
        if not boxes:
            return
        probe = _snapshot_tag(project, boxes[0], snapshot_name)
        clash = [
            name for name, snap in meta.items()
            if probe in (snap.get("boxes") or {}).values()
        ]
        if clash:
            raise ConfigError(
                f"[{cluster_name}] snapshot name '{snapshot_name}' maps to the "
                f"same docker tag as existing snapshot '{clash[0]}' ({probe}) "
                f"— taking it would overwrite that snapshot's image. Pick a "
                f"name that differs by more than punctuation."
            )

    def _rollback_commits(
        self, cluster_name: str, runner: ComposeRunner,
        tags: dict[str, str], snapshot_name: str,
    ) -> None:
        """Best-effort removal of images already committed by a ``take`` that
        then failed — without this they sit on disk with no metadata entry, so
        ``snapshot delete`` could never reclaim them."""
        for box, tag in tags.items():
            runner.image_rm(tag)
            self.logger.warning(
                f"[{cluster_name}] rolled back partial snapshot "
                f"'{snapshot_name}': removed {tag} (box '{box}')"
            )

    def _snapshot_meta_path(
        self, cluster_name: str, cluster_cfg: dict[str, Any]
    ) -> str:
        return os.path.join(
            self._workdir(cluster_cfg, cluster_name), "snapshots.json")

    def _read_snapshots(
        self, cluster_name: str, cluster_cfg: dict[str, Any]
    ) -> dict[str, Any]:
        path = self._snapshot_meta_path(cluster_name, cluster_cfg)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path) as fobj:
                data = json.load(fobj)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError) as exc:
            self.logger.warning(
                f"[{cluster_name}] could not read {path}: {exc}")
            return {}

    def _save_snapshots(
        self, cluster_name: str, cluster_cfg: dict[str, Any], meta: dict[str, Any]
    ) -> None:
        path = self._snapshot_meta_path(cluster_name, cluster_cfg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fobj:
            json.dump(meta, fobj, indent=2, sort_keys=True)

    def _check(self, cluster_name: str, op: str, result: Any,
               tool: str = "docker compose") -> bool:
        """Warn (don't raise) when a best-effort teardown op did not succeed.

        The teardown runner methods shell out with ``warn=True`` (no raise);
        their ``Result.ok`` is inspected here so a failed ``stop``/``down`` is
        surfaced instead of being silently reported as success. A ``result``
        without an ``ok`` attribute (e.g. a test double returning ``None``) is
        treated as success. *tool* names the binary for the message — the
        image ops (snapshots) are plain ``docker``, not ``docker compose``.
        """
        if not getattr(result, "ok", True):
            self.logger.warning(
                f"[{cluster_name}] '{tool} {op}' reported failure: "
                f"{_result_error(result)}"
            )
            return False
        return True

    # -- internals ---------------------------------------------------------

    def _compose_context(
        self, cluster_name: str, cluster_cfg: dict[str, Any]
    ) -> tuple[ComposeRunner, str, str]:
        """Regenerate the compose file (idempotent) and build a runner.

        Regeneration is for **bring-up only** — ``up_cluster`` and
        ``snapshot_restore_cluster`` (a restore is a bring-up, just from the
        snapshot's image tags); teardown uses :meth:`_teardown_runner`, which
        never regenerates.
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
            ) from None
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
                # Same resolver the generator uses to emit the mount, so the dir
                # we create is exactly the one docker will bind.
                abs_host = resolve_local_path(str(host_path), conf_dir)
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
        return compose_project_name(self.config, cluster_name)

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


def _format_ports(publishers: Any) -> str:
    """Compact ``hostport->targetport`` string from a compose ps ``Publishers``
    list (or pass through a plain string / empty)."""
    if not isinstance(publishers, list):
        return str(publishers or "")
    parts: list[str] = []
    for pub in publishers:
        if not isinstance(pub, dict):
            continue
        published, target = pub.get("PublishedPort"), pub.get("TargetPort")
        if published:
            parts.append(f"{published}->{target}")
        elif target:
            parts.append(str(target))
    return ", ".join(parts)


def compose_project_name(config: dict[str, Any], cluster_name: str) -> str:
    """The ``docker compose -p`` project name for a dc cluster — the single
    source of truth shared by :meth:`DockerComposeSession._compose_project`
    (compose ops) and ``BoxmanManager._compose_project_for`` (inventory
    ``ansible_host`` derivation), so the two can never drift apart."""
    dc = (config.get("provider") or {}).get("docker-compose") or {}
    base = dc.get("project_name") or config.get("project") or "boxman"
    return _sanitize_project_name(f"{base}_{cluster_name}")

def _result_error(result: Any) -> str:
    """The most useful error text off a shell ``Result`` (stderr, else stdout)."""
    return (
        getattr(result, "stderr", "") or getattr(result, "stdout", "") or ""
    ).strip()


#: docker caps a tag component at 128 chars; reject past that with our own
#: message rather than letting ``docker commit`` fail with a raw
#: "invalid reference format".
MAX_TAG_LEN = 128


def _snapshot_tag(project: str, box: str, snapshot_name: str) -> str:
    """Docker image reference for a box snapshot:
    ``boxman/<compose-project>_<box>:<snap>``, where the compose project is
    itself ``<project>_<cluster>`` — so snapshots of same-named boxes in
    different clusters do not collide. The repo path must be lowercase
    (``[a-z0-9._/-]``); the tag allows ``[A-Za-z0-9_.-]``."""
    def _repo(part: str) -> str:
        return re.sub(r"[^a-z0-9._-]", "-", part.lower()).strip("-._") or "x"

    def _tag(part: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "-", part).strip("-._") or "snap"

    tag = _tag(snapshot_name)
    if len(tag) > MAX_TAG_LEN:
        raise ConfigError(
            f"snapshot name '{snapshot_name}' is too long — docker caps an "
            f"image tag at {MAX_TAG_LEN} characters (this one is {len(tag)})."
        )
    return f"boxman/{_repo(project)}_{_repo(box)}:{tag}"


def _sanitize_project_name(name: str) -> str:
    """Coerce *name* to a valid compose project name (``[a-z0-9][a-z0-9_-]*``).

    Thin wrapper over :func:`boxman.utils.compose_names.sanitize_project_name`
    keeping this call site's rule set (underscores kept, ``boxman`` fallback).
    """
    return sanitize_project_name(name, allow_underscore=True)
