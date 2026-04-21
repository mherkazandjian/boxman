import os
import tempfile
from typing import Any

from .commands import VirshCommand
from .virsh_edit import VirshEdit


class SharedFolderManager(VirshCommand):
    """
    Class for managing shared folder (filesystem passthrough) operations in libvirt.

    Uses virtiofs for host directory sharing with VMs. Virtiofs requires
    memfd memory backing in the domain XML and QEMU 6.2+/libvirt 8.6+ for
    hotplug support.
    """

    def __init__(self, vm_name: str, provider_config: dict[str, Any] | None = None):
        super().__init__(provider_config)
        self.vm_name = vm_name

    def attach_shared_folder(self,
                             name: str,
                             host_path: str,
                             readonly: bool = False,
                             persistent: bool = True) -> dict[str, Any]:
        """
        Attach a host directory as a virtiofs filesystem to the VM.

        Tries live + persistent attachment first. If live attach fails
        (e.g. older QEMU without virtiofs hotplug), falls back to
        config-only (persistent for next boot).

        Args:
            name: Mount tag for the guest (used in mount -t virtiofs <name> /mnt)
            host_path: Path on the host to share
            readonly: Whether the share is read-only
            persistent: Whether to make the attachment persistent

        Returns:
            Dict with 'success' (bool) and 'restart_needed' (bool)
        """
        try:
            host_path = os.path.abspath(os.path.expanduser(host_path))
            if not os.path.isdir(host_path):
                self.logger.error(f"host path does not exist or is not a directory: {host_path}")
                return {'success': False, 'restart_needed': False}

            xml_content = self._generate_filesystem_xml(name, host_path, readonly)

            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            # Try live + persistent first
            attachment_args = ["--persistent"] if persistent else []
            result = self.execute(
                "attach-device", self.vm_name, temp_path, *attachment_args,
                warn=True)

            os.unlink(temp_path)

            if result.ok:
                self.logger.info(
                    f"attached shared folder '{name}' ({host_path}) "
                    f"to VM {self.vm_name}")
                return {'success': True, 'restart_needed': False}

            # Live attach failed — try config-only (will apply on next boot)
            self.logger.warning(
                f"live attach of shared folder '{name}' failed, "
                f"trying config-only: {result.stderr.strip()}")

            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            result = self.execute(
                "attach-device", self.vm_name, temp_path, "--config",
                warn=True)

            os.unlink(temp_path)

            if result.ok:
                self.logger.info(
                    f"shared folder '{name}' configured for VM {self.vm_name} "
                    f"(will be available after restart)")
                return {'success': True, 'restart_needed': True}

            self.logger.error(
                f"failed to attach shared folder '{name}': {result.stderr}")
            return {'success': False, 'restart_needed': False}

        except Exception as e:
            self.logger.error(f"error attaching shared folder '{name}': {e}")
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
            return {'success': False, 'restart_needed': False}

    def detach_shared_folder(self, name: str, host_path: str,
                             readonly: bool = False) -> dict[str, Any]:
        """
        Detach a shared folder from the VM.

        Args:
            name: Mount tag of the shared folder
            host_path: Host path (needed to generate matching XML)
            readonly: Whether the share was read-only

        Returns:
            Dict with 'success' (bool) and 'restart_needed' (bool)
        """
        try:
            xml_content = self._generate_filesystem_xml(name, host_path, readonly)

            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            # Try live + persistent
            result = self.execute(
                "detach-device", self.vm_name, temp_path, "--persistent",
                warn=True)

            os.unlink(temp_path)

            if result.ok:
                self.logger.info(
                    f"detached shared folder '{name}' from VM {self.vm_name}")
                return {'success': True, 'restart_needed': False}

            # Try config-only
            self.logger.warning(
                f"live detach of shared folder '{name}' failed, "
                f"trying config-only: {result.stderr.strip()}")

            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            result = self.execute(
                "detach-device", self.vm_name, temp_path, "--config",
                warn=True)

            os.unlink(temp_path)

            if result.ok:
                self.logger.info(
                    f"shared folder '{name}' will be removed from VM "
                    f"{self.vm_name} after restart")
                return {'success': True, 'restart_needed': True}

            self.logger.error(
                f"failed to detach shared folder '{name}': {result.stderr}")
            return {'success': False, 'restart_needed': False}

        except Exception as e:
            self.logger.error(f"error detaching shared folder '{name}': {e}")
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
            return {'success': False, 'restart_needed': False}

    def ensure_memfd_backing(self) -> dict[str, Any]:
        """
        Ensure the VM has memfd memory backing required by virtiofs.

        Checks domain XML for <memoryBacking><source type='memfd'/></memoryBacking>.
        If missing, adds it via VirshEdit. This change requires a VM restart
        if the VM is currently running.

        Returns:
            Dict with 'success' (bool) and 'restart_needed' (bool)
        """
        try:
            editor = VirshEdit(provider_config=self.provider_config)
            xml_content = editor.get_domain_xml(self.vm_name, inactive=True)

            from lxml import etree
            tree = etree.fromstring(xml_content.encode('utf-8'))

            # Check if memfd backing already exists
            memfd_sources = tree.xpath(
                '//memoryBacking/source[@type="memfd"]')
            if memfd_sources:
                return {'success': True, 'restart_needed': False}

            # Add memoryBacking element
            mem_backing = tree.find('memoryBacking')
            if mem_backing is None:
                mem_backing = etree.SubElement(tree, 'memoryBacking')

            source_elem = mem_backing.find('source')
            if source_elem is None:
                source_elem = etree.SubElement(mem_backing, 'source')
            source_elem.set('type', 'memfd')

            modified_xml = etree.tostring(tree, encoding='unicode', pretty_print=True)
            success = editor.redefine_domain(self.vm_name, modified_xml)

            if not success:
                self.logger.error(
                    f"failed to add memfd backing to VM {self.vm_name}")
                return {'success': False, 'restart_needed': False}

            # Check if VM is running — if so, the memfd change needs a restart
            result = self.execute("domstate", self.vm_name, warn=True)
            is_running = result.ok and 'running' in result.stdout

            if is_running:
                self.logger.info(
                    f"memfd memory backing added to VM {self.vm_name} "
                    f"(restart needed for virtiofs to work)")
                return {'success': True, 'restart_needed': True}

            self.logger.info(f"memfd memory backing added to VM {self.vm_name}")
            return {'success': True, 'restart_needed': False}

        except Exception as e:
            self.logger.error(
                f"error ensuring memfd backing for VM {self.vm_name}: {e}")
            return {'success': False, 'restart_needed': False}

    def configure_from_config(self, folder_config: dict[str, Any]) -> dict[str, Any]:
        """
        Configure a shared folder from a configuration dictionary.

        Ensures memfd backing is present, then attaches the shared folder.

        Args:
            folder_config: Dictionary with 'name', 'host_path', and
                          optional 'readonly' keys

        Returns:
            Dict with 'success' (bool) and 'restart_needed' (bool)
        """
        name = folder_config.get('name')
        host_path = folder_config.get('host_path')
        readonly = folder_config.get('readonly', False)

        if not name:
            self.logger.error("shared folder config missing 'name' field")
            return {'success': False, 'restart_needed': False}
        if not host_path:
            self.logger.error("shared folder config missing 'host_path' field")
            return {'success': False, 'restart_needed': False}

        # Ensure memfd backing for virtiofs
        memfd_result = self.ensure_memfd_backing()
        if not memfd_result['success']:
            return memfd_result

        attach_result = self.attach_shared_folder(
            name=name, host_path=host_path, readonly=readonly)

        # Combine restart signals
        restart_needed = (
            memfd_result.get('restart_needed', False) or
            attach_result.get('restart_needed', False)
        )

        return {
            'success': attach_result['success'],
            'restart_needed': restart_needed,
        }

    def get_attached_shared_folders(self) -> list[dict[str, Any]]:
        """
        Get all filesystem (shared folder) devices currently attached to the VM.

        Returns:
            List of dicts with 'name', 'host_path', and 'readonly' keys.
        """
        from lxml import etree

        editor = VirshEdit(provider_config=self.provider_config)
        xml_content = editor.get_domain_xml(self.vm_name)

        tree = etree.fromstring(xml_content.encode('utf-8'))

        folders = []
        for fs_elem in tree.xpath('//devices/filesystem'):
            source_elem = fs_elem.find('source')
            target_elem = fs_elem.find('target')
            if source_elem is None or target_elem is None:
                continue

            host_path = source_elem.get('dir', '')
            name = target_elem.get('dir', '')
            readonly = fs_elem.find('readonly') is not None

            if name and host_path:
                folders.append({
                    'name': name,
                    'host_path': host_path,
                    'readonly': readonly,
                })

        return folders

    def _generate_filesystem_xml(self,
                                 name: str,
                                 host_path: str,
                                 readonly: bool = False) -> str:
        """
        Generate XML for virtiofs filesystem attachment.

        Args:
            name: Mount tag for the guest
            host_path: Absolute path on the host
            readonly: Whether the share is read-only

        Returns:
            XML string for filesystem attachment
        """
        host_path = os.path.abspath(os.path.expanduser(host_path))
        readonly_elem = "\n  <readonly/>" if readonly else ""
        return f"""<filesystem type='mount' accessmode='passthrough'>
  <driver type='virtiofs'/>
  <source dir='{host_path}'/>
  <target dir='{name}'/>{readonly_elem}
</filesystem>"""
