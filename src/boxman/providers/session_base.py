"""
Shared config surface for provider sessions (#85 item 34).

Every provider session (``LibVirtSession``, ``VirtualBoxSession``,
``DockerComposeSession``) carries the same small config-resolution
surface: a constructor that extracts its own block from the project
config, a mutable ``provider_config``, ``uri`` / ``use_sudo``
properties, and ``update_provider_config``.

The semantics are uniform across providers:

* The session is handed an **already-merged** config (app-level
  defaults + runtime injection, overlaid by project-level settings via
  :func:`boxman.providers.merge_provider_configs`), so no precedence
  handling happens here.
* :meth:`SessionConfigMixin.update_provider_config` is a plain
  last-write-wins dict update, used for runtime metadata injection,
  which never carries project keys.

Per-provider differences are confined to two class attributes:
``provider_key`` (which block of ``provider:`` the session reads) and
``default_uri`` (libvirt defaults to ``qemu:///system``; the other
providers have no connection URI and default to the empty string).
"""

from __future__ import annotations

from typing import Any

from boxman import log


class SessionConfigMixin:
    """The shared config surface consumed through ``ProviderSession``."""

    #: str: key of this provider's block under the project's ``provider:``
    #: mapping (e.g. ``libvirt``, ``docker-compose``).
    provider_key: str = ''

    #: str: value ``uri`` falls back to when the config does not set one.
    default_uri: str = ''

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """
        Initialize the session config surface.

        Construction is deliberately side-effect free — nothing external
        is touched (no libvirt connection, no VBoxManage / docker call).

        Args:
            config: Optional project configuration dictionary. Its
                ``provider[<provider_key>]`` block is expected to be
                already merged (see the module docstring).
        """
        #: Dict[str, Any]: the configuration for this session
        self.config: dict[str, Any] = config or {}

        #: logging.Logger: the logger instance
        self.logger = log

        #: Dict[str, Any]: the effective (already-merged) provider config
        self._provider_config: dict[str, Any] = dict(
            ((self.config.get('provider') or {}).get(self.provider_key)) or {})

        #: the boxman manager instance (set by the CLI after construction)
        self.manager = None

    @property
    def provider_config(self) -> dict[str, Any]:
        return self._provider_config

    @provider_config.setter
    def provider_config(self, value: dict[str, Any]) -> None:
        self._provider_config = value or {}

    @property
    def uri(self) -> str:
        """Connection URI (empty for providers without one)."""
        return self.provider_config.get('uri', self.default_uri)

    @uri.setter
    def uri(self, value: str) -> None:
        self.provider_config['uri'] = value

    @property
    def use_sudo(self) -> bool:
        return self.provider_config.get('use_sudo', False)

    @use_sudo.setter
    def use_sudo(self, value: bool) -> None:
        self.provider_config['use_sudo'] = value

    def update_provider_config(self, new_config: dict[str, Any]) -> None:
        """
        Update provider_config with *new_config* (plain last-write-wins).

        Precedence between app-level and project-level settings is resolved
        upstream by :func:`boxman.providers.merge_provider_configs` before
        the session is built; this is only used for runtime metadata
        injection, which never carries project keys.

        Args:
            new_config: Additional config keys (e.g. runtime injection).
        """
        self.provider_config.update(new_config or {})

    def update_provider_config_with_runtime(self) -> None:
        """
        Default no-op: providers that only support the ``local`` runtime
        have no runtime metadata to inject. ``LibVirtSession`` overrides
        this with the real enrichment; present here so the manager can
        call it uniformly across providers.
        """
        return None
