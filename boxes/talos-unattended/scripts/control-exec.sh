#!/usr/bin/env bash
# Reach the control VM over ssh, with its identity verified first.
#
#   control-exec.sh [--inputs FILE] verify
#   control-exec.sh [--inputs FILE] run [--] COMMAND [ARG...]
#   control-exec.sh [--inputs FILE] address
#
# Runs on the host. `verify` establishes, without a console and without
# trusting the network, that the address it will ssh to is this project's
# control VM:
#
#   1. the control domain exists, is running and has the UUID the install
#      driver recorded when boxman created it;
#   2. its one NIC is on this project's fabric network, and the network's
#      DHCP lease for that NIC's MAC gives the address (exactly one);
#   3. its ssh host key is read from inside the guest through the libvirt
#      guest agent (a channel of the domain itself, not the network) and
#      pinned in the driver's private known_hosts;
#   4. ssh then logs in with the operator key only, BatchMode, strict host
#      key checking against that pin and nothing from ~/.ssh/config, and
#      the guest must answer.
#
# Each step is retried until TALOS_CONTROL_TIMEOUT (default 900 s) runs out;
# the error then names the step that never succeeded. `run` executes COMMAND
# (its words quoted for the remote shell) with the same pinned options, after
# checking the pin exists; stdin is passed through. `address` prints the
# verified address.
#
# Exit status: that of the remote command for `run`; 1 when the control VM
# cannot be verified; 3 when it is not the VM this driver created.
set -euo pipefail
PROG=control-exec
# shellcheck source=lib.sh
. "$(dirname -- "$0")/lib.sh"

INPUTS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --inputs) INPUTS=${2:?--inputs needs a file}; shift; shift ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2 ;;
        *) break ;;
    esac
done
[ $# -gt 0 ] || die "usage: control-exec.sh [--inputs FILE] verify|run|address ..."
ACTION=$1; shift

load_inputs "$INPUTS"
read_facts
TIMEOUT=${TALOS_CONTROL_TIMEOUT:-900}
POLL=${TALOS_POLL_SECONDS:-5}
ADDRESS_FILE=$DRIVER_DIR/control.address

# agent CMD-JSON: the guest agent's "return" member, as JSON
agent() {
    vsh qemu-agent-command "$CONTROL_DOMAIN" "$1" 2>/dev/null \
        | "$PYTHON" -c 'import json, sys; print(json.dumps(json.load(sys.stdin)["return"]))' 2>/dev/null
}

# host_key: the guest's ed25519 host public key, read through the agent
host_key() {
    local handle data
    handle=$(agent '{"execute":"guest-file-open","arguments":{"path":"/etc/ssh/ssh_host_ed25519_key.pub","mode":"r"}}') || return 1
    [[ $handle =~ ^[0-9]+$ ]] || return 1
    data=$(agent "{\"execute\":\"guest-file-read\",\"arguments\":{\"handle\":$handle,\"count\":4096}}") || data=""
    agent "{\"execute\":\"guest-file-close\",\"arguments\":{\"handle\":$handle}}" >/dev/null || true
    printf '%s' "$data" | "$PYTHON" -c '
import base64, json, sys
line = base64.b64decode(json.load(sys.stdin)["buf-b64"]).decode().strip()
kind, key = line.split()[:2]
assert kind == "ssh-ed25519" and key.startswith("AAAA"), line
print(kind, key)' 2>/dev/null
}

# control_address: ADDR := the one lease the fabric network holds for the
# control VM's one NIC; else WHY says what is missing (AMBIGUOUS=1 when
# waiting cannot help)
control_address() {
    local nics mac source ips n
    ADDR="" AMBIGUOUS=0
    nics=$(vsh domiflist "$CONTROL_DOMAIN" 2>/dev/null | awk 'NR > 2 && NF >= 5 {print $3, $5}') \
        || { WHY="cannot list the control VM's NICs"; return 1; }
    n=$(printf '%s\n' "$nics" | grep -c . || true)
    [ "$n" = 1 ] || { WHY="the control VM has $n NICs, expected 1"; AMBIGUOUS=1; return 1; }
    read -r source mac <<< "$nics"
    [ "$source" = "$NETWORK" ] || { WHY="the control VM's NIC is on $source, not $NETWORK"; AMBIGUOUS=1; return 1; }
    ips=$(lease_ips "$mac")
    n=$(printf '%s\n' "$ips" | grep -c . || true)
    case "$n" in
        1) ADDR=$ips ;;
        0) WHY="no DHCP lease on $NETWORK for the control VM's MAC $mac yet"; return 1 ;;
        *) WHY="several leases for the control VM's MAC $mac: ${ips//$'\n'/ }"; AMBIGUOUS=1; return 1 ;;
    esac
}

