"""Workspace defaults, workdir bookkeeping, and managed files for BoxmanManager."""





import os

from boxman import log
from boxman.utils.io import write_files


class WorkspaceMixin:

    def resolve_workspace_defaults(self) -> None:
        """
        Resolve workspace defaults for each cluster.

        For each cluster:
        - If workdir is not set, default to workspace.path / cluster_name
        - Auto-generate a per-cluster ``inventory/01-hosts.yml`` (location
          overridable via the cluster's ``inventory:`` key) containing ONLY
          that cluster's hosts, so per-cluster Ansible runs are isolated.

        At the workspace level (written to workspace.path):
        - Auto-generate env.sh, ansible.cfg, and a combined
          inventory/01-hosts.yml spanning every cluster (used for
          project-wide ops and ``boxman ssh`` alias resolution).
        """
        config = self.config
        workspace = config.get('workspace', {})
        workspace_path = workspace.get('path', '')
        clusters = config.get('clusters', {})

        # workspace-level files (written to workspace.path)
        ws_files = workspace.setdefault('files', {})

        def _resolve_output_path(custom_path, workspace_path):
            """Resolve a custom path: expanduser, make absolute relative to workspace.path, normpath."""
            p = os.path.expanduser(custom_path)
            if not os.path.isabs(p):
                p = os.path.join(os.path.expanduser(workspace_path), p)
            return os.path.normpath(p)

        # Determine output keys for generated files based on workspace config overrides.
        # When a custom path is set and resolves to an absolute path, os.path.join(rootdir, abs)
        # returns the absolute path — so it naturally bypasses rootdir in write_files().
        custom_ansible_config = workspace.get('ansible_config')
        custom_env_file = workspace.get('env_file')
        custom_inventory = workspace.get('inventory')

        if custom_env_file and workspace_path:
            env_sh_key = _resolve_output_path(custom_env_file, workspace_path)
        else:
            env_sh_key = 'env.sh'

        if custom_inventory and workspace_path:
            inv_dir = _resolve_output_path(custom_inventory, workspace_path)
            inventory_key = os.path.join(inv_dir, '01-hosts.yml')
        else:
            inventory_key = 'inventory/01-hosts.yml'

        if custom_ansible_config and workspace_path:
            ansible_cfg_key = _resolve_output_path(custom_ansible_config, workspace_path)
        else:
            ansible_cfg_key = 'ansible.cfg'

        for cluster_name, cluster in clusters.items():
            # resolve workdir: explicit > workspace.path/cluster_name (for both
            # libvirt and docker-compose clusters).
            if 'workdir' not in cluster:
                if workspace_path:
                    cluster['workdir'] = os.path.join(workspace_path, cluster_name)
                else:
                    self.logger.warning(
                        f"cluster '{cluster_name}' has no workdir and "
                        f"workspace.path is not set"
                    )

        # --- inventory generation ---
        # Project-wide host ordering, once, so a host's boxman_alias is
        # identical in the combined and per-cluster inventories (and lines up
        # with the node<N> ssh_config aliases). Rows are (cluster, name,
        # host_key, extra_vars): libvirt VMs reach via ssh (no extra vars);
        # docker-compose containers reach via the community.docker connection
        # to the deterministic compose container name (<project>-<box>-1).
        all_hosts: list[tuple[str, str, str, dict]] = []
        for cname, cluster in clusters.items():
            if self._is_compose_cluster(cname):
                project = self._compose_project_for(cname)
                for box in (cluster.get('boxes') or {}):
                    all_hosts.append((cname, box, f'{cname}_{box}', {
                        'ansible_connection': 'community.docker.docker',
                        'ansible_host': f'{project}-{box}-1',
                    }))
            else:
                for vm_name in (cluster.get('vms') or {}):
                    all_hosts.append((cname, vm_name, f'{cname}_{vm_name}', {}))

        pad_width = len(str(len(all_hosts) - 1)) if len(all_hosts) > 1 else 1
        alias_of = {
            (c, n): f"node{str(i).zfill(pad_width)}"
            for i, (c, n, _hk, _ev) in enumerate(all_hosts)
        }

        # --- env.sh (workspace-level) --- GATEWAYHOST is the first libvirt VM
        # (the `boxman ssh` default target); a dc-only project leaves it empty
        # since containers are reached with `boxman exec`, not ssh.
        if all_hosts and env_sh_key not in ws_files:
            first_vm = next((hk for (_c, _n, hk, ev) in all_hosts if not ev), '')
            inv_val = custom_inventory if custom_inventory else 'inventory'
            cfg_val = custom_ansible_config if custom_ansible_config else 'ansible.cfg'
            ws_files[env_sh_key] = (
                f"export INVENTORY={inv_val}\n"
                f"export SSH_CONFIG=ssh_config\n"
                f"export GATEWAYHOST={first_vm}\n"
                f"export ANSIBLE_CONFIG={cfg_val}\n"
                f"export ANSIBLE_INVENTORY=\"$INVENTORY\"\n"
                f"export ANSIBLE_SSH_ARGS=\"-F $SSH_CONFIG\"\n"
            )

        # Combined workspace inventory (every cluster's hosts). Kept for
        # project-wide ops and `boxman ssh <alias>` resolution. NOTE: because
        # it merges all clusters under all.hosts, an Ansible consumer that
        # iterates groups['all'] would see every cluster — which is why each
        # cluster also gets its own scoped inventory below.
        if all_hosts and inventory_key not in ws_files:
            host_aliases = [(hk, alias_of[(c, n)]) for (c, n, hk, _ev) in all_hosts]
            cluster_groups: dict[str, list[str]] = {}
            host_extra: dict[str, dict] = {}
            for (c, _n, hk, ev) in all_hosts:
                cluster_groups.setdefault(c, []).append(hk)
                if ev:
                    host_extra[hk] = ev
            ws_files[inventory_key] = self._render_inventory(
                host_aliases, cluster_groups, host_extra)

        # Per-cluster inventory (only that cluster's hosts). This is what a
        # `run --cluster <name>` consumes (load_workspace_env repoints
        # INVENTORY/ANSIBLE_INVENTORY at the cluster's `inventory:`), so
        # groups['all'] never spans clusters. Written under the cluster's own
        # inventory dir: `cluster.inventory` if set, else <workdir>/inventory.
        ws_path_abs = (os.path.abspath(os.path.expanduser(workspace_path))
                       if workspace_path else os.path.abspath('.'))
        combined_inv_abs = os.path.abspath(
            os.path.join(ws_path_abs, inventory_key))
        for cluster_name, cluster in clusters.items():
            cluster_hosts = [
                (n, hk, ev) for (c, n, hk, ev) in all_hosts if c == cluster_name
            ]
            if not cluster_hosts or 'workdir' not in cluster:
                continue
            workdir_abs = os.path.abspath(os.path.expanduser(cluster['workdir']))
            cluster_inv_key = self._cluster_inventory_key(cluster)
            cluster_inv_abs = os.path.abspath(
                os.path.join(workdir_abs, cluster_inv_key))

            # Guard: if this cluster's inventory resolves to the same file as
            # the combined workspace inventory (e.g. cluster.workdir ==
            # workspace.path), writing it would clobber the combined file that
            # `boxman ssh <alias>` relies on. Skip it and leave the combined
            # one intact rather than silently overwriting it.
            if cluster_inv_abs == combined_inv_abs:
                self.logger.warning(
                    f"cluster '{cluster_name}' inventory resolves to the "
                    f"combined workspace inventory ({combined_inv_abs}); "
                    f"skipping its per-cluster inventory to avoid overwriting "
                    f"it. Give the cluster a distinct workdir or `inventory:` "
                    f"to isolate it.")
                continue

            # Auto-wire `cluster.inventory` (default <workdir>/inventory) so
            # `run --cluster <name>` repoints at this tree without the user
            # having to declare it; load_workspace_env resolves the relative
            # value against the cluster workdir.
            cluster.setdefault('inventory', 'inventory')

            cluster_files = cluster.setdefault('files', {})
            if cluster_inv_key in cluster_files:
                continue
            host_aliases = [
                (hk, alias_of[(cluster_name, n)])
                for (n, hk, _ev) in cluster_hosts
            ]
            host_extra = {hk: ev for (n, hk, ev) in cluster_hosts if ev}
            cluster_files[cluster_inv_key] = self._render_inventory(
                host_aliases,
                {cluster_name: [hk for (_n, hk, _ev) in cluster_hosts]},
                host_extra,
            )

        # --- ansible.cfg (workspace-level) ---
        if ansible_cfg_key not in ws_files:
            ws_files[ansible_cfg_key] = (
                "[defaults]\n"
                "host_key_checking = False\n"
                "poll_interval = 5\n"
                "callbacks_enabled = timer\n"
                "forks = 10\n"
                "nocows = 1\n"
                "timeout = 30\n"
                "interpreter_python = auto_silent\n"
                "gathering = smart\n"
                "fact_caching = jsonfile\n"
                f"fact_caching_connection = {workspace_path}/.ansible_facts\n"
                "fact_caching_timeout = 86400\n"
                "ansible_managed = Ansible managed: {file} modified on "
                "%Y-%m-%d %H:%M:%S by {uid} on {host}\n"
                "\n"
                "[ssh_connection]\n"
                "pipelining = True\n"
                "ssh_args = -o ControlMaster=auto -o ControlPersist=60s\n"
                "control_path = /tmp/ansible-ssh-%%h-%%p-%%r\n"
            )

    def collect_workdirs(self) -> list:
        """
        Return the absolute paths of every workdir referenced by the project
        config: ``workspace.path`` (if set), one per cluster, plus one per
        template (falling back to the shared default ``~/boxman-templates``
        when a template doesn't set its own ``workdir``).

        The list is de-duplicated and path-expanded; callers use it to
        bind-mount host directories into the runtime container and to
        enforce runtime/workdir sentinel checks.
        """
        dirs: set = set()
        config = self.config or {}

        ws_path = (config.get('workspace') or {}).get('path')
        if ws_path:
            dirs.add(os.path.abspath(os.path.expanduser(ws_path)))

        for cluster in (config.get('clusters') or {}).values():
            wd = cluster.get('workdir')
            if wd:
                dirs.add(os.path.abspath(os.path.expanduser(wd)))

        templates = config.get('templates') or {}
        if templates:
            default_tpl_wd = os.path.abspath(
                os.path.expanduser('~/boxman-templates'))
            for tpl in templates.values():
                wd = tpl.get('workdir')
                if wd:
                    dirs.add(os.path.abspath(os.path.expanduser(wd)))
                else:
                    dirs.add(default_tpl_wd)

        return sorted(dirs)

    #: Filename of the marker written inside every workdir to record which
    #: runtime most recently provisioned it. Used to detect cross-runtime
    #: reuse (e.g. a workdir first used with 'local' then re-used with
    #: 'docker-compose') and prompt the user to switch to a separate path.
    RUNTIME_SENTINEL_FILENAME = '.boxman-runtime'

    @staticmethod
    def _canonical_runtime_name(name: str | None) -> str | None:
        """
        Normalise runtime names so ``docker``, ``docker-compose`` and
        any historical variant collapse to a single canonical form used
        for sentinel comparisons. Without this, a sentinel written as
        ``docker`` (the CLI flag) would be seen as mismatching against
        the internal runtime instance name ``docker-compose``.
        """
        if not name:
            return name
        n = name.strip().lower()
        if n in ('docker', 'docker-compose'):
            return 'docker-compose'
        return n

    def _read_runtime_sentinel(self, path: str) -> str | None:
        """Return the runtime name recorded in *path*'s sentinel, or None."""
        sentinel = os.path.join(path, self.RUNTIME_SENTINEL_FILENAME)
        try:
            with open(sentinel) as fobj:
                return fobj.read().strip() or None
        except OSError:
            return None

    def _write_runtime_sentinel(self, path: str, runtime: str) -> None:
        """
        Record that *path* is owned by *runtime* by writing a
        ``.boxman-runtime`` marker. Failures are non-fatal: the worst case
        is that the user sees the collision prompt again on the next run.
        """
        if not path or not os.path.isdir(path):
            return
        sentinel = os.path.join(path, self.RUNTIME_SENTINEL_FILENAME)
        try:
            with open(sentinel, 'w') as fobj:
                fobj.write(f"{runtime.strip()}\n")
        except OSError as exc:
            self.logger.debug(f"could not write {sentinel}: {exc}")

    @staticmethod
    def _runtime_suffix_for(runtime: str) -> str:
        """Return the suffix appended to a suggested path, e.g.
        'docker-runtime' for a docker-compose runtime."""
        base = runtime.split('-')[0]  # 'docker-compose' -> 'docker'
        return f"{base}-runtime"

    def _prompt_workdir_runtime_collision(
        self, path: str, runtime: str
    ) -> str:
        """
        If *path* already exists on disk and was last used by a different
        runtime, interactively prompt the user to switch to a
        runtime-specific alternative like ``<path>-docker-runtime``.

        Returns the path to use for this session (either *path* unchanged
        or the suggested alternative). The user's choice is not persisted
        to ``conf.yml``; a hint is logged so they can update it manually.
        """
        if not path:
            return path
        expanded = os.path.abspath(os.path.expanduser(path))

        if not os.path.isdir(expanded):
            return path

        # a dir that contains only our sentinel (or is empty) cannot
        # conflict — skip the prompt.
        try:
            entries = [
                e for e in os.listdir(expanded)
                if e != self.RUNTIME_SENTINEL_FILENAME
            ]
        except OSError:
            return path

        previous = self._read_runtime_sentinel(expanded)
        if (self._canonical_runtime_name(previous)
                == self._canonical_runtime_name(runtime)):
            return path
        if not entries and previous is None:
            return path

        suggested = f"{expanded.rstrip(os.sep)}-" \
                    f"{self._runtime_suffix_for(runtime)}"

        print()
        print(
            f"workdir '{expanded}' was last used with runtime "
            f"'{previous or 'unknown'}' (current runtime: '{runtime}')."
        )
        print(
            "Mixing artifacts across runtimes can cause confusing "
            "failures (paths baked into ansible.cfg, ssh_config, etc.)."
        )
        try:
            answer = input(
                f"Use a runtime-specific path "
                f"'{suggested}' for this run? [y/N]: "
            ).strip().lower()
        except EOFError:
            answer = ''

        if answer in ('y', 'yes'):
            self.logger.info(
                f"hint: update conf.yml to use '{suggested}' to persist "
                f"this choice across runs"
            )
            return suggested

        self.logger.warning(
            f"continuing with '{expanded}' despite runtime mismatch "
            f"(previous: '{previous or 'unknown'}', current: '{runtime}')"
        )
        return path

    def reconcile_workdirs_with_runtime(self, runtime_name: str) -> None:
        """
        For each workdir referenced by the project config
        (``workspace.path``, per-cluster ``workdir``, per-template
        ``workdir``), detect cross-runtime collisions and rewrite the
        path in-memory when the user accepts a suffixed alternative.

        No-op for the ``local`` runtime — it is the historical default
        and doesn't need to be quarantined away from a user's existing
        layouts.
        """
        if runtime_name == 'local':
            return

        config = self.config or {}
        workspace = config.setdefault('workspace', {})
        old_ws_path = workspace.get('path')

        if old_ws_path:
            old_abs = os.path.abspath(os.path.expanduser(old_ws_path))
            new_path = self._prompt_workdir_runtime_collision(
                old_abs, runtime_name)
            new_abs = os.path.abspath(os.path.expanduser(new_path))
            if new_abs != old_abs:
                workspace['path'] = new_abs
                # Migrate cluster workdirs that live underneath the old
                # workspace path so they track the rewritten root.
                for cluster in (config.get('clusters') or {}).values():
                    wd = cluster.get('workdir')
                    if not wd:
                        continue
                    wd_abs = os.path.abspath(os.path.expanduser(wd))
                    if wd_abs == old_abs:
                        cluster['workdir'] = new_abs
                    elif wd_abs.startswith(old_abs + os.sep):
                        cluster['workdir'] = (
                            new_abs + wd_abs[len(old_abs):]
                        )

        # Clusters whose workdir is explicitly set outside of
        # workspace.path still need an individual collision check.
        for cluster in (config.get('clusters') or {}).values():
            wd = cluster.get('workdir')
            if not wd:
                continue
            wd_abs = os.path.abspath(os.path.expanduser(wd))
            new_path = self._prompt_workdir_runtime_collision(
                wd_abs, runtime_name)
            new_abs = os.path.abspath(os.path.expanduser(new_path))
            if new_abs != wd_abs:
                cluster['workdir'] = new_abs

        for tpl in (config.get('templates') or {}).values():
            wd = tpl.get('workdir') or '~/boxman-templates'
            wd_abs = os.path.abspath(os.path.expanduser(wd))
            new_path = self._prompt_workdir_runtime_collision(
                wd_abs, runtime_name)
            new_abs = os.path.abspath(os.path.expanduser(new_path))
            if new_abs != wd_abs:
                tpl['workdir'] = new_abs

    def provision_files(self) -> None:
        """
        Provision files specified in the cluster and workspace configuration.
        """
        # workspace-level files (e.g. env.sh) → written to workspace.path
        workspace = self.config.get('workspace', {})
        workspace_path = workspace.get('path', '')
        if workspace_path:
            if ws_files := workspace.get('files'):
                write_files(ws_files, rootdir=workspace_path)
            self._write_runtime_sentinel(
                os.path.abspath(os.path.expanduser(workspace_path)),
                self._runtime_name,
            )

        # cluster-level files → written to cluster workdir
        clusters = self.config['clusters']
        for _cluster_name, cluster in clusters.items():
            if files := cluster.get('files'):
                write_files(files, rootdir=cluster['workdir'])
            wd = cluster.get('workdir')
            if wd:
                self._write_runtime_sentinel(
                    os.path.abspath(os.path.expanduser(wd)),
                    self._runtime_name,
                )

    def deprovision_files(self) -> None:
        """
        Remove files and directories created during provisioning.

        This includes:
        - Files listed under workspace.files and cluster.files
        - Generated SSH keys and ssh_config
        - Cluster workdirs (if empty after cleanup)
        """
        workspace = self.config.get('workspace', {})
        workspace_path = workspace.get('path', '')

        # Remove files listed in workspace.files
        if workspace_path:
            if ws_files := workspace.get('files'):
                self._remove_files(ws_files, rootdir=workspace_path)

        clusters = self.config['clusters']
        for _cluster_name, cluster in clusters.items():
            base_path = workspace_path or cluster.get('workdir', '')
            base_path = os.path.expanduser(base_path)

            # Remove files listed in cluster.files
            if files := cluster.get('files'):
                self._remove_files(files, rootdir=cluster['workdir'])

            # Remove generated SSH keys
            admin_key_name = cluster.get('admin_key_name', 'id_ed25519_boxman')
            ssh_key_files = {
                admin_key_name: "",
                f"{admin_key_name}.pub": "",
            }
            self._remove_files(ssh_key_files, rootdir=base_path)

            # Remove generated ssh_config
            ssh_config_name = cluster.get('ssh_config', 'ssh_config')
            self._remove_files({ssh_config_name: ""}, rootdir=base_path)

            # Remove cluster workdir if empty
            cluster_workdir = os.path.expanduser(cluster.get('workdir', ''))
            if cluster_workdir and os.path.isdir(cluster_workdir):
                try:
                    os.rmdir(cluster_workdir)
                    log.info(f'removed empty directory {cluster_workdir}')
                except OSError:
                    pass

    @staticmethod
    def _remove_files(files: dict[str, str], rootdir: str = None) -> None:
        rootdir_abs = os.path.normpath(os.path.expanduser(rootdir)) if rootdir else None
        candidate_dirs: set = set()
        for _fpath in files:
            if rootdir:
                fpath = os.path.join(rootdir, _fpath)
            else:
                fpath = _fpath
            fpath = os.path.normpath(os.path.expanduser(fpath))
            if os.path.exists(fpath):
                os.remove(fpath)
                log.info(f'removed file {fpath}')
            # Collect all ancestor directories up to (but excluding) rootdir,
            # even if the file was already gone — the dirs may still be empty.
            dirpath = os.path.dirname(fpath)
            while dirpath and dirpath != os.path.sep:
                if rootdir_abs and dirpath == rootdir_abs:
                    break
                candidate_dirs.add(dirpath)
                dirpath = os.path.dirname(dirpath)

        # Remove empty directories (deepest first)
        for dirpath in sorted(candidate_dirs, key=len, reverse=True):
            try:
                os.rmdir(dirpath)
                log.info(f'removed empty directory {dirpath}')
            except OSError:
                pass
