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

# --- policy homes: where each VM was *declared* to live ---------------------
#
# Required, with no fallback. This used to be derived from
# `(vmid - VM_ID_BASE) % 4`, while Terraform owned placement through
# `node_overrides`, `nodes` and `vm_id_base` -- none of which reached here. The
# two agreed only by coincidence of the shipped override, and an override to
# pve3/pve4, or a changed vm_id_base, would have silently contradicted the
# affinity rule. Guessing and warning would preserve exactly that defect, so a
# missing mapping is an error (#171 D1).
#
# It is the *configured* placement, deliberately not `node_name` from Terraform
# state: `ignore_changes` permits HA to move a VM, so refreshed state records
# where it ended up, and feeding that back would make the policy home follow
# the drift it exists to correct.
#
# Format: `vm:<id>=<node>,...`  (`make ha` builds it from the
# `ha_policy_homes` Terraform output; pass it by hand to run this directly.)
[[ -n ${PVE_HA_POLICY_HOMES:-} ]] || die \
    "PVE_HA_POLICY_HOMES is not set. Run 'make ha', which builds it from the
     ha_policy_homes terraform output, or pass it yourself as
     'vm:200=pve1,vm:201=pve2,...'. There is deliberately no fallback: the
     arithmetic this replaced could silently disagree with the placement
     terraform actually applied."

declare -A POLICY_HOME=()
IFS=',' read -ra _entries <<<"$PVE_HA_POLICY_HOMES"
for _e in "${_entries[@]}"; do
    _e=${_e// /}
    [[ -z $_e ]] && continue
    [[ $_e == *=* ]] || die "malformed PVE_HA_POLICY_HOMES entry '$_e' (want vm:200=pve1)"
    _sid=${_e%%=*}; _node=${_e#*=}
    [[ $_sid =~ ^vm:[0-9]+$ ]] || die "not a vm resource id: '$_sid'"
    [[ -n ${NODE_SITE[$_node]:-} ]] || die "unknown node '$_node' for $_sid"
    POLICY_HOME[$_sid]=$_node
done
(( ${#POLICY_HOME[@]} )) || die "PVE_HA_POLICY_HOMES is empty"
log "policy homes: ${#POLICY_HOME[@]} resources"

# Everything above is validated before the first write, so a bad mapping cannot
# leave the cluster with new CRS settings and stale rules (#171 D1).
crs="ha=dynamic,ha-auto-rebalance=1"
crs+=",ha-auto-rebalance-threshold=${PVE_CRS_THRESHOLD:-20}"
crs+=",ha-auto-rebalance-margin=${PVE_CRS_MARGIN:-10}"
crs+=",ha-auto-rebalance-hold-duration=${PVE_CRS_HOLD:-3}"
crs+=",ha-auto-rebalance-method=${PVE_CRS_METHOD:-bruteforce}"
crs+=",ha-rebalance-on-start=1"

log "cluster resource scheduling: $crs"
pssh "$first" "pvesh set /cluster/options --crs '$crs'"

# Registered HA resources, intersected with the policy: a resource this
# configuration does not own is left out of the rules rather than filed under
# whichever group the arithmetic would have picked.
mapfile -t sids < <(pssh "$first" "pvesh get /cluster/ha/resources --output-format json" \
                    | jq -r '.[].sid' | grep '^vm:' | sort)
(( ${#sids[@]} )) || die "no HA resources registered yet (run make tf-apply first)"

hpe1_res=() hpe2_res=() unowned=()
for sid in "${sids[@]}"; do
    node=${POLICY_HOME[$sid]:-}
    if [[ -z $node ]]; then unowned+=("$sid"); continue; fi
    case ${NODE_SITE[$node]} in
        hpe1) hpe1_res+=("$sid") ;;
        hpe2) hpe2_res+=("$sid") ;;
    esac
done
(( ${#unowned[@]} )) && log "not covered by this policy, left alone: ${unowned[*]}"

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

# drop_rule <rule>: a group with no members must not keep the membership it had
# last time. The old `(( ${#x[@]} )) && ensure_rule …` left a stale rule in
# place -- and, being an `&&` list under `set -e`, ended the script outright
# when a group was empty (#171 D1).
drop_rule() {
    local rule=$1
    if pssh "$first" "pvesh get /cluster/ha/rules/$rule >/dev/null 2>&1"; then
        pssh "$first" "pvesh delete /cluster/ha/rules/$rule"
        log "rule $rule removed: no resources are assigned to it any more"
    fi
}

if (( ${#hpe1_res[@]} )); then
    ensure_rule prefer-hpe1 "pve1:1,pve2:1" "$(join_by , "${hpe1_res[@]}")"
else
    drop_rule prefer-hpe1
fi
if (( ${#hpe2_res[@]} )); then
    ensure_rule prefer-hpe2 "pve3:1,pve4:1" "$(join_by , "${hpe2_res[@]}")"
else
    drop_rule prefer-hpe2
fi

echo "--- crs";   pssh "$first" "pvesh get /cluster/options --output-format json" | jq -r '.crs'
echo "--- rules"; pssh "$first" "ha-manager rules list"
echo "--- ha";    pssh "$first" "ha-manager status"
