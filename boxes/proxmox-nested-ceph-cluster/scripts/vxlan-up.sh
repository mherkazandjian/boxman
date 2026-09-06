#!/usr/bin/env bash
# Stretch the lab L2 to the peer host with a point-to-point VXLAN. Idempotent:
# safe to re-run after `boxman up`, `boxman destroy` (which deletes hpe1's
# libvirt bridge and orphans the tunnel) or a host reboot (the tunnel is not
# persistent; the firewall rule is).
#
#   hpe2: run BEFORE `boxman up` — creates br-pve so the installers find hpe1's
#         DHCP from their first boot (boxman adopts the existing bridge).
#   hpe1: run AFTER `boxman up` — libvirt must create virbr-pve itself; a
#         pre-existing bridge of that name makes `virsh net-start` fail.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

peer=${PEER[$SITE]}
local_ip=${HOST_IP[$SITE]}
remote_ip=${HOST_IP[$peer]}
bridge=${SITE_BRIDGE[$SITE]}

sudo modprobe vxlan

if [[ $SITE == hpe2 ]]; then
    if ! ip link show "$bridge" &>/dev/null; then
        log "creating shared bridge $bridge"
        sudo ip link add name "$bridge" type bridge
    fi
    sudo ip link set dev "$bridge" type bridge stp_state 0
elif ! ip link show "$bridge" &>/dev/null; then
    die "$bridge does not exist: run 'BOXMAN_SITE=hpe1 boxman up' first so libvirt creates it"
fi
sudo ip link set dev "$bridge" mtu "$LAB_MTU" up

if ! ip link show "$VXLAN_IF" &>/dev/null; then
    log "creating $VXLAN_IF: vni $VXLAN_ID $local_ip -> $remote_ip via $VXLAN_DEV udp/$VXLAN_PORT"
    sudo ip link add "$VXLAN_IF" type vxlan id "$VXLAN_ID" local "$local_ip" \
        remote "$remote_ip" dstport "$VXLAN_PORT" dev "$VXLAN_DEV"
fi
sudo ip link set dev "$VXLAN_IF" mtu "$LAB_MTU" master "$bridge" up

if [[ $SITE == hpe2 ]]; then
    # With br_netfilter loaded (docker does that), frames *bridged* through
    # br-pve traverse firewalld's forward hook, and a bridge bound to no zone
    # ends in the public zone's `reject`: guests' DHCP never reached hpe1
    # (tunnel counters 29 sent / 3 received) while host-originated pings
    # worked. boxman's physdev ACCEPT lives in the iptables table and cannot
    # override a later nftables reject, so bind the lab bridge to `trusted`.
    # (hpe1 needs nothing: libvirt puts virbr-pve into its own zone.)
    if [[ $(sudo firewall-cmd --get-zone-of-interface="$bridge" 2>/dev/null) != trusted ]]; then
        sudo firewall-cmd -q --zone=trusted --add-interface="$bridge"
        sudo firewall-cmd -q --permanent --zone=trusted --add-interface="$bridge"
        log "firewalld: $bridge bound to zone trusted (bridged frames were being rejected)"
    fi
    if ! ip -4 addr show dev "$bridge" | grep -qw "$PEER_BRIDGE_IP"; then
        # a host address on the bridge: lets hpe2 reach the nodes directly and
        # gives libvirt's ARP-based IP discovery something to work with
        sudo ip addr add "$PEER_BRIDGE_IP/24" dev "$bridge"
    fi
fi

# let the peer's tunnel traffic in (runtime + permanent, no reload needed)
rule="rule family=ipv4 source address=${remote_ip} port port=${VXLAN_PORT} protocol=udp accept"
for perm in "" "--permanent"; do
    if ! sudo firewall-cmd -q $perm --zone=public --query-rich-rule="$rule"; then
        sudo firewall-cmd -q $perm --zone=public --add-rich-rule="$rule"
        log "firewalld${perm:+ (permanent)}: allowed udp/$VXLAN_PORT from $remote_ip"
    fi
done

log "$bridge ports:"
bridge link show master "$bridge" | sed 's/^/    /'
ip -d link show "$VXLAN_IF" | grep -o "mtu [0-9]*\|vxlan id [0-9]* remote [0-9.]* local [0-9.]* dev [^ ]*" | tr '\n' ' '; echo
