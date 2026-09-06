#!/usr/bin/env bash
# Form the Proxmox cluster: pve1 creates it, pve2..pve4 join sequentially over
# ssh. Idempotent (a node already in a cluster is skipped). Run from hpe1.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

first=${NODES[0]}
hosts_block="# pve-lab-nodes"
for n in "${NODES[@]}"; do hosts_block+=$'\n'"${NODE_IP[$n]} $n.$DOMAIN $n"; done

for n in "${NODES[@]}"; do
    wait_ssh "$n"
    # node-to-node root ssh (pvecm add --use_ssh): the lab key is already
    # authorised everywhere by the answer file; give each node the private half
    # and pre-seed known_hosts so nothing prompts
    pscp -q "$KEY" "root@${NODE_IP[$n]}:/root/.ssh/id_ed25519"
    pssh "$n" "chmod 600 /root/.ssh/id_ed25519
        touch /root/.ssh/known_hosts
        for ip in ${NODE_IP[*]}; do ssh-keyscan -t ed25519 \$ip 2>/dev/null; done >> /root/.ssh/known_hosts
        sort -u -o /root/.ssh/known_hosts /root/.ssh/known_hosts
        grep -q '^# pve-lab-nodes' /etc/hosts || printf '%s\n' \"$hosts_block\" >> /etc/hosts"
    log "$n prepared ($(pssh "$n" hostname -f))"
done

if pssh "$first" pvecm status &>/dev/null; then
    log "$first already in a cluster"
else
    log "creating cluster $CLUSTER_NAME on $first"
    pssh "$first" "pvecm create $CLUSTER_NAME --link0 ${NODE_IP[$first]}"
    # pmxcfs restarts after create; a join that arrives before /etc/pve is
    # writable again fails with "unable to copy ssh ID: … Permission denied"
    for _ in $(seq 1 30); do
        pssh "$first" "pvecm status >/dev/null 2>&1 && touch /etc/pve/.pve-lab-ready" 2>/dev/null && break
        sleep 2
    done
fi

for n in "${NODES[@]:1}"; do
    if pssh "$n" pvecm status &>/dev/null; then
        log "$n already joined"; continue
    fi
    log "joining $n"
    pssh "$n" "pvecm add ${NODE_IP[$first]} --use_ssh --link0 ${NODE_IP[$n]}" 2>&1 | tee "$LOGS/pvecm-add-$n.log" | tail -3
    sleep 5
done

log "cluster status:"
pssh "$first" pvecm status | sed -n '/^Membership/,$p'
