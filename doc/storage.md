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
| `boxman storage pools` | list libvirt's storage pools; `--prune` removes the ones left on directories that are gone | any | only `--prune` |
| `boxman snapshot collapse --to <name>` | merge the snapshots between `<name>` and the live head into the head; every other snapshot remains revertable, and it refuses while another branch is built on `<name>` | off — auto-shutdown by default | yes |
| `boxman snapshot prune --dry-run` | list the files snapshots left behind that no snapshot records and no domain uses, each removable, kept (with the reason) or unknown | any | no (this version only lists) |

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
| `SNAPMEM` | sum of `<vm>_snapshot_*.raw` and `*.raw.zst` sizes in the workdir, and of every memory image its snapshots recorded (in a `memory_snapshot_dir`, say), each file once | Total memory-dump on-disk per VM (shown on the boot disk row) |
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

## `boxman storage pools`

libvirt keeps a storage pool on each directory a VM's disks are created
in: virt-install and virt-clone define one when there is none. boxman
sees to the one on each cluster workdir itself, before cloning (see
[the pool on a cluster's workdir](#the-pool-on-a-clusters-workdir)).
`destroy` retires the pools on the directories it removes. A pool left on a
directory that is gone some other way (by an older boxman, by a
`destroy` that could not read which domains use it, or by a directory
removed by hand) shows up here.

```bash
boxman storage pools                         # every pool: state, autostart, target, status, note
boxman storage pools --stale                 # only directory pools whose target is gone
boxman storage pools --prune --dry-run       # what --prune would remove
boxman storage pools --prune                 # remove them, after a prompt that lists them
boxman storage pools --prune cluster_1-3 -y  # remove only the pools named
```

`STATUS` is one of:

- `there`;
- `gone`: positively absent;
- `unknown`: a directory on the way cannot be searched. Such a pool is
  never pruned.

The note says whether the pool is a workdir of this project, has a
`bprj__` name, or is named by a domain's volume disk.

`--prune` acts on a directory pool only if its target is gone and no
domain names it. It stops the pool with `pool-destroy` and forgets it
with `pool-undefine`. It never runs `pool-delete`, and nothing on disk is
touched. The command exits non-zero in two cases:

- a pool named on the command line does not qualify: then nothing is
  pruned;
- a pool cannot be retired.

A target on an NFS export that is not mounted reads as gone, so mount it
first, or name only the pools to remove.

No conf.yml is needed: without one, the command uses boxman.yml's libvirt
provider on the local runtime. Under the docker runtime, run it beside
the project's conf.yml, because libvirt there is that project's
container.

### The pool on a cluster's workdir

Before it clones VMs into a cluster's workdir (`provision`, `up`,
`update`), boxman sees to a pool on it. Otherwise the clones, started
together, would race to define one. The pool is found by its target,
never by its name:

- A pool already on the workdir is used under its own name and started
  if it is down. It may be an older boxman's, named after the workdir's
  basename (`cluster_1`), or virt-install's (`cluster_1-3`). A pool counts
  as on the workdir when its target is spelled the same way once
  normalized, and is the same directory.
- A pool of a matching name on another directory, such as another
  project's `cluster_1`, is never taken for the workdir.
- With no pool on it, boxman defines
  `bprj__<project>__bprj_<cluster>_workdir`, starts it and sets it to
  start with the host.
  - If that name is taken by a pool on another directory (the workdir
    moved, say), boxman uses the first free `<name>_<n>` and leaves the
    other pool as it is.
  - If the project or cluster name holds a `/` or a control character,
    or the name would be longer than 200 bytes, the pool is named
    `bprj__<digest>_workdir`. The digest is the first 16 hex digits of
    the SHA-256 of `<project>/<cluster>`.
- Clusters that share a workdir share its pool. The first cluster to get
  there names it.
- A `bprj__` pool on the workdir that does not start with the host is set
  to, for example after its `pool-start` failed when it was defined. A
  pool with any other name keeps its setting.
- boxman makes the workdir itself, as the user it runs as. It does not
  run `pool-build`.
- If the pools cannot be listed, or the pool cannot be defined or
  started, boxman warns and the clones go on. virt-install and virt-clone
  then define a pool themselves, as they did before.

---

## `boxman snapshot collapse --to <name>`

The big-reclaim hammer when bloat lives in **older overlays** that
`storage compact` (which only operates on the head) can't touch — e.g.
long-lived snapshots taken while a daemon was writing continuously.

`qemu-img rebase` walks the chain and merges every cluster from the
snapshots between `<name>` and the live head, and from `<name>`'s own
overlay, into the live head, whose backing becomes the disk `<name>` was
taken on. `<name>` is then redefined to record the head as its overlay,
and its old overlay is removed: the layout libvirt's own delete of the
dropped snapshots leaves, which its delete of `<name>` afterwards
requires. Where intermediates wrote zero clusters (RAID5 sync, mkfs,
etc.), rebase writes sparse holes — so the reclaim is often dramatic
with little wall time.

What survives:

- **`<name>` and older snapshots remain revertable** — the disks they
  were taken on are untouched, and the rebased head has the disk
  `<name>` was taken on as its backing.
- **The live state of the VM** — semantically unchanged, just stored in
  a different file layout.

What is permanently lost:

- **Every snapshot between `<name>` and the current one** — their qcow2
  overlay files are unlinked, their libvirt metadata is cleared with
  `virsh snapshot-delete --metadata`, and then their `.raw` / `.raw.zst`
  memory dumps are unlinked — but for one another snapshot, of this VM or
  another, still records, which is kept and named in a warning.
- The ability to revert to a mid-chain state above `<name>`.

Snapshots form a tree, not a line: `take s1`, `take s2`, `restore s1`,
`take s3` leaves s2 and s3 both built on s1, with the VM running on s3
(`boxman snapshot log` draws it). The live disk is built on s3 alone, so
a collapse can merge s3 into it but not s2. `collapse --to s1` therefore
refuses, naming s2, and so does `collapse --to s2`, which the VM does not
run on: either would drop a snapshot without merging it into anything.
Delete the other branch first (`boxman snapshot delete --name s2`), or
collapse to a later snapshot of the VM's own line (`collapse --to s3`,
once s4 is taken after it). `--dry-run` says the same before anything
is touched, and the refusal leaves a running VM running.

