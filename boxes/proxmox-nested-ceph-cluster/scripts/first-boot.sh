#!/bin/bash
# Proxmox VE first-boot hook, baked into the auto-install ISO by
# prepare-iso.sh (`--on-first-boot`). Runs once as root on the freshly
# installed node, after the network and the PVE services are up.
#
#   1. repositories: disable the enterprise ones, enable pve-no-subscription
#   2. MTU 1450 on the lab NIC + vmbr0 (the L2 rides a VXLAN over a 1500 VLAN)
#   3. qemu-guest-agent, so boxman can discover the node's IP via the agent
#   4. a marker the orchestration scripts wait for before touching the node
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
mkdir -p /var/lib/pve-lab
exec >>/var/log/pve-lab-first-boot.log 2>&1
echo "== pve-lab first boot: $(hostname -f) $(date -Is)"

# 1. repositories (deb822; PVE 9 / Debian trixie)
for f in /etc/apt/sources.list.d/pve-enterprise.sources /etc/apt/sources.list.d/ceph.sources; do
    [ -f "$f" ] || continue
    sed -i '/^Enabled:/d' "$f"
    printf 'Enabled: false\n' >> "$f"
done
cat > /etc/apt/sources.list.d/proxmox.sources <<'EOF'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF

# 2. MTU 1450 on vmbr0 and its port (ifupdown2 re-applies it on every boot)
port=$(awk '/^[[:space:]]*bridge-ports/ {print $2; exit}' /etc/network/interfaces || true)
if ! grep -q 'mtu 1450' /etc/network/interfaces; then
    sed -i -E "/^iface (vmbr0|${port:-__none__}) inet/a\\        mtu 1450" /etc/network/interfaces
    ifreload -a || true
fi
ip link show vmbr0 | head -1

# 3. guest agent
apt-get update -qq
apt-get install -y -qq qemu-guest-agent
systemctl enable --now qemu-guest-agent

# 4. marker
date -Is > /var/lib/pve-lab/first-boot.done
echo "== done $(date -Is)"
