# Boxman Docker Libvirt Environment

A containerised libvirt/KVM environment that provides nested virtualisation
inside a Docker container. Intended for local development and testing of
boxman-managed VM infrastructure.

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker Engine + Compose v2 | `docker compose version` |
| KVM support on the host | `ls /dev/kvm` |
| Nested virtualisation enabled | `cat /sys/module/kvm_intel/parameters/nested` (or `kvm_amd`) should show `Y` or `1` |

## Directory Layout

```
containers/docker/
├── Dockerfile              # Rocky Linux 9.6 image with libvirt, QEMU, sshd
├── docker-compose.yml      # Service definition with device passthrough
├── supervisord.conf        # Process manager for libvirtd, virtlogd, sshd, dnsmasq
├── entrypoint.sh           # Runtime setup (SSH keys, network, user mapping)
├── Makefile                # Convenience targets
├── .env                    # Default environment variables
└── data/                   # Created at runtime (bind-mounted into container)
    ├── images/             # libvirt disk images
    ├── libvirt-run/        # libvirt socket (libvirt-sock)
    └── ssh/                # Generated SSH key pair and ssh config
        ├── id_ed25519
        ├── id_ed25519.pub
        └── boxman.conf
```

## Quick Start

```bash
# Start the default instance
make up

# SSH into the container
make ssh

# List VMs from the host via the unix socket
make virsh

# View logs
make logs

# Stop
make down
```

## Makefile Targets

| Target | Description |
|---|---|
| `make up` | Build, start the container, and symlink the SSH config |
| `make build` | Build the Docker image |
| `make rebuild` | Build the Docker image ignoring cache |
| `make down` | Stop the container |
| `make restart` | Stop, rebuild, and start |
| `make logs` | Follow container logs |
| `make ssh` | SSH into the container as `qemu_user` |
| `make ssh-debug` | SSH with verbose (`-vvv`) output |
| `make status` | Show container, supervisor, SSH, and port status |
| `make virsh` | Run `virsh list --all` via the unix socket |
| `make link-ssh` | Symlink SSH config to `~/.ssh/config.d/boxman/` |
| `make unlink-ssh` | Remove the SSH config symlink |
| `make clean-data` | Remove the data directory |
| `make clean-volumes` | Remove Docker compose volumes |
| `make clean-images` | Remove built Docker images |
| `make clean-containers` | Stop and remove containers |
| `make clean-all` | Full cleanup (containers, volumes, images, data, symlinks) |
| `make clean-dangling` | Prune dangling Docker images and volumes system-wide |
| `make help` | Show targets with descriptions |

## Connecting to Libvirt

### Via Unix Socket (from the host)

```bash
virsh -c "qemu+unix:///system?socket=$(pwd)/data/libvirt-run/libvirt-sock" list --all
```

### Via SSH

```bash
# Using the generated SSH config
ssh -F ./data/ssh/boxman.conf boxman-default

# Or via the symlink
ssh -F ~/.ssh/config.d/boxman/boxman-default.conf boxman-default

# Once inside, use virsh normally
sudo virsh list --all
```

### Via qemu+ssh (from the host)

```bash
virsh -c "qemu+ssh://qemu_user@127.0.0.1:2222/system?keyfile=$(pwd)/data/ssh/id_ed25519&known_hosts=/dev/null&no_verify=1" list --all
```

## Running Multiple Instances

Each instance needs unique ports and a separate data directory:

```bash
# Instance 1 (default)
make up

# Instance 2
BOXMAN_INSTANCE_NAME=dev02 \
BOXMAN_DATA_DIR=./data-dev02 \
BOXMAN_SSH_PORT=2223 \
BOXMAN_LIBVIRT_TCP_PORT=16510 \
BOXMAN_LIBVIRT_TLS_PORT=16515 \
docker compose -p boxman-dev02 up --build -d
```

Connect to instance 2:

```bash
ssh -F ~/.ssh/config.d/boxman/boxman-dev02.conf boxman-dev02
```

## Environment Variables

Configured in `.env` or overridden on the command line:

| Variable | Default | Description |
|---|---|---|
| `BOXMAN_INSTANCE_NAME` | `default` | Instance name (used in container name and SSH config) |
| `BOXMAN_DATA_DIR` | `./data` | Host directory for images, sockets, and SSH keys |
| `BOXMAN_SSH_PORT` | `2222` | Host port mapped to container SSH |
| `BOXMAN_LIBVIRT_TCP_PORT` | `16509` | Host port for libvirt plain TCP |
| `BOXMAN_LIBVIRT_TLS_PORT` | `16514` | Host port for libvirt TLS |
| `HOST_UID` | *(auto-detected)* | UID of the host user (set automatically by Makefile) |
| `HOST_GID` | *(auto-detected)* | GID of the host user (set automatically by Makefile) |

## Container Details

### Base Image

Rocky Linux 9.6

### Installed Packages

- `qemu-kvm`, `qemu-img` — KVM hypervisor
- `libvirt`, `libvirt-daemon`, `libvirt-client` — libvirt management
- `virt-install` — VM provisioning CLI
- `genisoimage`, `cloud-init` — cloud-init ISO generation
- `xmlstarlet` — XML manipulation for VM configs
- `supervisor` — process manager (libvirtd, virtlogd, sshd, dnsmasq)
- `openssh-server` — SSH access
- `dnsmasq`, `bridge-utils`, `iptables`, `nftables` — networking

### Users

| User | Purpose |
|---|---|
| `root` | Runs libvirtd, supervisord |
| `qemu_user` | SSH login user with passwordless `sudo` |

### Processes (managed by supervisord)

| Process | Priority | Description |
|---|---|---|
| `sshd` | 5 | OpenSSH server |
| `virtlogd` | 10 | Libvirt log daemon |
| `libvirtd` | 20 | Libvirt daemon |
| `dnsmasq` | 30 | DNS/DHCP for virtual networks |

## Hardening Guidelines

For production or shared environments:

1. Remove `privileged: true` and use only the listed `cap_add` entries
2. Re-enable libvirt socket authentication (`auth_unix_rw = "polkit"` or `"sasl"`)
3. Restrict socket bind-mount permissions (`chmod 0770`, group-based access)
4. Use a read-only root filesystem with explicit tmpfs/volume mounts
5. Replace `seccomp:unconfined` / `apparmor:unconfined` with custom profiles
6. Set container resource limits (`deploy.resources.limits`)
7. Use TLS for remote libvirt connections (port 16514)
8. Consider running libvirtd as a non-root user

## Troubleshooting

### "Failed to find user record for uid"

The host user's UID is not known inside the container. Ensure `HOST_UID` is
passed correctly (the Makefile does this automatically). Check with:

```bash
docker exec boxman-libvirt-default getent passwd $(id -u)
```

### sshd not starting

```bash
make status
docker exec boxman-libvirt-default cat /var/log/supervisor/sshd.stderr.log
```

### libvirtd socket not appearing

```bash
docker exec boxman-libvirt-default cat /var/log/supervisor/libvirtd.stderr.log
```

### virbr0 / default network missing

The entrypoint automatically starts the default network. If it fails:

```bash
docker exec boxman-libvirt-default virsh net-list --all
docker exec boxman-libvirt-default virsh net-start default
```

### cgroup errors when creating VMs

Ensure `cgroup_controllers = []` is set in `/etc/libvirt/qemu.conf` (handled
by the Dockerfile). The container must also be run with `privileged: true` or
appropriate cgroup mounts.
