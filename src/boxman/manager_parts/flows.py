"""Top-level provision/up/down/deprovision/destroy flows for BoxmanManager."""


import os
import shutil
import subprocess
import time

from boxman import log
from boxman.exceptions import ConfigError, ProvisionError


class FlowsMixin:

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
            self.raise_on_network_failures(network_results)

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
        network_results = self.reconcile_networks(
            allow_recreate=getattr(cli_args, 'recreate_networks', False),
            auto_accept=getattr(cli_args, 'yes', False))
        self.report_network_results(network_results)
        self.raise_on_network_failures(network_results)

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

        self._run_parallel(
            [(vm_name, _bring_up,
              (vm_name, state, vm_workdir_map.get(vm_name, '')))
             for vm_name, state in non_running.items()],
            op_label='bring up vm')

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
