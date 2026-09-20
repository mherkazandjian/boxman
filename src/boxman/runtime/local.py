"""
Local runtime – commands execute directly on the host.
"""

from collections.abc import Sequence

from boxman.runtime.base import RuntimeBase


class LocalRuntime(RuntimeBase):

    @property
    def name(self) -> str:
        return "local"

    def wrap_command(self, command: str,
                     pass_env: Sequence[str] | None = None) -> str:
        # a local command already has this process's environment
        return command
