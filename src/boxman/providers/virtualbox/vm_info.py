"""
``VBoxManage showvminfo`` command builder + parser.

Salvages the ``showvminfo --machinereadable`` knowledge from the legacy
``boxman.virtualbox.vbox_showvminfo.ShowVmInfo``. Now that output capture is
fixed (see :mod:`boxman.providers.virtualbox.commands`) the parser actually
receives text instead of ``None``. Phase 1 provides the builder + parser; the
info-driven flows land in Phase 2.
"""

from __future__ import annotations

from typing import Any

from .commands import VBoxManageCommand


class VmInfo:
    """Build ``VBoxManage showvminfo`` commands and parse the result."""

    def __init__(self, provider_config: dict[str, Any] | None = None):
        self.provider_config = provider_config or {}
        self.cmd = VBoxManageCommand(provider_config=self.provider_config)

    def build_showvminfo_command(self, vm_name: str) -> str:
        """Build ``VBoxManage showvminfo --details --machinereadable <vm>``."""
        return self.cmd.build_command(
            'showvminfo', vm_name, details=True, machinereadable=True)

    def info(self, vm_name: str) -> dict[str, str]:
        """
        Run ``showvminfo`` and return the parsed ``key=value`` mapping.

        Relies on the fixed runner capturing stdout (the legacy runner left it
        ``None``, so the parser always returned an empty dict).
        """
        result = self.cmd.run(
            'showvminfo', vm_name, details=True, machinereadable=True)
        return VBoxManageCommand.parse_machinereadable(result.stdout)
