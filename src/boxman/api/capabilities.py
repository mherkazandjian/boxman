"""
Provider capability map — drives the ``GET /projects/{p}/capabilities``
endpoint and the 501/409 gating for provider-specific operations.

Capabilities are coarse keys (matching :attr:`boxman.api.operations.Op.cap`).
A provider that doesn't advertise a cap cannot run any operation declaring it,
so the API rejects those with ``501 Not Implemented`` rather than shelling out
to a command that would fail confusingly.
"""

from __future__ import annotations

#: provider name → set of supported capability keys.
PROVIDER_CAPABILITIES: dict[str, set[str]] = {
    "libvirt": {
        "meta",
        "lifecycle",
        "templates",
        "image",
        "snapshot",
        "storage",
        "storage.guest-agent",
        "storage.qcow2",
        "control",
        "pxe",
        "run",
        "netlab",
        "export",
    },
    # Legacy provider — intentionally conservative until exercised.
    "virtualbox": {
        "meta",
        "lifecycle",
        "snapshot",
        "control",
        "image",
        "export",
    },
}

#: Caps that never depend on the provider (always available).
_PROVIDER_AGNOSTIC = {"meta", "run"}


def caps_for(provider: str) -> set[str]:
    """Return the capability set for a provider (unknown → agnostic only)."""
    return PROVIDER_CAPABILITIES.get(provider, set()) | _PROVIDER_AGNOSTIC


def supports(provider: str, cap: str) -> bool:
    return cap in caps_for(provider)


def universal_caps() -> set[str]:
    """Caps supported by *every* known provider (no detection needed to gate).

    Used to skip the (relatively expensive) provider-detection step for common
    operations like lifecycle / snapshot / control that all providers support.
    """
    sets = list(PROVIDER_CAPABILITIES.values())
    base = set.intersection(*sets) if sets else set()
    return base | _PROVIDER_AGNOSTIC
