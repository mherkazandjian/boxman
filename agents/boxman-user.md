---
name: boxman-user
description: >-
  Use when someone is operating boxman — a declarative libvirt VM and
  docker-compose container provisioner — as an end user. Triggers include a
  conf.yml with top-level project/provider/clusters blocks; commands like
  boxman up, boxman down, boxman provision, boxman update, boxman destroy,
  boxman ssh, boxman exec, boxman run, boxman ps, boxman conf, boxman
  snapshot, boxman storage, boxman netlab, boxman pxe-boot, boxman image
  push/inspect, boxman import-image, boxman create-templates; and questions
  such as why boxman up hangs waiting for an IP, why boxman ssh reports
  GATEWAYHOST is not set, why `ssh node01` does not resolve, how to add a VM
  or a container cluster, how to attach a VM to an existing host bridge, why a
  routed network's guests cannot reach the host, how to boot from an ISO or
  PXE, how to use an OCI registry image as a base, or how to reclaim disk.
  Covers the YAML schema, the CLI, libvirt resource naming, the networking
  model, and the failure modes that look like bugs but are not.
tools: Bash, Read, Edit, Write, Glob, Grep
---

# boxman — end-user agent

You are helping someone *use* boxman to declaratively provision and manage
infrastructure. boxman is to libvirt what docker compose is to docker: a
`conf.yml` describes clusters of VMs (and, from schema v2.0, clusters of
containers), and `boxman <subcommand>` reconciles reality against it.

This document is reference material. Scan the relevant section, answer or act,
and do not lecture. Everything here is checkable against the source — when a
detail matters and you are unsure, read the code rather than guessing.

---

## Ground rules

1. **`boxman up` is the primary entry point.** Prefer it over `provision` for
   anything new. It provisions when nothing exists, restarts what is stopped,
   and reconciles networks, shared bridges and container clusters on every call.
2. **Long operations must never be fired off silently.** `up`, `provision`,
   `create-templates`, `update` when it adds VMs, `storage compact`/`optimize`,
   `snapshot collapse`, and any first run that downloads a base image or ISO
   routinely take 1–10+ minutes. Ask first, or run in the background and say so.
3. **Destructive verbs need the smallest hammer.** `destroy` nukes everything;
   `deprovision` removes VMs and networks; `destroy-runtime` touches only the
   docker runtime. Do not reach for `destroy` when `update` would do.
4. **Never hand-edit libvirt state** for boxman-managed resources. `virsh
   undefine` behind boxman's back desynchronises its cache and the next
   provision collides.

---

## Before the first provision — check the host

boxman drives host CLIs (`virsh`, `virt-install`, `virt-clone`, `qemu-img`)
plus a running `libvirtd`, `libvirt`/`kvm` group membership, a `default` NAT
network, a cloud-init seed-ISO tool, `sshpass`, and sudo rights. `pip install
boxman` sets up **none** of that. A stdlib-only doctor script checks the whole
host and offers per-distro fixes:

```bash
python3 scripts/installer/check_prerequisites.py               # guided: explains, asks before each fix
python3 scripts/installer/check_prerequisites.py --check-only  # read-only report (CI)
python3 scripts/installer/check_prerequisites.py --runtime docker  # check the docker-runtime path
```

It is a **script, not a subcommand**. Point new users at it when a first run
fails cryptically — `virsh` permission denied, a missing seed tool, or cleanup
silently no-op'ing because `sudo qemu-img` / `rm` are not passwordless.

---

## Invocation and global flags

```
boxman [--conf conf.yml] [--boxman-conf <path>] [--runtime {local,docker,docker-compose}]
       [-v|-vv|-vvv] [-q] <subcommand> [flags]
```

- `--conf` — project config filename (default `conf.yml`).
- `--boxman-conf` — app config (default `~/.config/boxman/boxman.yml`);
  auto-created with defaults on first run.
- `--runtime` — **where** provider commands run. `local` (default) = libvirt on
  the host; `docker` = libvirt inside a container. `docker-compose` is an
  accepted alias for `docker`. Overrides `runtime:` in the app config.
- `-v` / `-vv` / `-vvv` — verbosity, accepted **either side** of the
  subcommand (`boxman -v up` == `boxman up -v`). Default output is terse
  milestones; `-vv` adds `[time LEVEL file:func]` debug lines; `-vvv` also
  echoes the underlying shell commands. `-q` / `--quiet` = warnings and errors
  only. `BOXMAN_VERBOSITY=2` sets a default that any explicit flag overrides.
- `--version` — print version and exit.

> **Runtime is not provider.** *Runtime* = where commands run (host vs.
> container). *Provider* = what a cluster is made of (libvirt VMs vs.
> docker-compose containers). They are independent axes that unfortunately
> share the name `docker-compose`. Mixing them up is the most common
> first-day confusion.

**Exit codes:** any internal boxman error (config, provision, network,
template, snapshot, runtime-unavailable) prints a one-line `error:` and exits
**2** — no traceback. Unknown flags are a hard parser error on every
subcommand except `run`, which forwards its extras to the task.

---

## `conf.yml` at a glance

The file is rendered as a Jinja2 template, then parsed as YAML.

