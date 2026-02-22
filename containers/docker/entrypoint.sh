#!/bin/bash
set -e

# Ensure required directories exist
mkdir -p /var/run/libvirt /var/lib/libvirt/images /etc/boxman/ssh

# Add host user to container's /etc/passwd so libvirtd can resolve the peer UID
if [ -n "$HOST_UID" ] && [ "$HOST_UID" != "0" ]; then
    if ! getent group "$HOST_GID" &>/dev/null; then
        groupadd -g "$HOST_GID" hostuser 2>/dev/null || true
    fi
    if ! getent passwd "$HOST_UID" &>/dev/null; then
        useradd -u "$HOST_UID" -g "$HOST_GID" -M -s /sbin/nologin -d /nonexistent hostuser 2>/dev/null || true
    fi
    echo "Registered host user: uid=$HOST_UID gid=$HOST_GID"
fi

# Ensure qemu_user home dir and ssh dir exist
mkdir -p /home/qemu_user/.ssh
chmod 700 /home/qemu_user /home/qemu_user/.ssh
chown -R qemu_user:qemu_user /home/qemu_user

# If SSH key doesn't exist yet in the bind-mounted dir, generate it
if [ ! -f /etc/boxman/ssh/id_ed25519 ]; then
    ssh-keygen -t ed25519 -f /etc/boxman/ssh/id_ed25519 -N "" -C "boxman-libvirt-container"
    echo "Generated new SSH key pair in /etc/boxman/ssh/"
fi

# Install the public key for qemu_user
cp /etc/boxman/ssh/id_ed25519.pub /home/qemu_user/.ssh/authorized_keys
chmod 600 /home/qemu_user/.ssh/authorized_keys
chown qemu_user:qemu_user /home/qemu_user/.ssh/authorized_keys

# Make SSH keys readable by the host user
chmod 644 /etc/boxman/ssh/id_ed25519.pub
chmod 600 /etc/boxman/ssh/id_ed25519
if [ -n "$HOST_UID" ]; then
    chown "$HOST_UID" /etc/boxman/ssh/id_ed25519 /etc/boxman/ssh/id_ed25519.pub
fi

# Generate ssh_config for host-side convenience
HOST_DATA_DIR="${BOXMAN_DATA_DIR:-./data}"
INSTANCE_NAME="${BOXMAN_INSTANCE_NAME:-default}"
cat > /etc/boxman/ssh/boxman.conf <<EOF
Host boxman-${INSTANCE_NAME}
    HostName 127.0.0.1
    Port ${BOXMAN_SSH_PORT:-2222}
    User qemu_user
    IdentityFile ${HOST_DATA_DIR}/ssh/id_ed25519
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
EOF
if [ -n "$HOST_UID" ]; then
    chown "$HOST_UID" /etc/boxman/ssh/boxman.conf
fi
echo "SSH config written to ${HOST_DATA_DIR}/ssh/boxman.conf"

# Regenerate SSH host keys if missing
ssh-keygen -A 2>/dev/null || true

# Clean up stale sockets
rm -f /var/run/libvirt/libvirt-sock /var/run/libvirt/libvirt-sock-ro

# Start supervisord in background
/usr/bin/supervisord -c /etc/supervisord.conf &
SUPERVISOR_PID=$!

# Wait for libvirtd socket to become available
echo "Waiting for libvirtd..."
for i in $(seq 1 30); do
    if [ -S /var/run/libvirt/libvirt-sock ]; then
        echo "libvirtd socket is up."
        break
    fi
    sleep 1
done

if [ ! -S /var/run/libvirt/libvirt-sock ]; then
    echo "ERROR: libvirtd socket did not appear within 30s"
    cat /var/log/supervisor/libvirtd.stderr.log 2>/dev/null || true
fi

# Start the default network if not already active
if virsh net-info default 2>/dev/null | grep -q "Active.*no"; then
    echo "Starting default network..."
    virsh net-start default
elif ! virsh net-info default &>/dev/null; then
    echo "Defining and starting default network..."
    virsh net-define /etc/libvirt/qemu/networks/default.xml
    virsh net-start default
fi

echo "libvirt is ready."

wait $SUPERVISOR_PID
