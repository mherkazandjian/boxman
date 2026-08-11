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

from collections.abc import Callable
from typing import Any

from boxman.abstract.providers import ProviderSession


def _libvirt_session(config: dict[str, Any]) -> ProviderSession:
    from boxman.providers.libvirt.session import LibVirtSession
    return LibVirtSession(config)


def _virtualbox_session(config: dict[str, Any]) -> ProviderSession:
    from boxman.providers.virtualbox.session import VirtualBoxSession
    return VirtualBoxSession(config)


def _docker_compose_session(config: dict[str, Any]) -> ProviderSession:
    from boxman.providers.docker_compose.session import DockerComposeSession
    return DockerComposeSession(config)


#: provider-type name -> factory producing a live session for that provider
PROVIDERS: dict[str, Callable[[dict[str, Any]], ProviderSession]] = {
    'libvirt': _libvirt_session,
    'virtualbox': _virtualbox_session,
    'docker-compose': _docker_compose_session,
}


def create_session(provider_type: str, config: dict[str, Any]) -> ProviderSession:
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


def merge_provider_configs(global_config: dict[str, Any],
                           local_config: dict[str, Any]) -> dict[str, Any]:
    """
    Merge global (app-level) and local (project-level) provider configs.

    This is the single merge implementation used by every code path that
    combines boxman.yml ``providers:`` settings with a project's
    ``provider:`` block (CLI session creation, ``show_conf``, and the
    manager's one-off virsh probes).

    Scalar keys: local overrides global (standard dict.update behaviour).

    ``sudo_skip_commands`` / ``force_sudo_commands``: per-command merging.
    If a command appears in a local list it is removed from the opposite
    global list so that the local setting wins.  Commands that only appear
    in the global lists are preserved.
    """
    merged = global_config.copy()
    # pull out the sudo command lists before the scalar update clobbers them
    g_skip = set(global_config.get('sudo_skip_commands', []))
    g_force = set(global_config.get('force_sudo_commands', []))
    l_skip = set(local_config.get('sudo_skip_commands', []))
    l_force = set(local_config.get('force_sudo_commands', []))

    # scalar overrides
    merged.update(local_config)

    # per-command merge: local wins over global for any given command
    all_local = l_skip | l_force
    final_skip = (g_skip - all_local) | l_skip
    final_force = (g_force - all_local) | l_force
    # if a command ended up in both after merge, force wins
    final_skip -= final_force

    merged['sudo_skip_commands'] = sorted(final_skip)
    merged['force_sudo_commands'] = sorted(final_force)
    return merged


def primary_provider_type(config: dict[str, Any] | None) -> str:
    """
    Return the project's primary provider type.

    The primary provider is the first key under the top-level
    ``provider:`` mapping of the project config; projects without a
    ``provider:`` section default to ``libvirt``. With config schema
    v2.0, clusters may override this with their own provider — this
    function returns the project-wide default.
    """
    providers = list(((config or {}).get('provider') or {}).keys())
    return providers[0] if providers else 'libvirt'
