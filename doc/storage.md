# Disk Reclaim and Storage

Boxman ships a `storage` subcommand for inspecting and reclaiming qcow2
disk space, plus optional zstd compression of snapshot memory dumps.
This page is the reference; the README's "Disk Reclaim and Storage"
section has the quick-start.

## At a glance

| Verb | Purpose | VM state | Mutating |
|---|---|---|---|
| `boxman storage df` | per-VM table: virtual size, allocated, chain depth, snapshots, snapshot memory, reclaim estimate | any | no |
| `boxman storage trim` | guest-side `fstrim` via the qemu guest agent (`virsh domfstrim`) | running | yes (in-guest) |
| `boxman storage compact` | host-side qcow2 reclaim — `virt-sparsify --in-place` (preserves snapshot chain) or `qemu-img convert [-c]` (flattens it) | off — auto-shutdown by default | yes |
| `boxman storage optimize` | orchestrator: `trim` → `compact` → restart | manages itself | yes |
| `boxman storage compress-snapshots` | zstd-compress (or `--decompress`) snapshot memory `.raw` files retroactively | any | yes |

`boxman snapshot take --compress-memory` is the *primary* surface for
memory compression — it runs at snapshot creation time so no `.raw` is
ever written to disk uncompressed. `storage compress-snapshots` is the
retroactive form for snapshots that were taken without it.

---

## Why disks bloat

Three independent causes typically conspire:

1. **No `discard='unmap'` on the disk driver.** Guest deletes can't
   propagate to the host, so the qcow2 grows monotonically with every
   write — even when the guest filesystem reports plenty of free space.
2. **Allocated-but-zero qcow2 clusters never get sparsified.** Every
   write to a virgin block allocates a cluster; subsequent rewrites or
   guest-side deletes leave the cluster allocated.
3. **Snapshot memory dumps are full RAM size, uncompressed.** A snapshot
   with `--memspec` writes the live RAM (multi-GB) to a `.raw` file next
   to the qcow2 overlays. These add up fast across many snapshots.

`storage df` separates these out so you can see where the bloat is.
`storage optimize` then addresses the first two together; the snapshot
memory case is opt-in via `--compress-memory` / `compress-snapshots`.

---

## `boxman storage df`

Read-only. One row per qcow2 file, plus aggregated snapshot memory
totals per VM:

```bash
$ boxman storage df
VM                                              DISK                           VIRTUAL    ALLOC CHAIN  SNAPS   SNAPMEM RECLAIM~
--------------------------------------------------------------------------------------------------------------------------------
bprj__demo__bprj_cluster_1_node01               bprj__…_node01.qcow2             20.0G     8.4G    2      1     2.1G    3.2G
bprj__demo__bprj_cluster_1_node01               bprj__…_node01_data.qcow2        50.0G     1.1G    1      0    0.0B   200.0M
```

Column meanings:

| Column | Source | Notes |
|---|---|---|
| `VIRTUAL` | `qemu-img info --output=json` (`virtual-size`) | The guest-visible disk size |
| `ALLOC` | `qemu-img info` (`actual-size`) | Bytes the qcow2 is consuming on the host |
| `CHAIN` | `qemu-img info --backing-chain` | Length of the backing-file chain (1 = no overlays) |
| `SNAPS` | `virsh snapshot-list --name` | Number of libvirt snapshots for the VM |
| `SNAPMEM` | sum of `<vm>_snapshot_*.raw` and `*.raw.zst` sizes | Total memory-dump on-disk per VM (shown on the boot disk row) |
| `RECLAIM~` | `qemu-img measure --output=json` (`required`) vs `actual-size` | Estimated bytes recoverable by `compact` |

`storage df` never writes anything. It is the right first step before
running any other verb.

---

## `boxman storage trim`

Runs `fstrim -av` *inside* every running guest via `virsh domfstrim`.
Requires:

- `qemu-guest-agent` installed and running in the guest (a virtio-serial
  channel `org.qemu.guest_agent.0` is wired up by default in templates
  created via `create-templates` / `cloudinit` / `template_manager`).
- `discard='unmap'` set on the disk driver in the domain XML.

If the second condition is missing, `trim` prints a warning rather than
silently no-op'ing:

```
vm bprj__…_node01: no discard='unmap' on disks — fstrim will not reclaim
host space. fix: edit the domain XML (`virsh edit bprj__…_node01`) or
recreate via `boxman destroy && boxman up`.
```

