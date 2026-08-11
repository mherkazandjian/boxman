"""
The docker-compose provider — provisions container clusters by generating
a per-cluster ``docker-compose.yml`` and driving ``docker compose``.

Phase 3 of the docker-compose provider epic
(https://github.com/mherkazandjian/boxman/issues/51). One compose project
per cluster (ADR-001); the session exposes coarse per-cluster lifecycle
methods that the manager dispatches to (see ``BoxmanManager`` compose
helpers), mirroring the ``ContainerlabManager`` integration pattern.
"""

from __future__ import annotations

from boxman.providers.docker_compose.compose_generator import ComposeGenerator
from boxman.providers.docker_compose.compose_runner import ComposeRunner
from boxman.providers.docker_compose.session import DockerComposeSession

__all__ = ["ComposeGenerator", "ComposeRunner", "DockerComposeSession"]