remote_words() { local w out=""; for w in "$@"; do out+=$(printf '%q ' "$w"); done; printf '%s' "${out% }"; }

verify() {
    local deadline=$((SECONDS + TIMEOUT)) want state uuid addr key ADDR AMBIGUOUS
    want=$(record_get uuid.control)
    [ -n "$want" ] || die "no recorded UUID for the control VM in $RECORD; run install.sh, which records it after boxman up"
    private_dir "$DRIVER_DIR"
    WHY="not tried yet"
    while :; do
        state=$(domain_state "$CONTROL_DOMAIN")
        uuid=$(domain_uuid "$CONTROL_DOMAIN" || true)
        if [ "$state" = absent ]; then
            WHY="the control domain $CONTROL_DOMAIN does not exist"
        elif [ "$uuid" != "$want" ]; then
            drift "$CONTROL_DOMAIN has uuid ${uuid:-unknown}, not the recorded $want: it is not the VM this driver created"
        elif [ "$state" != running ]; then
            WHY="the control VM is $state"
        elif ! control_address; then
            [ "$AMBIGUOUS" = 0 ] || drift "$WHY"
        elif addr=$ADDR; ! key=$(host_key); then
            WHY="the guest agent of $CONTROL_DOMAIN does not answer (or has no ed25519 host key) yet"
        else
            printf '%s %s\n' "$HOST_KEY_ALIAS" "$key" > "$KNOWN_HOSTS.new"
            chmod 0600 "$KNOWN_HOSTS.new" && mv -f "$KNOWN_HOSTS.new" "$KNOWN_HOSTS"
            ssh_opts
            if ssh "${SSH_OPTS[@]}" "$CONTROL_USER@$addr" true </dev/null 2>"$DRIVER_DIR/ssh.err"; then
                printf '%s\n' "$addr" > "$ADDRESS_FILE"
                log "verified: $CONTROL_DOMAIN (uuid $want) at $addr, host key ${key#* }"
                log "verified: ssh as $CONTROL_USER with the operator key and the pinned host key"
                return 0
            fi
            WHY="ssh to $CONTROL_USER@$addr with the pinned host key failed: $(tr '\n' ' ' < "$DRIVER_DIR/ssh.err")"
        fi
        [ $SECONDS -lt $deadline ] || die "control VM not verified within ${TIMEOUT}s: $WHY"
        sleep "$POLL"
    done
}

case "$ACTION" in
    verify) verify ;;
    address)
        [ -s "$ADDRESS_FILE" ] || die "the control VM is not verified yet; run: control-exec.sh verify"
        cat "$ADDRESS_FILE" ;;
    run)
        [ "${1:-}" != -- ] || shift
        [ $# -gt 0 ] || die "run: no command"
        [ -s "$ADDRESS_FILE" ] && [ -s "$KNOWN_HOSTS" ] \
            || die "the control VM is not verified yet; run: control-exec.sh verify"
        ssh_opts
        exec ssh "${SSH_OPTS[@]}" "$CONTROL_USER@$(cat "$ADDRESS_FILE")" "$(remote_words "$@")" ;;
    *) die "unknown action $ACTION (verify, run, address)" ;;
esac
