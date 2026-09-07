"""Docker Compose cluster orchestration for BoxmanManager."""












from boxman.exceptions import ConfigError, ProvisionError, SnapshotError


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
                # -> bool: a restore the session refused reports False
                # rather than raising (#164 FB-6).
                if not self.session_for_cluster(cname).snapshot_restore_cluster(
                        cname, cluster, snap):
                    failed.append(cname)
                    self.logger.error(f"[{cname}] snapshot restore failed")
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

        The snapshot session methods are annotated ``-> bool`` and report a
        refusal by returning False — a ``docker image rm`` blocked by a
        running container, for one. Discarding that made a failed dc
        snapshot delete report success (#164 FB-6).
        """
        any_selected = False
        failed: list[str] = []
        for cname, cluster in self._select_dc_clusters(cli_args):
            any_selected = True
            try:
                if not func(cname, cluster):
                    failed.append(cname)
                    self.logger.error(f"[{cname}] snapshot {op_label} failed")
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
        raise SnapshotError(
            f"snapshot {op_label} failed for docker-compose cluster(s): "
            f"{', '.join(failed)}")

    def _for_each_compose_cluster(self, op_label, func) -> list[str]:
        """Apply *func(cluster_name, cluster_cfg)* to every dc cluster,
        isolating failures and checking what the session reported.

        The lifecycle verbs used to call the session methods in a bare
        loop and discard their return value, so a cluster that cleanly
        failed to stop was reported as stopped, and a cluster that
        *raised* skipped every cluster after it (#164 FB-6). This is the
        lifecycle sibling of :meth:`_for_each_dc_cluster`, which already
        does the same for the snapshot verbs — the difference is that
        these verbs act on every cluster rather than a ``--cluster``
        selection, and that the sessions report failure by returning
        False as well as by raising.

        Returns:
            The names of the clusters that failed, in config order.
        """
        failed: list[str] = []
        for cname, cluster in self._compose_clusters.items():
            try:
                if not func(cname, cluster):
                    failed.append(cname)
                    self.logger.error(f"[{cname}] {op_label} failed")
            except Exception as exc:
                failed.append(cname)
                self.logger.error(f"[{cname}] {op_label} failed: {exc}")
        return failed

    @staticmethod
    def _raise_for_compose_failures(failed, op_label) -> None:
        """Raise one aggregated error naming every cluster that failed."""
        if failed:
            raise ProvisionError(
                f"docker-compose {op_label} failed for "
                f"{len(failed)} cluster(s): {', '.join(failed)} — "
                f"see the preceding per-cluster errors for the cause.")

    # --- docker-compose clusters: coarse per-cluster lifecycle ------------
    # docker-compose is cluster-scoped (one `docker compose up --wait` per
    # cluster, ADR-001/D1). Rather than the per-VM libvirt loops, the manager
    # dispatches a whole dc cluster to its session's coarse methods. These
    # helpers are no-ops for libvirt-only projects (``_compose_clusters()``
    # is empty).
    def provision_compose_clusters(self) -> None:
        """``docker compose up --wait`` every docker-compose cluster."""
        self._reject_compose_project_collisions()
        failed = self._for_each_compose_cluster(
            'up',
            lambda name, cfg: self.session_for_cluster(name).up_cluster(name, cfg))
        self._raise_for_compose_failures(failed, 'up')

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
        failed = self._for_each_compose_cluster(
            'stop',
            lambda name, cfg: self.session_for_cluster(name).stop_cluster(name, cfg))
        self._raise_for_compose_failures(failed, 'stop')

    def start_compose_clusters(self) -> None:
        """``docker compose start`` every docker-compose cluster.

        Reserved API surface for the later control-verb phase (a cheaper,
        no-recreate ``start`` after ``stop``). Not wired into a flow yet:
        ``up``-after-``down`` currently reconciles via
        :meth:`provision_compose_clusters` (``up -d --wait``), which also
        starts stopped containers and re-asserts readiness.
        """
        failed = self._for_each_compose_cluster(
            'start',
            lambda name, cfg: self.session_for_cluster(name).start_cluster(name, cfg))
        self._raise_for_compose_failures(failed, 'start')

    def deprovision_compose_clusters(self) -> None:
        """``docker compose down`` every docker-compose cluster (keep volumes).

        Raises on failure so ``deprovision``'s existing gate records it and
        the workspace/cache are not torn down over surviving containers.
        """
        failed = self._for_each_compose_cluster(
            'down',
            lambda name, cfg: self.session_for_cluster(name).down_cluster(name, cfg))
        self._raise_for_compose_failures(failed, 'down')

    def destroy_compose_clusters(self) -> None:
        """``docker compose down --volumes`` every docker-compose cluster.

        Raises on failure so ``destroy``'s ``teardown_ok`` gate sees it —
        without this a cluster whose containers survived still let the
        workspace be deleted and the project be unregistered (#164 X2).
        """
        failed = self._for_each_compose_cluster(
            'down --volumes',
            lambda name, cfg: self.session_for_cluster(name).destroy_cluster(name, cfg))
        self._raise_for_compose_failures(failed, 'down --volumes')
