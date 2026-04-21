"""
Protocols that describe the provider surface (libvirt / VirtualBox).

``LibVirtSession`` and the legacy VirtualBox session both implement the
same high-level contract: a constructor that takes a config dict, a
mutable ``provider_config`` + ``uri`` + ``use_sudo`` surface, and a
small set of orchestration methods (``start_vm``, ``destroy_vm``,
``clone_vm``, ``define_network``, snapshot APIs, etc.).

These used to be empty sentinel classes (``class Provider: pass``).
Phase 2.3 of the review plan turns them into
:class:`typing.Protocol`\\s so that code annotated with the protocol
type-checks against any concrete implementation — the protocols are
structural (duck-typed), not an inheritance contract.

Only the methods that are actually consumed by ``BoxmanManager`` and the
CLI are listed here. Provider-internal helpers stay off the protocol so
that implementations can keep internal refactoring flexibility.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Provider(Protocol):
    """A provider represents the static side of an infrastructure backend.

    At the moment this is thin — the bulk of the contract is on
    :class:`ProviderSession`. Kept as its own protocol so future
    provider-level metadata (display name, available regions, etc.)
    can be added without churning session-level callers.
    """

    name: str


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
