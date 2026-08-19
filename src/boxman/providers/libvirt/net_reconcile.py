"""Diff a network's desired configuration against its live libvirt definition.

libvirt splits network changes into two very different classes, and the whole
point of this module is to tell them apart before anything is touched:

- ``ip-dhcp-host`` and ``ip-dhcp-range`` can be changed on a running network
  with ``virsh net-update ... --live --config``. dnsmasq is reloaded, the
  bridge stays up and no guest notices.
- everything else -- the forward mode, the address, the netmask, the bridge
  attributes, the network mac -- cannot. libvirt answers ``can't update 'ip'
  section of network`` / ``can't update 'bridge' section``. The only way to
  apply those is to destroy and redefine the network, which deletes the bridge
  and leaves every attached guest with a dead nic: libvirt does **not**
  reconnect them when the network comes back.

So the diff returns the two classes separately and the caller decides. The
functions here are pure -- they take the desired configuration and the XML
libvirt reports, and return a plan. Nothing in this module talks to virsh.
"""

from __future__ import annotations

import ipaddress
import xml.etree.ElementTree as ET
from typing import Any
from xml.sax.saxutils import escape

#: fields that force a destroy + redefine because libvirt refuses to update
#: them in place. ``bridge_name`` is only compared when the configuration
#: pinned one, otherwise boxman assigned it and libvirt is authoritative
STRUCTURAL_FIELDS = (
    ('mode', 'forward mode'),
    ('ip_address', 'ip address'),
    ('netmask', 'netmask'),
    ('bridge_name', 'bridge name'),
    ('bridge_stp', 'bridge stp'),
    ('bridge_delay', 'bridge delay'),
    ('mac', 'mac address'),
    # libvirt cannot change dnsmasq options on a live network, and leaving
    # them unmodelled meant a network defined before they existed planned as
    # `action: none` -- so it could never be migrated, not even with
    # --recreate-networks, which only permits an already-planned recreate
    ('dhcp_options_trimmed', 'dhcp router/dns suppression'),
)


def _dhcp_options_trimmed(root) -> bool:
    """
    Whether the network tells dnsmasq to omit DHCP options 3 and 6.

    A routed network isolates the host, so the gateway and resolver dnsmasq
    would otherwise advertise -- both the bridge address -- are unreachable by
    construction, and the router option installs a default route at metric 0
    that black-holes the guest. The elements are namespaced, hence matching on
    the local name rather than a fixed prefix.
    """
    values = {element.get('value') for element in root.iter()
              if element.tag.rsplit('}', 1)[-1] == 'option'}
    return 'dhcp-option=3' in values and 'dhcp-option=6' in values


def parse_network_xml(xml_text: str) -> dict[str, Any]:
    """
    Turn the output of ``virsh net-dumpxml`` into a comparable dict.

    Args:
        xml_text: the network XML as libvirt reports it

    Returns:
        A dict with the same shape :func:`desired_state` produces. The uuid is
        deliberately dropped: it is regenerated on every definition and would
        otherwise read as permanent drift.
    """
    root = ET.fromstring(xml_text)

    forward = root.find('forward')
    bridge = root.find('bridge')
    mac = root.find('mac')
    ip = root.find('ip')

    state: dict[str, Any] = {
        'mode': forward.get('mode') if forward is not None else None,
        'bridge_name': bridge.get('name') if bridge is not None else None,
        'bridge_stp': bridge.get('stp') if bridge is not None else None,
        'bridge_delay': bridge.get('delay') if bridge is not None else None,
        'mac': mac.get('address').lower() if mac is not None and mac.get('address') else None,
        'ip_address': None,
        'netmask': None,
        'dhcp_range': None,
        'dhcp_hosts': [],
        'dhcp_options_trimmed': _dhcp_options_trimmed(root),
    }

    if ip is None:
        return state

    state['ip_address'] = ip.get('address')

    # libvirt accepts either spelling and echoes back whichever was given, so
    # normalise a prefix into the netmask boxman's schema uses
    if ip.get('netmask'):
        state['netmask'] = ip.get('netmask')
    elif ip.get('prefix'):
        state['netmask'] = str(ipaddress.IPv4Network(
            f"0.0.0.0/{ip.get('prefix')}").netmask)

    dhcp = ip.find('dhcp')
    if dhcp is None:
        return state

    dhcp_range = dhcp.find('range')
    if dhcp_range is not None:
        state['dhcp_range'] = {'start': dhcp_range.get('start'),
                               'end': dhcp_range.get('end')}

    for host in dhcp.findall('host'):
        entry = {'mac': (host.get('mac') or '').lower(), 'ip': host.get('ip')}
        if host.get('name'):
            entry['name'] = host.get('name')
        state['dhcp_hosts'].append(entry)

    return state


