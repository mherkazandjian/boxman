import os
import shlex
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from typing import Any

import invoke

from boxman import log
from boxman.exceptions import (
    CloneCleanupError,
    CloneSanitizerError,
    CloneSanitizerUnavailableError,
    ConfigError,
    ProvisionError,
)
from boxman.utils.shell import run as _shell_run

from .commands import VirshCommand, VirtCloneCommand, VirtSysprepCommand
from .virsh_parse import parse_domiflist

# Internal hand-off used by the retry wrapper. Clone attempts run under log
# suppression so transient errors stay quiet; successful ``auto`` degradation
# notices are collected here and re-emitted after that suppression ends.
CLONE_DEGRADATION_NOTICES_KEY = '_boxman_clone_degradation_notices'

#: Every clone-identity property takes the same three policies with the same
#: meaning: ``auto`` degrades to a notice and continues, ``required`` fails
#: closed and discards the clone, ``off`` leaves the property alone.
IDENTITY_POLICIES = frozenset({'auto', 'required', 'off'})

#: Host key types generated for a clone, matching what ``ssh-keygen -A``
#: produces on current OpenSSH. ``dsa`` is deliberately absent: upstream
#: removed it, and a guest that still wanted one would not accept it anyway.
SSH_HOST_KEY_TYPES = ('rsa', 'ecdsa', 'ed25519')

#: Name of the staged archive that carries the clone's host keys into
#: ``/etc/ssh`` with root ownership.
SSH_HOST_KEY_ARCHIVE = 'ssh-host-keys.tar'


@dataclass(frozen=True)
class IdentityProperty:
    """One guest identity property handled by the offline pass."""

    #: str: how the property reads in a message to the user
    name: str

    #: str: the per-vm config key carrying its policy
    config_key: str

    #: str: the resolved policy. Never ``off``: a disabled property is not
    #: part of a plan at all.
    policy: str


@dataclass
class IdentityPlan:
    """A single ``virt-sysprep`` pass assembled from the enabled properties.

    Boxman runs one pass rather than one per property because every
    invocation boots a libguestfs appliance and cloning is already the slow
    step. virt-sysprep applies its ``customize`` operation *last*, after
    every other operation, so a property that deletes files
    (``ssh-hostkeys``) and one that writes them (an ``--upload``) compose
    safely in the same pass.
    """

    #: list[IdentityProperty]: the properties this pass is responsible for
    properties: list[IdentityProperty] = field(default_factory=list)

    #: list[str]: ``--operations`` entries contributed by those properties
    operations: list[str] = field(default_factory=list)

    #: list[str]: customization arguments, as raw argv fragments
    customizations: list[str] = field(default_factory=list)

    #: list[str]: host-side temp directories to remove once the pass is done
    staging_dirs: list[str] = field(default_factory=list)

    #: bool: whether the pass carries customization flags. Known before they
    #: are staged, because it decides whether ``customize`` must be enabled.
    needs_customize: bool = False

    #: bool: whether fresh ssh host keys must be generated before the pass
    fresh_ssh_host_keys: bool = False

    @property
    def strictest_policy(self) -> str:
        """The policy a failure of this pass is judged against.

        One pass covers several properties whose policies are configured
        independently, so a failure resolves against the strictest of them:
        a single ``required`` property makes the whole pass fail closed.
        """
        return ('required'
                if any(prop.policy == 'required' for prop in self.properties)
                else 'auto')

    def describe(self) -> str:
        """The affected properties, as a readable list."""
        names = [prop.name for prop in self.properties]
        if len(names) < 2:
            return names[0] if names else 'nothing'
        return ', '.join(names[:-1]) + f" and {names[-1]}"


