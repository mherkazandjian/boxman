# proxmox-nested-ceph-cluster

A four-node **Proxmox VE 9** cluster with **Ceph**, running as nested-KVM
guests on **two physical hosts** (two nodes each), joined into one L2 by a
VXLAN, with a nested Ubuntu VM that is **live-migrated between the two
physical hosts** as the demo. boxman runs on each host with the same
`conf.yml`; a workstation-side `Makefile` sequences the two sites and drives
the Proxmox/Ceph steps over SSH.

```
 workstation ──ssh──▶ host1 (192.168.139.57)            host2 (192.168.139.58)
                      ┌──────────────────────┐          ┌──────────────────────┐
                      │ libvirt nat network   │  vxlan   │ shared bridge br-pve │
                      │ virbr-pve 10.77.0.1   │◀════════▶│ (mode: bridge net,   │
                      │ dnsmasq: DHCP/DNS/NAT │ id 7700  │  no dnsmasq)         │
                      │ for the WHOLE L2      │ udp 4789 │                      │
                      │  pve1 .11   pve2 .12  │ mtu 1450 │  pve3 .13   pve4 .14 │
                      │  demo01 .50 (nested)  │          │  ◀── demo01 migrates │
                      └──────────────────────┘          └──────────────────────┘
```

What is boxman-specific here, compared to
[`ubuntu-24.04-live-iso-boot`](../ubuntu-24.04-live-iso-boot):

- **One `conf.yml`, two renders.** `BOXMAN_SITE=host1|host2` picks the site
  (Jinja `env()`); the project name is the same on both hosts, the VM sets
  differ.
- **host1 is the DHCP side for both hosts.** Its `mode: nat` network reserves
  the MAC of *every* node, including the two that live on host2, and hands
  out the hostname too (`name:`) — that is what makes one generic
  auto-install ISO work for all four nodes.
- **host2 attaches ISO VMs to a host bridge** with a `shared_networks:` entry
  plus a `mode: bridge` network of the same bridge; `ensure_shared_bridges()`
  runs before `define_networks()` so the order works out.
- **`networks[].mac` on ISO-boot VMs** pins the NIC to its reservation (new
  in this change; PXE VMs get it too).
