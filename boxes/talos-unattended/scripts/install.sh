#!/usr/bin/env bash
# The install driver: from nothing to a healthy Talos cluster, unattended.
#
#   install.sh [--inputs FILE]        (or: make install)
#
# Runs on the host, serially, and is safe to run again: a second run checks
# what the first one built and changes nothing that is already right. It
# never regenerates the cluster identity, re-sends a node's initial config or
# bootstraps etcd twice. Stages:
#
#   preflight   scripts/preflight.sh; nothing before it has side effects
#   up          one project-wide `boxman up` (BOXMAN, default boxman); each
#               VM's UUID is recorded on the first run and must match after
#   control     scripts/control-exec.sh verify: the control VM's identity,
#               address and pinned ssh host key; then its talosctl, kubectl
#               and flux must be exactly the pinned versions
#   nodes       for each Talos node, from libvirt: its one NIC is on this
#               project's fabric with the reserved MAC, its boot disk is the
#               one boxman created in the workspace with the declared size,
#               and the fabric's DHCP lease for that MAC is the reserved
#               address (bounded wait, TALOS_LEASE_TIMEOUT, 600 s)
#   ship        this box's control-side scripts, the version set, the node
#               and disk tables and the control-side inputs (no credential)
#               to ~/talos-unattended on the control VM, over verified ssh
#   restore     a control VM that lost its state gets it back from the
#               backup; a cluster whose identity is lost everywhere stops
#   generate    gen-config.sh on the control VM (identity created once)
#   backup      the control VM's state to TALOS_BACKUP_DIR/state (0700),
#               after every stage that changes it; the identity's fingerprint
#               must never change
#   apply       apply-config.sh on the control VM
#   bootstrap   bootstrap.sh on the control VM; its kubeconfig is copied to
#               <workspace>/driver/kubeconfig (0600)
#   flux        flux.sh on the control VM, only with TALOS_FLUX=on; the
#               GitLab credential (GITLAB_TOKEN) goes over ssh on stdin
#   status      status.sh on the control VM
#
# Exit status: 0 converged; 1 a stage failed (the message names it; rerun
# after fixing the cause); 3 drift that needs a person.
set -euo pipefail
PROG=install
# shellcheck source=lib.sh
. "$(dirname -- "$0")/lib.sh"

INPUTS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --inputs) INPUTS=${2:?--inputs needs a file}; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done
[ -z "$INPUTS" ] || INPUTS=$(cd -P -- "$(dirname -- "$INPUTS")" && pwd -P)/$(basename -- "$INPUTS")

stage() { log "== $*"; }
# step NAME COMMAND...: run a stage's command; its failure ends the run with
# its status (3 stays drift)
step() {
    local name=$1 rc=0; shift
    "$@" || rc=$?
    [ $rc -eq 0 ] || DIE_STATUS=$rc die "stage $name failed (exit $rc); fix the cause above and run install.sh again"
}
cexec() { "$SCRIPTS/control-exec.sh" ${INPUTS:+--inputs "$INPUTS"} "$@"; }

stage preflight
step preflight "$SCRIPTS/preflight.sh" ${INPUTS:+--inputs "$INPUTS"}
load_inputs "$INPUTS"
read_facts
PROFILE=${TALOS_PROFILE:-full}
FLUX=${TALOS_FLUX:-off}

# the driver's directory, at its checked physical path only (kubeconfig,
# record, pinned host key, transient state archive)
bind_driver_dir
exec 8>"$DRIVER_DIR/.install.lock"
flock -n 8 || die "another install.sh holds $DRIVER_DIR/.install.lock"

# the backup, at the physical path preflight checked, and nowhere else
BACKUP=$(physical_path "$TALOS_BACKUP_DIR") || die "TALOS_BACKUP_DIR: cannot resolve it"
private_dir "$BACKUP"
[ "$(cd -P -- "$BACKUP" && pwd -P)" = "$BACKUP" ] \
    || die "TALOS_BACKUP_DIR $BACKUP changed under us (a component was swapped for a link); refusing to write the identity there"

if [ ! -e "$RECORD" ]; then
    record_set project "$PROJECT"
    record_set profile "$PROFILE"
    record_set started "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