### Usage

```bash
# Show what would be merged; no writes
boxman snapshot collapse --to before-slurm --dry-run

# Merge everything between before-slurm and the live head into the head
boxman snapshot collapse --to before-slurm

# Skip the destructive-action confirmation prompt
boxman snapshot collapse --to before-slurm --yes

# Refuse if any VM is running (default = auto-shutdown then restart)
boxman snapshot collapse --to before-slurm --no-shutdown
```

### Flags

| Flag | Default | Purpose |
|---|---|---|
| `--to <name>` | required | The snapshot to collapse to: it and every snapshot not between it and the live head stay revertable |
| `--vms` | `all` | VM scope |
| `--no-shutdown` | off | Refuse if running (default = auto-shutdown via `virsh shutdown`, fall back to `destroy` after 120 s) |
| `--dry-run` | off | List what would be merged per VM; no writes |
| `-y` / `--yes` | off | Skip the destructive-action confirmation prompt |

### Implementation notes

After `qemu-img rebase`, libvirt's domain XML still has cached
`<backingStore>` children that point at files we just deleted. Without
clearing them, `virsh start` errors out. `snapshot collapse` parses the
domain XML with `xml.etree.ElementTree`, removes every `<backingStore>`
under each `<disk device='disk'>`, writes the cleaned XML to a temp
file, and runs `virsh define` to refresh libvirt's view. Regex won't
work — `<backingStore>` elements nest.

### Relationship with `snapshot delete`

`boxman snapshot delete --name <X>` is a thin wrapper around `collapse`:

- If `<X>` is the only snapshot → online deletion via
  `virsh blockcommit --active --pivot --wait` (no shutdown, sub-second
  per disk).
- If `<X>` is the current snapshot, with nothing built on it, and has a
  parent → `collapse_to(parent_of_X)` — drops just `<X>` while keeping
  the parent and older revertable. Like any collapse it refuses when
  another branch is built on that parent.
- Otherwise (snapshots built on `<X>`, or `<X>` not the one the VM runs
  on) → refused, with a pointer to `snapshot collapse --to
  <parent_of_X>` where that collapse would drop `<X>`. Single-snapshot
  rebase in the middle of a chain is out of v1 scope; the user should
  drop the newer snapshots first or use `collapse` to take them all in
  one step.

This is only the path boxman takes when libvirt refuses the delete
itself, as every libvirt before 9.0 does for external snapshots.

---

## Where snapshot files go

Two optional cluster keys move a snapshot's files off the disks'
filesystem; unset, nothing changes:

