"""
``VBoxManage`` VM teardown command builders.

Salvages the ``list vms`` / ``controlvm poweroff`` / ``unregistervm --delete``
knowledge from the legacy ``Virtualbox.removevm`` / ``unregistervm`` path.
Phase 1 provides the pure builders; the guarded remove flow lands in Phase 2.
"""

from __future__ import annotations

from typing import Any

from .commands import VBoxManageCommand


class DestroyVM:
    """Build ``VBoxManage`` commands to power off and unregister a VM."""

    def __init__(self, provider_config: dict[str, Any] | None = None):
        self.provider_config = provider_config or {}
        self.cmd = VBoxManageCommand(provider_config=self.provider_config)

    def build_list_vms_command(self) -> str:
        """Build ``VBoxManage list vms``."""
        return self.cmd.build_command('list', 'vms')

    def build_poweroff_command(self, name: str) -> str:
        """Build ``VBoxManage controlvm <name> poweroff``."""
        return self.cmd.build_command('controlvm', name, 'poweroff')

    def build_unregister_command(self, name: str, delete: bool = True) -> str:
        """Build ``VBoxManage unregistervm <name> [--delete]``."""
        return self.cmd.build_command('unregistervm', name, delete=delete)

    def remove(self, *args: Any, **kwargs: Any) -> bool:
        raise NotImplementedError(
            "VirtualBox provider: DestroyVM.remove lands in Phase 2")
