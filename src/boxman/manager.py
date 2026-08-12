import os
from multiprocessing import Process, Queue
from typing import Any, Optional

from boxman import log
from boxman.abstract.providers import ProviderSession
from boxman.config_cache import BoxmanCache
from boxman.manager_parts.compose import ComposeMixin
from boxman.manager_parts.config import ConfigMixin
from boxman.manager_parts.control import ControlMixin
from boxman.manager_parts.flows import FlowsMixin
from boxman.manager_parts.images import ImagesMixin
from boxman.manager_parts.misc import MiscMixin
from boxman.manager_parts.naming import NamingMixin
from boxman.manager_parts.netlab import NetlabMixin
from boxman.manager_parts.networks import NetworksMixin
from boxman.manager_parts.snapshots import SnapshotsMixin
from boxman.manager_parts.ssh import SSHMixin
from boxman.manager_parts.vms import VMsMixin
from boxman.manager_parts.workspace import WorkspaceMixin
from boxman.netlab import ContainerlabManager
from boxman.providers import merge_provider_configs, primary_provider_type
from boxman.providers.libvirt.commands import VirshCommand
from boxman.runtime import RuntimeBase, create_runtime
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


class BoxmanManager(
    ConfigMixin, WorkspaceMixin, NamingMixin, SSHMixin,
    ImagesMixin, NetworksMixin, VMsMixin,
    SnapshotsMixin, FlowsMixin, ComposeMixin, ControlMixin, NetlabMixin,
    MiscMixin,
):
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