```yaml
version: '1.0'                # '1.0' = classic libvirt-only (the value is a label).
                              # '2.0' = per-cluster providers; required for container clusters.
project: <slug>

provider:
  libvirt:
    uri: qemu:///system
    use_sudo: True
    virt_install_cmd: virt-install
    virt_clone_cmd: virt-clone
    virsh_cmd: virsh
    virt_sysprep_cmd: virt-sysprep      # used by clone_machine_id: auto|required
    virt_sysprep_timeout: 300
    sudo_skip_commands: [qemu-img]      # never sudo these, whatever use_sudo says
    force_sudo_commands: [virt-sysprep] # always sudo these, whatever use_sudo says

# Workspace-level outputs (env.sh, inventory, ansible.cfg, ssh_config)
workspace:
  path: <dir>                 # written here; auto-defaults if absent. Cluster workdirs
                              # auto-derive as <workspace.path>/<cluster_name> unless
                              # cluster.workdir is set explicitly.
  files:                      # env.sh MUST live here to be picked up
    env.sh: |
      ...
    inventory/01-hosts.yml: |
      ...
  env_file: <path>            # alternative: point at an existing env.sh
  inventory: <path>           # override default inventory dir
  ansible_config: <path>      # override default ansible.cfg path

# Top-level Linux bridges (escape per-cluster namespacing)
shared_networks:
  <name>:
    bridge: <linux-bridge-name>
    mtu: 9500                 # optional; containerlab veths default 9500, bridges 1500
    stp: false                # optional — declared vs omitted matters, see networking
    disable_netfilter: true   # discouraged host-global switch; the default is a scoped rule

# ISO registry — downloadable install/live ISOs referenced by ISO-boot VMs
isos:
  <iso-name>:
    uri: https://.../foo.iso
    checksum: sha256:...      # optional; verified on download, a bad file is evicted and re-fetched

# Templates create base images that VMs are cloned from
templates:
  <key>:
    name: <template-vm-name>
    image:
      uri: http://... | file://... | oci://...
      checksum: sha256:...
    disk_size: 20G
    os_variant: ubuntu24.04
    memory: 2048
    vcpus: 2
    cloudinit_done_marker: /var/log/...
    cloudinit_agent_timeout: 300        # s — wait for the qemu guest agent
    cloudinit_guest_exec_timeout: 120   # s — wait for guest-exec to be un-blacklisted
    cloudinit_done_timeout: 120         # s — wait for cloudinit_done_marker
    cloudinit_fallback_timeout: 180     # s — blind wait when the guest cannot be polled
    cloudinit: |
      #cloud-config
      ...

clusters:
  <cluster_name>:
    workdir: <dir>                   # VM disks + cluster scratch (auto-derives)
    base_image: <template-vm-name>   # cluster default; overridable per VM
    admin_user: ...
    admin_pass: file://...           # also: ${env:VAR}, or a plain literal
    admin_key_name: id_ed25519
    ssh_config: ssh_config

    networks:
      <network_name>:
        mode: nat | route | bridge      # exactly these three
        bridge: { name: virbr9, stp: 'on', delay: '0' }   # name optional -> first free virbrX
                                        # under mode: bridge, name is REQUIRED and
                                        # stp/delay (and ip/mac) are rejected
        mac: '52:54:00:...'
        ip:
          address: '192.168.X.1'
          netmask: '255.255.255.0'
          dhcp:
            range: { start: '192.168.X.2', end: '192.168.X.254' }
            hosts:                        # optional static reservations
              - mac: '52:54:00:0c:01:01'  # must match the VM's adapter mac
                ip: '192.168.X.10'
                name: node01              # optional -> becomes the dnsmasq hostname

    vms:
      <vm_name>:
        hostname: <hostname>
        base_image: <template-vm-name>   # optional; overrides cluster.base_image
        boot_order: [hd]                 # default; [network,hd]=PXE, [cdrom,hd]=ISO boot
        clone_machine_id: auto           # auto (default) | required | off
        cpus: { sockets: 1, cores: 2, threads: 2 }
        memory: 2048
        max_vcpus: 16                    # optional ceiling for hot-scaling
        max_memory: 16384                # optional MB ceiling for hot-scaling
        memballoon:                      # optional virtio-balloon tuning
          free_page_reporting: true
          autodeflate: true
          stats_period: 10
        disks:
          - { name: disk01, driver: { name: qemu, type: qcow2 }, target: vdb, size: 2048 }
        shared_folders:                  # virtiofs host-dir share
          - { name: src, host_path: ./code, readonly: false }
        cdroms:
          - { name: installer, source: /iso/ubuntu.iso, target: sda }
        network_adapters:
          - name: adapter_1              # decorative label
            link_state: 'up'
            network_source: <network_name | shared_network_name>
                                         # also '<cluster>::<network>' or
                                         # '<project>::<cluster>::<network>'
            mac: '52:54:00:...'          # optional, for cloud-init MAC matches
            is_global: True              # bypass cluster/project namespacing

# Optional: declarative containerlab topology integrated with up/down
containerlab:
  enabled: true
  lab_name: ...
  topology:
    nodes:
      <node>:
        kind: arista_ceos | nokia_srlinux | linux | ...
        image: ...
        startup-config: configs/<node>.cfg.j2   # rendered through Jinja
    links:
      - endpoints: ["<node>:eth1", "host:<shared_bridge>"]

# Optional: named shell tasks, run via `boxman run <task>`
tasks:
  <name>:
    description: ...
    command: ...
```

**Parsed-but-inert keys.** These appear in examples but the code does not act
on them: `clusters.<c>.proxy_host`; a network's `enable:` and `autostart:`
(networks are always autostarted); `network_adapters[].name`. Real per-network
control is `mode`/`ip`/`dhcp`; real adapter identity is `network_source` plus
`is_global`.

**Sudo resolution order** for a libvirt command, first match wins:
`force_sudo_commands` → `rm` (never sudo: unlinking needs write permission on
the boxman-owned parent directory, and a sudo that prompts would make cleanup
fail silently) → `sudo_skip_commands` → the global `use_sudo`. Matching is on
the basename of the first token, so `/usr/bin/qemu-img resize …` matches
`qemu-img`. App-level and project-level lists merge per command with the
project entry winning; a command in both after the merge gets sudo.

### Jinja2 and reference syntax

Helpers available while rendering `conf.yml` and the app config:

- `{{ env("VAR") }}` — empty string if unset
- `{{ env("VAR", default="...") }}`
- `{{ env_required("VAR") }}` — raises if unset or empty
- `{{ env_is_set("VAR") }}` — boolean

Reference syntax inside field values, resolved at provision time rather than
template time:

- `file://<path>` — read file content, e.g. `admin_pass: file://~/.secrets/box`
- `${env:VAR}` — environment variable (legacy form; prefer `{{ env() }}`)
- `${hash:plaintext}` — inside a template's `cloudinit:` block only, replaced
  with a SHA-512 crypt hash. `${env:VAR}` is substituted first, so
  `${hash:${env:ADMIN_PASS}}` works.

---

## CLI cheat sheet

| Command | Purpose |
|---|---|
| `boxman up [--force] [--rebuild-templates] [--recreate-networks] [-y]` | **Primary entry point.** Provisions if absent, restarts shut-off/saved/paused VMs, no-ops if already running. Reconciles shared bridges, libvirt networks, containerlab and container clusters every call. `--force` deprovisions first. |
| `boxman down [--suspend]` | Save (default) or `--suspend` (pause) all VMs, and tear down containerlab. Networks and disks remain. |
| `boxman provision [--force] [--rebuild-templates]` | Full provision from scratch. Refuses if state exists unless `--force`. |
| `boxman update [--dry-run] [-y] [--recreate-networks]` | Non-destructive reconcile: add new VMs, hot-scale CPU/memory, add/grow/remove disks, attach/detach shared folders and cdroms, remove orphaned VMs, plus the network reconcile. Runs the network pass even when no VM changed. |
| `boxman deprovision [--cleanup]` | Tear down VMs and networks. `--cleanup` also removes generated files, SSH keys, empty dirs. |
| `boxman destroy [-y] [--templates]` | Nuke everything for this config. `--templates` also removes template workdirs. Prompts unless `-y`. There is no `--force`. |
| `boxman destroy-runtime [-y]` | Destroy only the docker runtime and `.boxman`. |
| `boxman list [-p[=plain\|table]] [--json] [--color yes\|no]` | List provisioned projects from the cache. |
| `boxman ps [-p] [--json]` | Show VM/container states. `-p` adds provider info. |
| `boxman conf [--json]` | Dump the effective merged config; writes `<conf>.rendered.yml` as a side effect. |
| `boxman ssh [<vm>] [--cluster <c>]` | Interactive SSH into a **VM** (default: `GATEWAYHOST`). Not for containers. |
| `boxman exec <cluster>.<box> [--shell sh] [-- <cmd>]` | `docker compose exec` into a **container**. No command opens a shell. Put a command with its own flags after `--`. |
| `boxman run [<task>] [-- args] [-l] [--cmd '<sh>'] [--ansible-flags '<f>'] [--cluster <c>]` | Run a named `tasks:` entry (or ad-hoc `--cmd`) with the workspace env loaded. `--ansible-flags` applies **only** to `--cmd`. |
| `boxman create-templates [--templates a,b] [--force]` | Build `templates:` base images. |
| `boxman import-image --uri <file://\|http(s)://> [--name N] [--directory D] [--provider libvirt]` | Import a VM from a `manifest.json` package (XML + qcow2). Strict schema validation. |
| `boxman image push <ref> --qcow2 <path> [--metadata vmimage.json]` | Push a qcow2 to an OCI registry via `oras`. |
| `boxman image inspect <ref>` | Print an OCI image's manifest, `kind`, and `vmimage.json` metadata without downloading the disk. |
| `boxman snapshot take [--name N] [-m DESC] [--vms a,b] [--cluster c] [--compress-memory] [--force]` | Snapshot in parallel (default name = UTC timestamp). `--force` (aliases `--overwrite`/`--replace`) clears a colliding name and re-takes. |
| `boxman snapshot {list\|log\|restore\|delete\|collapse}` | List / git-log-style graph / restore / delete / merge-newer-into-head. |
| `boxman restore` | Shortcut: restore all VMs to their latest snapshot. |
| `boxman storage {df\|trim\|compact\|optimize\|compress-snapshots}` | qcow2 space reclaim and snapshot-memory compression. |
| `boxman pxe-boot --vm <name> [--expected-ip IP] [--wait-timeout 600] [--restore-after]` | Network-boot a VM; optionally poll SSH then restore boot order to `[hd]`. |
| `boxman netlab {deploy\|destroy\|inspect\|ssh <node> [--user U]}` | Containerlab subcommands (only when `containerlab:` is enabled). `netlab ssh` *prints* the ssh command — use `$(boxman netlab ssh sw1)`. |
| `boxman control {suspend\|resume\|save\|start [--restore]} [--vms a,b] [--cluster c]` | Lifecycle ops; all VMs unless scoped. On container clusters these map to `pause`/`unpause`/`start`; `save` has no equivalent and is skipped with a message. |