- **An unattended *installer* ISO, not a live one.** The node installs,
  powers off, and the next `boxman up` boots it from disk (see "How the boot
  works" below).
- **Nested virtualisation** via `virt_install_extra_args: ['--cpu
  host-passthrough']` and a guest-agent channel, Ceph OSDs on extra `disks:`.

Verified end to end on 2026-09-06 (host1/host2: Rocky 9.8, EPYC 9825, libvirt
11.10, PVE 9.2-1 ISO → pve-manager 9.2.2, Ceph 20.2.2 tentacle):

| Step | Result |
|---|---|
| unattended install, per node | ~5–10 min, node powers off, next `boxman up` boots it from disk |
| DHCP over the VXLAN | pve3/pve4 leased .13/.14 + hostnames from host1's dnsmasq |
| first-boot hook | FQDN `pveN.pve.lab`, vmbr0 MTU 1450, 16 vCPUs with `svm`, `/dev/kvm` |
| MTU check pve1 → pve3 | 1450-byte frames pass with DF, 1500-byte frames refused |
| cluster | 4 nodes, quorate |
| Ceph | 3 mons, 2 mgrs, 8 OSDs, HEALTH_OK, `vmpool` 162 GiB usable |
| nested VM | `demo01` (Ubuntu 24.04 on `vmpool`) up and ssh-able 28 s after `qm start` |
| live migration pve1 → pve3 (host1 → host2) | 8–10 s, downtime 466 ms, 512 MiB/s; 0 of 55 pings lost (5/s) |
| live migration pve3 → pve1 (back) | 7 s, downtime 53 ms, 1.0 GiB/s; 3 of 49 pings lost; guest uptime uninterrupted |

## Read this first: what this box does *not* do

- It is **not HA**, and neither physical host's loss is survivable. Two
  separate things stop it:
  - **Corosync quorum.** Four voting nodes with no external voter (no QDevice)
    need three votes for a majority. Losing either host leaves two of four, so
    the survivors go inquorate and stop acting — this applies to host1 *and*
    host2.
  - **Ceph monitors.** Two of the three (mon.pve1, mon.pve2) are on host1, so
    losing that host also loses the monitor majority and the storage.

  It demonstrates clustering, shared storage, live migration and recovery from
  a single *nested* node failure — not fault tolerance of a physical host.
- It **stretches the L2 with a VXLAN that boxman does not manage.**
  `scripts/vxlan-up.sh` creates it (idempotently) and the Makefile runs it at
  the right moments; a host reboot or `boxman destroy` on host1 (which deletes
  the libvirt bridge) needs it re-run.
- It runs at **MTU 1450** inside the lab (VXLAN over a 1500-byte VLAN). The
  first-boot hook sets it on the nodes; the nested VM inherits it.
- It **does not migrate the Proxmox nodes themselves** between hosts; boxman
  has no migrate verb. The migration is of the *nested* VM, by Proxmox.

## Prerequisites

On both hosts (checked by `make host-prep`): nested KVM on, `/dev/kvm`
usable, libvirt + `virt-install`, docker (ISO build), `python3.12` + venv,
passwordless sudo, the `vxlan` module, a **usable firewalld** (`vxlan-up.sh`
binds the lab bridge to a zone and opens the tunnel port), `jq` on the
orchestration host (`host1` — the migration and HA scripts parse JSON there,
and the nodes themselves have no `jq`), internet egress, ~100 GB free under
`~/pve-lab`. On the workstation: ssh aliases `host1`/`host2`, `rsync`,
`openssl`, `ssh-keygen`, and `jq` + `terraform` for the Terraform and HA
targets.

Site-specific values live in one place, `scripts/lib.sh` (host IPs, the VLAN
interface `bond0.1439`, node IPs/MACs, VXLAN id), and must match `conf.yml`
(`tests/test_proxmox_nested_box.py` pins the MAC ↔ reservation contract).

## Bring it up

Everything runs from this directory on the workstation. `make help` lists
the targets; `make all` runs them in order (45–90 min, mostly waiting):

```bash
make keys answer        # lab ssh keypair + answer.toml (root pw hash, pubkey); both gitignored
make sync               # rsync the repo + keys to ~/pve-lab on both hosts, venv, pip install -e
make host-prep          # host checks + directories
make iso                # download proxmox-ve_9.2-1.iso, bake answer.toml + first-boot.sh (both hosts, ~5 min)
make up                 # host2 vxlan → host1 boxman up → host1 vxlan → host2 boxman up
make wait-installed     # nodes install unattended (~6–10 min) and power off
make boot               # boxman up #2: start from disk; first-boot hook runs (repos, MTU, agent)
make wait-first-boot
make mtu-check          # 1450-byte frames pass with DF, 1500-byte ones are refused
make cluster            # pvecm create on pve1, pvecm add pve2..pve4
make ceph               # pveceph install ×4, init, 3 mons, 2 mgrs, 8 OSDs, pool vmpool (+storage)
make demo               # nested Ubuntu VM demo01 on pve1, disk on vmpool, DHCP from host1 → 10.77.0.50
make migrate            # live-migrate demo01 pve1 → pve3 (host1 → host2) while pinging it
make migrate TARGET=pve1   # and back
```

`PVE_ROOT_PASSWORD` (default `pvelab123`) is the nodes' root password;
`LAB` (default `/home/mher/pve-lab`) the lab directory on the hosts. The same
scripts are exposed as `tasks:` for use on a host directly, e.g.
`BOXMAN_SITE=host2 boxman run vxlan-up`.

### Expected noise

- `boxman up` on **host2** waits the full IP-discovery timeout (600 s on the
  first run, 300 s later) before reporting: the guests there have no libvirt
  DHCP lease (host1's dnsmasq serves them) and no guest agent until the
  first-boot hook installs it. The installs run meanwhile; nothing is lost.
- `boxman up` ends with `ERROR: failed to add ssh keys to some vms` on both
  hosts, as on every ISO-boot box: there is no `admin_pass` and nothing to
  push a key into — the answer file already authorises the lab key for root.
- On host1 `boxman ssh pve1` (alias `pve_pve1`) works once the node has an
  address; on host2 the ssh_config stays empty until the agent is up.

## How the boot works (why there is no reinstall loop)

`boot_order: [cdrom, hd]` selects boxman's ISO-boot path, which runs
`virt-install --cdrom … --boot hd,cdrom`. virt-install then does a two-phase
install: the *transient* domain boots cdrom-first with `on_reboot=destroy`,
while the *persistent* definition it writes boots `hd,cdrom` with the install
media ejected. The answer file says `reboot-mode = "power-off"`, so after the
unattended install the domain is simply `shut off`; `make wait-installed`
watches for that, and `make boot` (`boxman up` again) starts the persistent
definition from disk. The first-boot hook (`scripts/first-boot.sh`, baked into
the ISO) then disables the enterprise repos, enables `pve-no-subscription`,
sets MTU 1450 on `vmbr0`, runs a **`dist-upgrade`**, installs
`qemu-guest-agent` and writes `/var/lib/pve-lab/first-boot.done`, which
`make wait-first-boot` polls before anything touches the node.

### Why the nodes are upgraded on first boot

The ISO's packages are frozen when it is built, but `pveceph install
--repository no-subscription` takes whatever Ceph is *current*. Without the
upgrade a node therefore pairs ISO-era PVE with a newer Ceph, and that
combination can be fatal: on 2026-09-15, Ceph 20.2.4 issued `aes256k` keys
(32 bytes, base64 ending in a single `=`) while `libpve-storage-perl` 9.1.5's
rbd keyring check demanded `==`. PVE rejected the keyring PVE itself had
written, every rbd storage stayed `inactive`, and `make demo` failed against a
`HEALTH_OK` cluster. It is fixed upstream in `libpve-storage-perl` 9.1.10 — the
nodes simply had 9.1.5, because nothing ever upgraded them. An earlier build on
2026-09-06 worked only because the ISO's PVE and the repo's Ceph were still in
step; that was luck with a nine-day shelf life, not a working design.

Two consequences worth knowing:

- **The upgrade stages a kernel the node is not running.** `proxmox-kernel`
  updates take effect at the next reboot, which this box never performs after
  first boot. Harmless for a lab; reboot a node if you need the new kernel.
- **A build is only as reproducible as the repo on the day.** Upgrading makes
  both halves move together rather than one, which is the point, but it does not
  pin them. If you need a byte-reproducible lab, pin `PVE_CEPH_VERSION` *and*
  the PVE packages — and accept that you are then shipping a deliberately frozen
  stack that will drift out of support.

This relies on the post-create CPU/memory step editing the persistent config
(`dumpxml --inactive`) — the fix that ships with this box. Before it, boxman
copied the transient install XML over the persistent one, and every ISO-boot
VM with `memory:` set would have re-run its installer on the second boot.

## Verify

```bash
make status                          # boxman ps on both hosts, pvecm status, ceph -s, qm list
ssh host1 'bridge link show master virbr-pve'      # vnet ports + vxlan-pve
ssh host2 'bridge link show master br-pve'
ssh host1 'ping -c2 10.77.0.13'                    # host1 → a node on host2, over the VXLAN
ssh host1 'virsh -c qemu:///system dumpxml bprj__pvelab__bprj_pve_pve1 --inactive | grep -A1 "<os>\|<cpu "'
ssh -L 8006:10.77.0.11:8006 host1                  # then https://localhost:8006, root / PVE_ROOT_PASSWORD
```

On a node (`ssh -i keys/id_ed25519_pvelab root@10.77.0.11` from host1):
`pvecm status` (4 nodes, quorate), `ceph -s` (HEALTH_OK, 8 OSDs), `pvesm
status` (`vmpool` on every node), `qm list`, `grep -c svm /proc/cpuinfo` and
`ls -l /dev/kvm` (the node is itself a KVM host).

## Terraform on top of the cluster

`terraform/` creates Rocky 9 VMs on the cluster with the
[`bpg/proxmox`](https://registry.terraform.io/providers/bpg/proxmox) provider,
API-only: each node downloads the GenericCloud qcow2 into `local:import/`
itself (`proxmox_download_file`), every VM imports it onto Ceph
(`disk { import_from }`, so the VMs stay live-migratable), and cloud-init uses
the provider's built-in `user_account` / `ip_config` (user `rocky`, the lab
key, DHCP from host1). No ssh from the workstation to the nodes is needed.
Terraform runs on the workstation through an ssh tunnel to pve1's API:

```bash
ssh -N -L 8006:10.77.0.11:8006 host1 &      # API tunnel (also serves the web UI)
make tf-token                              # API token root@pam!terraform -> terraform/.env (gitignored)
make tf-init
make tf-apply VM_COUNT=4                   # rocky01..04, one per node, 2 vCPU / 2 GiB / 16 GiB on vmpool
make tf-output                             # nodes, ids, addresses reported by the guest agent
make tf-destroy
```

Knobs are variables in `terraform/variables.tf` (`vm_count`, `cores`,
`memory_mb`, `disk_gb`, `nodes`, `image_url`, …). VM ids start at 200 (the
box's demo VM is 100). Placement is round-robin over `nodes`, i.e. two VMs per
physical host for `vm_count = 4`; `qm migrate <id> <node> --online` (or the
UI) moves them across hosts like the demo VM.

## HA, placement and Terraform: who owns what

Proxmox does not move VMs on its own unless they are HA resources. Once they
are, the HA manager restarts them elsewhere when a node dies, and with the
cluster resource scheduler (CRS) it may live-migrate them to balance load.
That conflicts with a Terraform config that asserts `node_name`, so the
ownership is split like this:

| concern | owner | where |
|---|---|---|
| VM shape (CPU, RAM, disks, cloud-init, HA membership) | Terraform | `terraform/main.tf`, `terraform/ha.tf` |
| initial node | Terraform (round-robin, `node_overrides`) | `terraform/main.tf` |
| where a VM runs afterwards, whether HA has it started | Proxmox | `lifecycle { ignore_changes = [node_name, started] }` |
| placement policy: CRS mode, auto-rebalance, node-affinity rules | Proxmox, applied by a script | `scripts/pve-ha.sh` (`make ha`) |

The provider has no resource for HA *rules* (PVE 9 replaced HA groups with
node-/resource-affinity rules), which is why the policy lives in a script:

- `crs: ha=dynamic` (static + live CPU/RAM usage), `ha-auto-rebalance=1` with
  threshold 20 %, margin 10 %, hold 3 HA rounds (~30 s), `ha-rebalance-on-start=1`
  — Proxmox VE 9.2's built-in balancer; no ProxLB or cron needed.
- rules `prefer-host1` (vm:200, 201, 204, 205 → pve1:1, pve2:1) and `prefer-host2`
  (vm:202, 203, 206, 207 → pve3:1, pve4:1), non-strict: equal priorities let
  CRS balance within a host.

  **Membership comes from Terraform's configured placement**, not from the VM
  id: `make ha` reads the `ha_policy_homes` output and hands it to
  `scripts/pve-ha.sh`, which requires it and has no fallback. So changing
  `node_overrides`, `nodes` or `vm_id_base` changes the affinity rules on the
  next `make ha`. The output is derived from the *declared* placement and
  deliberately not from `node_name` in state: `ignore_changes` lets the HA
  manager move a VM, and feeding that back would make the rule follow the drift
  it exists to correct. A registered HA resource this configuration does not
  own is left out of both rules rather than guessed at.

  Non-strict affinity *permits* placement on the other host, but in this lab
  that fallback cannot actually happen. Both of a group's preferred nodes are
  down only when their physical host is gone, and with four voting nodes and no
  external voter that leaves two of four votes — no quorum, so nothing is
  recovered anywhere. What the failover drill demonstrates is the case that
  does work: **one nested node lost, its guests fenced and restarted on the
  surviving node of the same host.** See "what this box does *not* do".

```bash
make tf-apply          # VMs + HA resources
make ha                # CRS + rules (idempotent)
make ha-status
make ha-failover NODE=pve4   # virsh destroy pve4 on host2, watch HA restart its VMs, boxman up brings pve4 back
```

Observed on 2026-09-06 with two CPU burners in each of the three VMs on pve2
(`ha-manager status` polled every 10 s): imbalance 11.9 % → 20.3 % → 35.2 %
within 22 s; at +32 s the HA manager live-migrated vm:201 to pve1 (inside its
`prefer-host1` rule); imbalance fell to 19 % and settled at ~18 %, below the
threshold, so nothing else moved.

Node failure, same day (`make ha-failover NODE=pve4`, i.e. `virsh destroy` of
the pve4 domain on host2): HA marked vm:203/vm:207 `fence` at +175 s (pve4 was
also the HA master, so a new master had to be elected first), restarted both
on pve3 per `prefer-host2`, and had them `started` at +206 s. `boxman up` on
host2 brought pve4 back 15 s later; within a minute the balancer moved vm:206
from pve3 (four VMs) to the empty pve4, imbalance 24 % → 11 %.

Because `node_name` is ignored, `terraform plan` stays clean after HA moves a
VM: only the `vms`/`ssh` outputs show the new nodes, no resource changes.
(The provider now prefers the shorter resource name `proxmox_haresource`; the
config keeps `proxmox_virtual_environment_haresource` until a state move is
worth doing.) If you would rather have Terraform own placement, drop the two entries
from `ignore_changes`, keep `migrate = true`, use `node_overrides`, and do not
enable auto-rebalance.

## Tear down

```bash
make down                # boxman down (save) on both hosts
make destroy             # boxman destroy -y on both: nodes, disks, host1's nat bridge
make host-clean          # vxlan-pve, host2's br-pve, the firewalld rule
```

`~/pve-lab/iso` (the ISOs), the venv and the synced source stay; delete
`~/pve-lab` by hand if you want the hosts pristine. Note that `boxman destroy`
deletes the file `admin_key_name` points at (it treats it as a generated key),
i.e. the lab key on that host — `make destroy` re-syncs it from `keys/`
afterwards, and a manual `boxman destroy` on a host needs a `make sync` before
the next `make up`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `wait-installed` times out, domain stays `running` | The installer failed and dropped to its shell (`reboot-on-error = false`). `virsh vncdisplay <domain>` and look; on host2 the usual cause is no DHCP because the VXLAN was not up before `boxman up` — run `make up` in the documented order. |
| Installer says the DHCP server must provide a host name | Either no DHCP offer arrived at all (`No DHCPOFFERS received` just above it on the console: see the next row) or the reservation's `name:` is missing / the node's MAC does not match it. `virsh net-dumpxml bprj__pvelab__bprj__clstr__pve__clstr__pvenet` on host1 shows the reservations; `virsh domiflist <domain>` the MAC. |
| host2 guests get no DHCP although `ping 10.77.0.1` from host2's `br-pve` address works | firewalld. With `br_netfilter` loaded (docker loads it), *bridged* frames traverse firewalld's forward hook, and a bridge bound to no zone ends in the public zone's `reject with icmpx admin-prohibited` — host-originated packets use INPUT/OUTPUT and never see it (tunnel counters told the story: 29 sent, 3 received). boxman's physdev `ACCEPT` is in the iptables table and cannot override a later nftables reject. `vxlan-up.sh` binds `br-pve` to the `trusted` zone; verify with a netns + veth on `br-pve` pinging `10.77.0.1` (a plain ping from the host proves nothing). TX checksum offload on the VXLAN was tested and is *not* a factor. |
| Installer aborts with `root disk '/dev/vda' too small (0 GB < 2 GB)` | The domain got `<driver type='raw'>` for the qcow2 boot disk, so the guest saw the 197 KiB file as a raw disk (`virsh domblkinfo <dom> vda` shows a tiny capacity). virt-install's `format=` only governs volume *creation*; for a file that already exists it takes the type from the libvirt pool's volume record, which was stale after a destroy plus a manual `rm` of images. boxman now passes `driver.type=qcow2` explicitly (fix shipped with this box). If you removed images by hand, `virsh pool-refresh vms` before the next `boxman up`. |
| A node that was still *installing* is suddenly `shut off` | Do not `virsh reset`/`reboot` a node during the install: the transient install domain has `on_reboot=destroy`, which libvirt also applies to a reset, and the persistent definition has no install media. Re-provision the site (`boxman destroy -y`, `make sync`, `make up` order) instead. |
| `virsh net-start` fails on host1: bridge in use | `virbr-pve` already existed (a leftover from `vxlan-up.sh` run too early). `sudo ip link del virbr-pve`, then `boxman up`, then `vxlan-up.sh`. |
| Nodes on host2 lose connectivity after a host reboot / `boxman destroy` on host1 | The VXLAN is not persistent, and host1's bridge is recreated by libvirt. `make boot` (or `vxlan-up.sh` on both hosts) restores it. |
| ssh works but `pvecm`/`pveceph` fail with a dpkg lock | The first-boot hook is still running apt. Use `make wait-first-boot`. |
| `Not a proper rbd authentication file` and `vmpool` `inactive` on a HEALTH_OK cluster | PVE's storage layer is older than the Ceph it installed (see *Why the nodes are upgraded on first boot*). Check `apt-cache policy libpve-storage-perl` on a node; the first-boot `dist-upgrade` is what prevents it. |
| `pvecm add` prompts or refuses | `--use_ssh` needs root ssh between nodes: `pve-cluster.sh` pushes the lab key and known_hosts first; re-run it. |
| `ceph -s` stuck below HEALTH_OK | Fresh OSDs peer for a minute or two; the script waits up to 5 min. Clock skew between the *hosts* (host2 runs a few minutes behind host1) is corrected inside the nodes by chrony via NAT, but check `ceph time-sync-status` if mons complain. |
| Big transfers hang while ping works | MTU: something is at 1500 on a 1450 path. `make mtu-check`; `ip -d link show vxlan-pve`, `ip link show vmbr0` on the node. |
| Nested VM has no address | It gets DHCP from host1 through the node's `vmbr0`; check the reservation `demo01`, `qm config 100` (`net0 … mtu=1`), and that the node's bridge carries the VM's MAC (`brctl showmacs vmbr0`). |

Do **not** run `boxman update` on these projects: the cdrom reconcile would
re-insert the install ISO. Harmless with the hd-first persistent boot order,
but a surprise.
