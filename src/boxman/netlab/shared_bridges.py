"""Host-level Linux bridge management for shared L2 domains.

A ``shared_networks:`` entry in ``conf.yml`` maps to a plain Linux
bridge on the host. Both sides of a hybrid topology attach to the same
bridge:

- boxman libvirt VMs via ``<interface type='bridge'><source bridge='X'/>``.
- External lab tools (containerlab ``host:`` endpoint, EVE-NG pnet,
  GNS3 cloud) via their own veth plumbing.

The resulting L2 domain lets a boxman VM and an emulated switch trade
LLDP/DHCP/802.1Q/STP as if they were cabled into the same physical
switch.

This module is intentionally small: create-if-missing, bring up, set
a couple of sysfs knobs. No teardown — shared bridges can be referenced
by multiple boxman projects concurrently, so removing them is an
explicit user action, not a side effect of ``boxman destroy``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from boxman import log
from boxman.utils.shell import run


def _bridge_exists(name: str) -> bool:
    result = run(f"ip link show dev {name}", warn=True, hide=True)
    return result.ok


def _run_sudo(cmd: str) -> None:
    """Run a root-required command via sudo, raising on failure."""
    run(f"sudo {cmd}", hide=True)


def _set_sysfs(path: str, value: str) -> None:
    """Write *value* to *path* under sysfs / procfs via ``tee``."""
    run(f"echo {value} | sudo tee {path}", hide=True)


def ensure(shared_networks: dict[str, dict[str, Any]] | None) -> None:
    """Ensure every bridge declared in *shared_networks* exists and is up.

    Idempotent. Safe to call repeatedly and across projects.

    Each entry is a dict with:

    - ``bridge`` (str, required): the Linux bridge name on the host.
    - ``stp`` (bool, default False): enable STP on the bridge.
    - ``disable_netfilter`` (bool, default True): set
      ``/proc/sys/net/bridge/bridge-nf-call-iptables=0`` so lab frames
      aren't dropped by host iptables rules. System-wide setting;
      we flip it once per ``ensure()`` call if any entry asks for it.
    """
    if not shared_networks:
        return

    want_nf_disabled = False
    for entry_name, entry in shared_networks.items():
        bridge = entry.get("bridge")
        if not bridge:
            raise ValueError(
                f"shared_networks[{entry_name!r}] missing required 'bridge' key"
            )

        if _bridge_exists(bridge):
            log.info(f"shared bridge {bridge!r} already present")
        else:
            log.info(f"creating shared bridge {bridge!r}")
            _run_sudo(f"ip link add name {bridge} type bridge")

        _run_sudo(f"ip link set dev {bridge} up")

        stp = "on" if entry.get("stp", False) else "off"
        _run_sudo(f"ip link set dev {bridge} type bridge stp_state "
                  f"{1 if stp == 'on' else 0}")

        if entry.get("disable_netfilter", True):
            want_nf_disabled = True

    if want_nf_disabled:
        nf_path = Path("/proc/sys/net/bridge/bridge-nf-call-iptables")
        if nf_path.exists():
            _set_sysfs(str(nf_path), "0")
        else:
            log.warning(
                "br_netfilter not loaded; skipping bridge-nf-call-iptables=0. "
                "If lab traffic is dropped, run `sudo modprobe br_netfilter` "
                "and retry."
            )


def is_shared_bridge(name: str,
                     shared_networks: dict[str, dict[str, Any]] | None) -> bool:
    """Return True iff *name* is a key in *shared_networks*."""
    if not shared_networks:
        return False
    return name in shared_networks


def resolve_bridge(name: str,
                   shared_networks: dict[str, dict[str, Any]] | None) -> str:
    """Return the underlying host bridge name for a shared_networks key.

    Raises ``KeyError`` if *name* is not a shared network.
    """
    if not shared_networks or name not in shared_networks:
        raise KeyError(f"{name!r} is not a shared_networks entry")
    bridge = shared_networks[name].get("bridge")
    if not bridge:
        raise ValueError(
            f"shared_networks[{name!r}] missing required 'bridge' key"
        )
    return bridge
