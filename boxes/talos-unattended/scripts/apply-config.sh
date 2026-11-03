#!/usr/bin/env bash
# Deliver each node's machine config, once, and wait until it is installed.
#
#   apply-config.sh --inputs FILE --nodes FILE --disks FILE
#
# Runs on the control VM, after gen-config.sh, with the node table it used
# ("<name> <role> <ipv4> <mac>") and a disk table ("<name> <bytes>": the size
# of the boot disk boxman created for that node). Every node is in one of
# three states, decided from this cluster's journal, never from guesswork:
#
#   recorded    journal/<node>.applied holds the hash of the config applied
#               to it. Only the authenticated API is used: it must answer
#               (bounded wait), and the published config must still have
#               that hash. A changed config is drift: v0 applies no updates
#               to an installed node; the run stops and says how to proceed.
#               A node that stops authenticating is an error, never a reason
#               to retry in insecure mode.
#   interrupted journal/<node>.applying holds the hash of a config whose
#               apply was sent but not confirmed. If the node now
#               authenticates with this cluster's CA, the apply took effect
#               and is recorded. If it still answers the maintenance API,
#               the apply never took effect and the node is fresh. Anything
#               else stops the run.
#   fresh       no journal entry. The node must answer the maintenance API
#               (bounded wait) and must NOT answer the authenticated one. Its
#               identity is checked before anything is sent:
#                 - exactly one link has permanent_addr equal to the
#                   reserved MAC: the selector of the generated
#                   LinkAliasConfig, mac(link.permanent_addr), relies on it;
#                 - TALOS_INSTALL_DISK exists, is the size boxman created,
#                   is writable, not a CD-ROM, and holds no partitions.
#               Then the intent is journaled, the config is applied through
#               the maintenance API, and the node must come back on the
#               authenticated API (bounded wait) before it is recorded.
#
# Timeouts (seconds): TALOS_MAINTENANCE_TIMEOUT (600) for a fresh node to
# answer, TALOS_INSTALL_TIMEOUT (1200) for an applied node to install and
# reboot, TALOS_AUTH_TIMEOUT (300) for a recorded node to answer.
#
# Output: one line per node, "<name>: <recorded|applied|recovered>".
# Exit status: 0 all installed; 1 a node failed or did not answer in time;
# 3 drift (a changed config, or a node in a state this cluster cannot
# explain). The insecure API is never used on a node with a journal record.
set -euo pipefail
PROG=apply-config
# shellcheck source=control-lib.sh
. "$(dirname -- "$0")/control-lib.sh"

parse_args "$@"
[ ${#ARGS[@]} -eq 0 ] || die "unknown argument: ${ARGS[0]}"
load_inputs
read_nodes
[ -n "${TALOS_INSTALL_DISK:-}" ] || die "TALOS_INSTALL_DISK is not set"
[ -r "${DISKS_FILE:-}" ] || die "cannot read the disk table ${DISKS_FILE:-(--disks missing)}"
need_state
lock

MAINT_TIMEOUT=${TALOS_MAINTENANCE_TIMEOUT:-600}
INSTALL_TIMEOUT=${TALOS_INSTALL_TIMEOUT:-1200}
AUTH_TIMEOUT=${TALOS_AUTH_TIMEOUT:-300}

disk_bytes() { awk -v n="$1" '$1 == n {print $2; exit}' "$DISKS_FILE"; }

# check_link NAME ADDR MAC: the reserved MAC is one link's permanent address
check_link() {
    local name=$1 addr=$2 mac=$3 rows matches
    rows=$(tmaint "$addr" get links -o yaml | yaml_rows permanentAddr hardwareAddr) \
        || die "$name: cannot list its links through the maintenance API"
    matches=$(awk -v m="$mac" '$2 == m' <<< "$rows")
    case "$(grep -c . <<< "$matches" || true)" in
        1) log "$name: link $(awk '{print $1}' <<< "$matches") has permanent_addr $mac (the reserved MAC): the generated LinkAliasConfig selector matches it"
           printf '%s\n' "$rows" > "$JOURNAL/$name.links" ;;
        0) if awk -v m="$mac" '$3 == m {f=1} END {exit !f}' <<< "$rows"; then
               drift "$name: a link carries $mac as its hardware address but not as permanent_addr; the generated selector mac(link.permanent_addr) would not match it. Links: $(tr '\n' ';' <<< "$rows")"
           fi
           drift "$name: no link has the reserved MAC $mac; this is not the node the reservation names. Links: $(tr '\n' ';' <<< "$rows")" ;;
        *) drift "$name: several links have permanent_addr $mac" ;;
    esac
}

