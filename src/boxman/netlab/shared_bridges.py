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

import os
import re
import shlex
from pathlib import Path
from typing import Any

from boxman import log
from boxman.exceptions import ConfigError
from boxman.utils.shell import run

#: Valid Linux bridge names: shell-safe characters only, and at most 15
#: chars — the kernel's IFNAMSIZ is 16 bytes *including* the trailing NUL.
BRIDGE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,15}$")


def _validate_bridge_name(name: str, entry_name: str) -> None:
    """Raise :class:`ConfigError` unless *name* is a usable bridge name.

    Bridge names are interpolated into sudo'd shell commands and must
    fit the kernel's IFNAMSIZ 15-character limit, so reject anything
    outside ``^[a-zA-Z0-9_.-]{1,15}$`` up front.
    """
    if not BRIDGE_NAME_RE.fullmatch(name):
        raise ConfigError(
            f"shared_networks[{entry_name!r}]: invalid bridge name {name!r}: "
            f"must match {BRIDGE_NAME_RE.pattern} "
            f"(kernel IFNAMSIZ 15-char limit)"
        )


def _normalise_bool(value: Any, entry_name: str, key: str) -> bool:
    """Coerce a configured on/off value to a bool.

    Same accepted spellings as a libvirt network's ``bridge.stp`` (see
    ``Network._normalise_stp``): yaml reads an unquoted ``on`` as the boolean
    True, while a quoted ``"off"`` arrives as a string. Plain truthiness is
    wrong for the latter -- a non-empty ``"off"`` is truthy, so it would turn
    the setting *on*, which for ``disable_netfilter`` means silently weakening
    bridge filtering host-wide. Anything that is neither on nor off is
    rejected rather than quietly becoming ``off``.
    """
    # A fast path only: str(True).lower() is "true" and str(False).lower() is
    # "false", both of which the tables below already accept, so removing this
    # changes nothing observable. Noted because a branch-guard mutation sweep
    # reports it as a survivor, and it is an equivalent mutant rather than a
    # gap in the tests -- no test can kill it.
    if isinstance(value, bool):
        return value

    text = str(value).strip().lower()
    if text in ('on', 'true', 'yes', '1'):
        return True
    if text in ('off', 'false', 'no', '0'):
        return False
    raise ConfigError(
        f"shared_networks[{entry_name!r}]: {key!r} must be on or off, "
        f"got {value!r}"
    )


def _needs_sudo() -> bool:
    """Whether the privileged bridge commands have to go through ``sudo``.

    Creating a bridge, ``ip link set``, the ``iptables`` accept rules and the
    ``bridge-nf-call-iptables`` sysctl all require CAP_NET_ADMIN — in practice,
    root. **No group membership grants it.** That is the whole point of
    deciding it here rather than taking it from the caller: a user in the
    ``libvirt`` group, for whom ``provider.libvirt.use_sudo: false`` is the
    *correct* setting because virsh genuinely needs no sudo for them, still
    gets ``RTNETLINK answers: Operation not permitted`` from ``ip link add``.
    The two settings share a word and mean different things, and threading the
    provider's flag in here (#164 FBN-12) broke every project that set it —
    bridges could no longer be brought up at all.

    So this is not a configuration question but a fact about the process:
    prefix ``sudo`` unless we are already root. When we are root the prefix is
    not merely redundant, it is a liability — ``sudo`` is frequently absent
    from a container image, which is the case FBN-12 was actually right about.

    Two things this does not detect, both failing loudly on the first command
    rather than being guessed at:

    - a *non-root* process that holds CAP_NET_ADMIN directly (``setcap
      cap_net_admin+ep`` on ``ip``, or a container granted the capability) does
      not need sudo, but still gets the prefix.
    - a *root* process whose bounding set excludes CAP_NET_ADMIN (a container
      without ``--cap-add=NET_ADMIN``) is refused anyway, and no prefix would
      help: sudo cannot grant a capability the bounding set excludes.
    """
    return os.geteuid() != 0


def _sudo_prefix(needs_sudo: bool) -> str:
    """``"sudo "`` when the process is not already root, else nothing."""
    return "sudo " if needs_sudo else ""


