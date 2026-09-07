#!/usr/bin/env bash
# One-time host checks + lab directories on a physical host. Read-mostly:
# the only changes are mkdir under $LAB and the lab key's permissions.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

mkdir -p "$LAB"/{iso,keys,vms,ws,build,logs}

fail=0
check() {  # check <label> <command...>
    local label=$1; shift
    if "$@" &>/dev/null; then log "ok   $label"; else log "FAIL $label"; fail=1; fi
}

check "site '$SITE' is known (hpe1|hpe2)"     test -n "${HOST_IP[$SITE]:-}"
check "underlay $VXLAN_DEV has ${HOST_IP[$SITE]}" bash -c "ip -4 addr show dev $VXLAN_DEV | grep -qw ${HOST_IP[$SITE]}"
check "nested KVM enabled"                    bash -c "grep -qx 1 /sys/module/kvm_amd/parameters/nested 2>/dev/null || grep -qx Y /sys/module/kvm_intel/parameters/nested 2>/dev/null"
check "/dev/kvm usable"                       test -r /dev/kvm -a -w /dev/kvm
check "libvirt reachable"                     virsh -c qemu:///system list
check "virt-install present"                  test -x /usr/bin/virt-install
check "docker usable (ISO build)"             docker info
check "python3.12 + venv"                     python3.12 -m venv --help
check "passwordless sudo"                     sudo -n true
check "vxlan kernel module"                   modinfo -n vxlan
check "internet egress"                       curl -fsSI -m 10 "$ISO_URL"
check "lab key present at $KEY"               test -f "$KEY"
[[ -f $KEY ]] && chmod 600 "$KEY"

# Free space where the node disks and the ISO live (4 x 64G root + 8 x 64G
# OSD thin qcow2 + ~3.5G of ISOs; 100G is a comfortable floor).
avail_gb=$(df -BG --output=avail "$LAB" | tail -1 | tr -dc 0-9)
check "$LAB has >= 100G free (${avail_gb}G)" test "$avail_gb" -ge 100

(( fail == 0 )) || die "host checks failed on $SITE"
log "host $SITE ready; lab dir $LAB"