# check_disk NAME ADDR: the install disk is the empty disk boxman created
check_disk() {
    local name=$1 addr=$2 want rows row id size ro cdrom vols used
    want=$(disk_bytes "$name")
    [[ $want =~ ^[0-9]+$ ]] || die "$name: no boot disk size in $DISKS_FILE"
    rows=$(tmaint "$addr" get disks -o yaml | yaml_rows dev_path size readonly cdrom) \
        || die "$name: cannot list its disks through the maintenance API"
    row=$(awk -v d="$TALOS_INSTALL_DISK" '$2 == d' <<< "$rows")
    [ -n "$row" ] || drift "$name: has no $TALOS_INSTALL_DISK; disks: $(tr '\n' ';' <<< "$rows")"
    read -r id _ size ro cdrom <<< "$row"
    [ "$size" = "$want" ] || drift "$name: $TALOS_INSTALL_DISK is $size bytes, not the $want-byte disk boxman created for it; refusing to install on it"
    [ "$ro" = false ] && [ "$cdrom" = false ] || drift "$name: $TALOS_INSTALL_DISK is read-only or a CD-ROM"
    # a partition of the disk, or a filesystem or partition table on it,
    # means it is not the empty disk boxman created
    if vols=$(tmaint "$addr" get discoveredvolumes -o yaml 2>/dev/null | yaml_rows dev_path parent name); then
        used=$(awk -v d="$TALOS_INSTALL_DISK" -v id="$id" '
            ($2 == d && $4 != "-") || ($2 != d && index($2, d) == 1) || $3 == id {print $2}' <<< "$vols")
        [ -z "$used" ] || drift "$name: $TALOS_INSTALL_DISK is not empty (${used//$'\n'/ }); only a newly created, empty disk is installed on"
        log "$name: $TALOS_INSTALL_DISK ($id) is the empty $size-byte disk boxman created"
    else
        log "$name: $TALOS_INSTALL_DISK ($id) is the $size-byte disk boxman created (volumes are not listable in maintenance mode)"
    fi
}

# --- classify every node, check the fresh ones, apply ------------------------
APPLIED=()   # nodes whose config was sent in this run, to wait for
for i in "${!NAMES[@]}"; do
    name=${NAMES[$i]} addr=${IPS[$i]} mac=${MACS[$i]}
    cfg=$STATE/nodes/$name.yaml
    [ -r "$cfg" ] || die "$name: no config $cfg; run gen-config.sh first"
    sha=$(sha256sum < "$cfg" | awk '{print $1}')
    done_sha=$(journal_get "$name.applied")
    intent_sha=$(journal_get "$name.applying")

    if [ -n "$done_sha" ]; then
        wait_for "$AUTH_TIMEOUT" "$name: authenticated API answers" auth_ok "$addr" \
            || die "$name was configured by this cluster but its authenticated API at $addr does not answer within ${AUTH_TIMEOUT}s; not retrying in insecure mode (check the VM is running: talosctl -e $addr -n $addr dmesg)"
        [ "$sha" = "$done_sha" ] \
            || drift "$name: its config changed since it was applied (applied ${done_sha:0:12}, now ${sha:0:12}); v0 does not update installed nodes. Revert the change, or apply it deliberately: talosctl --talosconfig $TC -e $addr -n $addr apply-config --file $cfg --dry-run, then without --dry-run"
        echo "$name: recorded"
        continue
    fi

    if [ -n "$intent_sha" ]; then
        if auth_ok "$addr"; then
            [ "$intent_sha" = "$sha" ] \
                || drift "$name: an interrupted apply sent config ${intent_sha:0:12}, but the published one is now ${sha:0:12}"
            journal_put "$name.applied" "$sha"
            rm -f "$JOURNAL/$name.applying"
            log "$name: the interrupted apply took effect; recorded"
            echo "$name: recovered"
            continue
        elif maint_ok "$addr"; then
            log "$name: an earlier apply never took effect (the node is still in maintenance mode); treating it as fresh"
        else
            die "$name: an apply was sent at $(stat -c %y "$JOURNAL/$name.applying" 2>/dev/null || echo ?) but the node answers neither API; rerun once it has finished booting"
        fi
    fi

    # fresh: never configured by this cluster
    wait_for "$MAINT_TIMEOUT" "$name: maintenance API answers at $addr" maint_ok "$addr" || {
        if auth_ok "$addr"; then
            drift "$name answers the authenticated API with this cluster's CA, but this state has no record of configuring it; restore $JOURNAL from the backup instead of applying again"
        fi
        die "$name: no maintenance API at $addr within ${MAINT_TIMEOUT}s (is the VM running and booted from the Talos ISO?)"
    }
    ! auth_ok "$addr" || drift "$name answers both APIs; refusing to guess"
    check_link "$name" "$addr" "$mac"
    check_disk "$name" "$addr"
    journal_put "$name.applying" "$sha"
    log "$name: applying $cfg (${sha:0:12}) through the maintenance API"
    tmaint "$addr" apply-config --file "$cfg" >&2 \
        || die "$name: apply-config failed; the journal keeps the attempt, a rerun checks which API the node answers before doing anything"
    APPLIED+=("$i")
done

# --- wait for every applied node to install, reboot and authenticate ---------
for i in "${APPLIED[@]}"; do
    name=${NAMES[$i]} addr=${IPS[$i]}
    wait_for "$INSTALL_TIMEOUT" "$name: installed; the authenticated API answers at $addr" auth_ok "$addr" \
        || die "$name: did not come back on the authenticated API within ${INSTALL_TIMEOUT}s after its config was applied; a rerun resumes from the journal"
    journal_put "$name.applied" "$(journal_get "$name.applying")"
    rm -f "$JOURNAL/$name.applying"
    echo "$name: applied"
done
