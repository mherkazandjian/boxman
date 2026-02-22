#!/bin/bash
set -e

# Merge host and container passwd/group using nss_wrapper
# so libvirtd can resolve both host UIDs and container system users (qemu, libvirt, etc.)
MERGED_PASSWD=/tmp/merged_passwd
MERGED_GROUP=/tmp/merged_group

cp /etc/passwd "$MERGED_PASSWD"
cp /etc/group "$MERGED_GROUP"

# Append container system users that are missing from the host passwd
if [ -f /etc/passwd.container ]; then
    while IFS= read -r line; do
        uid=$(echo "$line" | cut -d: -f3)
        if ! grep -q "^[^:]*:[^:]*:${uid}:" "$MERGED_PASSWD"; then
            echo "$line" >> "$MERGED_PASSWD"
        fi
    done < /etc/passwd.container
fi
if [ -f /etc/group.container ]; then
    while IFS= read -r line; do
        gid=$(echo "$line" | cut -d: -f3)
        if ! grep -q "^[^:]*:[^:]*:${gid}:" "$MERGED_GROUP"; then
            echo "$line" >> "$MERGED_GROUP"
        fi
    done < /etc/group.container
fi

export LD_PRELOAD=/usr/lib64/libnss_wrapper.so
export NSS_WRAPPER_PASSWD=$MERGED_PASSWD
export NSS_WRAPPER_GROUP=$MERGED_GROUP

# Clean up stale sockets
rm -f /var/run/libvirt/libvirt-sock /var/run/libvirt/libvirt-sock-ro

# Start supervisord in background (inherits NSS_WRAPPER env)
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

# Keep container running by waiting on supervisord
wait $SUPERVISOR_PID
