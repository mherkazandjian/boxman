#!/usr/bin/env bash
# Shared settings and helpers for the proxmox-nested-ceph-cluster scripts.
# Source it; every value can be overridden from the environment.
set -euo pipefail

LAB="${PVE_LAB_DIR:-$HOME/pve-lab}"
SITE="${BOXMAN_SITE:-$(hostname -s)}"
KEY="${PVE_LAB_KEY:-$LAB/keys/id_ed25519_pvelab}"
LOGS="$LAB/logs"
mkdir -p "$LOGS"

# --- Proxmox media ---------------------------------------------------------
ISO_NAME="proxmox-ve_9.2-1.iso"
ISO_SHA256="4e88fe416df9b527624a175f24c9aa07c714d3332afb1ee3dbf3879573ef2c6c"
ISO_URL="https://enterprise.proxmox.com/iso/${ISO_NAME}"
AUTO_ISO="${PVE_LAB_ISO:-$LAB/iso/proxmox-ve_9.2-1-auto.iso}"

# --- underlay: point-to-point VXLAN between the two hosts -------------------
VXLAN_DEV="${PVE_VXLAN_DEV:-bond0.1439}"   # host VLAN interface carrying the tunnel
VXLAN_ID="${PVE_VXLAN_ID:-7700}"
VXLAN_PORT=4789
VXLAN_IF="vxlan-pve"
LAB_MTU=1450                               # 1500 - 50 bytes VXLAN overhead
declare -A HOST_IP=([hpe1]=192.168.139.57 [hpe2]=192.168.139.58)
declare -A PEER=([hpe1]=hpe2 [hpe2]=hpe1)
declare -A SITE_BRIDGE=([hpe1]=virbr-pve [hpe2]=br-pve)

# --- the lab L2 and its nodes (must match conf.yml) ------------------------
LAB_NET=10.77.0.0/24
GATEWAY=10.77.0.1                          # hpe1's virbr-pve (libvirt nat: DHCP/DNS/NAT)
PEER_BRIDGE_IP=10.77.0.2                   # hpe2's br-pve (host access + ARP discovery only)
NODES=(pve1 pve2 pve3 pve4)
declare -A NODE_IP=([pve1]=10.77.0.11 [pve2]=10.77.0.12 [pve3]=10.77.0.13 [pve4]=10.77.0.14)
declare -A NODE_SITE=([pve1]=hpe1 [pve2]=hpe1 [pve3]=hpe2 [pve4]=hpe2)
DOMAIN=pve.lab
CLUSTER_NAME=pvelab
CEPH_POOL=vmpool

DEMO_VMID=100
DEMO_NAME=demo01
DEMO_MAC=52:54:00:77:00:50
DEMO_IP=10.77.0.50
DEMO_IMG_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"

# libvirt domain name boxman gives a node (project pvelab, cluster pve)
domain_name() { echo "bprj__pvelab__bprj_pve_$1"; }

# nodes hosted on a site, one per line
site_nodes() {
    local n
    for n in "${NODES[@]}"; do
        [[ ${NODE_SITE[$n]} == "$1" ]] && echo "$n"
    done
    return 0
}

# --- ssh helpers (root on a node, lab key, no host-key persistence) ---------
SSH_OPTS=(-i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=no
          -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10)
pssh() { local node=$1; shift; ssh "${SSH_OPTS[@]}" "root@${NODE_IP[$node]}" "$@"; }
pscp() { scp "${SSH_OPTS[@]}" "$@"; }

log() { printf '[%s %s] %s\n' "$(date +%H:%M:%S)" "$SITE" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# wait_ssh <node> [timeout-seconds]
wait_ssh() {
    local node=$1 timeout=${2:-900} deadline=$((SECONDS + ${2:-900}))
    until pssh "$node" true 2>/dev/null; do
        (( SECONDS < deadline )) || die "$node (${NODE_IP[$node]}) not reachable over ssh after ${timeout}s"
        sleep 10
    done
}

# join <sep> <items...>
join_by() { local IFS="$1"; shift; echo "$*"; }
