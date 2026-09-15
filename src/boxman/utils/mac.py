"""
One spelling for a MAC address.

libvirt accepts a group written with a single digit — ``52:54:0:c:1:1`` parses
— and stores the zero-padded form. So two configuration entries can name the
same address in two spellings, and anything that compares them as strings sees
two addresses: a uniqueness check passes, and a ``dhcp.hosts`` reservation
silently fails to match the NIC it was written for (#171).

Canonicalising at every comparison point is what keeps those two honest.
"""

from __future__ import annotations

import re

#: Six colon-separated hex groups, each one or two digits.
#:
#: Deliberately looser than the canonical form, because libvirt itself is: this
#: exists to reject the dash-separated and run-together spellings early, not to
#: impose zero padding that libvirt does not require. Tightening it would
#: refuse configurations that work today.
MAC_RE = re.compile(r'[0-9a-f]{1,2}(:[0-9a-f]{1,2}){5}')


def canonical_mac(value) -> str:
    """
    Zero-pad and lowercase a mac, the way libvirt stores one.

    Anything that is not six colon-separated groups is lowercased and returned
    unchanged, so an unexpected value still reaches libvirt and earns libvirt's
    own error instead of being silently mangled here.
    """
    groups = str(value).split(':')
    if len(groups) != 6:
        return str(value).lower()
    try:
        return ':'.join(f"{int(group, 16):02x}" for group in groups)
    except ValueError:
        return str(value).lower()


def is_mac_like(value) -> bool:
    """Whether *value* is six colon-separated hex groups (see :data:`MAC_RE`)."""
    return bool(MAC_RE.fullmatch(str(value).lower()))
