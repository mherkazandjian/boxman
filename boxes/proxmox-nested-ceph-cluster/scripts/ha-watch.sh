#!/usr/bin/env bash
# Watch the HA manager move the resources of one node, printing a timeline.
# Usage: ha-watch.sh <failed-node> [timeout-s]   (run from hpe1; the node was
# just hard-killed on its host, e.g. `virsh destroy bprj__pvelab__bprj_pve_pve4`)
# Exits 0 once every HA resource that was on <failed-node> is `started` on
# another node. Typical: fencing ~1-2 min, then restarts.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

node=$1; timeout=${2:-600}
[[ -n ${NODE_IP[$node]:-} ]] || die "usage: ha-watch.sh <node> [timeout]"
# ask a surviving node: the first one that is not the victim
for n in "${NODES[@]}"; do [[ $n != "$node" ]] && { query=$n; break; }; done

status() { pssh "$query" "pvesh get /cluster/ha/status/current --output-format json" 2>/dev/null; }

mapfile -t victims < <(status | jq -r ".[] | select(.type==\"service\" and .node==\"$node\") | .sid")
log "resources on $node: ${victims[*]:-none}"
(( ${#victims[@]} )) || exit 0

t0=$SECONDS; last=""
while :; do
    snap=$(status | jq -r '.[] | select(.type=="service") | "\(.sid)=\(.node):\(.state)"' | sort | tr '\n' ' ')
    nodesnap=$(status | jq -r '.[] | select(.type=="node") | "\(.node)=\(.status)"' | sort | tr '\n' ' ')
    if [[ "$snap $nodesnap" != "$last" ]]; then
        log "+$((SECONDS - t0))s  $nodesnap"
        log "       $snap"
        last="$snap $nodesnap"
    fi
    done_all=1
    for v in "${victims[@]}"; do
        [[ $snap == *"$v="* ]] || { done_all=0; continue; }
        [[ $snap =~ $v=([a-z0-9]+):started ]] && [[ ${BASH_REMATCH[1]} != "$node" ]] || done_all=0
    done
    (( done_all )) && { log "all resources of $node recovered elsewhere after $((SECONDS - t0))s"; exit 0; }
    (( SECONDS - t0 < timeout )) || die "not recovered after ${timeout}s"
    sleep 5
done
