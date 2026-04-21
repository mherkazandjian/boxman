"""
Preset cloud-init user-data, meta-data, and network-config templates.

Extracted from ``cloudinit.py`` in Phase 2.7 of the review plan
(see /home/mher/.claude/plans/) so the bulk of the orchestration class
stays smaller and these presets can be imported directly (e.g. by tests)
without pulling in the whole ``CloudInitTemplate`` class.

Also houses :func:`hash_password`, the SHA-512 ``crypt`` wrapper used by
``${hash:plaintext}`` placeholder substitution. Keeping it here makes
the dependency on the deprecated :mod:`crypt` module visible in one
place — Phase 2.8 will replace ``crypt`` with a ``passlib`` equivalent
before Python 3.13 drops it from stdlib.
"""

from __future__ import annotations

import crypt  # deprecated in 3.13 — replace with passlib in a later pass
import warnings

# Silence the 3.12 DeprecationWarning at import time; boxman still supports
# 3.10 where crypt is the standard path. The replacement work is tracked
# in PROPOSALS.md.
warnings.filterwarnings(
    "ignore",
    message=r"'crypt' is deprecated.*",
    category=DeprecationWarning,
)


DEFAULT_META_DATA = """\
instance-id: {instance_id}
local-hostname: {hostname}
"""


DEFAULT_USER_DATA = """\
#cloud-config
hostname: {hostname}
manage_etc_hosts: true

ssh_pwauth: true
chpasswd:
  expire: false
  users:
    - name: ubuntu
      password: ubuntu
      type: text

package_update: false

write_files:
  - path: /etc/boxman-template-marker
    permissions: "0644"
    content: |
      created by boxman cloud-init template provisioning
  - path: /etc/systemd/system/systemd-networkd-wait-online.service.d/override.conf
    permissions: "0644"
    content: |
      [Service]
      ExecStart=
      ExecStart=/usr/lib/systemd/systemd-networkd-wait-online --any

# Remove any file that disables cloud-init networking, then bring up interfaces
runcmd:
  - rm -f /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
  - rm -f /etc/cloud/cloud.cfg.d/subiquity-disable-cloudinit-networking.cfg
  - systemctl daemon-reload
  - netplan generate || true
  - netplan apply || true
  - dhclient -v || true
  - [ sh, -c, "echo template created at $(date -Is) >> /var/log/boxman-cloudinit.log" ]
"""


DEFAULT_NETWORK_CONFIG = """\
version: 2
ethernets:
  all-en:
    match:
      name: "en*"
    dhcp4: true
  all-eth:
    match:
      name: "eth*"
    dhcp4: true
"""


def hash_password(plain_password: str) -> str:
    """
    Hash *plain_password* with SHA-512 ``crypt`` for use in cloud-init's
    ``chpasswd`` / ``passwd`` fields.

    A fresh random salt is generated on every call, so the same plaintext
    hashes differently each time. That is intended — it prevents the
    hash from revealing identical reused passwords.
    """
    salt = crypt.mksalt(crypt.METHOD_SHA512)
    return crypt.crypt(plain_password, salt)
