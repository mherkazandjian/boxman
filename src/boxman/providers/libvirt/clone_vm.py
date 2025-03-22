import os
import uuid
from typing import Optional, Dict, Any, List, Union
import logging

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
        #: str: Name of the source VM
        self.src_vm_name = src_vm_name

        #: str: Name of the new VM
        self.new_vm_name = new_vm_name

        #: str: Path to the disk image
        self.new_image_path = os.path.expanduser(
            os.path.join(workdir, f'{new_vm_name}.qcow2'))

        #: VirtCloneCommand: Command executor for virt-clone
        self.virt_clone = VirtCloneCommand(provider_config)

        #: VirshCommand: Command executor for virsh
        self.virsh = VirshCommand(provider_config)

        #: logging.Logger: Logger instance
        self.logger = logging.getLogger(__name__)

    def create_clone(self) -> bool:
        """
        Clone a VM using virt-clone.

        Returns:
            True if successful, False otherwise
        """
        try:
            # Step 1: Clone the VM
            cmd_args = []
            cmd_kwargs = {
                'original': self.src_vm_name,
                'name': self.new_vm_name,
                'file': self.new_image_path,
                'auto_clone': True
            }

            self.logger.info(f"Cloning VM {self.src_vm_name} to {self.new_vm_name}")
            self.virt_clone.execute(*cmd_args, **cmd_kwargs)

            ## Step 2: Dump the XML configuration
            #xml_temp_path = f"/tmp/{self.new_vm_name}.xml"
            #self.logger.info(f"Dumping XML configuration to {xml_temp_path}")
            #self.virsh.execute("dumpxml", self.new_vm_name, ">", xml_temp_path)

            ## Step 3: Move the XML to the specified location if provided
            #if self.xml_path:
            #    os.makedirs(os.path.dirname(self.xml_path), exist_ok=True)
            #    os.rename(xml_temp_path, self.xml_path)
            #    self.logger.info(f"Moved XML configuration to {self.xml_path}")

            #    # Step 4: Define the VM with the XML file
            #    self.logger.info(f"Defining VM from {self.xml_path}")
            #    self.virsh.execute("define", "--validate", self.xml_path)

            return True
        except RuntimeError as e:
            self.logger.error(f"Error cloning VM: {e}")
            return False

    def clone(self) -> bool:
        """
        Clone the VM and start it.

        Returns:
            True if all operations were successful, False otherwise
        """
        if not self.create_clone():
            return False

        return True
