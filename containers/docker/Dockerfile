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
        openssh-server \
        openssh-clients \
        sshpass \
        sudo \
    && dnf clean all

COPY supervisord.conf /etc/supervisord.conf

RUN mkdir -p /var/run/libvirt /var/lib/libvirt/images /var/log/supervisor /etc/dnsmasq.d

# Configure sshd
RUN ssh-keygen -A && \
    mkdir -p /root/.ssh && chmod 700 /root/.ssh && \
    sed -i \
        -e 's/^#PermitRootLogin.*/PermitRootLogin prohibit-password/' \
        -e 's/^#PubkeyAuthentication.*/PubkeyAuthentication yes/' \
        -e 's/^#PasswordAuthentication.*/PasswordAuthentication no/' \
        /etc/ssh/sshd_config

# Create qemu user with passwordless sudo and SSH access
RUN useradd -m -s /bin/bash qemu_user && \
    echo 'qemu_user ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/qemu_user && \
    chmod 440 /etc/sudoers.d/qemu_user && \
    mkdir -p /home/qemu_user/.ssh && \
    chmod 700 /home/qemu_user/.ssh && \
    chown -R qemu_user:qemu_user /home/qemu_user/.ssh && \
    usermod -aG qemu qemu_user

# SSH key pair is generated at runtime in entrypoint.sh

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
    echo 'cgroup_controllers = []' >> /etc/libvirt/qemu.conf && \
    echo 'user = "root"' >> /etc/libvirt/qemu.conf && \
    echo 'group = "root"' >> /etc/libvirt/qemu.conf && \
    echo 'dynamic_ownership = 0' >> /etc/libvirt/qemu.conf

# enable default network autostart
RUN ln -sf /etc/libvirt/qemu/networks/default.xml /etc/libvirt/qemu/networks/autostart/default.xml

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# clean up stale sockets before starting supervisor
CMD ["/entrypoint.sh"]
