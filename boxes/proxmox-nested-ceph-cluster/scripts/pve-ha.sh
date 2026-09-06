#!/usr/bin/env bash
# Placement policy for the HA-managed VMs — the part the Terraform provider
# cannot express (it has HA resources, but no HA rules or CRS options):
#
#   * cluster resource scheduling `dynamic` (static + live CPU/RAM usage),
#     automatic rebalancing when node imbalance exceeds a threshold for a few
#     HA rounds, and CRS placement when an HA resource starts
#   * two non-strict node-affinity rules: the VMs Terraform placed on hpe1's
#     nodes prefer pve1/pve2 (equal priority, so CRS balances between them),
#     the hpe2 ones prefer pve3/pve4; when both preferred nodes are down the
#     HA manager may recover them anywhere (non-strict)
#
# Idempotent. Run from hpe1 after `make tf-apply` (the rules need the HA
# resources to exist). Knobs: PVE_CRS_THRESHOLD / MARGIN / HOLD (percent,
# percent, HA rounds) and PVE_CRS_METHOD (bruteforce|topsis).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

first=${NODES[0]}
VM_ID_BASE=${VM_ID_BASE:-200}
crs="ha=dynamic,ha-auto-rebalance=1"
crs+=",ha-auto-rebalance-threshold=${PVE_CRS_THRESHOLD:-20}"
crs+=",ha-auto-rebalance-margin=${PVE_CRS_MARGIN:-10}"
crs+=",ha-auto-rebalance-hold-duration=${PVE_CRS_HOLD:-3}"
crs+=",ha-auto-rebalance-method=${PVE_CRS_METHOD:-bruteforce}"
crs+=",ha-rebalance-on-start=1"

log "cluster resource scheduling: $crs"
pssh "$first" "pvesh set /cluster/options --crs '$crs'"

# HA resources currently registered, split by the Terraform round-robin
# convention: index (vmid - base) % 4 in {0,1} was created on hpe1's nodes,
# {2,3} on hpe2's. (Terraform ignores later moves; this is the *policy* home.)
mapfile -t sids < <(pssh "$first" "pvesh get /cluster/ha/resources --output-format json" \
                    | jq -r '.[].sid' | grep '^vm:' | sort)
(( ${#sids[@]} )) || die "no HA resources registered yet (run make tf-apply first)"
hpe1_res=() hpe2_res=()
for sid in "${sids[@]}"; do
    id=${sid#vm:}
    if (( (id - VM_ID_BASE) % 4 < 2 )); then hpe1_res+=("$sid"); else hpe2_res+=("$sid"); fi
done

# ensure_rule <rule> <nodes> <resources-csv>
ensure_rule() {
    local rule=$1 nodes=$2 resources=$3
    if pssh "$first" "pvesh get /cluster/ha/rules/$rule >/dev/null 2>&1"; then
        pssh "$first" "pvesh set /cluster/ha/rules/$rule --nodes '$nodes' --resources '$resources' --strict 0 --disable 0"
        log "rule $rule updated: nodes=$nodes resources=$resources"
    else
        pssh "$first" "pvesh create /cluster/ha/rules --type node-affinity --rule $rule --nodes '$nodes' --resources '$resources' --strict 0 --comment 'prefer this physical host; non-strict'"
        log "rule $rule created: nodes=$nodes resources=$resources"
    fi
}
(( ${#hpe1_res[@]} )) && ensure_rule prefer-hpe1 "pve1:1,pve2:1" "$(join_by , "${hpe1_res[@]}")"
(( ${#hpe2_res[@]} )) && ensure_rule prefer-hpe2 "pve3:1,pve4:1" "$(join_by , "${hpe2_res[@]}")"

echo "--- crs";   pssh "$first" "pvesh get /cluster/options --output-format json" | jq -r '.crs'
echo "--- rules"; pssh "$first" "ha-manager rules list"
echo "--- ha";    pssh "$first" "ha-manager status"
