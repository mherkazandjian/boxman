"""
Local runtime – commands execute directly on the host.
"""

from boxman.runtime.base import RuntimeBase


class LocalRuntime(RuntimeBase):

    @property
    def name(self) -> str:
        return "local"

    def wrap_command(self, command: str) -> str:
        return command
