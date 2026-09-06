# proxmox-nested-ceph-cluster

A four-node **Proxmox VE 9** cluster with **Ceph**, running as nested-KVM
guests on **two physical hosts** (two nodes each), joined into one L2 by a
VXLAN, with a nested Ubuntu VM that is **live-migrated between the two
physical hosts** as the demo. boxman runs on each host with the same
`conf.yml`; a workstation-side `Makefile` sequences the two sites and drives
the Proxmox/Ceph steps over SSH.

```
 workstation ──ssh──▶ hpe1 (192.168.139.57)            hpe2 (192.168.139.58)
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

- **One `conf.yml`, two renders.** `BOXMAN_SITE=hpe1|hpe2` picks the site
  (Jinja `env()`); the project name is the same on both hosts, the VM sets
  differ.
- **hpe1 is the DHCP side for both hosts.** Its `mode: nat` network reserves
  the MAC of *every* node, including the two that live on hpe2, and hands
  out the hostname too (`name:`) — that is what makes one generic
  auto-install ISO work for all four nodes.
- **hpe2 attaches ISO VMs to a host bridge** with a `shared_networks:` entry
  plus a `mode: bridge` network of the same bridge; `ensure_shared_bridges()`
  runs before `define_networks()` so the order works out.
- **`networks[].mac` on ISO-boot VMs** pins the NIC to its reservation (new
  in this change; PXE VMs get it too).
- **An unattended *installer* ISO, not a live one.** The node installs,
  powers off, and the next `boxman up` boots it from disk (see "How the boot
  works" below).
- **Nested virtualisation** via `virt_install_extra_args: ['--cpu
  host-passthrough']` and a guest-agent channel, Ceph OSDs on extra `disks:`.

Verified end to end on 2026-09-06 (hpe1/hpe2: Rocky 9.8, EPYC 9825, libvirt
11.10, PVE 9.2-1 ISO → pve-manager 9.2.2, Ceph 20.2.2 tentacle):

| Step | Result |
|---|---|
| unattended install, per node | ~5–10 min, node powers off, next `boxman up` boots it from disk |
| DHCP over the VXLAN | pve3/pve4 leased .13/.14 + hostnames from hpe1's dnsmasq |
| first-boot hook | FQDN `pveN.pve.lab`, vmbr0 MTU 1450, 16 vCPUs with `svm`, `/dev/kvm` |
| MTU check pve1 → pve3 | 1450-byte frames pass with DF, 1500-byte frames refused |
| cluster | 4 nodes, quorate |
| Ceph | 3 mons, 2 mgrs, 8 OSDs, HEALTH_OK, `vmpool` 162 GiB usable |
| nested VM | `demo01` (Ubuntu 24.04 on `vmpool`) up and ssh-able 28 s after `qm start` |
| live migration pve1 → pve3 (hpe1 → hpe2) | 8–10 s, downtime 466 ms, 512 MiB/s; 0 of 55 pings lost (5/s) |
| live migration pve3 → pve1 (back) | 7 s, downtime 53 ms, 1.0 GiB/s; 3 of 49 pings lost; guest uptime uninterrupted |

## Read this first: what this box does *not* do

- It is **not HA**: three Ceph monitors on two hosts cannot survive losing
  hpe1. It demonstrates clustering, shared storage and live migration, not
  fault tolerance.
- It **stretches the L2 with a VXLAN that boxman does not manage.**
  `scripts/vxlan-up.sh` creates it (idempotently) and the Makefile runs it at
  the right moments; a host reboot or `boxman destroy` on hpe1 (which deletes
  the libvirt bridge) needs it re-run.
- It runs at **MTU 1450** inside the lab (VXLAN over a 1500-byte VLAN). The
  first-boot hook sets it on the nodes; the nested VM inherits it.
- It **does not migrate the Proxmox nodes themselves** between hosts; boxman
  has no migrate verb. The migration is of the *nested* VM, by Proxmox.

## Prerequisites

On both hosts (checked by `make host-prep`): nested KVM on, `/dev/kvm`
usable, libvirt + `virt-install`, docker (ISO build), `python3.12` + venv,
passwordless sudo, the `vxlan` module, internet egress, ~100 GB free under
`~/pve-lab`. On the workstation: ssh aliases `hpe1`/`hpe2`, `rsync`,
`openssl`, `ssh-keygen`.

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
make up                 # hpe2 vxlan → hpe1 boxman up → hpe1 vxlan → hpe2 boxman up
make wait-installed     # nodes install unattended (~6–10 min) and power off
make boot               # boxman up #2: start from disk; first-boot hook runs (repos, MTU, agent)
make wait-first-boot
make mtu-check          # 1450-byte frames pass with DF, 1500-byte ones are refused
make cluster            # pvecm create on pve1, pvecm add pve2..pve4
make ceph               # pveceph install ×4, init, 3 mons, 2 mgrs, 8 OSDs, pool vmpool (+storage)
make demo               # nested Ubuntu VM demo01 on pve1, disk on vmpool, DHCP from hpe1 → 10.77.0.50
make migrate            # live-migrate demo01 pve1 → pve3 (hpe1 → hpe2) while pinging it
make migrate TARGET=pve1   # and back
```

`PVE_ROOT_PASSWORD` (default `pvelab123`) is the nodes' root password;
`LAB` (default `/home/mher/pve-lab`) the lab directory on the hosts. The same
scripts are exposed as `tasks:` for use on a host directly, e.g.
`BOXMAN_SITE=hpe2 boxman run vxlan-up`.

### Expected noise

- `boxman up` on **hpe2** waits the full IP-discovery timeout (600 s on the
  first run, 300 s later) before reporting: the guests there have no libvirt
  DHCP lease (hpe1's dnsmasq serves them) and no guest agent until the
  first-boot hook installs it. The installs run meanwhile; nothing is lost.
- `boxman up` ends with `ERROR: failed to add ssh keys to some vms` on both
  hosts, as on every ISO-boot box: there is no `admin_pass` and nothing to
  push a key into — the answer file already authorises the lab key for root.
- On hpe1 `boxman ssh pve1` (alias `pve_pve1`) works once the node has an
  address; on hpe2 the ssh_config stays empty until the agent is up.

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
sets MTU 1450 on `vmbr0`, installs `qemu-guest-agent` and writes
`/var/lib/pve-lab/first-boot.done`, which `make wait-first-boot` polls before
anything touches the node.

This relies on the post-create CPU/memory step editing the persistent config
(`dumpxml --inactive`) — the fix that ships with this box. Before it, boxman
copied the transient install XML over the persistent one, and every ISO-boot
VM with `memory:` set would have re-run its installer on the second boot.

## Verify

```bash
make status                          # boxman ps on both hosts, pvecm status, ceph -s, qm list
ssh hpe1 'bridge link show master virbr-pve'      # vnet ports + vxlan-pve
ssh hpe2 'bridge link show master br-pve'
ssh hpe1 'ping -c2 10.77.0.13'                    # hpe1 → a node on hpe2, over the VXLAN
ssh hpe1 'virsh -c qemu:///system dumpxml bprj__pvelab__bprj_pve_pve1 --inactive | grep -A1 "<os>\|<cpu "'
ssh -L 8006:10.77.0.11:8006 hpe1                  # then https://localhost:8006, root / PVE_ROOT_PASSWORD
```

On a node (`ssh -i keys/id_ed25519_pvelab root@10.77.0.11` from hpe1):
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
key, DHCP from hpe1). No ssh from the workstation to the nodes is needed.
Terraform runs on the workstation through an ssh tunnel to pve1's API:

```bash
ssh -N -L 8006:10.77.0.11:8006 hpe1 &      # API tunnel (also serves the web UI)
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

## Tear down

```bash
make down                # boxman down (save) on both hosts
make destroy             # boxman destroy -y on both: nodes, disks, hpe1's nat bridge
make host-clean          # vxlan-pve, hpe2's br-pve, the firewalld rule
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
| `wait-installed` times out, domain stays `running` | The installer failed and dropped to its shell (`reboot-on-error = false`). `virsh vncdisplay <domain>` and look; on hpe2 the usual cause is no DHCP because the VXLAN was not up before `boxman up` — run `make up` in the documented order. |
| Installer says the DHCP server must provide a host name | Either no DHCP offer arrived at all (`No DHCPOFFERS received` just above it on the console: see the next row) or the reservation's `name:` is missing / the node's MAC does not match it. `virsh net-dumpxml bprj__pvelab__bprj__clstr__pve__clstr__pvenet` on hpe1 shows the reservations; `virsh domiflist <domain>` the MAC. |
| hpe2 guests get no DHCP although `ping 10.77.0.1` from hpe2's `br-pve` address works | firewalld. With `br_netfilter` loaded (docker loads it), *bridged* frames traverse firewalld's forward hook, and a bridge bound to no zone ends in the public zone's `reject with icmpx admin-prohibited` — host-originated packets use INPUT/OUTPUT and never see it (tunnel counters told the story: 29 sent, 3 received). boxman's physdev `ACCEPT` is in the iptables table and cannot override a later nftables reject. `vxlan-up.sh` binds `br-pve` to the `trusted` zone; verify with a netns + veth on `br-pve` pinging `10.77.0.1` (a plain ping from the host proves nothing). TX checksum offload on the VXLAN was tested and is *not* a factor. |
| Installer aborts with `root disk '/dev/vda' too small (0 GB < 2 GB)` | The domain got `<driver type='raw'>` for the qcow2 boot disk, so the guest saw the 197 KiB file as a raw disk (`virsh domblkinfo <dom> vda` shows a tiny capacity). virt-install's `format=` only governs volume *creation*; for a file that already exists it takes the type from the libvirt pool's volume record, which was stale after a destroy plus a manual `rm` of images. boxman now passes `driver.type=qcow2` explicitly (fix shipped with this box). If you removed images by hand, `virsh pool-refresh vms` before the next `boxman up`. |
| A node that was still *installing* is suddenly `shut off` | Do not `virsh reset`/`reboot` a node during the install: the transient install domain has `on_reboot=destroy`, which libvirt also applies to a reset, and the persistent definition has no install media. Re-provision the site (`boxman destroy -y`, `make sync`, `make up` order) instead. |
| `virsh net-start` fails on hpe1: bridge in use | `virbr-pve` already existed (a leftover from `vxlan-up.sh` run too early). `sudo ip link del virbr-pve`, then `boxman up`, then `vxlan-up.sh`. |
| Nodes on hpe2 lose connectivity after a host reboot / `boxman destroy` on hpe1 | The VXLAN is not persistent, and hpe1's bridge is recreated by libvirt. `make boot` (or `vxlan-up.sh` on both hosts) restores it. |
| ssh works but `pvecm`/`pveceph` fail with a dpkg lock | The first-boot hook is still running apt. Use `make wait-first-boot`. |
| `pvecm add` prompts or refuses | `--use_ssh` needs root ssh between nodes: `pve-cluster.sh` pushes the lab key and known_hosts first; re-run it. |
| `ceph -s` stuck below HEALTH_OK | Fresh OSDs peer for a minute or two; the script waits up to 5 min. Clock skew between the *hosts* (hpe2 runs a few minutes behind hpe1) is corrected inside the nodes by chrony via NAT, but check `ceph time-sync-status` if mons complain. |
| Big transfers hang while ping works | MTU: something is at 1500 on a 1450 path. `make mtu-check`; `ip -d link show vxlan-pve`, `ip link show vmbr0` on the node. |
| Nested VM has no address | It gets DHCP from hpe1 through the node's `vmbr0`; check the reservation `demo01`, `qm config 100` (`net0 … mtu=1`), and that the node's bridge carries the VM's MAC (`brctl showmacs vmbr0`). |

Do **not** run `boxman update` on these projects: the cdrom reconcile would
re-insert the install ISO. Harmless with the hd-first persistent boot order,
but a surprise.
