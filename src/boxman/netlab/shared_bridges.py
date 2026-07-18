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


def _scoped_rule_body(bridge: str) -> str:
    """The physdev accept rule body shared by ``-C`` (check) and ``-I`` (insert).

    ``-i <br> -o <br> -m physdev --physdev-is-bridged`` matches frames the
    bridge **forwards between two of its ports** (br_netfilter's FORWARD
    traversal) — e.g. VM↔VM or VM↔containerlab-veth L2 lab traffic on the
    shared bridge — so it isn't dropped by a docker-style
    ``FORWARD``/``DOCKER-USER`` DROP policy while the host keeps
    ``bridge-nf-call-iptables=1`` (decision D8). It is also installed as
    belt-and-braces for macvlan-attached containers, though a macvlan endpoint
    is a child of the bridge *device* rather than a bridge port, so on mainline
    kernels VM↔container frames use the bridge local pass-up / ``br_dev_xmit``
    paths and likely do not traverse filter ``FORWARD`` at all.
    """
    return (f"-i {bridge} -o {bridge} "
            f"-m physdev --physdev-is-bridged -j ACCEPT")


def _iptables_chain_exists(chain: str) -> bool:
    return run(f"sudo iptables -t filter -n -L {chain}",
               warn=True, hide=True).ok


def _ensure_iptables_rule(chain: str, body: str) -> None:
    """Idempotently insert ``body`` at the top of filter table *chain*.

    Uses ``-C`` (check) before ``-I`` (insert at position 1) so repeated
    ``ensure()`` calls don't stack duplicate rules (spike scenario 7).
    """
    if run(f"sudo iptables -t filter -C {chain} {body}",
           warn=True, hide=True).ok:
        return  # already present
    _run_sudo(f"iptables -t filter -I {chain} 1 {body}")


def _ensure_scoped_accept(bridge: str) -> None:
    """Allow bridged lab frames on *bridge* via scoped per-bridge rules (D8).

    Inserts into ``FORWARD`` (works without docker) and, when the
    ``DOCKER-USER`` chain exists (docker host), there too — that copy survives
    a docker daemon restart (docker recreates but does not flush DOCKER-USER),
    which the spike confirmed.
    """
    body = _scoped_rule_body(bridge)
    _ensure_iptables_rule("FORWARD", body)
    if _iptables_chain_exists("DOCKER-USER"):
        _ensure_iptables_rule("DOCKER-USER", body)


def ensure(shared_networks: dict[str, dict[str, Any]] | None) -> None:
    """Ensure every bridge declared in *shared_networks* exists and is up.

    Idempotent. Safe to call repeatedly and across projects.

    Each entry is a dict with:

    - ``bridge`` (str, required): the Linux bridge name on the host.
    - ``stp`` (bool, default False): enable STP on the bridge.
    - ``disable_netfilter`` (bool, default **False**): when False (the
      default, decision D8), lab frames are allowed by an idempotent
      per-bridge scoped ``iptables`` accept rule and the host-global
      ``bridge-nf-call-iptables`` is left untouched. When True (an explicit,
      discouraged opt-in), the host-global ``bridge-nf-call-iptables=0`` is
      set instead, with a loud warning — it weakens docker/k8s bridge
      filtering host-wide and is reverted by any reboot / kubelet.

    ``subnet``/``gateway``/``ip_range`` may also be present; those are
    consumed by the docker-compose generator (macvlan IPAM), not here.
    """
    if not shared_networks:
        return

    globally_disabled: list[str] = []
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

        # Decision D8: default to scoped per-bridge accept rules; the
        # host-global sysctl disable is an explicit opt-in.
        if entry.get("disable_netfilter", False):
            globally_disabled.append(bridge)
        else:
            _ensure_scoped_accept(bridge)

    if globally_disabled:
        log.warning(
            f"disable_netfilter: true set for shared bridge(s) "
            f"{', '.join(repr(b) for b in globally_disabled)} — setting the "
            f"host-global bridge-nf-call-iptables=0. This weakens docker/k8s "
            f"bridge filtering HOST-WIDE, is reverted by any reboot "
            f"(br_netfilter defaults the sysctl to 1 on load) and by kubelet "
            f"on kubernetes hosts. Prefer the default (scoped per-bridge accept "
            f"rules) — drop 'disable_netfilter: true' unless you specifically "
            f"need the global disable."
        )
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
