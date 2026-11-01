# talos-unattended

A seven-VM Talos Linux lab that installs without a console, a browser or an
interactive prompt: three control planes, three workers and one Ubuntu
control VM. Boxman creates the VMs; the Talos side is configured through the
Talos API from the control VM.

> **Status: work in progress.** This box currently provides the VM topology,
> the control-VM template recipe and machine-config generation
> (`scripts/gen-config.sh`). The install driver that applies the configs,
> bootstraps etcd once, fetches a kubeconfig and bootstraps Flux is not here
> yet. `boxman up` on its own only creates and starts the VMs.

## Topology

| VM | group | vCPU | RAM | boot disk | address |
| --- | --- | ---: | ---: | ---: | --- |
| control | management | 2 | 2 GiB | 40 GiB | DHCP (dynamic range) |
| cp-01..cp-03 | talos | 2 | 4 GiB | 50 GiB | `<prefix>.11`-`.13` (reserved) |
| worker-01..worker-03 | talos | 4 | 6 GiB | 150 GiB | `<prefix>.21`-`.23` (reserved) |
| Kubernetes API VIP | - | - | - | - | `<prefix>.5` |

Total: 20 vCPU, 32 GiB RAM, 640 GiB of (thin) boot disks, plus headroom for
building the template.

- **management** owns the `fabric` NAT network. Its DHCP reservations map
  every Talos node's pinned MAC to its address and hostname, so a node's role
  never depends on boot or discovery order.
- **talos** holds six ISO-boot VMs on `management::fabric`. Their empty
  disks fall through to the shared Talos metal ISO (`isos.talos-install`),
  and they wait in maintenance mode until a config is applied.
- **templates.control-base** is a non-secret Ubuntu recipe: the operator's
  public key, the guest agent and pinned `talosctl`, `kubectl` and `flux`,
  each checked against its sha256. One fail-fast script installs the tools
  and writes the readiness marker as its last step, so a failed download or
  checksum fails the template build instead of publishing it.

The Talos guests have no ssh by design: `boxman ssh` does **not** reach them,
and no ssh or Ansible task may target the `talos` group. Only the control VM
is reached over ssh (`boxman ssh management_control`), with the operator key
the template authorizes. The `management` group deliberately has no
`admin_pass`, so boxman injects no key and no password exists.

## Inputs

Everything site-specific is private and comes from the environment:

```sh
cp inputs.env.example inputs.env && chmod 600 inputs.env
$EDITOR inputs.env           # replace every PLACEHOLDER
set -a; . ./inputs.env; set +a
```

`inputs.env` is git-ignored. With nothing supplied, `conf.yml` still renders
(for the repository's checks), but the ISO is a marked placeholder that
cannot be downloaded, the template's tool install refuses its placeholder
pins, and `gen-config.sh` refuses any value still containing `PLACEHOLDER`.

Pin a tested set: the Talos release (ISO, installer image and `talosctl`
all at `TALOS_VERSION`), a Kubernetes version that release supports, and the
`kubectl` and `flux` binaries. Build the ISO and the installer image from the
same Image Factory schematic with the `qemu-guest-agent` extension. The
extension in the boot ISO alone does not survive installation.

## Generating machine configs

`scripts/nodes-from-conf.py` prints the node table from `conf.yml` (on the
host, where boxman is installed); `scripts/gen-config.sh` turns it into
configs on the control VM:

```sh
python3 scripts/nodes-from-conf.py > nodes.txt
scripts/gen-config.sh --inputs inputs.env --nodes nodes.txt --dry-run   # the plan only
scripts/gen-config.sh --inputs inputs.env --nodes nodes.txt
```

`gen-config.sh` validates every input and the node table (3+3, or the 1+1
smoke profile) before it does anything, then:

1. checks that `talosctl` is exactly `TALOS_VERSION`;
2. locks `TALOS_STATE_DIR` (0700), and refuses to use a directory inside a
   git work tree, so secrets cannot land in a checkout;
3. creates the secrets bundle **once** (`talosctl gen secrets`), and only in
   an empty state directory. It records the bundle's fingerprint and identity
   inputs, and every rerun reuses it. A missing, altered or mismatched
   bundle, or a changed cluster name, endpoint or version, stops the run.
   So does a missing bundle beside any earlier state (identity, talosconfig,
   configs or patches): it never mints a second cluster identity;
4. runs `talosctl gen config` with explicit Talos and Kubernetes versions,
   install disk, installer image, NTP servers and pod/service CIDRs;
5. patches each node with its hostname, its MAC-selected DHCP interface and,
   on control planes, the shared VIP. Optional patches from `TALOS_PATCH_DIR`
   (`all.yaml`, `<role>.yaml`, `<node>.yaml`) go first, and the generated
   identity patch goes last so it cannot be overridden. It then validates
   every config (`--mode metal --strict`);
6. writes a `talosconfig` whose endpoints are the control-plane addresses,
   never the VIP, once only. It publishes configs only after all of them
   validate and reports each node as `created`, `changed` or `unchanged`.

Only commands and file paths are logged; no secret is printed. Back up
`TALOS_STATE_DIR` off the control VM: losing the secrets bundle means losing
the ability to manage the cluster.

## Known limitations

- The patch forms (`machine.network.hostname`, `interfaces[].vip`) and
  determinism of `talosctl gen config` across reruns are checked against the
  pinned release in the smoke run, not yet here.
- A single hypervisor is a single failure domain. Three control-plane VMs do
  not make the lab highly available.
- Delivery is through the Talos maintenance API, which is unauthenticated
  before a node is configured: keep `fabric` dedicated to this lab.
