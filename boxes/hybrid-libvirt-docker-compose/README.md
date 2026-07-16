# hybrid-libvirt-docker-compose

A **libvirt VM and a docker-compose container sharing an L2 domain** — the
Phase 4 (#52) capability, end to end. Both endpoints attach to the same host
Linux bridge `bx_app`; the container attaches through a docker **macvlan**
network whose `parent` is that bridge, so the VM and the container are directly
L2-adjacent (same broadcast domain, ARP resolves across, no router in between).

```
node01 (libvirt)                            web (docker-compose)
  adapter_1  dhcp   -> mgmt NAT               eth0 -> backend 172.31.0.0/24 (cluster-internal)
  adapter_2  10.10.0.20/24 --\                eth1 -> 10.10.0.10 (macvlan) --\
                              \                                               \
                               `----- bx_app  (shared bridge, 10.10.0.0/24) --'
```

On `boxman up` boxman:

1. creates the host bridge `bx_app` and installs a **scoped** per-bridge
   netfilter accept rule (decision D8 — `bridge-nf-call-iptables` is left
   untouched host-wide),
2. clones/boots `node01` with its second NIC (`adapter_2`) attached to `bx_app`,
3. generates the docker-compose file and brings `web` up: a macvlan network on
   `bx_app` (static `10.10.0.10`) plus a cluster-internal `backend` bridge.

`node01`'s management IP is the DHCP address on its mgmt NIC (that is what
`boxman ssh` uses). The shared bridge is a plain L2 domain with **no DHCP**, so
`node01`'s address on it (`10.10.0.20`) is assigned as an explicit step in the
test below — deliberately not at boot, so boxman doesn't mistake that
host-unreachable `10.10.0.x` address for node01's management IP.

## Prerequisites

- **libvirt / qemu-kvm** working under `qemu:///system`, and **Docker + the
  compose v2 plugin**, both on the same host.
- boxman on the **`local` runtime** (the default) — the docker-compose provider
  shells out to `docker compose` on the host.
- **`sudo`** — the shared bridge is created with `ip link ...` via sudo, and the
  libvirt provider uses `use_sudo: true`.
- **`sshpass`** on `PATH` — boxman injects the generated SSH key into the VM
  with `ssh-copy-id` over a password session (`apt install sshpass` /
  `dnf install sshpass`). Without it, key injection is skipped (non-fatal) and
  you fall back to the guest agent for the checks below.
- The macvlan support this box relies on lands with **Phase 4** — run it from a
  checkout that has it (the `feat/docker-compose-provider` line, once #62 is
  merged).
- First run downloads the Ubuntu 24.04 cloud image (~0.5 GB, cached under
  `~/.cache/boxman/images`).

> **qemu file access:** under `qemu:///system` the VM runs as `libvirt-qemu`,
> which must be able to traverse to the VM disks. If your `$HOME` (or the
> `workspace:`/template dirs) is mode `0700`, grant a search bit, e.g.
> `sudo setfacl -m u:libvirt-qemu:x $HOME`. virt-install prints the exact
> directories if this is missing.

## Bring it up

```bash
cd boxes/hybrid-libvirt-docker-compose
boxman up
```

## Verify the shared L2 (ping + ARP + isolation)

The container is `hybrid_libvirt_dc_services-web-1` (find it with
`docker ps --filter name=web`). It has `10.10.0.10` on the shared bridge and a
`backend` address on `172.31.0.0/24`.

**Step 1 — give node01 its address on the shared bridge.** It has no DHCP there,
so assign it once (the app NIC is the second one, `enp7s0` on this box):

```bash
boxman ssh compute_node01 -- sudo ip addr add 10.10.0.20/24 dev enp7s0
boxman ssh compute_node01 -- ip -4 -br addr show enp7s0     # -> 10.10.0.20/24
```

**Step 2 — ping both ways** (VM `10.10.0.20` ⇄ container `10.10.0.10`):

```bash
docker exec hybrid_libvirt_dc_services-web-1 ping -c3 10.10.0.20   # container -> VM
boxman ssh compute_node01 -- ping -c3 10.10.0.10                   # VM -> container
```

**Step 3 — ARP resolves across the bridge** (each side learns the other's real
MAC — proof this is L2, not routed):

```bash
boxman ssh compute_node01 -- ip neigh show 10.10.0.10       # -> lladdr <container mac>
docker exec hybrid_libvirt_dc_services-web-1 ip neigh show 10.10.0.20
```

**Step 4 — cluster-internal isolation.** The container's `backend` address is
*not* reachable from the VM (only the shared bridge is L2-adjacent):

```bash
docker exec hybrid_libvirt_dc_services-web-1 ip -4 -br addr   # note the 172.31.0.x
boxman ssh compute_node01 -- ping -c2 -W2 172.31.0.2          # -> 100% loss (isolated)
```

## Tear down

```bash
boxman destroy
```

This removes `node01`, the mgmt network, and the docker-compose cluster
(`down --volumes`). The shared bridge `bx_app` is **left in place by design** —
shared bridges can be used by multiple projects, so removing them is an explicit
action:

```bash
sudo ip link del bx_app          # only if nothing else uses it
```

## Troubleshooting

- **Template bake hangs on "waiting for QEMU guest agent"** when running boxman
  as a non-root user: the agent check uses a bare `virsh` (no `-c` URI), which
  defaults to `qemu:///session`. Export `LIBVIRT_DEFAULT_URI=qemu:///system`
  before `boxman up`.
- **`ssh-copy-id`/`boxman ssh` gets "Permission denied"** right after a
  `destroy` + `up`: the fresh `node01` reuses the same mgmt IP but has a new
  host key, so the client refuses password auth against the stale
  `known_hosts` entry. Clear it: `ssh-keygen -R <node01 mgmt ip>`.
- **`web` has no internet** (e.g. an in-container package install fails): the
  L2 test does not need it, but if you want it, keep the container's default
  route on `backend` (don't add a `gateway:` to `app_bridge`, as this box does).