class CloneVM:
    """
    Class to clone VMs in libvirt using virt-clone and virsh commands.
    """

    #: Retained under its original name; every clone-identity property
    #: shares the same set of policy values.
    MACHINE_ID_POLICIES = IDENTITY_POLICIES
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

        #: str: how a failure to reset the inherited machine ID is handled
        self.machine_id_policy = self._resolve_policy('clone_machine_id')

        #: str: how a failure to replace the inherited ssh host keys is
        #: handled
        self.ssh_host_keys_policy = self._resolve_policy(
            'clone_ssh_host_keys')

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

            # virt-clone changes the libvirt UUID and NIC MAC, but copies
            # the guest filesystem verbatim -- machine ID and ssh host keys
            # included. Reset them offline while the clone is shut off.
            # ``auto`` preserves compatibility with opaque/encrypted/
            # unsupported appliances; ``required`` fails closed; ``off``
            # deliberately keeps the legacy clone behavior.
            self.apply_identity_policies()

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

    def _resolve_policy(self, config_key: str) -> str:
        """Read and validate one ``auto|required|off`` policy key."""
        policy = self.info.get(config_key, 'auto')
        if not isinstance(policy, str) or policy not in IDENTITY_POLICIES:
            choices = ', '.join(sorted(IDENTITY_POLICIES))
            raise ConfigError(
                f"{config_key} for vm '{self.new_vm_name}' must be one of "
                f"{choices}, got {policy!r}")
        return policy

    def build_identity_plan(self) -> IdentityPlan:
        """Assemble the offline pass from the enabled identity policies.

        Pure: it decides *what* the pass must do without touching the host or
        the guest. Anything that has to be produced first -- fresh ssh host
        keys -- is only flagged here and materialized by
        :meth:`stage_identity_plan`.
        """
        plan = IdentityPlan()

        if self.machine_id_policy != 'off':
            plan.properties.append(IdentityProperty(
                'machine id', 'clone_machine_id', self.machine_id_policy))
            plan.operations.append('machine-id')

        if self.ssh_host_keys_policy != 'off':
            plan.properties.append(IdentityProperty(
                'ssh host keys', 'clone_ssh_host_keys',
                self.ssh_host_keys_policy))
            # virt-clone copies /etc/ssh/ssh_host_*_key verbatim, so without
            # this every clone of one template presents the template's host
            # keys: clients cannot tell two clones apart, and root on any one
            # clone holds the private host keys of all of them.
            plan.operations.append('ssh-hostkeys')
            plan.fresh_ssh_host_keys = True
            plan.needs_customize = True

        if plan.needs_customize and self.machine_id_policy == 'off':
            # virt-sysprep's ``customize`` operation always writes a fresh
            # /etc/machine-id -- with no customization flags at all, and with
            # no way to switch it off. Enabling any customizing property
            # therefore overrides clone_machine_id=off. Warn rather than
            # raise: raising would break every existing config that sets
            # ``off`` the moment another property defaults to ``auto``.
            self.logger.warning(
                f"vm {self.new_vm_name}: clone_machine_id=off cannot be "
                f"honoured while another clone identity property is enabled. "
                f"The offline pass they need runs virt-sysprep's 'customize' "
                f"operation, which always writes a fresh /etc/machine-id. "
                f"The clone gets a new machine id instead of the template's. "
                f"Set every clone_* identity policy to off to skip the pass "
                f"entirely.")

        return plan

    def stage_identity_plan(self, plan: IdentityPlan) -> None:
        """Produce the host-side inputs the pass uploads into the guest."""
        if plan.fresh_ssh_host_keys:
            customizations, staging_dir = self.generate_ssh_host_keys()
            plan.customizations.extend(customizations)
            plan.staging_dirs.append(staging_dir)

    def generate_ssh_host_keys(self) -> tuple[list[str], str]:
        """Generate a fresh set of ssh host keys for the clone, on the host.

        The keys are generated here and uploaded as plain files rather than
        produced inside the guest with ``--run-command 'ssh-keygen -A'``:
        virt-sysprep refuses to run a command in a guest whose architecture
        does not match the host's, while uploading a file does not care about
        the guest's architecture, whether its cloud-init is sealed, or
        whether it ships ``ssh-keygen`` at all. Deleting the inherited keys
        without replacing them is not portable either -- EL recreates missing
        keys at boot through ``sshd-keygen@``, but a Debian/Ubuntu guest
        without cloud-init would come up with no host keys and a failing sshd.

        They are delivered as a tar archive rather than with ``--upload``
        plus ``--chown``. libguestfs preserves the *source* file's ownership
        on upload, so the keys would otherwise land owned by whatever uid
        boxman runs as on the hypervisor, and ``--chown`` cannot be relied on
        to correct that: guestfs-tools 1.52.0 documents ``--chown
        UID:GID:PATH`` but its parser rejects every form of the argument
        ("invalid format for '--chown' parameter"), while 1.52.2 accepts it.
        A tar entry carries its own uid, gid and mode regardless of the file
        on disk, which works on both -- and keeps the private keys off the
        command line.

        The staging directory is an ordinary host temp directory: under the
        docker-compose runtime ``tempfile.gettempdir()`` is bind-mounted into
        the container at the same absolute path, the same mechanism the XML
        written for ``virsh define`` already relies on.

        Returns:
            The customization arguments that install the keys, and the
            staging directory the caller is responsible for removing.
        """
        staging_dir = tempfile.mkdtemp(
            prefix=f'boxman-hostkeys-{self.new_vm_name}-')

        try:
            for key_type in SSH_HOST_KEY_TYPES:
                private = os.path.join(
                    staging_dir, f'ssh_host_{key_type}_key')
                # -C '' keeps the hypervisor's user@host out of the guest's
                # public keys. -N '' is not a shortcut: an sshd host key
                # cannot be passphrase-protected.
                result = _shell_run(
                    f"ssh-keygen -q -t {key_type} -N '' -C '' "
                    f"-f {shlex.quote(private)}",
                    hide=True, warn=True)
                if not result.ok or not os.path.isfile(private):
                    detail = (
                        result.stderr or result.stdout or 'unknown error'
                    ).strip()
                    raise CloneSanitizerError(
                        f"could not generate a fresh {key_type} ssh host key "
                        f"for vm {self.new_vm_name}: {detail}")

            archive = os.path.join(staging_dir, SSH_HOST_KEY_ARCHIVE)
            with tarfile.open(archive, 'w') as tar:
                for name, mode in self.ssh_host_key_files():
                    source = os.path.join(staging_dir, name)
                    entry = tar.gettarinfo(source, arcname=name)
                    # the whole point of the archive: sshd must find its
                    # host keys owned by root, whoever generated them
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = 'root'
                    entry.mode = mode
                    with open(source, 'rb') as handle:
                        tar.addfile(entry, handle)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

        return ['--tar-in', f'{archive}:/etc/ssh'], staging_dir

    @staticmethod
    def ssh_host_key_files() -> list[tuple[str, int]]:
        """Each host key file the clone gets, with the mode sshd expects."""
        files = []
        for key_type in SSH_HOST_KEY_TYPES:
            files.append((f'ssh_host_{key_type}_key', 0o600))
            files.append((f'ssh_host_{key_type}_key.pub', 0o644))
        return files

    @staticmethod
    def assert_customize_invariant(operations: list[str],
                                   customizations: list[str]) -> None:
        """Guard the one virt-sysprep failure mode that is entirely silent.

        ``--operations`` *replaces* virt-sysprep's default operation set, and
        every customization flag (``--upload``, ``--chmod``, ``--hostname``,
        ``--write``, ...) is applied by the ``customize`` operation. Passing a
        customization while ``customize`` is absent from the operation list
        makes virt-sysprep exit 0 having done nothing at all: no warning, no
        diagnostic, and a clone that looks sanitized but is not. Anything that
        adds a customization must add the operation along with it.
        """
        if customizations and 'customize' not in operations:
            raise ProvisionError(
                "internal error: the offline identity pass requested the "
                f"customizations {customizations!r} without the 'customize' "
                "operation; virt-sysprep would silently ignore them")

    def build_sysprep_invocation(
            self, plan: IdentityPlan) -> tuple[list[str], dict[str, Any]]:
        """Build the single ``virt-sysprep`` call that executes *plan*.

        ``--no-selinux-relabel`` is deliberately never passed: relabelling is
        automatic in current guestfs-tools, and suppressing it would leave an
        uploaded host key mislabelled on an SELinux guest, where sshd would
        then refuse to read it.
        """
        operations = list(plan.operations)
        if (plan.needs_customize or plan.customizations) \
                and 'customize' not in operations:
            operations.append('customize')

        self.assert_customize_invariant(operations, plan.customizations)

        return list(plan.customizations), {
            'domain': self.new_vm_name,
            'operations': ','.join(operations),
            'keys_from_stdin': True,
            'warn': True,
            'execution_timeout': self.sysprep_timeout,
            'timeout': self.sysprep_timeout + self.SYSPREP_RUNNER_GRACE,
        }

    def apply_identity_policies(self) -> None:
        """Run the offline identity pass under the configured policies."""
        plan = self.build_identity_plan()

        if not plan.properties:
            self.logger.info(
                f"skipping the offline identity pass for vm "
                f"{self.new_vm_name} (every clone identity policy is off)")
            return

        try:
            self.run_identity_pass(plan)
        except CloneSanitizerError as sanitizer_error:
            if plan.strictest_policy == 'auto':
                message = self.degradation_message(plan, sanitizer_error)
                notices = self.info.get(CLONE_DEGRADATION_NOTICES_KEY)
                if isinstance(notices, list):
                    notices.append(message)
                else:
                    # Direct provider callers do not have a retry wrapper to
                    # re-emit the notice, so retain the normal warning path.
                    self.logger.warning(message)
                return

            # ``required`` is fail-closed. A cleanup failure is itself
            # terminal and preserves the sanitizer cause in its message and
            # exception chain, avoiding misleading clone retries.
            self.discard_unsafe_clone(sanitizer_error)
            raise

    def degradation_message(self, plan: IdentityPlan,
                            sanitizer_error: CloneSanitizerError) -> str:
        """Describe an ``auto`` degradation, naming every affected property.

        The pass is not atomic -- virt-sysprep can apply one customization
        and then fail on the next -- so this says the guest *may* have kept
        its template identity rather than claiming that it did.
        """
        policies = ', '.join(
            f"{prop.config_key}={prop.policy}" for prop in plan.properties)
        return (
            f"could not complete the offline identity pass for vm "
            f"{self.new_vm_name}; continuing because {policies}. The guest "
            f"may have kept its template's {plan.describe()}. Set the policy "
            f"to required to fail closed. Cause: {sanitizer_error}")

    def run_identity_pass(self, plan: IdentityPlan) -> None:
        """Execute one offline ``virt-sysprep`` pass for *plan*.

        Upstream's ``machine-id`` operation truncates regular
        ``/etc/machine-id`` and ``/var/lib/dbus/machine-id`` files; the
        ``customize`` operation, once enabled, writes a fresh random value
        into ``/etc/machine-id`` instead, which is the stronger guarantee --
        no dependence on the guest regenerating one at boot. ``ssh-hostkeys``
        removes the inherited host keys and the staged uploads replace them.

        This offline pass needs neither cloud-init nor a guest agent, but
        libguestfs must be able to inspect and write the guest. Opaque,
        encrypted and unsupported appliances are handled by the configured
        clone policy.
        """
        self.stage_identity_plan(plan)
        try:
            # Built outside the sanitizer try/except below on purpose: an
            # invariant violation is a boxman bug, not a guest that cannot be
            # inspected, and must not be degraded into an ``auto`` notice.
            args, kwargs = self.build_sysprep_invocation(plan)

            try:
                result = self.virt_sysprep.execute(*args, **kwargs)
            except invoke.exceptions.CommandTimedOut as exc:
                raise CloneSanitizerError(
                    f"virt-sysprep timed out after {self.sysprep_timeout}s "
                    f"while resetting vm {self.new_vm_name}") from exc
            except Exception as exc:
                raise CloneSanitizerError(
                    f"virt-sysprep could not inspect vm "
                    f"{self.new_vm_name}: {exc}") from exc

            if result.ok:
                self.logger.info(
                    f"reset inherited {plan.describe()} for vm "
                    f"{self.new_vm_name}")
                return

            self.raise_for_sysprep_failure(result)
        finally:
            for staging_dir in plan.staging_dirs:
                shutil.rmtree(staging_dir, ignore_errors=True)

    def raise_for_sysprep_failure(self, result: Any) -> None:
        """Turn a failed ``virt-sysprep`` result into a typed error."""
        if result.return_code in (124, 137):
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
            raise CloneSanitizerUnavailableError(message)
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
            raise CloneSanitizerUnavailableError(message)

        raise CloneSanitizerError(
            "virt-sysprep identity pass failed for vm "
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
