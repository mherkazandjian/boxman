"""
VBoxManage command runner for the VirtualBox provider.

This is the fixed successor to the legacy ``boxman.virtualbox.utils.Command``,
whose ``communicate()`` output-capture was commented out — so every parser that
read ``self.stdout`` got ``None``. Here the runner captures stdout/stderr
properly via ``subprocess.run(..., capture_output=True, text=True)``.

Compared to the legacy machinery this runner also:

* makes the binary configurable via ``vboxmanage_cmd`` (default ``VBoxManage``
  — most hosts ship the CamelCase name; the legacy code hardcoded lowercase
  ``vboxmanage`` which is not present on many installs),
* honours ``use_sudo`` from the provider config,
* is runtime-wrap ready: VirtualBox is a host-local hypervisor, so only the
  ``local`` runtime is supported and :meth:`_wrap_for_runtime` fails fast for
  anything else.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from typing import Any

from boxman import log


class VBoxManageCommand:
    """
    Build and run ``VBoxManage`` commands with captured output.

    The command surface is intentionally small this phase: :meth:`build_command`
    (pure, unit-testable string builder), :meth:`run` (captures output), and a
    couple of parse helpers that demonstrate the capture is actually wired up.
    """

    #: Default binary name. Most hosts ship the CamelCase ``VBoxManage``; the
    #: legacy code hardcoded lowercase ``vboxmanage`` which is not present on
    #: many installs. Overridable per provider config via ``vboxmanage_cmd``.
    DEFAULT_BINARY = 'VBoxManage'

    def __init__(self,
                 provider_config: dict[str, Any] | None = None,
                 override_use_sudo: bool | None = None):
        """
        Args:
            provider_config: The resolved ``provider.virtualbox`` config dict.
            override_use_sudo: When not ``None``, forces the ``use_sudo`` flag
                regardless of what the provider config says.
        """
        #: Dict[str, Any]: provider-specific configuration
        self.provider_config = provider_config or {}

        #: logging.Logger: logger instance
        self.logger = log

        #: str: path/name of the VBoxManage binary
        self.command_path = self.provider_config.get(
            'vboxmanage_cmd', self.DEFAULT_BINARY)

        #: bool: whether to prefix commands with sudo
        self.use_sudo = self.provider_config.get('use_sudo', False)

        #: bool: whether to log each command before running it
        self.verbose = self.provider_config.get('verbose', False)

        #: str: the runtime environment ('local' is the only supported value)
        self.runtime = self.provider_config.get('runtime', 'local')

        if override_use_sudo is not None:
            self.use_sudo = override_use_sudo

    def build_command(self, subcommand: str, *args: Any, **kwargs: Any) -> str:
        """
        Build a complete ``VBoxManage`` command string.

        The binary, subcommand, positional ``args``, and keyword values are
        quoted exactly once with :func:`shlex.quote`. Keyword ``kwargs`` become
        separate ``--flag value`` tokens, matching VBoxManage's native syntax
        rather than libvirt's provider-specific ``--flag=value`` form
        (``True`` -> bare ``--flag``; ``False``/``None`` -> skipped).
        Underscores in keys are converted to dashes. Keyword names are
        code-controlled option identifiers; all caller-provided data belongs in
        values and is quoted. Callers must pass raw values rather than values
        with pre-baked shell quotes.

        Args:
            subcommand: The VBoxManage subcommand (e.g. ``list``, ``clonevm``).
            *args: Positional arguments for the subcommand.
            **kwargs: Options rendered as ``--key value``.

        Returns:
            The command string ready to be split and executed.
        """
        parts: list[str] = []

        if self.use_sudo:
            parts.append('sudo')

        parts.append(shlex.quote(str(self.command_path)))
        parts.append(shlex.quote(str(subcommand)))
        parts.extend(shlex.quote(str(arg)) for arg in args)

        for key, value in kwargs.items():
            flag = f"--{key.replace('_', '-')}"
            if value is True:
                parts.append(flag)
            elif value is False or value is None:
                continue
            else:
                parts.append(flag)
                parts.append(shlex.quote(str(value)))

        return ' '.join(parts)

    def _wrap_for_runtime(self, command: str) -> str:
        """
        Wrap the command for the configured runtime.

        VirtualBox is a host-local hypervisor: ``VBoxManage`` must run directly
        on the host, so only the ``local`` runtime is supported. Any other
        runtime is a configuration error and fails fast here.
        """
        if self.runtime == 'local':
            return command
        raise ValueError(
            f"the 'virtualbox' provider only supports the 'local' runtime, "
            f"got '{self.runtime}'")

    def run(self,
            subcommand: str,
            *args: Any,
            check: bool = False,
            **kwargs: Any) -> subprocess.CompletedProcess:
        """
        Build and execute a ``VBoxManage`` command, capturing its output.

        Args:
            subcommand: The VBoxManage subcommand.
            *args: Positional arguments for the subcommand.
            check: When True, raise :class:`RuntimeError` on a non-zero exit.
            **kwargs: Options rendered as ``--key value``.

        Returns:
            The :class:`subprocess.CompletedProcess`. Unlike the legacy
            ``Command``, ``result.stdout`` / ``result.stderr`` are populated.
        """
        command = self._wrap_for_runtime(self.build_command(subcommand, *args, **kwargs))

        if self.verbose:
            self.logger.info(f">>> {command}")

        # FIX vs legacy: capture_output=True + text=True populate stdout/stderr
        # (the legacy Command.run() commented out communicate(), leaving them
        # None so every downstream parser silently read nothing).
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            check=False,
        )

        if check and result.returncode != 0:
            raise RuntimeError(
                f"VBoxManage command failed: {command}\n"
                f"exit code: {result.returncode}\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}")

        return result

    def list_vms(self) -> dict[str, str]:
        """
        Run ``VBoxManage list vms`` and return a ``{name: uuid}`` mapping.

        Serves as a small proof that output capture is wired up correctly.
        """
        result = self.run('list', 'vms')
        return self.parse_vms(result.stdout)

    @staticmethod
    def parse_vms(output: str | None) -> dict[str, str]:
        """
        Parse ``VBoxManage list vms`` output into a ``{name: uuid}`` dict.

        Each line looks like ``"vm-name" {uuid}``. Returns an empty dict for
        empty/``None`` input (the legacy failure mode, now handled gracefully).
        """
        if not output:
            return {}
        regex = r'(?:\"(.*)\") (?:\{(.*)\})'
        return dict(re.findall(regex, output))

    @staticmethod
    def parse_machinereadable(output: str | None) -> dict[str, str]:
        """
        Parse ``VBoxManage showvminfo --machinereadable`` ``key="value"``
        output into a dict. Empty/``None`` input yields an empty dict.
        """
        if not output:
            return {}
        parsed: dict[str, str] = {}
        for line in output.splitlines():
            if '=' not in line:
                continue
            key, _, value = line.partition('=')
            parsed[key.strip()] = value.strip().strip('"')
        return parsed