def desired_state(network) -> dict[str, Any]:
    """
    Read the desired state off an already-constructed :class:`Network`.

    The ``Network`` constructor is where defaults are applied and where the
    reservations are validated and normalised, so going through it keeps the
    two sides of the diff honest rather than re-implementing that here.

    Args:
        network: a ``Network`` built from the configuration

    Returns:
        A dict shaped like :func:`parse_network_xml`.
    """
    if network.forward_mode == 'bridge':
        return {
            'mode': 'bridge',
            'bridge_name': network.bridge_name,
            'bridge_stp': None,
            'bridge_delay': None,
            'mac': None,
            'ip_address': None,
            'netmask': None,
            'dhcp_range': None,
            'dhcp_hosts': [],
            'dhcp_options_trimmed': False,
        }

    dhcp_range = None
    if network.dhcp_range_start and network.dhcp_range_end:
        dhcp_range = {'start': network.dhcp_range_start,
                      'end': network.dhcp_range_end}

    return {
        'mode': network.forward_mode,
        # ``bridge_name`` is the effective desired name: a configured pin, a
        # newly allocated virbrX, or the current libvirt name when reconciling
        # an unpinned existing network.
        'bridge_name': network.bridge_name,
        'bridge_stp': str(network.bridge_stp),
        'bridge_delay': str(network.bridge_delay),
        'mac': network.mac_address.lower() if network.mac_address else None,
        'ip_address': network.ip_address,
        'netmask': network.netmask,
        'dhcp_range': dhcp_range,
        'dhcp_hosts': list(network.dhcp_hosts),
        # mirrors the template: emitted only for a routed network that hands
        # out addresses of its own
        'dhcp_options_trimmed': bool(
            network.forward_mode == 'route'
            and (dhcp_range or network.dhcp_hosts)),
    }


def _host_key(entry: dict[str, Any]) -> str:
    return (entry.get('mac') or '').lower()


def diff_dhcp_hosts(desired: list, actual: list) -> list[tuple[str, dict]]:
    """
    Reduce two reservation lists to the ``virsh net-update`` operations that
    turn *actual* into *desired*.

    Keyed on the mac, because that is what libvirt itself matches on: the same
    mac with a different address is a ``modify``, not a delete plus an add.

    Returns:
        A list of ``(command, entry)`` tuples where command is one of
        ``add-last``, ``modify`` or ``delete``.
    """
    desired_by_mac = {_host_key(entry): entry for entry in desired}
    actual_by_mac = {_host_key(entry): entry for entry in actual}

    deletes: list[tuple[str, dict]] = []
    modifies: list[tuple[str, dict]] = []
    adds: list[tuple[str, dict]] = []

    for mac in actual_by_mac:
        if mac not in desired_by_mac:
            deletes.append(('delete', actual_by_mac[mac]))

    # an address moving between two reservations that both survive cannot be
    # done with a modify: libvirt refuses to hand out an address another entry
    # still holds, so the pair would fail on every run. Those entries are
    # deleted first and added back in their new arrangement instead
    surviving_ips = {
        (entry.get('ip'), _host_key(entry)) for entry in actual
        if _host_key(entry) in desired_by_mac}
    contested = {
        mac for mac, entry in desired_by_mac.items()
        if mac in actual_by_mac
        and any(ip == entry.get('ip') and holder != mac
                for ip, holder in surviving_ips)}

    for mac, entry in desired_by_mac.items():
        if mac not in actual_by_mac:
            adds.append(('add-last', entry))
        elif mac in contested:
            deletes.append(('delete', actual_by_mac[mac]))
            adds.append(('add-last', entry))
        elif entry != actual_by_mac[mac]:
            modifies.append(('modify', entry))

    # delete, then modify, then add. libvirt rejects an entry whose address is
    # still held by another reservation, so every operation that frees an
    # address has to come before the one that claims it -- otherwise handing
    # an address from one mac to another fails on the first run and only
    # converges on the second
    return deletes + modifies + adds


def diff_dhcp_range(desired: dict | None,
                    actual: dict | None) -> list[tuple[str, dict]]:
    """
    Reduce two dhcp ranges to ``virsh net-update`` operations.

    libvirt refuses ``modify`` on this section -- *"dhcp ranges cannot be
    modified, only added or deleted"* -- so a change becomes a delete of the
    old range followed by an add of the new one.
    """
    if desired == actual:
        return []

    ops: list[tuple[str, dict]] = []
    if actual:
        ops.append(('delete', actual))
    if desired:
        ops.append(('add-last', desired))
    return ops


