import os
import yaml
from typing import Dict, Any, Optional, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from boxman.providers.libvirt.session import LibVirtSession

from boxman.utils.io import write_files

class BoxmanManager:
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the BoxmanManager.

        Args:
            config: Optional configuration dictionary or path to config file
        """
        #: Optional[str]: Path to the configuration file if one was provided
        self.config_path: Optional[str] = None

        #: Optional[Dict[str, Any]]: The loaded configuration dictionary
        self.config: Optional[Dict[str, Any]] = None

        #: Private backing field for the provider property
        self._provider = None

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
    # create the NAT guest only network(s)
    # create the guest only NAT networks
    #nat_networks = cluster['networks']
    #for nat_network, info in nat_networks.items():
    #    cls.natnetwork.add(
    #        nat_network,
    #        network=info['network'],
    #        enable=info.get('enable'),
    #        recreate=True,
    #        dhcp=info.get('dhcp')
    #    )
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
    #
    # clone the vms
    #
    #def _clone(vm_name, vm_info):
    #    print(f'clone the vm {vm_name}')
    #    pprint(vm_info)

    #    cls.removevm(vm_name)
    #    cls.clonevm(vmname=base_image, name=vm_name, basefolder=workdir)
    #    cls.group_vm(vmname=vm_name, groups=os.path.join(f'/{project}', cluster_group))

    #processes = [
    #    Process(target=_clone, args=(vm_name, vm_info))
    #    for vm_name, vm_info in vms.items()]
    #[p.start() for p in processes]
    #[p.join() for p in processes]

    def clone_vms(self) -> None:
        """
        Clone the VMs defined in the configuration.

        The following is done for every vm in every cluster

            - remove the vm
            - clone the vm
        """
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():

                vm_info = vm_info.copy()
                new_vm_name = f"{cluster_name}_{vm_name}"

                self.provider.clone_vm(
                    src_vm_name=cluster['base_image'],
                    new_vm_name=new_vm_name,
                    info=vm_info,
                    workdir=cluster['workdir']
                )


    def configure_network_interfaces(self) -> None:
        """
        Configure network interfaces for all VMs based on their network_adapters configuration.

        This method adds network interfaces to VMs after they have been cloned,
        connecting them to the appropriate networks.
        """
        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{cluster_name}_{vm_name}"

                print(f"Configuring network interfaces for VM {vm_name} in cluster {cluster_name}")

                if 'network_adapters' not in vm_info:
                    print(f"No network adapters defined for VM {vm_name}, skipping")
                    continue

                success = self.provider.configure_vm_network_interfaces(
                    vm_name=full_vm_name,
                    network_adapters=vm_info['network_adapters']
                )

                if success:
                    print(f"All network interfaces configured successfully for VM {vm_name}")
                else:
                    print(f"Some network interfaces could not be configured for VM {vm_name}")

    def configure_disks(self) -> None:
        """
        Configure disks for all VMs based on their disks configuration.

        This method creates and attaches disks to VMs after they have been cloned.
        """
        for cluster_name, cluster in self.config['clusters'].items():
            workdir = cluster.get('workdir', '.')

            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{cluster_name}_{vm_name}"

                print(f"Configuring disks for VM {vm_name} in cluster {cluster_name}")

                # Check if there are disks defined in the VM configuration
                if 'disks' not in vm_info or not vm_info['disks']:
                    print(f"No disks defined for VM {vm_name}, skipping")
                    continue

                # Configure all disks for this VM
                success = self.provider.configure_vm_disks(
                    vm_name=full_vm_name,
                    disks=vm_info['disks'],
                    workdir=workdir,
                    disk_prefix=full_vm_name
                )

                if success:
                    print(f"All disks configured successfully for VM {vm_name}")
                else:
                    print(f"Some disks could not be configured for VM {vm_name}")

    def destroy_vms(self) -> None:
        """
        Destroy the VMs specified in the cluster configuration.
        """
        if not self.provider:
            print("No provider set, cannot destroy VMs")
            return

        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name in cluster['vms'].keys():
                print(f"Destroying VM {vm_name} in cluster {cluster_name}")
                vm_name = f"{cluster_name}_{vm_name}"
                success = self.provider.destroy_vm(
                    name=vm_name,
                    remove_storage=True
                )

                if success:
                    print(f"Successfully destroyed VM {vm_name}")
                else:
                    print(f"Failed to destroy VM {vm_name}")
    ### end vms define / remove / destroy

    def start_vms(self) -> None:
        """
        Start all VMs in the configuration.

        This powers on all VMs after they have been configured.
        """
        if not self.provider:
            print("No provider set, cannot start VMs")
            return

        for cluster_name, cluster in self.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{cluster_name}_{vm_name}"

                print(f"Starting VM {vm_name}")
                success = self.provider.start_vm(full_vm_name)

                if success:
                    print(f"Successfully started VM {vm_name}")
                else:
                    print(f"Failed to start VM {vm_name}")

    def connect_info(self) -> None:
        """
        Display connection information for all VMs in all clusters.

        This method displays the VM names, hostnames, IP addresses, and
        other connection details for all configured VMs.
        """
        if not self.provider:
            print("No provider set, cannot retrieve connection information")
            return

        print("\n=== VM Connection Information ===\n")

        for cluster_name, cluster in self.config['clusters'].items():
            print(f"Cluster: {cluster_name}")
            print("-" * 60)

            for vm_name, vm_info in cluster['vms'].items():
                full_vm_name = f"{cluster_name}_{vm_name}"
                hostname = vm_info.get('hostname', vm_name)

                print(f"VM: {vm_name} (hostname: {hostname})")

                # Get IP addresses for all interfaces
                ip_addresses = self.provider.get_vm_ip_addresses(full_vm_name)

                if ip_addresses:
                    print("  IP Addresses:")
                    for iface, ip in ip_addresses.items():
                        print(f"    {iface}: {ip}")
                else:
                    print("  IP Addresses: Not available")

                # Get SSH connection information
                admin_user = cluster.get('admin_user', '<placeholder>')
                admin_key = os.path.expanduser(os.path.join(
                    cluster.get('workdir', '~'),
                    cluster.get('admin_key_name', 'id_ed25519_boxman')
                ))

                print("  Connect via SSH:")
                # Show direct connection if IP is available
                if ip_addresses:
                    first_ip = next(iter(ip_addresses.values()))
                    print(f"    Direct: ssh -i {admin_key} {admin_user}@{first_ip}")

                # Show connection using ssh_config if available
                if 'ssh_config' in cluster:
                    ssh_config = os.path.expanduser(os.path.join(
                        cluster.get('workdir', '~'),
                        cluster.get('ssh_config', 'ssh_config')
                    ))
                    print(f"    Via config: ssh -F {ssh_config} {hostname}")

                print()

            print()

    @staticmethod
    def provision(cls, cli_args):

        cls.deprovision(cls, cli_args)

        config = cls.config
        project = config['project']
        cluster_group = list(config['clusters'].keys())[0]  # one cluster supported for now
        # -------------------- global config ---------------------------

        cluster = config['clusters'][cluster_group]
        base_image = cluster['base_image']
        cluster_name = cluster_group
        proxy_host = cluster['proxy_host']
        admin_user = cluster['admin_user']
        admin_pass = cluster['admin_pass']
        admin_key_name = cluster['admin_key_name']
        ssh_config = cluster['ssh_config']
        workdir = cluster['workdir']
        # -------------------- end global config -----------------------

        admin_priv_key = os.path.expanduser(os.path.join(workdir, admin_key_name))
        admin_public_key = os.path.expanduser(os.path.join(workdir, admin_key_name + '.pub'))
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

        # Wait a moment for IPs to be assigned
        import time
        print("Waiting for VMs to initialize and get IP addresses...")
        time.sleep(10)

        cls.connect_info()
        asdasd
        ###############################################################################
        ###############################################################################
        ###############################################################################
        ###############################################################################
        ###############################################################################
        # create the NAT guest only network(s)
        # create the guest only NAT networks
        #nat_networks = cluster['networks']
        #for nat_network, info in nat_networks.items():
        #    cls.natnetwork.add(
        #        nat_network,
        #        network=info['network'],
        #        enable=info.get('enable'),
        #        recreate=True,
        #        dhcp=info.get('dhcp')
        #    )

        vms = cluster['vms']

        #
        # clone the vms
        #
        #def _clone(vm_name, vm_info):
        #    print(f'clone the vm {vm_name}')
        #    pprint(vm_info)

        #    cls.removevm(vm_name)
        #    cls.clonevm(vmname=base_image, name=vm_name, basefolder=workdir)
        #    cls.group_vm(vmname=vm_name, groups=os.path.join(f'/{project}', cluster_group))

        #processes = [
        #    Process(target=_clone, args=(vm_name, vm_info))
        #    for vm_name, vm_info in vms.items()]
        #[p.start() for p in processes]
        #[p.join() for p in processes]

        # .. todo:: prefix the vm name (not the hostname) with the cluster group name
        # .. todo:: place each vm in a virtualbox group (like in the ui)
        #vms = cluster['vms']
        #for vm_name, vm_info in vms.items():
        #    # set the path of the disk for every disk that is defined
        #    for disk_info in vm_info['disks']:
        #        disk_info['disk_path'] = os.path.join(
        #            workdir,
        #            f'{cluster_name}_{vm_name}_{disk_info["name"]}.vdi'
        #        )

        #
        # configure the disks
        #
        def _manage_disks(vm_name, vm_info):
            print(f'manage the disks of the vm {vm_name}')
            pprint(vm_info)

            # create the meedium and attach the disks
            # get the UUID of the disk from the name of the disk and delete it
            for disk_info in vm_info['disks']:

                disk_path = disk_info['disk_path']
                disk_uuid = cls.list('hdds').query_disk_by_path(disk_path)
                if disk_uuid:
                    print(f'disk {disk_path} already exists...close and  delete it')
                    # .. todo:: implement detaching the disk from the vm before
                    #           deleting it but if the vm to which the disk was
                    #           attached is off or deleted this is not a problem
                    cls.closemedium(
                        disk_info['medium_type'], target=disk_uuid, delete=True)

                cls.createmedium(
                    disk_info['medium_type'],
                    filename=disk_path,
                    format=disk_info['format'],
                    size=disk_info['size'])

                cls.storageattach(
                    vm_name,
                    storagectl=disk_info['attach_to']['controller']['storagectl'],
                    port=disk_info['attach_to']['controller']['port'],
                    medium=disk_path,
                    medium_type=disk_info['attach_to']['controller']['medium_type'])

        #for vm_name, vm_info in vms.items():
        #    _manage_disks(vm_name, vm_info)
        processes = [
            Process(target=_manage_disks, args=(vm_name, vm_info))
            for vm_name, vm_info in vms.items()]
        [p.start() for p in processes]
        [p.join() for p in processes]

        #
        # configure the network interfaces
        #
        def _manage_network_interfaces(vm_name, vm_info):
            print(f'manage the network interfaces {vm_name}')
            pprint(vm_info)

            # configure the network interfaces
            for interface_no, netowrk_interface_info in enumerate(vm_info['network_adapters']):
                cls.modifyvm_network_settings.apply(
                    vm_name,
                    interface_no + 1,
                    netowrk_interface_info
                )

            # create the port forwarding rule
            access_port = vm_info['access_port']
            cls.forward_local_port_to_vm(
                vmname=vm_name, host_port=access_port, guest_port="22")
            cls.startvm(vm_name)

        processes = [
            Process(target=_manage_network_interfaces, args=(vm_name, vm_info))
            for vm_name, vm_info in vms.items()]
        [p.start() for p in processes]
        [p.join() for p in processes]

        # generate the ssh configuration file for easy access without typing much
        # .. todo:: use the ssh config generator in utils.py
        print('write the ssh_config file')
        with open(ssh_config, 'w') as fobj:

            fobj.write('Host *\n')
            fobj.write('    StrictHostKeyChecking no\n')
            fobj.write('    UserKnownHostsFile /dev/null\n')
            fobj.write('\n\n')

            for vm_name, vm_info in vms.items():
                fobj.write(f'Host {vm_info["hostname"]}\n')
                fobj.write(f'    Hostname {proxy_host}\n')
                fobj.write(f'    User {admin_user}\n')
                fobj.write(f'    Port {vm_info["access_port"]}\n')
                fobj.write(f'    IdentityFile {admin_priv_key}\n')
                fobj.write('\n\n')

        # generate the ssh priv/pub key pair if it does not exist
        if not os.path.exists(admin_priv_key):
            cmd = f'ssh-keygen -t ed25519 -a 100 -f {admin_priv_key} -q -N ""'
            Command(cmd).run()
            for fpath in [admin_priv_key, admin_public_key]:
                _fpath = os.path.abspath(os.path.expanduser(fpath))
                assert os.path.isfile(_fpath)
            print('admin priv/pub key generated successfully')

        # wait for all the vms to be ssh'able
        print("wait for vms to be ssh'able")
        for vm_name, vm_info in vms.items():
            print(f'vm: {vm_name}')
            ssh_status = cls.wait_for_ssh_server_up(
                host='localhost',
                port=vm_info['access_port'],
                timeout=60,
                n_try=20
            )
            if ssh_status is True:
                print(f"vm {vm_name} is ssh'able")

            # add the ssh-key of the admin account to enable passwordless login
            # .. todo:: make sure that the machine is sshable by executing a ssh
            #           command that echo's the hostname or something and repeat
            #           until it succeeds with max n tries...etc...
            n_try = 5
            t_retry = 10
            ssh_success = False
            print(f'try to add ssh key to {vm_name}')
            for try_no in range(n_try):
                print(f'trial {try_no}')

                process = Command(
                    f'sshpass -p {admin_pass} ssh-copy-id -p {vm_info["access_port"]} '
                    f'-i {admin_public_key} -o StrictHostKeyChecking=no '
                    f'-o UserKnownHostsFile="/dev/null" {admin_user}@localhost'
                ).run(capture=True)

                print(process.stdout)
                print(process.stderr)

                if process.process.returncode == 0:

                    cmd = Command(f'ssh -F {ssh_config} {vm_info["hostname"]} hostname')
                    process = cmd.run(capture=True)
                    print('-' * 10)
                    print(f'stdout: {process.stdout}')
                    print(f'stderr: {process.stderr}')
                    print('-' * 10)
                    # .. todo:: replace the osboxes and hostname cmd check with
                    # something more reliable
                    if len(process.stdout.strip()) > 0:
                        ssh_success = True

                if ssh_success:
                    print('sucessfully sshed to vm')
                    break

                time.sleep(t_retry)

            if ssh_success is False:
                raise ValueError('could not add ssh key')

        print('to ssh to a certain host e.g mgmt01:')
        print(f'>>> ssh -F {ssh_config} mgmt01')

        print('to run ansible:')
        print(
            f'>>> ansible --ssh-common-args="-F {ssh_config}" -i /path/to/inventory all -m ping')


    @staticmethod
    def deprovision(cls, cli_args):

        config = cls.config
        project = config['project']
        cluster_group = list(config['clusters'].keys())[0]  # one cluster supported for now
        # -------------------- global config ---------------------------

        cluster = config['clusters'][cluster_group]
        base_image = cluster['base_image']
        cluster_name = cluster_group
        proxy_host = cluster['proxy_host']
        admin_user = cluster['admin_user']
        admin_pass = cluster['admin_pass']
        admin_key_name = cluster['admin_key_name']
        ssh_config = cluster['ssh_config']
        workdir = cluster['workdir']
        # -------------------- end global config -----------------------

        admin_priv_key = os.path.expanduser(os.path.join(workdir, admin_key_name))
        admin_public_key = os.path.expanduser(os.path.join(workdir, admin_key_name + '.pub'))
        ssh_config = os.path.expanduser(os.path.join(workdir, ssh_config))
        workdir = os.path.abspath(os.path.expanduser(workdir))
        if not os.path.isdir(workdir):
            os.makedirs(workdir)

        # .. todo:: implement undo'ing the provisioning of the files (not important for now)
        #cls.provision_files()

        cls.destroy_networks()

        for cluster_name, cluster in cls.config['clusters'].items():
            for vm_name, vm_info in cluster['vms'].items():

                vm_info = vm_info.copy()
                new_vm_name = f"{cluster_name}_{vm_name}"

                cls.provider.destroy_vm(new_vm_name)

                cls.provider.destroy_disks(
                    cluster['workdir'],
                    vm_name=new_vm_name,
                    disks=vm_info['disks']
                )

        return



        conf = cls.conf
        cluster_group = list(conf['clusters'].keys())[0]  # one cluster supported for now
        cluster = conf['clusters'][cluster_group]
        workdir = cluster['workdir']

        # delete the vms
        vms = cluster['vms']
        def _delete_vm(vm):
            cls.session.removevm(vm)
        processes = [Process(target=_delete_vm, args=(vm,)) for vm in vms]
        [p.start() for p in processes]
        [p.join() for p in processes]

        # delete the networks
        nat_networks = cluster['networks']
        def _delete_networks(nat_network):
            cls.session.natnetwork.remove(nat_network)
        processes = [Process(target=_delete_networks, args=(network,)) for network in nat_networks]
        [p.start() for p in processes]
        [p.join() for p in processes]

        # delete the workdir
        # .. todo:: delete the directory only if there are no vms left because
        #           sometimes if a vm is locked it is not deleted.
        workdir = os.path.abspath(os.path.expanduser(workdir))
        print(f'remove workdir {workdir}...')
        if os.path.isdir(workdir):
            shutil.rmtree(workdir)
        print(f'\ncompleted deprovisioning the cluster {cluster_group}')
