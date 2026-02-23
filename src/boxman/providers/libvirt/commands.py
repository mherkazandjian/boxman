from typing import Dict, Any, Optional
import invoke
from boxman import log

class LibVirtCommandBase:
    """
    Base class for executing libvirt-related commands.

    This class provides a foundation for all libvirt commands like virsh,
    virt-install, virt-clone, etc. It handles configuration, command execution,
    and error management.
    """
    def __init__(self,
                 override_config_use_sudo: Optional[bool] = None,
                 provider_config: Optional[Dict[str, Any]] = None):
        """
        Initialize a Command with provider configuration.

        Args:
            override_config_use_sudo: Whether to override the use_sudo setting with a custom value
            provider_config: Dictionary containing provider-specific configuration
                            such as credentials, paths, etc.
        """
        #: Dict[str, Any]: Configuration for the libvirt provider
        self.provider_config = provider_config or {}

        #: bool: Whether to use sudo for commands
        self.use_sudo = self.provider_config.get('use_sudo', False)

        #: bool: Whether to show command output
        self.verbose = self.provider_config.get('verbose', False)

        #: logging.Logger: Logger instance
        self.logger = log

        #: str: Command executable path
        self.command_path = None

        #: str: The runtime environment ('local', 'docker-compose', etc.)
        self.runtime = self.provider_config.get('runtime', 'local')

        #: str: The docker-compose container name for remote execution
        self.runtime_container = self.provider_config.get(
            'runtime_container', 'boxman-libvirt-default')

        # override use_sudo if provided
        if override_config_use_sudo is not None:
            self.use_sudo = override_config_use_sudo

    def build_command(self, *args, **kwargs) -> str:
        """
        Build a complete command string with options.

        Args:
            *args: Positional arguments for the command
            **kwargs: Keyword arguments that will be converted to command options

        Returns:
            Complete command string ready for execution
        """
        # Start with the prefix (sudo if needed)
        command_parts = []
        if self.use_sudo:
            command_parts.append("sudo")

        # Add the tool's path
        if self.command_path:
            command_parts.append(self.command_path)

        # Add positional arguments
        command_parts.extend([str(arg) for arg in args])

        # Add keyword arguments as options
        for key, value in kwargs.items():
            if value is True:
                command_parts.append(f"--{key.replace('_', '-')}")
            elif value is False or value is None:
                # Skip False or None values
                continue
            else:
                command_parts.append(f"--{key.replace('_', '-')}={value}")

        return " ".join(command_parts)

    def execute(self, *args,
                hide: bool = True,
                warn: bool = False,
                capture: bool = True,
                **kwargs) -> invoke.runners.Result:
        """
        Execute a command.

        Args:
            *args: Positional arguments for the command
            hide: Whether to hide command output
            warn: Whether to warn instead of raising exceptions
            capture: Whether to capture command output
            **kwargs: Keyword arguments to be passed as command options

        Returns:
            Result of the command execution

        Raises:
            RuntimeError: If the command fails and warn is False
        """
        command = self.build_command(*args, **kwargs)
        command = self._wrap_for_runtime(command)

        if self.verbose:
            self.logger.info(f"executing: {command}")

        try:
            result = invoke.run(command, hide=hide, warn=warn)

            if not result.ok and not warn:
                error_message = (
                    f"Command failed: {command}\n"
                    f"Exit code: {result.return_code}\n"
                    f"Stdout: {result.stdout}\n"
                    f"Stderr: {result.stderr}"
                )
                self.logger.error(error_message)
                raise RuntimeError(error_message)

            return result
        except invoke.exceptions.UnexpectedExit as exc:
            if not warn:
                error_message = (
                    f"Error executing command: {exc}\n"
                    f"Command: {command}\n"
                    f"Exit code: {exc.result.return_code}\n"
                    f"Stdout: {exc.result.stdout}\n"
                    f"Stderr: {exc.result.stderr}"
                )
                self.logger.error(error_message)
                raise RuntimeError(error_message)
            return exc.result

    def execute_shell(self, command: str, hide: bool = True, warn: bool = False) -> invoke.runners.Result:
        """
        Execute a raw shell command.

        This is useful for running commands that aren't directly related to the primary command tool,
        such as iptables, sysctl, etc.

        Args:
            command: The full shell command to execute
            hide: Whether to hide command output
            warn: Whether to warn instead of raising exceptions

        Returns:
            Result of the command execution

        Raises:
            RuntimeError: If the command fails and warn is False
        """
        # add sudo if needed
        if self.use_sudo and not command.startswith("sudo "):
            command = f"sudo {command}"

        # wrap for runtime environment
        command = self._wrap_for_runtime(command)

        if self.verbose:
            self.logger.info(f"Executing shell command: {command}")

        try:
            result = invoke.run(command, hide=hide, warn=warn)

            if not result.ok and not warn:
                error_message = (
                    f"Shell command failed: {command}\n"
                    f"Exit code: {result.return_code}\n"
                    f"Stdout: {result.stdout}\n"
                    f"Stderr: {result.stderr}"
                )
                self.logger.error(error_message)
                raise RuntimeError(error_message)

            return result
        except invoke.exceptions.UnexpectedExit as exc:
            if not warn:
                error_message = (
                    f"Error executing shell command: {exc}\n"
                    f"Command: {command}\n"
                    f"Exit code: {exc.result.return_code}\n"
                    f"Stdout: {exc.result.stdout}\n"
                    f"Stderr: {exc.result.stderr}"
                )
                self.logger.error(error_message)
                raise RuntimeError(error_message)
            return exc.result

    def _wrap_for_runtime(self, command: str) -> str:
        """
        Wrap a command string for execution in the configured runtime environment.

        For 'local' runtime, the command is returned unchanged.
        For 'docker-compose' runtime, the command is wrapped in a
        ``docker exec`` invocation targeting the runtime container.

        Args:
            command: The command string to wrap

        Returns:
            The (possibly wrapped) command string
        """
        if self.runtime == 'local':
            return command
        elif self.runtime == 'docker-compose':
            # escape single quotes in the command for safe shell wrapping
            escaped = command.replace("'", "'\\''")
            return f"docker exec --user root {self.runtime_container} bash -c '{escaped}'"
        else:
            raise ValueError(f"unsupported runtime: {self.runtime}")


