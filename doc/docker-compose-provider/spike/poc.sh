#!/usr/bin/env bash
#
# Netfilter/iptables spike for the docker-compose provider.
# Phase 0 deliverable — issue #48 (epic #42).
#
# Question answered empirically: can boxman keep the host-global
# bridge-nf-call-iptables=1 and still pass VM<->container L2 traffic on a
# shared bridge, using per-bridge scoped iptables rules instead of the
# global disable currently done by src/boxman/netlab/shared_bridges.py?
#
# Usage (run on a lab host, ideally one with docker installed):
#   sudo bash poc.sh                          # non-disruptive scenarios
#   sudo bash poc.sh --with-docker-restart    # + docker restart scenarios (4b, 5)
#   sudo bash poc.sh --emulate-docker-policy  # force FORWARD policy DROP when
#                                             # the host lacks docker's default
#   sudo bash poc.sh --keep                   # keep artifacts for inspection
#
# Everything the script creates is prefixed "spike" and torn down on exit
# (unless --keep). The bridge-nf-call-iptables value and FORWARD policy are
# restored to their initial state.
#
# Record the output in findings.md next to this script.

set -u

# ── constants ────────────────────────────────────────────────────────────
BR=br-spike
NS_VM=spike-vm          # stand-in for a libvirt VM (netns + veth)
NS_CT=spike-ct          # stand-in for a container (netns + veth)
IP_VM=10.99.0.11
IP_CT=10.99.0.12
SUBNET=10.99.0.0/24
MACVLAN_NET=spike-macvlan
MACVLAN_CT=spike-mvct
IP_MV=10.99.0.66
HOST_BR_IP=10.99.0.1/24
SHIM=spike-shim
SHIM_IP=10.99.0.2/32
NF=/proc/sys/net/bridge/bridge-nf-call-iptables

WITH_DOCKER_RESTART=0
EMULATE_POLICY=0
KEEP=0
for arg in "$@"; do
    case "$arg" in
        --with-docker-restart)   WITH_DOCKER_RESTART=1 ;;
        --emulate-docker-policy) EMULATE_POLICY=1 ;;
        --keep)                  KEEP=1 ;;
        *) echo "unknown flag: $arg" >&2; exit 64 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "must run as root (sudo)" >&2; exit 77; }

# ── result bookkeeping ───────────────────────────────────────────────────
declare -a RESULTS=()
record() {  # record <scenario> <expected> <actual>
    local verdict
    if [ "$2" = "$3" ]; then verdict="as-expected"
    elif [ "$2" = "n/a" ]; then verdict="info"
    else verdict="DIFFERS"; fi
    RESULTS+=("$1|$2|$3|$verdict")
    printf '  -> %-28s expected=%-12s actual=%-12s [%s]\n' "$1" "$2" "$3" "$verdict"
}
skip() { RESULTS+=("$1|$2|skipped|skip"); printf '  -> %-28s SKIPPED (%s)\n' "$1" "$3"; }

# ── helpers ──────────────────────────────────────────────────────────────
vm_ping_ct()   { ip netns exec "$NS_VM" ping -c1 -W1 "$IP_CT" >/dev/null 2>&1; }
vm_ping_mv()   { ip netns exec "$NS_VM" ping -c1 -W1 "$IP_MV" >/dev/null 2>&1; }
mv_ping_vm()   { docker exec "$MACVLAN_CT" ping -c1 -W1 "$IP_VM" >/dev/null 2>&1; }
host_ping_mv() { ping -c1 -W1 "$IP_MV" >/dev/null 2>&1; }
verdict_of()   { if "$@"; then echo works; else echo blocked; fi; }

FWD_RULE=(-i "$BR" -o "$BR" -m physdev --physdev-is-bridged -j ACCEPT)
add_fwd_rule() { iptables -C FORWARD "${FWD_RULE[@]}" 2>/dev/null || iptables -I FORWARD 1 "${FWD_RULE[@]}"; }
del_fwd_rule() { while iptables -C FORWARD "${FWD_RULE[@]}" 2>/dev/null; do iptables -D FORWARD "${FWD_RULE[@]}"; done; }
have_docker_user() { iptables -nL DOCKER-USER >/dev/null 2>&1; }
add_du_rule()  { iptables -C DOCKER-USER "${FWD_RULE[@]}" 2>/dev/null || iptables -I DOCKER-USER 1 "${FWD_RULE[@]}"; }
del_du_rule()  { while iptables -C DOCKER-USER "${FWD_RULE[@]}" 2>/dev/null; do iptables -D DOCKER-USER "${FWD_RULE[@]}"; done; }

