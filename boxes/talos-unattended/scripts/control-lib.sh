# shellcheck shell=bash
# shellcheck disable=SC2034  # the variables set here are read by the sourcing scripts
# Shared by the control-VM scripts of boxes/talos-unattended (apply-config.sh,
# bootstrap.sh, flux.sh, status.sh). Sourced, never run; the caller sets PROG.
#
# Every Talos call names its endpoint and node explicitly (-e ADDR -n ADDR):
# a control plane's or worker's own address, never the VIP, and nothing from
# a default talosconfig context. Authenticated calls use the cluster's
# talosconfig from TALOS_STATE_DIR; the unauthenticated maintenance API
# (--insecure) is only ever used on a node with no record of being
# configured. Nothing secret is printed: commands and paths only.

HERE=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

log() { printf '%s: %s\n' "${PROG:-talos}" "$*" >&2; }
die() { printf '%s: error: %s\n' "${PROG:-talos}" "$*" >&2; exit "${DIE_STATUS:-1}"; }
# drift: the cluster is not in a state this driver produced or can explain
drift() { DIE_STATUS=3 die "$@"; }

# parse_args "$@": --inputs FILE --nodes FILE [--disks FILE]
parse_args() {
    INPUTS="" NODES_FILE="" DISKS_FILE="" ARGS=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --inputs) INPUTS=${2:?--inputs needs a file}; shift ;;
            --nodes)  NODES_FILE=${2:?--nodes needs a file}; shift ;;
            --disks)  DISKS_FILE=${2:?--disks needs a file}; shift ;;
            -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2 ;;
            *) ARGS+=("$1") ;;
        esac
        shift
    done
}

# load_inputs: versions.env beside scripts/ as defaults, then --inputs
load_inputs() {
    local versions=$HERE/../versions.env line key
    if [ -r "$versions" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in ''|'#'*) continue ;; esac
            [[ $line =~ ^([A-Z][A-Z0-9_]*)=([A-Za-z0-9._:/@+-]*)$ ]] || die "$versions: bad line: $line"
            key=${BASH_REMATCH[1]}
            [ -n "${!key+set}" ] || export "$key=${BASH_REMATCH[2]}"
        done < "$versions"
    fi
    if [ -n "$INPUTS" ]; then
        [ -r "$INPUTS" ] || die "cannot read inputs file $INPUTS"
        set -a
        # shellcheck disable=SC1090
        . "$INPUTS"
        set +a
    fi
    [ -n "${TALOS_STATE_DIR:-}" ] || die "TALOS_STATE_DIR is not set"
    # read only now, from the shipped inputs: the control VM inherits nothing
    # from the host's environment. Every talosctl and kubectl call is bounded,
    # so a hung request cannot hold a stage past its own timeout:
    # TALOS_CALL_TIMEOUT (60 s) kills a call, and kubectl also gets a
    # per-request timeout (TALOS_REQUEST_TIMEOUT, 30 s)
    POLL=${TALOS_POLL_SECONDS:-10}
    CALL_TIMEOUT=${TALOS_CALL_TIMEOUT:-60}
    REQUEST_TIMEOUT=${TALOS_REQUEST_TIMEOUT:-30}
    # a zero bound is no bound (timeout(1) and kubectl both read 0 as "never"):
    # every timeout must be a positive number of seconds; polling may be 0
    [[ $POLL =~ ^[0-9]+$ ]] || die "TALOS_POLL_SECONDS must be a whole number of seconds"
    local var
    for var in TALOS_CALL_TIMEOUT TALOS_REQUEST_TIMEOUT TALOS_MAINTENANCE_TIMEOUT TALOS_INSTALL_TIMEOUT \
               TALOS_AUTH_TIMEOUT TALOS_ETCD_TIMEOUT TALOS_KUBE_TIMEOUT TALOS_FLUX_TIMEOUT TALOS_PREBOOT_CHECKS; do
        [ -z "${!var:-}" ] || [[ ${!var} =~ ^[1-9][0-9]*$ ]] || die "$var must be a positive whole number, got ${!var}"
    done
    STATE=$TALOS_STATE_DIR
    TC=$STATE/talosconfig
    JOURNAL=$STATE/journal
}

