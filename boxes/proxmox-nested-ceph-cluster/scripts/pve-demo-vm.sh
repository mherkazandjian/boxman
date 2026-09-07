#!/usr/bin/env bash
# Create the nested demo VM: Ubuntu 24.04 cloud image imported onto the Ceph
# pool, cloud-init with the lab key, DHCP from hpe1 (reservation demo01 ->
# 10.77.0.50), started on pve1. Idempotent. Run from hpe1.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

node=${1:-pve1}
wait_ssh "$node"

# the RBD storage shows up as inactive for a few seconds after pool creation
for _ in $(seq 1 30); do
    pssh "$node" "pvesm status --storage $CEPH_POOL 2>/dev/null | grep -qw active" && break
    sleep 5
done
pssh "$node" "pvesm status --storage $CEPH_POOL | grep -qw active" || die "storage $CEPH_POOL is not active on $node"

pssh "$node" "test -f /root/noble.img || curl -fsSL --retry 3 -o /root/noble.img $DEMO_IMG_URL"
pscp -q "$KEY.pub" "root@${NODE_IP[$node]}:/root/pvelab.pub"

if ! pssh "$node" "qm status $DEMO_VMID" &>/dev/null; then
    log "creating VM $DEMO_VMID ($DEMO_NAME) on $node"
    pssh "$node" "qm create $DEMO_VMID --name $DEMO_NAME --memory 2048 --cores 2 \
        --net0 virtio=$DEMO_MAC,bridge=vmbr0,mtu=1 \
        --scsihw virtio-scsi-pci --agent enabled=1 --serial0 socket --vga serial0 --ostype l26"
fi
pssh "$node" "qm config $DEMO_VMID | grep -q '^scsi0:' || \
    qm set $DEMO_VMID --scsi0 $CEPH_POOL:0,import-from=/root/noble.img"
pssh "$node" "qm set $DEMO_VMID --boot order=scsi0 --ide2 $CEPH_POOL:cloudinit \
    --ciuser demo --sshkeys /root/pvelab.pub --ipconfig0 ip=dhcp >/dev/null"
pssh "$node" "qm status $DEMO_VMID | grep -q running || qm start $DEMO_VMID"
pssh "$node" "qm config $DEMO_VMID | grep -E '^(name|net0|scsi0|ide2|ipconfig0):'"

log "waiting for $DEMO_NAME at $DEMO_IP"
for _ in $(seq 1 60); do ping -c1 -W2 "$DEMO_IP" &>/dev/null && break; sleep 5; done
ping -c1 -W2 "$DEMO_IP" &>/dev/null || die "$DEMO_IP does not answer; check 'qm terminal $DEMO_VMID' on $node"
for _ in $(seq 1 30); do
    ssh "${SSH_OPTS[@]}" "demo@$DEMO_IP" true 2>/dev/null && break; sleep 5
done
log "demo VM up: $(ssh "${SSH_OPTS[@]}" "demo@$DEMO_IP" 'hostname; ip -o -4 addr show scope global | awk "{print \$4}"; ip -o link show dev ens18 2>/dev/null | grep -o "mtu [0-9]*"; uname -r' | tr '\n' ' ')"
