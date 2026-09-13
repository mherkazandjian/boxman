"""VM control verbs (suspend/resume/save/start) for BoxmanManager."""

from boxman.exceptions import ProvisionError


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

    def _raise_for_control_failures(self, failed, verb) -> None:
        """Raise one aggregated error naming everything the verb could not do.

        Every session method these verbs call is annotated ``-> bool`` and
        reports a refusal by returning False. The verbs used to discard
        that and log success regardless, so ``boxman control save`` on a
        VM libvirt would not save printed nothing and exited 0 (#164 X3).
        """
        if failed:
            raise ProvisionError(
                f"control {verb} failed for {len(failed)} target(s): "
                f"{', '.join(failed)} — see the preceding errors for the "
                f"cause.")

    def suspend_vm(self, cli_args):
        """
        Suspend the machines: libvirt VMs → virsh suspend; docker-compose
        containers → ``docker compose pause``.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        failed = []
        for vm_name, _ in self._control_vm_targets(cli_args):
            if self.session_for_vm(vm_name).suspend_vm(vm_name):
                self.logger.info(f"vm {vm_name} suspended")
            else:
                failed.append(vm_name)
                self.logger.error(f"vm {vm_name} could not be suspended")
        for cluster_name, cluster in self._select_dc_clusters(cli_args):
            if not self._dc_session(cluster_name).pause_cluster(
                    cluster_name, cluster):
                failed.append(cluster_name)
                self.logger.error(f"[{cluster_name}] pause failed")
        self._raise_for_control_failures(failed, 'suspend')

    def resume_vm(self, cli_args):
        """
        Resume the machines: libvirt VMs → virsh resume; docker-compose
        containers → ``docker compose unpause``.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        failed = []
        for vm_name, _ in self._control_vm_targets(cli_args):
            if self.session_for_vm(vm_name).resume_vm(vm_name):
                self.logger.info(f"VM {vm_name} resumed")
            else:
                failed.append(vm_name)
                self.logger.error(f"vm {vm_name} could not be resumed")
        for cluster_name, cluster in self._select_dc_clusters(cli_args):
            if not self._dc_session(cluster_name).unpause_cluster(
                    cluster_name, cluster):
                failed.append(cluster_name)
                self.logger.error(f"[{cluster_name}] unpause failed")
        self._raise_for_control_failures(failed, 'resume')

    def save_vm(self, cli_args):
        """
        Save the state of libvirt VMs to a file. Not supported for
        docker-compose containers (no save-to-file state) — an explanatory
        message is logged, no traceback.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        failed = []
        for vm_name, workdir in self._control_vm_targets(cli_args):
            if not self.session_for_vm(vm_name).save_vm(vm_name, workdir):
                failed.append(vm_name)
                self.logger.error(
                    f"vm {vm_name} state could not be saved")
        for cluster_name, _cluster in self._select_dc_clusters(cli_args):
            self.logger.warning(
                f"'control save' is not supported for docker-compose cluster "
                f"'{cluster_name}' — containers have no save-to-file state; use "
                f"snapshots (Phase 7) or 'destroy'. Skipping."
            )
        self._raise_for_control_failures(failed, 'save')

    def start_vm(self, cli_args):
        """
        Start the machines: libvirt VMs (optionally --restore); docker-compose
        containers → ``docker compose start``.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        failed = []
        for vm_name, workdir in self._control_vm_targets(cli_args):
            if cli_args.restore:
                # The explicit opt-in: this is the one path allowed to apply
                # an external save file left by an older boxman. `up` refuses
                # to do it unasked (#164 FB-3).
                ok = self.session_for_vm(vm_name).restore_vm(
                    vm_name, workdir, allow_legacy=True)
                what = 'restored'
            else:
                ok = self.session_for_vm(vm_name).start_vm(vm_name)
                what = 'started'
            if not ok:
                failed.append(vm_name)
                self.logger.error(f"vm {vm_name} could not be {what}")
        for cluster_name, cluster in self._select_dc_clusters(cli_args):
            if getattr(cli_args, "restore", False):
                self.logger.info(
                    f"[{cluster_name}] --restore has no docker-compose "
                    f"equivalent; starting containers")
            if not self._dc_session(cluster_name).start_cluster(
                    cluster_name, cluster):
                failed.append(cluster_name)
                self.logger.error(f"[{cluster_name}] start failed")
        self._raise_for_control_failures(failed, 'start')