```yaml
clusters:
  web:
    workdir: ~/boxman/ws/web
    memory_snapshot_dir: /mnt/bulk/snapshots/web   # memory images; default: the workdir
    disk_snapshot_dir: /mnt/fast/overlays/web      # disk overlays; default: beside each disk
```

- Each is `~` expanded and otherwise taken as written; a relative path is
  refused. A missing one is made on first use: `0700` for memory images
  (the guest's RAM), `0711` for overlays (QEMU opens them as another user).
  One directory for both, or one inside the other, is made `0711` wherever
  QEMU must pass to reach the overlays, an existing `memory_snapshot_dir`
  too, which a warning then names. Local runtime only.
- After a take the newest overlay is the disk the guest writes to, so a
  `disk_snapshot_dir` holds live disks, not backups. A take sees to a
  libvirt storage pool on it, which the in-use scan reads the overlays
  through under `use_sudo: false`.
- A `snapshot restore` does not put the guest back in the
  `disk_snapshot_dir`: libvirt makes the overlays it reverts onto beside
  each disk's source as it was when the snapshot was taken, named
  `<disk>.<epoch>` (in the workdir, after restoring the first snapshot).
  The next take puts the new overlays in the `disk_snapshot_dir` again.
- A teardown removes from a `disk_snapshot_dir` only what libvirt names as
  the VM's: its disks, the layers below them, and the overlays its
  snapshots recorded; a data disk's only as layers of its own chain, the
  one it is attached through or one only its snapshots record that is built
  on it. Never what is there under its name, since another
  VM's full name can begin with this one's and a dot, and never a file
  another domain's snapshot records. Run again after an interruption, it
  goes on only while the backing chains of the overlays only its snapshots
  recorded are as first read, less the layers it removed; one rebased or
  replaced since keeps everything from then on, and is named.
- `storage df` and `storage compact` work from each VM's base disks in the
  workdir, as before: the overlays above them, beside the disk or in a
  `disk_snapshot_dir`, are not measured or compacted.
- `snapshot restore`, `delete` and `collapse` follow the paths libvirt
  recorded, wherever they are; a restore writes its `<overlay>.preserve`
  copies beside each overlay. They cover what the revert deletes: the
  overlays of the target and of newer snapshots, and those the current
  snapshot records, wherever it is in the chain. After a `snapshot delete`
  of the current one its parent is current, and a newer snapshot is built
  on the parent's overlays (#287). Reverting to a snapshot with no children,
  libvirt deletes its overlays and records the new ones in their place: the
  old ones are put back all the same, since whether another domain needs
  one cannot be told while it is gone, and named in a warning. Nothing
  removes them until `destroy`; `boxman snapshot prune --dry-run` lists
  them, and says whether each one can go (below).

---

## `boxman snapshot prune --dry-run`

Snapshots leave files behind that no snapshot records and no domain uses,
and nothing removes them until `destroy`:

- the overlays a restore puts back after the revert recorded new ones in
  their place: a snapshot's own overlays, once it is restored with nothing
  built on it (#266), and the overlay a revert made for a snapshot that was
  restored with no take since, once another one is restored (#288);
- a deleted snapshot's own overlays, which an earlier restore put back:
  libvirt's delete of a leaf that is not current unlinks the overlay its
  parent's take made, not these (#290);
- what a collapse or a delete kept because it could not tell whether
  something still needs it, and named in a warning; what a collapse by a
  boxman before #298 left under the head (a collapse now merges a revert's
  overlay under the head into the head, and removes it);
- an interrupted restore's `<overlay>.preserve` backups, their
  `.preserve.copy` scratch copies and its reservation scaffolds;
- memory images no snapshot records any more.

`snapshot prune --dry-run` lists them, each `removable`, `kept` with the
reason, or `unknown`, and what removing the removable ones would reclaim.
This version only lists: without `--dry-run` it refuses, and removing what
it lists comes in a later version.

```console
$ boxman snapshot prune --vms nixos01 --dry-run --min-age 0
scanned 76 domains (qemu:///system), 3 directories: <ws>, <ws>/overlays, /mnt/mem/nixos

STATUS       SIZE  KIND                  PATH                                     REASON
removable  196.0K  preserve-backup       <ws>/overlays/<vm>.m1.preserve           a spare copy of <vm>.m1, which is in place
removable    4.0K  preserve-reservation  <ws>/overlays/<vm>.m2.preserve           an unfilled backup reservation
removable   12.0K  take-overlay          <ws>/overlays/<vm>_data.t2               no snapshot 't2' is left; nothing records or uses it
removable  246.5M  memory-image          /mnt/mem/nixos/<vm>_snapshot_m1.raw.zst  no snapshot 'm1' is left; nothing records it
removable  507.1M  memory-image          /mnt/mem/nixos/<vm>_snapshot_m2.raw      no snapshot 'm2' is left; nothing records it
kept        12.0K  preserve-backup       <ws>/overlays/<vm>.t2.preserve           the only copy of <ws>/overlays/<vm>.t2, which is missing: mv -fT <ws>/overlays/<vm>.t2.preserve <ws>/overlays/<vm>.t2
kept        12.0K  revert-overlay        <ws>/overlays/<vm>_data.1793993915       <ws>/userlayer.qcow2 is built on it
unknown     12.0K  preserve-backup       <ws>/overlays/<vm>_data.t2.preserve      it differs from <vm>_data.t2, which is in place: which one holds the overlay cannot be told
unknown      4.0M  memory-image          /mnt/mem/nixos/<vm>_snapshot_fake.raw    its save header could not be read

would reclaim 753.8M (5 files); kept 2 (24.0K); unknown 2 (4.0M); 13 recorded or in use not shown (--all)
note: a .preserve copy made with reflinks shares blocks with its overlay; removing it frees only those it does not share
dry run: nothing was changed
```

With the default `--min-age 1h` the same files read `too recent (changed
2m ago; --min-age 1h)`, but for the only copy, which says how to put it
back. SIZE is what a file takes on disk (`st_blocks`).

### Where it looks

Each selected cluster's `workdir`, its `disk_snapshot_dir` and its
`memory_snapshot_dir`, and the ones it set before: a directory a storage
pool boxman defined for its `disk_snapshot_dir`
(`bprj__<project>__bprj_<cluster>_disk_snapshots`, or that with `_<n>`) is
still on, or where one of its VMs' snapshots records an overlay or a memory
image under the name a take gives it. Each directory once, by identity.
Never the image cache, the directory of any domain's CD-ROM or other media
source, or the mount point one is under; a note names each one left out.
An old `memory_snapshot_dir` that no snapshot records anything in any more
is not found.

### What it lists

A file named for one of the selected VMs, its name matched literally: an
overlay `<vm>.<s>` or `<vm>_<disk>.<s>` that is a qcow2 image built on
another, and none of the disks the VM declares (`revert-overlay` when `<s>`
is a revert's epoch, else `take-overlay`); a memory image
`<vm>_snapshot_<s>.raw` or `.raw.zst`; a restore's `<X>.preserve`,
`<X>.preserve.copy` or reservation scaffold beside an overlay `X` of the
VM. A longer name of a domain on the connection, or of a VM of the project,
takes a file: `<vm>.x.s1` is domain `<vm>.x`'s, and is not listed. A
`<vm>.save`, a file that is no image, or a qcow2 built on nothing is never
listed.

### When a file is removable

When every one of these holds, each by path and by identity
(`st_dev`, `st_ino`):

- **What it holds is its VM's.** An overlay is built right on an image
  named for its VM, or on a layer of one of its disks, and on no domain's
  media source; one whose backing file is gone counts by that name. A
  memory image's save header names the VM: a `.raw` is read by libvirt
  (`virsh save-image-dumpxml`), a `.raw.zst` through `zstd -dc | head`.
  One that names another domain is kept; one whose header names the VM
  with another domain's UUID is `unknown`. A VM whose domain is gone has
  only `unknown` files: they are `destroy`'s.
- **No domain uses it**: a disk or CD-ROM of either definition of any
  domain, a layer of their chains, a block job's destination, the
  definition a managed save holds, an active backup job's files.
- **No snapshot of any domain names it**: in `<disks>`, `<revertDisks>`,
  its saved `<domain>` or `<inactiveDomain>`, as its memory image (with the
  `.raw.zst` beside it), or as a layer below any of those.
- **No file in the scanned directories that stays is built on it.** One
  header level of every image there is read, and a file that stays keeps
  everything below it.
- **It is a regular file with one name**, no mount point, in a directory
  you may write to.
- **It has not changed for `--min-age`** (default `1h`; `0` is allowed). A
  rename counts, so an overlay a restore put back counts from that
  restore, and a restore that is running is left alone.
- **A `.preserve` is provably spare**: an unfilled reservation, or a copy
  of an overlay that is in place with the same size and modification
  time. One whose overlay is missing is the only copy: kept, with the
  `mv -fT` that puts it back. One that differs from its overlay is
  `unknown`.
- **No process holds its image lock**: an advisory `qemu-img info`
  without `-U`, asked of what would otherwise be removable.

A scan that fails — the domains, their snapshots, a backing chain, a header
in a scanned directory, a directory that cannot be listed — makes every
file `unknown`. The table is printed all the same, and the command exits 2
with `nothing was pruned: …`, under `--dry-run` too. A
`.boxman-removing-*` directory a removal left behind is named in a note,
never listed.

It reads only the libvirt connection in the provider config. A file
outside the scanned directories that is built on a listed one and that
nothing on the connection names is not seen.

### Flags

| Flag | Meaning |
|---|---|
| `--cluster C`, `--vms a,b` | Whose files to list, as `snapshot take` selects VMs. Files of the project's other VMs in the same directories are never listed. |
| `--kind overlay,memory,preserve` | Which kinds to list; default all three. A file of a kind left out stays, and so does what it is built on. |
| `--min-age AGE` | Seconds, or `30m`, `1h`, `2d`; default `1h`. |
| `--all` | Also list the files a snapshot records or a domain uses, as `kept`; by default only their count is shown. |
| `--dry-run` | List, change nothing. Required in this version. |

Under the docker runtime only the cluster workdirs are scanned (the
snapshot directories are local-runtime only); libvirt and the images are
read in the project's container. A workdir shared with another project's
container cannot be told. A docker-compose cluster is skipped with a note.

---

## Snapshot memory compression (zstd)

A live snapshot taken with `virsh snapshot-create-as --memspec=…` writes
the VM's full RAM to a `<vm>_snapshot_<name>.raw` file — in the cluster's
`memory_snapshot_dir` when it sets one, else in its `workdir`. These dominate
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

### Deleting a compressed snapshot

`boxman snapshot delete`, `snapshot take --force` (which deletes the
snapshot it replaces) and a collapse that drops a snapshot all remove its
memory dump in both forms, `.raw` and `.raw.zst`, from wherever the snapshot
recorded it — a `memory_snapshot_dir` included. libvirt's own delete
unlinks only the `.raw` its record names, so the `.raw.zst` is boxman's to
remove. A dump another snapshot still records — of this VM or another, whose
image shares the name in a directory two clusters share — is kept, and so is
one whose records cannot be read; a warning names it. A delete that fails
removes nothing.

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

## The guest's clock after a memory restore

A snapshot that saved the guest's memory stops its clock with everything
else, and a revert carries on from that instant: the guest comes back behind
the host by the snapshot's age. So does a guest `boxman up` restores from the
managed save `boxman down` left (behind by the downtime), one `control start`
restores the same way, and one `control resume` or `up` resumes after a
pause. Each time, boxman sets the guest's clock from the host's with
`virsh domtime --now`, through the QEMU guest agent, and says so:

```
set the clock of bprj__lab__bprj_win_win01 from the host's (it was 71 s behind)
```

It never makes a restore noticeably slower. It first reads the agent's
channel from the VM's live definition, which takes no longer than a
`virsh dumpxml`. libvirt records the channel connected before a revert or a
managed-save restore returns, so a channel that reads disconnected, or a VM
that has none, means no agent: boxman warns at once and does not wait. Only
a channel that has no state yet is looked at again, and an agent that does
not answer yet is asked again, within `guest_clock_wait`, 1 s by default.
Every virsh call is cut off so the whole cannot outlast that wait by more
than a second, even with a hung agent.

Whenever the clock was not set (no agent, none connected, no answer in time,
a call cut off, a refusal), boxman warns once, saying how far behind the
clock is left: the snapshot's age, or how long ago the managed save was
written. The restore still succeeds. A snapshot that saved no memory leaves
the VM shut off, and its guest reads the time when it boots; one taken while
paused comes back paused, and its clock is set when boxman resumes it.

`guest_clock_wait` is a number of seconds, 0 or more. It is set on the
provider, in `conf.yml` (`provider.libvirt`) or in `boxman.yml`
(`providers.libvirt`, which `conf.yml` overrides), and a cluster or a VM can
override it:

```yaml
provider:
  libvirt:
    guest_clock_wait: 1        # the default
clusters:
  lab:
    guest_clock_wait: 0.1      # this cluster's VMs (for development, say)
    vms:
      win01:
        guest_clock_wait: 3    # this VM
```

0 looks at the channel and asks the agent once, without waiting. When a
wait below the default runs out, the warning says so. A value that is not a
number of seconds, 0 or more, is refused before any command runs.

On Linux the agent sets the clock only where the guest has `/sbin/hwclock`:
Ubuntu 24.04's cloud image has none, its agent refuses, and the warning says
so. `util-linux-extra` provides it; the agent looks for it once, so restart
`qemu-guest-agent` after installing it.

---

## The in-use scan

Before boxman removes a file that another VM could still need (a CD-ROM
overlay after a restore, a VM's disks at teardown, a memory image after a
delete or collapse, a leftover a `take --force` clears), it reads what every
domain defined on the host uses: each domain's disks and CD-ROMs in both
definitions, every layer below them, and its block jobs. It reads every
domain because one project's file can be another's base (the ISO cache, a
shared template). A file whose use cannot be told is kept.

On a host with many domains this scan costs a few `virsh` and `qemu-img`
calls per domain. boxman reads up to 8 domains at once, each as it would
alone, and credits a file to the first domain listed whatever order the
reads finish in. `in_use_scan_workers` sets that number in `provider.libvirt`
(conf.yml) or `providers.libvirt` (boxman.yml):

```yaml
provider:
  libvirt:
    in_use_scan_workers: 8     # the default; 1 reads one domain at a time
```

---

## Soft requirements

| Tool | Used by | Soft-require behaviour |
|---|---|---|
| `virt-sparsify` (from `guestfs-tools`) | `compact --method sparsify` and `auto` when snapshots exist | Pre-flight check; clear error before any destructive op: `virt-sparsify not found in the runtime — install guestfs-tools, or pass --method convert` |
| `zstd` | Memory compression / decompression | Same — error if missing on first compress; restore-side decompress also surfaces a clear error |
| `qemu-guest-agent` | `trim` (`virsh domfstrim`) | If unresponsive, `trim` errors with `qemu-guest-agent not responsive. install it in the guest (apt/dnf install qemu-guest-agent) and reboot.` |
| `qemu-guest-agent` | Setting the guest's clock after a memory restore or a resume (`virsh domtime --now`) | Without one that answers, a warning says how far behind the clock is left; the restore or resume still succeeds |

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
| `could not set the clock of … (no QEMU guest agent is connected to its channel)` after a restore | No guest agent runs in the guest | Install and start `qemu-guest-agent` in the guest; until then its clock stays behind by the amount the warning gives |
| `could not set the clock of … (no QEMU guest agent answered within 0.1 s …; guest_clock_wait is 0.1 s, below its 1 s default)` | The agent did not answer within a short `guest_clock_wait` | Raise `guest_clock_wait`, or leave it at its default |
| `could not set the clock of … refused (… not currently supported …)` | A Linux guest without `/sbin/hwclock`, whose agent then refuses to set the time | In the guest: `sudo apt install util-linux-extra` (Debian, Ubuntu), then `sudo systemctl restart qemu-guest-agent`: the agent looks for `hwclock` once |
| `compact` refuses with "snapshots present and --drop-snapshots not given" | Method `convert`/`convert-compressed` would flatten the chain | Either pass `--drop-snapshots`, or use `--method sparsify` (or default `auto`) which preserves the chain |
| `compact` refuses with "vm … is running and --no-shutdown was passed" | You opted out of auto-shutdown but the VM is up | Drop `--no-shutdown`, or shut the VM down manually first |
| `virt-sparsify not found in the runtime` | `guestfs-tools` not installed in the active runtime | Install `guestfs-tools` with `dnf` (RHEL/Rocky) or `apt` (Debian/Ubuntu); or use `--method convert`; or switch to the docker runtime which already includes it |
| `zstd not found in runtime` (during compress / decompress) | `zstd` missing on the host or in the runtime | Install zstd; the docker runtime image already includes it |
| `df` shows huge `SNAPMEM` | Snapshot memory dumps not compressed | `boxman storage compress-snapshots` |
| `df` shows huge `ALLOC` and `CHAIN > 1` | Sparse holes in overlays | `boxman storage compact` (auto picks sparsify) |
| `df` shows huge `ALLOC` and `CHAIN = 1` and no snapshots | Allocated-but-zero clusters in the head qcow2 | `boxman storage compact --method convert-compressed` for the most aggressive shrink |
