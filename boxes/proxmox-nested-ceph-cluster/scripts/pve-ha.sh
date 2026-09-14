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
#
# `mapfile -t x < <(cmd | jq)` cannot see the process substitution's exit
# status -- not even under `pipefail` -- so inventory that printed a partial
# list and then failed was used as if complete, and with the rule cleanup
# below that silently deleted a rule (the same defect already fixed in
# ha-watch.sh, reintroduced here).
inventory=$(pssh "$first" "pvesh get /cluster/ha/resources --output-format json") \
    || die "could not read the HA resources from $first"
sids_raw=$(jq -r '.[].sid' <<<"$inventory" | grep '^vm:' | sort) \
    || die "could not parse the HA resource list from $first"
[[ -n $sids_raw ]] || die "no HA resources registered yet (run make tf-apply first)"
mapfile -t sids <<<"$sids_raw"

declare -A DESIRED=()          # sid -> rule it belongs in
hpe1_res=() hpe2_res=() unowned=()
for sid in "${sids[@]}"; do
    node=${POLICY_HOME[$sid]:-}
    if [[ -z $node ]]; then unowned+=("$sid"); continue; fi
    case ${NODE_SITE[$node]} in
        hpe1) hpe1_res+=("$sid"); DESIRED[$sid]=prefer-hpe1 ;;
        hpe2) hpe2_res+=("$sid"); DESIRED[$sid]=prefer-hpe2 ;;
    esac
done
(( ${#unowned[@]} )) && log "not covered by this policy, left alone: ${unowned[*]}"

# rule_state <rule>: prints the rule's resources csv, or nothing when it does
# not exist. A *query* that fails is not evidence of absence: ssh answers 255
# for a connection problem, and treating that as "no such rule" skipped a
# cleanup and still reported success.
rule_state() {
    local rule=$1 out rc=0
    out=$(pssh "$first" "pvesh get /cluster/ha/rules/$rule --output-format json 2>/dev/null") || rc=$?
    case $rc in
        0)   jq -r '.resources // ""' <<<"$out" 2>/dev/null || echo "" ;;
        255) die "could not reach $first to query rule $rule" ;;
        *)   echo "" ;;                       # pvesh said no such rule
    esac
}

set_rule() {   # set_rule <rule> <nodes> <resources-csv>
    local rule=$1 nodes=$2 resources=$3
    if [[ -n $(rule_state "$rule") ]] || pssh "$first" "pvesh get /cluster/ha/rules/$rule >/dev/null 2>&1"; then
        pssh "$first" "pvesh set /cluster/ha/rules/$rule --nodes '$nodes' --resources '$resources' --strict 0 --disable 0"
        log "rule $rule updated: nodes=$nodes resources=$resources"
    else
        pssh "$first" "pvesh create /cluster/ha/rules --type node-affinity --rule $rule --nodes '$nodes' --resources '$resources' --strict 0 --comment 'prefer this physical host; non-strict'"
        log "rule $rule created: nodes=$nodes resources=$resources"
    fi
}

drop_rule() {
    local rule=$1
    if [[ -n $(rule_state "$rule") ]] || pssh "$first" "pvesh get /cluster/ha/rules/$rule >/dev/null 2>&1"; then
        pssh "$first" "pvesh delete /cluster/ha/rules/$rule"
        log "rule $rule removed: no resources are assigned to it any more"
    fi
}

# --- withdraw first, then add ----------------------------------------------
#
# Proxmox checks a node-affinity rule for feasibility before persisting it and
# refuses a resource that is already a member of another one. Adding the
# incoming member before its old rule had released it therefore failed, and a
# VM could never move from one host's rule to the other's -- the reverse
# direction happened to work, which is why one-directional testing missed it.
for rule in prefer-hpe1 prefer-hpe2; do
    current=$(rule_state "$rule")
    [[ -n $current ]] || continue
    keep=()
    IFS=',' read -ra _members <<<"$current"
    for m in "${_members[@]}"; do
        m=${m// /}
        [[ -z $m ]] && continue
        [[ ${DESIRED[$m]:-} == "$rule" ]] && keep+=("$m")
    done
    if (( ${#keep[@]} == ${#_members[@]} )); then
        continue                      # nothing is leaving this rule
    fi
    if (( ${#keep[@]} )); then
        nodes=$([[ $rule == prefer-hpe1 ]] && echo "pve1:1,pve2:1" || echo "pve3:1,pve4:1")
        pssh "$first" "pvesh set /cluster/ha/rules/$rule --nodes '$nodes' --resources '$(join_by , "${keep[@]}")' --strict 0 --disable 0"
        log "rule $rule: released departing members before the additions"
    else
        pssh "$first" "pvesh delete /cluster/ha/rules/$rule"
        log "rule $rule: released every member before the additions"
    fi
done

if (( ${#hpe1_res[@]} )); then
    set_rule prefer-hpe1 "pve1:1,pve2:1" "$(join_by , "${hpe1_res[@]}")"
else
    drop_rule prefer-hpe1
fi
if (( ${#hpe2_res[@]} )); then
    set_rule prefer-hpe2 "pve3:1,pve4:1" "$(join_by , "${hpe2_res[@]}")"
else
    drop_rule prefer-hpe2
fi

echo "--- crs";   pssh "$first" "pvesh get /cluster/options --output-format json" | jq -r '.crs'
echo "--- rules"; pssh "$first" "ha-manager rules list"
echo "--- ha";    pssh "$first" "ha-manager status"
