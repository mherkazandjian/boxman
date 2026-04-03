"""
Abstract base for runtime environments.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class RuntimeBase(ABC):
    """
    A runtime wraps provider commands so they execute in the right place.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}

    @abstractmethod
    def wrap_command(self, command: str) -> str:
        """
        Wrap *command* for execution in this runtime.

        For a local runtime this is a no-op; for docker-compose it
        prefixes ``docker exec <container> bash -c '...'``.
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. 'local' or 'docker-compose'."""

    def ensure_ready(self) -> None:
        """
        Ensure the runtime environment is up and ready to accept commands.

        The default implementation is a no-op (local runtime needs nothing).
        Subclasses like DockerComposeRuntime override this to start
        containers and verify health.
        """

    def inject_into_provider_config(
        self, provider_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Return a **copy** of *provider_config* enriched with runtime
        information so that ``LibVirtCommandBase`` (and friends) can
        use it transparently.
        """
        cfg = provider_config.copy()
        cfg["runtime"] = self.name
        return cfg
