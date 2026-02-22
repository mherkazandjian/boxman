FROM rockylinux/rockylinux:9.6

RUN dnf install -y epel-release && \
    dnf config-manager --set-enabled crb && \
    dnf install -y \
        qemu-kvm \
        qemu-img \
        libvirt \
        libvirt-daemon \
        libvirt-daemon-driver-qemu \
        libvirt-client \
        virt-install \
        genisoimage \
        cloud-init \
        xmlstarlet \
        supervisor \
        iproute \
        dnsmasq \
        bridge-utils \
        iptables \
        nftables \
        libvirt-daemon-config-network \
        nss_wrapper \
    && dnf clean all

COPY supervisord.conf /etc/supervisord.conf

RUN mkdir -p /var/run/libvirt /var/lib/libvirt/images /var/log/supervisor /etc/dnsmasq.d

# Save container system users that are needed at runtime.
# The host /etc/passwd will be mounted over this at runtime,
# so we merge needed entries via the entrypoint.
RUN cp /etc/passwd /etc/passwd.container && \
    cp /etc/group /etc/group.container

# Configure libvirtd: disable unix socket auth for local dev access
RUN sed -i \
    -e 's/^#unix_sock_group =.*/unix_sock_group = "root"/' \
    -e 's/^#unix_sock_ro_perms =.*/unix_sock_ro_perms = "0777"/' \
    -e 's/^#unix_sock_rw_perms =.*/unix_sock_rw_perms = "0777"/' \
    -e 's/^#auth_unix_ro =.*/auth_unix_ro = "none"/' \
    -e 's/^#auth_unix_rw =.*/auth_unix_rw = "none"/' \
    /etc/libvirt/libvirtd.conf

# disable cgroup management in qemu — required when running inside a container
RUN echo 'cgroup_device_acl = [' >> /etc/libvirt/qemu.conf && \
    echo '    "/dev/null", "/dev/full", "/dev/zero",' >> /etc/libvirt/qemu.conf && \
    echo '    "/dev/random", "/dev/urandom",' >> /etc/libvirt/qemu.conf && \
    echo '    "/dev/ptmx", "/dev/kvm",' >> /etc/libvirt/qemu.conf && \
    echo '    "/dev/net/tun", "/dev/vhost-net"' >> /etc/libvirt/qemu.conf && \
    echo ']' >> /etc/libvirt/qemu.conf && \
    echo 'cgroup_controllers = []' >> /etc/libvirt/qemu.conf

# enable default network autostart
RUN ln -sf /etc/libvirt/qemu/networks/default.xml /etc/libvirt/qemu/networks/autostart/default.xml

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# clean up stale sockets before starting supervisor
CMD ["/entrypoint.sh"]