def _bridge_exists(name: str) -> bool:
    """Whether *name* exists as a link on the host.

    Deliberately not sudo'd: ``ip link show`` reads netlink, which is
    world-readable, so requiring root here would break a host that runs
    boxman unprivileged for read-only work. ``iptables -L`` below is the
    probe that genuinely needs it.
    """
    result = run(f"ip link show dev {shlex.quote(name)}", warn=True, hide=True)
    return result.ok


def _run_sudo(cmd: str, needs_sudo: bool = True) -> None:
    """Run a root-required command, raising on failure."""
    run(f"{_sudo_prefix(needs_sudo)}{cmd}", hide=True)


def _set_sysfs(path: str, value: str, needs_sudo: bool = True) -> None:
    """Write *value* to *path* under sysfs / procfs via ``tee``."""
    run(f"echo {value} | {_sudo_prefix(needs_sudo)}tee {path}", hide=True)


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
    return (f"-i {shlex.quote(bridge)} -o {shlex.quote(bridge)} "
            f"-m physdev --physdev-is-bridged -j ACCEPT")


def _iptables_chain_exists(chain: str, needs_sudo: bool = True) -> bool:
    return run(f"{_sudo_prefix(needs_sudo)}iptables -t filter -n -L {chain}",
               warn=True, hide=True).ok


def _ensure_iptables_rule(chain: str, body: str, needs_sudo: bool = True) -> None:
    """Idempotently insert ``body`` at the top of filter table *chain*.

    Uses ``-C`` (check) before ``-I`` (insert at position 1) so repeated
    ``ensure()`` calls don't stack duplicate rules (spike scenario 7).
    """
    if run(f"{_sudo_prefix(needs_sudo)}iptables -t filter -C {chain} {body}",
           warn=True, hide=True).ok:
        return  # already present
    _run_sudo(f"iptables -t filter -I {chain} 1 {body}", needs_sudo=needs_sudo)


def _ensure_scoped_accept(bridge: str, needs_sudo: bool = True) -> None:
    """Allow bridged lab frames on *bridge* via scoped per-bridge rules (D8).

    Inserts into ``FORWARD`` (works without docker) and, when the
    ``DOCKER-USER`` chain exists (docker host), there too — that copy survives
    a docker daemon restart (docker recreates but does not flush DOCKER-USER),
    which the spike confirmed.

    Known gap: only the IPv4 ``iptables`` filter table is covered. Hosts
    with a restrictive *IPv6* FORWARD policy need the analogous
    ``ip6tables`` accept rule, which is not installed yet — IPv6 lab frames
    forwarded between bridge ports may be dropped on such hosts.
    """
    body = _scoped_rule_body(bridge)
    _ensure_iptables_rule("FORWARD", body, needs_sudo=needs_sudo)
    if _iptables_chain_exists("DOCKER-USER", needs_sudo=needs_sudo):
        _ensure_iptables_rule("DOCKER-USER", body, needs_sudo=needs_sudo)