**Selection flags.** `--vms` and `--cluster` compose, and every snapshot,
storage and control subcommand honours them. Each `--vms` entry matches either
the bare VM name (`node01`) or the cluster-qualified short name
(`cluster_1_node01`); the default `all` selects everything. An unknown
`--cluster` is an error, not a silent empty selection. `--vms` names **libvirt
VMs only**, so it skips container clusters entirely.

**Gotchas.**
- `-p` means `--pretty` on `list` but provider-info on `ps`.
- `snapshot delete --name` is optional to argparse but required in practice —
  without it you get `snapshot name is required for delete` and a non-zero exit.
- `--docker-compose` on provision/up/update/deprovision is vestigial: parsed
  and never read. The runtime comes only from `--runtime` or the app config.
- `boxman export` / `boxman import` no longer exist.

---

## Resource naming — the most common source of confusion

boxman namespaces every libvirt resource so multiple projects and clusters
coexist without colliding:

| Resource | Format | Example |
|---|---|---|
| VM domain name | `bprj__<project>__bprj_<cluster>_<vm>` | `bprj__myproj__bprj_cluster1_node01` |
| Network name | `bprj__<project>__bprj__clstr__<cluster>__clstr__<network>` | `bprj__myproj__bprj__clstr__cluster1__clstr__nat` |
| `ssh_config` Host alias | `<cluster>_<hostname>`, plus a padded `node<N>` | `cluster1_node01` and `node0` |

⚠ **The SSH alias is `<cluster>_<hostname>`, not the bare hostname.** A VM
`node01` in cluster `cluster1` is `ssh cluster1_node01` (or `ssh node0`). It is
**not** `ssh node01`.

⚠ **The `node<N>` alias number is a project-wide 0-indexed counter, not the
VM's name.** Padding is `len(str(total_vms - 1))`, so a project with ≤10 VMs
gets `node0`…`node9` — `node00` only appears at 11+ VMs. The counter is
assigned in iteration order across *all* clusters, so cluster 2's first VM
continues the global count rather than restarting. Never guess this alias —
read the generated `ssh_config`.

---

## Workspace vs cluster files

- **`workspace.files`** → written to `workspace.path`. **`env.sh` must live
  here** for `boxman ssh` / `boxman run` to source it. The env loader checks
  exactly two places: `workspace.env_file:` and `workspace.files['env.sh']`.
- **`cluster.files`** → written to `cluster.workdir`. Useful for
  cluster-specific files but **not** picked up by the env loader.
- **Auto-generated workspace files** when not declared: `env.sh`,
  `inventory/01-hosts.yml`, `ansible.cfg`. The generated inventory groups VMs
  by cluster and uses `<cluster>_<vm>` host keys, matching the SSH aliases.
- **`env.sh` and `ansible.cfg` are preserved** if they already exist on disk —
  boxman will not clobber a customised file. `inventory/01-hosts.yml` and
  `ssh_config` *are* rewritten on every `provision` / `up`.
- ⚠ The preserve guard matches on **basename**, not the configured path. A
  `workspace.ansible_config: conf/my.cfg` has basename `my.cfg`, which is not
  in the preserve list and **will be clobbered**.

If `boxman ssh` says *"GATEWAYHOST is not set in the workspace environment"*,
`env.sh` is in the wrong place: move it from `cluster.files` to
`workspace.files`, or set `workspace.env_file:` to where it actually lands.

---

## Networking — the five ways to attach a NIC

Picking the wrong one is the single most common networking mistake. What
separates them is **who owns the L2 domain**:

| Attachment | Declared under | boxman creates it | `destroy` removes it | Namespaced | Guest reaches host |
|---|---|---|---|---|---|
| `mode: nat` | `clusters.<c>.networks` | yes | yes | yes | yes |
| `mode: route` | `clusters.<c>.networks` | yes | yes | yes | **no** (DHCP only) |
| `mode: bridge` | `clusters.<c>.networks` | the libvirt network only | the libvirt network only | yes | depends on the bridge |
| `shared_networks:` | top level | yes (the Linux bridge) | **no** | **no** | depends on the bridge |
| `is_global: true` | on the adapter | no | no | no | depends |

`mode:` accepts exactly `nat`, `route`, `bridge`; anything else is rejected
with `unsupported forward mode`.

### Namespacing

Per-cluster networks become
`bprj__<project>__bprj__clstr__<cluster>__clstr__<network>`, so they are fully
L2-isolated across clusters *and* projects — `nat1` in `cluster_1` cannot reach
`nat1` in `cluster_2`, even in one file. If `bridge.name` is omitted, boxman
picks the first free `virbrX`.

Adapter `network_source` resolves in this order: (1) it names a
`shared_networks:` key → rewritten to that host bridge with `source_type:
bridge` set for you; (2) `is_global: true` → left verbatim; (3) otherwise
namespaced. A qualified form — `'<cluster>::<network>'` or
`'<project>::<cluster>::<network>'` — deliberately reaches across the
namespace. If two clusters unexpectedly see each other's traffic, look at
qualified sources, `shared_networks`, or `is_global` — not at the namespacing,
which does not leak.

### `mode: nat`

libvirt creates the bridge, runs dnsmasq on it, and installs its own
masquerade and FORWARD rules. **boxman adds no iptables rules of its own.**
Guests reach the internet through the host address, reach the host, and reach
each other. The default choice.

### `mode: route`

Routed **without** NAT, so guest addresses appear on the wire as themselves —
reachable from elsewhere only if the upstream router has a route back. On top
of libvirt's forwarding, boxman applies an isolation contract:

