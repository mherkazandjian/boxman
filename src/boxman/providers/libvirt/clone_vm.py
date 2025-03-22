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
                 config: Dict[str, Any],
                 provider_config: Optional[Dict[str, Any]] = None):
        """
        Initialize the VM cloning operation.

        Args:
            name: Name of the new VM
            config: Dictionary containing VM configuration
            provider_config: Configuration for the libvirt provider
        """
        #: str: Name of the new VM
        self.new_vm_name = new_vm_name

        #: Dict[str, Any]: VM configuration
        self.vm_config = config

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
                'original': self.original,
                'name': self.new_vm_name,
                'file': self.disk_path,
                'auto_clone': True
            }

            # Add MAC address if specified
            if self.mac_address:
                cmd_kwargs['mac'] = self.mac_address

            self.logger.info(f"Cloning VM {self.original} to {self.new_vm_name}")
            self.virt_clone.execute(*cmd_args, **cmd_kwargs)

            # Step 2: Dump the XML configuration
            xml_temp_path = f"/tmp/{self.new_vm_name}.xml"
            self.logger.info(f"Dumping XML configuration to {xml_temp_path}")
            self.virsh.execute("dumpxml", self.new_vm_name, ">", xml_temp_path)

            # Step 3: Move the XML to the specified location if provided
            if self.xml_path:
                os.makedirs(os.path.dirname(self.xml_path), exist_ok=True)
                os.rename(xml_temp_path, self.xml_path)
                self.logger.info(f"Moved XML configuration to {self.xml_path}")

                # Step 4: Define the VM with the XML file
                self.logger.info(f"Defining VM from {self.xml_path}")
                self.virsh.execute("define", "--validate", self.xml_path)

            return True
        except RuntimeError as e:
            self.logger.error(f"Error cloning VM: {e}")
            return False

    def start_vm(self) -> bool:
        """
        Start the cloned VM.

        Returns:
            True if successful, False otherwise
        """
        try:
            self.logger.info(f"Starting VM {self.new_vm_name}")
            self.virsh.execute("start", self.new_vm_name)
            return True
        except RuntimeError as e:
            self.logger.error(f"Error starting VM: {e}")
            return False

    def remove_vm(self) -> bool:
        """
        Remove the VM (undefine).

        Returns:
            True if successful, False otherwise
        """
        try:
            # First, check if VM is running and shut it down
            result = self.virsh.execute("domstate", self.new_vm_name, warn=True)
            if result.ok and "running" in result.stdout:
                self.logger.info(f"Stopping VM {self.new_vm_name}")
                self.virsh.execute("shutdown", self.new_vm_name)

                # Wait for VM to shut down
                import time
                for _ in range(30):  # Wait up to 30 seconds
                    result = self.virsh.execute("domstate", self.new_vm_name, warn=True)
                    if "shut off" in result.stdout:
                        break
                    time.sleep(1)

                # Force destroy if still running
                result = self.virsh.execute("domstate", self.new_vm_name, warn=True)
                if result.ok and "running" in result.stdout:
                    self.logger.info(f"Forcing VM {self.new_vm_name} to stop")
                    self.virsh.execute("destroy", self.new_vm_name)

            # Undefine the VM
            self.logger.info(f"Removing VM {self.new_vm_name}")
            self.virsh.execute("undefine", self.new_vm_name, "--remove-all-storage")
            return True
        except RuntimeError as e:
            self.logger.error(f"Error removing VM: {e}")
            return False

    def clone_and_start(self) -> bool:
        """
        Clone the VM and start it.

        Returns:
            True if all operations were successful, False otherwise
        """
        if not self.create_clone():
            return False

        if not self.start_vm():
            return False

        return True
