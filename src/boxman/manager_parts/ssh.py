"""SSH keys, ssh config, and connect-info helpers for BoxmanManager."""





import os
import time

from boxman.utils.references import resolve_reference
from boxman.utils.shell import run


class SSHMixin:

    def get_connect_info(self) -> bool:
        """
        Gather connection information for all VMs in all clusters.

        Queries all VMs in parallel and returns True only if every VM has at
        least one IP address.

        Returns:
            True if all VMs have at least one IP address, False otherwise
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        vm_names = [
            f"{prj_name}_{cluster_name}_{vm_name}"
            for cluster_name, cluster in self._vm_clusters.items()
            for vm_name in cluster['vms']
        ]

        def _check(full_vm_name):
            return self.session_for_vm(full_vm_name).get_vm_ip_addresses(full_vm_name)

        # Routed through _run_parallel: a crashed child can no longer
        # deadlock the parent on a blocking result_queue.get().
        results, _failures = self._run_parallel(
            [(n, _check, (n,)) for n in vm_names],
            op_label='ip address check')

        all_vms_have_ip = True
        for full_vm_name in vm_names:
            ips = results.get(full_vm_name)
            if not ips:
                all_vms_have_ip = False
                self.logger.warning(f"vm {full_vm_name} does not have an ip address yet")

        return all_vms_have_ip

    def connect_info(self) -> None:
        """
        Display connection information for all VMs in all clusters.

        This method displays the VM names, hostnames, IP addresses, and
        other connection details for all configured VMs.
        """
        self.logger.status("=== vm connection information ===")
        ws_path = self.config.get('workspace', {}).get('path', '')

        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self._vm_clusters.items():
            self.logger.status(f"cluster: {cluster_name}")

            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                hostname = vm_info.get('hostname', vm_name)

                self.logger.status(f"vm: {vm_name} (hostname: {hostname})")

                # get the ip addresses for all interfaces
                ip_addresses = self.session_for_cluster(cluster_name).get_vm_ip_addresses(full_vm_name)

                if ip_addresses:
                    self.logger.status("  ip addresses:")
                    for iface, ip in ip_addresses.items():
                        self.logger.status(f"    {iface}: {ip}")
                else:
                    self.logger.status("  ip addresses: not available")

                # get ssh connection information
                admin_user = cluster.get('admin_user', '<placeholder>')
                base_path = ws_path or cluster.get('workdir', '~')
                admin_key = os.path.expanduser(os.path.join(
                    base_path,
                    cluster.get('admin_key_name', 'id_ed25519_boxman')
                ))

                self.logger.status("  connect via ssh:")
                # show direct connection if ip is available
                if ip_addresses:
                    first_ip = next(iter(ip_addresses.values()))
                    self.logger.status(f"    direct: ssh -i {admin_key} {admin_user}@{first_ip}")

                # show connection using ssh_config if available
                if 'ssh_config' in cluster:
                    ssh_config = os.path.expanduser(os.path.join(
                        base_path,
                        cluster.get('ssh_config', 'ssh_config')
                    ))
                    self.logger.status(f"    via config: ssh -F {ssh_config} {cluster_name}_{hostname}")

                self.logger.status("")

            self.logger.status("")

        # docker-compose clusters: container status + published ports + the
        # exec entry point (containers are reached with `boxman exec`, not ssh).
        for cluster_name, cluster in self._compose_clusters.items():
            self.logger.info(f"cluster: {cluster_name} (docker-compose)")
            self.logger.info("-" * 60)
            try:
                status = {
                    r["service"]: r for r in
                    self._dc_session(cluster_name).container_status(
                        cluster_name, cluster)
                }
            except Exception as exc:
                self.logger.warning(f"  could not query containers: {exc}")
                status = {}
            for box_name in (cluster.get("boxes") or {}):
                row = status.get(box_name, {})
                state = row.get("state", "not created")
                health = f" ({row['health']})" if row.get("health") else ""
                self.logger.info(f"container: {box_name}  [{state}{health}]")
                if row.get("ports"):
                    self.logger.info(f"  published ports: {row['ports']}")
                self.logger.info(f"  connect: boxman exec {cluster_name}.{box_name}")
                self.logger.info("")
            self.logger.info("")

    #: Alias of the ProxyJump stanza written into ``ssh_config`` when the
    #: docker runtime is active. Kept as a class constant so tests and
    #: downstream tooling can grep for it reliably.
    SSH_JUMP_HOST_ALIAS = "boxman-libvirt-jump"

    def _docker_ssh_jump_stanza(self) -> str | None:
        """
        Return the ``Host boxman-libvirt-jump`` block to prepend to
        ``ssh_config`` when the docker runtime is active, or ``None``
        when the runtime is ``local`` (VMs are directly reachable from
        the host in that case).
        """
        from boxman.runtime.docker_compose import DockerComposeRuntime

        if not isinstance(self.runtime_instance, DockerComposeRuntime):
            return None

        rt = self.runtime_instance
        return (
            f"Host {self.SSH_JUMP_HOST_ALIAS}\n"
            f"    HostName     127.0.0.1\n"
            f"    Port         {rt.ssh_port}\n"
            f"    User         qemu_user\n"
            f"    IdentityFile {rt.ssh_identity_path}\n"
            f"    StrictHostKeyChecking no\n"
            f"    UserKnownHostsFile /dev/null\n"
            f"\n\n"
        )

    def write_ssh_config(self) -> None:
        """
        Generate SSH configuration file for easy access to VMs.

        Creates an SSH config file in the workspace directory that allows
        simplified access to VMs without typing full connection details.

        Under the docker runtime, a ``Host boxman-libvirt-jump`` stanza
        is prepended and each VM block gets a ``ProxyJump`` directive so
        that host-side ``ssh``, ``scp`` and ansible transparently hop
        through the libvirt container to reach VMs on libvirt's internal
        NAT network.
        """
        ws_path = self.config.get('workspace', {}).get('path', '')
        prj_name = f'bprj__{self.config["project"]}__bprj'

        # count total VMs across all clusters for zero-padded alias numbering
        total_vms = sum(
            len(cluster.get('vms', {}))
            for cluster in self.config['clusters'].values()
        )
        pad_width = len(str(total_vms - 1)) if total_vms > 1 else 1
        vm_counter = 0

        jump_stanza = self._docker_ssh_jump_stanza()

        # Group clusters by their resolved ssh_config path. When
        # workspace.path is set every cluster resolves to the SAME path,
        # so we must write each unique path once with all relevant VM
        # blocks — opening 'w' once per cluster would have the later
        # iteration truncate the earlier one's entries.
        groups: dict[str, list[tuple[str, dict, str]]] = {}
        for cluster_name, cluster in self._vm_clusters.items():
            base_path = ws_path or cluster.get('workdir', '~')
            ssh_config = os.path.expanduser(os.path.join(
                base_path,
                cluster.get('ssh_config', 'ssh_config')
            ))
            groups.setdefault(ssh_config, []).append(
                (cluster_name, cluster, base_path)
            )

        for ssh_config, clusters_in_group in groups.items():
            self.logger.info(f"writing ssh config to {ssh_config}")

            with open(ssh_config, 'w') as fobj:
                # write global SSH options
                fobj.write('Host *\n')
                fobj.write('    StrictHostKeyChecking no\n')
                fobj.write('    UserKnownHostsFile /dev/null\n')
                fobj.write('\n\n')

                # docker runtime: jump host stanza
                if jump_stanza:
                    fobj.write(jump_stanza)

                for cluster_name, cluster, base_path in clusters_in_group:
                    admin_priv_key = os.path.expanduser(os.path.join(
                        base_path,
                        cluster.get('admin_key_name', 'id_ed25519_boxman')
                    ))

                    for vm_name, vm_info in cluster['vms'].items():
                        full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                        hostname = vm_info.get('hostname', vm_name)
                        prefixed_host = f"{cluster_name}_{hostname}"
                        padded_alias = f"node{str(vm_counter).zfill(pad_width)}"
                        vm_counter += 1

                        # get the first ip address if available
                        ip_addresses = self.session_for_cluster(cluster_name).get_vm_ip_addresses(full_vm_name)

                        if ip_addresses:
                            first_ip = next(iter(ip_addresses.values()))

                            fobj.write(f'Host {prefixed_host} {padded_alias}\n')
                            fobj.write(f'    Hostname {first_ip}\n')
                            fobj.write(f'    User {cluster.get("admin_user", "admin")}\n')
                            fobj.write(f'    IdentityFile {admin_priv_key}\n')
                            if jump_stanza:
                                fobj.write(
                                    f'    ProxyJump {self.SSH_JUMP_HOST_ALIAS}\n')
                            fobj.write('\n\n')
                        else:
                            self.logger.warning(
                                f"no ip address available for the vm {vm_name}, "
                                "skipping SSH config entry")

            self.logger.info(f"ssh config file written to {ssh_config}")
            self.logger.info(f"to connect: ssh -F {ssh_config} <hostname>")

    def generate_ssh_keys(self) -> bool:
        """
        Generate SSH keys for connecting to VMs.

        Creates an SSH key pair in the workspace directory if it doesn't
        already exist.

        Returns:
            bool: True if successful, False otherwise
        """
        success = True
        ws_path = self.config.get('workspace', {}).get('path', '')

        for _, cluster in self._vm_clusters.items():
            base_path = os.path.expanduser(ws_path or cluster['workdir'])
            admin_key_name = cluster.get('admin_key_name', 'id_ed25519_boxman')

            admin_priv_key = os.path.join(base_path, admin_key_name)
            admin_pub_key = os.path.join(base_path, f"{admin_key_name}.pub")

            # create directory if it doesn't exist
            if not os.path.isdir(base_path):
                os.makedirs(base_path, exist_ok=True)

            # generate key pair if it doesn't exist
            if not os.path.exists(admin_priv_key):
                self.logger.info(f"generating ssh key pair in {base_path}")

                try:
                    cmd = f'ssh-keygen -t ed25519 -a 100 -f {admin_priv_key} -q -N ""'
                    run(cmd, hide=True, warn=True)

                    # verify keys were created
                    if os.path.isfile(admin_priv_key) and os.path.isfile(admin_pub_key):
                        self.logger.info(f"ssh key pair successfully generated at {admin_priv_key}")
                    else:
                        self.logger.warning(f"failed to generate ssh key pair at {admin_priv_key}")
                        success = False

                except Exception as exc:
                    self.logger.error(f"error generating ssh key pair: {exc}")
                    success = False
            else:
                self.logger.info(f"using existing ssh key pair at {admin_priv_key}")

        return success

    def get_global_authorized_keys(self) -> list[str]:
        """
        Resolve and return all global SSH authorized keys from the app config.

        Reads ``ssh.authorized_keys`` from :pyattr:`app_config` (the top-level
        ``boxman.yml``), resolves each entry via :pyfunc:`fetch_value`, and
        returns a list of public-key strings.

        Returns:
            List of resolved SSH public key strings.
        """
        raw_keys = (
            (self.app_config or {})
            .get("ssh", {})
            .get("authorized_keys", [])
        )
        resolved: list[str] = []
        for entry in raw_keys:
            try:
                resolved.append(self.fetch_value(entry))
            except (ValueError, FileNotFoundError) as exc:
                self.logger.warning(f"skipping unresolvable SSH key entry: {exc}")
        return resolved

    def write_global_authorized_keys_file(self, output_path: str) -> None:
        """
        Resolve global SSH keys from app_config and write them to a file.

        This bridges the Python-side boxman.yml config with the container
        entrypoint, which cannot read boxman.yml directly. The entrypoint
        reads ``global_authorized_keys`` from the bind-mounted ssh dir.

        Args:
            output_path: Path to write the authorized keys file
                         (e.g. ``<data_dir>/ssh/global_authorized_keys``).
        """
        keys = self.get_global_authorized_keys()
        if not keys:
            self.logger.info("no global authorized keys to write")
            return

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as fobj:
            for key in keys:
                fobj.write(key + "\n")
        self.logger.info(
            f"wrote {len(keys)} global authorized key(s) to {output_path}")

    @classmethod
    def fetch_value(cls, value) -> str:
        """
        Resolve a config reference (``${env:VAR}`` / ``file://…``) to its
        concrete value, or return the literal as-is.

        Thin wrapper around :func:`boxman.utils.references.resolve_reference`
        — kept on the class so external callers importing
        ``BoxmanManager.fetch_value`` keep working (Phase 2.4 extraction).

        Raises:
            ValueError: If the environment variable is not set.
            FileNotFoundError: If the referenced file does not exist.
        """
        return resolve_reference(value)

    def add_ssh_keys_to_vms(self) -> bool:
        """
        Add the generated SSH public key to all VMs to enable password-less login.

        Uses sshpass to add the public key to each VM using the admin password.

        Returns:
            bool: True if all VMs received the key successfully, False otherwise
        """
        all_successful = True
        ws_path = self.config.get('workspace', {}).get('path', '')

        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self._vm_clusters.items():
            base_path = os.path.expanduser(ws_path or cluster['workdir'])
            admin_key_name = cluster.get('admin_key_name', 'id_ed25519_boxman')
            admin_pub_key = os.path.join(base_path, f"{admin_key_name}.pub")

            admin_user = cluster.get('admin_user', 'admin')
            admin_pass = self.fetch_value(cluster.get('admin_pass', None))

            if not admin_pass:
                self.logger.info(
                    f"warning: No admin password provided for cluster {cluster_name}, "
                    "cannot add SSH keys")
                all_successful = False
                continue

            if not os.path.isfile(admin_pub_key):
                self.logger.error(f"error: SSH public key {admin_pub_key} does not exist")
                all_successful = False
                continue

            self.logger.info(f"adding ssh public key to VMs in cluster {cluster_name}")

            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                # get the ip addresses for this vm
                ip_addresses = self.session_for_cluster(cluster_name).get_vm_ip_addresses(full_vm_name)

                if not ip_addresses:
                    self.logger.warning(
                        f"no ip address available for vm {vm_name}, "
                        "cannot add ssh key")
                    all_successful = False
                    continue

                # use first available ip address
                ip_address = next(iter(ip_addresses.values()))

                self.logger.info(f"adding ssh key to vm {vm_name} ({ip_address})...")

                # try to add the key with exponential backoff
                hostname = vm_info.get('hostname', vm_name)
                prefixed_host = f"{cluster_name}_{hostname}"
                success = self._try_add_ssh_key(
                    ip_address=ip_address,
                    hostname=prefixed_host,
                    admin_user=admin_user,
                    admin_pass=admin_pass,
                    pub_key_path=admin_pub_key,
                    ssh_conf_path=os.path.join(base_path, cluster['ssh_config'])
                )

                if success:
                    self.logger.info(f"successfully added the ssh key to the vm {vm_name}")
                else:
                    self.logger.error(f"failed to add the ssh key to the vm {vm_name}")
                    all_successful = False

        return all_successful

    def _try_add_ssh_key(self,
                         ip_address: str,
                         hostname: str,
                         admin_user: str,
                         admin_pass: str,
                         pub_key_path: str,
                         ssh_conf_path: str) -> bool:
        """
        Try to add an SSH key to a VM with exponential backoff.

        Args:
            ip_address: IP address of the VM
            hostname: Hostname of the VM
            admin_user: Username for SSH login
            admin_pass: Password for SSH login
            pub_key_path: Path to the public key file
            ssh_conf_path: Path to the SSH config file

        Returns:
            bool: True if successful, False otherwise
        """
        wait_time = 1  # Start with 1 second
        max_retries = 10
        max_wait = 60  # Maximum wait per attempt

        for attempt in range(1, max_retries + 1):
            self.logger.info(
                f"attempt {attempt}/{max_retries} to add ssh key (waiting {wait_time}s)")

            # use sshpass to add the public key
            cmd = (
                f'sshpass -p {admin_pass} ssh-copy-id -i {pub_key_path} '
                f'-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null '
                f'{admin_user}@{ip_address}'
            )

            # When using a non-local runtime, the VM is only reachable from
            # inside the container, so wrap the command with docker exec.
            cmd = self.runtime_instance.wrap_command(cmd)

            result = run(cmd, hide=True, warn=True)

            if result.ok:
                # Log ssh-copy-id informational output
                if result.stdout.strip():
                    for line in result.stdout.strip().splitlines():
                        self.logger.info(f"ssh-copy-id: {line}")

                # verify we can ssh without password
                ssh_success = self._verify_ssh_connection(hostname, ssh_conf_path)

                if ssh_success:
                    return True
            else:
                # Log ssh-copy-id output — warning for retries, error on last attempt
                is_last = attempt == max_retries
                log_fn = self.logger.error if is_last else self.logger.warning
                combined = (result.stderr.strip() or result.stdout.strip())
                if combined:
                    for line in combined.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        log_fn(f"ssh-copy-id: {line}")
                else:
                    log_fn("ssh key addition failed (no output)")

            # wait before next attempt with exponential backoff
            time.sleep(wait_time)
            wait_time = min(wait_time * 2, max_wait)

        return False

    def _verify_ssh_connection(self, hostname: str, ssh_config_path: str) -> bool:
        """
        Verify SSH connection to a VM.

        Args:
            hostname: Hostname of the VM
            ssh_config_path: Path to the SSH config file

        Returns:
            bool: True if successful, False otherwise
        """
        self.logger.info(f"verifying ssh connection to: {hostname}")
        ssh_cmd = f'ssh -o BatchMode=yes -F {ssh_config_path} {hostname} hostname'

        # When using a non-local runtime, the VM is only reachable from
        # inside the container. The shared ssh_config contains a
        # ProxyJump directive aimed at the host-side published SSH port
        # (e.g. 127.0.0.1:2417), which is *not* reachable from inside
        # the container's own netns — there, 127.0.0.1:2417 is a dead
        # port. Override ProxyJump/ProxyCommand to "none" so the
        # in-container ssh hits the VM IP directly (via the `Hostname`
        # directive in the VM's Host block).
        if self._runtime_name != 'local':
            ssh_cmd = (
                f'ssh -o BatchMode=yes -o ProxyJump=none '
                f'-o ProxyCommand=none -F {ssh_config_path} '
                f'{hostname} hostname'
            )
        ssh_cmd = self.runtime_instance.wrap_command(ssh_cmd)

        result = run(ssh_cmd, hide=True, warn=True)

        if result.ok and result.stdout.strip():
            hostname_output = result.stdout.strip()
            self.logger.info(f"ssh connection verified: {hostname_output}")
            return True
        else:
            self.logger.error(f"ssh connection failed for {hostname}: {result.stderr.strip()}")
            return False

    def setup_ssh_access(self) -> bool:
        """
        Set up SSH access to all VMs.

        This method:
        1. Writes global authorized keys file (resolved from boxman.yml)
        2. Generates SSH keys if they don't exist
        3. Adds the public key to all vms
        4. Writes an SSH config file for easy access

        Returns:
            bool: True if all steps completed successfully, False otherwise
        """
        # write global authorized keys so they can be consumed by container
        # entrypoints or cloud-init scripts
        for _, cluster in self._vm_clusters.items():
            workdir = os.path.expanduser(cluster['workdir'])
            global_keys_path = os.path.join(workdir, 'global_authorized_keys')
            self.write_global_authorized_keys_file(global_keys_path)

        if not self.generate_ssh_keys():
            self.logger.error("failed to generate ssh keys")
            return False

        self.write_ssh_config()

        if not self.add_ssh_keys_to_vms():
            self.logger.error("failed to add ssh keys to some vms")
            return False

        self.logger.info("")
        self.logger.info("ssh access setup complete")
        self.logger.info("you can now connect to vms using the ssh config file")

        return True