restart_docker() { systemctl restart docker 2>/dev/null || service docker restart; sleep 3; }

# ── cleanup ──────────────────────────────────────────────────────────────
cleanup() {
    [ "$KEEP" -eq 1 ] && { echo "--keep: leaving spike artifacts in place"; return; }
    echo "cleaning up..."
    del_fwd_rule || true
    have_docker_user && del_du_rule || true
    docker rm -f "$MACVLAN_CT"      >/dev/null 2>&1 || true
    docker network rm "$MACVLAN_NET" >/dev/null 2>&1 || true
    ip link del "$SHIM"             >/dev/null 2>&1 || true
    ip netns del "$NS_VM"           >/dev/null 2>&1 || true
    ip netns del "$NS_CT"           >/dev/null 2>&1 || true
    ip link del "$BR"               >/dev/null 2>&1 || true
    [ -n "${ORIG_NF:-}" ] && [ -e "$NF" ] && echo "$ORIG_NF" > "$NF"
    [ "${POLICY_EMULATED:-0}" -eq 1 ] && iptables -P FORWARD "$ORIG_FWD_POLICY"
}
trap cleanup EXIT

# ── environment capture (paste into findings.md) ────────────────────────
echo "=== environment ==="
uname -r
iptables --version
command -v docker >/dev/null && docker --version || echo "docker: not installed"
grep -h PRETTY_NAME /etc/os-release 2>/dev/null || true

# ── setup ────────────────────────────────────────────────────────────────
echo "=== setup ==="
modprobe br_netfilter 2>/dev/null || true
[ -e "$NF" ] || { echo "br_netfilter unavailable — spike meaningless without it" >&2; exit 1; }
ORIG_NF=$(cat "$NF")
ORIG_FWD_POLICY=$(iptables -S FORWARD | awk '/^-P FORWARD/{print $3}')
echo "initial bridge-nf-call-iptables=$ORIG_NF, FORWARD policy=$ORIG_FWD_POLICY"

HAVE_DOCKER=0
command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && HAVE_DOCKER=1

POLICY_EMULATED=0
if [ "$ORIG_FWD_POLICY" != "DROP" ]; then
    if [ "$EMULATE_POLICY" -eq 1 ]; then
        echo "emulating docker's FORWARD policy DROP (restored on exit)"
        iptables -P FORWARD DROP
        POLICY_EMULATED=1
    else
        echo "NOTE: FORWARD policy is $ORIG_FWD_POLICY (docker hosts use DROP);"
        echo "      baseline blockage may not reproduce — rerun with --emulate-docker-policy"
    fi
fi

