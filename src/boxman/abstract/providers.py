"""
Protocol that describes the shared provider-session surface.

``LibVirtSession``, ``VirtualBoxSession`` and ``DockerComposeSession``
all implement the same core contract: a constructor that takes a config
dict, a mutable ``provider_config`` + ``uri`` + ``use_sudo`` surface
(see :class:`boxman.providers.session_base.SessionConfigMixin`), and a
common set of lifecycle/network/snapshot methods.

This used to be an empty sentinel class (``class Provider: pass``).
Phase 2.3 of the review plan turned it into a :class:`typing.Protocol`
so that code annotated with the protocol type-checks against any
concrete implementation — the protocol is structural (duck-typed), not
an inheritance contract.

The protocol deliberately lists only the surface that is **uniform
across all three providers** — the subset ``BoxmanManager`` and the CLI
drive provider-agnostically. The manager calls many more
provider-specific methods (per-cluster lifecycle on docker-compose,
plan/update/storage ops on libvirt); those stay off the protocol so
implementations keep their internal refactoring flexibility.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProviderSession(Protocol):
    """A live session against a provider — capable of mutating its state.

    Every concrete session (e.g. ``LibVirtSession``) satisfies this
    protocol by attribute/method shape, so ``isinstance(x, ProviderSession)``
    works at runtime and ``x: ProviderSession`` type-checks statically.
    """

    # --- config surface -----------------------------------------------------
    provider_config: dict[str, Any]
    uri: str
    use_sudo: bool

    def update_provider_config(self, new_config: dict[str, Any]) -> None: ...

    # --- VM lifecycle -------------------------------------------------------
    def start_vm(self, vm_name: str) -> bool: ...
    def destroy_vm(self, name: str, force: bool = False) -> bool: ...
    def clone_vm(
        self,
        new_vm_name: str,
        src_vm_name: str,
        info: dict[str, Any],
        workdir: str,
    ) -> bool: ...

    # --- networking ---------------------------------------------------------
    def define_network(
        self,
        name: str | None = None,
        info: dict[str, Any] | None = None,
        workdir: str | None = None,
    ) -> bool: ...

    def destroy_network(
        self,
        name: str | None = None,
        info: dict[str, Any] | None = None,
    ) -> bool: ...

    def remove_network(
        self,
        name: str | None = None,
        info: dict[str, Any] | None = None,
    ) -> bool: ...

    # --- snapshots ----------------------------------------------------------
    def snapshot_take(self, *args: Any, **kwargs: Any) -> bool: ...
    def snapshot_restore(self, vm_name: str, snapshot_name: str | None = None) -> bool: ...
    def snapshot_delete(self, vm_name: str, snapshot_name: str) -> bool: ...
    def snapshot_list(self, vm_name: str | None = None) -> list[dict[str, str]]: ...