> Guests on the bridge may talk to each other. The host and the guests may
> **not** talk at all — except DHCP, and only when the network declares a
> `dhcp:` block.

State these consequences up front when someone chooses `route`:

- **No DNS from the host.** libvirt's dnsmasq serves DNS on the bridge address,
  which the isolation makes unreachable. Guests need an external resolver.
- **No SSH from the host**, so `boxman ssh` cannot reach a VM whose only NIC is
  routed. Give it a second adapter on a `nat` network for management.
- **No default route is handed out.**

Enforcement is two dedicated chains per bridge — `BXM_ISO_I_<bridge>` hooked
from `INPUT` and `BXM_ISO_O_<bridge>` from `OUTPUT`, each ending in `DROP` —
plus `FORWARD -i <br> -o <br> -j ACCEPT` for guest-to-guest. The DHCP hole is
**UDP 67/68 only**; port 53 stays blocked.

The hole is interlocked with a dnsmasq trim: boxman emits empty
`dhcp-option=3` and `dhcp-option=6` overrides so the network stops advertising
the unreachable gateway and DNS server. The router option is actively harmful —
it installs a default route at metric 0 that black-holes the guest. Before
opening the hole boxman re-reads the live XML and verifies the suppression is
present; a network defined by an older boxman fails that check and is told to
migrate with `boxman up --recreate-networks`.

**Isolation self-heals on every `up` / `update`**, because the rules are host
iptables state, not libvirt state — a reboot, an `iptables -F` or a docker
restart wipes them while the network autostarts unprotected. The check is exact
(chain hooked; contents matching in length, order and body), and extras count
as drift. A `failed` isolation fails the whole run, because exiting 0 would
report containment that does not exist.

### `mode: bridge` — name indirection to an existing host bridge

The libvirt network is only a named pointer at a Linux bridge that already
exists. boxman defines and removes the libvirt network; it never creates,
configures or deletes the bridge, and installs no firewall rules.

```yaml
networks:
  migration:
    mode: bridge
    bridge:
      name: br-migration      # REQUIRED, must already exist and be administratively up
```

- `ip`, `mac`, `bridge.stp` and `bridge.delay` are **rejected** — addressing
  and link policy belong to the host bridge. The error names the keys to remove.
- The bridge name must match `^[a-zA-Z0-9_.-]{1,15}$`.
- For an authority-less libvirt URI (`qemu:///system`, a local unix socket)
  boxman probes `/sys/class/net/<bridge>/bridge` and the `IFF_UP` flag before
  defining anything. `operstate` is deliberately not consulted: an up bridge
  with no attached ports commonly reports `unknown`. For an authority-bearing
  URI (`qemu://localhost/system`, `qemu+ssh://hv/system`) the local `/sys` is
  not authoritative, so the check is skipped and the remote daemon validates at
  `net-start`.
- Use it when several hypervisors should map one stable network name onto their
  own local bridge — what live migration wants. Use `shared_networks:` instead
  when boxman should *create* the bridge here.
- ⚠ Moving a network between `nat`/`route` and `bridge` crosses bridge
  ownership: libvirt deletes a bridge it manages and preserves one it does not.
  Use different bridge names across that transition, or omit the managed side's
  `bridge.name` so boxman reserves a fresh automatic one.

### `shared_networks:` — host bridges for hybrid labs

A plain Linux bridge on the host: not a libvirt network, **not namespaced**.
Use it for cross-cluster L2 glue, hybrid libvirt + containerlab labs
(containerlab attaches via `endpoints: ["<node>:eth1", "host:<bridge>"]`),
container boxes via macvlan, or an external bridge.

Creation is idempotent: create if missing, bring up, apply `mtu` and `stp`
**when declared**. There is **no teardown** — `destroy` leaves shared bridges
alone because another project may still use one.

⚠ "boxman will not tear it down" is the *only* sense in which a shared bridge
is safe across projects. Bridge names are global, and every run re-writes the
settings it declares onto whatever bridge the name resolves to →
**last-run-wins**. What keeps you out of a co-tenant's way is the omissions:

| Setting | On every run | If a project omits it |
|---|---|---|
| bridge exists, link `up` | re-applied | — |
| `mtu` | applied only when declared | existing MTU left alone |
| `stp` | applied only when declared | left alone (a bridge boxman *creates* starts STP off) |
| `disable_netfilter` | acts only when `true` | the host-global sysctl is **not** restored |

So `stp: false` is not the same as omitting `stp` — the first is an opinion
written over anyone who asked for `true`. `disable_netfilter` cannot be undone
that way at all: once any project sets it, `bridge-nf-call-iptables` stays `0`
until a reboot or kubelet puts it back.

By default boxman installs a **scoped** per-bridge accept rule so bridged lab
frames survive a docker-style `FORWARD DROP` policy:

```
-i <br> -o <br> -m physdev --physdev-is-bridged -j ACCEPT
```

inserted into `FORWARD`, and into `DOCKER-USER` too when that chain exists
(docker recreates but does not flush `DOCKER-USER`, so that copy survives a
daemon restart). IPv4 only. `disable_netfilter: true` is the discouraged
host-wide alternative — prefer the default.

`stp` and `disable_netfilter` accept booleans, or `on`/`true`/`yes`/`1` and
`off`/`false`/`no`/`0`. Anything else is rejected, and a key with no value is
an error rather than a silent "leave it alone".

### `is_global: true`

Disables namespacing entirely; `network_source` is a literal libvirt network
name (e.g. `default`). Add `source_type: bridge` to name a host Linux bridge
instead. boxman will not create, validate or remove any of it.

⚠ `source_type: 'bridge'` **alone is not enough.** It is an internal marker
boxman sets for you when `network_source` names a `shared_networks` key;
nothing validates a hand-written one. Without `is_global: True`,
`network_source: br0` still gets namespaced and the attach fails on a
nonexistent bridge.

### DHCP reservations

Validated up front, because libvirt accepts spellings that define cleanly and
then hand out an unusable address:

- every reservation needs both `mac` and `ip`;
- the address must be inside the network and must not be the gateway, network
  or broadcast address;
- no `mac`, `ip` or `name` may repeat;
- a reservation *inside* the DHCP range is fine — dnsmasq excludes a reserved
  address from dynamic allocation;
- the optional `name` becomes the dnsmasq hostname.

⚠ A reservation `name:` may not contain `&`, `<`, `>`, `"` or `'`. libvirt
writes the name back out unescaped internally, so such a network defines fine
and then fails at `net-start` with `EntityRef: expecting ';'`. boxman rejects
it up front instead.

### Reconciliation — changing a network after it exists

Edit a network and the next `up` or `update` picks it up; no re-provision. What
happens depends on what changed, because libvirt only allows some sections to
be edited in place:

| Changed | Applied how | Disruptive? |
|---|---|---|
| `ip.dhcp.hosts` | `virsh net-update --live --config` | no — dnsmasq reloads, bridge stays up |
| `ip.dhcp.range` | same, as delete + add (libvirt refuses `modify` on a range) | no |
| network defined but stopped | started | no |
| network absent | defined and started | no |
| `mode`, `ip.address`, `ip.netmask`, `mac`, `bridge.name`, `bridge.stp`, `bridge.delay`, DHCP-option suppression | destroy + define again | **yes** — needs `--recreate-networks` |

