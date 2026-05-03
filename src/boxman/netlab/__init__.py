"""Network-lab subsystem.

Sibling to the libvirt provider. Owns host-level Linux bridges that are
shared between boxman libvirt VMs and external lab tools (containerlab,
EVE-NG, GNS3). The bridges form a single L2 broadcast domain so a
boxman Linux VM and an emulated switch can exchange LLDP, DHCP,
dot1q-tagged frames, and the rest.
"""

from boxman.netlab.containerlab import (
    ContainerlabManager,
    ContainerlabNotInstalled,
)
from boxman.netlab.shared_bridges import ensure, is_shared_bridge, resolve_bridge

__all__ = [
    "ContainerlabManager",
    "ContainerlabNotInstalled",
    "ensure",
    "is_shared_bridge",
    "resolve_bridge",
]