def ensure(shared_networks: dict[str, dict[str, Any]] | None) -> None:
    """Ensure every bridge declared in *shared_networks* exists and is up.

    Idempotent for a given declaration, and safe to call repeatedly.

    **API note.** This took a ``use_sudo`` keyword between #164 FBN-12 and this
    change. It is gone rather than deprecated: its only caller passed the
    libvirt provider's flag, which answers a different question and disabled
    sudo for callers who needed it (see :func:`_needs_sudo`). Privilege is
    decided here now, so there is no correct value a caller could pass.

    Across projects it is narrower than that. Bridge names are global and not
    namespaced, and this re-writes whatever settings an entry declares onto
    whichever bridge the name resolves to, so two projects declaring the same
    bridge differently get last-run-wins. What *is* guaranteed across projects
    is that boxman never tears a shared bridge down; agreeing on the settings
    is the callers' problem. Omitting a key is how an entry stays out of a
    co-tenant's way -- ``stp: false`` is an opinion, no ``stp`` key at all is
    not. ``disable_netfilter`` escapes even that, being a host-global sysctl
    nothing here restores.

    Each entry is a dict with:

    - ``bridge`` (str, required): the Linux bridge name on the host. Must
      match ``^[a-zA-Z0-9_.-]{1,15}$`` (kernel IFNAMSIZ limit);
      :class:`ConfigError` is raised otherwise.
    - ``mtu`` (int, optional): MTU applied to the bridge at ensure time
      (``ip link set dev <br> mtu <n>``). Bridges default to 1500 while
      containerlab veth links default to 9500, so set this (e.g. 9500) on
      bridges that carry jumbo lab traffic to avoid a silent blackhole.
    - ``stp`` (bool, or an ``on``/``off`` spelling, optional): enable STP on
      the bridge. Applied only when
      declared. An entry that omits it leaves the current setting alone on a
      bridge that already exists, and gets STP off on one boxman creates --
      because the name is shared, so writing a default on every run would
      clobber a co-tenant project's explicit choice.
    - ``disable_netfilter`` (bool, or an ``on``/``off`` spelling; default
      **False** when the key is absent): when False (the
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

    # Derived from the process rather than taken from configuration --
    # see _needs_sudo(). Resolved once so a single run cannot be
    # internally inconsistent.
    needs_sudo = _needs_sudo()

    globally_disabled: list[str] = []
    for entry_name, entry in shared_networks.items():
        bridge = entry.get("bridge")
        if not bridge:
            raise ConfigError(
                f"shared_networks[{entry_name!r}] missing required 'bridge' key"
            )
        _validate_bridge_name(bridge, entry_name)

        mtu = entry["mtu"] if "mtu" in entry else None
        if "mtu" in entry and (
                not isinstance(mtu, int) or isinstance(mtu, bool) or mtu <= 0):
            raise ConfigError(
                f"shared_networks[{entry_name!r}]: 'mtu' must be a positive "
                f"integer, got {mtu!r}"
            )

        # Normalised before anything is touched, so a typo cannot leave a
        # freshly created, half-configured bridge behind.
        #
        # Membership rather than ``.get()``: those collapse an explicit
        # ``stp:`` carrying no value into "absent", so a config mistake would
        # silently mean "leave the setting alone" instead of being reported.
        stp_enabled = (_normalise_bool(entry["stp"], entry_name, 'stp')
                       if "stp" in entry else None)
        disable_netfilter = (
            _normalise_bool(entry["disable_netfilter"], entry_name,
                            'disable_netfilter')
            if "disable_netfilter" in entry else False)

        qbridge = shlex.quote(bridge)

        created = not _bridge_exists(bridge)
        if created:
            log.info(f"creating shared bridge {bridge!r}")
            _run_sudo(f"ip link add name {qbridge} type bridge",
                      needs_sudo=needs_sudo)
        else:
            log.info(f"shared bridge {bridge!r} already present")

        _run_sudo(f"ip link set dev {qbridge} up", needs_sudo=needs_sudo)

        if mtu is not None:
            _run_sudo(f"ip link set dev {qbridge} mtu {mtu}", needs_sudo=needs_sudo)

        # Shared bridge names are global and not namespaced, so writing the
        # default on every run is not a no-op: a project that never mentions
        # `stp` would switch it off for every other project sharing the
        # bridge. Write it only when this project has an opinion -- or when
        # boxman just created the bridge and it needs a defined initial
        # state. `mtu` above is guarded for the same reason.
        if stp_enabled is not None:
            _run_sudo(f"ip link set dev {qbridge} type bridge stp_state "
                      f"{1 if stp_enabled else 0}", needs_sudo=needs_sudo)
        elif created:
            _run_sudo(f"ip link set dev {qbridge} type bridge stp_state 0",
                      needs_sudo=needs_sudo)

        # Decision D8: default to scoped per-bridge accept rules; the
        # host-global sysctl disable is an explicit opt-in.
        if disable_netfilter:
            globally_disabled.append(bridge)
        else:
            _ensure_scoped_accept(bridge, needs_sudo=needs_sudo)

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
            _set_sysfs(str(nf_path), "0", needs_sudo=needs_sudo)
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
