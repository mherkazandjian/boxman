#!/usr/bin/env bash
# Prove the 1450-byte path MTU across the VXLAN: a 1422-byte payload
# (1450 - 28 bytes IP/ICMP) must pass with DF set, 1472 (a 1500 frame) must not.
# Run from hpe1.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

from=${1:-pve1}; to=${2:-pve3}
wait_ssh "$from"
ok=1
if pssh "$from" "ping -c3 -M do -s 1422 -W2 ${NODE_IP[$to]}" >/dev/null; then
    log "ok   $from -> $to: 1450-byte frames pass with DF"
else
    log "FAIL $from -> $to: 1450-byte frames dropped"; ok=0
fi
if pssh "$from" "ping -c2 -M do -s 1472 -W2 ${NODE_IP[$to]}" >/dev/null 2>&1; then
    log "WARN $from -> $to: 1500-byte frames pass too (jumbo underlay? or MTU not lowered)"
else
    log "ok   $from -> $to: 1500-byte frames correctly refused (needs fragmentation)"
fi
log "host -> $to: $(ping -c3 -M do -s 1422 -W2 "${NODE_IP[$to]}" | tail -2 | head -1)"
(( ok )) || exit 1
