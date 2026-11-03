#!/usr/bin/env bash
# Report the lab's state, read-only.
#
#   status.sh [--inputs FILE]                       on the host
#   status.sh --control --inputs FILE --nodes FILE  on the control VM
#
# On the host: each VM's libvirt state and whether its UUID is the one the
# install driver recorded, the cluster identity's fingerprint in the backup,
# then the control VM's view, fetched over the verified ssh channel
# (control-exec.sh). On the control VM: the identity fingerprint, each node's
# recorded config and authenticated API, the etcd bootstrap journal and
# member count, Kubernetes nodes, CoreDNS, the CNI and the test pod, and
# Flux when it is on. Nothing is changed, nothing secret is printed.
#
# Exit status: 0 every check healthy, 1 otherwise.
set -uo pipefail
PROG=status

if [ "${1:-}" = --control ]; then
    shift
    # shellcheck source=control-lib.sh
    . "$(dirname -- "$0")/control-lib.sh"
    parse_args "$@"
    load_inputs
    read_nodes
    bad=0
    say() { printf '%-10s %s\n' "$1" "$2"; }
    flag() { bad=1; say "$1" "$2"; }
    if [ -r "$STATE/secrets.sha256" ]; then
        say identity "secrets bundle sha256 $(awk '{print substr($1, 1, 16)}' "$STATE/secrets.sha256")..."
    else
        flag identity "no secrets bundle in $STATE"
    fi
    for i in "${!NAMES[@]}"; do
        name=${NAMES[$i]} addr=${IPS[$i]}
        applied=$(journal_get "$name.applied")
        if [ -z "$applied" ]; then
            flag "$name" "not configured by this cluster"
        elif auth_ok "$addr"; then
            say "$name" "${ROLES[$i]} $addr: config ${applied:0:12} applied, authenticated API answers"
        else
            flag "$name" "${ROLES[$i]} $addr: config ${applied:0:12} applied, authenticated API DOES NOT answer"
        fi
    done
    boot=$(journal_get bootstrap.done)
    if [ -z "$boot" ]; then
        flag etcd "not bootstrapped$( [ -z "$(journal_get bootstrap.intent)" ] || echo " (an unconfirmed request is journaled)")"
    else
        n=$(tauth "${CP_IPS[0]}" etcd members 2>/dev/null | awk 'NR > 1 && NF' | grep -c . || true)
        if [ "$n" -eq ${#CP_IPS[@]} ]; then say etcd "bootstrapped once ($boot); $n member(s)"
        else flag etcd "bootstrapped once ($boot); $n member(s), expected ${#CP_IPS[@]}"; fi
    fi
    if [ -s "$STATE/kubeconfig" ] && kc --request-timeout=10s get --raw=/readyz >/dev/null 2>&1; then
        nodes=$(kc get nodes --no-headers 2>/dev/null)
        ready=$(awk '$2 == "Ready"' <<< "$nodes" | grep -c . || true)
        if [ "$ready" -eq ${#NAMES[@]} ]; then say kube "$ready/${#NAMES[@]} nodes Ready"
        else flag kube "$ready/${#NAMES[@]} nodes Ready"; fi
        for target in deployment/coredns "daemonset/${TALOS_CNI_DAEMONSET:-kube-flannel}"; do
            if kc -n kube-system rollout status "$target" --timeout=10s >/dev/null 2>&1; then say kube "$target rolled out"
            else flag kube "$target NOT rolled out"; fi
        done
        pod=$(kc -n talos-smoke get pods -l app=smoke \
            -o jsonpath='{range .items[*]}{.metadata.name} {.status.phase} {.spec.nodeName}{"\n"}{end}' 2>/dev/null | head -1)
        case "$pod" in
            *" Running "?*) say kube "test pod: $pod" ;;
            *) flag kube "test pod: ${pod:-none}" ;;
        esac
    else
        flag kube "no working kubeconfig in $STATE (the API does not answer)"
    fi
    if [ "${TALOS_FLUX:-off}" = on ]; then
        if kc -n flux-system wait --for=condition=Ready gitrepository/flux-system kustomization/flux-system --timeout=10s >/dev/null 2>&1; then
            say flux "root GitRepository and Kustomization Ready"
        else flag flux "root GitRepository/Kustomization NOT Ready"; fi
    else
        say flux "off"
    fi
    exit $bad
fi

set -e
# shellcheck source=lib.sh
. "$(dirname -- "$0")/lib.sh"
INPUTS=""
[ "${1:-}" != --inputs ] || { INPUTS=${2:?--inputs needs a file}; shift 2; }
load_inputs "$INPUTS"
read_facts
bad=0
for i in "${!VM_DOMAIN[@]}"; do
    dom=${VM_DOMAIN[$i]} want=$(record_get "uuid.${VM_NAME[$i]}")
    state=$(domain_state "$dom")
    if [ "$state" = absent ]; then
        printf '%-10s %s\n' "${VM_NAME[$i]}" "absent"; bad=1
    elif [ -n "$want" ] && [ "$(domain_uuid "$dom")" != "$want" ]; then
        printf '%-10s %s\n' "${VM_NAME[$i]}" "$state, NOT the domain this driver created"; bad=1
    else
        printf '%-10s %s\n' "${VM_NAME[$i]}" "$state${want:+, uuid as recorded}"
        [ "$state" = running ] || bad=1
    fi
done
if [ -r "$TALOS_BACKUP_DIR/state/secrets.sha256" ]; then
    printf '%-10s %s\n' backup "secrets bundle sha256 $(awk '{print substr($1, 1, 16)}' "$TALOS_BACKUP_DIR/state/secrets.sha256")... in $TALOS_BACKUP_DIR"
else
    printf '%-10s %s\n' backup "no backup of the identity yet"; bad=1
fi
if "$SCRIPTS/control-exec.sh" ${INPUTS:+--inputs "$INPUTS"} verify 2>/dev/null; then
    "$SCRIPTS/control-exec.sh" ${INPUTS:+--inputs "$INPUTS"} run -- \
        "$REMOTE_BOX/scripts/status.sh" --control --inputs "$REMOTE_BOX/control.env" --nodes "$REMOTE_BOX/nodes.txt" \
        || bad=1
else
    printf '%-10s %s\n' control "not reachable through the verified channel"; bad=1
fi
exit $bad
