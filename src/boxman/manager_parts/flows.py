"""Top-level provision/up/down/deprovision/destroy flows for BoxmanManager."""


import os
import shlex
import shutil
import subprocess
import time

from boxman import log
from boxman.exceptions import BoxmanError, ConfigError, ProvisionError


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
        # A VM that was never defined does not appear in _get_vm_states()
        # at all, so comparing only the states it *does* report would let a
        # missing VM pass as provisioned. Expect every name the config
        # declares and treat an absent one as not running.
        _expected = self._get_project_vm_names()
        still_down: dict[str, str] = {}
        for _round in range(1, 21):
            vm_states = self._get_vm_states()
            not_running = {
                name: vm_states.get(name, 'not defined')
                for name in _expected
                if vm_states.get(name) != 'running'
            }
            if not not_running:
                self.logger.info("all VMs are running")
                break
            self.logger.info(
                f"round {_round}: {len(not_running)} VM(s) not yet running "
                f"({', '.join(f'{n}={s}' for n, s in not_running.items())}), retrying..."
            )
            for _vm_name in not_running:
                # the loop re-reads the state each round, so the bool here
                # adds nothing the next iteration would not see
                self.session_for_vm(_vm_name).start_vm(_vm_name)
            time.sleep(3)
        else:
            vm_states = self._get_vm_states()
            still_down = {
                name: vm_states.get(name, 'not defined')
                for name in _expected
                if vm_states.get(name) != 'running'
            }
            if still_down:
                self.logger.error(
                    f"gave up after 20 rounds; the following VMs are still not running: "
                    f"{', '.join(f'{n}={s}' for n, s in still_down.items())}"
                )

        # Carried to the end of provision rather than raised here: the VMs
        # that did come up still need their ssh access, their compose
        # clusters and their lab. Warning and continuing, though, meant
        # `provision` exited 0 with dead VMs — after burning the full IP
        # timeout on them (#164 X3).
        undead = sorted(still_down) if still_down else []

        # use adaptive wait for ip address assignment, skipping the VMs that
        # never started — they have no lease coming
        _waiting_for = [name for name in self._get_project_vm_names()
                        if name not in undead]
        if _waiting_for:
            self.wait_for_vm_ips(_waiting_for, max_wait=600)

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

        if undead:
            raise ProvisionError(
                f"provision finished, but {len(undead)} VM(s) never started: "
                f"{', '.join(undead)}. The rest of the project was "
                f"provisioned — see the preceding errors for the cause.")

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
            # A cluster that will not come up must not cost the operator
            # their connection info and ssh config, so it is reported after
            # the reconciliation rather than raised through it.
            compose_error = self._reconcile_compose_clusters()
            self.connect_info()
            # Re-write SSH config in case IPs changed (DHCP renewals after
            # a host reboot, manual virsh net cycle, etc.) or in case the
            # file is missing/stale from an older boxman version.
            self.write_ssh_config()
            if compose_error:
                raise ProvisionError(compose_error)
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
            """Bring one VM up from whatever state it is in.

            Every provider call here returns a bool. Discarding them made
            the aggregation below unreachable for the ordinary case: a
            guest that libvirt cleanly refused to start reported no
            failure at all, only a log line (#164 X3).
            """
            session = self.session_for_vm(vm_name)
            self.logger.info(f"VM '{vm_name}' is in state '{state}'")
            if state == 'paused':
                self.logger.info(f"resuming VM '{vm_name}'...")
                action, ok = 'resume', session.resume_vm(vm_name)
            elif state in ('saved', 'managedsave'):
                self.logger.info(f"restoring VM '{vm_name}' from saved state...")
                action, ok = 'restore', session.restore_vm(vm_name, workdir)
            elif state in ('shut off', 'shutoff'):
                # A domain reported 'shut off' holds no managed saved state —
                # _get_vm_states() would have said 'managedsave' — but an
                # older boxman may have left an external save file beside it.
                # Cold-booting over that discards the guest's memory, and
                # restoring it unasked risks applying a stale image to a disk
                # that has moved on. Refuse and let the user choose
                # (#164 FB-3).
                self._refuse_stale_external_save(vm_name, workdir)
                self.logger.info(f"starting VM '{vm_name}'...")
                action, ok = 'start', session.start_vm(vm_name)
            elif state in ('crashed', 'dying'):
                self.logger.warning(
                    f"VM '{vm_name}' is in state '{state}', "
                    f"force-stopping and starting it...")
                # libvirt refuses `start` while a domain is still active,
                # and a crashed one is active — so it has to be stopped
                # first. This used to call
                # destroy_vm(vm_name, remove_storage=False), which raised
                # TypeError (no such parameter) and, had the argument
                # existed, would have *undefined* the domain rather than
                # stopping it (#164 X3).
                if not session.force_stop_vm(vm_name):
                    raise ProvisionError(
                        f"could not force-stop vm '{vm_name}' (state "
                        f"'{state}') before restarting it")
                action, ok = 'restart', session.start_vm(vm_name)
            else:
                self.logger.warning(
                    f"VM '{vm_name}' is in unexpected state '{state}', "
                    f"attempting to start...")
                action, ok = 'start', session.start_vm(vm_name)

            if not ok:
                raise ProvisionError(
                    f"could not {action} vm '{vm_name}' (was '{state}')")

        _results, failures = self._run_parallel(
            [(vm_name, _bring_up,
              (vm_name, state, vm_workdir_map.get(vm_name, '')))
             for vm_name, state in non_running.items()],
            op_label='bring up vm')
        # The failure is carried to the end of `up` rather than raised
        # here: the VMs that did come up still need their networks, their
        # lab and their ssh config reconciled, and the operator still
        # needs the connection info for them. Raising on the spot turned
        # one dead VM into a wholly unconfigured project.

        # Wait for IP addresses — but not for the VMs that failed to come
        # up. They have no lease coming, and each one would burn the full
        # timeout before `up` gets to report why.
        waiting_for = [name for name in self._get_project_vm_names()
                       if name not in failures]
        if waiting_for:
            self.wait_for_vm_ips(waiting_for, max_wait=300)

        # Reconcile the containerlab lab after the VMs are up so the
        # shared bridges have live endpoints on both sides.
        self.ensure_netlab_up()

        # Bring up docker-compose clusters after the VMs are up (idempotent).
        compose_error = self._reconcile_compose_clusters()

        # Display connection information
        self.connect_info()

        # Re-write SSH config with current IPs
        self.write_ssh_config()

        problems = []
        if failures:
            problems.append(
                f"could not bring up {len(failures)} VM(s) "
                f"({', '.join(sorted(failures))})")
        if compose_error:
            problems.append(compose_error)
        if problems:
            raise ProvisionError(
                f"{'; '.join(problems)}. The rest of the project was "
                f"reconciled — see the preceding errors for the cause.")

        self.logger.info("infrastructure is up")

    def _reconcile_compose_clusters(self) -> str:
        """Bring the dc clusters up, returning the error instead of raising.

        ``up`` still has connection info and the ssh config to write for
        the parts of the project that did come up; a compose cluster that
        will not start is reported once the rest is reconciled.

        Returns:
            The aggregated error message, or ``''`` when every cluster
            came up.
        """
        try:
            self.provision_compose_clusters()
        except BoxmanError as exc:
            return str(exc)
        return ''

    def _refuse_stale_external_save(self, vm_name: str, workdir: str) -> None:
        """
        Refuse to cold-boot a VM over an external save file.

        Older boxman versions saved guest state with ``virsh save`` into the
        cluster workdir, outside libvirt's control. ``up`` never restored
        those files, so it silently discarded the memory image. Restoring one
        automatically is no better: nothing ties an external save to the
        current contents of the disk, so a stale one corrupts the guest.

        The only safe move is to stop and let the user pick (#164 FB-3).

        Raises:
            ProvisionError: if an external save file exists for *vm_name*.
        """
        if not workdir:
            return
        save_path = os.path.join(
            os.path.abspath(os.path.expanduser(workdir)), f"{vm_name}.save")
        if not os.path.exists(save_path):
            return
        raise ProvisionError(
            f"vm '{vm_name}' is shut off, but an external save file written "
            f"by an older boxman exists at {save_path}.\n"
            f"Starting the VM now would discard that memory image, and "
            f"restoring it automatically is not safe either: nothing ties it "
            f"to the current contents of the disk.\n"
            f"  - to use it:     boxman control start --restore --vms "
            f"{vm_name.split('_')[-1]}\n"
            f"  - to discard it: rm {shlex.quote(save_path)}\n"
            f"then run 'boxman up' again.")

    def down(self, cli_args):
        """
        Bring down the infrastructure by saving or suspending all VMs.

        By default, saves each VM's state to disk with libvirt's *managed*
        save (same as ``boxman control save``), so the next ``up`` or
        ``control start`` restores it rather than cold-booting. With
        ``--suspend``, pauses VMs in memory instead (same as
        ``boxman control suspend``).

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
                if not self.session_for_vm(vm_name).suspend_vm(vm_name):
                    raise ProvisionError(f"could not suspend vm '{vm_name}'")
                self.logger.info(f"VM '{vm_name}' suspended")

            processes = [
                (vm_name, _suspend, (vm_name,))
                for vm_name, _ in vm_list
            ]
        else:
            if vm_list:
                self.logger.info("saving the state of all VMs to disk...")

            def _save(vm_name, workdir):
                # Not "to <workdir>": the memory image belongs to libvirt
                # now, under /var/lib/libvirt/qemu/save (#164 FB-3).
                self.logger.info(f"saving VM '{vm_name}' state...")
                if not self.session_for_vm(vm_name).save_vm(vm_name, workdir):
                    raise ProvisionError(
                        f"could not save vm '{vm_name}' to '{workdir}'")
                self.logger.info(f"VM '{vm_name}' state saved")

            processes = [
                (vm_name, _save, (vm_name, workdir))
                for vm_name, workdir in vm_list
            ]

        _results, failures = self._run_parallel(processes, op_label='down')

        # Stop the compose clusters even when a VM failed: `down` was asked
        # to bring the whole project down, and leaving the containers
        # running because one guest would not save is a partial teardown
        # the operator has no way to see.
        compose_error = ''
        try:
            self.stop_compose_clusters()
        except BoxmanError as exc:
            compose_error = str(exc)

        problems = []
        if failures:
            problems.append(
                f"could not bring down {len(failures)} VM(s) "
                f"({', '.join(sorted(failures))}); their state is undefined")
        if compose_error:
            problems.append(compose_error)
        if problems:
            raise ProvisionError(
                f"{'; '.join(problems)} — see the preceding errors for "
                f"the cause.")

        self.logger.info("infrastructure is down")

    def deprovision(self, cli_args, finalize: bool = True):
        """
        Tear down the project's containerlab lab, compose clusters, VMs and
        networks.

        Teardown is kept separate from *cleanup*. The teardown steps always
        run and their failures are collected; the parts that cannot be undone
        — removing the generated provisioning files and forgetting the project
        — happen only once every teardown step is confirmed complete. A
        partial failure therefore leaves both the surviving resources and the
        state needed to retry: the cache entry that keeps them visible to
        ``boxman list``, and the ssh keys and inventory a second attempt needs.

        Args:
            cli_args: Parsed CLI arguments; ``--cleanup`` additionally removes
                the generated provisioning files.
            finalize: When False the caller owns the final cleanup. ``destroy``
                passes False so it can finish its own teardown steps first and
                only then remove files and cache entries.

        Raises:
            ProvisionError: If any resource survived the teardown, or the
                teardown could not be confirmed.
        """
        # Ensure provider configs reflect runtime settings.
        # Project-level provider settings (from conf.yml) always take
        # precedence over app-level defaults (from boxman.yml).
        self._update_sessions_with_runtime()

        # Tear down the containerlab lab first so its veths release any
        # shared bridges before we touch libvirt state.
        self.destroy_netlab()

        # Tear down docker-compose clusters (`docker compose down`: remove
        # containers + networks, keep named volumes). A failure here must not
        # abort the libvirt VM / network teardown that follows (deprovision is
        # also invoked from `provision --force`), but it is remembered so the
        # command cannot go on to report success.
        # The flag is a bool of its own: deriving it from the message would
        # lose an exception raised with an empty one.
        compose_failed = False
        compose_error = ''
        try:
            self.deprovision_compose_clusters()
        except Exception as exc:
            compose_failed = True
            compose_error = str(exc) or exc.__class__.__name__
            self.logger.error(
                f"deprovision_compose_clusters raised: {compose_error} "
                f"— continuing")

        processes = [
            (f"{cluster_name}/{vm_name}", self._destroy_vm_and_disks,
             (cluster_name, cluster, vm_name, vm_info))
            for cluster_name, cluster in self._vm_clusters.items()
            for vm_name, vm_info in cluster['vms'].items()
        ]
        _results, vm_failures = self._run_parallel(
            processes, op_label='deprovision vm')

        net_failures = self.destroy_networks()

        torn_down, reason = self._confirm_project_torn_down()

        if vm_failures or net_failures or compose_failed or not torn_down:
            # Resources survived the teardown, or we could not prove that they
            # did not. Keep the project registered and its generated files in
            # place, and fail loudly rather than exiting 0 over the leftovers.
            problems: list[str] = []
            if vm_failures:
                problems.append("VMs: " + "; ".join(
                    f"{name}: {why}"
                    for name, why in sorted(vm_failures.items())))
            if net_failures:
                problems.append("networks: " + "; ".join(
                    f"{name}: {why}"
                    for name, why in sorted(net_failures.items())))
            if compose_failed:
                problems.append(f"docker-compose: {compose_error}")
            if not torn_down:
                problems.append(reason)

            self.logger.error(
                "deprovision left resources behind; keeping project "
                f"'{self.config['project']}' registered in the cache and its "
                f"generated files in place so the teardown can be retried")
            raise ProvisionError(
                "deprovision did not complete — " + " | ".join(problems))

        if not finalize:
            return

        if getattr(cli_args, 'cleanup', False):
            self.deprovision_files()

        self.unregister_from_cache()

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
    def _safe_delete_target(path: str) -> str:
        """
        Validate a recursive-deletion argument and return its canonical target.

        ``destroy`` hands user-supplied configuration (``workspace.path``,
        template workdirs) to a recursive delete that also bind-mounts the
        directory into a throwaway container for ``rm -rf``. A typo such as
        ``workspace.path: /`` or ``~`` would therefore delete that whole tree,
        so every deletion is vetted here first.

        The *original* argument is checked before it is resolved — an empty
        string resolves to the current directory and a relative path would
        silently become an absolute deletion target — and a leaf symlink is
        refused outright rather than followed. What is then validated, and
        returned, is the canonical path, so the target that was vetted is
        exactly the target that gets deleted. ``destroy``'s preflight and
        :meth:`_force_rmtree` share this one validator, so a path can never
        pass the preflight and be rejected later, once teardown has begun.

        This is a guardrail against mistakes, not a sandbox: it cannot know
        that some deep, plausible-looking directory is precious.

        Args:
            path: The deletion argument, as configured.

        Returns:
            The canonical, symlink-resolved path that may be deleted.

        Raises:
            ProvisionError: If the argument, or its canonical target, is
                unsafe to delete recursively.
        """
        if not path or not os.path.isabs(path):
            raise ProvisionError(
                f"refusing to recursively delete {path!r}: the path is "
                f"empty or not absolute")
        if os.path.islink(path):
            raise ProvisionError(
                f"refusing to recursively delete {path!r}: it is a symlink, "
                f"so the deletion would land on whatever it points at")

        real = os.path.realpath(path)

        if real == os.path.realpath(os.path.expanduser("~")):
            raise ProvisionError(
                f"refusing to recursively delete the home directory ({real})")
        if real == os.path.dirname(real) or os.path.ismount(real):
            raise ProvisionError(
                f"refusing to recursively delete {real}: it is a filesystem "
                f"root or a mount point")
        if len([part for part in real.split(os.sep) if part]) < 2:
            raise ProvisionError(
                f"refusing to recursively delete the top-level path {real}")
        if os.path.exists(os.path.join(real, ".git")):
            raise ProvisionError(
                f"refusing to recursively delete {real}: it looks like a "
                f"repository root (it contains .git)")

        return real

    @staticmethod
    def _force_rmtree(path: str) -> None:
        """
        Remove *path* and everything under it.

        The argument is vetted by :meth:`_safe_delete_target` and the
        canonical path it returns is what gets removed. Falls back to a
        throwaway ``docker run --rm alpine rm -rf`` when ``shutil.rmtree``
        leaves root-owned leftovers behind (created by the libvirt container
        running as root).

        ``ignore_errors=True`` stays on both attempts — partial failures are
        expected, which is why the fallback exists — but if the directory is
        *still* there at the end this raises instead of logging a warning, so
        callers can stop and keep the rest of the project's state rather than
        carrying on after a cleanup that silently failed.

        Args:
            path: Directory to remove.

        Raises:
            ProvisionError: If the target is unsafe to delete, or if it
                survived both removal attempts.
        """
        real = FlowsMixin._safe_delete_target(path)

        if not os.path.isdir(real):
            log.info(f"{real} does not exist — nothing to remove")
            return

        log.info(f"removing {real}")
        shutil.rmtree(real, ignore_errors=True)
        if not os.path.isdir(real):
            log.info(f"removed {real}")
            return

        log.info(
            f"{real} still exists (root-owned leftovers), "
            f"removing via docker")
        result = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{real}:/cleanup",
             "alpine", "sh", "-c", "rm -rf /cleanup/* /cleanup/.[!.]* || true"],
            check=False,
        )
        if result.returncode != 0:
            log.warning(
                f"docker alpine rm -rf exited with {result.returncode}")
        # The bind-mount dir itself can't be removed from inside the
        # container, but it should now be empty.
        shutil.rmtree(real, ignore_errors=True)
        if os.path.isdir(real):
            raise ProvisionError(
                f"could not remove {real}: it still exists after both the "
                f"direct removal and the containerised fallback")
        log.info(f"removed {real}")

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

        # Preflight every path this command would delete, with the same guard
        # the deletion itself uses, before anything is torn down. Discovering
        # a misconfigured workspace.path halfway through — with the VMs
        # already gone — is exactly the failure this avoids.
        delete_targets = list(template_dirs)
        if workspace_path:
            delete_targets.append(workspace_path)
        if is_docker and runtime_plan:
            delete_targets.extend(runtime_plan.get("paths_to_delete", []))
        for target in delete_targets:
            self._safe_delete_target(target)

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
        # Everything irreversible — the generated provisioning files, the
        # cache entry, the workspace tree, the template trees — is deferred
        # until the teardown above it is confirmed complete. Removing that
        # state while VMs, networks or containers survive leaves resources
        # that are both orphaned and invisible to `boxman list`, with the ssh
        # keys needed to reach them gone.
        teardown_ok = True
        problems: list[str] = []

        # 1. Best-effort: start the runtime so we can run virsh to
        #    deprovision VMs. If it fails (port conflict, docker daemon
        #    unreachable, libvirtd unresponsive in a zombie mount
        #    namespace, …) we skip the VM-level step. A short ready_timeout
        #    keeps the failure path snappy: if the runtime is broken, we
        #    don't want to wait a full minute during destroy.
        runtime_up = True
        if is_docker:
            runtime.ready_timeout = min(
                getattr(runtime, "ready_timeout", 60), 10)
            # destroy exists to tear this project down and has already been
            # confirmed above, so a container recreate that stops guests is
            # authorised here. Without this the guest guard would refuse,
            # ensure_ready would fail, and the VM-level deprovision would be
            # skipped — stranding the very VMs destroy was asked to remove.
            runtime.allow_recreate = True
        try:
            runtime.ensure_ready()
        except Exception as exc:
            runtime_up = False
            self.logger.warning(
                f"runtime could not be started ({exc}) — "
                f"skipping VM-level deprovision")

        # 2. deprovision VMs + networks. finalize=False: this command owns
        #    the file and cache cleanup, and runs it only once every step
        #    below has also passed.
        if runtime_up and self.provider is not None:
            cleanup_args = type("Args", (), {
                "cleanup": True,
                "docker_compose": getattr(cli_args, "docker_compose", False),
            })()
            try:
                self.deprovision(cleanup_args, finalize=False)
            except Exception as exc:
                teardown_ok = False
                problems.append(str(exc))
                self.logger.error(f"deprovision failed: {exc}")
        elif self._has_libvirt_clusters():
            teardown_ok = False
            problems.append(
                "the runtime or provider session was unavailable, so this "
                "project's VMs and networks were never torn down")

        # 2b. fully tear down docker-compose clusters — destroy goes beyond
        #     deprovision's `docker compose down` (keeps named volumes) to
        #     `down --volumes` and removes the generated compose file. Runs
        #     regardless of the libvirt-in-container runtime state: the
        #     compose provider shells out to the host docker directly. This
        #     is a different operation from the one deprovision ran, so its
        #     failure has to be caught separately.
        try:
            self.destroy_compose_clusters()
        except Exception as exc:
            teardown_ok = False
            detail = str(exc) or exc.__class__.__name__
            problems.append(f"docker-compose teardown failed: {detail}")
            self.logger.error(f"destroy_compose_clusters raised: {detail}")

        # 2c. fail-closed confirmation that nothing of this project survived.
        if teardown_ok:
            confirmed, reason = self._confirm_project_torn_down()
            if not confirmed:
                teardown_ok = False
                problems.append(reason)

        if not teardown_ok:
            summary = " | ".join(problems)
            self.logger.error(
                "keeping the workspace, template dirs, generated files, "
                "runtime state and the cache entry so the surviving "
                "resources stay visible and the teardown can be retried")
            raise ProvisionError(f"destroy did not complete — {summary}")

        # 3. tear down the docker runtime (reuses _force_rmtree for the
        #    .boxman dir, no double prompt). A failure here stops the
        #    remaining cleanup rather than being logged and ignored: the
        #    runtime holds the libvirt state, so removing the workspace and
        #    the cache entry over a container that is still up loses the only
        #    handles on it.
        if is_docker:
            try:
                boxman_dir = runtime.destroy_runtime()
            except Exception as exc:
                detail = str(exc) or exc.__class__.__name__
                self.logger.error(
                    "keeping the workspace, template dirs, generated files "
                    "and the cache entry: the runtime teardown failed")
                raise ProvisionError(
                    f"destroy did not complete — runtime teardown failed: "
                    f"{detail}") from exc
            if boxman_dir and os.path.isdir(boxman_dir):
                self._force_rmtree(boxman_dir)

        # 4. remove the generated provisioning files (env.sh, ansible.cfg,
        #    inventory, ssh_config, generated SSH keys)
        self.deprovision_files()

        # 5. nuke the workspace workdir
        if workspace_path:
            self._force_rmtree(workspace_path)

        # 6. nuke template workdirs (only when --templates was passed)
        for tpl_dir in template_dirs:
            self._force_rmtree(tpl_dir)

        # 7. unregister the project LAST. Every removal above raises on
        #    failure, so reaching this point means there is nothing left to
        #    stay visible for — and if one of them did fail, the project is
        #    still in the cache and `boxman list` still shows what survived.
        self.unregister_from_cache()

        self.logger.info("destroy complete")
