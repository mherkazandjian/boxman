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
#               cluster exists and is recorded as such. Otherwise every
#               control plane must authenticate and the bootstrap node must
#               report synchronised time; the intent is journaled, then the
#               bootstrap RPC goes to the bootstrap node (the first control
#               plane) only, and success is recorded once etcd reports
#               members.
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

# ---- every control plane on the authenticated API ---------------------------
for i in "${!CP_IPS[@]}"; do
    wait_for "$AUTH_TIMEOUT" "${CP_NAMES[$i]}: authenticated API answers" auth_ok "${CP_IPS[$i]}" \
        || die "${CP_NAMES[$i]} (${CP_IPS[$i]}) does not answer the authenticated API within ${AUTH_TIMEOUT}s; run apply-config.sh first"
done

# ---- etcd, once -------------------------------------------------------------
if [ -n "$(journal_get bootstrap.done)" ]; then
    wait_for "$ETCD_TIMEOUT" "etcd: bootstrapped earlier ($(journal_get bootstrap.done)); $BOOT_NAME reports members" has_members "$BOOT" \
        || die "etcd was bootstrapped ($(journal_get bootstrap.done)) but $BOOT_NAME reports no members within ${ETCD_TIMEOUT}s; inspect: talosctl --talosconfig $TC -e $BOOT -n $BOOT service etcd"
elif [ -n "$(journal_get bootstrap.intent)" ]; then
    log "etcd: a bootstrap was requested ($(journal_get bootstrap.intent)) but never confirmed; inspecting instead of bootstrapping again"
    if wait_for "$ETCD_TIMEOUT" "etcd: $BOOT_NAME reports members: the earlier bootstrap took effect" has_members "$BOOT"; then
        journal_put bootstrap.done "$BOOT_NAME $(now) recovered"
    else
        drift "etcd: a bootstrap was requested at $(journal_get bootstrap.intent) but $BOOT_NAME reports no members after ${ETCD_TIMEOUT}s. Not bootstrapping again. Inspect: talosctl --talosconfig $TC -e $BOOT -n $BOOT service etcd; ... logs etcd. Only if certain it never ran, remove $JOURNAL/bootstrap.intent and rerun"
    fi
else
    existing=""
    for i in "${!CP_IPS[@]}"; do
        if has_members "${CP_IPS[$i]}"; then existing=${CP_NAMES[$i]}; break; fi
    done
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
api_ok() { kc --request-timeout=10s get --raw=/readyz >/dev/null 2>&1; }
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
kc wait --for=condition=Ready nodes --all --timeout="${KUBE_TIMEOUT}s" >&2 \
    || die "kubernetes: not every node is Ready after ${KUBE_TIMEOUT}s"
log "kubernetes: every node Ready"
kc -n kube-system rollout status deployment/coredns --timeout="${KUBE_TIMEOUT}s" >&2 \
    || die "kubernetes: CoreDNS did not roll out within ${KUBE_TIMEOUT}s"
kc -n kube-system rollout status "daemonset/$CNI" --timeout="${KUBE_TIMEOUT}s" >&2 \
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
kc -n talos-smoke rollout status deployment/smoke --timeout="${KUBE_TIMEOUT}s" >&2 \
    || die "kubernetes: the test Deployment did not become available within ${KUBE_TIMEOUT}s"
running=$(kc -n talos-smoke get pods -l app=smoke \
    -o jsonpath='{range .items[*]}{.metadata.name} {.status.phase} {.spec.nodeName}{"\n"}{end}' \
    | awk '$2 == "Running" && $3 != ""')
[ -n "$running" ] || die "kubernetes: the test Deployment has no Running, scheduled pod"
log "kubernetes: test pod running: $(head -1 <<< "$running")"
echo "cluster: healthy"
