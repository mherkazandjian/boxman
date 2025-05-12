import os
from typing import Optional, Dict, Any, List
import tempfile
from jinja2 import Environment, FileSystemLoader
import pkg_resources

from .commands import LibVirtCommandBase, VirshCommand


class DiskManager(VirshCommand):
    """
    Class for managing VM disk operations in libvirt.

    This class handles creating disk images with qemu-img and attaching them to VMs.
    """

    def __init__(self, vm_name: str, provider_config: Optional[Dict[str, Any]] = None):
        """
        Initialize the disk manager.

        Args:
            vm_name: Name of the VM to manage disks for
            provider_config: Configuration for the libvirt provider
        """
        super().__init__(provider_config)

        #: str: the name of the VM
        self.vm_name = vm_name

    def create_disk(self, disk_path: str, size: int, format: str = 'qcow2') -> bool:
        """
        Create a disk image using qemu-img.

        Args:
            disk_path: Path where the disk image will be created
            size: Size of the disk in MiB
            format: Disk format (default: qcow2)

        Returns:
            True if successful, False otherwise
        """
        try:
            # ensure the directory exists
            disk_dir = os.path.dirname(os.path.expanduser(disk_path))
            os.makedirs(disk_dir, exist_ok=True)

            # convert size to proper format (MB to bytes)
            size_in_mb = size

            # build qemu-img command
            cmd = f"qemu-img create -f {format} {disk_path} {size_in_mb}M"
            self.logger.info(f"creating disk image: {cmd}")

            # execute the command
            cmd_executor = LibVirtCommandBase(
                provider_config=self.provider_config,
                override_config_use_sudo=False)
            result = cmd_executor.execute_shell(cmd)

            if not result.ok:
                self.logger.error(f"failed to create disk image: {result.stderr}")
                return False

            self.logger.info(f"successfully created disk image at {disk_path}")
            return True
        except Exception as exc:
            self.logger.error(f"error creating disk image: {exc}")
            return False

    def attach_disk(self,
                   disk_path: str,
                   target_dev: str,
                   driver_name: str = 'qemu',
                   driver_type: str = 'qcow2',
                   persistent: bool = True) -> bool:
        """
        Attach a disk to the VM.

        Args:
            disk_path: Path to the disk image
            target_dev: Target device name (e.g., 'vdb')
            driver_name: Name of the disk driver (default: qemu)
            driver_type: Type of the disk driver (default: qcow2)
            persistent: Whether to make the attachment persistent

        Returns:
            True if successful, False otherwise
        """
        try:
            # Generate an XML file for the disk attachment
            xml_content = self._generate_disk_xml(
                disk_path=disk_path,
                target_dev=target_dev,
                driver_name=driver_name,
                driver_type=driver_type
            )

            # Create a temporary file for the XML
            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            # Attach the disk
            attachment_args = ["--persistent"] if persistent else []
            result = self.execute("attach-device", self.vm_name, temp_path, *attachment_args)

            # Clean up the temporary file
            os.unlink(temp_path)

            if not result.ok:
                self.logger.error(f"Failed to attach disk: {result.stderr}")
                return False

            self.logger.info(f"Successfully attached disk {disk_path} to VM {self.vm_name}")
            return True
        except Exception as e:
            self.logger.error(f"Error attaching disk: {e}")
            # Clean up temp file if it exists
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
            return False

    def _generate_disk_xml(self,
                          disk_path: str,
                          target_dev: str,
                          driver_name: str,
                          driver_type: str) -> str:
        """
        Generate XML for disk attachment.

        Args:
            disk_path: Path to the disk image
            target_dev: Target device name
            driver_name: Name of the disk driver
            driver_type: Type of the disk driver

        Returns:
            XML string for disk attachment
        """
        # Simple XML template for disk attachment
        return f"""<disk type='file' device='disk'>
  <driver name='{driver_name}' type='{driver_type}'/>
  <source file='{os.path.abspath(os.path.expanduser(disk_path))}'/>
  <target dev='{target_dev}'/>
</disk>"""

    def configure_from_disk_config(self,
                                  disk_config: Dict[str, Any],
                                  workdir: str,
                                  disk_prefix: str = "") -> bool:
        """
        Configure a disk from configuration.

        Args:
            disk_config: Dictionary with disk configuration
            workdir: Working directory for disk images
            disk_prefix: Prefix to add to disk image filename

        Returns:
            True if successful, False otherwise
        """
        try:
            # Extract configuration
            disk_name = disk_config.get("name", "disk")
            disk_size = disk_config.get("size", 1024)  # Default 1GB

            # Get driver info
            driver = disk_config.get("driver", {})
            driver_name = driver.get("name", "qemu")
            driver_type = driver.get("type", "qcow2")

            # Get target device
            target_dev = disk_config.get("target", "vdb")

            # Create disk path
            if disk_prefix:
                disk_path = os.path.join(workdir, f"{disk_prefix}_{disk_name}.{driver_type}")
            else:
                disk_path = os.path.join(workdir, f"{disk_name}.{driver_type}")

            # Ensure path is expanded
            disk_path = os.path.expanduser(disk_path)

            # 1. Create the disk
            if not self.create_disk(disk_path, disk_size, format=driver_type):
                self.logger.error(f"Failed to create disk {disk_path}")
                return False

            # 2. Attach the disk to the VM
            if not self.attach_disk(
                disk_path=disk_path,
                target_dev=target_dev,
                driver_name=driver_name,
                driver_type=driver_type
            ):
                self.logger.error(f"Failed to attach disk {disk_path} to VM {self.vm_name}")
                return False

            self.logger.info(f"Successfully configured disk {disk_name} for VM {self.vm_name}")
            return True
        except Exception as e:
            self.logger.error(f"Error configuring disk from config: {e}")
            return False
