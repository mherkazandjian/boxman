"""VM control verbs (suspend/resume/save/start) for BoxmanManager."""



























class ControlMixin:

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
