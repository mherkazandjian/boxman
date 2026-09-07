#!/usr/bin/env bash
# Undo vxlan-up.sh on this host: remove the VXLAN, hpe2's shared bridge, and
# the firewalld rule. Run after `boxman destroy` on both sites. boxman never
# removes shared bridges itself, so this is the explicit teardown.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

peer=${PEER[$SITE]}; remote_ip=${HOST_IP[$peer]}; bridge=${SITE_BRIDGE[$SITE]}

if ip link show "$VXLAN_IF" &>/dev/null; then
    sudo ip link del "$VXLAN_IF"; log "removed $VXLAN_IF"
fi
if [[ $SITE == hpe2 ]] && ip link show "$bridge" &>/dev/null; then
    if [[ -z $(bridge link show master "$bridge") ]]; then
        sudo ip link del "$bridge"; log "removed $bridge"
    else
        log "keeping $bridge: it still has ports (VMs attached?)"
    fi
    for perm in "" "--permanent"; do
        sudo firewall-cmd -q $perm --zone=trusted --query-interface="$bridge" \
            && sudo firewall-cmd -q $perm --zone=trusted --remove-interface="$bridge" \
            && log "firewalld${perm:+ (permanent)}: $bridge unbound from trusted"
    done
fi
rule="rule family=ipv4 source address=${remote_ip} port port=${VXLAN_PORT} protocol=udp accept"
for perm in "" "--permanent"; do
    sudo firewall-cmd -q $perm --zone=public --query-rich-rule="$rule" \
        && sudo firewall-cmd -q $perm --zone=public --remove-rich-rule="$rule" \
        && log "firewalld${perm:+ (permanent)}: rule removed"
done
log "host $SITE clean"
