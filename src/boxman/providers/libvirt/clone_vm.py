import os
from typing import Any

import invoke

from boxman import log
from boxman.exceptions import (
    CloneCleanupError,
    CloneSanitizerError,
    CloneSanitizerUnavailable,
    ConfigError,
)

from .commands import VirshCommand, VirtCloneCommand, VirtSysprepCommand
from .virsh_parse import parse_domiflist


class CloneVM:
    """
    Class to clone VMs in libvirt using virt-clone and virsh commands.
    """

    MACHINE_ID_POLICIES = frozenset({'auto', 'required', 'off'})
    DEFAULT_SYSPREP_TIMEOUT = 300
    SYSPREP_RUNNER_GRACE = VirtSysprepCommand.TIMEOUT_KILL_GRACE + 5

    def __init__(self,
                src_vm_name: str,
                new_vm_name: str,
                info: dict[str, Any],
                workdir: str | None = None,
                provider_config: dict[str, Any] | None = None):
        """
        Initialize the VM cloning operation.

        Args:
            src_vm_name: Name of the source VM
            new_vm_name: Name of the new VM
            info: Dictionary containing VM configuration
            provider_config: Configuration for the libvirt provider
        """
        provider_config = provider_config or {}

        #: str: the name of the source vm
        self.src_vm_name = src_vm_name

        #: str: the name of the new vm
        self.new_vm_name = new_vm_name

        #: str: the path to the disk image
        self.new_image_path = os.path.expanduser(os.path.join(workdir, f'{new_vm_name}.qcow2'))

        #: the info of the vm
        self.info = info

        #: str: how an offline machine-ID reset failure is handled
        self.machine_id_policy = info.get('clone_machine_id', 'auto')
        if (not isinstance(self.machine_id_policy, str)
                or self.machine_id_policy not in self.MACHINE_ID_POLICIES):
            choices = ', '.join(sorted(self.MACHINE_ID_POLICIES))
            raise ConfigError(
                f"clone_machine_id for vm '{new_vm_name}' must be one of "
                f"{choices}, got {self.machine_id_policy!r}")

        #: int: bounded libguestfs inspection time; avoids a wedged appliance
        #: blocking the parent process forever while it joins clone workers.
        self.sysprep_timeout = provider_config.get(
            'virt_sysprep_timeout', self.DEFAULT_SYSPREP_TIMEOUT)
        if (isinstance(self.sysprep_timeout, bool)
                or not isinstance(self.sysprep_timeout, int)
                or self.sysprep_timeout < 1):
            raise ConfigError(
                "provider.libvirt.virt_sysprep_timeout must be a positive "
                f"integer, got {self.sysprep_timeout!r}")

        #: VirtCloneCommand: the command executor for virt-clone
        self.virt_clone = VirtCloneCommand(provider_config)

        #: VirtSysprepCommand: offline guest identity sanitizer
        self.virt_sysprep = VirtSysprepCommand(provider_config)

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

            self.logger.status(f"cloning the vm {self.src_vm_name} to {self.new_vm_name}")
            self.virt_clone.execute(*cmd_args, **cmd_kwargs)

            # virt-clone changes the libvirt UUID and NIC MAC, but copies the
            # guest filesystem verbatim. Reset standard Linux machine-ID files
            # while the clone is shut off. ``auto`` preserves compatibility
            # with opaque/encrypted/unsupported appliances; ``required`` fails
            # closed; ``off`` deliberately keeps the legacy clone behavior.
            self.apply_machine_identity_policy()

            # after cloning, remove all inherited network interfaces if the machine has network
            # interfaces defined. .. todo:: do this later on when configuring network interfaces
            if 'network_adapters' in self.info:
                if not self.remove_network_interfaces():
                    self.logger.warning(
                        f"failed to remove network interfaces from the vm {self.new_vm_name}")

            return True
        except RuntimeError as exc:
            self.logger.error(f"Error cloning the vm: {exc}")
            return False

    def apply_machine_identity_policy(self) -> None:
        """Apply the configured ``auto|required|off`` clone policy."""
        if self.machine_id_policy == 'off':
            self.logger.info(
                f"skipping machine identity reset for vm {self.new_vm_name} "
                "(clone_machine_id=off)")
            return

        try:
            self.reset_machine_identity()
        except CloneSanitizerError as sanitizer_error:
            if self.machine_id_policy == 'auto':
                self.logger.warning(
                    f"could not reset inherited machine identity for vm "
                    f"{self.new_vm_name}; continuing because "
                    f"clone_machine_id=auto. The guest may retain its "
                    f"template identity. Set clone_machine_id=required to "
                    f"fail closed. Cause: {sanitizer_error}")
                return

            # ``required`` is fail-closed. A cleanup failure is itself
            # terminal and preserves the sanitizer cause in its message and
            # exception chain, avoiding misleading clone retries.
            self.discard_unsafe_clone(sanitizer_error)
            raise

    def reset_machine_identity(self) -> None:
        """Clear inherited machine IDs in the shut-off cloned guest.

        Upstream's ``machine-id`` operation truncates regular
        ``/etc/machine-id`` and ``/var/lib/dbus/machine-id`` files. A normal
        D-Bus symlink to ``/etc/machine-id`` remains intact. Early boot then
        generates a new identity before networking starts.

        This offline operation does not require cloud-init or a guest agent,
        but libguestfs must be able to inspect and write the guest. Opaque,
        encrypted, and unsupported appliances are handled by the configured
        clone policy.
        """
        try:
            result = self.virt_sysprep.execute(
                domain=self.new_vm_name,
                operations="machine-id",
                keys_from_stdin=True,
                warn=True,
                execution_timeout=self.sysprep_timeout,
                timeout=self.sysprep_timeout + self.SYSPREP_RUNNER_GRACE,
            )
        except invoke.exceptions.CommandTimedOut as exc:
            raise CloneSanitizerError(
                f"virt-sysprep timed out after {self.sysprep_timeout}s while "
                f"resetting vm {self.new_vm_name}") from exc
        except Exception as exc:
            raise CloneSanitizerError(
                f"virt-sysprep could not inspect vm {self.new_vm_name}: "
                f"{exc}") from exc

        if result.ok:
            self.logger.info(
                f"reset inherited machine identity for vm {self.new_vm_name}")
            return

        if result.return_code == 124:
            raise CloneSanitizerError(
                f"virt-sysprep timed out after {self.sysprep_timeout}s while "
                f"resetting vm {self.new_vm_name}")

        detail = (result.stderr or result.stdout or "unknown error").strip()
        detail_lower = detail.lower()
        binary = os.path.basename(str(self.virt_sysprep.command_path)).lower()
        missing_signature = (
            "command not found" in detail_lower
            or f"{binary}: not found" in detail_lower
            or "no such file or directory" in detail_lower
        )
        configured_binary = str(self.virt_sysprep.command_path).lower()
        sudo_missing = (
            f"sudo: {configured_binary}: command not found" in detail_lower
            or f"sudo: {binary}: command not found" in detail_lower
        )
        missing = (
            result.return_code == 127
            and binary in detail_lower
            and missing_signature
        ) or sudo_missing
        if missing:
            message = (
                "virt-sysprep is not installed in the "
                "active runtime. Install guestfs-tools (Arch/Debian/Ubuntu/"
                "RHEL), app-emulation/guestfs-tools (Gentoo), "
                "nixpkgs#guestfs-tools (NixOS), or libguestfs (Guix), then "
                "retry."
            )
            raise CloneSanitizerUnavailable(message)
        sudo_denied = (
            "sudo:" in detail_lower
            and (
                "password" in detail_lower
                or "terminal is required" in detail_lower
                or "askpass" in detail_lower
            )
        )
        if sudo_denied:
            message = (
                "virt-sysprep cannot run "
                "non-interactively with the configured sudo policy. Grant "
                "passwordless sudo for virt-sysprep, or set "
                "provider.libvirt.use_sudo to false when the active user "
                "already has libvirt access, then retry."
            )
            raise CloneSanitizerUnavailable(message)

        raise CloneSanitizerError(
            "virt-sysprep machine-id reset failed for vm "
            f"{self.new_vm_name}: {detail}")

    def discard_unsafe_clone(
            self, sanitizer_error: CloneSanitizerError | None = None) -> None:
        """Remove the newly-created clone after identity sanitization fails.

        If libvirt cannot remove it, raise a terminal error containing both
        failure causes. A plain-undefine fallback is intentionally avoided:
        it would leave the known clone disk behind, while deleting individual
        domain disks risks shared or inherited media.
        """
        try:
            result = self.virsh.execute(
                "undefine",
                self.new_vm_name,
                "--remove-all-storage",
                warn=True,
            )
        except Exception as cleanup_error:
            sanitizer_detail = (
                str(sanitizer_error) if sanitizer_error is not None
                else 'machine identity reset failed')
            message = (
                f"{sanitizer_detail}; additionally failed to discard unsafe "
                f"clone {self.new_vm_name}: {cleanup_error}. The clone "
                "remains shut off and requires manual cleanup.")
            raise CloneCleanupError(
                message,
                sanitizer_error=sanitizer_error,
                cleanup_error=cleanup_error,
            ) from cleanup_error

        if not result.ok:
            cleanup_detail = (
                result.stderr or result.stdout or 'unknown error').strip()
            sanitizer_detail = (
                str(sanitizer_error) if sanitizer_error is not None
                else 'machine identity reset failed')
            message = (
                f"{sanitizer_detail}; additionally failed to discard unsafe "
                f"clone {self.new_vm_name}: {cleanup_detail}. The clone "
                "remains shut off and requires manual cleanup.")
            error = CloneCleanupError(
                message,
                sanitizer_error=sanitizer_error,
                cleanup_error=cleanup_detail,
            )
            if sanitizer_error is not None:
                raise error from sanitizer_error
            raise error

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
            result = self.virsh.execute("domiflist", vm_name, warn=True)
            if not result.ok:
                self.logger.error(f"Failed to get interface list for VM {vm_name}")
                return False

            # parse the output to extract interface information
            interfaces = [
                (row.type, row.source, row.mac)
                for row in parse_domiflist(result.stdout)
            ]

            self.logger.info(
                f"found {len(interfaces)} network interfaces to remove from the vm {vm_name}")

            # Remove each interface
            for iface_type, _source, mac in interfaces:
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
