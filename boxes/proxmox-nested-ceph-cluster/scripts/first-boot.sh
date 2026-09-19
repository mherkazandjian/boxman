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
IFACES_FILE=/etc/network/interfaces

if [ ! -d /sys/class/net/vmbr0/brif ]; then
    echo "ERROR: vmbr0 is not a bridge on this node. The lab's L2 rides a"
    echo "       VXLAN and every node reaches it through vmbr0."
    echo "       Not writing the readiness marker."
    exit 1
fi

# What this hook understands is the plain stanza syntax the Proxmox installer
# writes: one directive per line, stanzas opened by a keyword, literal
# interface names. ifupdown2's full grammar is larger than that -- line
# continuations that survive blank lines, CRLF, aliases like `ens18:0` and
# ranges like `ens[18-19]` that name the same interface as a later stanza,
# mapping stanzas -- and it belongs to a Python parser. Approximating enough
# of it in awk produced a file that was misread a different way each round,
# twice destructively. So anything outside the plain shapes is refused, here,
# before a byte is rewritten.
unsupported=$(awk '
    /\r/            { print "CRLF line endings"; exit }
    # any backslash, not just one ending a line: ifupdown2 treats it as a
    # separator wherever it appears, so `source\ PATH` is a source directive
    # and `iface\ ens18` a header, both invisible to a reader splitting on
    # whitespace
    /\\/            { print "backslashes"; exit }
    # form feed, vertical tab, a non-breaking space: ifupdown2 separates on
    # them, awk does not, so they hide a header or a boundary in plain sight
    /[^\t -~]/      { print "control or non-ASCII characters"; exit }
    $1 == "mapping" { print "a mapping stanza"; exit }
    $1 == "iface" && $2 !~ /^[A-Za-z0-9_.@-]+$/ {
        print "an interface alias or range (" $2 ")"; exit
    }
' "$IFACES_FILE")
if [ -n "$unsupported" ]; then
    echo "ERROR: $IFACES_FILE uses $unsupported, which this hook does not"
    echo "       model. It sets mtu $LAB_MTU on vmbr0 and its ports, and it"
    echo "       will not guess at syntax it cannot read back exactly."
    echo "       Set the MTU by hand, or simplify the file, and re-run."
    echo "       Not writing the readiness marker."
    exit 1
fi

# A trailing `source` glob is what the installer writes, and harmless in
# itself -- but an included file can carry a second `iface vmbr0` stanza, and
# ifupdown2 concatenates the bridge-ports of every definition rather than
# taking the first. That would add a port this hook never sees, never
# configures and never verifies, while everything here passed. So the includes
# are read just far enough to refuse that. (Reading the kernel's attachments
# instead would pick up the tap devices of running guests.)
check_include() {            # <path>: an include this hook can rule out
    if ! LC_ALL=C awk '/\\/ || /[^\t -~]/ { bad = 1 } END { exit(bad) }' "$1"; then
        echo "ERROR: the included file $1 uses a backslash or a non-ASCII"
        echo "       character, so this hook cannot rule out a second vmbr0"
        echo "       definition hiding in it."
        echo "       Not writing the readiness marker."
        exit 1
    fi
    if awk '$1 == "source" || $1 == "source-directory" { found = 1 }
            END { exit(found ? 0 : 1) }' "$1"; then
        echo "ERROR: $1 sources further files. ifupdown2 follows those"
        echo "       recursively; this hook reads one level to rule out a"
        echo "       second vmbr0 definition and will not chase a chain."
        echo "       Not writing the readiness marker."
        exit 1
    fi
    if awk '$1 == "iface" && $2 !~ /^[A-Za-z0-9_.@-]+$/ { found = 1 }
            END { exit(found ? 0 : 1) }' "$1"; then
        echo "ERROR: $1 uses an interface alias or range, which ifupdown2"
        echo "       normalises -- so it can name vmbr0 without spelling it."
        echo "       Not writing the readiness marker."
        exit 1
    fi
    if awk '$1 == "iface" && $2 == "vmbr0" { found = 1 }
            END { exit(found ? 0 : 1) }' "$1"; then
        echo "ERROR: $1 defines vmbr0 as well, and ifupdown2 merges the"
        echo "       bridge-ports of every definition -- so the port list"
        echo "       configured and checked here would be a subset of the"
        echo "       one the node actually brings up."
        echo "       Not writing the readiness marker."
        exit 1
    fi
}

# A trailing `source` glob is what the installer writes, and harmless in
# itself -- but an included file can carry a second `iface vmbr0` stanza, and
# ifupdown2 concatenates the bridge-ports of every definition rather than
# taking the first. That would add a port this hook never sees, never
# configures and never verifies, while everything here passed. So each include
# is read far enough to refuse that. (Reading the kernel's attachments instead
# would pick up the tap devices of running guests.)
#
# The enumeration has to match the parser's: it resolves relative paths
# against the file that sourced them, and lists a source-directory with
# os.listdir(), which includes dotfiles. `dotglob` covers the second; reading
# one pattern per line and globbing once covers a matched filename containing
# a space.
shopt -s nullglob dotglob
while IFS= read -r pattern; do
    case $pattern in
        /*) ;;
        *)  pattern="$(dirname "$IFACES_FILE")/$pattern" ;;
    esac
    for inc in $pattern; do
        [ -f "$inc" ] && check_include "$inc"
    done
done < <(awk '
    $1 == "source"           { for (i = 2; i <= NF; i++) print $i }
    $1 == "source-directory" { for (i = 2; i <= NF; i++) print $i "/*" }
' "$IFACES_FILE")
shopt -u nullglob dotglob

# With those refused, a stanza is plain lines: it opens at auto, iface, vlan,
# source, source-directory or any allow-* keyword, names are compared as
# strings -- `ens18.100` as a regex also matches `ens18x100` -- and ifupdown2
# takes the FIRST mtu in a stanza, so a wrong one is replaced, not joined.
AWK_STANZA='
function closes(word) {
    return word == "auto" || word == "iface" || word == "vlan" ||
           word == "source" || word == "source-directory" ||
           word ~ /^allow-/
}
'

stanza_mtu() {               # <iface> -> its effective mtu, or nothing
    awk -v want="$1" "$AWK_STANZA"'
        $1 == "iface" && $2 == want     { inside = 1; next }
        closes($1)                      { inside = 0 }
        inside && $1 == "mtu" && !found { print $2; found = 1 }
    ' "$IFACES_FILE"
}

set_stanza_mtu() {           # <iface>: exactly one mtu, first in the stanza
    tmp=$(mktemp "$IFACES_FILE.pve-lab.XXXXXX")
    awk -v want="$1" -v mtu="$LAB_MTU" "$AWK_STANZA"'
        $1 == "iface" && $2 == want {
            print; print "        mtu " mtu; inside = 1; next
        }
        closes($1)            { inside = 0 }
        inside && $1 == "mtu" { next }
        { print }
    ' "$IFACES_FILE" > "$tmp"
    cat "$tmp" > "$IFACES_FILE"
    rm -f "$tmp"
}

# A `source` ahead of an interface's own stanza can define it earlier, and the
# earlier definition's MTU is the one that counts -- so editing the stanza we
# can see would leave the node at 1500 with every check here passing. The
# stock installer writes its stanzas first and the glob last, which is fine;
# anything else is refused rather than half-understood.
source_precedes() {          # <iface>
    awk -v want="$1" "$AWK_STANZA"'
        $1 == "source" || $1 == "source-directory" { seen = 1 }
        $1 == "iface" && $2 == want { if (seen) found = 1; exit }
        END { exit(found ? 0 : 1) }
    ' "$IFACES_FILE"
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
    # ifupdown2 normalises the underscore spelling to the hyphen one
    inside && ($1 == "bridge-ports" || $1 == "bridge_ports") {
        for (i = 2; i <= NF; i++) if ($i != "none") print $i
    }
' "$IFACES_FILE")
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
    if source_precedes "$iface"; then
        echo "ERROR: $iface's stanza comes after a source directive, so an"
        echo "       included file may define it first and win the MTU. This"
        echo "       hook will not edit around that."
        echo "       Not writing the readiness marker."
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
        echo "ERROR: $iface has no stanza in $IFACES_FILE to carry"
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
