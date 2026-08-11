"""
``VBoxManage`` storage/medium command builders.

Salvages the ``createmedium`` / ``closemedium`` / ``storageattach`` knowledge
from the legacy ``Virtualbox`` session. The default medium format is sourced
from the provider config key ``default_medium_format`` (default ``VDI``).
Phase 1 provides the pure builders; disk provisioning lands in Phase 2.
"""

from __future__ import annotations

from typing import Any

from .commands import VBoxManageCommand

_VALID_MEDIUM_TYPES = ('disk', 'dvd', 'floppy')


class Storage:
    """Build ``VBoxManage`` medium/storage commands."""

    def __init__(self, provider_config: dict[str, Any] | None = None):
        self.provider_config = provider_config or {}
        self.cmd = VBoxManageCommand(provider_config=self.provider_config)
        #: default disk format for created media (VDI | VMDK | VHD)
        self.default_medium_format = self.provider_config.get(
            'default_medium_format', 'VDI')

    def build_createmedium_command(self,
                                   filename: str,
                                   size_mb: int,
                                   medium_type: str = 'disk',
                                   medium_format: str | None = None,
                                   variant: str = 'Standard') -> str:
        """Build ``VBoxManage createmedium <type> --filename .. --format .. --size .. --variant ..``."""
        if medium_type not in _VALID_MEDIUM_TYPES:
            raise ValueError(f"medium_type must be one of {_VALID_MEDIUM_TYPES}")
        medium_format = medium_format or self.default_medium_format
        return self.cmd.build_command(
            'createmedium', medium_type,
            filename=filename, format=medium_format, size=size_mb, variant=variant)

    def build_closemedium_command(self,
                                  target: str,
                                  medium_type: str = 'disk',
                                  delete: bool = False) -> str:
        """Build ``VBoxManage closemedium <type> <target> [--delete]``."""
        if medium_type not in _VALID_MEDIUM_TYPES:
            raise ValueError(f"medium_type must be one of {_VALID_MEDIUM_TYPES}")
        return self.cmd.build_command(
            'closemedium', medium_type, target, delete=delete)

    def build_storageattach_command(self,
                                    vm: str,
                                    storagectl: str,
                                    port: int,
                                    medium: str,
                                    medium_type: str = 'hdd') -> str:
        """Build ``VBoxManage storageattach <vm> --storagectl .. --port .. --medium .. --type ..``."""
        return self.cmd.build_command(
            'storageattach', vm,
            storagectl=storagectl, port=port, medium=medium, type=medium_type)
