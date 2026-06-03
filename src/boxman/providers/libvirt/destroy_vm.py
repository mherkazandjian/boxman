import time
from typing import Any

from .commands import VirshCommand


class DestroyVM(VirshCommand):
    """
    Class to destroy (remove) VMs in libvirt using virsh commands.

    This class encapsulates all operations related to safely removing a VM,
    including shutting it down gracefully before un-defining it.
    """
    def __init__(self,
                 name: str,
                 provider_config: dict[str, Any] | None = None):
        """
        Initialize the VM destruction operation.

        Args:
            name: Name of the VM to destroy
            provider_config: Configuration for the libvirt provider
        """
        super().__init__(provider_config=provider_config)

        #: str: the name of the VM to destroy
        self.name = name

        #: int: the maximum seconds to wait for the vms to shutdown
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

    def is_vm_shut_off(self) -> bool:
        """
        Check if the VM is fully stopped ("shut off").

        This is stricter than ``not is_vm_running()``: a VM in the "in shutdown"
        state is no longer "running" but has not yet reached "shut off" — the
        QEMU process is still alive and storage cannot be removed yet.

        Returns:
            True when the domain is "shut off" or no longer exists.
        """
        try:
            result = self.execute("domstate", self.name, warn=True)
            if not result.ok:
                return True   # domain gone → effectively stopped
            return "shut off" in result.stdout
        except RuntimeError:
            return True  # treat unreachable as stopped

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
                    timeout: int | None = None,
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
            # try graceful shutdown first
            self.logger.info(f"shutting down vm {self.name} gracefully")
            self.execute("shutdown", self.name)

            # wait for vm to reach "shut off" — not just "not running", because
            # a VM in the "in shutdown" state is no longer "running" but the
            # QEMU process is still alive (and storage cannot be removed yet).
            for i in range(timeout):
                if self.is_vm_shut_off():
                    self.logger.info(f"vm {self.name} shut down successfully after {i+1} seconds")
                    return True
                time.sleep(1)

            if not force:
                self.logger.warning(
                    f"vm {self.name} did not shut down within {timeout} "
                    f"seconds and force is disabled")
                return False

            # force shutdown if requested
            self.logger.warning(
                f"the vm {self.name} did not shut down within {timeout} "
                f"seconds, forcing shutdown")
            return self.force_shutdown_vm()

        except RuntimeError as exc:
            self.logger.error(f"error shutting down the vm {self.name}: {exc}")
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
            self.logger.info(f"vm {self.name} is not running, no need to force shutdown")
            return True

        try:
            self.logger.info(f"force shutting down the vm {self.name}")
            self.execute("destroy", self.name)

            # verify that the vm is no longer running
            if not self.is_vm_running():
                self.logger.info(f"vm {self.name} force shutdown successfully")
                return True

            self.logger.error(f"vm {self.name} is still running after force shutdown")
            return False
        except RuntimeError as exc:
            self.logger.error(f"error force shutting down vm {self.name}: {exc}")
            return False

    def destroy_vm(self,
                   force: bool | None = None,
                   timeout: int | None = None) -> bool:
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
            self.logger.info(f"vm {self.name} is not defined, nothing to undefine")
            return True

        try:
            self.logger.info(f"un-defining vm {self.name}")

            self.execute("undefine", self.name)

            # verify that the vm is no longer defined
            if not self.is_vm_defined():
                self.logger.info(f"vm {self.name} undefined successfully")
                return True

            self.logger.error(f"vm {self.name} is still defined after undefine")
            return False
        except RuntimeError as exc:
            self.logger.error(f"error un-defining vm {self.name}: {exc}")
            return False

    def force_undefine_vm(self) -> bool:
        """
        Force undefine (remove) the vm, ensuring it is stopped first.

        Returns:
            True if undefine successful, False otherwise
        """
        if not self.is_vm_defined():
            self.logger.info(f"vm {self.name} is not defined, nothing to undefine")
            return True

        # --remove-all-storage requires the domain to be fully stopped.
        # If the domain is still alive (running, in-shutdown, paused, etc.)
        # force-kill it before attempting storage removal.
        if not self.is_vm_shut_off():
            self.logger.warning(
                f"vm {self.name} is not shut off, force-killing before undefine")
            self.execute("destroy", self.name, warn=True)

        try:
            self.logger.info(f"**force** un-defining vm {self.name}")

            self.execute("undefine --remove-all-storage --wipe-storage --delete-storage-volume-snapshots --snapshots-metadata", self.name)

            # verify that the vm is no longer defined
            if not self.is_vm_defined():
                self.logger.info(f"vm {self.name} undefined successfully")
                return True

            self.logger.error(f"vm {self.name} is still defined after undefine")
            return False
        except RuntimeError as exc:
            self.logger.error(f"error un-defining vm {self.name}: {exc}")
            return False

    def remove(self, force: bool | None = None) -> bool:
        """
        Completely remove the VM: stop it, then undefine it together with its
        storage and snapshot metadata.

        Snapshot metadata is cleared in a single atomic
        ``undefine --snapshots-metadata`` inside :meth:`force_undefine_vm`.
        We deliberately do NOT iterate ``virsh snapshot-delete`` per
        snapshot: for boxman's external snapshots (which carry saved memory
        state) that does not actually delete them, and on a still-running or
        *paused* domain it pile-ups "cannot acquire state change lock"
        timeouts that can wedge the domain until libvirtd is restarted.

        Args:
            force: If True, skip the graceful shutdown and force-kill the VM
                before undefining. If False/None, attempt a graceful shutdown
                first; anything still alive — including a *paused* domain,
                which ``is_vm_running`` does not report — is force-killed by
                :meth:`force_undefine_vm` regardless.

        Returns:
            True if the VM was removed, False otherwise.
        """
        # Best-effort graceful shutdown on the non-force path. A timeout here
        # is not fatal: force_undefine_vm() force-kills whatever is still
        # alive (running, paused, in-shutdown) before undefining.
        if force is not True and self.is_vm_running():
            self.shutdown_vm(timeout=self.shutdown_timeout, force=False)

        return self.force_undefine_vm()
