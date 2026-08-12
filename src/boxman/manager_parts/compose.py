"""Docker Compose cluster orchestration for BoxmanManager."""












from boxman.exceptions import ConfigError


class ComposeMixin:

    def _select_dc_clusters(self, cli_args) -> list[tuple[str, dict]]:
        """docker-compose clusters selected by ``--cluster`` (an unset/absent
        ``--cluster`` selects all). ``(name, cfg)`` pairs — empty for a
        libvirt-only project.

        A **narrowed** ``--vms`` (anything other than the default ``all``)
        deselects every dc cluster: ``--vms`` names libvirt VMs and has no
        container meaning, so treating it as "VMs only" keeps an explicitly
        scoped command — ``snapshot restore --vms node01`` — from also
        force-recreating containers the user scoped away. ``--cluster``
        remains the way to reach containers, and wins when both are given.
        """
        dc_clusters = self._compose_clusters
        if not dc_clusters:
            return []
        wanted = getattr(cli_args, 'cluster', None)
        vms = getattr(cli_args, 'vms', None)
        if wanted is None and vms not in (None, '', 'all'):
            self.logger.info(
                f"--vms is libvirt-only, so docker-compose cluster(s) "
                f"{', '.join(dc_clusters)} were skipped; use --cluster to "
                f"include containers."
            )
            return []
        return [
            (name, cluster)
            for name, cluster in dc_clusters.items()
            if wanted in (None, name)
        ]

    def _restore_dc_plan(self, dc_plan) -> list:
        """Run the validated docker-compose restores, isolating per-cluster
        failures so one bad cluster can't strand the rest. Returns the names
        of the clusters that failed."""
        if not dc_plan:
            return []
        # The macvlan parent bridges must exist before compose recreates the
        # containers — after a host reboot they are gone and the recreate
        # would fail with a cryptic "parent interface does not exist".
        self.ensure_shared_bridges()
        failed = []
        for cname, cluster, snap in dc_plan:
            try:
                self.session_for_cluster(cname).snapshot_restore_cluster(
                    cname, cluster, snap)
            except Exception as exc:
                failed.append(cname)
                self.logger.error(
                    f"[{cname}] snapshot restore failed: {exc}")
        return failed

    def _for_each_dc_cluster(self, cli_args, op_label, func) -> tuple[bool, list]:
        """Apply *func(cluster_name, cluster_cfg)* to each selected dc cluster,
        isolating failures.

        Returns ``(any_selected, failed_cluster_names)``. Without this a single
        failing dc cluster — e.g. one that was never brought up — would raise
        straight out of the verb and skip both the remaining dc clusters and
        every VM in a mixed project.
        """
        any_selected = False
        failed: list[str] = []
        for cname, cluster in self._select_dc_clusters(cli_args):
            any_selected = True
            try:
                func(cname, cluster)
            except Exception as exc:
                failed.append(cname)
                self.logger.error(f"[{cname}] snapshot {op_label} failed: {exc}")
        return any_selected, failed

    def _exit_if_dc_failed(self, failed, op_label) -> None:
        """Exit non-zero when any dc cluster failed, *after* the rest of the
        verb has run — the failure is reported per cluster as it happens, but
        the command must not report success overall."""
        if not failed:
            return
        import sys
        self.logger.error(
            f"snapshot {op_label} failed for docker-compose cluster(s): "
            f"{', '.join(failed)}"
        )
        sys.exit(1)

    # --- docker-compose clusters: coarse per-cluster lifecycle ------------
    # docker-compose is cluster-scoped (one `docker compose up --wait` per
    # cluster, ADR-001/D1). Rather than the per-VM libvirt loops, the manager
    # dispatches a whole dc cluster to its session's coarse methods. These
    # helpers are no-ops for libvirt-only projects (``_compose_clusters()``
    # is empty).
    def provision_compose_clusters(self) -> None:
        """``docker compose up --wait`` every docker-compose cluster."""
        self._reject_compose_project_collisions()
        for cluster_name, cluster in self._compose_clusters.items():
            self.session_for_cluster(cluster_name).up_cluster(cluster_name, cluster)

    def _reject_compose_project_collisions(self) -> None:
        """
        Reject two docker-compose clusters whose sanitized ``docker compose``
        project names collide (e.g. ``web.api`` and ``web_api`` both →
        ``<base>_web_api``, or case-only differences).

        Colliding clusters would share compose state; teardown runs
        ``docker compose down --remove-orphans``, so tearing one down could
        delete the sibling's containers. Fail fast at provision — before any
        compose state exists — with an actionable message.

        Raises:
            ConfigError: If any two dc clusters map to the same project name.
        """
        seen: dict[str, str] = {}
        for cluster_name in self._compose_clusters:
            proj = self.session_for_cluster(cluster_name).compose_project_name(
                cluster_name)
            if proj in seen:
                raise ConfigError(
                    f"clusters '{seen[proj]}' and '{cluster_name}' both map to "
                    f"docker compose project '{proj}' — rename one so their "
                    f"compose state can't collide (teardown uses "
                    f"--remove-orphans)."
                )
            seen[proj] = cluster_name

    def stop_compose_clusters(self) -> None:
        """``docker compose stop`` every docker-compose cluster (boxman down)."""
        for cluster_name, cluster in self._compose_clusters.items():
            self.session_for_cluster(cluster_name).stop_cluster(cluster_name, cluster)

    def start_compose_clusters(self) -> None:
        """``docker compose start`` every docker-compose cluster.

        Reserved API surface for the later control-verb phase (a cheaper,
        no-recreate ``start`` after ``stop``). Not wired into a flow yet:
        ``up``-after-``down`` currently reconciles via
        :meth:`provision_compose_clusters` (``up -d --wait``), which also
        starts stopped containers and re-asserts readiness.
        """
        for cluster_name, cluster in self._compose_clusters.items():
            self.session_for_cluster(cluster_name).start_cluster(cluster_name, cluster)

    def deprovision_compose_clusters(self) -> None:
        """``docker compose down`` every docker-compose cluster (keep volumes)."""
        for cluster_name, cluster in self._compose_clusters.items():
            self.session_for_cluster(cluster_name).down_cluster(cluster_name, cluster)

    def destroy_compose_clusters(self) -> None:
        """``docker compose down --volumes`` every docker-compose cluster."""
        for cluster_name, cluster in self._compose_clusters.items():
            self.session_for_cluster(cluster_name).destroy_cluster(cluster_name, cluster)