### `discard='unmap'` is the default for new VMs

As of this release, all four disk-creation paths (`disk.py`,
`bare_vm.py`, `cloudinit.py`, `template_manager.py`) set
`discard='unmap'` on the driver element by default. New VMs are
trim-ready out of the box.

**Existing VMs** need to be redefined to pick this up. The least
disruptive route is `virsh edit <vm>` followed by a reboot — auto-XML
rewrite is intentionally out of scope.

### Flags

| Flag | Purpose |
|---|---|
| `--vms` | Comma-separated list (default `all`) — present on every storage verb |
| `--dry-run` | Print which VMs would be trimmed; do not call `domfstrim` |

---

## `boxman storage compact`

Host-side qcow2 reclaim. Two methods:

| `--method` | Tool | Effect on snapshot chain | Effect on size |
|---|---|---|---|
| `auto` (default) | sparsify if any snapshot exists, else convert | preserves chain when sparsifying | moderate |
| `sparsify` | `virt-sparsify --in-place` | preserves | moderate |
| `convert` | `qemu-img convert -O qcow2` | **flattens — drops snapshots** | aggressive |
| `convert-compressed` | `qemu-img convert -O qcow2 -c` | **flattens — drops snapshots** | most aggressive |

### Auto-shutdown by default

`compact` requires the VM to be off. By default it runs `virsh shutdown`,
waits up to 120 s, falls back to `virsh destroy`, runs the compaction,
and starts the VM again. To skip running VMs instead of shutting them
down, pass `--no-shutdown`.

### Snapshot-flattening guard

`convert` and `convert-compressed` rewrite the qcow2 from scratch —
the backing-chain is collapsed and **all libvirt snapshots are dropped**.
Boxman refuses these methods when snapshots exist unless you pass
`--drop-snapshots` to opt in. `auto` quietly steers around this by
choosing `sparsify` when snapshots are present.

### Flags

| Flag | Default | Purpose |
|---|---|---|
| `--vms` | `all` | VM scope |
| `--method` | `auto` | One of `auto / sparsify / convert / convert-compressed` |
| `--no-shutdown` | off | Skip running VMs instead of shutting them down |
| `--drop-snapshots` | off | Required for chain-flattening methods when snapshots exist |
| `--dry-run` | off | Print before/after estimates from `qemu-img info` + `qemu-img measure`; do not write |

### Example workflows

```bash
# Safe default — preserves snapshots, auto-shuts down
boxman storage compact

# Maximum reclaim, no snapshots needed
boxman storage compact --method convert-compressed --drop-snapshots

# Look only — see what would happen
boxman storage compact --dry-run

# Don't touch running VMs
boxman storage compact --no-shutdown
```

---

## `boxman storage optimize`

The orchestrator. Equivalent to:

```
boxman storage trim
boxman storage compact
```

with the right state transitions in between. Most users only need this
verb.

### Flags

| Flag | Purpose |
|---|---|
| `--vms` | VM scope |
| `--method` | Same as `compact` |
| `--skip-trim` | Skip the guest-side fstrim phase |
| `--skip-compact` | Skip the host-side compaction phase |
| `--no-shutdown` | Don't auto-shutdown; running VMs are skipped during compact |
| `--drop-snapshots` | Same as `compact` |
| `--dry-run` | Same as `compact` |

---

## Snapshot memory compression (zstd)

A live snapshot taken with `virsh snapshot-create-as --memspec=…` writes
the VM's full RAM to a `<vm>_snapshot_<name>.raw` file. These dominate
the disk footprint of a project with many snapshots — RAM doesn't
compact like a qcow2.

zstd typically reduces these by **~71%** at sub-second per GB
(level 3, multi-threaded `-T0`).

### Compress at creation — preferred

```bash
boxman snapshot take --name pre-upgrade --compress-memory
boxman snapshot take --name pre-upgrade --compress-memory --memory-compress-level 10
```

The `.raw` is compressed to `.raw.zst` immediately after the snapshot
succeeds. No uncompressed copy ever lands on disk persistently.

`--memory-compress-level` is the zstd level. `3` (default) is the
sweet spot; `10` adds ~5× wall time for ~5% more reduction; `19`
adds *much* more time for marginal extra reduction.

### Compress retroactively

For snapshots already taken without `--compress-memory`:

