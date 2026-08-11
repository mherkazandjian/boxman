"""
``VBoxManage clonevm`` command builder.

Salvages the command-string knowledge from the legacy
``boxman.virtualbox.vboxmanage.Virtualbox.clonevm``. Phase 1 provides the pure
builder; wiring it into a working clone flow lands in Phase 2.
"""

from __future__ import annotations

from typing import Any

from .commands import VBoxManageCommand


class CloneVM:
    """Build ``VBoxManage clonevm`` commands."""

    def __init__(self, provider_config: dict[str, Any] | None = None):
        self.provider_config = provider_config or {}
        self.cmd = VBoxManageCommand(provider_config=self.provider_config)

    def build_clone_command(self,
                            src_vm_name: str,
                            new_vm_name: str,
                            snapshot: str | None = None,
                            mode: str = 'all',
                            basefolder: str | None = None,
                            register: bool = True) -> str:
        """
        Build the ``VBoxManage clonevm`` command string.

        Args:
            src_vm_name: Name/uuid of the source VM.
            new_vm_name: Name of the new (cloned) VM.
            snapshot: Optional snapshot uuid/name to clone from.
            mode: ``machine`` | ``machineandchildren`` | ``all``.
            basefolder: Optional path where the new VM data is stored.
            register: Whether to register the clone with VirtualBox.
        """
        kwargs: dict[str, Any] = {'mode': mode, 'name': new_vm_name}
        if snapshot is not None:
            kwargs['snapshot'] = snapshot
        if basefolder is not None:
            kwargs['basefolder'] = basefolder
        if register:
            kwargs['register'] = True
        return self.cmd.build_command('clonevm', src_vm_name, **kwargs)

    def clone(self, *args: Any, **kwargs: Any) -> bool:
        raise NotImplementedError(
            "VirtualBox provider: CloneVM.clone lands in Phase 2")
