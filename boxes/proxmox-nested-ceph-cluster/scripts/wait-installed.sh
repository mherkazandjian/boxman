#!/usr/bin/env bash
# Block until this site's nodes have finished the unattended install. The
# answer file says `reboot-mode = "power-off"`, and virt-install's transient
# install domain ends on a reboot anyway (on_reboot=destroy), so "installed"
# == libvirt reports the domain `shut off`. Usage: wait-installed.sh [timeout-s]
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

timeout=${1:-1800}
deadline=$((SECONDS + timeout))
mapfile -t nodes < <(site_nodes "$SITE")
(( ${#nodes[@]} )) || die "no nodes on site $SITE"

while :; do
    all_off=1 states=""
    for n in "${nodes[@]}"; do
        s=$(virsh -c qemu:///system domstate "$(domain_name "$n")" 2>/dev/null || echo missing)
        states+="$n=$s  "
        [[ $s == "shut off" ]] || all_off=0
    done
    log "$states"
    (( all_off )) && { log "all $SITE nodes installed (shut off); next: boxman up"; exit 0; }
    (( SECONDS < deadline )) || die "still installing after ${timeout}s. Inspect the console: virsh -c qemu:///system vncdisplay $(domain_name "${nodes[0]}")"
    sleep 20
done