# read_nodes: NAMES ROLES IPS MACS from --nodes, CP_* for the control planes
read_nodes() {
    local name role ip mac extra
    [ -r "$NODES_FILE" ] || die "cannot read node table ${NODES_FILE:-(--nodes missing)}"
    NAMES=(); ROLES=(); IPS=(); MACS=(); CP_NAMES=(); CP_IPS=()
    while read -r name role ip mac extra; do
        case "$name" in ''|'#'*) continue ;; esac
        [ -z "${extra:-}" ] && [ -n "${mac:-}" ] || die "node table: bad line: $name $role $ip $mac"
        NAMES+=("$name"); ROLES+=("$role"); IPS+=("$ip"); MACS+=("$mac")
        if [ "$role" = controlplane ]; then CP_NAMES+=("$name"); CP_IPS+=("$ip"); fi
    done < "$NODES_FILE"
    [ ${#NAMES[@]} -gt 0 ] || die "node table $NODES_FILE is empty"
    [ ${#CP_IPS[@]} -gt 0 ] || die "node table $NODES_FILE has no control plane"
    if [ -n "${TALOS_VIP:-}" ]; then
        local ip
        for ip in "${IPS[@]}"; do [ "$ip" != "$TALOS_VIP" ] || die "node table uses the VIP $TALOS_VIP as a node address"; done
    fi
}

# need_state: the generated state this script works from
need_state() {
    [ -d "$STATE" ] || die "no state in $STATE; run gen-config.sh first"
    [ -r "$TC" ] || die "no talosconfig in $STATE; run gen-config.sh first"
    ( umask 077 && mkdir -p "$JOURNAL" ) && chmod 0700 "$JOURNAL" || die "cannot create $JOURNAL"
    [ -r "$JOURNAL" ] && [ -x "$JOURNAL" ] && [ -w "$JOURNAL" ] || die "cannot read and write the journal $JOURNAL"
}

# lock: one driver step at a time on this state (shared with gen-config.sh)
lock() {
    exec 9>"$STATE/.lock"
    flock -n 9 || die "another run holds $STATE/.lock"
}

# journal_get NAME / journal_put NAME VALUE: small atomic records
# A missing entry reads empty; one that exists but cannot be read, or is
# empty, stops the run: unreadable is never the same as "never happened".
# Call it in a plain assignment (x=$(journal_get NAME)), never inside a
# condition, so its failure stops the caller too.
journal_get() {
    local f=$JOURNAL/$1 value
    if [ -e "$f" ] || [ -L "$f" ]; then
        [ -f "$f" ] && [ ! -L "$f" ] && [ -r "$f" ] \
            || die "cannot read the journal entry $f; refusing to guess what happened"
        value=$(cat -- "$f") || die "cannot read the journal entry $f; refusing to guess what happened"
        [ -n "$value" ] || die "the journal entry $f is empty; refusing to guess what happened"
        printf '%s\n' "$value"
    elif [ ! -d "$JOURNAL" ] || [ ! -x "$JOURNAL" ]; then
        die "cannot read the journal $JOURNAL; refusing to guess what happened"
    fi
}
# journal_put NAME VALUE: written, flushed, renamed into place and read back,
# each step checked; any failure stops the run before the caller acts
journal_put() {
    local f=$JOURNAL/$1 tmp=$JOURNAL/.$1.tmp
    [ -n "$2" ] || die "refusing to journal an empty $1"
    rm -f -- "$tmp" || die "cannot write $f (a stale $tmp is in the way)"
    printf '%s\n' "$2" > "$tmp" || { rm -f -- "$tmp"; die "cannot write $f"; }
    sync -- "$tmp" || { rm -f -- "$tmp"; die "cannot flush $f"; }
    mv -f -- "$tmp" "$f" || { rm -f -- "$tmp"; die "cannot write $f"; }
    sync -- "$JOURNAL" || die "cannot flush $JOURNAL"
    [ "$(cat -- "$f" 2>/dev/null)" = "$2" ] || die "$f did not keep what was written"
}
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# authenticated Talos API of the node at ADDR, never through the VIP
tauth() { local addr=$1; shift; timeout --kill-after=10 "$CALL_TIMEOUT" talosctl --talosconfig "$TC" -e "$addr" -n "$addr" "$@"; }
# the unauthenticated maintenance API: only for nodes never configured
tmaint() { local addr=$1; shift; timeout --kill-after=10 "$CALL_TIMEOUT" talosctl -n "$addr" "$@" --insecure; }
# kubectl on this cluster's own kubeconfig, bounded per request and per call
kc() {
    timeout --kill-after=10 "$CALL_TIMEOUT" \
        kubectl --kubeconfig "$STATE/kubeconfig" --request-timeout="${REQUEST_TIMEOUT}s" "$@"
}
# kcw SECONDS ARGS: a kubectl wait or rollout status with its own --timeout,
# killed if it outlives that by more than a call's bound
kcw() {
    local t=$1; shift
    timeout --kill-after=10 $((t + CALL_TIMEOUT)) kubectl --kubeconfig "$STATE/kubeconfig" "$@" --timeout="${t}s"
}

# auth_ok ADDR: the node answers the authenticated API with this cluster's CA
auth_ok() { tauth "$1" version >/dev/null 2>&1; }
# maint_ok ADDR: the node answers the maintenance API (it is unconfigured)
maint_ok() { tmaint "$1" get disks -o yaml >/dev/null 2>&1; }

# wait_for SECONDS DESCRIPTION COMMAND...: poll until COMMAND succeeds
wait_for() {
    local timeout=$1 what=$2 deadline; shift 2
    deadline=$((SECONDS + timeout))
    until "$@"; do
        [ $SECONDS -lt $deadline ] || return 1
        sleep "$POLL"
    done
    log "$what"
}

# yaml_rows FIELD...: one line per resource of `talosctl get -o yaml` on
# stdin: metadata.id then each spec FIELD ("-" when missing)
yaml_rows() {
    python3 -c '
import sys, yaml
fields = sys.argv[1:]
for doc in yaml.safe_load_all(sys.stdin):
    if doc is None:
        continue
    if not isinstance(doc, dict) or not isinstance(doc.get("spec"), dict):
        sys.exit("not a talosctl resource listing")
    spec = doc.get("spec") or {}
    row = [str((doc.get("metadata") or {}).get("id", "-"))]
    for f in fields:
        value = spec.get(f)
        row.append("-" if value in (None, "") else str(value).lower())
    print(" ".join(row))
' "$@"
}