[ "$(record_get profile)" = "$PROFILE" ] \
    || drift "this lab was built with the $(record_get profile) profile, not $PROFILE; v0 does not change a cluster's size"

# ---- up -----------------------------------------------------------------------
stage "up: boxman up, project $PROJECT"
read -ra BOXMAN_CMD <<< "${BOXMAN:-boxman}"
step up "${BOXMAN_CMD[@]}" --conf "$BOX_DIR/conf.yml" up
# what this run's up created: every UUID, the network's and each node's boot
# disk, recorded together and only after up succeeded on a run that started
# with none of them present (preflight), then created=yes. A later run
# touches only what matches.
boot_disk() {   # boot_disk DOMAIN: "<source> <allocation>" of its one disk, vda
    local disks src alloc
    disks=$(vsh domblklist "$1" --details | awk 'NR > 2 && $2 == "disk" {print $3, $4}') \
        || die "$1: cannot list its disks"
    [ "$(grep -c . <<< "$disks")" = 1 ] && [ "${disks%% *}" = vda ] \
        || drift "$1: expected exactly one disk, vda; libvirt reports: ${disks//$'\n'/; }"
    src=${disks#* }
    alloc=$(vsh domblkinfo "$1" vda | awk '/^Allocation:/ {print $2}') || die "$1: cannot read its disk allocation"
    [[ $alloc =~ ^[0-9]+$ ]] || die "$1: cannot read its disk allocation"
    echo "$src $alloc"
}
if [ "$(record_get created)" != yes ]; then
    entries=()
    for i in "${!VM_DOMAIN[@]}"; do
        dom=${VM_DOMAIN[$i]} name=${VM_NAME[$i]}
        [ "$(domain_state "$dom")" != absent ] || die "boxman up finished but $dom does not exist"
        uuid=$(domain_uuid "$dom")
        [ -n "$uuid" ] || die "cannot read the uuid of $dom"
        entries+=("uuid.$name=$uuid")
        if [ "${VM_ROLE[$i]}" != control ]; then
            bd=$(boot_disk "$dom") || exit $?
            read -r src alloc <<< "$bd"
            entries+=("disk.$name=$src" "alloc.$name=$alloc")
        fi
    done
    net_uuid=$(vsh net-uuid "$NETWORK" | sed -n '1{s/[[:space:]]*$//;p}') && [ -n "$net_uuid" ] \
        || die "boxman up finished but the uuid of $NETWORK cannot be read"
    entries+=("net.uuid=$net_uuid")
    for entry in "${entries[@]}"; do record_set "${entry%%=*}" "${entry#*=}"; done
    record_set created yes
    log "up: recorded the ${#VM_DOMAIN[@]} VMs and the network this run created, by uuid"
else
    for i in "${!VM_DOMAIN[@]}"; do
        dom=${VM_DOMAIN[$i]} want=$(record_get "uuid.${VM_NAME[$i]}")
        [ "$(domain_uuid "$dom")" = "$want" ] || drift "$dom is not the domain this driver created (uuid $want)"
    done
    [ "$(vsh net-uuid "$NETWORK" | sed -n '1{s/[[:space:]]*$//;p}')" = "$(record_get net.uuid)" ] \
        || drift "$NETWORK is not the network this driver created"
    log "up: ${#VM_DOMAIN[@]} VMs and the network, each the one this driver created"
fi

# ---- control --------------------------------------------------------------------
stage "control: verify the control VM"
step control cexec verify
tool_version() {   # tool_version TOOL: the version the control VM's TOOL reports
    case "$1" in
        talosctl) cexec run -- talosctl version --client --short | awk '/Talos/ {print $2; exit}' ;;
        kubectl)  cexec run -- kubectl version --client -o json \
                      | "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["clientVersion"]["gitVersion"])' ;;
        flux)     cexec run -- flux version --client | awk '/flux/ {print $2; exit}' ;;
    esac
}
for pair in "talosctl $TALOS_VERSION" "kubectl $KUBECTL_VERSION" "flux $FLUX_VERSION"; do
    read -r tool want <<< "$pair"
    have=$(tool_version "$tool") || have=""
    [ "$have" = "$want" ] || die "control: $tool is ${have:-missing}, the version set pins $want; rebuild the control template (boxman up --rebuild-templates on a fresh project)"
