#!/usr/bin/env bash
# Live-migrate a VM to another node while pinging it from this host.
# Usage: pve-migrate.sh [target-node] [vmid] [vm-ip]
#   defaults: pve3 (i.e. hpe1 -> hpe2), the demo VM 100 at 10.77.0.50
# The disk lives on Ceph, so only RAM moves. Run from hpe1.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

target=${1:-pve3}
DEMO_VMID=${2:-$DEMO_VMID}
DEMO_IP=${3:-$DEMO_IP}
[[ -n ${NODE_IP[$target]:-} ]] || die "unknown node $target"

# `read < <(cmd | jq)` sees whether *read* got a row, not whether the producer
# finished: ssh printing a valid row and then exiting 255, or jq failing after
# emitting one, both left a usable-looking $src and the migration started
# anyway. Capture and check each step before anything is done with the answer.
# (Predates this work -- caa4be2 -- and is the fourth instance of the defect.)
resources=$(pssh pve1 "pvesh get /cluster/resources --type vm --output-format json") \
    || die "could not read the cluster resources from pve1"
row=$(jq -r ".[] | select(.vmid == $DEMO_VMID) | \"\(.node) \(.name)\"" <<<"$resources") \
    || die "could not parse the cluster resources from pve1"
read -r src DEMO_NAME <<<"$row"
[[ -n ${src:-} ]] || die "VM $DEMO_VMID not found in the cluster"
[[ $src != "$target" ]] || die "VM $DEMO_VMID is already on $target"
log "VM $DEMO_VMID ($DEMO_NAME) on $src (${NODE_SITE[$src]}) -> $target (${NODE_SITE[$target]})"

pinglog="$LOGS/ping-migrate-$src-$target.log"
ping -i 0.2 -w 600 "$DEMO_IP" > "$pinglog" 2>&1 &
pingpid=$!
sleep 1

t0=$(date +%s%N)
# `... | tee | grep` reports *grep's* exit status, and the `|| true` that was
# there to tolerate a no-match swallowed a failed migration along with it, so
# a rejected `qm migrate` was reported as a success (#171 B10). Keep the
# migration's own status; filter the log afterwards for the readable lines.
migrate_rc=0
pssh "$src" "qm migrate $DEMO_VMID $target --online" \
    > "$LOGS/qm-migrate-$src-$target.log" 2>&1 || migrate_rc=$?
t1=$(date +%s%N)
grep -E "starting|migration (status|speed|finished)|downtime|successfully|ERROR" \
    "$LOGS/qm-migrate-$src-$target.log" || true

sleep 2
# stop the ping before any exit, so a failed migration does not leak it
kill -INT "$pingpid" 2>/dev/null; wait "$pingpid" 2>/dev/null || true   # INT makes ping print its stats
stats=$(grep -E 'packets transmitted' "$pinglog" || echo "no ping stats")

(( migrate_rc == 0 )) || die "qm migrate $DEMO_VMID $src -> $target failed (exit $migrate_rc); see $LOGS/qm-migrate-$src-$target.log"

log "migration took $(( (t1 - t0) / 1000000 )) ms end to end (qm migrate call)"
log "ping during migration: $stats"

# Assert where it landed. The placement was printed but never checked, so a
# migration that returned 0 and left the guest on its source node still read
# as a successful demo (#171 B10).
placement=$(pssh pve1 "pvesh get /cluster/resources --type vm --output-format json" \
    | jq -r ".[] | select(.vmid == $DEMO_VMID) | \"\(.node) (\(.status))\"")
log "now on: $placement"
[[ ${placement%% *} == "$target" ]] || \
    die "VM $DEMO_VMID ended up on ${placement%% *}, not the requested $target"
ssh "${SSH_OPTS[@]}" "${4:-demo}@$DEMO_IP" 'echo "guest uptime: $(cut -d" " -f1 /proc/uptime)s"'
