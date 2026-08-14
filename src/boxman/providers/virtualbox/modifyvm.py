"""
``VBoxManage modifyvm`` command builders.

Salvages the NIC/network-settings and NAT port-forward knowledge from the
legacy ``boxman.virtualbox.vbox_modifyvm.NetworkSettings`` and the
``forward_local_port_to_vm`` helper. Phase 1 provides the pure builders; the
per-interface configuration flow lands in Phase 2.
"""

from __future__ import annotations

from typing import Any

from .commands import VBoxManageCommand


class ModifyVm:
    """Build ``VBoxManage modifyvm`` commands."""

    def __init__(self, provider_config: dict[str, Any] | None = None):
        self.provider_config = provider_config or {}
        self.cmd = VBoxManageCommand(provider_config=self.provider_config)

    def build_nic_command(self,
                          vm: str,
                          nic_num: int,
                          nic_type: str,
                          network_name: str | None = None,
                          cable_connected: bool | None = None) -> str:
        """
        Build ``VBoxManage modifyvm <vm> --nic<n> <type> [--nat-network<n> ..]``.

        Args:
            vm: Name/uuid of the VM.
            nic_num: 1-based NIC index.
            nic_type: e.g. ``nat`` or ``natnetwork``.
            network_name: NAT-network name (required for ``natnetwork``).
            cable_connected: Optional cable-connected toggle.
        """
        args: list[Any] = [vm, f'--nic{nic_num}', nic_type]
        if nic_type == 'natnetwork':
            if not network_name:
                raise ValueError("natnetwork nic requires a network_name")
            args.extend([f'--nat-network{nic_num}', network_name])
        if cable_connected is not None:
            args.extend([f'--cableconnected{nic_num}', 'on' if cable_connected else 'off'])
        return self.cmd.build_command('modifyvm', *args)

    def build_natpf_command(self,
                            vm: str,
                            host_port: int,
                            guest_port: int,
                            rule_name: str = 'guestssh',
                            nic_num: int = 1) -> str:
        """Build a ``VBoxManage modifyvm <vm> --natpf<n> <rule>`` command.

        ``<rule>`` is passed to the central command builder as an unquoted raw
        value. The rendered shell string contains quotes only when the rule's
        contents require them; :meth:`VBoxManageCommand.run` splits it back
        into one exact argv token before execution.
        """
        rule = f'{rule_name},tcp,,{host_port},,{guest_port}'
        return self.cmd.build_command('modifyvm', vm, f'--natpf{nic_num}', rule)
