"""
Runtime environments for boxman.

A *runtime* controls **where** provider commands are executed:
  - ``local``          – directly on the host (default)
  - ``docker``         – inside a boxman docker-compose container
"""

from boxman.runtime.base import RuntimeBase
from boxman.runtime.local import LocalRuntime
from boxman.runtime.docker_compose import DockerComposeRuntime


def create_runtime(name: str, **kwargs) -> RuntimeBase:
    """
    Factory: return a runtime instance for the given name.

    Args:
        name: One of 'local', 'docker'.
        **kwargs: Passed to the runtime constructor.

    Returns:
        A RuntimeBase subclass instance.

    Raises:
        ValueError: If the runtime name is unknown.
    """
    runtimes = {
        "local": LocalRuntime,
        "docker": DockerComposeRuntime,
        "docker-compose": DockerComposeRuntime,
    }
    if name not in runtimes:
        raise ValueError(
            f"unknown runtime '{name}', supported: {', '.join(runtimes)}"
        )
    return runtimes[name](**kwargs)
