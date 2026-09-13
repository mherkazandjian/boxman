#!/usr/bin/env bash
# Watch the HA manager move the resources of one node, printing a timeline.
# Usage: ha-watch.sh <failed-node> [timeout-s]   (run from hpe1; the node was
# just hard-killed on its host, e.g. `virsh destroy bprj__pvelab__bprj_pve_pve4`)
# Exits 0 once every HA resource that was on <failed-node> is `started` on
# another node. Typical: fencing ~1-2 min, then restarts.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

node=$1; shift || true
# `--list` prints the HA resources currently on <node> and exits. The drill
# calls it *before* pulling the plug: once the node is gone HA starts moving
# guests immediately, so an inventory taken afterwards can miss the ones that
# already relocated and then declare the drill complete (#171 B13).
list_only=0
if [[ ${1:-} == --list ]]; then list_only=1; shift; fi
timeout=${1:-600}; shift || true
# any remaining arguments are a victim list captured before the kill
preset_victims=("$@")

[[ -n ${NODE_IP[$node]:-} ]] || die "usage: ha-watch.sh <node> [--list] [timeout] [sid...]"
# ask a surviving node: the first one that is not the victim
for n in "${NODES[@]}"; do [[ $n != "$node" ]] && { query=$n; break; }; done

status() { pssh "$query" "pvesh get /cluster/ha/status/current --output-format json" 2>/dev/null; }

if (( ${#preset_victims[@]} )); then
    victims=("${preset_victims[@]}")
else
    # `mapfile -t x < <(cmd | jq)` cannot see the process substitution's exit
    # status, so a transient API error used to yield an empty list, which read
    # as "nothing was running there" and exited 0 at once -- a failed drill
    # reported as a successful one (#171 B11).
    inventory=$(status) || die "could not query HA status from $query"
    [[ -n $inventory ]] || die "empty HA status from $query"
    victims_raw=$(jq -r ".[] | select(.type==\"service\" and .node==\"$node\") | .sid" <<<"$inventory") \
        || die "could not parse the HA status from $query"
    if [[ -n $victims_raw ]]; then mapfile -t victims <<<"$victims_raw"; else victims=(); fi
fi

if (( list_only )); then
    printf '%s\n' "${victims[@]:-}"
    exit 0
fi

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
