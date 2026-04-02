import os
from typing import Optional, Dict, Any, List
import tempfile

from .commands import VirshCommand

from boxman import log


class CDROMManager(VirshCommand):
    """
    Class for managing CDROM/ISO device operations in libvirt.

    Handles attaching ISO images as CDROM devices to VMs, detaching them,
    and swapping media in existing CDROM slots.
    """

    def __init__(self, vm_name: str, provider_config: Optional[Dict[str, Any]] = None):
        super().__init__(provider_config)
        self.vm_name = vm_name

    def attach_cdrom(self,
                     source_path: str,
                     target_dev: Optional[str] = None,
                     persistent: bool = True) -> bool:
        """
        Attach an ISO image as a CDROM device to the VM.

        Args:
            source_path: Absolute path to the ISO image
            target_dev: Target device name (e.g., 'hdc'). Auto-assigned if None.
            persistent: Whether to make the attachment persistent

        Returns:
            True if successful, False otherwise
        """
        try:
            source_path = os.path.abspath(os.path.expanduser(source_path))
            if not os.path.isfile(source_path):
                self.logger.error(f"ISO file does not exist: {source_path}")
                return False

            if target_dev is None:
                target_dev = self._find_next_available_target()
                if target_dev is None:
                    self.logger.error(
                        f"could not find available CDROM target for VM {self.vm_name}")
                    return False

            xml_content = self._generate_cdrom_xml(source_path, target_dev)

            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            attachment_args = ["--persistent"] if persistent else []
            result = self.execute("attach-device", self.vm_name, temp_path, *attachment_args)

            os.unlink(temp_path)

            if not result.ok:
                self.logger.error(f"failed to attach CDROM: {result.stderr}")
                return False

            self.logger.info(
                f"attached CDROM {source_path} as {target_dev} on VM {self.vm_name}")
            return True
        except Exception as e:
            self.logger.error(f"error attaching CDROM: {e}")
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
            return False

    def detach_cdrom(self, target_dev: str, persistent: bool = True) -> bool:
        """
        Detach a CDROM device from the VM.

        Args:
            target_dev: Target device name to detach (e.g., 'hdc')
            persistent: Whether to make the detachment persistent

        Returns:
            True if successful, False otherwise
        """
        try:
            # Generate XML for the device to detach (source not needed for detach)
            xml_content = f"""<disk type='file' device='cdrom'>
  <target dev='{target_dev}' bus='ide'/>
  <readonly/>
</disk>"""

            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            detach_args = ["--persistent"] if persistent else []
            result = self.execute("detach-device", self.vm_name, temp_path, *detach_args)

            os.unlink(temp_path)

            if not result.ok:
                self.logger.error(
                    f"failed to detach CDROM {target_dev} from {self.vm_name}: {result.stderr}")
                return False

            self.logger.info(f"detached CDROM {target_dev} from VM {self.vm_name}")
            return True
        except Exception as e:
            self.logger.error(f"error detaching CDROM: {e}")
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
            return False

    def change_media(self, target_dev: str, source_path: str) -> bool:
        """
        Swap ISO media in an existing CDROM slot.

        Args:
            target_dev: Target device name (e.g., 'hdc')
            source_path: Path to the new ISO image

        Returns:
            True if successful, False otherwise
        """
        try:
            source_path = os.path.abspath(os.path.expanduser(source_path))
            if not os.path.isfile(source_path):
                self.logger.error(f"ISO file does not exist: {source_path}")
                return False

            result = self.execute(
                "change-media", self.vm_name, target_dev,
                source_path, "--live", "--config",
                warn=True)

            if not result.ok:
                self.logger.error(
                    f"failed to change media on {target_dev}: {result.stderr}")
                return False

            self.logger.info(
                f"changed CDROM media on {target_dev} to {source_path} "
                f"on VM {self.vm_name}")
            return True
        except Exception as e:
            self.logger.error(f"error changing CDROM media: {e}")
            return False

    def configure_from_config(self, cdrom_config: Dict[str, Any]) -> bool:
        """
        Configure a CDROM device from a configuration dictionary.

        Args:
            cdrom_config: Dictionary with 'name', 'source', and optional 'target' keys

        Returns:
            True if successful, False otherwise
        """
        source = cdrom_config.get('source')
        if not source:
            self.logger.error("CDROM config missing 'source' field")
            return False

        target = cdrom_config.get('target')
        return self.attach_cdrom(source_path=source, target_dev=target)

    def get_attached_cdroms(self) -> List[Dict[str, Any]]:
        """
        Get all CDROM devices currently attached to the VM.

        Returns a list of dicts with 'target' and 'source' keys.
        Excludes seed ISOs (used for cloud-init).
        """
        result = self.execute("domblklist", self.vm_name, "--details", warn=True)
        if not result.ok:
            return []

        cdroms = []
        lines = result.stdout.strip().split('\n')
        for line in lines[2:]:
            parts = line.split()
            if len(parts) < 3:
                continue
            device = parts[1]
            target = parts[2]
            source = parts[3] if len(parts) >= 4 else '-'

            if device != 'cdrom':
                continue
            if source == '-':
                continue
            # exclude seed ISOs (cloud-init)
            if os.path.basename(source).startswith('seed'):
                continue

            cdroms.append({
                'target': target,
                'source': source,
            })

        return cdroms

    def _generate_cdrom_xml(self, source_path: str, target_dev: str) -> str:
        """
        Generate XML for CDROM device attachment.

        Args:
            source_path: Absolute path to ISO image
            target_dev: Target device name

        Returns:
            XML string for CDROM attachment
        """
        return f"""<disk type='file' device='cdrom'>
  <driver name='qemu' type='raw'/>
  <source file='{source_path}'/>
  <target dev='{target_dev}' bus='ide'/>
  <readonly/>
</disk>"""

    def _find_next_available_target(self) -> Optional[str]:
        """
        Find the next available IDE target device for a CDROM.

        Parses domblklist to find which hdX devices are in use and
        returns the first available one.

        Returns:
            Device name (e.g., 'hdc') or None if no slot available
        """
        result = self.execute("domblklist", self.vm_name, "--details", warn=True)

        used_targets = set()
        if result.ok:
            lines = result.stdout.strip().split('\n')
            for line in lines[2:]:
                parts = line.split()
                if len(parts) >= 3:
                    used_targets.add(parts[2])

        # IDE supports hda-hdd (4 devices)
        for suffix in ('a', 'b', 'c', 'd'):
            candidate = f'hd{suffix}'
            if candidate not in used_targets:
                return candidate

        # Fall back to sata targets
        for i in range(10):
            candidate = f'sd{chr(ord("a") + i)}'
            if candidate not in used_targets:
                return candidate

        return None