done
log "control: talosctl $TALOS_VERSION, kubectl $KUBECTL_VERSION, flux $FLUX_VERSION as pinned"

# ---- nodes: identity from libvirt, before anything reaches them ------------------
stage "nodes: check each Talos node's NIC, disk and lease"
"$PYTHON" "$SCRIPTS/nodes-from-conf.py" > "$DRIVER_DIR/nodes.txt" \
    || die "cannot write the node table from conf.yml"
: > "$DRIVER_DIR/disks.txt"
WS_PHYS=$(physical_path "$WORKSPACE") || die "cannot resolve the workspace $WORKSPACE"
LEASE_TIMEOUT=${TALOS_LEASE_TIMEOUT:-600}
POLL=${TALOS_POLL_SECONDS:-5}
while read -r name role addr mac; do
    j=""
    for i in "${!VM_NAME[@]}"; do [ "${VM_NAME[$i]}" != "$name" ] || j=$i; done
    [ -n "$j" ] && [ "${VM_ROLE[$j]}" = "$role" ] && [ "${VM_MAC[$j]}" = "$mac" ] \
        || drift "$name: the node table and the rendered conf.yml disagree"
    dom=${VM_DOMAIN[$j]}
    nics=$(vsh domiflist "$dom" | awk 'NR > 2 && NF >= 5 {print $3, tolower($5)}') \
        || die "$name: cannot list its NICs"
    [ "$nics" = "$NETWORK $mac" ] \
        || drift "$name: expected exactly one NIC on $NETWORK with MAC $mac, libvirt reports: ${nics//$'\n'/; }"
    bd=$(boot_disk "$dom") || exit $?
    read -r disk alloc <<< "$bd"
    [ "$disk" = "$(record_get "disk.$name")" ] \
        || drift "$name: its boot disk is $disk, not $(record_get "disk.$name"), the one boxman created for it"
    case "$disk" in /*) ;; *) drift "$name: its boot disk $disk is not an absolute path" ;; esac
    disk_dir=$(physical_path "$(dirname -- "$disk")") || die "$name: cannot resolve the directory of its boot disk $disk"
    below "$disk_dir" "$WS_PHYS" || drift "$name: its boot disk $disk is not in this project's workspace $WS_PHYS"
    capacity=$(vsh domblkinfo "$dom" vda | awk '/^Capacity:/ {print $2}') || die "$name: cannot read its disk size"
    [ "$capacity" = "${VM_DISK[$j]}" ] \
        || drift "$name: its boot disk is $capacity bytes, conf.yml declares ${VM_DISK[$j]}"
    # positive evidence the disk is still the empty one boxman created: not
    # one byte allocated since this driver recorded it right after creation
    evidence=-
    [ "$alloc" != "$(record_get "alloc.$name")" ] || evidence=created-empty
    echo "$name ${VM_DISK[$j]} $evidence" >> "$DRIVER_DIR/disks.txt"
    deadline=$((SECONDS + LEASE_TIMEOUT))
    while :; do
        ips=$(lease_ips "$mac")
        [ -z "$ips" ] || break
        [ $SECONDS -lt $deadline ] || die "$name: no DHCP lease for $mac on $NETWORK within ${LEASE_TIMEOUT}s (is it booting the Talos ISO?)"
        sleep "$POLL"
    done
    [ "$ips" = "$addr" ] || drift "$name: the lease for $mac is ${ips//$'\n'/ }, not the reserved $addr"
    log "nodes: $name ($role) is $dom, NIC $mac on the fabric, lease $addr as reserved, boot disk $disk (${VM_DISK[$j]} bytes, as created${evidence/#-/, written since})"
done < "$DRIVER_DIR/nodes.txt"

# ---- ship --------------------------------------------------------------------------
stage "ship: scripts, version set, node table and inputs to the control VM"
SHIP=$DRIVER_DIR/ship
rm -rf "$SHIP"
mkdir -p "$SHIP/scripts"
for f in gen-config.sh check-node-config.py control-lib.sh apply-config.sh bootstrap.sh flux.sh status.sh; do
    cp "$SCRIPTS/$f" "$SHIP/scripts/$f"
done
cp "$BOX_DIR/versions.env" "$DRIVER_DIR/nodes.txt" "$DRIVER_DIR/disks.txt" "$SHIP/"
if [ -n "${TALOS_PATCH_DIR:-}" ]; then
    [ -d "$TALOS_PATCH_DIR" ] || die "TALOS_PATCH_DIR $TALOS_PATCH_DIR is not a directory"
    mkdir "$SHIP/patches"
    for f in "$TALOS_PATCH_DIR"/*.yaml; do
        [ -e "$f" ] || continue
        cp -- "$f" "$SHIP/patches/" || die "cannot copy the patch $f"
    done
fi
{
    echo "# written by install.sh: the control VM's inputs; no credential"
    for var in TALOS_STATE_DIR TALOS_CLUSTER_NAME TALOS_INSTALL_DISK TALOS_VIP TALOS_NTP_SERVERS \
               TALOS_POD_CIDR TALOS_SERVICE_CIDR TALOS_SMOKE_IMAGE TALOS_CNI_DAEMONSET \
               TALOS_MAINTENANCE_TIMEOUT TALOS_INSTALL_TIMEOUT TALOS_AUTH_TIMEOUT TALOS_ETCD_TIMEOUT \
               TALOS_KUBE_TIMEOUT TALOS_FLUX_TIMEOUT TALOS_POLL_SECONDS TALOS_CALL_TIMEOUT \
               TALOS_REQUEST_TIMEOUT TALOS_PREBOOT_CHECKS TALOS_FLUX \
               GITLAB_HOST GITLAB_OWNER GITOPS_REPOSITORY GITOPS_BRANCH GITOPS_CLUSTER_PATH GITLAB_PERSONAL; do
        [ -z "${!var:-}" ] || printf '%s=%q\n' "$var" "${!var}"
    done
    for entry in "${PINNED[@]}"; do
        key=${entry%%=*}
        printf '%s=%q\n' "$key" "${!key}"
    done
    [ ! -d "$SHIP/patches" ] || printf 'TALOS_PATCH_DIR=%q\n' "$REMOTE_BOX/patches"
} > "$SHIP/control.env"
chmod -R go-rwx "$SHIP"
# shellcheck disable=SC2016
tar -C "$SHIP" -cf - . | step ship cexec run -- bash -c \
    'set -e; umask 077; rm -rf "$1.new"; mkdir -p "$1.new"; tar -xf - -C "$1.new"; rm -rf "$1"; mv "$1.new" "$1"' \
    _ "$REMOTE_BOX"
REMOTE=("--inputs" "$REMOTE_BOX/control.env" "--nodes" "$REMOTE_BOX/nodes.txt")

# ---- restore / generate / backup -----------------------------------------------------
# backup: the control VM's state into $BACKUP/state; the identity never changes
backup() {
    local tar=$DRIVER_DIR/state.tar new=$BACKUP/state.new have want
    rm -rf "$new" "$tar"
    # shellcheck disable=SC2016
    cexec run -- bash -c 'cd "$1" && tar -cf - --exclude=.lock --exclude=".staging.*" .' _ "$TALOS_STATE_DIR" > "$tar" \
        || die "backup: cannot read the control VM's state $TALOS_STATE_DIR"
    ( umask 077 && mkdir -p "$new" && tar -xf "$tar" -C "$new" ) || die "backup: cannot unpack the state"
    rm -f "$tar"
    have=$(cat "$new/secrets.sha256" 2>/dev/null || true)
    [ -n "$have" ] || die "backup: the control VM's state has no secrets fingerprint"
    # an entry the previous backup holds and the control VM lost is lost
    # state, never a reason to act again: stop, and keep the old backup.
    # Only X.applying may go, once X.applied replaced it.
    local lost=() entry name
    for entry in "$BACKUP/state/journal"/*; do
        [ -e "$entry" ] || continue
        name=${entry##*/}
        [ -e "$new/journal/$name" ] && continue
        [ "${name%.applying}" != "$name" ] && [ -e "$new/journal/${name%.applying}.applied" ] && continue
        lost+=("$name")
    done
    if [ ${#lost[@]} -gt 0 ]; then
        rm -rf "$new"
        drift "backup: the control VM's journal lost ${lost[*]}, which the backup in $BACKUP/state holds; the backup was kept. Restore those entries from it before running again"
    fi
    want=$(record_get identity)
    if [ -n "$want" ] && [ "$want" != "$have" ]; then
        rm -rf "$new"
        drift "backup: the control VM's cluster identity changed (fingerprint ${have:0:16}..., recorded ${want:0:16}...); the backup in $BACKUP/state was kept"
    fi
    local said=unchanged
    [ -n "$want" ] || { record_set identity "$have"; said=recorded; }
    rm -rf "$BACKUP/state.old"
    [ ! -e "$BACKUP/state" ] || mv "$BACKUP/state" "$BACKUP/state.old"
    mv "$new" "$BACKUP/state" && rm -rf "$BACKUP/state.old"
    if [ -s "$BACKUP/state/kubeconfig" ]; then
        install -m 0600 "$BACKUP/state/kubeconfig" "$DRIVER_DIR/kubeconfig"
    fi
    log "backup: $1: state in $BACKUP/state, identity ${have:0:16}... $said"
}

stage "restore: the control VM's state"
# gen-config's own guard on TALOS_STATE_DIR (no checkout, a physical path,
# valid inputs) before anything, a restored backup included, is written there
step check cexec run -- "$REMOTE_BOX/scripts/gen-config.sh" "${REMOTE[@]}" --dry-run >/dev/null
rc=0
cexec run -- test -e "$TALOS_STATE_DIR/secrets.yaml" || rc=$?
case "$rc" in
    0) log "restore: the control VM holds the cluster state" ;;
    1) if [ -n "$(record_get identity)" ]; then
           [ -r "$BACKUP/state/secrets.yaml" ] \
               || drift "restore: the cluster identity exists (recorded) but neither the control VM nor $BACKUP/state holds it"
           # shellcheck disable=SC2016
           tar -C "$BACKUP/state" -cf - . | step restore cexec run -- bash -c \
               'set -e; umask 077; mkdir -p "$1"; chmod 0700 "$1"; tar -xf - -C "$1"' _ "$TALOS_STATE_DIR"
           log "restore: the control VM's state was restored from $BACKUP/state"
       else
           log "restore: a new cluster; nothing to restore"
       fi ;;
    *) die "restore: cannot inspect the control VM (ssh exit $rc)" ;;