`bridge.name` is compared only when the config pinned one. A guest holding a
lease keeps its old address until renewal, so a new reservation takes hold at
the next renewal or an `ifdown`/`ifup` in the guest.

Two values are normalised so they do not read as permanent drift: an unquoted
`stp: on` (YAML makes it boolean `True`; libvirt stores `on`, and anything
that is neither on nor off is rejected), and a short-form network `mac:` like
`52:54:0:a:b:c`, which libvirt zero-pads. A reservation `mac:` is *not* padded.

⚠ **Structural changes need `--recreate-networks`.** libvirt answers *"can't
update 'ip' section"* / *"can't update 'bridge' section"*, so the only way to
apply them is to destroy the network and define it again — which deletes the
bridge and leaves attached guests with a dead NIC. libvirt does not reconnect
them. Without the flag, boxman reports the drift and applies nothing. With it,
you get a prompt naming the affected VMs (skip with `-y`), and each one is
reconnected afterwards: a hot detach/attach where the machine type supports PCI
hotplug, otherwise a graceful reboot. Run with `-v` to see the plan lines.

---

## Multi-cluster projects

- boxman writes a combined workspace inventory *and* a per-cluster
  `inventory/01-hosts.yml` under each cluster's dir; each cluster's
  `inventory:` is auto-wired if undeclared.
- `--cluster <name>` repoints the workspace env (inventory, gateway,
  `ssh_config`, `ansible.cfg`) at one cluster's tree. Available on `run`,
  `ssh`, and the snapshot/storage/control verbs.
- `--vms a,b` targets individual VMs by bare or cluster-qualified name.

---

## Templates

- The `templates:` block builds base images via cloud-init, then snapshots
  them. Cloned VMs inherit that state.
- **Per-VM cloud-init customisation is not supported** — `cloudinit:` is
  per-template and shared across every VM cloned from it. For per-VM static
  IPs, use `match: macaddress:` blocks in the shared template so each VM only
  matches its own MAC.
- **Per-VM `base_image`** lets different VMs in one cluster clone from
  different templates.
- **Templates without `qemu-guest-agent`** make `up` slow and flaky for IP
  detection. boxman tries `domifaddr --source=lease` → `--source=arp` →
  `--source=agent`; only the agent failure logs loudly, but if all three return
  nothing the wait loop spins until timeout.
- **Big-package templates need bigger timeouts.** After `virt-install` boxman
  waits for the guest agent (`cloudinit_agent_timeout`, 300s), then for
  `guest-exec` to be usable (`cloudinit_guest_exec_timeout`, 120s), then polls
  for `cloudinit_done_marker` (`cloudinit_done_timeout`, 120s). A template that
  installs a large package set will blow past the marker poll — raise it
  (`cloudinit_done_timeout: 900`). When the guest cannot be polled at all,
  boxman falls back to a blind `cloudinit_fallback_timeout` wait (180s).
- **A failed verification fails the template.** If a configured
  `cloudinit_done_marker` never appears, `create-templates` logs *"cloud-init
  did not complete — template is not usable"*, shuts the VM down and exits
  non-zero rather than shipping a half-built base image; `provision` and `up`
  abort rather than cloning from it. The VM is left **defined and shut off**
  with the seed ISO attached and the cloud-init log dumped, so it can be
  inspected; `--force` rebuilds it. A template with no marker configured is not
  a hard failure — there is nothing to check, so boxman blind-waits and says so.
- Templates that bring no `cloudinit:` block of their own (including the
  implicit ones behind `base_image: oci://…`) get
  `/var/log/boxman-cloudinit.log` — the file the stock user-data's **last**
  `runcmd` entry appends to. When writing your own marker, remember
  `write_files` runs in cloud-init's *config* stage, long before `runcmd`, so a
  marker written there is found while the build is still going. Point the
  marker at something the last `runcmd` produces.
- **`clone_machine_id: auto | required | off`** controls resetting the identity
  a clone inherits — `/etc/machine-id`, ssh host keys and friends, via
  `virt-sysprep`. `auto` (default) warns and continues if the sanitizer cannot
  run, preserving opaque-appliance compatibility; `required` makes that a hard
  failure and removes the unsafe clone; `off` skips it. ⚠ `virt-sysprep` is
  often the one libvirt tool that still needs a password under a
  command-scoped sudoers policy.
- After editing a template's cloud-init, recreate it: `boxman create-templates
  --force`, or `boxman provision --rebuild-templates`.
- Base-image downloads are cached (default `~/.cache/boxman/images`,
  overridable via `cache.cache_dir` in the app config); checksums are verified
  on every read.

---

## ISO / live-ISO boot

For VMs that boot directly from an ISO — a live desktop, or an installer that
lays the OS onto an empty disk — instead of cloning a template. No
`base_image`, no cloud-init seed. Two pieces: a top-level `isos:` registry and
a VM whose `boot_order` starts with `cdrom`.

```yaml
isos:
  ubuntu-noble-live:
    uri: https://releases.ubuntu.com/24.04/ubuntu-24.04.4-desktop-amd64.iso
    checksum: sha256:3a4c98...

clusters:
  live:
    networks:
      live-net: { mode: nat, ip: { address: 192.168.90.1, netmask: 255.255.255.0,
                                   dhcp: { range: { start: 192.168.90.2, end: 192.168.90.254 } } } }
    vms:
      ubuntu-live01:
        vcpus: 2                        # NOTE: flat int, NOT cpus:{sockets,cores,threads}
        memory: 4096
        boot_order: [cdrom, hd]
        disk_size: 16G                  # empty boot disk the installer can target
        networks:                       # NOTE: networks:[{name}], NOT network_adapters:
          - name: live-net
        cdroms:
          - name: ubuntu-noble-live     # references the isos: entry
```

- ISOs download once, cache alongside base images, and are checksum-verified (a
  bad file is evicted so the next run re-downloads).
- **`boot_order` is a dispatch selector, not the firmware order.**
  `boot_order[0] == 'cdrom'` picks the ISO-boot path; the libvirt firmware
  order is hardcoded to `hd,cdrom` (disk first, falling through to CDROM). So
  the first boot finds the empty disk unbootable and boots the installer; once
  the OS is installed the disk wins. `[cdrom, hd]` and `[cdrom]` behave
  identically — anything past element 0 is ignored.
- **`cdroms:` entry forms:** a bare string, `{name: <iso-name>}` (resolved from
  `isos:`), or `{source: /abs/path.iso}` (explicit local file). The first cdrom
  is the boot ISO; extras attach afterwards.
- **Direct-install VMs use a simpler schema than cloned VMs:** `vcpus:` (flat
  int) instead of `cpus:{…}`, and `networks: [{name: …}]` for the first NIC
  instead of `network_adapters:`; `disk_size:` sets the empty boot disk. On a
  *cloned* VM those three keys are inert. Extra `disks:` work on both.
  Precisely: that is what the **create** path reads — a `cpus:` map on an ISO
  VM is still applied afterwards by the configure step, and an ISO VM using
  `networks:` logs a spurious `no network adapters defined for vm <x>,
  skipping` warning that is harmless.
- **Boot config is validated before any cloning starts.** An `hd`-boot VM needs
  a `base_image`; a `cdrom`-boot VM needs a first `cdroms:` entry that either
  names a known `isos:` key or carries an explicit `source:`; a `network`-boot
  VM needs neither. Problems are aggregated into one error.
- **Local runtime only** — a non-empty `isos:` block is rejected under
  `--runtime docker`, because the host image cache is not visible to the
  in-container `virt-install`. ⚠ The guard fires only when `isos:` is
  non-empty, so a VM booting via `cdroms: [{source: /abs/path.iso}]` with no
  `isos:` entry is **not** rejected and will fail later and messier inside the
  container.

---

## PXE boot (Cobbler / network install)

1. **Declare the VM with `boot_order: [network, hd]`.** boxman then creates a
   *bare VM* — empty qcow2, no clone, no cloud-init seed. `base_image` is
   ignored for that VM.
2. **Provision normally.** The DHCP/TFTP/HTTP comes from the PXE server, not
   from boxman.
3. **For a one-off re-install on an existing VM:**

```bash
boxman pxe-boot --vm <full-domain-name> \
                --expected-ip 192.168.123.50 \
                --wait-timeout 600 \
                --restore-after