def diff_network(desired: dict[str, Any],
                 actual: dict[str, Any]) -> dict[str, Any]:
    """
    Compare a desired and an actual network state.

    Args:
        desired: from :func:`desired_state`
        actual: from :func:`parse_network_xml`

    Returns:
        A plan dict with:

        - ``action``: ``none``, ``live`` or ``recreate``
        - ``structural``: human-readable descriptions of the changes that
          require a destroy + redefine
        - ``host_ops`` / ``range_ops``: the ``net-update`` operations that can
          be applied to the running network
    """
    structural = []
    for field, label in STRUCTURAL_FIELDS:
        want = desired.get(field)
        have = actual.get(field)

        # A missing desired bridge means effective-name discovery failed. Do
        # not recreate a network based on an unresolved comparison; callers
        # surface the discovery failure separately.
        if field == 'bridge_name' and not want:
            continue

        if want is not None and str(want) != str(have):
            structural.append(f"{label} {have!r} -> {want!r}")

    host_ops = diff_dhcp_hosts(desired.get('dhcp_hosts') or [],
                               actual.get('dhcp_hosts') or [])
    range_ops = diff_dhcp_range(desired.get('dhcp_range'),
                                actual.get('dhcp_range'))

    if structural:
        action = 'recreate'
    elif host_ops or range_ops:
        action = 'live'
    else:
        action = 'none'

    return {
        'action': action,
        'structural': structural,
        'host_ops': host_ops,
        'range_ops': range_ops,
    }


def bridge_ownership_conflict(desired: dict[str, Any],
                              actual: dict[str, Any],
                              *,
                              desired_bridge_is_pinned: bool = True) -> str | None:
    """Explain an unsafe same-name managed/external bridge transition.

    In nat/route mode libvirt owns the Linux bridge and deletes it with the
    network. In bridge mode the host owns it and libvirt deliberately leaves it
    alone. Reusing one bridge name while crossing that ownership boundary
    therefore cannot be reconciled by destroy/redefine: one direction deletes
    the desired prerequisite, the other leaves an interface the new managed
    network cannot claim. ``desired_bridge_is_pinned`` distinguishes that
    second case from an unpinned managed network: during planning its resolved
    name is still the current bridge, but the recreate path reserves a new
    automatic name before removing anything.
    """
    old_mode = actual.get('mode')
    new_mode = desired.get('mode')
    old_bridge = actual.get('bridge_name')
    new_bridge = desired.get('bridge_name')
    managed = {'nat', 'route'}

    if not old_bridge or not new_bridge or old_bridge != new_bridge:
        return None
    if old_mode in managed and new_mode == 'bridge':
        return (
            f"cannot change forward mode {old_mode!r} -> 'bridge' while "
            f"reusing {old_bridge!r}: removing the current libvirt network "
            "would delete that managed bridge; create a distinct host bridge "
            "and set bridge.name to it")
    if (old_mode == 'bridge' and new_mode in managed
            and desired_bridge_is_pinned):
        return (
            f"cannot change forward mode 'bridge' -> {new_mode!r} while "
            f"reusing {old_bridge!r}: bridge mode preserves that host-owned "
            "interface, so the new managed network cannot claim its name; "
            "choose a different bridge.name or omit it for auto-allocation")
    return None


def describe_plan(name: str, plan: dict[str, Any]) -> list[str]:
    """Render a plan as log lines, one per change."""
    lines = []
    for change in plan['structural']:
        lines.append(f"network {name}: {change} (needs recreate)")
    for command, entry in plan['range_ops']:
        verb = 'remove' if command == 'delete' else 'add'
        lines.append(
            f"network {name}: {verb} dhcp range "
            f"{entry['start']}-{entry['end']}")
    for command, entry in plan['host_ops']:
        verb = {'add-last': 'add', 'modify': 'update', 'delete': 'remove'}[command]
        name_part = f" ({entry['name']})" if entry.get('name') else ''
        lines.append(
            f"network {name}: {verb} reservation {entry['mac']} -> "
            f"{entry.get('ip')}{name_part}")
    return lines


def _attr(value: Any) -> str:
    """
    Escape a value for use inside a single-quoted XML attribute.

    The jinja template escapes with ``|e``, and these elements have to do the
    same: a reservation name is free-form configuration text, so a name holding
    an ``&`` or a quote would define happily through the template and then make
    every later ``net-update`` on that network fail to parse.
    """
    return escape(str(value), {"'": '&apos;', '"': '&quot;'})


def host_element(entry: dict[str, Any]) -> str:
    """Render a reservation as the ``<host>`` element ``net-update`` matches on."""
    attrs = f"mac='{_attr(entry['mac'])}'"
    if entry.get('name'):
        attrs += f" name='{_attr(entry['name'])}'"
    if entry.get('ip'):
        attrs += f" ip='{_attr(entry['ip'])}'"
    return f"<host {attrs}/>"


def range_element(entry: dict[str, Any]) -> str:
    """Render a dhcp range as the ``<range>`` element ``net-update`` matches on."""
    return (f"<range start='{_attr(entry['start'])}' "
            f"end='{_attr(entry['end'])}'/>")
