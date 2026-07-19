"""
``VBoxManage natnetwork`` command builders + list parser.

Salvages the NAT-network knowledge from the legacy
``boxman.virtualbox.vbox_natnetwork`` / ``natnetwork`` modules. Two legacy bugs
are fixed here:

* the ``add`` builder referenced an undefined ``dhcp`` variable while the
  keyword argument was misspelled ``dchp`` (a latent ``NameError``);
* output capture is now real (see :mod:`boxman.providers.virtualbox.commands`).

Phase 1 provides the pure builders + parser; the create/destroy flow lands in
Phase 3.
"""

from __future__ import annotations

import re
from typing import Any

from .commands import VBoxManageCommand


class NatNetwork:
    """Build ``VBoxManage natnetwork`` commands and parse ``natnetwork list``."""

    def __init__(self, provider_config: dict[str, Any] | None = None):
        self.provider_config = provider_config or {}
        self.cmd = VBoxManageCommand(provider_config=self.provider_config)

    def build_add_command(self,
                          network_name: str,
                          network: str,
                          enable: bool = False,
                          dhcp: str = 'on') -> str:
        """
        Build ``VBoxManage natnetwork add`` (dhcp is ``on``/``off``).

        Args:
            network_name: Name of the NAT network.
            network: CIDR of the network (e.g. ``10.0.1.0/24``).
            enable: Whether to enable the network.
            dhcp: ``on`` or ``off`` (``None``/``False`` -> ``off``).
        """
        if dhcp in (None, False):
            dhcp = 'off'
        if dhcp not in ('on', 'off'):
            raise ValueError(f"dhcp must be 'on' or 'off', got {dhcp!r}")
        kwargs: dict[str, Any] = {'netname': network_name, 'network': network, 'dhcp': dhcp}
        if enable:
            kwargs['enable'] = True
        return self.cmd.build_command('natnetwork', 'add', **kwargs)

    def build_stop_command(self, network_name: str) -> str:
        """Build ``VBoxManage natnetwork stop --netname <name>``."""
        return self.cmd.build_command('natnetwork', 'stop', netname=network_name)

    def build_remove_command(self, network_name: str) -> str:
        """Build ``VBoxManage natnetwork remove --netname <name>``."""
        return self.cmd.build_command('natnetwork', 'remove', netname=network_name)

    def build_list_command(self) -> str:
        """Build ``VBoxManage natnetwork list``."""
        return self.cmd.build_command('natnetwork', 'list')

    @staticmethod
    def parse_list(output: str | None) -> dict[str, dict[str, str]]:
        """
        Parse ``VBoxManage natnetwork list`` output into
        ``{name: {name, network, gateway, ipv6, enabled}}``.
        """
        if not output:
            return {}
        networks: dict[str, dict[str, str]] = {}
        patterns = ['Name', 'Network', 'Gateway', 'IPv6', 'Enabled']
        for block in re.finditer(r"(Name.*(?:\n.+)+)", output):
            info: dict[str, str] = {}
            for pattern in patterns:
                matches = re.findall(
                    rf"(?i)^{pattern}\:(.*$)", block.group(0), re.MULTILINE)
                if matches:
                    info[pattern.lower()] = matches[0].strip()
            if 'name' in info:
                networks[info['name']] = info
        return networks