class VirshCommand(LibVirtCommandBase):  # Fixed missing closing parenthesis:
    """
    Class for executing virsh commands for managing libvirt domains,
    networks, storage, etc.
    """
    def __init__(self, provider_config: Optional[Dict[str, Any]] = None):
        """
        Initialize a virsh command executor.

        Args:
            provider_config: Dictionary containing provider-specific configuration
        """
        super().__init__(provider_config=provider_config)

        #: str: Connection URI for libvirt
        self.uri = self.provider_config.get('uri', 'qemu:///system')

        #: str: the path to the virsh binary
        self.command_path = self.provider_config.get('virsh_cmd', 'virsh')

    def build_command(self, cmd: str, *args, **kwargs) -> str:
        """
        Build a complete virsh command string with options.

        Args:
            cmd: The virsh subcommand to execute (like list, start, define, etc.)
            *args: Positional arguments for the command
            **kwargs: Keyword arguments that will be converted to command options

        Returns:
            Complete command string ready for execution
        """
        # start with the prefix (sudo if needed)
        command_parts = []
        if self.use_sudo:
            command_parts.append("sudo")

        # add the virsh command with connection URI
        command_parts.append(f"{self.command_path} -c {self.uri}")

        # add the actual virsh subcommand
        command_parts.append(cmd)

        # add positional arguments
        command_parts.extend([str(arg) for arg in args])

        # add keyword arguments as options
        for key, value in kwargs.items():
            if value is True:
                command_parts.append(f"--{key.replace('_', '-')}")
            elif value is False or value is None:
                # skip False or None values
                continue
            else:
                command_parts.append(f"--{key.replace('_', '-')}={value}")

        return " ".join(command_parts)

    def execute(self, cmd: str, *args, **kwargs) -> invoke.runners.Result:
        """
        Execute a virsh command.

        Args:
            cmd: The virsh subcommand to execute
            *args: Positional arguments for the command
            **kwargs: Keyword arguments to be passed as command options

        Returns:
            Result of the command execution
        """
        return super().execute(cmd, *args, **kwargs)


class VirtInstallCommand(LibVirtCommandBase):
    """
    Class for executing virt-install commands for creating new libvirt
    virtual machines.
    """

    def __init__(self, provider_config: Optional[Dict[str, Any]] = None):
        """
        Initialize a virt-install command executor.

        Args:
            provider_config: Dictionary containing provider-specific configuration
        """
        super().__init__(provider_config)

        #: str: the path to virt-install binary
        self.command_path = self.provider_config.get('virt_install_cmd', 'virt-install')

        #: str: the connection URI for libvirt
        self.uri = self.provider_config.get('uri', 'qemu:///system')

    def build_command(self, *args, **kwargs) -> str:
        """
        Build a complete virt-install command string with options.

        Args:
            *args: Positional arguments for the command
            **kwargs: Keyword arguments that will be converted to command options

        Returns:
            Complete command string ready for execution
        """
        # add URI option if not already in kwargs
        if 'connect' not in kwargs:
            kwargs['connect'] = self.uri

        return super().build_command(*args, **kwargs)


class VirtCloneCommand(LibVirtCommandBase):
    """
    Class for executing virt-clone commands for cloning existing libvirt
    virtual machines.
    """

    def __init__(self, provider_config: Optional[Dict[str, Any]] = None):
        """
        Initialize a virt-clone command executor.

        Args:
            provider_config: Dictionary containing provider-specific configuration
        """
        super().__init__(provider_config=provider_config)

        #: str: the path to virt-clone binary
        self.command_path = self.provider_config.get('virt_clone_cmd', 'virt-clone')

        #: str: the connection URI for libvirt
        self.uri = self.provider_config.get('uri', 'qemu:///system')

    def build_command(self, *args, **kwargs) -> str:
        """
        Build a complete virt-clone command string with options.

        Args:
            *args: Positional arguments for the command
            **kwargs: Keyword arguments that will be converted to command options

        Returns:
            Complete command string ready for execution
        """
        # Add URI option if not already in kwargs
        if 'connect' not in kwargs:
            kwargs['connect'] = self.uri

        return super().build_command(*args, **kwargs)
