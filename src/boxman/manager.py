import json
import os
import shutil
import subprocess
import time
from multiprocessing import Process, Queue
from typing import Any, Optional

import yaml

from boxman import log
from boxman.abstract.providers import ProviderSession
from boxman.config_cache import BoxmanCache
from boxman.exceptions import ConfigError, ProvisionError, SnapshotError
from boxman.manager_parts.config import ConfigMixin
from boxman.manager_parts.images import ImagesMixin
from boxman.manager_parts.naming import NamingMixin
from boxman.manager_parts.networks import NetworksMixin
from boxman.manager_parts.ssh import SSHMixin
from boxman.manager_parts.vms import VMsMixin
from boxman.manager_parts.workspace import WorkspaceMixin
from boxman.netlab import ContainerlabManager, shared_bridges
from boxman.providers import merge_provider_configs, primary_provider_type
from boxman.providers.libvirt.commands import VirshCommand
from boxman.runtime import RuntimeBase, create_runtime
from boxman.task_runner import TaskRunner
from boxman.utils.jinja_env import create_jinja_env

#: Seconds to wait for a finished child's queue message before declaring its
#: result missing (the child has already joined, so any wait here is just
#: feeder-thread flush latency — or a child that died before queue.put).
_PARALLEL_RESULT_TIMEOUT = 5


def _parallel_worker(result_queue, label, target, args):
    """
    Child-process wrapper for :meth:`BoxmanManager._run_parallel`.

    Always reports the outcome on *result_queue* — even when *target*
    raises — so the parent can never mistake a crashed worker for a
    success (or block forever waiting for a message that never comes).
    """
    try:
        result_queue.put((label, True, target(*args)))
    except Exception as exc:
        result_queue.put((label, False, f"{type(exc).__name__}: {exc}"))




