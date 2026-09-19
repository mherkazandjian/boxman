"""
Abstract base for runtime environments.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any


class RuntimeBase(ABC):
    """
    A runtime wraps provider commands so they execute in the right place.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    @abstractmethod
    def wrap_command(self, command: str,
                     pass_env: Sequence[str] | None = None) -> str:
        """
        Wrap *command* for execution in this runtime.

        For a local runtime this is a no-op; for docker-compose it
        prefixes ``docker exec <container> bash -c '...'``.

        *pass_env* names environment variables the command needs that must
        survive the wrapping. A local command inherits the environment, so
        the local runtime ignores it; a containerised one does not, so the
        docker runtime forwards each name. It exists so a secret can be
        handed over out of band rather than on the argv.
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. 'local' or 'docker-compose'."""

    def ensure_ready(self) -> None:  # noqa: B027 - intentional no-op default; local runtime needs no setup
        """
        Ensure the runtime environment is up and ready to accept commands.

        The default implementation is a no-op (local runtime needs nothing).
        Subclasses like DockerComposeRuntime override this to start
        containers and verify health.
        """

    def inject_into_provider_config(
        self, provider_config: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Return a **copy** of *provider_config* enriched with runtime
        information so that ``LibVirtCommandBase`` (and friends) can
        use it transparently.
        """
        cfg = provider_config.copy()
        cfg["runtime"] = self.name
        return cfg
