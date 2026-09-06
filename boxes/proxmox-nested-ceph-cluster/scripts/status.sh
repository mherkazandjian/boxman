#!/usr/bin/env bash
# One-screen picture of the lab from hpe1: bridge ports, cluster, Ceph, VMs.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

bridge=${SITE_BRIDGE[$SITE]}
log "$bridge ports: $(bridge link show master "$bridge" 2>/dev/null | awk '{print $2}' | tr '\n' ' ')"
for n in "${NODES[@]}"; do
    if pssh "$n" true 2>/dev/null; then
        log "$n ${NODE_IP[$n]}: $(pssh "$n" 'echo "$(hostname -f) up $(cut -d. -f1 /proc/uptime)s, $(qm list | tail -n +2 | wc -l) vm(s)"')"
    else
        log "$n ${NODE_IP[$n]}: no ssh"
    fi
done
if pssh pve1 true 2>/dev/null; then
    echo "--- pvecm status";  pssh pve1 'pvecm status 2>&1 | sed -n "/^Membership/,\$p"' || true
    echo "--- ceph";          pssh pve1 'ceph -s 2>&1 | head -14' || true
    echo "--- vms";           pssh pve1 'pvesh get /cluster/resources --type vm --output-format json 2>/dev/null' \
                                  | jq -r '.[] | "\(.vmid) \(.name) \(.node) \(.status)"' || true   # jq runs here, not on the node
fi