class BoxmanManager(ConfigMixin, WorkspaceMixin, NamingMixin, SSHMixin, ImagesMixin, NetworksMixin, VMsMixin):
    def __init__(self,
                 config: dict[str, Any] | None = None):
        """
        Initialize the BoxmanManager.

        Args:
            config: Optional configuration dictionary or path to config file
        """
        #: Optional[str]: the path to the configuration file if one was provided
        self.config_path: str | None = None

        #: Optional[Dict[str, Any]]: the loaded configuration dictionary
        self.config: dict[str, Any] | None = None

        #: the private backing field for the provider property (the
        #: default session — see :meth:`register_session`)
        self._provider = None

        #: Dict[str, ProviderSession]: live sessions keyed by provider
        #: type ('libvirt', 'virtualbox', …); populated by
        #: :meth:`register_session` and consumed by
        #: :meth:`session_for_cluster`.
        #:
        #: .. note:: Keying by provider *type* is correct for Phase 1:
        #:    a libvirt host has one connection, so two libvirt clusters
        #:    share one session. The per-cluster session model ADR-001
        #:    needs (one docker-compose *project* per cluster) re-keys
        #:    this map by cluster in Phase 3 (#51); the
        #:    :meth:`session_for_cluster` / :meth:`session_for_vm` seam
        #:    keeps every call site unchanged when that lands.
        self._sessions: dict[str, ProviderSession] = {}

        #: the logger instance
        self.logger = log

        #: str: the runtime environment name ('local', 'docker-compose', etc.)
        self._runtime_name: str = 'local'

        #: Optional[RuntimeBase]: the resolved runtime instance (created lazily)
        self._runtime_instance: RuntimeBase | None = None

        #: Optional[ContainerlabManager]: created lazily when conf.yml has a
        #: ``containerlab:`` block (see :meth:`netlab`).
        self._netlab: ContainerlabManager | None = None

        if isinstance(config, str):
            self.config_path = config
            self.config = self.load_config(config)
            self.resolve_workspace_defaults()

        self.cache = BoxmanCache()

        #: Optional[Dict[str, Any]]: the boxman application-level config (from boxman.yml)
        self.app_config: dict[str, Any] | None = None

    @property
    def provider(self) -> Optional["ProviderSession"]:
        """
        Get the default provider session (compat shim).

        Cluster-scoped flows should use :meth:`session_for_cluster`
        instead; this property remains for call sites that are
        provider-type specific (import-image, template management) or
        genuinely single-session.

        Returns:
            The provider session instance or None if not initialized.
            Any provider that satisfies the ``ProviderSession`` protocol
            (``LibVirtSession``, ``VirtualBoxSession``, ...) is accepted.
        """
        return self._provider

    @provider.setter
    def provider(self, value: "ProviderSession") -> None:
        """
        Set the default provider session (compat shim).

        Prefer :meth:`register_session`. The setter keeps older call
        sites and tests working: the assigned session is also registered
        under the project's primary provider type so that
        :meth:`session_for_cluster` resolves it.

        Args:
            value: The provider session instance
        """
        self._provider = value
        if value is not None:
            # ``getattr`` keeps the setter safe on ``__new__``-built
            # managers (used in tests), which have no ``config`` yet.
            self._get_sessions()[
                primary_provider_type(getattr(self, 'config', None))
            ] = value

    def _get_sessions(self) -> dict[str, "ProviderSession"]:
        """
        Return the provider-type → session map, creating it if needed.

        Managers are sometimes constructed with ``__new__`` in tests
        (bypassing ``__init__``); the lazy guard keeps the session map
        available on those instances too.
        """
        if not hasattr(self, '_sessions'):
            self._sessions = {}
        return self._sessions

    def _update_sessions_with_runtime(self) -> None:
        """
        Ensure every registered session's provider config carries the
        runtime metadata (``runtime``, ``runtime_container``) so provider
        commands are wrapped for the active runtime.

        Call this at the start of every verb flow that drives provider
        commands; without it a session built before the runtime was
        resolved would run virsh on the local host instead of inside the
        runtime container.
        """
        for _session in self._get_sessions().values():
            if hasattr(_session, 'update_provider_config_with_runtime'):
                _session.update_provider_config_with_runtime()

    def register_session(self, provider_type: str, session: "ProviderSession") -> None:
        """
        Register a live *session* for *provider_type*.

        The first registered session also becomes the default session
        returned by the legacy :attr:`provider` property.

        Args:
            provider_type: The provider type name (e.g. ``libvirt``).
            session: The constructed provider session.
        """
        self._get_sessions()[provider_type] = session
        if getattr(self, '_provider', None) is None:
            self._provider = session

    def provider_type_for_cluster(self, cluster_name: str) -> str:
        """
        Resolve the provider type that manages *cluster_name*.

        A per-cluster ``provider:`` key wins (config schema v2.0 makes
        this official in Phase 2 of the docker-compose provider epic);
        otherwise the project's primary provider type applies.

        Args:
            cluster_name: The cluster name as it appears under
                ``clusters:`` in the project config.

        Returns:
            The provider type name.
        """
        cluster = ((self.config or {}).get('clusters') or {}).get(cluster_name) or {}
        return cluster.get('provider') or primary_provider_type(self.config)

    def _has_libvirt_clusters(self) -> bool:
        """
        Whether this project has any clusters that resolve to the libvirt
        provider. Projects without a ``provider:`` section default to
        libvirt (see :func:`primary_provider_type`).

        Used to skip libvirt-only virsh probes in projects that have no
        libvirt clusters at all (e.g. a pure docker-compose project).
        """
        config = self.config or {}
        clusters = config.get('clusters') or {}
        if not clusters:
            return False
        if not (config.get('provider') or {}):
            return True
        return any(
            self.provider_type_for_cluster(name) == 'libvirt'
            for name in clusters
        )

    def _libvirt_provider_config(self) -> dict[str, Any]:
        """
        Resolve the libvirt provider config for a one-off virsh probe:
        the project's ``provider.libvirt`` block merged over app-level
        (boxman.yml) ``providers.libvirt`` defaults, with runtime
        metadata injected.
        """
        provider_config = (
            (self.config or {}).get('provider', {}).get('libvirt', {}))
        if self.app_config and 'providers' in self.app_config:
            app_prov = self.app_config['providers'].get('libvirt', {})
            provider_config = merge_provider_configs(app_prov, provider_config)
        if hasattr(self, 'runtime_instance'):
            provider_config = self.runtime_instance.inject_into_provider_config(
                provider_config)
        return provider_config

    def _virsh(self) -> "VirshCommand":
        """
        Build a one-off VirshCommand for a libvirt probe, using the config
        from :meth:`_libvirt_provider_config` so app-level settings and the
        runtime (local host vs. container) are honored.
        """
        return VirshCommand(provider_config=self._libvirt_provider_config())

    def session_for_cluster(self, cluster_name: str) -> "ProviderSession":
        """
        Return the provider session that manages *cluster_name*.

        Args:
            cluster_name: The cluster name as it appears under
                ``clusters:`` in the project config.

        Returns:
            The provider session registered for the cluster's provider
            type.

        Raises:
            ValueError: If no session is registered for the cluster's
                provider type.
        """
        provider_type = self.provider_type_for_cluster(cluster_name)
        session = self._get_sessions().get(provider_type)
        if session is not None:
            return session
        # Compat parity: when nothing is registered for the project's
        # *primary* type but a default session was assigned directly
        # (older tests / call sites poke ``_provider`` without going
        # through :meth:`register_session`), resolve to it — exactly
        # what the old single ``self.provider`` did for every flow.
        default = getattr(self, '_provider', None)
        if default is not None and provider_type == primary_provider_type(self.config):
            return default
        raise ValueError(
            f"no provider session registered for provider "
            f"'{provider_type}' (needed by cluster '{cluster_name}')"
        )

    def _dc_session(self, cluster_name: str) -> "ProviderSession":
        """Return the docker-compose session for *cluster_name*, lazily creating
        and caching one if none is registered.

        The read-only display/access verbs (``ps``/``connect_info``/``exec``)
        dispatch without the full provider-setup path, so a session may not be
        pre-registered — a dc session is cheap and needs no libvirt, so it is
        built on demand here rather than requiring that setup.
        """
        try:
            return self.session_for_cluster(cluster_name)
        except ValueError:
            from boxman.providers import create_session
            session = create_session('docker-compose', self.config)
            session.manager = self
            self._get_sessions()['docker-compose'] = session
            return session

    def session_for_vm(self, full_vm_name: str) -> Optional["ProviderSession"]:
        """
        Resolve the provider session that manages *full_vm_name*.

        Convenience for flow helpers that only hold a full VM name
        (``bprj__<project>__bprj_<cluster>_<vm>``). Names that cannot be
        derived from the current config (e.g. VMs already removed from
        it) fall back to the default session.

        Args:
            full_vm_name: The full (project-prefixed) VM name.

        Returns:
            The provider session managing the VM's cluster, or the
            default session when the name is not in the config. May be
            ``None`` when the name is unknown and no default session has
            been registered — the same NoneType behaviour the old
            ``self.provider`` call sites had.
        """
        cluster_name = self._vm_cluster_map().get(full_vm_name)
        if cluster_name is None:
            return self.provider
        return self.session_for_cluster(cluster_name)

    def _is_compose_cluster(self, cluster_name: str) -> bool:
        """True if *cluster_name* is provisioned by the docker-compose provider."""
        return self.provider_type_for_cluster(cluster_name) == 'docker-compose'

    @property
    def _vm_clusters(self) -> dict[str, Any]:
        """
        Clusters handled by a **per-VM** provider (libvirt).

        The manager's per-VM / per-network lifecycle helpers iterate this
        instead of ``config['clusters']`` so docker-compose clusters (which
        carry ``boxes:``, not ``vms:``, and are provisioned coarsely via the
        ``*_compose_clusters`` helpers) are never fed into the per-VM loops.
        For a libvirt-only project this is exactly ``config['clusters']``, so
        behaviour is unchanged.
        """
        return {
            name: cluster
            for name, cluster in (self.config.get('clusters') or {}).items()
            if not self._is_compose_cluster(name)
        }

    @property
    def _compose_clusters(self) -> dict[str, Any]:
        """docker-compose clusters, in config order (empty for libvirt-only
        projects). A ``@property`` for symmetry with :attr:`_vm_clusters`."""
        return {
            name: cluster
            for name, cluster in (self.config.get('clusters') or {}).items()
            if self._is_compose_cluster(name)
        }

    def _compose_project_for(self, cluster_name: str) -> str:
        """The ``docker compose -p`` project name for a dc cluster, so container
        names can be derived (``<project>-<box>-1``) at inventory-render time
        without a live session or docker query. Single-sourced with the
        session's ``_compose_project`` so the inventory ``ansible_host`` can
        never diverge from the real compose container name."""
        from boxman.providers.docker_compose.session import compose_project_name
        return compose_project_name(self.config, cluster_name)

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

    def _run_parallel(self, tasks, op_label='parallel task'):
        """
        Run picklable workers in child processes and report per-task failures.

        Args:
            tasks: iterable of ``(label, target, args)`` tuples; ``target`` is
                called as ``target(*args)`` in a child process. Labels must be
                unique within the batch.
            op_label: verb phrase used in the per-failure error messages.

        Returns:
            ``(results, failures)`` dicts keyed by task label. A task lands in
            ``failures`` when its child raised, exited non-zero, or reported
            no result at all (e.g. it was killed); otherwise the worker's
            return value lands in ``results``. Every failure is logged here —
            callers only need the return value when they react to failures
            (e.g. snapshot restore's retry rounds).
        """
        from queue import Empty

        tasks = list(tasks)
        if not tasks:
            return {}, {}

        result_queue: Queue = Queue()
        processes = [
            Process(
                target=_parallel_worker,
                args=(result_queue, label, target, args))
            for label, target, args in tasks
        ]
        [p.start() for p in processes]
        [p.join() for p in processes]

        reported = {}
        for _ in processes:
            try:
                label, ok, payload = result_queue.get(
                    timeout=_PARALLEL_RESULT_TIMEOUT)
                reported[label] = (ok, payload)
            except Empty:
                # A child exited without reporting (killed, or the queue
                # broke) — the per-process loop below marks it as failed.
                break

        results: dict[str, Any] = {}
        failures: dict[str, str] = {}
        for i, (label, _target, _args) in enumerate(tasks):
            if processes[i].exitcode != 0:
                failures[label] = f"worker exited with code {processes[i].exitcode}"
            elif label not in reported:
                failures[label] = "worker exited without reporting a result"
            else:
                ok, payload = reported[label]
                if ok:
                    results[label] = payload
                else:
                    failures[label] = str(payload)

        for label, reason in failures.items():
            self.logger.error(f"{op_label} failed for {label}: {reason}")
        return results, failures

    def _vm_cluster_map(self) -> dict[str, str]:
        """
        Map every full VM name of this project to its cluster name.

        Uses the same name construction as the provision/destroy flows
        (``bprj__<project>__bprj_<cluster>_<vm>``) so call sites that
        only hold a full VM name (e.g. the post-provision start retry
        loop) can resolve the owning cluster without parsing names.

        Returns:
            Mapping of full VM name to cluster name. Empty when the
            config is unset or has no ``project`` (e.g. the import-image
            provider-slice config) so :meth:`session_for_vm` cleanly
            falls back to the default session instead of raising.
        """
        config = self.config or {}
        if 'project' not in config:
            return {}
        prj_name = f'bprj__{config["project"]}__bprj'
        return {
            f"{prj_name}_{cluster_name}_{vm_name}": cluster_name
            for cluster_name, cluster in (config.get('clusters') or {}).items()
            for vm_name in (cluster.get('vms') or {}).keys()
        }

    @property
    def runtime(self) -> str:
        """Return the runtime environment name."""
        return self._runtime_name

    @runtime.setter
    def runtime(self, value: str) -> None:
        """Set the runtime environment name and reset the cached instance."""
        self._runtime_name = value
        self._runtime_instance = None  # force re-creation

    @property
    def runtime_instance(self) -> RuntimeBase:
        """
        Return the runtime instance, creating it on first access.

        The runtime config is taken from ``app_config`` if available.
        """
        if self._runtime_instance is None:
            runtime_config = (self.app_config or {}).get("runtime_config", {})
            self._runtime_instance = create_runtime(
                self._runtime_name, config=runtime_config
            )
        return self._runtime_instance

    @property
    def netlab(self) -> ContainerlabManager | None:
        """Return the ContainerlabManager for this project, or ``None``.

        Created lazily on first access when ``conf.yml`` has a
        ``containerlab:`` block with ``enabled: true`` (default).
        """
        if self._netlab is not None:
            return self._netlab
        if not self.config:
            return None
        lab_config = self.config.get("containerlab")
        if not lab_config or not lab_config.get("enabled", True):
            return None

        workspace_path = (self.config.get("workspace", {}) or {}).get("path")
        workdir = workspace_path or os.path.dirname(self.config_path or ".") or "."
        # The workdir must be absolute: containerlab resolves relative
        # startup-config paths in the topology against the topology file's
        # own directory, so a relative workdir would emit paths like
        # ``netlab/configs/x.cfg`` that containerlab then looks up as
        # ``netlab/netlab/configs/x.cfg``.
        workdir = os.path.abspath(os.path.expanduser(workdir))
        jinja_env = create_jinja_env(workdir)
        self._netlab = ContainerlabManager(
            lab_config=lab_config,
            workdir=workdir,
            jinja_env=jinja_env,
        )
        return self._netlab

    def get_provider_config_with_runtime(
        self, provider_config: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Return a copy of *provider_config* enriched with runtime metadata.

        This should be called before passing the config to provider command
        classes (``VirshCommand``, ``VirtInstallCommand``, etc.) so they
        know how to wrap commands.

        Args:
            provider_config: The raw provider configuration dict.

        Returns:
            A new dict with ``runtime`` and related keys injected.
        """
        return self.runtime_instance.inject_into_provider_config(provider_config)

    ### register/un-register the project in the cache
    def register_project_in_cache(self) -> None:
        """
        Register the project in the Boxman cache.

        This method saves the project configuration to the cache for later use.

        Raises:
            RuntimeError: If the project is already registered in the cache.
        """
        success = self.cache.register_project(
            project_name=self.config['project'],
            config_fpath=self.config_path,
            runtime=self._runtime_name)

        if success is False:
            raise RuntimeError(
                f"Project '{self.config['project']}' is already in the cache. "
                f"Deprovision it first with: boxman deprovision"
            )

    def unregister_from_cache(self) -> None:
        """
        Register the project in the Boxman cache.

        This method saves the project configuration to the cache for later use.
        """
        self.cache.unregister_project(project_name=self.config['project'])

    def list_projects(self, cli_args) -> None:
        """
        List all registered projects.
        """
        projects = self.cache.list_projects()

        pretty = getattr(cli_args, 'pretty', None) if cli_args else None
        use_json = getattr(cli_args, 'json', False) if cli_args else False
        use_color = getattr(cli_args, 'color', 'yes') != 'no' if cli_args else True

        # --- JSON output ---
        if use_json:
            print(json.dumps(projects if projects else {}, indent=2, default=str))
            return

        # ANSI helpers
        if use_color and pretty:
            bold = "\033[1m"
            cyan = "\033[1;36m"
            green = "\033[1;32m"
            yellow = "\033[1;33m"
            dim = "\033[2m"
            reset = "\033[0m"
        else:
            bold = cyan = green = yellow = dim = reset = ""

        if not projects:
            if pretty:
                print(f"{yellow}No projects registered.{reset}")
            else:
                self.logger.info("No projects registered.")
            return

        if pretty == 'table':
            # Collect rows: [project, config, runtime, networks_summary]
            rows = []
            for proj_name, proj_info in projects.items():
                if isinstance(proj_info, dict):
                    conf = proj_info.get('conf', 'n/a')
                    runtime = proj_info.get('runtime', 'n/a')
                    networks = proj_info.get('networks', {})
                    net_parts = []
                    for net_name, net_info in networks.items():
                        if isinstance(net_info, dict):
                            ip = net_info.get('ip_address', 'n/a')
                            bridge = net_info.get('bridge_name', 'n/a')
                            net_parts.append(f"{net_name} (ip={ip}, br={bridge})")
                        else:
                            net_parts.append(net_name)
                    nets_str = "; ".join(net_parts) if net_parts else "-"
                else:
                    conf = str(proj_info)
                    runtime = "n/a"
                    nets_str = "-"
                rows.append((proj_name, conf, runtime, nets_str))

            headers = ("PROJECT", "CONFIG", "RUNTIME", "NETWORKS")
            # compute column widths
            col_widths = [len(h) for h in headers]
            for row in rows:
                for i, cell in enumerate(row):
                    col_widths[i] = max(col_widths[i], len(cell))

            def fmt_row(cells, bold=False):
                parts = []
                for i, cell in enumerate(cells):
                    parts.append(cell.ljust(col_widths[i]))
                line = "  ".join(parts)
                if bold:
                    return f"{bold}{line}{reset}"
                return line

            print()
            print(fmt_row(headers, bold=True))
            print("  ".join("-" * w for w in col_widths))
            for row in rows:
                print(fmt_row(row))
            print()

        elif pretty == 'plain':
            print()
            print(f"{bold}Registered projects:{reset}")
            print()
            for proj_name, proj_info in projects.items():
                print(f"  {cyan}{proj_name}{reset}")
                if isinstance(proj_info, dict):
                    conf = proj_info.get('conf', 'n/a')
                    runtime = proj_info.get('runtime', 'n/a')
                    print(f"    {dim}config:{reset}  {conf}")
                    print(f"    {dim}runtime:{reset} {runtime}")

                    networks = proj_info.get('networks', {})
                    if networks:
                        print(f"    {dim}networks:{reset}")
                        for net_name, net_info in networks.items():
                            ip = net_info.get('ip_address', 'n/a') if isinstance(net_info, dict) else 'n/a'
                            bridge = net_info.get('bridge_name', 'n/a') if isinstance(net_info, dict) else 'n/a'
                            print(f"      {green}-{reset} {net_name}")
                            print(f"          {dim}ip:{reset} {ip}  {dim}bridge:{reset} {bridge}")
                else:
                    print(f"    {proj_info}")
                print()

        else:
            # default logger-based output (no --pretty, no --json)
            self.logger.info("Registered projects:\n")
            for proj_name, proj_info in projects.items():
                self.logger.info(f"  project: {proj_name}")
                if isinstance(proj_info, dict):
                    conf = proj_info.get('conf', 'n/a')
                    runtime = proj_info.get('runtime', 'n/a')
                    self.logger.info(f"    config:  {conf}")
                    self.logger.info(f"    runtime: {runtime}")

                    networks = proj_info.get('networks', {})
                    if networks:
                        self.logger.info("    networks:")
                        for net_name, net_info in networks.items():
                            ip = net_info.get('ip_address', 'n/a') if isinstance(net_info, dict) else 'n/a'
                            bridge = net_info.get('bridge_name', 'n/a') if isinstance(net_info, dict) else 'n/a'
                            self.logger.info(f"      - {net_name}")
                            self.logger.info(f"          ip: {ip}  bridge: {bridge}")
                else:
                    self.logger.info(f"    {proj_info}")
                self.logger.info("")
    ### end register the project in the cache

    ### networks define / remove / destroy
    def ensure_shared_bridges(self) -> None:
        """Create host Linux bridges declared under top-level ``shared_networks:``.

        Idempotent and cross-project safe. No-op when ``shared_networks`` is
        absent. Bridges are intentionally *not* removed on destroy — multiple
        boxman projects can share the same bridge.
        """
        shared = (self.config or {}).get('shared_networks')
        if not shared:
            return
        self.logger.info(f"ensuring {len(shared)} shared bridge(s) exist on host")
        shared_bridges.ensure(shared)

    def deploy_netlab(self) -> None:
        """Render and deploy the containerlab topology, if configured.

        No-op when there's no ``containerlab:`` block or ``enabled: false``.
        Runs preflight first so a missing ``containerlab`` / ``docker`` binary
        surfaces a clear error before any shell-out is attempted.
        """
        netlab = self.netlab
        if netlab is None:
            return
        netlab.preflight()
        # Resolve startup-config templates relative to the *config file's*
        # directory. abspath() is required: run the documented way
        # (`cd boxes/<box> && boxman up`), config_path is the bare relative
        # default "conf.yml", so os.path.dirname() would return "" — which
        # render_topology treats as "unset" and falls back to the workspace
        # dir, where configs/ does not exist (FileNotFoundError on sw1's
        # startup-config).
        source_root = (os.path.dirname(os.path.abspath(self.config_path))
                       if self.config_path else None)
        netlab.render_topology(source_root=source_root)
        netlab.deploy()

    def destroy_netlab(self) -> None:
        """Tear down the containerlab lab, if configured.

        Ordered before libvirt/network teardown in ``deprovision`` so lab
        container veths release their hold on shared bridges first.
        """
        netlab = self.netlab
        if netlab is None:
            return
        try:
            netlab.preflight()
        except Exception as exc:
            # Binary missing post-hoc is survivable — we still want to tear
            # down libvirt state even if containerlab is no longer on PATH.
            self.logger.warning(f"skipping containerlab destroy: {exc}")
            return
        netlab.destroy()

    def ensure_netlab_up(self) -> None:
        """Idempotent reconciliation of the containerlab lab state.

        Called from ``boxman up`` so `down`/`up` cycles (or a host reboot)
        bring the lab back alongside the VMs. Deploys fresh if absent,
        starts stopped nodes if some containers linger, no-ops if already
        running.
        """
        netlab = self.netlab
        if netlab is None:
            return
        netlab.preflight()
        # Render the topology so ensure_up has a .clab.yml to deploy from
        # if the lab is missing entirely. abspath() so a bare relative
        # --conf (the default "conf.yml") resolves to the box directory
        # rather than "" — see deploy_netlab() for the full rationale.
        source_root = (os.path.dirname(os.path.abspath(self.config_path))
                       if self.config_path else None)
        netlab.render_topology(source_root=source_root)
        netlab.ensure_up()

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

    def provision(self, cli_args):

        config = self.config

        # Ensure provider configs reflect runtime settings.
        # Project-level provider settings (from conf.yml) always take
        # precedence over app-level defaults (from boxman.yml).
        self._update_sessions_with_runtime()

        # --- Pre-check: detect state that would block a clean provision ---
        # Block on either (a) live VMs from this project, or (b) a stale
        # cache entry with no live VMs. The second case used to slip
        # through --force: _find_existing_project_vms was empty so
        # deprovision was skipped, then register_project_in_cache
        # rejected the duplicate entry. Treat both as "needs force".
        force = getattr(cli_args, 'force', False)
        existing_vms = self._find_existing_project_vms()
        self.cache.read_projects_cache()
        project_name = config.get('project')
        in_cache = bool(
            project_name
            and project_name in (self.cache.projects or {})
        )

        if existing_vms or in_cache:
            reasons: list[str] = []
            if existing_vms:
                names = ", ".join(f"'{v}'" for v in existing_vms)
                reasons.append(f"existing VM(s): {names}")
            if in_cache:
                reasons.append(
                    f"project '{project_name}' is already registered in the cache")
            summary = "; ".join(reasons)

            if not force:
                raise ProvisionError(
                    f"cannot provision — {summary}. "
                    f"Use --force to deprovision first and re-provision."
                )

            self.logger.warning(
                f"state will be deprovisioned first (--force): {summary}"
            )
            self.deprovision(cli_args)
        # --------------------------------------------------------------

        try:
            self.register_project_in_cache()
        except RuntimeError as exc:
            raise ProvisionError(str(exc)) from exc

        # Expand any `base_image: oci://…` references into implicit templates
        # before template build / cloning (the clone path needs a VM name).
        self._expand_oci_base_images()

        # --rebuild-templates: force-recreate all templates before provisioning
        rebuild_templates = getattr(cli_args, 'rebuild_templates', False)
        if rebuild_templates:
            self.logger.info(
                "rebuilding all templates (--rebuild-templates implies --force "
                "for create-templates)..."
            )
            if self._create_templates_impl(requested=None, force=True):
                raise ProvisionError(
                    "aborting: not every template could be rebuilt")
        else:
            # Auto-create any template VMs that are referenced as base_image
            # but do not yet exist.
            if not self.ensure_templates_exist():
                raise ProvisionError(
                    "aborting: not every template could be created")

        try:
            self.validate_base_images()
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

        self.provision_files()

        self.ensure_shared_bridges()

        self.define_networks()

        self.clone_vms()

        self.configure_and_start_vms()

        # Ensure all VMs are actually running after the parallel start.
        # With many VMs starting simultaneously, some may fail due to resource
        # contention. Retry starting any that are not in 'running' state.
        self.logger.info("verifying all VMs are running after parallel start...")
        for _round in range(1, 21):
            vm_states = self._get_vm_states()
            not_running = {
                name: state for name, state in vm_states.items()
                if state != 'running'
            }
            if not not_running:
                self.logger.info("all VMs are running")
                break
            self.logger.info(
                f"round {_round}: {len(not_running)} VM(s) not yet running "
                f"({', '.join(f'{n}={s}' for n, s in not_running.items())}), retrying..."
            )
            for _vm_name in not_running:
                self.session_for_vm(_vm_name).start_vm(_vm_name)
            time.sleep(3)
        else:
            vm_states = self._get_vm_states()
            still_down = {n: s for n, s in vm_states.items() if s != 'running'}
            if still_down:
                self.logger.warning(
                    f"gave up after 20 rounds; the following VMs are still not running: "
                    f"{', '.join(f'{n}={s}' for n, s in still_down.items())}"
                )

        # use adaptive wait for ip address assignment
        self.wait_for_vm_ips(self._get_project_vm_names(), max_wait=600)

        # Eject cdrom (seed.iso) from every VM now that cloud-init has run.
        # This prevents snapshot-related failures caused by qcow2-over-raw
        # backing chain issues and tray-lock errors on subsequent snapshots.
        self.logger.info("ejecting cdrom (seed.iso) from all VMs post-provisioning...")
        prj_name = f'bprj__{config["project"]}__bprj'
        for _cluster_name, _cluster in self._vm_clusters.items():
            for _vm_name, _ in _cluster['vms'].items():
                _full_vm_name = f"{prj_name}_{_cluster_name}_{_vm_name}"
                self.session_for_cluster(_cluster_name).eject_cdrom(_full_vm_name)

        # generate ssh keys, add them to vms, and write ssh config
        self.setup_ssh_access()

        # display connection information (after ssh setup so connections are ready)
        self.connect_info()

        # bring up docker-compose clusters (no-op for libvirt-only projects);
        # after libvirt VMs, mirroring the netlab hook's "extra infra last" order
        self.provision_compose_clusters()

        # render and deploy the containerlab topology (no-op if not configured)
        self.deploy_netlab()

    def up(self, cli_args):
        """
        Bring up the infrastructure.

        - If no project VMs exist, run a full provision.
        - If all VMs exist and are running, do nothing.
        - If all VMs exist but some/all are not running (shut off, paused,
          saved), start/resume them.
        - If only some VMs exist (partial state) and --force is not set,
          error out. With --force, deprovision and re-provision.

        Reuses the same provider methods as ``boxman control start`` and
        ``boxman control resume``.
        """
        config = self.config
        expected_vms = self._get_project_vm_names()

        if not expected_vms:
            # No libvirt VMs. A docker-compose-only project still has work to do.
            if self._compose_clusters:
                # A first run (project not yet registered) must go through full
                # provision() — cache registration, provision_files() (cluster
                # files: + runtime sentinels) and netlab — exactly like a libvirt
                # project's first `up` (Case 1 below). provision() ends by calling
                # provision_compose_clusters(), so the containers come up too. A
                # subsequent `up` just reconciles the compose clusters (idempotent),
                # mirroring the libvirt "all running" reconcile path (Case 3).
                self.cache.read_projects_cache()
                project_name = config.get('project')
                if project_name and project_name in (self.cache.projects or {}):
                    # Shared bridges must exist before macvlan-attached
                    # containers come up: a host reboot drops the
                    # (non-persistent) Linux bridge, so recreate it on this
                    # dc-only reconcile path too — mirroring the hybrid
                    # "all VMs running" path (ensure_shared_bridges → up).
                    self.ensure_shared_bridges()
                    self.provision_compose_clusters()
                else:
                    self.logger.info(
                        "no existing project state found, running full provision...")
                    self.provision(cli_args)
                return
            raise ConfigError("no VMs defined in configuration")

        vm_states = self._get_vm_states()
        existing_names = set(vm_states.keys())
        expected_names = set(expected_vms)

        # --- Case 1: No VMs exist → full provision ---
        if not existing_names:
            self.logger.info("no existing VMs found, running full provision...")
            self.provision(cli_args)
            return

        # --- Case 2: Partial state (some exist, some don't) ---
        missing = expected_names - existing_names
        if missing:
            force = getattr(cli_args, 'force', False)
            names_str = ", ".join(f"'{v}'" for v in sorted(missing))
            if not force:
                raise ProvisionError(
                    f"partial infrastructure state: the following VM(s) are "
                    f"missing: {names_str}. Use --force to deprovision and "
                    f"re-provision everything."
                )
            else:
                self.logger.warning(
                    f"partial state detected (missing: {names_str}). "
                    f"Deprovisioning and re-provisioning (--force)..."
                )
                self.provision(cli_args)
                return

        # --- Case 3: All VMs exist → check states ---
        non_running = {
            name: state for name, state in vm_states.items()
            if state != 'running'
        }

        if not non_running:
            self.logger.info("all VMs are already running")
            # Still reconcile shared bridges + lab — a host reboot or a
            # manual `docker stop` may have left lab containers down
            # even though the VMs stayed up.
            self.ensure_shared_bridges()
            network_results = self.reconcile_networks(
                allow_recreate=getattr(cli_args, 'recreate_networks', False),
                auto_accept=getattr(cli_args, 'yes', False))
            self.report_network_results(network_results)

            # a recreate power-cycles the guests attached to the network, so
            # the addresses connect_info() and the ssh config are about to be
            # written from do not exist yet
            if any(outcome in ('recreated', 'partial')
                   for outcome in network_results.values()):
                self.wait_for_vm_ips(self._vms_worth_waiting_for())

            self.ensure_netlab_up()
            # Reconcile docker-compose clusters too: a host reboot or manual
            # `docker compose stop` may have left them down (idempotent).
            self.provision_compose_clusters()
            self.connect_info()
            # Re-write SSH config in case IPs changed (DHCP renewals after
            # a host reboot, manual virsh net cycle, etc.) or in case the
            # file is missing/stale from an older boxman version.
            self.write_ssh_config()
            return

        # --- Start / resume VMs that are not running ---
        self.logger.info(
            f"{len(non_running)} VM(s) are not running, bringing them up..."
        )

        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        # Shared bridges must exist before VMs attach to them on boot.
        self.ensure_shared_bridges()

        # Same for the libvirt networks: a VM that is about to be started has
        # to find the network it is wired to, and any reservation added to the
        # config since the last run has to be in dnsmasq before the guest asks
        # for a lease.
        self.report_network_results(self.reconcile_networks(
            allow_recreate=getattr(cli_args, 'recreate_networks', False),
            auto_accept=getattr(cli_args, 'yes', False)))

        # Build workdir lookup for restore operations
        vm_workdir_map = dict(self._control_vm_targets(cli_args))

        def _bring_up(vm_name, state, workdir):
            session = self.session_for_vm(vm_name)
            self.logger.info(f"VM '{vm_name}' is in state '{state}'")
            if state == 'paused':
                self.logger.info(f"resuming VM '{vm_name}'...")
                session.resume_vm(vm_name)
            elif state in ('saved', 'managedsave'):
                self.logger.info(f"restoring VM '{vm_name}' from saved state...")
                session.restore_vm(vm_name, workdir)
            elif state in ('shut off', 'shutoff'):
                self.logger.info(f"starting VM '{vm_name}'...")
                session.start_vm(vm_name)
            elif state in ('crashed', 'dying'):
                self.logger.warning(
                    f"VM '{vm_name}' is in state '{state}', "
                    f"attempting to destroy and start...")
                session.destroy_vm(vm_name, remove_storage=False)
                session.start_vm(vm_name)
            else:
                self.logger.warning(
                    f"VM '{vm_name}' is in unexpected state '{state}', "
                    f"attempting to start...")
                session.start_vm(vm_name)

        processes = [
            Process(
                target=_bring_up,
                args=(vm_name, state, vm_workdir_map.get(vm_name, ''))
            )
            for vm_name, state in non_running.items()
        ]
        [p.start() for p in processes]
        [p.join() for p in processes]

        # Wait for IP addresses
        self.wait_for_vm_ips(self._get_project_vm_names(), max_wait=300)

        # Reconcile the containerlab lab after the VMs are up so the
        # shared bridges have live endpoints on both sides.
        self.ensure_netlab_up()

        # Bring up docker-compose clusters after the VMs are up (idempotent).
        self.provision_compose_clusters()

        # Display connection information
        self.connect_info()

        # Re-write SSH config with current IPs
        self.write_ssh_config()

        self.logger.info("infrastructure is up")

    def down(self, cli_args):
        """
        Bring down the infrastructure by saving or suspending all VMs.

        By default, saves each VM's state to disk (same as
        ``boxman control save``). With ``--suspend``, pauses VMs in memory
        instead (same as ``boxman control suspend``).

        docker-compose clusters are always brought down with
        ``docker compose stop`` (containers kept, reversible via ``up``);
        ``--suspend`` does not apply to them — compose has no in-memory
        pause analog wired in this phase, so the flag is a no-op for dc
        clusters.

        Reuses ``_control_vm_targets()`` and the same provider methods as
        ``boxman control save`` / ``boxman control suspend``.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        vm_list = self._control_vm_targets(cli_args)

        if not vm_list and not self._compose_clusters:
            self.logger.info("no VMs found in configuration")
            return

        use_suspend = getattr(cli_args, 'suspend', False)

        if use_suspend:
            if vm_list:
                self.logger.info("suspending all VMs (--suspend)...")

            def _suspend(vm_name):
                self.logger.info(f"suspending VM '{vm_name}'...")
                self.session_for_vm(vm_name).suspend_vm(vm_name)
                self.logger.info(f"VM '{vm_name}' suspended")

            processes = [
                (vm_name, _suspend, (vm_name,))
                for vm_name, _ in vm_list
            ]
        else:
            if vm_list:
                self.logger.info("saving the state of all VMs to disk...")

            def _save(vm_name, workdir):
                self.logger.info(f"saving VM '{vm_name}' state to '{workdir}'...")
                self.session_for_vm(vm_name).save_vm(vm_name, workdir)
                self.logger.info(f"VM '{vm_name}' state saved")

            processes = [
                (vm_name, _save, (vm_name, workdir))
                for vm_name, workdir in vm_list
            ]

        self._run_parallel(processes, op_label='down')

        # Stop docker-compose clusters (keep containers; reversible via `up`).
        self.stop_compose_clusters()

        self.logger.info("infrastructure is down")

    def deprovision(self, cli_args):

        # Ensure provider configs reflect runtime settings.
        # Project-level provider settings (from conf.yml) always take
        # precedence over app-level defaults (from boxman.yml).
        self._update_sessions_with_runtime()

        # Tear down the containerlab lab first so its veths release any
        # shared bridges before we touch libvirt state.
        self.destroy_netlab()

        # Tear down docker-compose clusters (`docker compose down`: remove
        # containers + networks, keep named volumes). Best-effort, like
        # destroy's step 2b: a failure here must not abort the libvirt VM /
        # network / files / cache teardown that follows (deprovision is also
        # invoked from `provision --force`).
        try:
            self.deprovision_compose_clusters()
        except Exception as exc:
            self.logger.warning(
                f"deprovision_compose_clusters raised: {exc} — continuing")

        processes = [
            (f"{cluster_name}/{vm_name}", self._destroy_vm_and_disks,
             (cluster_name, cluster, vm_name, vm_info))
            for cluster_name, cluster in self._vm_clusters.items()
            for vm_name, vm_info in cluster['vms'].items()
        ]
        _results, vm_failures = self._run_parallel(
            processes, op_label='deprovision vm')

        net_failures = self.destroy_networks()

        if getattr(cli_args, 'cleanup', False):
            self.deprovision_files()

        if vm_failures or net_failures:
            # Resources survived the teardown: keep the project registered
            # so it stays visible to `boxman list` and a later deprovision
            # can finish the job instead of the leftovers becoming
            # cache-invisible.
            self.logger.warning(
                "deprovision left resources behind; keeping project "
                f"'{self.config['project']}' registered in the cache")
        else:
            self.unregister_from_cache()

        return

    def destroy_runtime(self, cli_args):
        """
        Destroy the Docker Compose runtime environment and remove
        the ``.boxman`` directory from the project directory.
        """
        from boxman.runtime.docker_compose import DockerComposeRuntime

        runtime = self.runtime_instance
        if not isinstance(runtime, DockerComposeRuntime):
            self.logger.warning(
                f"destroy-runtime is only supported for the docker-compose "
                f"runtime (current runtime: {runtime.name})")
            return

        auto_accept = getattr(cli_args, "auto_accept", False)
        plan = runtime.plan_destroy_runtime()

        if not plan["actions"]:
            self.logger.info("nothing to do")
            return

        # Display the plan
        print("\nThe following actions will be performed:\n")
        for i, action in enumerate(plan["actions"], 1):
            print(f"  {i}. {action}")

        if plan["commands"]:
            print("\nCommands to execute:\n")
            for cmd in plan["commands"]:
                print(f"  $ {cmd}")

        if plan["paths_to_delete"]:
            print("\nPaths to delete:\n")
            for p in plan["paths_to_delete"]:
                print(f"  {p}")

        print()

        if not auto_accept:
            try:
                answer = input("Proceed? [y/N] ").strip().lower()
            except EOFError:
                print("No input available, aborted.")
                return
            if answer not in ("y", "yes"):
                print("Aborted.")
                return

        boxman_dir = runtime.destroy_runtime()
        if boxman_dir and os.path.isdir(boxman_dir):
            self._force_rmtree(boxman_dir)
        else:
            self.logger.info("no .boxman directory to remove")

    @staticmethod
    def _force_rmtree(path: str) -> None:
        """
        Remove *path* and everything under it. Falls back to a throwaway
        ``docker run --rm alpine rm -rf`` when ``shutil.rmtree`` leaves
        root-owned leftovers (created by the libvirt container running
        as root). Safe to call for any absolute path — emits info/warning
        logs, never raises.
        """
        if not path or not os.path.isdir(path):
            log.info(f"{path} does not exist — nothing to remove")
            return

        abs_path = os.path.abspath(path)
        log.info(f"removing {abs_path}")
        shutil.rmtree(abs_path, ignore_errors=True)
        if not os.path.isdir(abs_path):
            log.info(f"removed {abs_path}")
            return

        log.info(
            f"{abs_path} still exists (root-owned leftovers), "
            f"removing via docker")
        result = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{abs_path}:/cleanup",
             "alpine", "sh", "-c", "rm -rf /cleanup/* /cleanup/.[!.]* || true"],
            check=False,
        )
        if result.returncode != 0:
            log.warning(
                f"docker alpine rm -rf exited with {result.returncode}")
        # The bind-mount dir itself can't be removed from inside the
        # container, but it should now be empty.
        shutil.rmtree(abs_path, ignore_errors=True)
        if os.path.isdir(abs_path):
            log.warning(f"{abs_path} could not be fully removed")
        else:
            log.info(f"removed {abs_path}")

    def destroy(self, cli_args):
        """
        Full-teardown command: deprovision VMs and networks, tear down
        the docker-compose runtime (if used), and ``rm -rf`` the
        workspace workdir. Optionally also removes template workdirs
        when ``--templates`` is passed. Prompts for confirmation unless
        ``--auto-accept`` is set.

        This is the inverse of ``boxman up`` — it aims to leave the
        machine in the state it was in before the project was first
        provisioned.
        """
        from boxman.runtime.docker_compose import DockerComposeRuntime

        auto_accept = getattr(cli_args, "auto_accept", False)
        wipe_templates = getattr(cli_args, "templates", False)

        config = self.config or {}
        workspace_path = (config.get('workspace') or {}).get('path', '')
        if workspace_path:
            workspace_path = os.path.abspath(
                os.path.expanduser(workspace_path))

        template_dirs: list = []
        if wipe_templates:
            for tpl in (config.get('templates') or {}).values():
                wd = tpl.get('workdir') or '~/boxman-templates'
                template_dirs.append(
                    os.path.abspath(os.path.expanduser(wd)))
            template_dirs = sorted(set(template_dirs))

        runtime = self.runtime_instance
        is_docker = isinstance(runtime, DockerComposeRuntime)
        runtime_plan = runtime.plan_destroy_runtime() if is_docker else None

        # --------- "nothing to do" short-circuit --------------------
        # Avoid prompting the user (and avoid spinning up the runtime
        # just to discover there's nothing to deprovision) when every
        # piece of state this command would touch is already gone.
        project_name = config.get('project')
        # BoxmanCache defers the read, so .projects is None until we ask.
        # Without this load, the "in_cache" check silently treats every
        # project as absent and the command reports "nothing to do" even
        # for a properly registered project — see the rocky9 repro.
        self.cache.read_projects_cache()
        in_cache = bool(
            project_name
            and project_name in (self.cache.projects or {})
        )
        ws_present = bool(workspace_path and os.path.exists(workspace_path))
        boxman_dir_present = bool(
            is_docker and runtime_plan
            and runtime_plan.get("boxman_dir")
            and os.path.isdir(runtime_plan["boxman_dir"])
        )
        container_present = bool(
            is_docker and runtime_plan
            and runtime_plan.get("container_running")
        )
        templates_present = any(
            os.path.exists(d) for d in template_dirs
        )
        # docker-compose clusters keep a generated docker-compose.yml in their
        # workdir until destroy_cluster removes it (only on a successful
        # teardown). Treat its presence as state to tear down so destroy stays
        # retryable after the cache entry was lost — the terms above are
        # otherwise cache-/workspace-/runtime-centric and miss dc state.
        compose_present = any(
            os.path.isfile(os.path.join(
                os.path.expanduser(cluster.get('workdir', '')),
                'docker-compose.yml'))
            for cluster in self._compose_clusters.values()
            if cluster.get('workdir')
        )

        if not (in_cache or ws_present or boxman_dir_present
                or container_present or templates_present or compose_present):
            self.logger.info(
                f"nothing to do — project '{project_name or '?'}' "
                f"is not registered, no workspace dir, no runtime "
                f"state on disk")
            return

        # --------- build the action plan for the user ---------------
        print("\nThe following actions will be performed:\n")
        step = 1
        print(f"  {step}. destroy every VM and network defined in "
              f"'{self.config_path}'")
        step += 1
        print(f"  {step}. remove generated provisioning files "
              f"(env.sh, ansible.cfg, inventory, ssh_config, SSH keys)")
        step += 1
        # Disclose docker-compose named-volume deletion explicitly: destroy runs
        # `docker compose down --volumes`, which permanently removes named
        # volumes (declarable via compose_extra) — not obvious from the steps
        # above, which only mention VMs/networks/files.
        for _dc_name in self._compose_clusters:
            print(f"  {step}. tear down docker-compose cluster '{_dc_name}' "
                  f"(docker compose down --volumes — removes its containers, "
                  f"networks AND named volumes)")
            step += 1
        if is_docker and runtime_plan and runtime_plan["actions"]:
            for action in runtime_plan["actions"]:
                print(f"  {step}. {action}")
                step += 1
        if workspace_path:
            print(f"  {step}. remove workspace workdir tree '{workspace_path}'")
            step += 1
        for tpl_dir in template_dirs:
            print(f"  {step}. remove template workdir '{tpl_dir}'")
            step += 1

        paths = []
        if is_docker and runtime_plan:
            paths.extend(runtime_plan.get("paths_to_delete", []))
        if workspace_path:
            paths.append(workspace_path)
        paths.extend(template_dirs)
        if paths:
            print("\nPaths to delete:\n")
            for p in paths:
                print(f"  {p}")

        print()
        if not auto_accept:
            try:
                answer = input("Proceed? [y/N] ").strip().lower()
            except EOFError:
                print("No input available, aborted.")
                return
            if answer not in ("y", "yes"):
                print("Aborted.")
                return

        # ------------- execute --------------------------------------
        # 1. Best-effort: start the runtime so we can run virsh to
        #    deprovision VMs. If it fails (port conflict, docker daemon
        #    unreachable, libvirtd unresponsive in a zombie mount
        #    namespace, …) we still want to tear down docker state and
        #    nuke the workspace — so we skip the VM-level step instead
        #    of aborting. A short ready_timeout keeps the failure path
        #    snappy: if the runtime is broken, we don't want to wait a
        #    full minute during destroy.
        runtime_up = True
        if is_docker:
            runtime.ready_timeout = min(
                getattr(runtime, "ready_timeout", 60), 10)
        try:
            runtime.ensure_ready()
        except Exception as exc:
            runtime_up = False
            self.logger.warning(
                f"runtime could not be started ({exc}) — "
                f"skipping VM-level deprovision")

        # 2. deprovision VMs + networks + provisioning files (only when
        #    the runtime and provider session are available)
        if runtime_up and self.provider is not None:
            cleanup_args = type("Args", (), {
                "cleanup": True,
                "docker_compose": getattr(cli_args, "docker_compose", False),
            })()
            try:
                self.deprovision(cleanup_args)
            except Exception as exc:
                self.logger.warning(f"deprovision raised: {exc} — continuing")

        # 2b. fully tear down docker-compose clusters — destroy goes beyond
        #     deprovision's `docker compose down` (keeps named volumes) to
        #     `down --volumes` and removes the generated compose file. Runs
        #     regardless of the libvirt-in-container runtime state: the
        #     compose provider shells out to the host docker directly.
        try:
            self.destroy_compose_clusters()
        except Exception as exc:
            self.logger.warning(
                f"destroy_compose_clusters raised: {exc} — continuing")

        # 3. tear down the docker runtime (reuses _force_rmtree for the
        #    .boxman dir, no double prompt)
        if is_docker:
            try:
                boxman_dir = runtime.destroy_runtime()
                if boxman_dir and os.path.isdir(boxman_dir):
                    self._force_rmtree(boxman_dir)
            except Exception as exc:
                self.logger.warning(f"destroy_runtime raised: {exc}")

        # 4. unregister the project from the boxman cache. We do this
        #    unconditionally (in addition to whatever deprovision did)
        #    so that stale cache entries left over from earlier failed
        #    runs don't block the next `up`.
        try:
            self.unregister_from_cache()
        except Exception as exc:
            self.logger.warning(f"unregister_from_cache raised: {exc}")

        # 5. nuke the workspace workdir
        if workspace_path:
            self._force_rmtree(workspace_path)

        # 6. nuke template workdirs (only when --templates was passed)
        for tpl_dir in template_dirs:
            self._force_rmtree(tpl_dir)

        self.logger.info("destroy complete")

    ### start snapshot functions ####
    def snapshot_list(self, cli_args):
        """
        List snapshots of the VMs and docker-compose clusters in the project.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        for full_vm_name, cluster_name, _vm_name, _workdir in (
                self._select_vm_targets(cli_args)):
            self.session_for_cluster(cluster_name).snapshot_list(full_vm_name)
        # docker-compose clusters (docker commit-backed, D3)
        for cname, cluster in self._select_dc_clusters(cli_args):
            snaps = self.session_for_cluster(cname).snapshot_list_cluster(cname, cluster)
            self.logger.info(f"cluster: {cname} (docker-compose)")
            if not snaps:
                self.logger.info("  (no snapshots)")
            for name in sorted(snaps, key=lambda k: snaps[k].get('created', '')):
                snap = snaps[name]
                self.logger.info(
                    f"  {name}  created={snap.get('created', '?')}  "
                    f"{snap.get('description', '')}".rstrip())
                for box, tag in (snap.get('boxes') or {}).items():
                    self.logger.info(f"      {box}: {tag}")

    def snapshot_log(self, cli_args):
        """
        Aggregated git-log-style view of snapshots across every VM.

        Each unique snapshot name becomes one row showing description,
        creation time, the list of VMs that have it, and a ``← current``
        marker if it's the current snapshot for any VM. Default ordering
        is newest-first by chain depth (with creation_time as tiebreaker);
        ``--reverse`` flips it. ``--no-graph`` suppresses the leftmost
        ``*``/``|`` column; ``--json`` emits machine-readable output
        matching the shape of ``boxman ps --json``.
        """
        from boxman.utils.snapshot_graph import render_graph

        prj_name = f'bprj__{self.config["project"]}__bprj'
        prj_prefix = f'{prj_name}_'

        # 1. Per-VM data.
        per_vm: dict[str, dict] = {}
        for full_vm_name, cluster_name, _vm_name, _workdir in (
                self._select_vm_targets(cli_args)):
            data = self.session_for_cluster(cluster_name).snapshot_log_data(full_vm_name)
            per_vm[full_vm_name] = data

        if not any(d.get('chain') for d in per_vm.values()):
            self.logger.info("no snapshots found")
            return

        # 2. Aggregate by snapshot name.
        aggregated: dict[str, dict] = {}
        for full_vm_name, data in per_vm.items():
            short_vm = full_vm_name[len(prj_prefix):] \
                if full_vm_name.startswith(prj_prefix) else full_vm_name
            current = data.get('current')
            for snap in data.get('chain', []):
                name = snap['name']
                entry = aggregated.setdefault(name, {
                    'name': name,
                    'description': snap.get('description', ''),
                    'creation_time': snap.get('creation_time'),
                    'parent': snap.get('parent'),
                    'depth': snap.get('depth', 0),
                    'vms': [],
                    'current_for': [],
                })
                entry['vms'].append(short_vm)
                # Take the max depth seen across VMs (handles partial-take
                # divergence where some VMs have a deeper chain).
                entry['depth'] = max(entry['depth'], snap.get('depth', 0))
                ct = snap.get('creation_time')
                if ct and (not entry.get('creation_time')
                           or ct > entry['creation_time']):
                    entry['creation_time'] = ct
                # Description and parent: first-write-wins; usually
                # consistent across VMs for a given snapshot name.
                if not entry.get('description'):
                    entry['description'] = snap.get('description', '')
                if not entry.get('parent'):
                    entry['parent'] = snap.get('parent')
                if current == name:
                    entry['current_for'].append(short_vm)

        # 3. Sort: newest-first by depth desc, then creation_time desc.
        rows = sorted(
            aggregated.values(),
            key=lambda r: (r['depth'], r.get('creation_time') or ''),
            reverse=True,
        )

        max_count = getattr(cli_args, 'max_count', None)
        if max_count is not None and max_count >= 0:
            rows = rows[:max_count]

        if getattr(cli_args, 'reverse', False):
            rows = list(reversed(rows))

        # 4. Render.
        if getattr(cli_args, 'as_json', False):
            payload = [
                {
                    'name': r['name'],
                    'description': r['description'],
                    'creation_time': r.get('creation_time'),
                    'parent': r.get('parent'),
                    'depth': r['depth'],
                    'vms': sorted(r['vms']),
                    'current_for': sorted(r['current_for']),
                }
                for r in rows
            ]
            print(json.dumps(payload, indent=2))
            return

        if getattr(cli_args, 'no_graph', False):
            entries: list[tuple[str, dict | None]] = [('', r) for r in rows]
        else:
            entries = render_graph(rows)

        # Column widths (only over real rows — transitions skip the columns).
        real = [r for _, r in entries if r is not None]
        name_w = max((len(r['name']) for r in real), default=4)
        time_w = max((len(r.get('creation_time') or '') for r in real),
                     default=10)

        for prefix, row in entries:
            if row is None:
                print(prefix)
                continue
            current_for = row['current_for']
            vms_total = len(row['vms'])
            cur_marker = ''
            if current_for:
                if len(current_for) == vms_total:
                    cur_marker = '  ← current'
                else:
                    cur_marker = f"  ← current ({len(current_for)}/{vms_total})"
            vm_list = ','.join(sorted(row['vms']))
            description = row.get('description') or ''
            ctime = row.get('creation_time') or '?'
            print(
                f"{prefix}{row['name']:<{name_w}}  "
                f"{ctime:<{time_w}}  "
                f"\"{description}\"  "
                f"[{vm_list}]{cur_marker}"
            )

    def _select_vm_targets(self, cli_args):
        """
        Resolve the VMs selected by the ``--cluster`` / ``--vms`` flags.

        Returns a list of ``(full_vm_name, cluster_name, vm_name, workdir)``
        tuples, filtered by:

        * ``--cluster <name>`` — restrict to a single cluster (raises
          :class:`ValueError` if the cluster is unknown);
        * ``--vms <csv>`` — restrict to specific VMs, each matched against
          either the bare VM name (``node01``) or the cluster-qualified short
          name (``cluster_1_node01``). The default ``'all'`` selects every VM.

        Both filters compose: ``--cluster cluster_2 --vms node01`` selects
        only ``cluster_2``'s ``node01``. With neither flag the result is every
        VM in every cluster — preserving the previous whole-project behaviour.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'

        cluster_filter = getattr(cli_args, 'cluster', None)
        vms_raw = getattr(cli_args, 'vms', None)
        if vms_raw is None:
            vms_raw = 'all'

        vm_filter: set | None = None
        if isinstance(vms_raw, (list, tuple)):
            vm_filter = {str(v).strip() for v in vms_raw if str(v).strip()}
        elif str(vms_raw).strip().lower() != 'all':
            vm_filter = {v.strip() for v in str(vms_raw).split(',') if v.strip()}
        if vm_filter is not None and not vm_filter:
            vm_filter = None

        clusters = self.config['clusters']
        if cluster_filter is not None and cluster_filter not in clusters:
            raise ValueError(
                f"cluster '{cluster_filter}' not found in config "
                f"(available: {', '.join(clusters) or '(none)'})"
            )

        targets = []
        for cluster_name, cluster in clusters.items():
            if cluster_filter is not None and cluster_name != cluster_filter:
                continue
            if self._is_compose_cluster(cluster_name):
                # docker-compose clusters carry ``boxes:``, not ``vms:`` —
                # they are selected via ``_select_dc_clusters`` instead.
                continue
            workdir = os.path.expanduser(cluster['workdir'])
            for vm_name in cluster.get('vms', {}):
                short_name = f"{cluster_name}_{vm_name}"
                if vm_filter is not None and not (
                    vm_name in vm_filter or short_name in vm_filter
                ):
                    continue
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                targets.append((full_vm_name, cluster_name, vm_name, workdir))
        return targets

    def snapshot_take(self, cli_args):
        """
        Take a snapshot of the selected VMs (parallel), then verify each one.
        docker-compose clusters are snapshotted per-cluster via ``docker
        commit`` (decision D3; named volumes are NOT captured).

        Honours ``--cluster`` / ``--vms`` so a single cluster (or VM) can be
        snapshotted independently in a multi-cluster project.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        # docker-compose clusters first (cluster-scoped, D3). Failures are
        # isolated per cluster so a dc cluster that isn't up can't stop the
        # VMs of a mixed project from being snapshotted.
        dc_done, dc_failed = self._for_each_dc_cluster(
            cli_args, 'take',
            lambda cname, cluster: self.session_for_cluster(cname).snapshot_take_cluster(
                cname, cluster, cli_args.snapshot_name,
                getattr(cli_args, 'snapshot_descr', '') or ''))

        vm_targets = [
            (full_vm_name, workdir)
            for full_vm_name, _cluster_name, _vm_name, workdir
            in self._select_vm_targets(cli_args)
        ]
        if not vm_targets:
            if not dc_done:
                self.logger.warning(
                    "no VMs or containers matched the given "
                    "--cluster/--vms selection")
            self._exit_if_dc_failed(dc_failed, 'take')
            return

        compress_memory = getattr(cli_args, 'compress_memory', False)
        compress_level = getattr(cli_args, 'memory_compress_level', 3)
        force = getattr(cli_args, 'force', False)

        def _take(full_vm_name, vm_dir, snapshot_name, description):
            self.session_for_vm(full_vm_name).snapshot_take(
                vm_name=full_vm_name,
                vm_dir=vm_dir,
                snapshot_name=snapshot_name,
                description=description,
                compress_memory=compress_memory,
                compress_level=compress_level,
                force=force)

        processes = [
            (full_vm_name, _take,
             (full_vm_name, vm_dir,
              cli_args.snapshot_name, cli_args.snapshot_descr))
            for full_vm_name, vm_dir in vm_targets
        ]
        self._run_parallel(processes, op_label='snapshot take')

        # Verify every snapshot in the main process after all takes complete.
        self.logger.info("verifying snapshots after take...")
        all_ok = True
        for full_vm_name, _ in vm_targets:
            valid, errors = self.session_for_vm(full_vm_name).validate_snapshot(
                full_vm_name, cli_args.snapshot_name)
            if valid:
                self.logger.info(f"snapshot ok: {full_vm_name} / '{cli_args.snapshot_name}'")
            else:
                all_ok = False
                for err in errors:
                    self.logger.error(
                        f"snapshot invalid: {full_vm_name} / '{cli_args.snapshot_name}': {err}")

        if all_ok:
            self.logger.info("all snapshots verified successfully")
        else:
            raise SnapshotError(
                "one or more snapshots failed verification — check errors above")
        self._exit_if_dc_failed(dc_failed, 'take')

    def snapshot_restore(self, cli_args):
        """
        Restore the state of the selected VMs from a snapshot (parallel).

        Honours ``--cluster`` / ``--vms`` so a single cluster (or VM) can be
        rolled back independently in a multi-cluster project.

        Workflow
        --------
        1. Resolve snapshot names in the main process (use latest if not specified).
        2. Pre-validate ALL resolved snapshots; abort if any are invalid.
        3. Run parallel restores, tracking per-VM success via a Queue.
        4. Retry failed VMs in subsequent rounds until ALL succeed.

        Both provider types are resolved and validated **before either is
        mutated**: a docker-compose restore is a destructive
        ``up --force-recreate``, so running it ahead of the VM pre-validation
        would let an invalid VM snapshot abort the command with the containers
        already recreated — a partial restore, despite the pre-validation
        contract above.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        # ── 0. Resolve + validate docker-compose clusters (cluster-scoped,
        #      D3). Nothing is mutated here — the recreate happens in step 3
        #      once the VM snapshots have been validated too.
        dc_plan = []      # [(cluster_name, cluster_cfg, resolved_snapshot)]
        dc_selected = False
        dc_abort = False
        for cname, cluster in self._select_dc_clusters(cli_args):
            dc_selected = True
            session = self.session_for_cluster(cname)
            snap = session.snapshot_resolve_cluster(
                cname, cluster, cli_args.snapshot_name)
            if snap is None:
                self.logger.error(f"[{cname}] no snapshots to restore")
                continue
            if not cli_args.snapshot_name:
                self.logger.info(f"[{cname}] resolved latest snapshot: '{snap}'")
            valid, errors = session.validate_snapshot_cluster(cname, cluster, snap)
            if valid:
                self.logger.info(f"snapshot ok: [{cname}] / '{snap}'")
                dc_plan.append((cname, cluster, snap))
            else:
                dc_abort = True
                for err in errors:
                    self.logger.error(
                        f"snapshot invalid: [{cname}] / '{snap}': {err}")

        selected = self._select_vm_targets(cli_args)
        if not selected and not dc_plan:
            if not dc_selected:
                self.logger.warning(
                    "no VMs or containers matched the given "
                    "--cluster/--vms selection")
            if dc_abort:
                self.logger.error(
                    "aborting restore — one or more snapshots have errors "
                    "(see above)")
            return

        if not selected:
            # containers only: nothing to pre-validate on the libvirt side
            if dc_abort:
                self.logger.error(
                    "aborting restore — one or more snapshots have errors "
                    "(see above)")
                return
            self._exit_if_dc_failed(self._restore_dc_plan(dc_plan), 'restore')
            return

        # ── 1. Resolve snapshot names ────────────────────────────────────────
        vm_targets = []  # list of (full_vm_name, resolved_snapshot_name)
        for full_vm_name, _cluster_name, _vm_name, _workdir in selected:
            snap_name = cli_args.snapshot_name
            if not snap_name:
                snap_name = self.session_for_vm(full_vm_name).get_latest_snapshot(full_vm_name)
                if snap_name is None:
                    raise SnapshotError(
                        f"no snapshot found for {full_vm_name}, aborting restore")
                self.logger.info(
                    f"resolved latest snapshot for {full_vm_name}: '{snap_name}'")
            vm_targets.append((full_vm_name, snap_name))

        # ── 2. Pre-validate all snapshots ────────────────────────────────────
        self.logger.info("pre-validating snapshots before restore...")
        abort = dc_abort   # a bad container snapshot aborts the whole restore
        for full_vm_name, snap_name in vm_targets:
            valid, errors = self.session_for_vm(full_vm_name).validate_snapshot(full_vm_name, snap_name)
            if valid:
                self.logger.info(f"snapshot ok: {full_vm_name} / '{snap_name}'")
            else:
                abort = True
                for err in errors:
                    self.logger.error(
                        f"snapshot invalid: {full_vm_name} / '{snap_name}': {err}")

        if abort:
            raise SnapshotError(
                "aborting restore — one or more snapshots have errors (see above)")

        # ── 3. Everything validated: mutate. Containers first (fast, coarse),
        #      then the parallel VM restores below. A dc failure is reported
        #      now but only exits after the VMs have had their turn.
        dc_failed = self._restore_dc_plan(dc_plan)

        # ── 3 & 4. Parallel restore with retry until all succeed ─────────────
        def _restore(full_vm_name, snapshot_name):
            return bool(self.session_for_vm(full_vm_name).snapshot_restore(
                full_vm_name, snapshot_name))

        pending = list(vm_targets)
        max_rounds = 20

        for round_num in range(1, max_rounds + 1):
            self.logger.info(
                f"restore round {round_num}: {len(pending)} VM(s) to restore")

            # _run_parallel reports raised/killed workers as failures too, so
            # a dying child can no longer look like a successful restore.
            results, failures = self._run_parallel(
                [(vm, _restore, (vm, snap)) for vm, snap in pending],
                op_label='snapshot restore')

            failed = []
            for vm, snap in pending:
                if vm not in failures and results.get(vm):
                    self.logger.info(f"restored: {vm} to '{snap}'")
                else:
                    self.logger.warning(f"failed: {vm} to '{snap}', will retry")
                    failed.append((vm, snap))

            if not failed:
                self.logger.info("all VMs restored successfully")
                self._exit_if_dc_failed(dc_failed, 'restore')
                return

            pending = failed
            if round_num < max_rounds:
                self.logger.info(f"{len(failed)} VM(s) failed, retrying in 3s...")
                time.sleep(3)

        self.logger.error(
            f"restore gave up after {max_rounds} rounds. "
            f"still failing: {[vm for vm, _ in pending]}")
        self._exit_if_dc_failed(dc_failed, 'restore')


    def snapshot_delete(self, cli_args):
        """
        Delete a snapshot of the selected VMs.

        Honours ``--cluster`` / ``--vms`` so a snapshot can be removed from a
        single cluster (or VM) in a multi-cluster project.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        if not cli_args.snapshot_name:
            self.logger.error("error: Snapshot name is required")
            return

        # docker-compose clusters (cluster-scoped, D3), failures isolated so
        # one cluster can't strand the others or the VMs.
        dc_done, dc_failed = self._for_each_dc_cluster(
            cli_args, 'delete',
            lambda cname, cluster: self.session_for_cluster(cname).snapshot_delete_cluster(
                cname, cluster, cli_args.snapshot_name))

        targets = self._select_vm_targets(cli_args)
        if not targets:
            if not dc_done:
                self.logger.warning(
                    "no VMs or containers matched the given "
                    "--cluster/--vms selection")
            self._exit_if_dc_failed(dc_failed, 'delete')
            return

        for full_vm_name, _cluster_name, _vm_name, _workdir in targets:
            self.session_for_cluster(_cluster_name).snapshot_delete(full_vm_name, cli_args.snapshot_name)
            self.logger.info(f"Snapshot {cli_args.snapshot_name} deleted for VM {full_vm_name}")
        self._exit_if_dc_failed(dc_failed, 'delete')

    @staticmethod
    def _collapse_one_vm(provider_config, full_vm_name, workdir, vm_info,
                         target, no_shutdown, dry_run):
        """Worker target for parallel snapshot collapse — must be picklable."""
        from boxman.providers.libvirt.snapshot import SnapshotManager
        from boxman.providers.libvirt.storage import StorageManager

        snapshot_mgr = SnapshotManager(provider_config)
        storage = StorageManager(provider_config)

        if dry_run:
            snapshot_mgr.collapse_to(full_vm_name, target, dry_run=True)
            return

        was_running = storage.is_running(full_vm_name)
        if was_running:
            if no_shutdown:
                log.error(
                    f"collapse: vm {full_vm_name} is running and "
                    f"--no-shutdown was passed; skipping")
                return
            if not storage.shutdown_and_wait(full_vm_name):
                log.error(
                    f"collapse: shutdown failed for {full_vm_name}, skipping")
                return

        ok = snapshot_mgr.collapse_to(full_vm_name, target, dry_run=False)

        if was_running:
            if not storage.start(full_vm_name):
                log.error(f"collapse: failed to restart {full_vm_name}")

        if ok:
            log.info(
                f"collapse ok: {full_vm_name} — kept '{target}' and older")
        else:
            log.error(f"collapse failed: {full_vm_name}")

    def snapshot_collapse(self, cli_args):
        """
        Collapse snapshots newer than ``--to`` into the live head per VM.

        Auto-shuts down running VMs by default (qemu-img rebase is
        offline-only). Use ``--no-shutdown`` to skip running VMs instead.
        Snapshots older than the target remain revertable; everything
        between target and head is merged into head and dropped.
        """
        target = cli_args.target
        dry_run = getattr(cli_args, 'dry_run', False)
        no_shutdown = getattr(cli_args, 'no_shutdown', False)
        yes = getattr(cli_args, 'yes', False)

        targets = []
        for full_vm_name, cluster_name, vm_name, workdir in (
                self._select_vm_targets(cli_args)):
            vm_info = self.config['clusters'][cluster_name]['vms'][vm_name] or {}
            targets.append((full_vm_name, workdir, vm_info))

        if not yes and not dry_run:
            self.logger.warning(
                f"about to collapse all snapshots newer than '{target}' "
                f"on {len(targets)} vm(s). This is irreversible — run "
                f"with --dry-run first if unsure, or pass --yes to skip "
                f"this prompt.")
            try:
                confirm = input("continue? [y/N]: ").strip().lower()
            except EOFError:
                confirm = ''
            if confirm != 'y':
                self.logger.info("aborted")
                return

        # Phase 1 (#49): snapshot collapse stays on the default session —
        # it manipulates qcow2 chains via libvirt-specific managers.
        provider_config = self.provider.provider_config
        processes = [
            (full_vm_name, BoxmanManager._collapse_one_vm,
             (provider_config, full_vm_name, workdir, vm_info,
              target, no_shutdown, dry_run))
            for full_vm_name, workdir, vm_info in targets
        ]
        self._run_parallel(processes, op_label='snapshot collapse')
    ### end snapshot functions ####

    ### start storage functions ####
    @staticmethod
    def _format_bytes(num: int | None) -> str:
        if num is None:
            return "-"
        for unit in ("B", "K", "M", "G", "T"):
            if abs(num) < 1024.0:
                return f"{num:.1f}{unit}"
            num /= 1024.0
        return f"{num:.1f}P"

    def storage_df(self, cli_args):
        """
        Per-VM disk usage table: virtual size, allocated, chain depth,
        snapshots, snapshot memory (.raw) total, estimated reclaim.
        """
        from boxman.providers.libvirt.storage import vm_disk_paths

        storage = self.provider.storage  # Phase 1 (#49): storage_df stays on the default session until Phase 3
        rows = []
        snap_mem_total_per_vm: dict[str, int] = {}
        for full_vm_name, cluster_name, vm_name, workdir in (
                self._select_vm_targets(cli_args)):
            vm_info = self.config['clusters'][cluster_name]['vms'][vm_name] or {}
            disks = vm_disk_paths(workdir, full_vm_name, vm_info)
            snap_count = storage.count_snapshots(full_vm_name)
            mem_files = storage.snapshot_memory_files(workdir, full_vm_name)
            mem_total = sum(os.path.getsize(p) for p in mem_files if os.path.isfile(p))
            snap_mem_total_per_vm[full_vm_name] = mem_total
            for disk_path in disks:
                if not os.path.isfile(disk_path):
                    continue
                info = storage.disk_info(disk_path)
                chain = storage.disk_chain(disk_path)
                measure = storage.disk_measure(disk_path)
                disk_size = info.get('actual-size')
                virtual = info.get('virtual-size')
                required = measure.get('required')
                reclaim_est = (disk_size - required
                               if disk_size is not None and required is not None
                               else None)
                rows.append({
                    'vm': full_vm_name,
                    'disk': os.path.basename(disk_path),
                    'virtual': virtual,
                    'allocated': disk_size,
                    'chain': len(chain),
                    'snapshots': snap_count,
                    'snap_mem': mem_total,
                    'reclaim_est': reclaim_est,
                })

        # render
        header = (f"{'VM':<48}{'DISK':<28}{'VIRTUAL':>10}{'ALLOC':>10}"
                  f"{'CHAIN':>6}{'SNAPS':>7}{'SNAPMEM':>10}{'RECLAIM~':>10}")
        self.logger.info(header)
        self.logger.info("-" * len(header))
        for row in rows:
            line = (
                f"{row['vm']:<48}"
                f"{row['disk']:<28}"
                f"{self._format_bytes(row['virtual']):>10}"
                f"{self._format_bytes(row['allocated']):>10}"
                f"{row['chain']:>6}"
                f"{row['snapshots']:>7}"
                f"{self._format_bytes(row['snap_mem']):>10}"
                f"{self._format_bytes(row['reclaim_est']):>10}"
            )
            self.logger.info(line)
        if not rows:
            self.logger.info("(no qcow2 disks found on host)")

    def storage_trim(self, cli_args):
        """
        Run ``virsh domfstrim`` (qemu-guest-agent) on every running VM.
        Warns when a VM's disks lack ``discard='unmap'`` — fstrim will succeed
        but nothing will be returned to the host.
        """
        storage = self.provider.storage  # Phase 1 (#49): storage_trim stays on the default session until Phase 3
        for full_vm_name, _c, _v, _workdir in self._select_vm_targets(cli_args):
            if not storage.is_running(full_vm_name):
                self.logger.warning(
                    f"skip trim: vm {full_vm_name} is not running")
                continue
            if not storage.has_discard_unmap(full_vm_name):
                self.logger.warning(
                    f"vm {full_vm_name}: no discard='unmap' on disks — fstrim "
                    f"will not reclaim host space. fix: edit the domain XML "
                    f"(`virsh edit {full_vm_name}`) or recreate via "
                    f"`boxman destroy && boxman up`.")
            if getattr(cli_args, 'dry_run', False):
                self.logger.info(f"[dry-run] would fstrim: {full_vm_name}")
                continue
            storage.fstrim_guest(full_vm_name)

    @staticmethod
    def _compact_one_vm(provider_config, full_vm_name, workdir, vm_info,
                        method, drop_snapshots, no_shutdown, dry_run):
        """Worker target for parallel compact — must be picklable."""
        from boxman.providers.libvirt.storage import StorageManager, vm_disk_paths

        storage = StorageManager(provider_config)
        disks = [p for p in vm_disk_paths(workdir, full_vm_name, vm_info)
                 if os.path.isfile(p)]
        if not disks:
            log.info(f"compact: no disks found for {full_vm_name}, skipping")
            return

        was_running = storage.is_running(full_vm_name)
        if was_running:
            if no_shutdown:
                log.error(
                    f"compact: vm {full_vm_name} is running and --no-shutdown "
                    f"was passed; skipping")
                return
            if dry_run:
                log.info(f"[dry-run] would shutdown {full_vm_name}")
            else:
                if not storage.shutdown_and_wait(full_vm_name):
                    log.error(f"compact: shutdown failed for {full_vm_name}, skipping")
                    return

        has_snapshots = storage.count_snapshots(full_vm_name) > 0
        for disk_path in disks:
            before = storage.disk_info(disk_path).get('actual-size', 0)
            if dry_run:
                measure = storage.disk_measure(disk_path)
                est = measure.get('required')
                log.info(
                    f"[dry-run] {full_vm_name}: would compact {os.path.basename(disk_path)} "
                    f"method={method} allocated={before} estimated_after={est}")
                continue
            ok = storage.compact_disk(
                disk_path,
                method=method,
                has_snapshots=has_snapshots,
                drop_snapshots=drop_snapshots)
            after = storage.disk_info(disk_path).get('actual-size', 0)
            if ok:
                log.info(
                    f"compact ok: {full_vm_name}/{os.path.basename(disk_path)} "
                    f"{before} -> {after}")
            else:
                log.error(
                    f"compact failed: {full_vm_name}/{os.path.basename(disk_path)}")

        if was_running and not no_shutdown and not dry_run:
            if not storage.start(full_vm_name):
                log.error(f"compact: failed to restart {full_vm_name}")

    def storage_compact(self, cli_args):
        """
        Compact every VM's qcow2 file(s). Auto-shuts down running VMs by
        default (use ``--no-shutdown`` to skip running VMs instead). Refuses
        chain-flattening methods when snapshots exist unless
        ``--drop-snapshots`` is passed.
        """
        targets = []
        for full_vm_name, cluster_name, vm_name, workdir in (
                self._select_vm_targets(cli_args)):
            vm_info = self.config['clusters'][cluster_name]['vms'][vm_name] or {}
            targets.append((full_vm_name, workdir, vm_info))
        method = getattr(cli_args, 'method', 'auto')
        drop_snapshots = getattr(cli_args, 'drop_snapshots', False)
        no_shutdown = getattr(cli_args, 'no_shutdown', False)
        dry_run = getattr(cli_args, 'dry_run', False)

        provider_config = self.provider.provider_config  # Phase 1 (#49): storage_compact stays on the default session until Phase 3
        processes = [
            (full_vm_name, BoxmanManager._compact_one_vm,
             (provider_config, full_vm_name, workdir, vm_info,
              method, drop_snapshots, no_shutdown, dry_run))
            for full_vm_name, workdir, vm_info in targets
        ]
        self._run_parallel(processes, op_label='storage compact')

    def storage_optimize(self, cli_args):
        """
        Trim every running VM (via guest agent) and compact every VM's
        qcow2 file(s). Auto-shutdown semantics from ``storage_compact`` apply.
        """
        if not getattr(cli_args, 'skip_trim', False):
            self.logger.info("storage optimize: phase 1 — trim (guest fstrim)")
            self.storage_trim(cli_args)
        else:
            self.logger.info("storage optimize: skipping trim phase (--skip-trim)")

        if not getattr(cli_args, 'skip_compact', False):
            self.logger.info("storage optimize: phase 2 — compact (host qcow2)")
            self.storage_compact(cli_args)
        else:
            self.logger.info("storage optimize: skipping compact phase (--skip-compact)")

    def storage_compress_snapshots(self, cli_args):
        """
        zstd-compress every snapshot's memory ``.raw`` file (or decompress
        with ``--decompress``). Use this retroactively on snapshots that
        were taken without ``--compress-memory``.
        """
        decompress = getattr(cli_args, 'decompress', False)
        level = getattr(cli_args, 'level', 3)
        action = "decompress" if decompress else "compress"
        for full_vm_name, _c, _v, _workdir in self._select_vm_targets(cli_args):
            self.logger.info(f"storage {action}-snapshots: {full_vm_name}")
            processed, total = self.session_for_vm(full_vm_name).compress_snapshots_memory(
                full_vm_name, level=level, decompress=decompress)
            self.logger.info(
                f"  {action}ed {processed}/{total} snapshot memory file(s) "
                f"for {full_vm_name}")
    ### end storage functions ####

    ### start control vm functions ####
    def _control_vm_targets(self, cli_args):
        """
        ``(full_vm_name, workdir)`` pairs for the VMs selected by
        ``--cluster`` / ``--vms`` (every VM when neither flag is given).
        """
        return [
            (full_vm_name, workdir)
            for full_vm_name, _c, _v, workdir
            in self._select_vm_targets(cli_args)
        ]

    def suspend_vm(self, cli_args):
        """
        Suspend the machines: libvirt VMs → virsh suspend; docker-compose
        containers → ``docker compose pause``.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        for vm_name, _ in self._control_vm_targets(cli_args):
            self.session_for_vm(vm_name).suspend_vm(vm_name)
            self.logger.info(f"vm {vm_name} suspended")
        for cluster_name, cluster in self._select_dc_clusters(cli_args):
            self._dc_session(cluster_name).pause_cluster(cluster_name, cluster)

    def resume_vm(self, cli_args):
        """
        Resume the machines: libvirt VMs → virsh resume; docker-compose
        containers → ``docker compose unpause``.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        for vm_name, _ in self._control_vm_targets(cli_args):
            self.session_for_vm(vm_name).resume_vm(vm_name)
            self.logger.info(f"VM {vm_name} resumed")
        for cluster_name, cluster in self._select_dc_clusters(cli_args):
            self._dc_session(cluster_name).unpause_cluster(cluster_name, cluster)

    def save_vm(self, cli_args):
        """
        Save the state of libvirt VMs to a file. Not supported for
        docker-compose containers (no save-to-file state) — an explanatory
        message is logged, no traceback.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        for vm_name, workdir in self._control_vm_targets(cli_args):
            self.session_for_vm(vm_name).save_vm(vm_name, workdir)
        for cluster_name, _cluster in self._select_dc_clusters(cli_args):
            self.logger.warning(
                f"'control save' is not supported for docker-compose cluster "
                f"'{cluster_name}' — containers have no save-to-file state; use "
                f"snapshots (Phase 7) or 'destroy'. Skipping."
            )

    def start_vm(self, cli_args):
        """
        Start the machines: libvirt VMs (optionally --restore); docker-compose
        containers → ``docker compose start``.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        for vm_name, workdir in self._control_vm_targets(cli_args):
            if cli_args.restore:
                self.session_for_vm(vm_name).restore_vm(vm_name, workdir)
            else:
                self.session_for_vm(vm_name).start_vm(vm_name)
        for cluster_name, cluster in self._select_dc_clusters(cli_args):
            if getattr(cli_args, "restore", False):
                self.logger.info(
                    f"[{cluster_name}] --restore has no docker-compose "
                    f"equivalent; starting containers")
            self._dc_session(cluster_name).start_cluster(cluster_name, cluster)
    ### end control vm functions ####

    ### task runner functions ####

    def run_task(self, cli_args):
        """
        Run a named task or ad-hoc command with the workspace environment.
        """
        runner = TaskRunner(
            config=self.config,
            cluster_name=getattr(cli_args, "cluster", None),
        )

        if getattr(cli_args, "list_tasks", False):
            tasks = runner.list_tasks()
            if not tasks:
                print("No tasks defined in conf.yml")
                return
            max_name = max(len(t["name"]) for t in tasks)
            for task in tasks:
                desc = task["description"]
                print(f"  {task['name']:<{max_name}}  {desc}")
            return

        extra_args = getattr(cli_args, "extra_args", None) or []

        if getattr(cli_args, "cmd", None):
            exit_code = runner.run_command(
                cli_args.cmd,
                extra_args,
                ansible_flags=getattr(cli_args, "ansible_flags", None),
            )
        else:
            task_name = cli_args.task_name
            remaining = getattr(cli_args, "remaining_args", [])

            # Parse dynamic task flags from remaining CLI args based on
            # {placeholder} markers in the task command.
            task_flags = {}
            if task_name and task_name in runner.tasks:
                task_cmd = runner.tasks[task_name].get("command", "")
                placeholders = TaskRunner.extract_placeholders(task_cmd)

                if placeholders and remaining:
                    placeholder_set = set(placeholders)
                    # Python's argparse._parse_optional() returns None (positional)
                    # for argument strings that contain a space, so a flag value
                    # like '--limit node01' (a single bash-quoted arg) lands in
                    # extra_args instead of remaining.  We detect this by checking
                    # whether the next element in remaining is itself a recognised
                    # placeholder flag; if so, the current flag's value was
                    # consumed by argparse and is at the front of extra_args.
                    extra_args = list(extra_args)  # mutable copy; pops are visible at runner.run()
                    i = 0
                    while i < len(remaining):
                        arg = remaining[i]
                        if not arg.startswith("--"):
                            log.error(f"unrecognized argument: {arg}")
                            import sys
                            sys.exit(1)

                        name = arg[2:].replace("-", "_")
                        if name not in placeholder_set:
                            log.error(f"unrecognized argument: {arg}")
                            import sys
                            sys.exit(1)

                        # Determine the value for this flag.  Normal case:
                        # remaining[i + 1] is the value.  Exception: if that
                        # next arg is itself a recognised placeholder flag, the
                        # value for this flag was misclassified as a positional
                        # by argparse and sits at the front of extra_args.
                        value = None
                        if i + 1 < len(remaining):
                            next_arg = remaining[i + 1]
                            if next_arg.startswith("--"):
                                next_name = next_arg[2:].replace("-", "_")
                                if next_name not in placeholder_set:
                                    # next_arg is a value that starts with --
                                    value = next_arg
                                    i += 2
                                # else: next_arg is another flag → fall through
                            else:
                                value = next_arg
                                i += 2

                        if value is None:
                            # Value not in remaining; consume from extra_args.
                            if not extra_args:
                                log.error(f"argument {arg}: expected a value")
                                import sys
                                sys.exit(1)
                            value = extra_args.pop(0)
                            i += 1

                        task_flags[name] = value
                elif remaining:
                    log.error(
                        f"unrecognized arguments: {' '.join(remaining)}. "
                        f"Task '{task_name}' has no {{placeholder}} markers "
                        f"in its command."
                    )
                    import sys
                    sys.exit(1)

            exit_code = runner.run(task_name, extra_args, task_flags=task_flags)

        if exit_code != 0:
            import sys
            sys.exit(exit_code)

    def _get_vm_list(self) -> list[tuple[str, str, str]]:
        """
        Return the ordered list of VMs from the config.

        Returns:
            List of (cluster_name, vm_name, full_virsh_name) tuples.
            The list index is the boxman VM id (0-based).
        """
        project = self.config.get("project", "")
        prj_prefix = f"bprj__{project}__bprj_"
        vms = []
        for cluster_name, cluster in self.config.get("clusters", {}).items():
            for vm_name in cluster.get("vms", {}).keys():
                full_name = f"{prj_prefix}{cluster_name}_{vm_name}"
                vms.append((cluster_name, vm_name, full_name))
        return vms

    def resolve_vm_name(self, identifier: str) -> str:
        """
        Resolve a VM identifier to the short name used in the workspace
        (``{cluster}_{vm}``).

        The identifier can be:
        - A numeric boxman id (from ``boxman ps``)
        - A VM name (returned as-is)

        Raises:
            ValueError: If the numeric id is out of range.
        """
        if identifier.isdigit():
            vm_list = self._get_vm_list()
            idx = int(identifier)
            if idx < 0 or idx >= len(vm_list):
                raise ValueError(
                    f"VM id {idx} out of range (0-{len(vm_list) - 1})"
                )
            cluster_name, vm_name, _ = vm_list[idx]
            return f"{cluster_name}_{vm_name}"
        return identifier

    def show_conf(self, cli_args, merged_provider=None):
        """
        Display the effective merged configuration.

        Shows the merged provider config and the rendered project config.
        With ``--json``, outputs a single JSON object.
        """
        as_json = getattr(cli_args, 'json', False)

        # Read the rendered config file
        rendered_config = None
        if self.config_path:
            config_dir = os.path.dirname(os.path.abspath(self.config_path))
            config_basename = os.path.splitext(os.path.basename(self.config_path))[0]
            rendered_path = os.path.join(config_dir, f"{config_basename}.rendered.yml")
            if os.path.isfile(rendered_path):
                with open(rendered_path) as fobj:
                    rendered_config = fobj.read()

        if as_json:
            output = {
                "provider": merged_provider or {},
                "rendered_config": yaml.safe_load(rendered_config) if rendered_config else None,
            }
            print(json.dumps(output, indent=2, default=str))
            return

        # Plain text output
        print("Provider config")
        print("───────────────")
        if merged_provider:
            for key, value in merged_provider.items():
                print(f"  {key}: {value}")
        else:
            print("  (none)")

        print()
        print("Rendered config")
        print("───────────────")
        if rendered_config:
            print(rendered_config)
        else:
            print("  (conf.rendered.yml not found — run 'boxman provision' first)")

    def ps(self, cli_args):
        """
        Display the state of all project VMs in a table.

        With ``-p``, two extra columns are added showing the provider-specific
        virsh Id and virsh Name for each VM.
        """
        provider_info = getattr(cli_args, 'provider_info', False)
        as_json = getattr(cli_args, 'json', False)

        vm_list = self._get_vm_list()
        dc_clusters = self._compose_clusters

        if not vm_list and not dc_clusters:
            if as_json:
                print(json.dumps([], indent=2))
            else:
                print("No VMs or containers defined in configuration")
            return

        # Query virsh for VM states — only when the project has libvirt
        # clusters (a dc-only project has no libvirt to talk to).
        vm_info: dict[str, tuple[str, str]] = {}
        if vm_list and self._has_libvirt_clusters():
            result = self._virsh().execute("list", "--all", hide=True, warn=True)
            if result.ok:
                for line in result.stdout.strip().splitlines():
                    line = line.strip()
                    if not line or line.startswith("---") or line.startswith("Id"):
                        continue
                    parts = line.split(None, 2)
                    if len(parts) >= 3:
                        virsh_id, virsh_name, state = parts[0], parts[1], parts[2].strip()
                        vm_info[virsh_name] = (virsh_id, state)

        # Build records: libvirt VMs first (numeric ids), then dc containers.
        records = []
        for idx, (cluster_name, vm_name, full_name) in enumerate(vm_list):
            virsh_id, state = vm_info.get(full_name, ("-", "not created"))
            rec = {"id": idx, "cluster": cluster_name, "vm": vm_name,
                   "provider": self.provider_type_for_cluster(cluster_name),
                   "state": state}
            if provider_info:
                rec["virsh_id"] = virsh_id
                rec["virsh_name"] = full_name
            records.append(rec)

        for cluster_name, cluster in dc_clusters.items():
            try:
                status = {
                    r["service"]: r for r in
                    self._dc_session(cluster_name).container_status(
                        cluster_name, cluster)
                }
            except Exception as exc:  # a status probe must never break `ps`
                self.logger.warning(
                    f"could not query containers for '{cluster_name}': {exc}")
                status = {}
            for box_name in (cluster.get("boxes") or {}):
                row = status.get(box_name)
                state = "not created"
                if row:
                    state = row["state"] + (
                        f" ({row['health']})" if row.get("health") else "")
                rec = {"id": "-", "cluster": cluster_name, "vm": box_name,
                       "provider": "docker-compose", "state": state}
                if provider_info:
                    rec["virsh_id"] = "-"
                    rec["virsh_name"] = "-"
                records.append(rec)

        if as_json:
            print(json.dumps(records, indent=2))
            return

        if not records:
            print("No VMs or containers defined in configuration")
            return

        # Print table
        if provider_info:
            headers = ("Id", "Cluster", "Name", "Provider", "State",
                       "Virsh Id", "Virsh Name")
            rows = [(str(r["id"]), r["cluster"], r["vm"], r["provider"],
                     r["state"], r["virsh_id"], r["virsh_name"]) for r in records]
        else:
            headers = ("Id", "Cluster", "Name", "Provider", "State")
            rows = [(str(r["id"]), r["cluster"], r["vm"], r["provider"],
                     r["state"]) for r in records]

        col_count = len(headers)
        widths = [
            max(len(headers[i]), *(len(row[i]) for row in rows))
            for i in range(col_count)
        ]
        print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
        print("  ".join("-" * w for w in widths))
        for row in rows:
            print("  ".join(val.ljust(w) for val, w in zip(row, widths)))

    def ssh_session(self, cli_args):
        """
        Open an interactive SSH session to a VM.
        """
        vm_name = getattr(cli_args, "vm_name", None)

        # Resolve numeric id to VM name
        if vm_name:
            vm_name = self.resolve_vm_name(vm_name)

        runner = TaskRunner(
            config=self.config,
            cluster_name=getattr(cli_args, "cluster", None),
        )

        exit_code = runner.ssh_to_host(vm_name)

        if exit_code != 0:
            import sys
            sys.exit(exit_code)

    def _resolve_container_target(self, target: str) -> tuple[str, str]:
        """Resolve a ``boxman exec`` target to ``(cluster, box)``.

        ``<cluster>.<box>`` is split on the last dot; a bare ``<box>`` is
        allowed when exactly one docker-compose cluster defines it. Raises
        ``ConfigError`` for an unknown target or one that names a libvirt VM
        (which should use ``boxman ssh``).
        """
        dc_clusters = self._compose_clusters
        if '.' in target:
            cluster, box = target.rsplit('.', 1)
            if cluster not in (self.config.get('clusters') or {}):
                raise ConfigError(f"no cluster '{cluster}' in this project.")
            if not self._is_compose_cluster(cluster):
                raise ConfigError(
                    f"cluster '{cluster}' is a libvirt cluster — use "
                    f"'boxman ssh' for VMs, not 'boxman exec'."
                )
            return cluster, box
        matches = [
            cn for cn, cl in dc_clusters.items()
            if target in (cl.get('boxes') or {})
        ]
        if len(matches) == 1:
            return matches[0], target
        if not matches:
            raise ConfigError(
                f"no docker-compose container '{target}' — give the target as "
                f"'<cluster>.<box>' (dc clusters: "
                f"{', '.join(dc_clusters) or 'none'})."
            )
        raise ConfigError(
            f"container '{target}' is ambiguous across clusters "
            f"{', '.join(matches)} — use '<cluster>.<box>'."
        )

    def exec_container(self, cli_args):
        """Exec into a docker-compose container via ``docker compose exec``.

        Interactive shell when no command is given (``--shell`` picks the shell);
        a trailing command (after ``--`` when it has flags) runs
        non-interactively. Runs the argv list with ``shell=False`` and inherited
        stdio, so an interactive shell attaches to the real terminal.
        """
        import subprocess
        import sys

        cmd = list(getattr(cli_args, 'cmd', None) or [])
        shell = getattr(cli_args, 'shell', None) or 'sh'

        cluster, box = self._resolve_container_target(cli_args.target)
        cluster_cfg = self.config['clusters'][cluster]
        argv = self._dc_session(cluster).exec_command_for(
            cluster, cluster_cfg, box, cmd=cmd or None, shell=shell
        )
        self.logger.info(f"exec: {' '.join(argv)}")
        result = subprocess.run(argv)
        if result.returncode != 0:
            sys.exit(result.returncode)

    ### end task runner functions ####

    ### netlab (containerlab) CLI handlers ####

    def netlab_deploy(self, cli_args):
        """``boxman netlab deploy`` — render topology and deploy the lab."""
        if self.netlab is None:
            self.logger.error(
                "no 'containerlab:' block in conf.yml (or enabled: false)")
            return
        self.deploy_netlab()

    def netlab_destroy(self, cli_args):
        """``boxman netlab destroy`` — destroy only the containerlab lab."""
        if self.netlab is None:
            self.logger.error(
                "no 'containerlab:' block in conf.yml (or enabled: false)")
            return
        self.destroy_netlab()

    def netlab_inspect(self, cli_args):
        """``boxman netlab inspect`` — print lab state as JSON."""
        if self.netlab is None:
            self.logger.error(
                "no 'containerlab:' block in conf.yml (or enabled: false)")
            return
        self.netlab.preflight()
        print(json.dumps(self.netlab.inspect(), indent=2))

    def netlab_ssh(self, cli_args):
        """``boxman netlab ssh <node>`` — print ssh command for a lab node."""
        if self.netlab is None:
            self.logger.error(
                "no 'containerlab:' block in conf.yml (or enabled: false)")
            return
        node_name = getattr(cli_args, "node", None)
        if not node_name:
            self.logger.error("missing required argument: node name")
            return
        user = getattr(cli_args, "user", None)
        print(self.netlab.ssh_command(node_name, user=user))
