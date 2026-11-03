#!/usr/bin/env bash
# Bootstrap etcd exactly once, then wait for a healthy, usable cluster.
#
#   bootstrap.sh --inputs FILE --nodes FILE
#
# Runs on the control VM after apply-config.sh. Every Talos call goes to a
# control plane's own address (-e ADDR -n ADDR), never the VIP: the VIP only
# exists once etcd does.
#
# etcd, under the state lock and a journal (journal/bootstrap.intent, .done):
#   done        recorded earlier: etcd must answer on the bootstrap node
#               (bounded wait). Nothing is sent.
#   intent only a bootstrap was requested but its outcome never recorded
#               (interrupted, or the response was lost). etcd is inspected:
#               members on the bootstrap node mean it worked, and that is
#               recorded; none within TALOS_ETCD_TIMEOUT is ambiguous and the
#               run stops with what to check. It never bootstraps again.
#   neither     if any control plane already reports etcd members, the
#               cluster exists and is recorded as such. Otherwise a missing
#               journal is not taken as "never bootstrapped": every control
#               plane must positively show etcd waiting for its bootstrap
#               (`talosctl service etcd` in state Preparing, no members, and
#               an empty etcd data directory, read with `talosctl list`) on
#               TALOS_PREBOOT_CHECKS (3) consecutive observations, and the
#               bootstrap node must report synchronised time. Anything else
#               (etcd running or failed without members, a query that does
#               not answer) stops the run. Then the intent is journaled, the
#               bootstrap RPC goes to the bootstrap node (the first control
#               plane) only, and success is recorded once etcd reports
#               members. A journal entry that cannot be read stops the run.
#
# Health and access, bounded by TALOS_KUBE_TIMEOUT (900 s):
#   - etcd has one member per control plane;
#   - a kubeconfig for this cluster in TALOS_STATE_DIR/kubeconfig (0600),
#     fetched from the bootstrap node; an existing one that works is kept;
#   - the API answers through the VIP; every node registered and Ready;
#   - CoreDNS and the CNI (TALOS_CNI_DAEMONSET, default kube-flannel) rolled
#     out;
#   - a small test Deployment (namespace talos-smoke, image
#     TALOS_SMOKE_IMAGE) scheduled and Running.
#
# Exit status: 0 healthy; 1 a stage failed or timed out (the message names
# it); 3 ambiguous etcd state, which needs a person.
set -euo pipefail
PROG=bootstrap
# shellcheck source=control-lib.sh
. "$(dirname -- "$0")/control-lib.sh"

parse_args "$@"
[ ${#ARGS[@]} -eq 0 ] || die "unknown argument: ${ARGS[0]}"
load_inputs
read_nodes
need_state
lock

ETCD_TIMEOUT=${TALOS_ETCD_TIMEOUT:-600}
AUTH_TIMEOUT=${TALOS_AUTH_TIMEOUT:-300}
KUBE_TIMEOUT=${TALOS_KUBE_TIMEOUT:-900}
SMOKE_IMAGE=${TALOS_SMOKE_IMAGE:-registry.k8s.io/pause:3.10}
CNI=${TALOS_CNI_DAEMONSET:-kube-flannel}
BOOT_NAME=${CP_NAMES[0]} BOOT=${CP_IPS[0]}

# members ADDR: the number of etcd members ADDR reports (fails if none)
members() { tauth "$1" etcd members 2>/dev/null | awk 'NR > 1 && NF' | grep -c . ; }
has_members() { local n; n=$(members "$1") && [ "$n" -gt 0 ]; }
time_synced() { tauth "$1" get timestatus -o yaml 2>/dev/null | yaml_rows synced | awk '$2 == "true" {f=1} END {exit !f}'; }
# etcd_state ADDR: the etcd service's state on ADDR (empty if unknown)
etcd_state() { tauth "$1" service etcd 2>/dev/null | awk '$1 == "STATE" {print $2; exit}'; }

# etcd_data ADDR: "empty" when ADDR's etcd data directory is positively
# empty or absent, "data" when it holds anything, "unknown" otherwise. Read
# with the authenticated file API and parsed strictly (a NODE/NAME header,
# then one entry per line).
etcd_data() {
    local out rc=0
    out=$(tauth "$1" list /var/lib/etcd 2>&1) || rc=$?
    if [ $rc -ne 0 ]; then
        case "$out" in
            *"no such file or directory"*) echo empty ;;
            *) echo unknown ;;
        esac
        return
    fi
    awk 'NR == 1 { if ($1 != "NODE" || $NF != "NAME") bad = 1; next }
         NF == 0 { next }
         NF != 2 { bad = 1; next }
         $2 != "." { data = 1 }
         END { print bad ? "unknown" : data ? "data" : "empty" }' <<< "$out"
}

