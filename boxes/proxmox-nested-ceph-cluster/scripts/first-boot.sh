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

if [ ! -d /sys/class/net/vmbr0/brif ]; then
    echo "ERROR: vmbr0 is not a bridge on this node. The lab's L2 rides a"
    echo "       VXLAN and every node reaches it through vmbr0."
    echo "       Not writing the readiness marker."
    exit 1
fi

# Reading and writing the interfaces file use the same stanza-aware
# interpretation. ifupdown2 accepts any whitespace in a header and takes the
# FIRST mtu in a stanza, so a wrong existing value has to be replaced rather
# than followed by ours -- and interface names are compared as strings, never
# interpolated into a regex, because `ens18.100` as one also matches
# `ens18x100`, and this runs as root and reloads what it just wrote.
AWK_STANZA='
function closes(word) {
    return word == "auto" || word == "iface" || word == "source" ||
           word == "source-directory" || word == "allow-hotplug" ||
           word == "allow-auto"
}
'

stanza_mtu() {               # <iface> -> its effective mtu, or nothing
    awk -v want="$1" "$AWK_STANZA"'
        $1 == "iface" && $2 == want { inside = 1; next }
        inside && closes($1)        { inside = 0 }
        inside && $1 == "mtu" && !found { print $2; found = 1 }
    ' /etc/network/interfaces
}

set_stanza_mtu() {           # <iface>: exactly one mtu, first in the stanza
    tmp=$(mktemp /etc/network/interfaces.pve-lab.XXXXXX)
    awk -v want="$1" -v mtu="$LAB_MTU" "$AWK_STANZA"'
        $1 == "iface" && $2 == want {
            print; print "        mtu " mtu; inside = 1; next
        }
        inside && closes($1) { inside = 0 }
        inside && $1 == "mtu" { next }
        { print }
    ' /etc/network/interfaces > "$tmp"
    cat "$tmp" > /etc/network/interfaces
    rm -f "$tmp"
}

# vmbr0's ports come from vmbr0's own stanza, every token of it. A file-wide
# `grep bridge-ports` took the second token of whichever such line came first,
# so another bridge declared above substituted its port and a two-port vmbr0
# had only the first one looked at. Reading the kernel's bridge membership
# instead would be worse: it also lists the tap devices of running guests,
# which have no stanza and are nobody's uplink.
ports=$(awk -v want=vmbr0 "$AWK_STANZA"'
    $1 == "iface" && $2 == want { inside = 1; next }
    inside && closes($1)        { inside = 0 }
    inside && $1 == "bridge-ports" {
        for (i = 2; i <= NF; i++) if ($i != "none") print $i
    }
' /etc/network/interfaces)
if [ -z "$ports" ]; then
    echo "ERROR: vmbr0 declares no bridge-ports; nothing carries the lab L2."
    echo "       Not writing the readiness marker."
    exit 1
fi

# ...and each declared port is really attached to it. A port that is
# configured but not enslaved leaves the bridge without an uplink, which looks
# like a working node until the first packet has to leave it.
for iface in $ports; do
    if [ ! -e "/sys/class/net/vmbr0/brif/$iface" ]; then
        echo "ERROR: $iface is declared as a port of vmbr0 but is not attached"
        echo "       to it. Not writing the readiness marker."
        exit 1
    fi
done

for iface in vmbr0 $ports; do
    [ "$(stanza_mtu "$iface")" = "$LAB_MTU" ] || set_stanza_mtu "$iface"
done

# The persisted result, checked rather than assumed: an interface whose stanza
# lives in a `source`d file is not in this one to edit, and silently leaving it
# at 1500 is exactly the outcome this step exists to prevent.
for iface in vmbr0 $ports; do
    if [ "$(stanza_mtu "$iface")" != "$LAB_MTU" ]; then
        echo "ERROR: $iface has no stanza in /etc/network/interfaces to carry"
        echo "       mtu $LAB_MTU -- a sourced file, perhaps. The MTU would not"
        echo "       survive the next boot. Not writing the readiness marker."
        exit 1
    fi
done

# Deliberately tolerated: ifupdown2 refuses a reload on a node whose interfaces
# already match, and that is not a failure. Its status is not the question --
# the live MTU below is, and that is checked either way.
ifreload -a || echo "note: ifreload exited non-zero; verifying the live MTU anyway"

# The check that makes the marker mean what it says. Runs whether the reload
# succeeded, failed, or was skipped because the file already said 1450.
for iface in vmbr0 $ports; do
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