esac

stage "generate: machine configs on the control VM"
step generate cexec run -- "$REMOTE_BOX/scripts/gen-config.sh" "${REMOTE[@]}"
backup generate

stage "apply: deliver the configs"
apply_rc=0
cexec run -- "$REMOTE_BOX/scripts/apply-config.sh" "${REMOTE[@]}" --disks "$REMOTE_BOX/disks.txt" || apply_rc=$?
backup apply
[ $apply_rc -eq 0 ] || DIE_STATUS=$apply_rc die "stage apply failed (exit $apply_rc); fix the cause above and run install.sh again"

stage "bootstrap: etcd once, then health"
boot_rc=0
cexec run -- "$REMOTE_BOX/scripts/bootstrap.sh" "${REMOTE[@]}" || boot_rc=$?
backup bootstrap
[ $boot_rc -eq 0 ] || DIE_STATUS=$boot_rc die "stage bootstrap failed (exit $boot_rc); fix the cause above and run install.sh again"

if [ "$FLUX" = on ]; then
    stage "flux: bootstrap to $GITLAB_HOST/$GITLAB_OWNER/$GITOPS_REPOSITORY"
    printf '%s\n' "$GITLAB_TOKEN" | step flux cexec run -- "$REMOTE_BOX/scripts/flux.sh" --inputs "$REMOTE_BOX/control.env"
else
    log "flux: off (TALOS_FLUX=on with a GitOps target and credential enables it)"
fi

stage status
step status cexec run -- "$REMOTE_BOX/scripts/status.sh" --control "${REMOTE[@]}"
log "converged: project $PROJECT, $PROFILE profile; kubeconfig $DRIVER_DIR/kubeconfig (0600); talosconfig and secrets backed up in $BACKUP/state"