# preboot: every control plane positively before its first bootstrap,
# several times in a row: no etcd members, the etcd service waiting
# (Preparing), and an empty etcd data directory. The service phase alone is
# no proof (Talos is also Preparing while it restarts an existing member).
# EXISTING names a control plane that already has members.
PREBOOT_CHECKS=${TALOS_PREBOOT_CHECKS:-3}
preboot() {
    local i state data seen=""
    EXISTING=""
    for i in "${!CP_IPS[@]}"; do
        if has_members "${CP_IPS[$i]}"; then EXISTING=${CP_NAMES[$i]}; return 0; fi
        state=$(etcd_state "${CP_IPS[$i]}")
        data=$(etcd_data "${CP_IPS[$i]}")
        seen+=" ${CP_NAMES[$i]}=${state:-unknown}/data-$data"
        [ "$state" = Preparing ] && [ "$data" = empty ] || { PREBOOT_SEEN=$seen; PREBOOT_RUN=0; return 1; }
    done
    PREBOOT_SEEN=$seen
    PREBOOT_RUN=$((PREBOOT_RUN + 1))
    [ "$PREBOOT_RUN" -ge "$PREBOOT_CHECKS" ]
}

# ---- every control plane on the authenticated API ---------------------------
for i in "${!CP_IPS[@]}"; do
    wait_for "$AUTH_TIMEOUT" "${CP_NAMES[$i]}: authenticated API answers" auth_ok "${CP_IPS[$i]}" \
        || die "${CP_NAMES[$i]} (${CP_IPS[$i]}) does not answer the authenticated API within ${AUTH_TIMEOUT}s; run apply-config.sh first"
done

# ---- etcd, once -------------------------------------------------------------
# the journal, read in checked assignments: an entry that exists but cannot be
# read, is empty or is malformed stops the run instead of reading as absent
boot_done=$(journal_get bootstrap.done)
boot_intent=$(journal_get bootstrap.intent)
for rec in "$boot_done" "$boot_intent"; do
    [ -z "$rec" ] || [[ $rec =~ ^[a-z0-9-]+\ [0-9TZ:-]+(\ [a-z-]+)?$ ]] \
        || die "the bootstrap journal in $JOURNAL holds a malformed entry ($rec); refusing to guess what happened"
done
if [ -n "$boot_done" ]; then
    wait_for "$ETCD_TIMEOUT" "etcd: bootstrapped earlier ($boot_done); $BOOT_NAME reports members" has_members "$BOOT" \
        || die "etcd was bootstrapped ($boot_done) but $BOOT_NAME reports no members within ${ETCD_TIMEOUT}s; inspect: talosctl --talosconfig $TC -e $BOOT -n $BOOT service etcd"
elif [ -n "$boot_intent" ]; then
    log "etcd: a bootstrap was requested ($boot_intent) but never confirmed; inspecting instead of bootstrapping again"
    if wait_for "$ETCD_TIMEOUT" "etcd: $BOOT_NAME reports members: the earlier bootstrap took effect" has_members "$BOOT"; then
        journal_put bootstrap.done "$BOOT_NAME $(now) recovered"
    else
        drift "etcd: a bootstrap was requested at $boot_intent but $BOOT_NAME reports no members after ${ETCD_TIMEOUT}s. Not bootstrapping again. Inspect: talosctl --talosconfig $TC -e $BOOT -n $BOOT service etcd; ... logs etcd. Only if certain it never ran, remove $JOURNAL/bootstrap.intent and rerun"
    fi
else
    PREBOOT_RUN=0 PREBOOT_SEEN="" EXISTING=""
    wait_for "$AUTH_TIMEOUT" "etcd: no members yet; checked whether every control plane waits for its bootstrap" preboot \
        || drift "etcd: cannot establish that etcd was never bootstrapped (last seen:${PREBOOT_SEEN:- nothing}); the journal has no record of a bootstrap, but that alone never authorizes one. Inspect: talosctl --talosconfig $TC -e $BOOT -n $BOOT service etcd; restore $JOURNAL from the backup if it was lost"
    existing=$EXISTING
    [ -n "$existing" ] || log "etcd: never bootstrapped: seen $PREBOOT_CHECKS times in a row:$PREBOOT_SEEN, no members"
    if [ -n "$existing" ]; then
        log "etcd: $existing already reports members; the cluster exists, no bootstrap"
        journal_put bootstrap.done "$existing $(now) found-existing"
    else
        wait_for "$AUTH_TIMEOUT" "etcd: $BOOT_NAME reports synchronised time" time_synced "$BOOT" \
            || die "$BOOT_NAME's time is not synchronised after ${AUTH_TIMEOUT}s; etcd needs it (check TALOS_NTP_SERVERS are reachable from the fabric network)"
        journal_put bootstrap.intent "$BOOT_NAME $(now)"
        log "etcd: bootstrapping on $BOOT_NAME ($BOOT), once"
        if ! tauth "$BOOT" bootstrap >&2; then
            die "etcd: the bootstrap RPC to $BOOT_NAME failed; the journal records the request, and a rerun inspects etcd instead of bootstrapping again"
        fi
        wait_for "$ETCD_TIMEOUT" "etcd: $BOOT_NAME reports members" has_members "$BOOT" \
            || die "etcd: bootstrapped on $BOOT_NAME but no members after ${ETCD_TIMEOUT}s; a rerun inspects etcd instead of bootstrapping again"
        journal_put bootstrap.done "$BOOT_NAME $(now) bootstrapped"
    fi
