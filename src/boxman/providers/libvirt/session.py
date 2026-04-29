import json
import os
import time
from multiprocessing import Process, Queue
from typing import Any

from boxman import log

from .cdrom import CDROMManager
from .clone_vm import CloneVM
from .commands import VirshCommand
from .destroy_vm import DestroyVM
from .disk import DiskManager
from .disk_cleanup import remove_vm_disks
from .import_image import ImageImporter
from .net import Network, NetworkInterface
from .shared_folder import SharedFolderManager
from .snapshot import SnapshotManager
from .virsh_edit import VirshEdit


class LibVirtSession:
    def __init__(self,
                 config: dict[str, Any] | None = None):
        """
        Initialize the LibVirtSession.

        Args:
            config: Optional configuration dictionary
        """
        #: Optional[Dict[str, Any]]: The configuration for this session
        self.config = config

        #: logging.Logger: the logger instance
        self.logger = log

        # get provider config from the project configuration — these are
        # authoritative and must never be overridden by app-level defaults.
        self._project_provider_config = config.get('provider', {}).get('libvirt', {})

        #: Dict[str, Any]: the base provider config (may be enriched with
        #: app-level or runtime settings, but project-level keys always win
        #: via the property).
        self._provider_config_base = self._project_provider_config.copy()

        #: the boxman manager instance (mainly to get access to the cache)
        self.manager = None

    @property
    def provider_config(self) -> dict[str, Any]:
        """
        Return the effective provider config.

        Project-level settings (from conf.yml) always take precedence
        over app-level (boxman.yml) or runtime-injected values.
        """
        merged = self._provider_config_base.copy()
        merged.update(self._project_provider_config)
        return merged

    @provider_config.setter
    def provider_config(self, value: dict[str, Any]) -> None:
        """
        Set the base provider config.

        The getter will overlay project-level settings on top, so
        project-level keys like ``use_sudo`` can never be overridden.
        """
        self._provider_config_base = value

    @property
    def uri(self) -> str:
        return self.provider_config.get('uri', 'qemu:///system')

    @uri.setter
    def uri(self, value: str) -> None:
        self._provider_config_base['uri'] = value

    @property
    def use_sudo(self) -> bool:
        return self.provider_config.get('use_sudo', False)

    @use_sudo.setter
    def use_sudo(self, value: bool) -> None:
        self._provider_config_base['use_sudo'] = value

    def update_provider_config(self, new_config: dict[str, Any]) -> None:
        """
        Update provider_config with *new_config*, but project-level settings
        always win (enforced by the property getter).

        Args:
            new_config: Additional config keys (e.g. from boxman.yml providers
                        section or runtime injection).
        """
        merged = self._provider_config_base.copy()
        merged.update(new_config)
        self._provider_config_base = merged

    def update_provider_config_with_runtime(self) -> None:
        """
        Enrich provider_config with runtime metadata from the manager.

        Project-level provider settings always take precedence over
        app-level (boxman.yml) settings and runtime defaults.
        """
        if self.manager is None:
            return

        # Get the runtime-enriched config from the manager
        enriched = self.manager.get_provider_config_with_runtime(
            self.provider_config
        )
        # Merge, ensuring project-level keys win
        self.update_provider_config(enriched)

    def import_image(self,
                     manifest_uri: str,
                     vm_name: str,
                     vm_dir: str) -> bool:
        """
        Import an image into the libvirt storage pool.

        :param manifest_uri: URI of the manifest of the image to import
        :param vm_name: Name to assign to the imported VM
        :param vm_dir: Directory for the imported VM
        :return: True if successful, False otherwise
        """
        if manifest_uri.startswith('file://'):
            manifest_path = os.path.expanduser(manifest_uri[len('file://'):])
            with open(manifest_path) as fobj:
                manifest = json.load(fobj)
        elif manifest_uri.startswith('http://') or manifest_uri.startswith('https://'):
            raise NotImplementedError('http/https image uris are not implemented yet')

        image_importer = ImageImporter(
            manifest_path=manifest_path,
            uri=self.manager.config['uri'],
            disk_dir=vm_dir,
            vm_name=vm_name,
            keep_uuid=False)

        image_importer.import_image()

    def define_network(self,
                       name: str = None,
                       info: dict[str, Any] | None = None,
                       workdir: str | None = None) -> bool:
        """
        Define a network that can be used to be attached to the interfaces of vms.

        Args:
            name: Name of the network
            info: Dictionary containing network configuration
            workdir: Working directory for XML files (resolved to absolute
                     path so it works inside container runtimes)

        Returns:
            True if successful, False otherwise
        """
        # Resolve workdir to an absolute path so it is valid both on the
        # host and inside a bind-mounted docker-compose container.
        if workdir:
            workdir = os.path.abspath(os.path.expanduser(workdir))
            os.makedirs(workdir, exist_ok=True)

        network = Network(
            name=name,
            info=info,
            provider_config=self.provider_config,
            manager=self.manager)

        status = network.define_network(file_path=os.path.join(workdir, f'{name}_net_define.xml'))

        return status

    def destroy_network(self,
                        name: str = None,
                        info: dict[str, Any] | None = None) -> bool:
        """
        Destroy a network.

        Args:
            cluster_name: Name of the cluster
            network_name: Name of the network

        Returns:
            True if successful, False otherwise
        """
        network = Network(name=name, info=info)
        status = network.destroy_network()
        return status

    def undefine_network(self,
                         name: str = None,
                         info: dict[str, Any] | None = None) -> bool:
        """
        Undefine a network.

        Args:
            cluster_name: Name of the cluster
            network_name: Name of the network

        Returns:
            True if successful, False otherwise
        """
        network = Network(name=name, info=info)
        status = network.undefine_network()
        return status

    def remove_network(self,
                       name: str = None,
                       info: dict[str, Any] | None = None) -> bool:
        """
        Complete removal of a network: destroy and undefine.

        Args:
            name: The name of the network

        Returns:
            True if successful, False otherwise
        """
        network = Network(
            name=name,
            info=info,
            provider_config=self.provider_config,
            assign_new_bridge=False,
            manager=self.manager
        )
        status = network.remove_network()
        return status

    def clone_vm(self,
                 new_vm_name: str,
                 src_vm_name: str,
                 info: dict[str, Any],
                 workdir: str) -> bool:
        """
        Clone a VM, or create a bare PXE-boot VM if boot_order starts with 'network'.

        Args:
            new_vm_name: Name of the new VM
            src_vm_name: Name of the source VM (unused when boot_order is network-first)
            info: VM configuration information
            workdir: Working directory for disk images

        Returns:
            True if successful, False otherwise
        """
        boot_order = info.get('boot_order', ['hd'])
        if boot_order and boot_order[0] == 'network':
            from .bare_vm import BareVM
            bare = BareVM(
                vm_name=new_vm_name,
                info=info,
                provider_config=self.provider_config,
                workdir=workdir,
            )
            status = bare.create()
            if not status:
                raise RuntimeError(
                    f"Failed to create bare PXE VM '{new_vm_name}'"
                )
            return True

        cloner = CloneVM(
            src_vm_name=src_vm_name,
            new_vm_name=new_vm_name,
            info=info,
            provider_config=self.provider_config,
            workdir=workdir,
        )

        status = cloner.clone()
        if not status:
            raise RuntimeError(
                f"Failed to clone VM {src_vm_name} to {new_vm_name}"
            )

    def destroy_disks(self,
                      workdir : str,
                      vm_name: str,
                      disks: list[dict[str, str]],
                      ) -> bool:
        """
        Destroy disks associated with the VM.

        Removes:
        - The primary boot disk ({vm_name}.qcow2)
        - Any extra named disks ({vm_name}_{disk}.qcow2)
        - All snapshot artifacts: external overlay files (e.g.
          {vm_name}.2026-03-02T15:36:54, {vm_name}.1772465824) and memory
          snapshot raw files ({vm_name}_snapshot_*.raw)

        Args:
            workdir: Directory where disk images are stored
            vm_name: Full name of the VM
            disks: Extra disk configurations from the cluster config

        Returns:
            True if successful, False otherwise
        """
        # Delegates to the pure-filesystem helper extracted in Phase 2.6.
        return remove_vm_disks(workdir, vm_name, disks)

    def set_boot_order(self, vm_name: str, order: list[str]) -> bool:
        """
        Set the boot device order for a VM.

        Args:
            vm_name: Name of the VM
            order: List of boot devices in priority order, e.g. ['network', 'hd']

        Returns:
            True if successful
        """
        from lxml import etree

        editor = VirshEdit(self.provider_config)
        xml_content = editor.get_domain_xml(vm_name, inactive=True)

        tree = etree.fromstring(xml_content.encode('utf-8'))
        os_elem = tree.find('os')
        if os_elem is None:
            os_elem = etree.SubElement(tree, 'os')
        for b in os_elem.findall('boot'):
            os_elem.remove(b)
        for dev in order:
            boot = etree.SubElement(os_elem, 'boot')
            boot.set('dev', dev)

        modified_xml = etree.tostring(tree, encoding='unicode', pretty_print=True)
        self.logger.info(
            f"setting boot order for '{vm_name}' to {order}")
        return editor.redefine_domain(vm_name, modified_xml)

    def restore_boot_order(self, vm_name: str) -> bool:
        """Restore boot order to ['hd'] (local disk only)."""
        return self.set_boot_order(vm_name, ['hd'])

    def wait_for_ssh(self,
                     ip: str,
                     port: int = 22,
                     timeout: int = 600,
                     interval: int = 10) -> bool:
        """
        Poll for SSH availability on a host.

        Args:
            ip: IP address to connect to
            port: SSH port (default: 22)
            timeout: Maximum seconds to wait
            interval: Seconds between retries

        Returns:
            True if SSH becomes available before timeout
        """
        import socket
        import time

        elapsed = 0
        while elapsed < timeout:
            try:
                with socket.create_connection((ip, port), timeout=5):
                    self.logger.info(
                        f"SSH available on {ip}:{port} after {elapsed}s")
                    return True
            except OSError:
                self.logger.info(
                    f"SSH not yet available on {ip}, retrying in {interval}s...")
                time.sleep(interval)
                elapsed += interval
        self.logger.error(f"SSH timeout after {timeout}s on {ip}:{port}")
        return False

    def destroy_vm(self, name: str, force: bool = False) -> bool:
        """
        Destroy (remove) a vm.

        Args:
            name: Name of the vm to destroy
            force: Whether to forcefully destroy the vm

        Returns:
            True if successful, False otherwise
        """
        destroyer = DestroyVM(name=name, provider_config=self.provider_config)
        if not force:
            status = destroyer.remove()
        else:
            status = destroyer.force_undefine_vm()
        return status

    def start_vm(self, vm_name: str) -> bool:
        """
        Start a VM.

        Args:
            vm_name: Name of the VM to start

        Returns:
            True if successful, False otherwise
        """

        try:
            virsh = VirshCommand(provider_config=self.provider_config)

            # check if the vm is already running
            result = virsh.execute("domstate", vm_name, warn=True)
            if result.ok and "running" in result.stdout:
                self.logger.info(f"vm {vm_name} is already running")
                return True

            # try to start the VM
            result = virsh.execute("start", vm_name)

            if not result.ok:
                self.logger.error(f"failed to start VM {vm_name}: {result.stderr}")
                return False

            # verify that VM is running
            verify_result = virsh.execute("domstate", vm_name)

            if "running" in verify_result.stdout:
                self.logger.info(f"vm {vm_name} started successfully")
                return True
            else:
                self.logger.error(
                    f"vm {vm_name} did not start properly. current state: {verify_result.stdout}")
                return False

        except Exception as exc:
            import traceback
            self.logger.error(f"error starting vm {vm_name}: {exc}")
            self.logger.error(traceback.format_exc())
            return False

    def add_network_interface(self,
                              vm_name: str,
                              network_source: str,
                              link_state: str = 'active',
                              mac_address: str | None = None,
                              model: str = 'virtio') -> bool:
        """
        Add a network interface to a VM.

        Args:
            vm_name: Name of the VM
            network_source: Name of the network to attach to
            link_state: State of the link ('active' or 'inactive')
            mac_address: Optional MAC address for the interface
            model: NIC model (default: virtio)

        Returns:
            True if successful, False otherwise
        """
        network_interface = NetworkInterface(vm_name=vm_name, provider_config=self.provider_config)

        return network_interface.add_interface(
            network_source=network_source,
            link_state=link_state,
            mac_address=mac_address,
            model=model
        )

    def configure_vm_network_interfaces(self,
                                       vm_name: str,
                                       network_adapters: list[dict[str, Any]]) -> bool:
        """
        Configure all network interfaces for a VM.

        Args:
            vm_name: Name of the VM
            network_adapters: List of network adapter configurations

        Returns:
            True if all adapters were configured successfully, False otherwise
        """
        network_interface = NetworkInterface(
            vm_name=vm_name,
            provider_config=self.provider_config)

        success = True
        for i, adapter_config in enumerate(network_adapters):
            self.logger.info(f"configuring network interface {i+1} for vm {vm_name}")

            if not network_interface.configure_from_config(adapter_config):
                self.logger.error(f"failed to configure network interface {i+1} for vm {vm_name}")
                success = False
            else:
                self.logger.info(
                    f"successfully configured network interface {i+1} for vm {vm_name}")

        return success

    def configure_vm_disks(self,
                           vm_name: str,
                           disks: list[dict[str, Any]],
                           workdir: str,
                           disk_prefix: str = "") -> bool:
        """
        Configure all disks for a VM.

        Args:
            vm_name: the name of the VM
            disks: the list of disk configurations
            workdir: the working directory for disk images
            disk_prefix: the prefix to add to disk image filenames

        Returns:
            True if all disks were configured successfully, False otherwise
        """
        disk_manager = DiskManager(vm_name=vm_name, provider_config=self.provider_config)
        result_queue: Queue = Queue()

        def _configure(i, disk_config):
            self.logger.info(f"configuring disk {i+1} for VM {vm_name}")
            ok = disk_manager.configure_from_disk_config(
                disk_config=disk_config,
                workdir=workdir,
                disk_prefix=disk_prefix
            )
            result_queue.put((i, ok))
            if ok:
                self.logger.info(f"successfully configured disk {i+1} for vm {vm_name}")
            else:
                self.logger.error(f"failed to configure disk {i+1} for vm {vm_name}")

        processes = [
            Process(target=_configure, args=(i, disk_config))
            for i, disk_config in enumerate(disks)
        ]
        [p.start() for p in processes]
        [p.join() for p in processes]

        success = True
        for _ in range(len(disks)):
            _, ok = result_queue.get()
            if not ok:
                success = False

        return success

    def configure_vm_cdroms(self,
                            vm_name: str,
                            cdroms: list[dict[str, Any]]) -> bool:
        """
        Configure all CDROM devices for a VM.

        Args:
            vm_name: Name of the VM
            cdroms: List of CDROM configurations

        Returns:
            True if all CDROMs were configured successfully, False otherwise
        """
        cdrom_manager = CDROMManager(vm_name=vm_name, provider_config=self.provider_config)
        success = True
        for i, cdrom_config in enumerate(cdroms):
            self.logger.info(
                f"configuring CDROM {i+1} ('{cdrom_config.get('name', '?')}') "
                f"for VM {vm_name}")
            if not cdrom_manager.configure_from_config(cdrom_config):
                self.logger.error(
                    f"failed to configure CDROM {i+1} for VM {vm_name}")
                success = False
            else:
                self.logger.info(
                    f"successfully configured CDROM {i+1} for VM {vm_name}")
        return success

    def update_vm_cdroms(self,
                         vm_name: str,
                         new_cdroms: list[dict[str, Any]],
                         removed_cdroms: list[dict[str, Any]],
                         changed_cdroms: list[dict[str, Any]],
                         vm_running: bool) -> bool:
        """
        Apply CDROM changes: attach new, detach removed, swap changed.

        Args:
            vm_name: Full VM domain name
            new_cdroms: CDROM configs to attach
            removed_cdroms: CDROM entries to detach (dicts with 'target')
            changed_cdroms: CDROM entries with changed source
                            (dicts with 'target' and 'source')
            vm_running: Whether the VM is currently running

        Returns:
            True if all operations succeeded, False otherwise
        """
        cdrom_manager = CDROMManager(vm_name=vm_name, provider_config=self.provider_config)
        success = True

        for cdrom_config in new_cdroms:
            name = cdrom_config.get('name', '?')
            self.logger.info(f"attaching new CDROM '{name}' to VM {vm_name}")
            if not cdrom_manager.configure_from_config(cdrom_config):
                self.logger.error(f"failed to attach CDROM '{name}' to {vm_name}")
                success = False

        for entry in removed_cdroms:
            target = entry['target']
            self.logger.info(f"detaching CDROM {target} from VM {vm_name}")
            if not cdrom_manager.detach_cdrom(target):
                self.logger.error(f"failed to detach CDROM {target} from {vm_name}")
                success = False

        for entry in changed_cdroms:
            target = entry['target']
            source = entry['source']
            self.logger.info(
                f"changing CDROM media on {target} to {source} on VM {vm_name}")
            if not cdrom_manager.change_media(target, source):
                self.logger.error(
                    f"failed to change CDROM media on {target} on {vm_name}")
                success = False

        return success

    def configure_vm_shared_folders(self,
                                    vm_name: str,
                                    shared_folders: list[dict[str, Any]]) -> bool:
        """
        Configure all shared folders for a VM.

        Args:
            vm_name: Name of the VM
            shared_folders: List of shared folder configurations

        Returns:
            True if all shared folders were configured successfully, False otherwise
        """
        folder_manager = SharedFolderManager(
            vm_name=vm_name, provider_config=self.provider_config)
        success = True
        for i, folder_config in enumerate(shared_folders):
            name = folder_config.get('name', '?')
            self.logger.info(
                f"configuring shared folder {i+1} ('{name}') for VM {vm_name}")
            result = folder_manager.configure_from_config(folder_config)
            if not result['success']:
                self.logger.error(
                    f"failed to configure shared folder '{name}' for VM {vm_name}")
                success = False
            else:
                if result.get('restart_needed'):
                    self.logger.info(
                        f"shared folder '{name}' configured for VM {vm_name} "
                        f"(restart needed)")
                else:
                    self.logger.info(
                        f"successfully configured shared folder '{name}' "
                        f"for VM {vm_name}")
        return success

    def update_vm_shared_folders(self,
                                 vm_name: str,
                                 new_folders: list[dict[str, Any]],
                                 removed_folders: list[dict[str, Any]],
                                 changed_folders: list[dict[str, Any]],
                                 vm_running: bool) -> dict[str, Any]:
        """
        Apply shared folder changes: attach new, detach removed, re-attach changed.

        Args:
            vm_name: Full VM domain name
            new_folders: Folder configs to attach
            removed_folders: Folder entries to detach (dicts with 'name', 'host_path')
            changed_folders: Folder configs with changed host_path or readonly
            vm_running: Whether the VM is currently running

        Returns:
            Dict with 'success' (bool) and 'restart_needed' (bool)
        """
        folder_manager = SharedFolderManager(
            vm_name=vm_name, provider_config=self.provider_config)
        success = True
        restart_needed = False

        for folder_config in new_folders:
            name = folder_config.get('name', '?')
            self.logger.info(f"attaching new shared folder '{name}' to VM {vm_name}")
            result = folder_manager.configure_from_config(folder_config)
            if not result['success']:
                self.logger.error(
                    f"failed to attach shared folder '{name}' to {vm_name}")
                success = False
            if result.get('restart_needed'):
                restart_needed = True

        for entry in removed_folders:
            name = entry['name']
            host_path = entry.get('host_path', '')
            readonly = entry.get('readonly', False)
            self.logger.info(f"detaching shared folder '{name}' from VM {vm_name}")
            result = folder_manager.detach_shared_folder(name, host_path, readonly)
            if not result['success']:
                self.logger.error(
                    f"failed to detach shared folder '{name}' from {vm_name}")
                success = False
            if result.get('restart_needed'):
                restart_needed = True

        for folder_config in changed_folders:
            name = folder_config.get('name', '?')
            self.logger.info(
                f"updating shared folder '{name}' on VM {vm_name}")
            # Detach the old version first (get current state from XML)
            current_folders = folder_manager.get_attached_shared_folders()
            current = next((f for f in current_folders if f['name'] == name), None)
            if current:
                detach_result = folder_manager.detach_shared_folder(
                    name, current['host_path'], current['readonly'])
                if not detach_result['success']:
                    self.logger.error(
                        f"failed to detach old shared folder '{name}' from {vm_name}")
                    success = False
                    continue
                if detach_result.get('restart_needed'):
                    restart_needed = True

            # Attach the new version
            result = folder_manager.configure_from_config(folder_config)
            if not result['success']:
                self.logger.error(
                    f"failed to re-attach shared folder '{name}' to {vm_name}")
                success = False
            if result.get('restart_needed'):
                restart_needed = True

        return {'success': success, 'restart_needed': restart_needed}

    def get_vm_ip_addresses(self, vm_name: str) -> dict[str, str]:
        """
        Get all IP addresses for a VM.

        Tries domifaddr sources in order: lease → arp → agent.
        The default 'lease' source reads dnsmasq DHCP files and can return
        empty even when the VM has an IP (e.g. if cloud-init configured the
        address before the lease was recorded).  Falling back to 'arp' and
        'agent' ensures we find the address regardless of how it was assigned.

        Args:
            vm_name: Name of the VM

        Returns:
            Dictionary mapping interface names to IP addresses
        """
        try:
            # use virsh commands to get domain info
            virsh = VirshCommand(provider_config=self.provider_config)

            # first check if the vm is running
            result = virsh.execute("domstate", vm_name, warn=True)
            if not result.ok or "running" not in result.stdout:
                self.logger.warning(f"the vm {vm_name} is not running, cannot get the ip addresses")
                return {}

            def _parse_domifaddr(output: str) -> dict[str, str]:
                """Parse domifaddr stdout into {iface: ip} dict.

                Only returns routable IPv4 addresses — loopback (127.x),
                link-local IPv6 (fe80::), and any other IPv6 addresses are
                excluded because they are not useful SSH targets.
                """
                addrs = {}
                lines = output.strip().split('\n')
                if len(lines) > 2:
                    for line in lines[2:]:
                        parts = line.split()
                        if len(parts) >= 4:
                            iface_name = parts[0]
                            raw_address = parts[3]
                            if raw_address.startswith('N/A') or raw_address == '-':
                                continue
                            ip_address = raw_address.split('/')[0]
                            if (iface_name
                                    and iface_name != '-'
                                    and ip_address
                                    and ':' not in ip_address        # skip IPv6
                                    and not ip_address.startswith('127.')):  # skip loopback
                                addrs[iface_name] = ip_address
                return addrs

            # Try each source in order; return the first non-empty result.
            for source in ('lease', 'arp', 'agent'):
                result = virsh.execute("domifaddr", vm_name, f"--source={source}", warn=True)
                if not result.ok:
                    self.logger.debug(
                        f"domifaddr --source={source} failed for {vm_name}: {result.stderr.strip()}")
                    continue
                addrs = _parse_domifaddr(result.stdout)
                if addrs:
                    self.logger.debug(
                        f"got ip addresses for {vm_name} via source '{source}': {addrs}")
                    return addrs

            return {}

        except Exception as exc:
            import traceback
            self.logger.error(f"error getting ip addresses for vm {vm_name}: {exc}")
            self.logger.debug(traceback.format_exc())
            return {}

    ### snapshots
    def snapshot_take(self,
                      vm_name=None,
                      vm_dir=None,
                      snapshot_name=None,
                      description=None):
        """
        Create a snapshot of a specific VM

        Args:
            vm_name (str, optional): Full name of the VM to snapshot
            vm_dir (str): Directory for the VM
            snapshot_name (str): Name for the snapshot
            description (str, optional): Description for the snapshot

        Returns:
            bool: True if successful, False otherwise
        """
        snapshot_mgr = SnapshotManager(self.provider_config)
        self.logger.info(f"processing the vm: {vm_name}")

        return snapshot_mgr.create_snapshot(
            vm_name=vm_name,
            vm_dir=vm_dir,
            snapshot_name=snapshot_name,
            description=description)

    def snapshot_list(self, vm_name=None):
        """
        List snapshots for VMs in the specified cluster or all clusters.

        Args:
            cluster_name (str, optional): Name of the cluster. If None, all clusters.
            vm_name (str, optional): Name of the VM. If None, all VMs in cluster(s).

        Returns:
            dict: Dictionary of snapshots per VM
        """
        self.logger.info(f"list the snapshots for the vm: {vm_name}")
        snapshot_mgr = SnapshotManager(self.provider_config)
        snapshots = snapshot_mgr.list_snapshots(vm_name)
        for snapshot in snapshots:
            self.logger.info(
                f"  Snapshot: {snapshot['name']} - Description: {snapshot['description']}")

        return snapshots

    def snapshot_restore(self, vm_name, snapshot_name=None):
        """
        Restore a VM to a specific snapshot.

        If snapshot_name is None, the current (latest) snapshot is used.

        Args:
            vm_name (str): Name of the VM to revert
            snapshot_name (str, optional): Name of the snapshot to revert to

        Returns:
            bool: True if successful, False otherwise
        """
        snapshot_mgr = SnapshotManager(self.provider_config)
        if snapshot_name is None:
            snapshot_name = snapshot_mgr.get_latest_snapshot(vm_name)
            if snapshot_name is None:
                self.logger.error(f"no snapshots found for vm {vm_name}")
                return False
            self.logger.info(f"restoring latest snapshot '{snapshot_name}' for vm {vm_name}")
        else:
            self.logger.info(f"reverting the vm {vm_name} to snapshot {snapshot_name}")
        return snapshot_mgr.snapshot_restore(vm_name, snapshot_name)

    def eject_cdrom(self, vm_name: str) -> None:
        """
        Eject cloud-init seed ISO media from a VM's cdrom drive(s).

        Only ejects cdroms whose source file is the seed ISO itself or a
        qcow2 overlay of it (filenames that start with ``seed``).  Other
        cdrom ISOs that may be intentionally mounted are left untouched.

        Called after provisioning completes (once cloud-init has run and the VM
        has an IP address) so the seed ISO is never present during snapshot
        operations.  Uses ``--force`` to bypass the guest kernel's tray lock.
        """
        virsh = VirshCommand(provider_config=self.provider_config)
        result = virsh.execute("domblklist", vm_name, "--details", warn=True)
        if not result.ok:
            return
        for line in result.stdout.splitlines():
            parts = line.split()
            # domblklist --details columns: Type  Device  Target  Source
            if len(parts) >= 3 and parts[1] == 'cdrom':
                target = parts[2]
                source = parts[3] if len(parts) >= 4 else '-'
                if source == '-':
                    self.logger.debug(f"cdrom {target} on {vm_name} is already empty")
                    continue
                basename = os.path.basename(source)
                if not basename.startswith('seed'):
                    self.logger.debug(
                        f"skipping non-seed cdrom {target} on {vm_name} (source: {source})")
                    continue
                self.logger.info(f"ejecting seed cdrom {target} from {vm_name} (was: {source})")
                eject_result = virsh.execute(
                    "change-media", vm_name, target,
                    "--eject", "--force", "--live", "--config",
                    warn=True)
                if not eject_result.ok:
                    self.logger.warning(
                        f"failed to eject cdrom {target} from {vm_name}: "
                        f"{eject_result.stderr.strip()}")

    def get_latest_snapshot(self, vm_name):
        """Return the current snapshot name for a VM, or None if none exists."""
        snapshot_mgr = SnapshotManager(self.provider_config)
        return snapshot_mgr.get_latest_snapshot(vm_name)

    def validate_snapshot(self, vm_name, snapshot_name):
        """
        Validate that a snapshot is intact and can be safely restored.

        Returns:
            tuple[bool, List[str]]: (valid, errors)
        """
        snapshot_mgr = SnapshotManager(self.provider_config)
        return snapshot_mgr.validate_snapshot(vm_name, snapshot_name)

    def snapshot_delete(self, vm_name, snapshot_name):
        """
        Delete a specific snapshot from a VM.

        Args:
            vm_name (str): Name of the VM
            snapshot_name (str): Name of the snapshot to delete

        Returns:
            bool: True if successful, False otherwise
        """
        self.logger.info(f"deleting the snapshot {snapshot_name} from vm {vm_name}")
        snapshot_mgr = SnapshotManager(self.provider_config)
        return snapshot_mgr.delete_snapshot(vm_name, snapshot_name)
    # end snapshots

    ### control vm
    def suspend_vm(self, vm_name: str) -> bool:
        """
        Suspend (pause) a VM.

        Args:
            vm_name: Name of the VM to suspend

        Returns:
            True if successful, False otherwise
        """
        try:
            virsh = VirshCommand(provider_config=self.provider_config)

            # check if the vm is running
            result = virsh.execute("domstate", vm_name, warn=True)
            if not result.ok or "running" not in result.stdout:
                self.logger.warning(f"vm {vm_name} is not running, cannot suspend")
                return False

            # try to suspend the vm
            self.logger.info(f"suspending the vm {vm_name}")
            result = virsh.execute("suspend", vm_name)

            if not result.ok:
                self.logger.error(f"failed to suspend the vm {vm_name}: {result.stderr}")
                return False

            # verify if the vm is suspended
            verify_result = virsh.execute("domstate", vm_name)
            if "paused" in verify_result.stdout:
                self.logger.info(f"suspended the vm {vm_name} successfully")
                return True
            else:
                self.logger.error(
                    f"vm {vm_name} not suspended. current state: {verify_result.stdout}")
                return False

        except Exception as exc:
            self.logger.error(f"error suspending the vm {vm_name}: {exc}")
            return False

    def resume_vm(self, vm_name: str) -> bool:
        """
        Resume a suspended VM.

        Args:
            vm_name: Name of the VM to resume

        Returns:
            True if successful, False otherwise
        """
        try:
            virsh = VirshCommand(provider_config=self.provider_config)

            # check if the vm is suspended
            result = virsh.execute("domstate", vm_name, warn=True)
            if not result.ok:
                self.logger.warning(f"vm {vm_name} does not exist")
                return False
            if "paused" not in result.stdout:
                self.logger.warning(
                    f"vm {vm_name} is not suspended (current state: {result.stdout.strip()})")
                return False

            # try to resume the vm
            self.logger.info(f"resuming the vm {vm_name}")
            result = virsh.execute("resume", vm_name)

            if not result.ok:
                self.logger.error(f"failed to resume the vm {vm_name}: {result.stderr}")
                return False

            # verify that the vm is running
            verify_result = virsh.execute("domstate", vm_name)
            if "running" in verify_result.stdout:
                self.logger.info(f"the vm {vm_name} resumed successfully")
                return True
            else:
                self.logger.error(
                    f"the vm {vm_name} not resumed. current state: {verify_result.stdout}")
                return False

        except Exception as exc:
            self.logger.error(f"error resuming the vm {vm_name}: {exc}")
            return False

    def save_vm(self, vm_name: str, workdir: str) -> bool:
        """
        Save VM state to a file in the specified workdir.

        Args:
            vm_name: Name of the VM to save
            workdir: Directory where the VM state will be saved

        Returns:
            True if successful, False otherwise
        """
        try:
            virsh = VirshCommand(provider_config=self.provider_config)

            # Check if VM is running
            result = virsh.execute("domstate", vm_name, warn=True)
            if not result.ok or "running" not in result.stdout:
                self.logger.warning(f"vm {vm_name} is not running, cannot save state")
                return False

            # expand the workdir path and ensure it exists
            workdir = os.path.expanduser(workdir)
            if not os.path.exists(workdir):
                os.makedirs(workdir, exist_ok=True)

            save_path = os.path.join(workdir, f"{vm_name}.save")

            # try to save the vm state
            self.logger.info(f"saving the vm {vm_name} state to {save_path}")
            result = virsh.execute("save", vm_name, save_path)

            if not result.ok:
                self.logger.error(f"failed to save the vm {vm_name} state: {result.stderr}")
                return False

            # verify the save file exists
            if os.path.exists(save_path):
                self.logger.info(f"vm {vm_name} state saved successfully to {save_path}")
                return True
            else:
                self.logger.error(f"Save file {save_path} not created for VM {vm_name}")
                return False

        except Exception as exc:
            self.logger.error(f"error saving the vm {vm_name} state: {exc}")
            return False

    def restore_vm(self, vm_name: str, workdir: str) -> bool:
        """
        Restore the vm from a saved state file in the specified workdir.

        Args:
            vm_name: Name of the VM to restore
            workdir: Directory where the VM state was saved

        Returns:
            True if successful, False otherwise
        """
        try:
            virsh = VirshCommand(provider_config=self.provider_config)

            # expand the workdir path
            workdir = os.path.expanduser(workdir)
            save_path = os.path.join(workdir, f"{vm_name}.save")

            # check if save file exists
            if not os.path.exists(save_path):
                self.logger.error(f"save file {save_path} does not exist")
                return False

            # check if the VM is defined but not running
            exists_result = virsh.execute("domstate", vm_name, warn=True)
            if exists_result.ok and "running" in exists_result.stdout:
                self.logger.warning(f"vm {vm_name} is already running, shutting down first")
                shutdown_result = virsh.execute("shutdown", vm_name)
                if not shutdown_result.ok:
                    self.logger.error(f"Failed to shutdown VM {vm_name} before restore: {shutdown_result.stderr}")
                    return False

                # wait for the vm to shut down
                for i in range(30):
                    state_result = virsh.execute("domstate", vm_name, warn=True)
                    if state_result.ok and "shut off" in state_result.stdout:
                        break
                    time.sleep(1)
                else:
                    self.logger.error(f"vm {vm_name} did not shut down within timeout")
                    return False

            # try to restore the vm
            self.logger.info(f"restoring the vm {vm_name} from {save_path}")
            result = virsh.execute("restore", save_path)

            if not result.ok:
                self.logger.error(f"failed to restore the vm {vm_name}: {result.stderr}")
                return False

            # Verify VM is running
            verify_result = virsh.execute("domstate", vm_name)
            if "running" in verify_result.stdout:
                self.logger.info(f"restoring vm {vm_name} successfully from {save_path}")
                # Optionally remove the save file after successful restore
                # os.remove(save_path)
                return True
            else:
                self.logger.error(
                    f"vm {vm_name} not restored. Current state: {verify_result.stdout}")
                return False

        except Exception as exc:
            self.logger.error(f"error restoring the vm {vm_name}: {exc}")
            return False
    ### end control vm

    def configure_vm_cpu_memory(self,
                                vm_name: str,
                                cpus: dict[str, int] | None = None,
                                memory_mb: int | None = None,
                                max_vcpus: int | None = None,
                                max_memory_mb: int | None = None) -> bool:
        """
        Configure cpu and memory settings for a vm.

        Args:
            vm_name: Name of the vm
            cpus: Dictionary with 'sockets', 'cores', 'threads' keys
            memory_mb: Memory in MB
            max_vcpus: Maximum vCPU ceiling for hot-scaling
            max_memory_mb: Maximum memory ceiling in MB for hot-scaling

        Returns:
            True if successful, False otherwise
        """
        editor = VirshEdit(provider_config=self.provider_config)
        return editor.configure_cpu_memory(
            vm_name, cpus, memory_mb,
            max_vcpus=max_vcpus, max_memory_mb=max_memory_mb)

    ### update operations (for `boxman update`)

    def shutdown_and_wait(self, vm_name: str, timeout: int = 60) -> bool:
        """
        Gracefully shut down a VM and wait until it reaches 'shut off' state.

        Falls back to virsh destroy (force stop) if the timeout is exceeded.

        Args:
            vm_name: Name of the VM
            timeout: Maximum seconds to wait for graceful shutdown

        Returns:
            True if the VM is shut off, False otherwise
        """
        virsh = VirshCommand(provider_config=self.provider_config)

        # check current state
        result = virsh.execute('domstate', vm_name, warn=True)
        if result.ok and 'shut off' in result.stdout:
            return True

        # attempt graceful shutdown
        self.logger.info(f"shutting down VM {vm_name}...")
        virsh.execute('shutdown', vm_name, warn=True)

        # poll for shut off
        waited = 0
        poll_interval = 2
        while waited < timeout:
            time.sleep(poll_interval)
            waited += poll_interval
            result = virsh.execute('domstate', vm_name, warn=True)
            if result.ok and 'shut off' in result.stdout:
                self.logger.info(f"VM {vm_name} is shut off after {waited}s")
                return True

        # force stop as fallback
        self.logger.warning(
            f"VM {vm_name} did not shut off within {timeout}s, forcing stop")
        virsh.execute('destroy', vm_name, warn=True)
        time.sleep(2)

        result = virsh.execute('domstate', vm_name, warn=True)
        if result.ok and 'shut off' in result.stdout:
            return True

        self.logger.error(f"failed to shut off VM {vm_name}")
        return False

    def update_vm_cpu_memory(self,
                             vm_name: str,
                             cpus: dict[str, int] | None,
                             memory_mb: int | None,
                             vm_state: str,
                             actual_cpus: dict[str, int],
                             actual_memory_mb: int,
                             max_vcpus: int | None = None,
                             max_memory_mb: int | None = None) -> dict[str, Any]:
        """
        Apply CPU and/or memory changes, choosing hot or cold path.

        Args:
            vm_name: Full VM domain name
            cpus: Desired CPU topology (sockets, cores, threads) or None
            memory_mb: Desired memory in MiB or None
            vm_state: Current VM state string
            actual_cpus: Current CPU topology dict
            actual_memory_mb: Current memory in MiB
            max_vcpus: Desired max vCPU ceiling or None
            max_memory_mb: Desired max memory ceiling in MiB or None

        Returns:
            Dict with 'success', 'method' ('hot'/'cold'), 'restart_needed' keys
        """
        editor = VirshEdit(provider_config=self.provider_config)
        is_running = vm_state == 'running'

        if not is_running:
            # VM is stopped — use cold XML redefine
            success = editor.configure_cpu_memory(
                vm_name, cpus, memory_mb,
                max_vcpus=max_vcpus, max_memory_mb=max_memory_mb)
            return {'success': success, 'method': 'cold', 'restart_needed': False}

        # VM is running — update persistent config and apply live where
        # possible. Libvirt does NOT allow raising the live maximum vCPU
        # or memory ceiling on a running VM, so increases beyond the
        # current max require a restart.
        #
        # Strategy:
        # 1. Update the persistent (inactive) config in ONE shot via
        #    virsh dumpxml --inactive + modify + virsh define.
        # 2. For changes within the current live ceiling: hot-set live.
        # 3. For changes exceeding the ceiling: flag restart_needed.

        # --- Step 1: update persistent config (inactive XML) ---
        xml_content = editor.get_domain_xml(vm_name, inactive=True)
        modifications = []

        desired_total = None
        if cpus:
            sockets = cpus.get('sockets', 1)
            cores = cpus.get('cores', 1)
            threads = cpus.get('threads', 1)
            desired_total = sockets * cores * threads

            effective_max_vcpus = max_vcpus or desired_total
            if max_vcpus is not None and max_vcpus < desired_total:
                effective_max_vcpus = desired_total

            modifications.append(('//vcpu', 'text', str(effective_max_vcpus)))

            if effective_max_vcpus > desired_total:
                # libvirt requires topology product == max vcpu count.
                # scale sockets so that sockets * cores * threads == max.
                cores_x_threads = cores * threads
                if effective_max_vcpus % cores_x_threads == 0:
                    max_sockets = effective_max_vcpus // cores_x_threads
                    modifications.extend([
                        ('//cpu/topology', 'sockets', str(max_sockets)),
                        ('//cpu/topology', 'cores', str(cores)),
                        ('//cpu/topology', 'threads', str(threads)),
                    ])
                else:
                    # can't express with given cores*threads, remove topology
                    from lxml import etree
                    tree = etree.fromstring(xml_content.encode('utf-8'))
                    for topo in tree.xpath('//cpu/topology'):
                        topo.getparent().remove(topo)
                    xml_content = etree.tostring(
                        tree, encoding='unicode', pretty_print=True)

                modifications.append(
                    ('//vcpu', 'current', str(desired_total)))
            else:
                modifications.extend([
                    ('//cpu/topology', 'sockets', str(sockets)),
                    ('//cpu/topology', 'cores', str(cores)),
                    ('//cpu/topology', 'threads', str(threads)),
                ])

        if memory_mb is not None and (memory_mb != actual_memory_mb or max_memory_mb is not None):
            effective_max_memory = max_memory_mb or memory_mb
            if max_memory_mb is not None and max_memory_mb < memory_mb:
                effective_max_memory = memory_mb
            max_memory_kib = effective_max_memory * 1024
            current_memory_kib = memory_mb * 1024
            modifications.extend([
                ('//memory', 'text', str(max_memory_kib)),
                ('//currentMemory', 'text', str(current_memory_kib)),
            ])

        if modifications:
            modified_xml = editor.modify_xml_xpath(xml_content, modifications)
            if not editor.redefine_domain(vm_name, modified_xml):
                self.logger.error(
                    f"VM {vm_name}: failed to update persistent config")
                return {'success': False, 'method': 'hot', 'restart_needed': False}

        # --- Step 2 & 3: live changes ---
        restart_needed = False
        success = True

        if cpus and desired_total:
            current_max = actual_cpus.get('total_vcpus', 1)
            if desired_total <= current_max:
                if not editor.hot_set_vcpus(vm_name, desired_total):
                    # hot-unplug may not be supported; fall back to restart
                    self.logger.info(
                        f"VM {vm_name}: live vCPU change failed, "
                        f"will restart to apply")
                    restart_needed = True
            else:
                # live max can't be raised on a running VM
                restart_needed = True

        if memory_mb is not None and memory_mb != actual_memory_mb:
            from .vm_differ import VMStateDiffer
            differ = VMStateDiffer(provider_config=self.provider_config)
            current_max_mem = differ.get_max_memory_mb(vm_name)

            if memory_mb <= current_max_mem:
                if not editor.hot_set_memory(vm_name, memory_mb):
                    # hot memory change failed; fall back to restart
                    self.logger.info(
                        f"VM {vm_name}: live memory change failed, "
                        f"will restart to apply")
                    restart_needed = True
            else:
                # live max can't be raised on a running VM
                restart_needed = True

        if restart_needed:
            self.logger.info(
                f"VM {vm_name}: persistent config updated. "
                f"Restart needed for changes to take effect "
                f"(live max ceiling cannot be raised on a running VM).")

        method = 'cold' if restart_needed else 'hot'
        return {'success': success, 'method': method, 'restart_needed': restart_needed}

    def update_vm_disks(self,
                        vm_name: str,
                        new_disks: list[dict[str, Any]],
                        resize_disks: list[dict[str, Any]],
                        workdir: str,
                        disk_prefix: str,
                        vm_running: bool) -> bool:
        """
        Apply disk changes: create+attach new disks and resize existing ones.

        Args:
            vm_name: Full VM domain name
            new_disks: List of disk configs to create and attach
            resize_disks: List of dicts with target, source, desired_size_mb
            workdir: Working directory for disk images
            disk_prefix: Prefix for disk image filenames
            vm_running: Whether the VM is currently running

        Returns:
            True if all operations succeeded, False otherwise
        """
        success = True
        disk_manager = DiskManager(vm_name=vm_name, provider_config=self.provider_config)

        # create and attach new disks
        for disk_config in new_disks:
            self.logger.info(
                f"adding new disk '{disk_config.get('name', 'disk')}' "
                f"to VM {vm_name}")
            if not disk_manager.configure_from_disk_config(
                disk_config=disk_config,
                workdir=workdir,
                disk_prefix=disk_prefix
            ):
                self.logger.error(
                    f"failed to add disk '{disk_config.get('name')}' to {vm_name}")
                success = False

        # resize existing disks
        for resize_info in resize_disks:
            self.logger.info(
                f"resizing disk {resize_info['target']} on {vm_name} from "
                f"{resize_info['current_size_mb']}M to {resize_info['desired_size_mb']}M")
            if not disk_manager.resize_disk(
                disk_path=resize_info['source'],
                target_dev=resize_info['target'],
                new_size_mb=resize_info['desired_size_mb'],
                vm_running=vm_running
            ):
                self.logger.error(
                    f"failed to resize disk {resize_info['target']} on {vm_name}")
                success = False

        return success
