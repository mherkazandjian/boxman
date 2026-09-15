#!/bin/bash
# Proxmox VE first-boot hook, baked into the auto-install ISO by
# prepare-iso.sh (`--on-first-boot`). Runs once as root on the freshly
# installed node, after the network and the PVE services are up.
#
#   1. repositories: disable the enterprise ones, enable pve-no-subscription
#   2. MTU 1450 on the lab NIC + vmbr0 (the L2 rides a VXLAN over a 1500 VLAN)
#   3. dist-upgrade, so the ISO's PVE matches the Ceph the repo will install
#   4. qemu-guest-agent, so boxman can discover the node's IP via the agent
#   5. a marker the orchestration scripts wait for before touching the node
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

# 3. bring PVE in step with the repo before Ceph is installed from it.
#
# The ISO's packages are frozen when it is built, but `pveceph install
# --repository no-subscription` takes whatever Ceph is current, so an
# un-upgraded node pairs ISO-era PVE with a newer Ceph. On 2026-09-15 that
# combination was fatal: Ceph 20.2.4 issues aes256k keys (32 bytes, base64
# ending in ONE '='), while libpve-storage-perl 9.1.5's rbd keyring check
# demanded '==' -- so PVE rejected the keyring PVE itself had just written,
# every rbd storage stayed inactive, and `make demo` failed against a
# HEALTH_OK cluster. Fixed upstream in libpve-storage-perl 9.1.10; the nodes
# were on 9.1.5 because nothing ever upgraded them. The 2026-09-06 build only
# worked because the ISO's PVE and the repo's Ceph happened to still be in
# step -- luck with a nine-day shelf life.
#
# Deliberately no `|| true`: under `set -e` a failed upgrade leaves the marker
# below unwritten, and wait-first-boot.sh then fails pointing at this log,
# which is what should happen. It also stages a kernel the node will not run
# until it reboots; fine for a lab, noted in the README.
apt-get update -qq
# --force-conf*: step 1 above edits /etc/apt/sources.list.d/pve-enterprise.sources,
# which IS a dpkg conffile. If an upgrade also changes its packaged contents dpkg
# prompts -- and DEBIAN_FRONTEND=noninteractive does not answer a conffile prompt.
# This hook runs as a service with null stdin, so the prompt fails with EOF, the
# upgrade aborts, and the marker below is never written. confold keeps our edit;
# confdef takes the package default wherever one is defined.
# (/etc/network/interfaces, edited in step 2, is NOT a conffile and is preserved
# by ifupdown2 regardless.)
apt-get -y -qq \
    -o Dpkg::Options::=--force-confdef \
    -o Dpkg::Options::=--force-confold \
    dist-upgrade

# 4. guest agent
apt-get install -y -qq qemu-guest-agent
systemctl enable --now qemu-guest-agent

# 5. marker
date -Is > /var/lib/pve-lab/first-boot.done
echo "== done $(date -Is)"
