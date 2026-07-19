"""
``VBoxManage snapshot`` command builders.

Salvages the take / list / delete / restore knowledge from the legacy
``boxman.virtualbox.vbox_snapshot.Snapshot``. Phase 1 provides the pure
builders; the snapshot orchestration lands in Phase 4.
"""

from __future__ import annotations

from typing import Any

from .commands import VBoxManageCommand


class Snapshot:
    """Build ``VBoxManage snapshot <vm> <verb>`` commands."""

    def __init__(self, provider_config: dict[str, Any] | None = None):
        self.provider_config = provider_config or {}
        self.cmd = VBoxManageCommand(provider_config=self.provider_config)

    def build_take_command(self,
                           vm_name: str,
                           snapshot_name: str,
                           description: str | None = None,
                           live: bool = True) -> str:
        """Build ``VBoxManage snapshot <vm> take <name> [--description ..] [--live]``."""
        kwargs: dict[str, Any] = {}
        if description:
            kwargs['description'] = description
        if live:
            kwargs['live'] = True
        return self.cmd.build_command('snapshot', vm_name, 'take', snapshot_name, **kwargs)

    def build_list_command(self, vm_name: str) -> str:
        """Build ``VBoxManage snapshot <vm> list``."""
        return self.cmd.build_command('snapshot', vm_name, 'list')

    def build_delete_command(self, vm_name: str, snapshot_name: str) -> str:
        """Build ``VBoxManage snapshot <vm> delete <name>``."""
        return self.cmd.build_command('snapshot', vm_name, 'delete', snapshot_name)

    def build_restore_command(self, vm_name: str, snapshot_name: str) -> str:
        """Build ``VBoxManage snapshot <vm> restore <name>``."""
        return self.cmd.build_command('snapshot', vm_name, 'restore', snapshot_name)
