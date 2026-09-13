#!/usr/bin/env bash
# Undo vxlan-up.sh on this host: remove the VXLAN, hpe2's shared bridge, and
# the firewalld rule. Run after `boxman destroy` on both sites. boxman never
# removes shared bridges itself, so this is the explicit teardown.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

peer=${PEER[$SITE]}; remote_ip=${HOST_IP[$peer]}; bridge=${SITE_BRIDGE[$SITE]}
rc=0

# Removals used to sit inside `&&` lists whose status nothing inspected, so with
# both queries answering "bound" and both removals failing, the script still
# printed "host <site> clean" (#171 B18). firewall-cmd answers a --query with 0
# for present and 1 for absent; anything else is the query itself failing, which
# is not evidence of absence.
fw_drop() {   # fw_drop <perm|""> <zone> <--query-x> <--remove-x> <value> <label>
    local perm=$1 zone=$2 qflag=$3 rflag=$4 value=$5 label=$6 q=0
    sudo firewall-cmd -q $perm --zone="$zone" "$qflag=$value" || q=$?
    case $q in
        0) if sudo firewall-cmd -q $perm --zone="$zone" "$rflag=$value"; then
               log "firewalld${perm:+ (permanent)}: $label removed from $zone"
           else
               log "ERROR: firewalld${perm:+ (permanent)}: could not remove $label from $zone"
               rc=1
           fi ;;
        1) : ;;   # absent, which is the expected state after a clean run
        *) log "ERROR: firewalld${perm:+ (permanent)}: could not query $label in $zone (exit $q)"
           rc=1 ;;
    esac
}

if ip link show "$VXLAN_IF" &>/dev/null; then
    sudo ip link del "$VXLAN_IF"; log "removed $VXLAN_IF"
fi
if [[ $SITE == hpe2 ]]; then
    if ip link show "$bridge" &>/dev/null; then
        if [[ -z $(bridge link show master "$bridge") ]]; then
            sudo ip link del "$bridge"; log "removed $bridge"
        else
            log "keeping $bridge: it still has ports (VMs attached?)"
        fi
    fi
    # Deliberately outside the "bridge still exists" test. The permanent zone
    # assignment outlives the interface, so after a reboot -- when the bridge
    # is gone but firewalld's config still names it -- the old nesting skipped
    # this entirely and reported a clean host (#171 B18).
    for perm in "" "--permanent"; do
        fw_drop "$perm" trusted --query-interface --remove-interface "$bridge" "interface $bridge"
    done
fi
rule="rule family=ipv4 source address=${remote_ip} port port=${VXLAN_PORT} protocol=udp accept"
for perm in "" "--permanent"; do
    fw_drop "$perm" public --query-rich-rule --remove-rich-rule "$rule" "the vxlan rich rule"
done

(( rc == 0 )) || die "host $SITE was NOT fully cleaned; see the errors above"
log "host $SITE clean"
