import os
import time
from typing import Optional, Dict, Any, List, Union

from .commands import VirshCommand


class DestroyVM(VirshCommand):
    """
    Class to destroy (remove) VMs in libvirt using virsh commands.

    This class encapsulates all operations related to safely removing a VM,
    including shutting it down gracefully before un-defining it.
    """
    def __init__(self,
                 name: str,
                 provider_config: Optional[Dict[str, Any]] = None):
        """
        Initialize the VM destruction operation.

        Args:
            name: Name of the VM to destroy
            provider_config: Configuration for the libvirt provider
        """
        super().__init__(provider_config)

        #: str: Name of the VM to destroy
        self.name = name

        #: int: Maximum seconds to wait for VM to shutdown
        self.shutdown_timeout = 30

    def is_vm_running(self) -> bool:
        """
        Check if the VM is running.

        Returns:
            True if VM is running, False otherwise
        """
        try:
            result = self.execute("domstate", self.name, warn=True)
            return result.ok and "running" in result.stdout
        except RuntimeError:
            return False

    def is_vm_defined(self) -> bool:
        """
        Check if the VM exists (is defined).

        Returns:
            True if VM exists, False otherwise
        """
        try:
            result = self.execute("dominfo", self.name, warn=True)
            return result.ok
        except RuntimeError:
            return False

    def shutdown_vm(self,
                    timeout: Optional[int] = None,
                    force: bool = False) -> bool:
        """
        Shutdown a VM gracefully or by force if requested.

        This method attempts to shutdown the VM gracefully first. If the force parameter
        is True and graceful shutdown times out, it will forcibly power off the VM.

        Args:
            timeout: Maximum seconds to wait for graceful shutdown
            force: Whether to force power off if graceful shutdown fails

        Returns:
            True if VM is successfully shut down, False otherwise
        """
        if not self.is_vm_running():
            self.logger.info(f"VM {self.name} is not running, no need to shut down")
            return True

        if timeout is None:
            timeout = self.shutdown_timeout

        try:
            # Try graceful shutdown first
            self.logger.info(f"Shutting down VM {self.name} gracefully")
            self.execute("shutdown", self.name)

            # Wait for VM to shut down
            for i in range(timeout):
                if not self.is_vm_running():
                    self.logger.info(f"VM {self.name} shut down successfully after {i+1} seconds")
                    return True
                time.sleep(1)

            if not force:
                self.logger.warning(f"VM {self.name} did not shut down within {timeout} seconds and force is disabled")
                return False

            # Force shutdown if requested
            self.logger.warning(f"VM {self.name} did not shut down within {timeout} seconds, forcing shutdown")
            return self.force_shutdown_vm()

        except RuntimeError as e:
            self.logger.error(f"Error shutting down VM {self.name}: {e}")
            return False

    def force_shutdown_vm(self) -> bool:
        """
        Force power off a VM.

        This method is equivalent to pulling the power plug on a physical machine.
        It should be used as a last resort when graceful shutdown fails.

        Returns:
            True if VM is successfully powered off, False otherwise
        """
        if not self.is_vm_running():
            self.logger.info(f"VM {self.name} is not running, no need to force shutdown")
            return True

        try:
            self.logger.info(f"Force shutting down VM {self.name}")
            self.execute("destroy", self.name)

            # Verify VM is no longer running
            if not self.is_vm_running():
                self.logger.info(f"VM {self.name} force shutdown successfully")
                return True

            self.logger.error(f"VM {self.name} is still running after force shutdown")
            return False
        except RuntimeError as e:
            self.logger.error(f"Error force shutting down VM {self.name}: {e}")
            return False

    def destroy_vm(self,
                   force: Optional[bool] = None,
                   timeout: Optional[int] = None) -> bool:
        """
        Stop a running VM either gracefully or by force.

        This is a convenience method that combines shutdown_vm and force_shutdown_vm.
        For more control, use those methods directly.

        Args:
            force: Whether to force destroy the VM. If None, will try graceful shutdown first,
                  then force destroy if timeout is reached
            timeout: Maximum seconds to wait for shutdown, defaults to self.shutdown_timeout

        Returns:
            True if VM is stopped successfully, False otherwise
        """
        if force is True:
            return self.force_shutdown_vm()

        return self.shutdown_vm(timeout=timeout, force=force is not False)

    def undefine_vm(self) -> bool:
        """
        Undefine (remove) the VM.

        Args:
            remove_storage: Whether to remove associated storage

        Returns:
            True if undefine successful, False otherwise
        """
        if not self.is_vm_defined():
            self.logger.info(f"VM {self.name} is not defined, nothing to undefine")
            return True

        try:
            self.logger.info(f"Un-defining VM {self.name}")

            self.execute("undefine", self.name)

            # Verify VM is no longer defined
            if not self.is_vm_defined():
                self.logger.info(f"VM {self.name} undefined successfully")
                return True

            self.logger.error(f"VM {self.name} is still defined after undefine")
            return False
        except RuntimeError as e:
            self.logger.error(f"Error un-defining VM {self.name}: {e}")
            return False

    def delete_all_snapshots(self) -> bool:
        """
        Delete all snapshots for the VM.

        This should be called before undefining the VM to ensure
        all associated snapshot data is cleaned up.

        Returns:
            True if all snapshots were deleted successfully or if there are no snapshots,
            False if there was an error deleting any snapshot
        """
        if not self.is_vm_defined():
            self.logger.info(f"VM {self.name} is not defined, no snapshots to delete")
            return True

        try:
            # List all snapshots
            self.logger.info(f"Checking for snapshots of VM {self.name}")
            result = self.execute("snapshot-list", self.name, "--name", warn=True)

            if not result.ok:
                self.logger.warning(f"Failed to list snapshots for VM {self.name}: {result.stderr}")
                return False

            if not result.stdout.strip():
                self.logger.info(f"No snapshots found for VM {self.name}")
                return True

            # Process the list of snapshots
            snapshots = [s for s in result.stdout.strip().split('\n') if s.strip()]

            if not snapshots:
                self.logger.info(f"No snapshots found for VM {self.name}")
                return True

            self.logger.info(f"Found {len(snapshots)} snapshots to delete for VM {self.name}")

            # Delete each snapshot
            success = True
            for snapshot in snapshots:
                try:
                    self.logger.info(f"Deleting snapshot '{snapshot}' for VM {self.name}")
                    delete_result = self.execute("snapshot-delete", self.name, snapshot)

                    if not delete_result.ok:
                        self.logger.error(f"Failed to delete snapshot '{snapshot}' for VM {self.name}: {delete_result.stderr}")
                        success = False
                except RuntimeError as e:
                    self.logger.error(f"Error deleting snapshot '{snapshot}' for VM {self.name}: {e}")
                    success = False

            return success
        except RuntimeError as e:
            self.logger.error(f"Error handling snapshots for VM {self.name}: {e}")
            return False

    def remove(self, force: Optional[bool] = None) -> bool:
        """
        Completely destroy the VM: shutdown, force destroy if needed, and undefine.

        Args:
            force: Whether to force destroy the VM if it's running

        Returns:
            True if all operations were successful, False otherwise
        """
        if self.is_vm_running():
            if not self.destroy_vm(force=force):
                self.logger.error(f"Failed to stop VM {self.name}, cannot proceed with undefine")
                return False

        # Delete all snapshots before undefining the VM
        if not self.delete_all_snapshots():
            self.logger.warning(f"Failed to delete all snapshots for VM {self.name}, continuing with undefine anyway")
            # Continuing with undefine even if snapshot deletion failed
            # This is a deliberate choice to ensure VM removal attempts completion

        status = self.undefine_vm()

        return status