```

That sets boot order to `[network, hd]`, starts the VM, polls SSH at
`--expected-ip`, and on success restores boot order to `[hd]`.

The libvirt network used for PXE must be the same network the PXE server's
DHCP/TFTP/HTTP serves on.

---

## Image management

| Feature | Purpose | Entry point |
|---|---|---|
| Image cache | Download a base image once, reuse it | Automatic |
| Template creation | Build a cloud-init-customised template VM | `boxman create-templates` |
| `import-image` | Define a libvirt VM from a `(disk + XML + manifest.json)` package | `boxman import-image` |
| `image push` | Publish a qcow2 + optional metadata to an OCI registry | `boxman image push` |
| `oci://` base images | Pull a VM disk from an OCI registry (boxman artifact or KubeVirt containerDisk) | `image.uri: oci://…` / `base_image: oci://…` |
| `image inspect` | Print an OCI manifest + metadata without downloading the blob | `boxman image inspect <ref>` |

**`import-image` manifest schema:**

```json
{ "xml_path": "vm/vm.xml", "image_path": "vm/disk.qcow2", "provider": "libvirt" }
```

`--uri` accepts `file://`, `http://`, `https://`; the manifest is downloaded
but `xml_path` / `image_path` still resolve relative to the manifest's
directory, so fully-remote packages are not supported yet. Schema validation
runs *before* any disk I/O. `provider` must be `libvirt`.

**OCI auth** for push and pull: `ORAS_USERNAME` / `ORAS_PASSWORD`, or
`~/.oras/config.json` (populated by `oras login`), or an interactive prompt.
The `oras` CLI must be on `PATH`; boxman does not reimplement OCI, and errors
include the full `oras` stderr.

⚠ `boxman import-image` defines the domain from vendor-supplied XML and injects
no `discard` attribute, so imported VMs need `discard='unmap'` added by hand
before `storage trim` will work on them.

---

## The docker-compose provider — container clusters (`version: '2.0'`)

A cluster can be made of **containers instead of VMs**. This is the *provider*
axis, unrelated to the docker *runtime*; the provider requires the **`local`
runtime** (mixing them is a config error with an explanatory message).

```yaml
version: '2.0'                 # REQUIRED — per-cluster providers are v2.0
project: shop

provider:
  docker-compose:
    project_name: shop         # compose project prefix (default: the project name)

clusters:
  web:
    provider: docker-compose
    workdir: ./.boxman/web     # where the generated docker-compose.yml is written
    readiness_timeout: 120     # seconds `up -d --wait` may take

    networks:
      appnet: { driver: bridge, subnet: 172.28.0.0/24 }   # cluster-internal, isolated

    boxes:                     # containers — the provider-neutral key (NOT vms:)
      cache:
        image: redis:7-alpine
        networks: [appnet]
        volumes:
          - { name: cache_data, container_path: /data }        # named (docker-managed)
      frontend:
        image: nginx:alpine
        volumes:
          - { host_path: ./site, container_path: /usr/share/nginx/html, readonly: true }
        compose_extra:         # escape hatch: deep-merged into the generated file verbatim
          deploy: { resources: { limits: { cpus: '0.50' } } }
```

Prerequisites: Docker Engine with the **compose v2** plugin (`docker compose
version`), and either group membership or `use_sudo: true` on the provider. No
KVM and no cloud images needed.

Each box becomes a compose *service*. boxman writes a real
`docker-compose.yml` into the cluster `workdir` — a **generated artifact**:
edit `conf.yml`, not the output. `provision` / `up` run `docker compose up -d
--wait`, so the command returns only once every service is `running`, or
`healthy` when the box declares a `healthcheck:`.

| Verb | Docker equivalent | Named volumes |
|---|---|---|
| `provision` / `up` | `up -d --wait` | created if missing |
| `down` | `stop` | kept |
| `deprovision` | `down` | **kept** |
| `destroy` | `down --volumes` | **removed** |
| `control suspend` / `resume` | `pause` / `unpause` | — |
| `control start` | `start` | — |
| `control save` | *(none)* | explains and skips |

- **Volumes:** a relative `host_path` resolves against the directory holding
  `conf.yml`, and boxman pre-creates it **as you** (left to docker, the daemon
  would create it root-owned). `workdir: <path>` on a box is shorthand for a
  bind of `.` at that path. `size:` on a named volume is **advisory** —
  docker's `local` driver cannot enforce a quota, so boxman warns.
- **Networking:** cluster-internal `networks:` are isolated. A box joining a
  top-level `shared_networks:` entry is attached with a **macvlan** whose
  parent is that host bridge, making it directly L2-adjacent to any VM on the
  same bridge.
- **Getting in:** `boxman ssh` stays VM-only; use `boxman exec <cluster>.<box>`.
- **Ansible:** containers appear in the generated inventory as ordinary hosts
  using the `community.docker` connection plugin, so `boxman run` and `tasks:`
  reach containers and VMs alike. Install `ansible-galaxy collection install
  community.docker` and `pip install docker` on the control host. ⚠ Ansible
  *modules* need a Python interpreter inside the container; minimal (alpine)
  images have none and fail with *"No python interpreters found"* — use
  `-m raw`, pick an image with Python, or use `boxman exec`.
- **Snapshots** are backed by `docker commit` into
  `boxman/<project>_<cluster>_<box>:<name>`, recorded in `snapshots.json` in
  the cluster workdir. ⚠ **Named-volume data is not part of a snapshot** —
  `docker commit` captures the writable layer only, so a restore rolls the
  container filesystem back and leaves volume data untouched. Back volumes up
  separately. A restore is a point-in-time recreate, **not a pin**: a later
  `boxman up` regenerates from `conf.yml`.

| Symptom | Cause |
|---|---|
| `requires runtime 'local'` | The provider was used under the `docker` **runtime**. |
| `'docker compose' … not available` | Compose v2 plugin missing, or docker needs `use_sudo: true`. |
| `snapshot 'x' already exists` | Names are single-use; delete first. Names differing only by punctuation collide too. |
| `No python interpreters found` | Ansible module against a minimal image. |
| `no containers to snapshot` | The cluster is not up. |
| Container state lost after `up` | Expected — the filesystem is rebuilt from the declared image. Persist data in a volume. |