fi

# ---- health and access ------------------------------------------------------
all_members() { local n; n=$(members "$BOOT") && [ "$n" -eq ${#CP_IPS[@]} ]; }
wait_for "$KUBE_TIMEOUT" "etcd: ${#CP_IPS[@]} member(s), one per control plane" all_members \
    || die "etcd: $BOOT_NAME reports $(members "$BOOT" || echo 0) member(s), expected ${#CP_IPS[@]}, after ${KUBE_TIMEOUT}s"

KC=$STATE/kubeconfig
api_ok() { kc get --raw=/readyz >/dev/null 2>&1; }
fetch_kubeconfig() {
    local tmp
    tmp=$(mktemp -d "$STATE/.staging.XXXXXX") || return 1
    if tauth "$BOOT" kubeconfig "$tmp/kubeconfig" >/dev/null 2>&1; then
        chmod 0600 "$tmp/kubeconfig" && mv -f "$tmp/kubeconfig" "$KC"
        rmdir "$tmp"
        return 0
    fi
    rm -rf "$tmp"
    return 1
}
if [ -s "$KC" ] && api_ok; then
    log "kubeconfig: keeping $KC (it works)"
else
    wait_for "$KUBE_TIMEOUT" "kubeconfig: fetched into $KC (0600)" fetch_kubeconfig \
        || die "kubeconfig: could not fetch it from $BOOT_NAME within ${KUBE_TIMEOUT}s"
fi
wait_for "$KUBE_TIMEOUT" "kubernetes: the API answers through the endpoint" api_ok \
    || die "kubernetes: the API at the cluster endpoint (the VIP) does not answer within ${KUBE_TIMEOUT}s"

registered() { [ "$(kc get nodes --no-headers 2>/dev/null | grep -c .)" -eq ${#NAMES[@]} ]; }
wait_for "$KUBE_TIMEOUT" "kubernetes: all ${#NAMES[@]} node(s) registered" registered \
    || die "kubernetes: $(kc get nodes --no-headers 2>/dev/null | grep -c . || true) of ${#NAMES[@]} nodes registered after ${KUBE_TIMEOUT}s"
kcw "$KUBE_TIMEOUT" wait --for=condition=Ready nodes --all >&2 \
    || die "kubernetes: not every node is Ready after ${KUBE_TIMEOUT}s"
log "kubernetes: every node Ready"
kcw "$KUBE_TIMEOUT" -n kube-system rollout status deployment/coredns >&2 \
    || die "kubernetes: CoreDNS did not roll out within ${KUBE_TIMEOUT}s"
kcw "$KUBE_TIMEOUT" -n kube-system rollout status "daemonset/$CNI" >&2 \
    || die "kubernetes: the CNI daemonset $CNI did not roll out within ${KUBE_TIMEOUT}s"
log "kubernetes: CoreDNS and $CNI are running"

kc apply -f - >&2 <<EOF || die "kubernetes: cannot apply the test Deployment"
apiVersion: v1
kind: Namespace
metadata:
  name: talos-smoke
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: smoke
  namespace: talos-smoke
spec:
  replicas: 1
  selector:
    matchLabels: {app: smoke}
  template:
    metadata:
      labels: {app: smoke}
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 65535
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: pause
          image: $SMOKE_IMAGE
          resources:
            requests: {cpu: 10m, memory: 16Mi}
            limits: {memory: 32Mi}
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
EOF
kcw "$KUBE_TIMEOUT" -n talos-smoke rollout status deployment/smoke >&2 \
    || die "kubernetes: the test Deployment did not become available within ${KUBE_TIMEOUT}s"
running=$(kc -n talos-smoke get pods -l app=smoke \
    -o jsonpath='{range .items[*]}{.metadata.name} {.status.phase} {.spec.nodeName}{"\n"}{end}' \
    | awk '$2 == "Running" && $3 != ""')
[ -n "$running" ] || die "kubernetes: the test Deployment has no Running, scheduled pod"
log "kubernetes: test pod running: $(head -1 <<< "$running")"
echo "cluster: healthy"
