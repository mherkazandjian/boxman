import os
import uuid
import re
from typing import Optional, Dict, Any
from boxman import log

from .commands import VirtCloneCommand, VirshCommand


class CloneVM:
    """
    Class to clone VMs in libvirt using virt-clone and virsh commands.
    """

    def __init__(self,
                src_vm_name: str,
                new_vm_name: str,
                info: Dict[str, Any],
                workdir: Optional[str] = None,
                provider_config: Optional[Dict[str, Any]] = None):
        """
        Initialize the VM cloning operation.

        Args:
            src_vm_name: Name of the source VM
            new_vm_name: Name of the new VM
            info: Dictionary containing VM configuration
            provider_config: Configuration for the libvirt provider
        """
        #: str: the name of the source vm
        self.src_vm_name = src_vm_name

        #: str: the name of the new vm
        self.new_vm_name = new_vm_name

        #: str: the path to the disk image
        self.new_image_path = os.path.expanduser(os.path.join(workdir, f'{new_vm_name}.qcow2'))

        #: VirtCloneCommand: the command executor for virt-clone
        self.virt_clone = VirtCloneCommand(provider_config)

        #: VirshCommand: the command executor for virsh
        self.virsh = VirshCommand(provider_config)

        #: logging.Logger: the logger instance
        self.logger = log

    def create_clone(self) -> bool:
        """
        Clone a VM using virt-clone.

        Returns:
            True if successful, False otherwise
        """
        try:
            cmd_args = []
            cmd_kwargs = {
                'original': self.src_vm_name,
                'name': self.new_vm_name,
                'file': self.new_image_path,
                'auto_clone': True
            }

            self.logger.info(f"cloning the vm {self.src_vm_name} to {self.new_vm_name}")
            self.virt_clone.execute(*cmd_args, **cmd_kwargs)

            # after cloning, remove all inherited network interfaces
            if not self.remove_network_interfaces():
                self.logger.warning(
                    f"failed to remove network interfaces from the vm {self.new_vm_name}")

            return True
        except RuntimeError as exc:
            self.logger.error(f"Error cloning the vm: {exc}")
            return False

    def clone(self) -> bool:
        """
        Clone the vm and start it.

        Returns:
            True if all operations were successful, False otherwise
        """
        if not self.create_clone():
            return False

        return True

    def remove_network_interfaces(self) -> bool:
        """
        Remove all network interfaces from the cloned vm.

        This ensures we start with a clean slate and can add the interfaces
        specified in the configuration.

        Returns:
            True if successful, False otherwise
        """
        try:
            vm_name = self.new_vm_name
            # use virsh domiflist to get the network interfaces
            result = self.virsh.execute("domiflist", vm_name)
            if not result.ok:
                self.logger.error(f"Failed to get interface list for VM {vm_name}")
                return False

            # parse the output to extract interface information
            # output format is like:
            # Interface  Type       Source     Model       MAC
            # -------------------------------------------------------
            # vnet0      network    default    virtio      52:54:00:xx:xx:xx

            interfaces = []
            lines = result.stdout.strip().split('\n')
            if len(lines) > 2:  # Skip header and separator lines
                for line in lines[2:]:
                    parts = line.split()
                    if len(parts) >= 5:  # Interface Type Source Model MAC
                        iface_type = parts[1]
                        source = parts[2]
                        mac = parts[4]
                        interfaces.append((iface_type, source, mac))

            self.logger.info(
                f"found {len(interfaces)} network interfaces to remove from the vm {vm_name}")

            # Remove each interface
            for iface_type, source, mac in interfaces:
                self.logger.info(f"removing interface with MAC {mac} from the vm {vm_name}")

                # Use the detach-interface command with the correct type and MAC
                remove_result = self.virsh.execute(
                    "detach-interface",
                    self.new_vm_name,
                    iface_type,  # Use the actual interface type from domiflist
                    f"--mac={mac}",
                    "--config",  # Make change persistent
                    warn=True
                )

                if not remove_result.ok:
                    self.logger.warning(
                        f"failed to remove interface with mac {mac}: {remove_result.stderr}")
                else:
                    self.logger.info(f"successfully removed interface with mac {mac}")

            return True
        except Exception as exc:
            self.logger.error(f"Error removing network interfaces: {exc}")
            return False
