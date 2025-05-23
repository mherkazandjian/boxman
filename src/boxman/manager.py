import os
import time
import re
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

    @classmethod
    def full_network_name(cls,
                          project_config: Dict[str, Any],
                          cluster_name: str = None,
                          network_name: str = None) -> str:
        """
        Return the computed network name based on how it is resolved in the name string.

        The network name is expected to have the following format:

            <project_name>::<cluster_name>::<base_network_name>

        The project prefix is always added to the network name.

           prj_name = f'bprj__{project_config["project"]}__bprj'

        The cluster name is added after the project name as such

           cluster_name = f'clstr__{cluster_name}__clstr'

        Finally the network name is added as such

            full_network_name = f'{prj_name}__{cluster_name}__{base_network_name}'

        Args:
            project_config (Dict[str, Any]): The project configuration dictionary.
            network_name (str): The network name string, e.g. 'base_name',
              'cluster_name::base_name', or 'project_name::cluster_name::base_name'.

        Returns:
            str: The fully qualified network name.
        """
        parts = network_name.split("::")

        if len(parts) == 3:           # project, cluster, base
            _project, _cluster_name, _base_name = parts
            retval = f'bprj__{_project}__bprj'
            retval = retval + f'__clstr__{_cluster_name}__clstr'
            retval = retval + f'__{_base_name}'
            return retval
        elif len(parts) == 2:         # cluster, base
            _cluster_name, _base_name = parts
            retval = f'bprj__{project_config["project"]}__bprj'
            retval = retval + f'__clstr__{_cluster_name}__clstr'
            retval = retval + f'__{_base_name}'
            return retval
        elif len(parts) == 1:         # base only
            _base_name = parts[0]
            retval = f'bprj__{project_config["project"]}__bprj'
            retval = retval + f'__clstr__{cluster_name}__clstr'
            retval = retval + f'__{_base_name}'
            return retval
        else:
            raise ValueError(f"Invalid network name format: {network_name}")

    ### networks define / remove / destroy
    def define_networks(self) -> None:
        """
        Define the networks specified in the cluster configuration.
        """
        for cluster_name, cluster in self.config['clusters'].items():
            for network_name, network_info in cluster['networks'].items():

                _network_name = self.full_network_name(
                    project_config=self.config,
                    cluster_name=cluster_name,
                    network_name=network_name
                )

                self.provider.define_network(
                    name=_network_name,
                    info=network_info,
                    workdir=cluster['workdir']
                )
                self.logger.info(f"defined network {_network_name} in {cluster['workdir']}")

    def destroy_networks(self) -> None:
        """
        Destroy the networks specified in the cluster configuration.
        """
        for cluster_name, cluster in self.config['clusters'].items():
            for network_name, network_info in cluster['networks'].items():

                _network_name = self.full_network_name(
                    project_config=self.config,
                    cluster_name=cluster_name,
                    network_name=network_name
                )

                self.provider.remove_network(
                    name=_network_name,
                    info=network_info
                )
                self.logger.info(f"removed network {_network_name} in {cluster['workdir']}")
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
            self.logger.info(f"cloning vm {new_vm_name} from base image {cluster['base_image']}")
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
                vm_info = vm_info.copy()

                self.logger.info(
                    f"configuring network interfaces for VM {vm_name} in cluster {cluster_name}")

                if 'network_adapters' not in vm_info:
                    self.logger.warning(f"no network adapters defined for vm {vm_name}, skipping")
                    continue
                else:
                    # use the fully qualified network name to which the adapter will connect to
                    for adapter in vm_info['network_adapters']:

                        full_network_name = self.full_network_name(
                            project_config=self.config,
                            cluster_name=cluster_name,
                            network_name=adapter['network_source']
                        )

                        adapter['network_source'] = full_network_name

                success = self.provider.configure_vm_network_interfaces(
                    vm_name=full_vm_name,
                    network_adapters=vm_info['network_adapters']
                )

                if success:
                    self.logger.info(
                        f"All network interfaces configured successfully for vm {vm_name}")
                else:
                    self.logger.warning(
                        f"Some network interfaces could not be configured for vm {vm_name}")

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

                self.logger.info(f"configuring disks for vm {vm_name} in cluster {cluster_name}")

                # check if there are disks defined in the VM configuration
                if 'disks' not in vm_info or not vm_info['disks']:
                    self.logger.warning(f"no disks defined for vm {vm_name}, skipping")
                    continue

                # configure all disks for this vm
                success = self.provider.configure_vm_disks(
                    vm_name=full_vm_name,
                    disks=vm_info['disks'],
                    workdir=workdir,
                    disk_prefix=full_vm_name)

                if success:
                    self.logger.info(f"all disks configured successfully for vm {vm_name}")
                else:
                    self.logger.warning(f"some disks could not be configured for vm {vm_name}")

    def configure_cpu_mem(self) -> None:
        """
        Configure CPU and memory settings for all VMs based on their configuration.

        This method modifies the CPU and memory settings of VMs after they have been cloned.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                self.logger.info(
                    f"configuring CPU and memory for vm {vm_name} in cluster {cluster_name}")

                # extract CPU and memory configuration
                cpus = vm_info.get('cpus')
                memory = vm_info.get('memory')

                # skip if neither CPU nor memory is configured
                if not cpus and not memory:
                    self.logger.warning(
                        f"no CPU or memory configuration for vm {vm_name}, skipping")
                    continue

                # configure CPU and memory
                success = self.provider.configure_vm_cpu_memory(
                    vm_name=full_vm_name,
                    cpus=cpus,
                    memory_mb=memory
                )

                if success:
                    self.logger.info(f"successfully configured CPU and memory for vm {vm_name}")
                else:
                    self.logger.warning(f"failed to configure CPU and memory for vm {vm_name}")

    def start_vms(self) -> None:
        """
        Start all VMs in the configuration.

        This powers on all VMs after they have been configured.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"

                self.logger.info(f"starting vm {full_vm_name}")
                success = self.provider.start_vm(full_vm_name)

                if success:
                    self.logger.info(f"successfully started the vm {full_vm_name}")
                else:
                    self.logger.warning(f"failed to start the vm {full_vm_name}")

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

                # get the ip addresses for this vm
                ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

                # if no ip addresses found, mark as failure
                if not ip_addresses:
                    all_vms_have_ip = False
                    self.logger.warning(f"vm {full_vm_name} does not have an ip address yet")

        return all_vms_have_ip

    def connect_info(self) -> None:
        """
        Display connection information for all VMs in all clusters.

        This method displays the VM names, hostnames, IP addresses, and
        other connection details for all configured VMs.
        """
        self.logger.info("=== vm connection information ===")

        prj_name = f'bprj__{self.config["project"]}__bprj'
        for cluster_name, cluster in self.config['clusters'].items():
            self.logger.info(f"cluster: {cluster_name}")
            self.logger.info("-" * 60)

            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                hostname = vm_info.get('hostname', vm_name)

                self.logger.info(f"vm: {vm_name} (hostname: {hostname})")

                # get the ip addresses for all interfaces
                ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

                if ip_addresses:
                    self.logger.info("  ip addresses:")
                    for iface, ip in ip_addresses.items():
                        self.logger.info(f"    {iface}: {ip}")
                else:
                    self.logger.info("  ip addresses: not available")

                # get ssh connection information
                admin_user = cluster.get('admin_user', '<placeholder>')
                admin_key = os.path.expanduser(os.path.join(
                    cluster.get('workdir', '~'),
                    cluster.get('admin_key_name', 'id_ed25519_boxman')
                ))

                self.logger.info("  connect via ssh:")
                # show direct connection if ip is available
                if ip_addresses:
                    first_ip = next(iter(ip_addresses.values()))
                    self.logger.info(f"    direct: ssh -i {admin_key} {admin_user}@{first_ip}")

                # show connection using ssh_config if available
                if 'ssh_config' in cluster:
                    ssh_config = os.path.expanduser(os.path.join(
                        cluster.get('workdir', '~'),
                        cluster.get('ssh_config', 'ssh_config')
                    ))
                    self.logger.info(f"    via config: ssh -F {ssh_config} {hostname}")

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
            # get the ssh config path
            ssh_config = os.path.expanduser(os.path.join(
                cluster.get('workdir', '~'),
                cluster.get('ssh_config', 'ssh_config')
            ))

            admin_priv_key = os.path.expanduser(os.path.join(
                cluster.get('workdir', '~'),
                cluster.get('admin_key_name', 'id_ed25519_boxman')
            ))

            self.logger.info(f"writing ssh config to {ssh_config}")

            with open(ssh_config, 'w') as fobj:
                # write global SSH options
                fobj.write('Host *\n')
                fobj.write('    StrictHostKeyChecking no\n')
                fobj.write('    UserKnownHostsFile /dev/null\n')
                fobj.write('\n\n')

                # write host-specific configurations
                for vm_name, vm_info in cluster['vms'].items():
                    full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                    hostname = vm_info.get('hostname', vm_name)

                    # get the first ip address if available
                    ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

                    if ip_addresses:
                        first_ip = next(iter(ip_addresses.values()))

                        fobj.write(f'Host {hostname}\n')
                        fobj.write(f'    Hostname {first_ip}\n')
                        fobj.write(f'    User {cluster.get("admin_user", "admin")}\n')
                        fobj.write(f'    IdentityFile {admin_priv_key}\n')
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
                self.logger.info(f"generating ssh key pair in {workdir}")

                try:
                    cmd = f'ssh-keygen -t ed25519 -a 100 -f {admin_priv_key} -q -N ""'
                    result = run(cmd, hide=True, warn=True)

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

    @classmethod
    def fetch_value(cls, value) -> str:
        """
        Get the value referenced by *value*.

        supported reference formats:
          - Environment variable:  '${env:ENV_VAR_NAME}'
          - File contents:         'file:///abs/or/relative/path'
          -                        'file://~/path/to/file'

        for any other string the input is returned unchanged.

        Raises:
            ValueError: If the environment variable is not set.
            FileNotFoundError: If the referenced file does not exist.
        """
        if isinstance(value, str):
            # ${env:VAR}
            env_match = re.fullmatch(r"\$\{env:(.+)\}", value)
            if env_match:
                var = env_match.group(1)
                if var not in os.environ:
                    raise ValueError(f"environment variable '{var}' is not set")
                return os.environ[var]

            # file:///path/to/file
            if value.startswith("file://"):
                path = os.path.expanduser(value[len("file://"):])
                if not os.path.isfile(path):
                    raise FileNotFoundError(f"referenced file does not exist: {path}")
                with open(path, "r") as fobj:
                    return fobj.read().rstrip("\n")

        # default: return as-is
        return value

    def add_ssh_keys_to_vms(self) -> bool:
        """
        Add the generated SSH public key to all VMs to enable password-less login.

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
                ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

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
                success = self._try_add_ssh_key(
                    ip_address=ip_address,
                    hostname=vm_info['hostname'],
                    admin_user=admin_user,
                    admin_pass=admin_pass,
                    pub_key_path=admin_pub_key,
                    ssh_conf_path=os.path.join(workdir, cluster['ssh_config'])
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
        max_retries = 5
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

            result = run(cmd, hide=False, warn=True)

            if result.ok:
                # verify we can ssh without password
                ssh_success = self._verify_ssh_connection(hostname, ssh_conf_path)

                if ssh_success:
                    return True
            else:
                self.logger.error(f"ssh key addition failed: {result.stderr.strip()}")

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
        ssh_cmd = f'ssh -F {ssh_config_path} {hostname} hostname'
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
        1. Generates SSH keys if they don't exist
        2. Adds the public key to all vms
        3. Writes an SSH config file for easy access

        Returns:
            bool: True if all steps completed successfully, False otherwise
        """
        if not self.generate_ssh_keys():
            self.logger.error("failed to generate ssh keys")
            return False

        self.write_ssh_config()

        if not self.add_ssh_keys_to_vms():
            self.logger.error("failed to add ssh keys to some vms")
            return False

        self.logger.info("\nssh access setup complete")
        self.logger.info("you can now connect to vms using the ssh config file")

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

        cls.configure_cpu_mem()

        cls.configure_network_interfaces()

        cls.configure_disks()

        cls.start_vms()

        # use adaptive wait for ip address assignment
        cls.logger.info("waiting for vms to initialize and get the ip addresses...")
        wait_time = 1  # Start with 1 second
        max_wait = 600  # Maximum total wait time (10 minutes)
        total_waited = 0

        while total_waited < max_wait:
            # check if all vms have ip addresses
            if cls.get_connect_info():
                cls.logger.info(f"All vms have ip addresses (waited {total_waited}s)")
                break

            # if we get here, at least one vm doesn't have an ip yet
            cls.logger.info(f"wait {wait_time}s for ip assignment (total waited: {total_waited}s)")
            time.sleep(wait_time)
            total_waited += wait_time
            # double the wait time up to 1 minute max per iteration
            wait_time = min(wait_time * 2, 60)

        if total_waited >= max_wait:
            cls.logger.warning(
                "Reached maximum wait time. Some vms may not have ip addresses.")

        # display connection information
        cls.connect_info()

        # generate ssh keys, add them to vms, and write ssh config
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

        cls.destroy_networks()
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
                    vm_dir=os.path.expanduser(cluster['workdir']),
                    snapshot_name=cli_args.snapshot_name,
                    description=cli_args.snapshot_descr)

    @staticmethod
    def snapshot_restore(cls, cli_args):
        """
        Restore the state of the VMs in the cluster from a snapshot.
        """
        if not cli_args.snapshot_name:
           cls.logger.error("error: snapshot name is required")
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
            cls.logger.error("error: Snapshot name is required")
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
        prj_name = f'bprj__{self.config["project"]}__bprj'
        if hasattr(cli_args, 'vms') and cli_args.vms:
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
            cls.logger.info(f"vm {vm_name} suspended")

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
