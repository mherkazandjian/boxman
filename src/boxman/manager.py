import os
import time
from typing import Dict, Any, Optional
import yaml
from multiprocessing import Pool
from multiprocessing import Process
from invoke import run

from boxman.providers.libvirt.session import LibVirtSession
from boxman.utils.io import write_files
from boxman import log

class BoxmanManager:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the BoxmanManager.

        Args:
            config: Optional configuration dictionary or path to config file
        """
        #: Optional[str]: the path to the configuration file if one was provided
        self.config_path: Optional[str] = None

        #: Optional[Dict[str, Any]]: the loaded configuration dictionary
        self.config: Optional[Dict[str, Any]] = None

        #: the private backing field for the provider property
        self._provider = None

        #: the logger instance
        self.logger = log

        if isinstance(config, str):
            self.config_path = config
            self.config = self.load_config(config)

    @property
    def provider(self) -> Optional["LibVirtSession"]:
        """
        Get the current provider session.

        Returns:
            The provider session instance or None if not initialized
        """
        return self._provider

    @provider.setter
    def provider(self, value: "LibVirtSession") -> None:
        """
        Set the provider session.

        Args:
            value: The provider session instance
        """
        self._provider = value

    def load_config(self, config_path: str) -> Dict[str, Any]:
        """
        Load configuration from a YAML file.

        Args:
            config_path: Path to the configuration file

        Returns:
            Dict containing the configuration
        """
        # Load the configuration file
        with open(config_path) as fobj:
            conf: Dict[str, Any] = yaml.safe_load(fobj.read())
        return conf

    def provision_files(self) -> None:
        """
        Provision files specified in the cluster configuration.
        """
        clusters = self.config['clusters']
        for cluster_name, cluster in clusters.items():
            if files := cluster.get('files'):
                write_files(files, rootdir=cluster['workdir'])

    ### networks define / remove / destroy
    def define_networks(self) -> None:
        """
        Define the networks specified in the cluster configuration.
        """
        for cluster_name, cluster in self.config['clusters'].items():
            for network_name, network_info in cluster['networks'].items():
                _network_name = f'{cluster_name}_{network_name}'
                self.provider.define_network(
                    name=_network_name,
                    info=network_info,
                    workdir=cluster['workdir']
                )

    def destroy_networks(self) -> None:
        """
        Destroy the networks specified in the cluster configuration.
        """
        for cluster_name, cluster in self.config['clusters'].items():
            for network_name in cluster['networks'].keys():
                self.provider.remove_network(cluster_name, network_name)
    ### end networks define / remove / destroy

    ### vms define / remove / destroy
    def clone_vms(self) -> None:
        """
        Clone the VMs defined in the configuration.

        The following is done for every vm in every cluster

            - remove the vm
            - clone the vm
        """
        def vm_clone_tasks():
            prj_name = f'bprj__{self.config["project"]}__bprj'
            for cluster_name, cluster in self.config['clusters'].items():
                for vm_name, vm_info in cluster['vms'].items():
                    vm_info = vm_info.copy()
                    new_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                    yield cluster, vm_info, new_vm_name

        def _clone(cluster, vm_info, new_vm_name):
            self.provider.clone_vm(
                src_vm_name=cluster['base_image'],
                new_vm_name=new_vm_name,
                info=vm_info,
                workdir=cluster['workdir']
            )

        # clone the vms one at a time
        for cluster, vm_info, new_vm_name in vm_clone_tasks():
            self.logger.info(f"Cloning VM {new_vm_name} from base image {cluster['base_image']}")
            _clone(cluster, vm_info, new_vm_name)
            time.sleep(1)  # Add a small delay to avoid overwhelming the provider

        # optionally use multiprocessing to speed up the cloning process
        #processes = [
        #    Process(target=_clone, args=(cluster, vm_info, new_vm_name))
        #    for cluster, vm_info, new_vm_name in vm_clone_tasks()
        #]
        #[p.start() for p in processes]
        #[p.join() for p in processes]

    def destroy_vms(self) -> None:
        """
        Destroy the VMs specified in the cluster configuration.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        def vm_destroy_tasks():
            for cluster_name, cluster in self.config['clusters'].items():
                for vm_name in cluster['vms'].keys():
                    full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                    yield full_vm_name, cluster_name, vm_name

        def _destroy(full_vm_name, cluster_name, vm_name):
            self.logger.info(f"Destroying VM {vm_name} in cluster {cluster_name}")
            self.provider.destroy_vm(
                name=full_vm_name,
                remove_storage=True
            )

        processes = [
            Process(target=_destroy, args=(full_vm_name, cluster_name, vm_name))
            for full_vm_name, cluster_name, vm_name in vm_destroy_tasks()
        ]
        [p.start() for p in processes]
        [p.join() for p in processes]
    ### end vms define / remove / destroy

    def configure_network_interfaces(self) -> None:
        """
        Configure network interfaces for all VMs based on their network_adapters configuration.

        This method adds network interfaces to VMs after they have been cloned,
        connecting them to the appropriate networks.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                self.logger.info(f"Configuring network interfaces for VM {vm_name} in cluster {cluster_name}")

                if 'network_adapters' not in vm_info:
                    self.logger.warning(f"No network adapters defined for VM {vm_name}, skipping")
                    continue

                success = self.provider.configure_vm_network_interfaces(
                    vm_name=full_vm_name,
                    network_adapters=vm_info['network_adapters']
                )

                if success:
                    self.logger.info(f"All network interfaces configured successfully for VM {vm_name}")
                else:
                    self.logger.warning(f"Some network interfaces could not be configured for VM {vm_name}")

    def configure_disks(self) -> None:
        """
        Configure disks for all VMs based on their disks configuration.

        This method creates and attaches disks to VMs after they have been cloned.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            workdir = cluster.get('workdir', '.')

            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                self.logger.info(f"Configuring disks for VM {vm_name} in cluster {cluster_name}")

                # Check if there are disks defined in the VM configuration
                if 'disks' not in vm_info or not vm_info['disks']:
                    self.logger.warning(f"No disks defined for VM {vm_name}, skipping")
                    continue

                # Configure all disks for this VM
                success = self.provider.configure_vm_disks(
                    vm_name=full_vm_name,
                    disks=vm_info['disks'],
                    workdir=workdir,
                    disk_prefix=full_vm_name)

                if success:
                    self.logger.info(f"All disks configured successfully for VM {vm_name}")
                else:
                    self.logger.warning(f"Some disks could not be configured for VM {vm_name}")

    def start_vms(self) -> None:
        """
        Start all VMs in the configuration.

        This powers on all VMs after they have been configured.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                self.logger.info(f"Starting VM {vm_name}")
                success = self.provider.start_vm(full_vm_name)

                if success:
                    self.logger.info(f"Successfully started VM {vm_name}")
                else:
                    self.logger.warning(f"Failed to start VM {vm_name}")

    def get_connect_info(self) -> bool:
        """
        Gather connection information for all VMs in all clusters.

        This method attempts to get IP addresses for all VMs and returns
        True only if all VMs have at least one IP address.

        Returns:
            True if all VMs have at least one IP address, False otherwise
        """
        all_vms_have_ip = True

        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                # Get IP addresses for this VM
                ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

                # If no IP addresses found, mark as failure
                if not ip_addresses:
                    all_vms_have_ip = False
                    self.logger.warning(f"VM {vm_name} does not have an IP address yet")

        return all_vms_have_ip

    def connect_info(self) -> None:
        """
        Display connection information for all VMs in all clusters.

        This method displays the VM names, hostnames, IP addresses, and
        other connection details for all configured VMs.
        """
        self.logger.info("\n=== VM Connection Information ===\n")

        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            self.logger.info(f"Cluster: {cluster_name}")
            self.logger.info("-" * 60)

            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                hostname = vm_info.get('hostname', vm_name)

                self.logger.info(f"VM: {vm_name} (hostname: {hostname})")

                # Get IP addresses for all interfaces
                ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

                if ip_addresses:
                    self.logger.info("  IP Addresses:")
                    for iface, ip in ip_addresses.items():
                        self.logger.info(f"    {iface}: {ip}")
                else:
                    self.logger.info("  IP Addresses: Not available")

                # Get SSH connection information
                admin_user = cluster.get('admin_user', '<placeholder>')
                admin_key = os.path.expanduser(os.path.join(
                    cluster.get('workdir', '~'),
                    cluster.get('admin_key_name', 'id_ed25519_boxman')
                ))

                self.logger.info("  Connect via SSH:")
                # Show direct connection if IP is available
                if ip_addresses:
                    first_ip = next(iter(ip_addresses.values()))
                    self.logger.info(f"    Direct: ssh -i {admin_key} {admin_user}@{first_ip}")

                # Show connection using ssh_config if available
                if 'ssh_config' in cluster:
                    ssh_config = os.path.expanduser(os.path.join(
                        cluster.get('workdir', '~'),
                        cluster.get('ssh_config', 'ssh_config')
                    ))
                    self.logger.info(f"    Via config: ssh -F {ssh_config} {hostname}")

                self.logger.info("")

            self.logger.info("")

    def write_ssh_config(self) -> None:
        """
        Generate SSH configuration file for easy access to VMs.

        Creates an SSH config file in the workdir of each cluster that allows
        simplified access to VMs without typing full connection details.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            # Get the SSH config path
            ssh_config = os.path.expanduser(os.path.join(
                cluster.get('workdir', '~'),
                cluster.get('ssh_config', 'ssh_config')
            ))

            admin_priv_key = os.path.expanduser(os.path.join(
                cluster.get('workdir', '~'),
                cluster.get('admin_key_name', 'id_ed25519_boxman')
            ))

            self.logger.info(f"Writing SSH config to {ssh_config}")

            with open(ssh_config, 'w') as fobj:
                # Write global SSH options
                fobj.write('Host *\n')
                fobj.write('    StrictHostKeyChecking no\n')
                fobj.write('    UserKnownHostsFile /dev/null\n')
                fobj.write('\n\n')

                # Write host-specific configurations
                for vm_name, vm_info in cluster['vms'].items():
                    full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                    hostname = vm_info.get('hostname', vm_name)

                    # Get the first IP address if available
                    ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

                    if ip_addresses:
                        first_ip = next(iter(ip_addresses.values()))

                        fobj.write(f'Host {hostname}\n')
                        fobj.write(f'    Hostname {first_ip}\n')
                        fobj.write(f'    User {cluster.get("admin_user", "admin")}\n')
                        fobj.write(f'    IdentityFile {admin_priv_key}\n')
                        fobj.write('\n\n')
                    else:
                        self.logger.warning(f"No IP address available for VM {vm_name}, skipping SSH config entry")

            self.logger.info(f"SSH config file written to {ssh_config}")
            self.logger.info(f"To connect: ssh -F {ssh_config} <hostname>")

    def generate_ssh_keys(self) -> bool:
        """
        Generate SSH keys for connecting to VMs.

        Creates an SSH key pair in each cluster's workdir if it doesn't already exist.

        Returns:
            bool: True if successful, False otherwise
        """
        success = True

        for _, cluster in self.config['clusters'].items():
            workdir = os.path.expanduser(cluster['workdir'])
            admin_key_name = cluster.get('admin_key_name', 'id_ed25519_boxman')

            admin_priv_key = os.path.join(workdir, admin_key_name)
            admin_pub_key = os.path.join(workdir, f"{admin_key_name}.pub")

            # create workdir if it doesn't exist
            if not os.path.isdir(workdir):
                os.makedirs(workdir, exist_ok=True)

            # generate key pair if it doesn't exist
            if not os.path.exists(admin_priv_key):
                self.logger.info(f"generating SSH key pair in {workdir}")

                try:
                    cmd = f'ssh-keygen -t ed25519 -a 100 -f {admin_priv_key} -q -N ""'
                    result = run(cmd, hide=True, warn=True)

                    # verify keys were created
                    if os.path.isfile(admin_priv_key) and os.path.isfile(admin_pub_key):
                        self.logger.info(f"SSH key pair successfully generated at {admin_priv_key}")
                    else:
                        self.logger.warning(f"Failed to generate SSH key pair at {admin_priv_key}")
                        success = False

                except Exception as e:
                    self.logger.error(f"Error generating SSH key pair: {e}")
                    success = False
            else:
                self.logger.info(f"Using existing SSH key pair at {admin_priv_key}")

        return success

    def add_ssh_keys_to_vms(self) -> bool:
        """
        Add the generated SSH public key to all VMs to enable passwordless login.

        Uses sshpass to add the public key to each VM using the admin password.

        Returns:
            bool: True if all VMs received the key successfully, False otherwise
        """
        all_successful = True

        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            workdir = os.path.expanduser(cluster['workdir'])
            admin_key_name = cluster.get('admin_key_name', 'id_ed25519_boxman')
            admin_pub_key = os.path.join(workdir, f"{admin_key_name}.pub")
            admin_user = cluster.get('admin_user', 'admin')
            admin_pass = cluster.get('admin_pass', '')

            if not admin_pass:
                self.logger.info(f"Warning: No admin password provided for cluster {cluster_name}, cannot add SSH keys")
                all_successful = False
                continue

            if not os.path.isfile(admin_pub_key):
                self.logger.error(f"Error: SSH public key {admin_pub_key} does not exist")
                all_successful = False
                continue

            self.logger.info(f"Adding SSH public key to VMs in cluster {cluster_name}")

            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                # get IP addresses for this VM
                ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

                if not ip_addresses:
                    self.logger.warning(f"No IP address available for VM {vm_name}, cannot add SSH key")
                    all_successful = False
                    continue

                # use first available IP address
                ip_address = next(iter(ip_addresses.values()))

                self.logger.info(f"Adding SSH key to VM {vm_name} ({ip_address})...")

                # try to add the key with exponential backoff
                success = self._try_add_ssh_key(
                    ip_address=ip_address,
                    hostname=vm_info['hostname'],
                    admin_user=admin_user,
                    admin_pass=admin_pass,
                    pub_key_path=admin_pub_key,
                    ssh_conf_path=os.path.join(workdir, cluster['ssh_config'])
                )

                if success:
                    self.logger.info(f"Successfully added SSH key to VM {vm_name}")
                else:
                    self.logger.error(f"Failed to add SSH key to VM {vm_name}")
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
        max_retries = 5
        max_wait = 60  # Maximum wait per attempt

        for attempt in range(1, max_retries + 1):
            self.logger.info(f"Attempt {attempt}/{max_retries} to add SSH key (waiting {wait_time}s)")

            # use sshpass to add the public key
            cmd = (
                f'sshpass -p {admin_pass} ssh-copy-id -i {pub_key_path} '
                f'-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null '
                f'{admin_user}@{ip_address}'
            )

            result = run(cmd, hide=False, warn=True)

            if result.ok:
                # verify we can SSH without password
                ssh_success = self._verify_ssh_connection(hostname, ssh_conf_path)

                if ssh_success:
                    return True
            else:
                self.logger.error(f"SSH key addition failed: {result.stderr.strip()}")

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
        self.logger.info(f"verifying SSH connection to: {hostname}")
        ssh_cmd = f'ssh -F {ssh_config_path} {hostname} hostname'
        result = run(ssh_cmd, hide=True, warn=True)

        if result.ok and result.stdout.strip():
            hostname_output = result.stdout.strip()
            self.logger.info(f"SSH connection verified: {hostname_output}")
            return True
        else:
            self.logger.error(f"SSH connection failed for {hostname}: {result.stderr.strip()}")
            return False

    def setup_ssh_access(self) -> bool:
        """
        Set up SSH access to all VMs.

        This method:
        1. Generates SSH keys if they don't exist
        2. Adds the public key to all VMs
        3. Writes an SSH config file for easy access

        Returns:
            bool: True if all steps completed successfully, False otherwise
        """
        if not self.generate_ssh_keys():
            self.logger.error("Failed to generate SSH keys")
            return False

        self.write_ssh_config()

        if not self.add_ssh_keys_to_vms():
            self.logger.error("Failed to add SSH keys to some VMs")
            return False

        self.logger.info("\nSSH access setup complete")
        self.logger.info("You can now connect to VMs using the SSH config file")

        return True

    @staticmethod
    def provision(cls, cli_args):

        cls.deprovision(cls, cli_args)

        config = cls.config
        cluster_group = list(config['clusters'].keys())[0]  # one cluster supported for now
        # -------------------- global config ---------------------------

        cluster = config['clusters'][cluster_group]
        ssh_config = cluster['ssh_config']
        workdir = cluster['workdir']
        # -------------------- end global config -----------------------

        ssh_config = os.path.expanduser(os.path.join(workdir, ssh_config))
        workdir = os.path.abspath(os.path.expanduser(workdir))
        if not os.path.isdir(workdir):
            os.makedirs(workdir)

        cls.provision_files()

        cls.define_networks()

        cls.clone_vms()

        cls.configure_network_interfaces()

        cls.configure_disks()

        cls.start_vms()

        # Use adaptive wait for IP address assignment
        cls.logger.info("Waiting for VMs to initialize and get IP addresses...")
        wait_time = 1  # Start with 1 second
        max_wait = 600  # Maximum total wait time (10 minutes)
        total_waited = 0

        while total_waited < max_wait:
            # Check if all VMs have IP addresses
            if cls.get_connect_info():
                cls.logger.info(f"All VMs have IP addresses (waited {total_waited}s)")
                break

            # If we get here, at least one VM doesn't have an IP yet
            cls.logger.info(f"Waiting {wait_time}s for IP assignment (total waited: {total_waited}s)")
            time.sleep(wait_time)
            total_waited += wait_time
            wait_time = min(wait_time * 2, 60)  # Double the wait time up to 1 minute max per iteration

        if total_waited >= max_wait:
            cls.logger.warning("Warning: Reached maximum wait time. Some VMs may not have IP addresses.")

        # Display connection information
        cls.connect_info()

        # Generate SSH keys, add them to VMs, and write SSH config
        cls.setup_ssh_access()

    @staticmethod
    def deprovision(cls, cli_args):

        config = cls.config
        cluster_group = list(config['clusters'].keys())[0]  # one cluster supported for now
        # -------------------- global config ---------------------------

        cluster = config['clusters'][cluster_group]
        cluster_name = cluster_group
        ssh_config = cluster['ssh_config']
        workdir = cluster['workdir']
        # -------------------- end global config -----------------------

        ssh_config = os.path.expanduser(os.path.join(workdir, ssh_config))
        workdir = os.path.abspath(os.path.expanduser(workdir))


        cls.destroy_networks()

        prj_name = f'bprj__{cls.config["project"]}__bprj'
        for cluster_name, cluster in cls.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():

                vm_info = vm_info.copy()
                new_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                cls.provider.destroy_vm(new_vm_name)

                cls.provider.destroy_disks(
                    cluster['workdir'],
                    vm_name=new_vm_name,
                    disks=vm_info['disks']
                )

        # .. todo:: implement undo'ing the provisioning of the files (not important for now)
        #cls.provision_files()

        return

    ### start snapshot functions ####
    @staticmethod
    def snapshot_list(cls, cli_args):
        """
        List snapshots of the VMs in the cluster.
        """
        prj_name = f'bprj__{cls.config["project"]}__bprj'
        for cluster_name, cluster in cls.config['clusters'].items():
            for vm_name, _ in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                cls.provider.snapshot_list(full_vm_name)

    @staticmethod
    def snapshot_take(cls, cli_args):
        """
        Take a snapshot of the VMs in the cluster.
        """
        prj_name = f'bprj__{cls.config["project"]}__bprj'
        for cluster_name, cluster in cls.config['clusters'].items():
            for vm_name, _ in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                cls.provider.snapshot_take(
                    vm_name=full_vm_name,
                    snapshot_name=cli_args.snapshot_name,
                    description=cli_args.snapshot_descr)

    @staticmethod
    def snapshot_restore(cls, cli_args):
        """
        Restore the state of the VMs in the cluster from a snapshot.
        """
        if not cli_args.snapshot_name:
           cls.logger.error("Error: Snapshot name is required")
           return

        prj_name = f'bprj__{cls.config["project"]}__bprj'
        for cluster_name, cluster in cls.config['clusters'].items():
            for vm_name, _ in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                cls.provider.snapshot_restore(full_vm_name, cli_args.snapshot_name)


    @staticmethod
    def snapshot_delete(cls, cli_args):
        """
        Delete a snapshot of the VMs in the cluster.
        """
        if not cli_args.snapshot_name:
            cls.logger.error("Error: Snapshot name is required")
            return

        prj_name = f'bprj__{cls.config["project"]}__bprj'
        for cluster_name, cluster in cls.config['clusters'].items():
            for vm_name, _ in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                cls.provider.snapshot_delete(full_vm_name, cli_args.snapshot_name)
                cls.logger.info(f"Snapshot {cli_args.snapshot_name} deleted for VM {full_vm_name}")
    ### end snapshot functions ####

    ### start control vm functions ####
    def process_vm_list(self, cli_args):
        """
        Process the list of VMs to control.
        """
        retval = []
        prj_name = f'bprj__{cls.config["project"]}__bprj'
        if hasattr(cli_args, 'vms') and cli_args.vms:
            _vm_names = cli_args.vms.split(',')
            for cluster_name, cluster in self.config['clusters'].items():
               workdir = os.path.expanduser(cluster['workdir'])
               for vm_name, _ in cluster['vms'].items():
                   full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                   retval.append((full_vm_name, workdir))
        else:
            vm_names = []
            for cluster_name, cluster in self.config['clusters'].items():
                workdir = os.path.expanduser(cluster['workdir'])
                for vm_name, _ in cluster['vms'].items():
                    full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                    vm_names.append((full_vm_name, workdir))

        return retval

    @staticmethod
    def suspend_vm(cls, cli_args):
        """
        Suspend (pause) the VMs in the cluster.
        """
        for vm_name, _ in cls.process_vm_list(cli_args):
            cls.provider.suspend_vm(vm_name)
            cls.logger.info(f"VM {vm_name} suspended")

    @staticmethod
    def resume_vm(cls, cli_args):
        """
        Resume previously suspended VMs in the cluster.
        """
        for vm_name, _ in cls.process_vm_list(cli_args):
            cls.provider.resume_vm(vm_name)
            cls.logger.info(f"VM {vm_name} resumed")

    @staticmethod
    def save_vm(cls, cli_args):
        """
        Save the state of the VMs in the cluster to a file.
        """
        for vm_name, workdir in cls.process_vm_list(cli_args):
            cls.provider.save_vm(vm_name, workdir)

    @staticmethod
    def start_vm(cls, cli_args):
        """
        Start VMs in the cluster.
        """
        for vm_name, workdir in cls.process_vm_list(cli_args):
            if cli_args.restore:
                cls.provider.restore_vm(vm_name, workdir)
            else:
                cls.provider.start_vm(vm_name)
    ### end control vm functions ####