---

## Updating a running cluster

`boxman update` is the non-destructive path; prefer it over `provision
--force` when only tweaking a running cluster.

- **CPU / memory**: hot-scaled up to the VM's live ceiling. Asking for more is
  **not an error** — boxman writes the persistent config and logs *"Restart
  needed for changes to take effect (live max ceiling cannot be raised on a
  running VM)"*. Power-cycle to pick it up. Set `max_vcpus` / `max_memory` at
  provision time to leave headroom; the ceilings themselves cannot be raised
  live.
- **`memballoon`**: `free_page_reporting` / `autodeflate` / `stats_period` are
  diffed and applied; some transitions are flagged restart-pending. Free-page
  reporting returns pages the guest has freed. Host-wide dedup of *live*
  identical pages is KSM — a host policy boxman does not manage.
- **Disks**: added at runtime; removed on next reboot.
- **`shared_folders`** (virtiofs): hot-attached with a persistent fallback to
  config-only on next boot. Hotplug needs QEMU 6.2+ / libvirt 8.6+; older hosts
  fall back gracefully.
- **`cdroms`**: attach / detach / swap by `target` device.
- **VM add/remove**: new VMs are added; orphaned ones prompt before removal
  (skip with `--yes`).
- **`--dry-run`** shows the diff without applying.

---

## Snapshots and storage reclaim

### Snapshots

- `snapshot take` snapshots selected VMs in parallel (default name = UTC
  timestamp); overlay disks are preserved. `--compress-memory` zstd-compresses
  the memory `.raw` (~70% smaller, transparently decompressed on restore).
  `--force` clears a colliding name or leftover files and re-takes.
- `snapshot restore [--name N]` restores (latest if omitted). `boxman restore`
  is the shortcut for "all VMs to latest".
- `snapshot list` is the raw libvirt list; `snapshot log [-n N] [--reverse]
  [--json] [--no-graph]` is a git-log-style aggregated view with an ASCII graph
  and a `← current` marker. Prefer `log` for a human overview.
- `snapshot delete --name N` deletes a named snapshot.
- `snapshot collapse --to N [--no-shutdown] [--dry-run] [-y]` merges every
  snapshot *newer* than `N` into the live disk head to reclaim space; `N` and
  older stay revertable. Offline-only: it auto-shuts running VMs down unless
  `--no-shutdown`, which skips them instead.

### `boxman storage`

qcow2 disks grow and do not shrink on their own.

| Command | What it does |
|---|---|
| `storage df` | Per-VM table: virtual size, allocated, chain depth, snapshot count and memory, estimated reclaimable space. **Start here.** |
| `storage trim [--dry-run]` | `fstrim` inside running guests via qemu-guest-agent. Needs disks with `discard='unmap'`. |
| `storage compact [--method auto\|sparsify\|convert\|convert-compressed] [--no-shutdown] [--drop-snapshots] [--dry-run]` | Shrink the qcow2 on the host. `auto` sparsifies when snapshots exist, else converts. Chain-flattening methods refuse to run with snapshots unless `--drop-snapshots`. |
| `storage optimize [--skip-trim] [--skip-compact] …` | Orchestrator: trim in-guest, then compact on the host. The one-shot "reclaim everything". |
| `storage compress-snapshots [--level L] [--decompress]` | Retroactively zstd-(de)compress existing snapshot memory files. |

- **`discard='unmap'` is the default on every disk boxman creates**, so
  `storage trim` works out of the box (clones inherit it). VMs created before
  that change do not have it — `virsh edit <dom>`, add `discard='unmap'` to the
  disk `<driver>`, reboot. `storage trim` warns when it is missing.
- `compact` / `optimize` / `collapse` power VMs down (unless `--no-shutdown`)
  and rewrite disk images. Treat them as slow and disruptive.

---

## Common failure modes

### `boxman up` waits forever for an IP, logs `Guest agent is not responding`

The agent failure is harmless if lease/arp work. The real problem is all three
sources returning nothing → the guest is not doing DHCP on the new NIC. Check
inside the VM via a console:

```
ip a              # any address?
nmcli c show      # any active profile bound to the new MAC?
journalctl -u NetworkManager -n 30
```

Usual root causes: a NetworkManager profile bound to the *template's* old MAC
(the clone has a new one, so nothing matches); a static netplan config from
cloud-init that does not match by MAC; or no DHCP client running at all. Fix
the template — install `qemu-guest-agent`, add match-by-MAC config — and
recreate it.

### `boxman ssh` raises "GATEWAYHOST is not set in the workspace environment"

`env.sh` is not where the loader looks. Move it to `workspace.files`, or set
`workspace.env_file:`.

### `ssh node01` (bare) → "Could not resolve hostname"

The alias is `<cluster>_<hostname>`. Read the generated `ssh_config` for the
real aliases and update inventory / scripts.

### VM exists in `virsh list` but boxman thinks it does not

The cache is out of sync. `boxman destroy -y` is cleanest; otherwise edit the
projects cache. Do not `virsh undefine` boxman-managed VMs by hand.

### Provision succeeds but `boxman ssh` connects to the wrong IP

`ssh_config` caches the IP at `up` time. If the VM rebooted with a new lease,
run `boxman up` again — it re-discovers the IP and rewrites the file.

### `cannot provision — existing VM(s)` / `project is already registered`

Pass `--force` (deprovisions first) or `boxman destroy` then re-up. The check
covers both live VMs and stale cache entries.

### `partial infrastructure state: the following VM(s) are missing: …`

Some VMs exist and some do not, and `up` refuses to guess. `--force`
deprovisions and re-provisions everything. If the missing VMs were newly
*added* to `conf.yml`, you want `boxman update` instead.

### `boxman update` does not apply the new CPU/memory

Exceeding the live ceiling is not an error — see the update section above.
Power-cycle the VM.

### A routed-network guest has an address but no connectivity

Expected for `mode: route` if the network was defined by an older boxman that
still advertises the unreachable bridge as gateway and DNS. Check with `virsh
net-dumpxml <name> | grep dhcp-option`; if nothing comes back, migrate it with
`boxman up --recreate-networks`. Also by design: a routed guest cannot resolve
names, and `boxman ssh` cannot reach it.

### Guests get an address and reach the host, but nothing past it

Docker and libvirt are fighting over the shared `filter` table — docker leaves
the `FORWARD` policy at `DROP` and libvirt's rules become collateral damage.
The prerequisites script detects both the acute and latent form; the README
section *"Docker and libvirt fight over the same firewall table"* has the fix.

### `bridge '<name>' is already in use by another active network`

Two networks want the same bridge. Pin distinct `bridge.name` values, or omit
them and let boxman allocate free `virbrX` names.

### ISO-boot VM keeps booting the installer

ISO-boot VMs use a hardcoded firmware order of `hd,cdrom` — the disk is tried
first and the ISO is the fallback. If it keeps landing in the installer, the OS
never got written to the `disk_size:` disk. A *live* ISO runs from RAM and
never installs — that is expected.

### `boxman image push` fails with "oras CLI not found"

Install `oras` and put it on `PATH`.

### `boxman pxe-boot` succeeds but SSH never comes up

