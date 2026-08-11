"""
Provider registry: maps provider-type names to session factories.

The registry is the single place that knows how to construct a live
provider session (``LibVirtSession``, the legacy ``Virtualbox``, …) from
a project configuration. It mirrors the ``create_runtime(name, **kwargs)``
factory convention in :mod:`boxman.runtime`.

Factories import their session class lazily so that importing
``boxman.providers`` (or a sibling provider package) never drags in the
dependencies of every other provider.
"""

from __future__ import annotations

from typing import Any, Callable

from boxman.abstract.providers import ProviderSession


def _libvirt_session(config: dict[str, Any]) -> "ProviderSession":
    from boxman.providers.libvirt.session import LibVirtSession
    return LibVirtSession(config)


def _virtualbox_session(config: dict[str, Any]) -> "ProviderSession":
    from boxman.providers.virtualbox.session import VirtualBoxSession
    return VirtualBoxSession(config)


def _docker_compose_session(config: dict[str, Any]) -> "ProviderSession":
    from boxman.providers.docker_compose.session import DockerComposeSession
    return DockerComposeSession(config)


#: provider-type name -> factory producing a live session for that provider
PROVIDERS: dict[str, Callable[[dict[str, Any]], "ProviderSession"]] = {
    'libvirt': _libvirt_session,
    'virtualbox': _virtualbox_session,
    'docker-compose': _docker_compose_session,
}


def create_session(provider_type: str, config: dict[str, Any]) -> "ProviderSession":
    """
    Create a live session for *provider_type* from *config*.

    Args:
        provider_type: A key of :data:`PROVIDERS` (e.g. ``libvirt``).
        config: The (already enriched) configuration dict handed to the
            session constructor.

    Returns:
        The constructed provider session.

    Raises:
        ValueError: If *provider_type* is not a registered provider.
    """
    try:
        factory = PROVIDERS[provider_type]
    except KeyError:
        supported = ', '.join(sorted(PROVIDERS))
        raise ValueError(
            f"unknown provider type: '{provider_type}' "
            f"(supported: {supported})"
        ) from None
    return factory(config)


def primary_provider_type(config: dict[str, Any] | None) -> str:
    """
    Return the project's primary provider type.

    The primary provider is the first key under the top-level
    ``provider:`` mapping of the project config; projects without a
    ``provider:`` section default to ``libvirt``. Until config schema
    v2.0 introduces per-cluster providers (Phase 2 of the docker-compose
    provider epic), this is the type every cluster resolves to.
    """
    providers = list(((config or {}).get('provider') or {}).keys())
    return providers[0] if providers else 'libvirt'