ip link add name "$BR" type bridge
ip link set dev "$BR" up
for pair in "$NS_VM:$IP_VM" "$NS_CT:$IP_CT"; do
    ns=${pair%%:*}; ip=${pair##*:}
    ip netns add "$ns"
    ip link add "${ns}-h" type veth peer name "${ns}-n"
    ip link set "${ns}-n" netns "$ns"
    ip link set "${ns}-h" master "$BR" up
    ip -n "$ns" addr add "$ip/24" dev "${ns}-n"
    ip -n "$ns" link set "${ns}-n" up
    ip -n "$ns" link set lo up
done
echo "bridge $BR with $NS_VM($IP_VM) and $NS_CT($IP_CT) attached"

# ── scenario 1: baseline — nf=1, no rules → expect blocked ───────────────
echo "=== scenario 1: baseline (nf-call-iptables=1, no rules) ==="
echo 1 > "$NF"
if [ "$ORIG_FWD_POLICY" = "DROP" ] || [ "$POLICY_EMULATED" -eq 1 ]; then
    record "1-baseline-nf1" "blocked" "$(verdict_of vm_ping_ct)"
else
    skip "1-baseline-nf1" "blocked" "FORWARD policy not DROP on this host"
fi

# ── scenario 2: current behavior — global nf=0 → expect works ────────────
echo "=== scenario 2: global bridge-nf-call-iptables=0 (current boxman default) ==="
echo 0 > "$NF"
record "2-global-disable" "works" "$(verdict_of vm_ping_ct)"
echo 1 > "$NF"

# ── scenario 3: target state — nf=1 + scoped FORWARD rule → works ────────
echo "=== scenario 3: nf=1 + scoped physdev rule in FORWARD (target state) ==="
add_fwd_rule
record "3-scoped-forward" "works" "$(verdict_of vm_ping_ct)"
del_fwd_rule

# ── scenario 4: DOCKER-USER variant ──────────────────────────────────────
echo "=== scenario 4: nf=1 + scoped rule in DOCKER-USER ==="
if have_docker_user; then
    add_du_rule
    record "4-docker-user" "works" "$(verdict_of vm_ping_ct)"
    if [ "$WITH_DOCKER_RESTART" -eq 1 ] && [ "$HAVE_DOCKER" -eq 1 ]; then
        restart_docker
        if iptables -C DOCKER-USER "${FWD_RULE[@]}" 2>/dev/null; then persisted=works; else persisted=blocked; fi
        record "4b-du-survives-restart" "works" "$persisted"
    else
        skip "4b-du-survives-restart" "works" "needs --with-docker-restart"
    fi
    del_du_rule
else
    skip "4-docker-user" "works" "DOCKER-USER chain absent (docker not installed?)"
fi

# ── scenario 5: fragility — nf=0, restart docker, does it flip back? ─────
echo "=== scenario 5: docker restart flips nf-call-iptables back? ==="
if [ "$WITH_DOCKER_RESTART" -eq 1 ] && [ "$HAVE_DOCKER" -eq 1 ]; then
    echo 0 > "$NF"
    restart_docker
    record "5-restart-flips-nf" "n/a" "nf=$(cat "$NF")"
    echo 1 > "$NF"
else
    skip "5-restart-flips-nf" "n/a" "needs --with-docker-restart and docker"
fi

# ── scenario 6: macvlan container on the bridge ──────────────────────────
echo "=== scenario 6: docker macvlan(parent=$BR) container <-> netns 'VM' ==="
if [ "$HAVE_DOCKER" -eq 1 ]; then
    if docker network create -d macvlan --subnet="$SUBNET" --ip-range=10.99.0.64/26 \
           --gateway="${HOST_BR_IP%/*}" -o parent="$BR" "$MACVLAN_NET" >/dev/null 2>&1 \
       && docker run -d --rm --name "$MACVLAN_CT" --network "$MACVLAN_NET" \
           --ip "$IP_MV" alpine:3 sleep 600 >/dev/null 2>&1; then
        add_fwd_rule                       # target state active
        record "6a-mv-ct->vm"  "works" "$(verdict_of mv_ping_vm)"
        record "6b-vm->mv-ct"  "works" "$(verdict_of vm_ping_mv)"
        # L2 proof: neighbor entry for the container MAC must exist in the VM ns
        if ip -n "$NS_VM" neigh show "$IP_MV" 2>/dev/null | grep -qv FAILED \
           && [ -n "$(ip -n "$NS_VM" neigh show "$IP_MV" 2>/dev/null)" ]; then
            record "6c-arp-resolves" "works" "works"
        else
            record "6c-arp-resolves" "works" "blocked"
        fi
        # host<->macvlan caveat: host IP on the parent cannot reach the child...
        ip addr add "$HOST_BR_IP" dev "$BR"
        record "6d-host->mv (caveat)" "blocked" "$(verdict_of host_ping_mv)"
        # ...unless via a macvlan shim on the same parent
        ip link add "$SHIM" link "$BR" type macvlan mode bridge
        ip addr add "$SHIM_IP" dev "$SHIM"
        ip link set "$SHIM" up
        ip route add "$IP_MV/32" dev "$SHIM"
        record "6e-host->mv via shim" "works" "$(verdict_of host_ping_mv)"
        ip route del "$IP_MV/32" dev "$SHIM" 2>/dev/null || true
        ip link del "$SHIM"
        ip addr del "$HOST_BR_IP" dev "$BR"
        del_fwd_rule
    else
        skip "6-macvlan" "works" "macvlan net/container creation failed (image pull?)"
    fi
else
    skip "6-macvlan" "works" "docker not available"
fi

# ── scenario 7: idempotency of the rule-management pattern ───────────────
echo "=== scenario 7: iptables -C check-before-insert is idempotent ==="
add_fwd_rule; add_fwd_rule
count=$(iptables -S FORWARD | grep -cF -- "-i $BR -o $BR -m physdev --physdev-is-bridged -j ACCEPT" || true)
[ "$count" -eq 1 ] && record "7-idempotent-insert" "works" "works" \
                   || record "7-idempotent-insert" "works" "blocked"
del_fwd_rule

# ── summary ──────────────────────────────────────────────────────────────
echo
echo "=== summary (paste into findings.md) ==="
printf '%-28s %-12s %-14s %s\n' "scenario" "expected" "actual" "verdict"
mismatch=0
for r in "${RESULTS[@]}"; do
    IFS='|' read -r s e a v <<<"$r"
    printf '%-28s %-12s %-14s %s\n' "$s" "$e" "$a" "$v"
    [ "$v" = "DIFFERS" ] && mismatch=1
done
exit $mismatch
