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

for n in "${MONS[@]}"; do
    if ! pssh "$first" "ceph mon dump 2>/dev/null | grep -qw mon.$n"; then
        # each new mon must see a quorate cluster before joining it; the
        # election after the previous mon takes a few seconds
        wait_quorum "$first"
        pssh "$n" "pveceph mon create"
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