```bash
# Compress every snapshot's memory file across every VM
boxman storage compress-snapshots

# Just one VM
boxman storage compress-snapshots --vms node01

# Higher compression ratio
boxman storage compress-snapshots --level 10

# Reverse: decompress .raw.zst back to .raw
boxman storage compress-snapshots --decompress
```

### Restore is transparent

`boxman snapshot restore` (and the lower-level `SnapshotManager.snapshot_restore`)
reads the snapshot's recorded memory file path from libvirt's snapshot
XML. If the `.raw` is missing but a `.raw.zst` sibling exists, it is
decompressed to `.raw` before `virsh snapshot-revert` runs, then the
temporary `.raw` is deleted on success — the next revert remains
compressed. No flag, no opt-in.

This means `--compress-memory` is *safe to leave on by default* once the
runtime has `zstd` available.

---

## Soft requirements

| Tool | Used by | Soft-require behaviour |
|---|---|---|
| `virt-sparsify` (from `guestfs-tools` / `libguestfs-tools`) | `compact --method sparsify` and `auto` when snapshots exist | Pre-flight check; clear error before any destructive op: `virt-sparsify not found in the runtime — install guestfs-tools / libguestfs-tools, or pass --method convert` |
| `zstd` | Memory compression / decompression | Same — error if missing on first compress; restore-side decompress also surfaces a clear error |
| `qemu-guest-agent` | `trim` (`virsh domfstrim`) | If unresponsive, `trim` errors with `qemu-guest-agent not responsive. install it in the guest (apt/dnf install qemu-guest-agent) and reboot.` |

The bundled docker runtime image (`containers/docker/Dockerfile`) ships
both `guestfs-tools` and `zstd` — no extra install needed if you use
`--runtime=docker`. Running on the local runtime requires installing
them on the host.

---

## End-to-end recipes

### Reclaim a bloated cluster, keep snapshots

```bash
boxman storage df                              # baseline
boxman storage compress-snapshots              # shrink RAM dumps first
boxman storage optimize                        # trim + sparsify + restart
boxman storage df                              # confirm
```

### Maximum reclaim, throw away snapshots

```bash
boxman storage df
boxman storage optimize --method convert-compressed --drop-snapshots
boxman storage df
boxman snapshot take --name vanilla --compress-memory
```

### Inspect without writing anything

```bash
boxman storage df
boxman storage compact --dry-run
boxman storage optimize --dry-run
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `trim` warns "no discard='unmap' on disks" | Pre-existing VM created before `discard='unmap'` became default | `virsh edit <vm>` to add `discard='unmap'` to the `<driver/>` element on each `<disk device='disk'>`, then reboot. Or `boxman destroy && boxman up` to regenerate. |
| `fstrim failed: … qemu-guest-agent not responsive` | The guest agent isn't installed or the agent channel is missing | In the guest: `sudo apt install qemu-guest-agent && sudo systemctl enable --now qemu-guest-agent` (or `dnf` equivalent). Reboot if the agent channel was added by `boxman update`. |
| `compact` refuses with "snapshots present and --drop-snapshots not given" | Method `convert`/`convert-compressed` would flatten the chain | Either pass `--drop-snapshots`, or use `--method sparsify` (or default `auto`) which preserves the chain |
| `compact` refuses with "vm … is running and --no-shutdown was passed" | You opted out of auto-shutdown but the VM is up | Drop `--no-shutdown`, or shut the VM down manually first |
| `virt-sparsify not found in the runtime` | `guestfs-tools` not installed in the active runtime | `dnf install guestfs-tools` (RHEL/Rocky) or `apt install libguestfs-tools` (Debian/Ubuntu); or use `--method convert`; or switch to the docker runtime which already includes it |
| `zstd not found in runtime` (during compress / decompress) | `zstd` missing on the host or in the runtime | Install zstd; the docker runtime image already includes it |
| `df` shows huge `SNAPMEM` | Snapshot memory dumps not compressed | `boxman storage compress-snapshots` |
| `df` shows huge `ALLOC` and `CHAIN > 1` | Sparse holes in overlays | `boxman storage compact` (auto picks sparsify) |
| `df` shows huge `ALLOC` and `CHAIN = 1` and no snapshots | Allocated-but-zero clusters in the head qcow2 | `boxman storage compact --method convert-compressed` for the most aggressive shrink |
