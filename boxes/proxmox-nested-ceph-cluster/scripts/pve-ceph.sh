#!/usr/bin/env bash
# Hyper-converged Ceph on the four nodes: packages everywhere (parallel),
# init on pve1, mons on pve1-3, mgrs on pve1+pve3, one OSD per data disk
# (/dev/vdb, /dev/vdc on every node = 8 OSDs), replicated pool `vmpool`
# registered as Proxmox storage. Idempotent. Run from hpe1.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

first=${NODES[0]}
MONS=(pve1 pve2 pve3)
MGRS=(pve1 pve3)
OSD_DEVS=(/dev/vdb /dev/vdc)
version_arg=${PVE_CEPH_VERSION:+--version $PVE_CEPH_VERSION}

log "installing ceph packages on ${NODES[*]} (repository no-subscription ${version_arg:-default version})"
pids=()
for n in "${NODES[@]}"; do
    # `yes |` covers pveceph versions whose apt-get call still prompts
    pssh "$n" "command -v ceph-osd >/dev/null && exit 0
        export DEBIAN_FRONTEND=noninteractive
        yes | pveceph install --repository no-subscription $version_arg" \
        > "$LOGS/ceph-install-$n.log" 2>&1 &
    pids+=($!)
done
fail=0
for i in "${!pids[@]}"; do
    wait "${pids[$i]}" || { log "ceph install failed on ${NODES[$i]} (see $LOGS/ceph-install-${NODES[$i]}.log)"; fail=1; }
done
(( fail == 0 )) || exit 1
log "ceph $(pssh "$first" ceph --version | awk '{print $3}') installed on all nodes"

pssh "$first" "test -f /etc/pve/ceph.conf || pveceph init --network $LAB_NET --size 3 --min_size 2"

# wait_quorum [node]: block until the mon cluster answers on that node
wait_quorum() {
    local n=${1:-$first}
    for _ in $(seq 1 60); do
        pssh "$n" "timeout 10 ceph quorum_status >/dev/null 2>&1" && return 0
        sleep 3
    done
    die "ceph monitors did not reach quorum (checked from $n)"
}

# mon_exists <node>: does <node> already run a monitor?
#
# Asks the node, not the cluster. `ceph mon dump` reaches out to a monitor, so
# on a cluster that has none yet it blocks until `timeout` kills it -- and an
# assignment from a failing command substitution ends the script outright under
# this file's `set -e`, which is how the first attempt at this fix still exited
# with no monitor created. /var/lib/ceph/mon/ceph-<node> is local, needs no
# running cluster and cannot hang. The mgr loop below already uses exactly this
# test. Only an explicit `yes`/`no` is an answer: anything else -- ssh failing,
# empty output, an unexpected reply -- is reported, never read as absence
# (#171 B1).
mon_exists() {
    local node=$1 out
    out=$(pssh "$node" "test -d /var/lib/ceph/mon/ceph-$node && echo yes || echo no") \
        || die "cannot ask $node whether it already runs a ceph monitor"
    case $out in
        yes) return 0 ;;
        no)  return 1 ;;
        *)   die "unexpected reply from $node when checking for a ceph monitor: ${out:-<empty>}" ;;
    esac
}

# Reachability first, so a later probe failure is a real failure and not
# mistaken for "this node has no monitor".
for n in "${MONS[@]}"; do wait_ssh "$n" 300; done

# Is there a monitor anywhere yet? Decided before the loop, because only the
# genuinely first monitor may skip the quorum wait.
bootstrapped=0
for n in "${MONS[@]}"; do
    if mon_exists "$n"; then bootstrapped=1; break; fi
done

for n in "${MONS[@]}"; do
    if ! mon_exists "$n"; then
        if (( bootstrapped )); then
            # Every monitor after the first must see a quorate cluster before it
            # joins: the election following the previous one takes a few seconds,
            # and `pveceph mon create` fails with "Could not connect to ceph
            # cluster" inside that window.
            wait_quorum "$first"
        else
            log "bootstrapping the first ceph monitor on $n"
        fi
        pssh "$n" "pveceph mon create"
        bootstrapped=1
        mon_exists "$n" || die "pveceph mon create on $n reported success but left no monitor"
    fi
    wait_quorum "$n"
    log "mon $n ok"
done

for n in "${MGRS[@]}"; do
    pssh "$n" "test -d /var/lib/ceph/mgr/ceph-$n || pveceph mgr create"
    log "mgr $n ok"
done

for n in "${NODES[@]}"; do
    for dev in "${OSD_DEVS[@]}"; do
        pssh "$n" "ceph-volume lvm list $dev >/dev/null 2>&1 || pveceph osd create $dev" \
            > "$LOGS/osd-$n-$(basename "$dev").log" 2>&1 \
            || die "osd create $dev on $n failed (see $LOGS/osd-$n-$(basename "$dev").log)"
        log "osd $n:$dev ok"
    done
done

pssh "$first" "ceph osd pool ls | grep -qx $CEPH_POOL || \
    pveceph pool create $CEPH_POOL --add_storages 1 --size 3 --min_size 2 --pg_autoscale_mode on"
log "pool $CEPH_POOL ok"

# wait for HEALTH_OK (PGs peering after 8 fresh OSDs), then show the picture
for _ in $(seq 1 30); do
    pssh "$first" "ceph health" | grep -q HEALTH_OK && break
    sleep 10
done
pssh "$first" "ceph -s; echo; pvesm status"
