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
)


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
    dhcp_range = None
    if network.dhcp_range_start and network.dhcp_range_end:
        dhcp_range = {'start': network.dhcp_range_start,
                      'end': network.dhcp_range_end}

    return {
        'mode': network.forward_mode,
        # the *configured* name, not the one in use: when the configuration
        # does not pin one boxman assigned it and libvirt is authoritative, so
        # leaving this None is what keeps an auto-assigned virbrX from reading
        # as drift
        'bridge_name': network.pinned_bridge_name,
        'bridge_stp': str(network.bridge_stp),
        'bridge_delay': str(network.bridge_delay),
        'mac': network.mac_address.lower() if network.mac_address else None,
        'ip_address': network.ip_address,
        'netmask': network.netmask,
        'dhcp_range': dhcp_range,
        'dhcp_hosts': list(network.dhcp_hosts),
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

        # boxman picks the bridge name unless the configuration pinned one, so
        # only a pinned name can be said to have drifted
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