The PXE server did not pick up the install. Check the NIC MAC is registered
there, that the libvirt network is the one its DHCP/TFTP serves, and that
`--expected-ip` matches the reservation.

### cloud-init password login fails on Python 3.13 / conda

Recent boxman hashes cloud-init passwords with `passlib`; older versions used
stdlib `crypt`, which is removed in 3.13 and broken on some conda/RHEL builds.
Upgrade boxman so `passlib` is present, and recreate the template.

---

## What not to do

- **Don't** `virsh destroy` / `virsh undefine` boxman-managed VMs by hand — the
  cache will not know and the next provision collides.
- **Don't** edit auto-generated `inventory/01-hosts.yml` or `ssh_config`; they
  are rewritten every `provision` / `up`. Edit `conf.yml` instead. (`env.sh`
  and `ansible.cfg` *are* preserved once they exist — but a hand-edited
  `env.sh` then silently shadows `conf.yml` changes, so prefer editing
  `workspace.files['env.sh']` and deleting the on-disk copy to force a regen.)
- **Don't** delete the storage-pool directory while boxman thinks the VMs
  exist.
- **Don't** flip `use_sudo: True` ↔ `False` mid-project — resources created by
  one mode are not visible to the other and you will orphan VMs.
- **Don't** bypass `provision`'s refusal to clobber existing state by clearing
  the cache by hand.
- **Don't** confuse `destroy` (full nuke) with `deprovision` (VMs + networks)
  or `destroy-runtime` (docker runtime only).

---

## Request → approach

| Request | Approach |
|---|---|
| "Add a VM" | Edit `clusters.<c>.vms.<new>` → `boxman update`. Not `provision`. |
| "Bump CPU/RAM on a running VM" | Edit `cpus`/`memory` → `boxman update`. Beyond the live ceiling it applies on the next power-cycle. |
| "Mount a host directory in a VM" | Add `shared_folders: [{name, host_path, readonly}]` → `boxman update`. Inside: `mount -t virtiofs <name> /mnt/...`. |
| "Attach an ISO" | Add `cdroms: [{name, source, target}]` → `boxman update`. |
| "PXE-boot a bare VM" | `boot_order: [network, hd]`, provision normally. One-off re-install: `boxman pxe-boot`. |
| "Boot from a live/install ISO" | `isos:` entry + a VM with `boot_order: [cdrom, hd]`, `disk_size:`, `cdroms:` — use `vcpus:`/`networks:` on that VM. Local runtime only. |
| "Use a registry image as the base" | `base_image: oci://registry/repo:tag`, or `templates.<k>.image.uri: oci://…`. `boxman image inspect` first. |
| "Push a built qcow2 to our registry" | `boxman image push registry/repo:tag --qcow2 <path>`. `oras login` first. |
| "Import a vendor VM package" | `boxman import-image --uri file:///path/manifest.json --name <vm> --directory <dir>`. |
| "Run containers instead of VMs" | `version: '2.0'`, `provider: docker-compose` on the cluster, `boxes:` → `boxman up`. Get in with `boxman exec`. Local runtime only. |
| "Attach a VM to a bridge that already exists" | `mode: bridge` + `bridge.name` (a stable libvirt name for it), or `is_global: True` + `source_type: bridge` (no libvirt network at all). Not `shared_networks:` — that is for bridges boxman should own. |
| "Make a network reachable without NAT" | `mode: route`, plus a route back on the upstream router. Accept the isolation contract, and add a `nat` adapter for management. |
| "Make two clusters talk through a router" | Two `shared_networks` (one per cluster ↔ router transit), an adapter on each, and a `containerlab:` router node with interfaces on both bridges. |
| "Two clusters' networks are talking when they shouldn't" | They are not — per-cluster networks are namespaced and L2-isolated. Look for a `shared_networks` adapter, an `is_global` adapter, or a `::`-qualified `network_source`. |
| "My VM has no internet" | `virsh net-list --all`; confirm `network_source` matches a defined network; check `iptables -L FORWARD -n -v`; check the guest's default route. |
| "Validate this conf.yml" | `boxman conf` renders Jinja, parses YAML and dumps the merged config. |
| "Run a one-off command on all hosts" | Define a `tasks:` entry → `boxman run <task>`, or `boxman run --cmd 'ansible all -m shell -a "<cmd>"'`. |
| "I broke the template, recreate it" | `boxman create-templates --force`. |
| "Reclaim disk" | `boxman storage df`, then `boxman storage optimize`. |
| "See / prune snapshot history" | `boxman snapshot log`; `boxman snapshot collapse --to <name>`. |
| "Turn up the logging" | `-v` / `-vv` / `-vvv` either side of the subcommand, or `BOXMAN_VERBOSITY=2`. `-q` for warnings only. |
| "Tear down only the docker runtime" | `boxman destroy-runtime`. |

---

## Where things live

| Path | Role |
|---|---|
| `<project_dir>/conf.yml` | Project config — the file the user edits. |
| `<project_dir>/conf.rendered.yml` | Jinja-rendered conf, written by `conf` / config load. Gitignored, safe to delete. |
| `~/.config/boxman/boxman.yml` | App-level config: defaults, runtime, ssh keys, image cache. |
| `~/.config/boxman/cache/projects.json` | Cache of provisioned projects and networks. Runtime-scoped. |
| `~/.cache/boxman/images/` | Image and ISO cache. |
| `<workspace.path>/env.sh` | Sourced by `boxman ssh` / `boxman run`. |
| `<workspace.path>/ssh_config` | OpenSSH config with `<cluster>_<hostname>` Host blocks. |
| `<workspace.path>/inventory/` | Ansible inventory (auto-generated unless overridden). |
| `<workspace.path>/<cluster>/` | Default cluster workdir when `cluster.workdir` is unset. |
| `<cluster.workdir>/` | VM qcow2 disks, libvirt storage pool. |
| `<cluster.workdir>/docker-compose.yml` | Generated compose file for a container cluster. Do not hand-edit. |
| `<cluster.workdir>/snapshots.json` | Snapshot ledger for a container cluster. |
| `~/boxman-templates/` | Default per-template workdir. |

### Reference docs in the boxman source

| Doc | Covers |
|---|---|
| `README.md` | Install, quick start, runtimes, the docker/libvirt firewall conflict |
| `doc/network.md` | The full networking model, with diagrams |
| `doc/storage.md` | qcow2 growth, reclaim commands, snapshot chains |
| `doc/image-management.md` | Image cache, templates, `import-image`, OCI push/pull |
| `doc/ksm.md` | Host-level KSM memory dedup |
| `doc/docker-compose-provider/user-guide.md` | Container clusters end to end |
| `doc/tutorial/README.md` | Guided walk-through from an empty host to a running cluster |
| `boxes/` | Runnable example configs — the best copy-paste starting points |

---

## Useful shortcuts

- `boxman conf | less` — exactly what boxman parsed.
- `boxman conf --json | jq .` — same, machine-readable.
- `cat <workspace.path>/ssh_config` — the real SSH aliases.
- `cat <workspace.path>/env.sh` — what `boxman ssh` will source.
- `boxman ps -p` — states plus provider info.
- `virsh list --all`, `virsh net-list --all` — ground truth for libvirt.
- `jq . ~/.config/boxman/cache/projects.json` — what boxman thinks is provisioned.
