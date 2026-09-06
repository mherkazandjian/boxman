#!/usr/bin/env bash
# Live-migrate the demo VM to another node while pinging it from this host.
# Usage: pve-migrate.sh [target-node]   (default pve3, i.e. hpe1 -> hpe2)
# The disk lives on Ceph, so only RAM moves. Run from hpe1.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

target=${1:-pve3}
[[ -n ${NODE_IP[$target]:-} ]] || die "unknown node $target"

src=$(pssh pve1 "pvesh get /cluster/resources --type vm --output-format json" \
      | jq -r ".[] | select(.vmid == $DEMO_VMID) | .node")
[[ -n $src ]] || die "VM $DEMO_VMID not found in the cluster"
[[ $src != "$target" ]] || die "VM $DEMO_VMID is already on $target"
log "VM $DEMO_VMID ($DEMO_NAME) on $src (${NODE_SITE[$src]}) -> $target (${NODE_SITE[$target]})"

pinglog="$LOGS/ping-migrate-$src-$target.log"
ping -i 0.2 -w 600 "$DEMO_IP" > "$pinglog" 2>&1 &
pingpid=$!
sleep 1

t0=$(date +%s%N)
pssh "$src" "qm migrate $DEMO_VMID $target --online" 2>&1 | tee "$LOGS/qm-migrate-$src-$target.log" | grep -E "starting|migration (status|speed|finished)|downtime|successfully|ERROR" || true
t1=$(date +%s%N)

sleep 2
kill -INT "$pingpid" 2>/dev/null; wait "$pingpid" 2>/dev/null || true   # INT makes ping print its stats
stats=$(grep -E 'packets transmitted' "$pinglog" || echo "no ping stats")

log "migration took $(( (t1 - t0) / 1000000 )) ms end to end (qm migrate call)"
log "ping during migration: $stats"
log "now on: $(pssh pve1 "pvesh get /cluster/resources --type vm --output-format json" | jq -r ".[] | select(.vmid == $DEMO_VMID) | \"\(.node) (\(.status))\"")"
ssh "${SSH_OPTS[@]}" "demo@$DEMO_IP" 'echo "guest uptime: $(cut -d" " -f1 /proc/uptime)s"'
