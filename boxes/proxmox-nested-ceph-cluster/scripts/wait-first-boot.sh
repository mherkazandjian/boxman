#!/usr/bin/env bash
# Wait until the nodes are reachable over ssh AND their first-boot hook has
# written its marker (repos, MTU, guest agent done). sshd is up well before
# the hook finishes, so "ssh works" alone would race the hook's apt-get.
# Usage: wait-first-boot.sh [node ...]   (default: all four; run from hpe1)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

nodes=("$@"); (( ${#nodes[@]} )) || nodes=("${NODES[@]}")
timeout=${PVE_FIRST_BOOT_TIMEOUT:-900}

for n in "${nodes[@]}"; do
    log "waiting for ssh on $n (${NODE_IP[$n]})"
    wait_ssh "$n" "$timeout"
    deadline=$((SECONDS + timeout))
    until pssh "$n" test -f /var/lib/pve-lab/first-boot.done 2>/dev/null; do
        (( SECONDS < deadline )) || die "$n: first-boot marker missing after ${timeout}s; see /var/log/pve-lab-first-boot.log on the node"
        sleep 10
    done
    log "$n: $(pssh "$n" 'hostname -f; ip -o link show vmbr0 | grep -o "mtu [0-9]*"; grep -c svm /proc/cpuinfo | sed "s/^/svm cpus: /"; test -c /dev/kvm && echo /dev/kvm ok' | tr '\n' ' ')"
done
log "first boot complete on: ${nodes[*]}"
