#!/bin/bash
# Proxmox VE first-boot hook, baked into the auto-install ISO by
# prepare-iso.sh (`--on-first-boot`). Runs once as root on the freshly
# installed node, after the network and the PVE services are up.
#
#   1. repositories: disable the enterprise ones, enable pve-no-subscription
#   2. MTU 1450 on the lab NIC + vmbr0, verified live (the L2 rides a VXLAN
#      over a 1500 VLAN)
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
#
# The lab's L2 rides a VXLAN over a 1500-byte VLAN, so 1450 is what fits. The
# marker this hook ends with is what wait-first-boot.sh waits for and what the
# README says certifies the MTU -- so the live value is read back here rather
# than assumed from a reload that was tolerated, skipped, or simply believed.
LAB_MTU=1450

port=$(awk '/^[[:space:]]*bridge-ports/ {print $2; exit}' /etc/network/interfaces || true)
if [ -z "$port" ] || [ "$port" = none ]; then
    echo "ERROR: no bridge port found in /etc/network/interfaces; cannot set"
    echo "       the lab MTU on vmbr0's port. Not writing the readiness marker."
    exit 1
fi

# Per interface, and only within its own stanza. A file-wide `grep -q mtu 1450`
# is satisfied by a comment, or by one interface already carrying it while the
# other is untouched -- and then skips the edit AND the reload for both.
stanza_has_mtu() {           # <iface>
    awk -v want_iface="$1" -v want_mtu="$LAB_MTU" '
        $1 == "iface" && $2 == want_iface { inside = 1; next }
        $1 == "auto" || $1 == "iface" || $1 == "source" { inside = 0 }
        inside && $1 == "mtu" && $2 == want_mtu { found = 1 }
        END { exit(found ? 0 : 1) }
    ' /etc/network/interfaces
}

for iface in vmbr0 "$port"; do
    if ! stanza_has_mtu "$iface"; then
        sed -i -E "/^iface $iface inet/a\\        mtu $LAB_MTU" /etc/network/interfaces
    fi
done

# Deliberately tolerated: ifupdown2 refuses a reload on a node whose interfaces
# already match, and that is not a failure. Its status is not the question --
# the live MTU below is, and that is checked either way.
ifreload -a || echo "note: ifreload exited non-zero; verifying the live MTU anyway"

# The check that makes the marker mean what it says. Runs whether the reload
# succeeded, failed, or was skipped because the file already said 1450.
for iface in vmbr0 "$port"; do
    live=$( { ip -o link show "$iface" || true; } \
            | awk '{ for (i = 1; i < NF; i++) if ($i == "mtu") print $(i + 1) }' )
    if [ "$live" != "$LAB_MTU" ]; then
        echo "ERROR: $iface has MTU ${live:-unknown}, expected $LAB_MTU."
        echo "       The VXLAN carries $LAB_MTU-byte frames; a node left at 1500"
        echo "       loses large packets silently, and Ceph on top of it fails in"
        echo "       ways that look like anything but an MTU problem."
        echo "       Not writing the readiness marker."
        exit 1
    fi
    echo "ok: $iface mtu $live"
done

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
