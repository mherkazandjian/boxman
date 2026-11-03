# talos-unattended

A seven-VM Talos Linux lab that installs without a console, a browser or an
interactive prompt: three control planes, three workers and one Ubuntu
control VM. Boxman creates the VMs; the Talos side is configured through the
Talos API from the control VM.

> **Status: work in progress.** `make install` (scripts/install.sh) takes
> the lab from nothing to a healthy cluster and can be rerun safely; Flux
> is opt-in. `boxman up` on its own only creates and starts the VMs: the
> driver runs it, then configures the cluster from the control VM.

## Topology

| VM | group | vCPU | RAM | boot disk | address |
| --- | --- | ---: | ---: | ---: | --- |
| control | management | 2 | 2 GiB | 40 GiB | DHCP (dynamic range) |
| cp-01..cp-03 | talos | 2 | 4 GiB | 50 GiB | `<prefix>.11`-`.13` (reserved) |
| worker-01..worker-03 | talos | 4 | 6 GiB | 150 GiB | `<prefix>.21`-`.23` (reserved) |
| Kubernetes API VIP | - | - | - | - | `<prefix>.5` |

Total: 20 vCPU, 32 GiB RAM, 640 GiB of (thin) boot disks, plus headroom for
building the template.

`TALOS_PROFILE=smoke` renders the 1+1 smoke profile instead: `control`,
`cp-01` and `worker-01`, sized as above (8 vCPU, 12 GiB RAM, 240 GiB). Pick
the profile before `boxman up`; the default, `full`, is the table above.

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
(for the repository's checks), and `gen-config.sh` refuses any value still
containing `PLACEHOLDER`.

## Version set

`versions.env` pins the tested set; its comments record where each value
and hash came from. `conf.yml` and `gen-config.sh` both read it, so nothing
version-related goes in `inputs.env`.

| pin | value | source |
| --- | --- | --- |
| Talos | v1.14.5 | latest stable release |
| `talosctl` | sha256 `ff432b68…` | the release's `sha256sum.txt` |
| Image Factory schematic | `ce4c9805…` | `siderolabs/qemu-guest-agent` only |
| boot ISO | `metal-amd64.iso`, sha256 `36723c65…` | hashed at download (the factory publishes none) |
| installer image | `factory.talos.dev/metal-installer/<schematic>:v1.14.5@sha256:8a510984…` | the factory registry's digest for the tag |
| Kubernetes | 1.37.1 | Talos v1.14.5's default; 1.14 supports 1.33-1.37 |
| `kubectl` | v1.37.1, sha256 `65691ff7…` | `dl.k8s.io` `.sha256` |
| `flux` | v2.10.0, sha256 `208dd701…` | the release's checksums file; supports Kubernetes 1.35-1.37 |

The ISO and the installer image come from the same schematic: the guest
agent extension in the boot ISO alone does not survive installation.

The file is plain `KEY=value` lines (no quotes or expansion). An
environment variable of the same name overrides a pin, and `gen-config.sh`
logs each override. To move to a new set, change every related pin together
(the ISO and installer image embed the schematic id and Talos version, and
`tests/test_talos_unattended_box.py` checks that they agree), record the
sources in the comments, then repeat the real-`talosctl` checks described in
[Testing](#testing).

## Installing

```sh
cp inputs.env.example inputs.env && chmod 600 inputs.env   # fill it in
make preflight      # checks only; changes nothing
make install        # boxman up, then configure, bootstrap and check
make status         # read-only report
```

`make install` runs `scripts/install.sh`, a serial driver on the host. It
stops at the first failed stage with a message naming it; fix the cause and
run it again. A rerun checks what the earlier run built and changes nothing
that is already right: it never regenerates the cluster identity, re-sends
a node's initial config or bootstraps etcd a second time.

| stage | what it does, and what it refuses |
| --- | --- |
| preflight | `scripts/preflight.sh`, before any side effect: inputs and pins, libvirt with the local runtime, exactly one control VM and 3+3 nodes (or 1+1 with `TALOS_PROFILE=smoke`), memory and disk for what is not yet running or created, the fabric subnet against every other libvirt network, host address and route, the operator key, the backup location, and Flux's inputs when it is on. Every reading is checked; a failed or partial one stops the run. A first run refuses to adopt existing domains or networks of the project; a later one finds exactly the domains it recorded |
| up | one project-wide `boxman up` (`BOXMAN` overrides the command); each VM's UUID is recorded, and a later run stops if a domain was recreated |
| control | `scripts/control-exec.sh verify`: the control domain's UUID, its one lease on the fabric, and its ssh host key read from inside the guest through the libvirt guest agent. ssh then uses only the operator key, `BatchMode` and that pinned host key, nothing from `~/.ssh/config`. The control VM's `talosctl`, `kubectl` and `flux` must be the pinned versions |
| nodes | per Talos node, from libvirt: one NIC on this project's fabric with the reserved MAC, a boot disk in the workspace of the declared size, and a DHCP lease for the MAC that is the reserved address |
| ship | the control-side scripts, `versions.env`, the node and disk tables and the control-side inputs (never a credential) to `~/talos-unattended` on the control VM |
| restore, generate | a control VM that lost its state gets it back from the backup; then `gen-config.sh` (identity created once) |
| apply | `scripts/apply-config.sh` on the control VM: a fresh node must answer the maintenance API and not the authenticated one; exactly one link's `permanent_addr` must be its reserved MAC, and the install disk must be the empty disk of the declared size. The config goes out once, under a journal, and the node must come back on the authenticated API. A recorded node is only ever reached authenticated: no answer is an error, never a reason to retry insecurely, and a changed config is drift (v0 does not update installed nodes) |
| bootstrap | `scripts/bootstrap.sh`: etcd is bootstrapped once, on the first control plane's own address (never the VIP), under the state lock and a journal. An unconfirmed earlier request is inspected, never repeated; an existing etcd is never bootstrapped. Then: one etcd member per control plane, a kubeconfig, every node Ready, CoreDNS and the CNI rolled out, and a test Deployment (`talos-smoke/smoke`) Running |
| flux | `scripts/flux.sh`, only with `TALOS_FLUX=on` (below) |
| status | `scripts/status.sh` on the control VM |

After every stage that changes it, the control VM's state is copied to
`TALOS_BACKUP_DIR/state` (0700): the secrets bundle, talosconfig, configs,
the journal and the kubeconfig. Its fingerprint must never change. The
backup's physical path must lie outside the workspace and outside any git
checkout. The driver keeps its own record, the pinned host key and a copy
of the kubeconfig (0600) in `<workspace>/driver`.

Exit status: 0 converged, 1 a stage failed, 3 drift that needs a person
(a recreated VM, a changed config on an installed node, an unconfirmed etcd
bootstrap with no etcd, a changed identity).

Waits are bounded, in seconds: `TALOS_CONTROL_TIMEOUT` (900, control VM
verified), `TALOS_LEASE_TIMEOUT` (600, node leases), `TALOS_MAINTENANCE_TIMEOUT`
(600), `TALOS_INSTALL_TIMEOUT` (1200), `TALOS_AUTH_TIMEOUT` (300),
`TALOS_ETCD_TIMEOUT` (600), `TALOS_KUBE_TIMEOUT` (900), `TALOS_FLUX_TIMEOUT`
(600). `TALOS_SMOKE_IMAGE` (default `registry.k8s.io/pause:3.10`) and
`TALOS_CNI_DAEMONSET` (default `kube-flannel`) name the test workload's image
and the CNI to wait for.

### Flux (opt-in)

Flux is off unless `TALOS_FLUX=on`. Then preflight requires `GITLAB_HOST`,
`GITLAB_OWNER`, `GITOPS_REPOSITORY`, `GITOPS_BRANCH`, `GITOPS_CLUSTER_PATH`
(relative, inside the repository) and `GITLAB_TOKEN`, before anything is
created. The project must already exist: `flux.sh` checks it through the
GitLab API and stops otherwise, because `flux bootstrap` would create a
missing one. It bootstraps with `--deploy-token-auth`, so Flux keeps only a
read-only deploy token. The token travels to the control VM on stdin and
reaches `flux` and `curl` through the environment and stdin, never argv, a
file or the log. A rerun leaves an existing bootstrap alone if it points at
the same repository, branch and path, and stops as drift if not. Either way
the root GitRepository and Kustomization must be Ready.

## Generating machine configs

`scripts/nodes-from-conf.py` prints the node table from `conf.yml` (on the
host, where boxman is installed); `scripts/gen-config.sh` turns it into
configs on the control VM:

```sh
python3 scripts/nodes-from-conf.py > nodes.txt
scripts/gen-config.sh --inputs inputs.env --nodes nodes.txt --dry-run   # the plan only
scripts/gen-config.sh --inputs inputs.env --nodes nodes.txt
```

The pins come from `versions.env` beside `scripts/` unless `--versions FILE`
names another file. Render the node table with the same `TALOS_PROFILE` as
the VMs. For the smoke profile:

```sh
TALOS_PROFILE=smoke python3 scripts/nodes-from-conf.py > nodes.txt   # cp-01, worker-01
scripts/gen-config.sh --inputs inputs.env --nodes nodes.txt --dry-run
```

`gen-config.sh` validates every input and the node table (3+3, or the 1+1
smoke profile) before it does anything. That includes CIDRs Talos would
reject later (host bits set) and ones that overlap each other, the VIP or a
node address. Then it:

1. checks that `talosctl` is exactly `TALOS_VERSION`, and that `python3`
   with PyYAML (part of the control template) is there to check configs;
2. refuses a `TALOS_STATE_DIR` that is, or might be, inside a git checkout,
   before creating anything. The path may not contain `.` or `..`, and its
   nearest existing directory is resolved through any symlinks first. Every
   parent of that physical path must be searchable and must not hold a
   checkout's `.git` (a worktree or submodule's `.git` file, or a `.git`
   directory with a `HEAD`). When git is installed, it must also positively
   answer "not a git repository". Anything uncertain (an unresolvable path,
   an unreadable `.git` or parent directory, or a git error such as dubious
   ownership) is a refusal. Once created, the directory must still resolve
   to the checked physical path, the only one used from then on. It is then
   locked (0700);
3. creates the secrets bundle **once** (`talosctl gen secrets`), and only in
   an empty state directory. It records the bundle's fingerprint and identity
   inputs, and every rerun reuses it. A missing, altered or mismatched
   bundle, or a changed cluster name, endpoint or version, stops the run.
   So does a missing bundle beside any earlier state (identity, talosconfig,
   configs or patches): it never mints a second cluster identity;
4. runs `talosctl gen config` with explicit Talos and Kubernetes versions,
   install disk and installer image. A cluster patch sets the NTP servers
   (a `TimeSyncConfig` document) and the pod/service CIDRs (the
   `KubeNetworkConfig` document);
5. patches each node: first the optional patches from `TALOS_PATCH_DIR`
   (`all.yaml`, `<role>.yaml`, `<node>.yaml`), then the generated node patch.
   That patch holds four Talos v1.12+ documents: a `HostnameConfig` with the
   node's static name, a `LinkAliasConfig` that names the NIC with the
   node's reserved MAC `fabric`, a `DHCPv4Config` on `fabric` and, on control
   planes only, a `Layer2VIPConfig` for the shared VIP on `fabric`.
   `scripts/check-node-config.py` then compares the merged config with the
   generated one, document by document. A patch may change other documents,
   but it may not add or delete one. It also may not change:
   - cluster identity and trust: the `DiscoveryIdentityConfig`,
     `KubeClusterConfig`, Kubernetes and aggregator CA, service account,
     etcd encryption, authentication and authorizer documents, and the
     main document's type, tokens and CAs;
   - install inputs: the `UnattendedInstallConfig` (installer image and
     disk);
   - networking: the `KubeNetworkConfig` (the CIDRs) and the node patch's
     documents;
   - the legacy v1alpha1 fields (`machine.network`, `machine.install`,
     `cluster.network`, ...) that would duplicate these documents.

   The checker also confirms the node's name, MAC, DHCP and VIP-by-role
   independently of the patch. Every config is then validated
   (`--mode metal --strict`). A rejected or invalid config stops the run
   with nothing published, and the error names only the setting, never its
   value;
6. writes a `talosconfig` whose endpoints are the control-plane addresses,
   never the VIP, once only. It publishes configs only after all of them
   pass and reports each node as `created`, `changed` or `unchanged`.

`patches.example/` shows one patch at each level: `all.yaml` bounds the
boot-time wait for NTP, `controlplane.yaml` adds an API certificate name,
`worker.yaml` labels the workers and `worker-01.yaml` adds a zone label to
one node. Copy the directory, adapt it and point `TALOS_PATCH_DIR` at the
copy. A patch for one role must only touch documents that role's config
has (the API server's exists only on control planes), or the check rejects
it as an added document.

Only commands and file paths are logged; no secret is printed. Back up
`TALOS_STATE_DIR` off the control VM: losing the secrets bundle means losing
the ability to manage the cluster.

## Testing

`pytest tests/test_talos_unattended_box.py` needs no VMs and no network: it
renders `conf.yml` and runs the scripts against a fake `talosctl` on `PATH`.
The fake writes the v1.14 document layout and merges the way `talosctl`
does: lists append, maps merge, and `$patch: delete` removes a document.

`pytest tests/test_talos_unattended_driver.py` runs the install driver
and every stage script unchanged against fake `virsh`, `ip`, `df`, `boxman`,
`ssh`, `talosctl`, `kubectl`, `flux` and `curl` (tests/talos_unattended_fakes.py)
sharing one simulated lab: a fresh install, a converged rerun, and a failure
and a resume for every stage.

Point `TALOS_UNATTENDED_TALOSCTL` at a `talosctl` of the pinned release to
also run the same checks against the real binary. It needs no cluster and no
network. With it, the tests check that:
- every config passes `validate --strict`;
- the patch forms land as expected;
- every protected change is caught after Talos' own merge;
- a rerun is byte-identical;
- the fake writes exactly the real document layout.

```sh
curl -fsSLo talosctl https://github.com/siderolabs/talos/releases/download/v1.14.5/talosctl-linux-amd64
echo "<TALOSCTL_SHA256 from versions.env>  talosctl" | sha256sum -c - && chmod +x talosctl
TALOS_UNATTENDED_TALOSCTL=$PWD/talosctl pytest tests/test_talos_unattended_box.py
```

## Known limitations

- Whether `link.permanent_addr` carries the libvirt MAC on the virtio NIC
  is checked by `apply-config.sh` on every fresh node before its config is
  sent: a node where it does not stops the run.
- v0 does not update an installed node, replace one, or change the profile
  of a built lab: those stop as drift.
- The Talos ISO stays attached after installation. The firmware boots the
  disk first (`hd,cdrom`), so an installed node does not boot the ISO again.
- Patches may change existing documents only. Adding a config document (for
  example an extension service or a trusted root) or deleting one is
  rejected for now.
- A single hypervisor is a single failure domain. Three control-plane VMs do
  not make the lab highly available.
- Delivery is through the Talos maintenance API, which is unauthenticated
  before a node is configured: keep `fabric` dedicated to this lab.
