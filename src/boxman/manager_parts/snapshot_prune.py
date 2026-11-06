"""``boxman snapshot prune``: the snapshot files of this project's VMs that
no snapshot records and no domain uses any more (#294). This version lists
them (``--dry-run``); removing them comes in a later one."""

from __future__ import annotations

import os
import time

from boxman.exceptions import ConfigError, SnapshotError
from boxman.image_cache import ImageCache
from boxman.providers.libvirt.disk import disk_path_for
from boxman.providers.libvirt.disk_cleanup import (
    MOUNTINFO,
    memory_image_name,
    mount_points,
)
from boxman.providers.libvirt.disk_ownership import disk_logical_name
from boxman.providers.libvirt.snapshot import SnapshotManager
from boxman.providers.libvirt.snapshot_prune import (
    KEPT,
    MEMORY_IMAGE,
    OVERLAYS,
    PRESERVE_BACKUP,
    REMOVABLE,
    SKIPPED,
    UNKNOWN,
    Entry,
    Held,
    Listing,
    References,
    Scans,
    ScopeDir,
    VmFacts,
    decide,
    find_candidates,
    scan_failures,
)

#: the order the table lists the statuses in
_ORDER = {REMOVABLE: 0, KEPT: 1, UNKNOWN: 2}


def _mount_point_of(path: str, mounts: set[str]) -> str | None:
    """The mount point *path* is under, resolved, if *mounts* names one."""
    current = os.path.dirname(os.path.realpath(path))
    while True:
        if current in mounts:
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


class SnapshotPruneMixin:

    def snapshot_prune(self, cli_args) -> None:
        """
        ``boxman snapshot prune --dry-run``: list the files a snapshot of
        the selected VMs left in the project's snapshot directories, each
        ``removable``, ``kept`` with the reason, or ``unknown``
        (:func:`snapshot_prune.decide`), and what removing the removable
        ones would reclaim. Nothing is changed.

        The directories, de-duplicated by identity, are each selected
        cluster's workdir, its ``disk_snapshot_dir`` and
        ``memory_snapshot_dir``, those set before -- a storage pool boxman
        defined for its ``disk_snapshot_dir`` is still on one, or a snapshot
        records an overlay or a memory image there under the name a take
        gives it -- but never the image cache, the directory of a domain's
        CD-ROM or other media source, or a mount point one is under.

        Raises:
            ConfigError: run without ``--dry-run`` (removal comes in a later
                version), or an unknown ``--cluster``.
            SnapshotError: a directory could not be listed, or a scan failed:
                every file is ``unknown``, the table is printed all the same,
                and nothing could be pruned.
        """
        if not getattr(cli_args, 'dry_run', False):
            raise ConfigError(
                "snapshot prune only lists what it would remove in this "
                "version: run it with --dry-run. Removing those files comes "
                "in a later version.")
        kinds = getattr(cli_args, 'kinds', None)
        min_age = getattr(cli_args, 'min_age', 3600)
        self._update_sessions_with_runtime()
        try:
            targets = self._select_vm_targets(cli_args)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        cluster_filter = getattr(cli_args, 'cluster', None)
        notes = [f"skipped docker-compose cluster {name}: its snapshots are "
                 f"container images" for name in self._compose_clusters
                 if cluster_filter in (None, name)]
        if not targets:
            for note in notes:
                print(f"note: {note}")
            print("no VM matched the --cluster/--vms selection")
            return
        clusters = list(dict.fromkeys(cluster for _f, cluster, _v, _w
                                      in targets))
        session = self.session_for_cluster(clusters[0])
        domains = session.domains()
        references = session.snapshot_references()
        held = session.files_held()
        vms = self._prune_vm_facts(domains, held)
        known = {name for name, _uuid in (domains or [])} | set(vms)
        dirs, excluded, failures = self._prune_dirs(
            clusters, session, [vms[full] for full, *_ in targets],
            references, held)
        if domains is None:
            failures.insert(0, "the domains could not be listed")
        notes += excluded
        listing = find_candidates(dirs, vms, known)
        selected = {full for full, *_ in targets}
        listing.entries = [entry for entry in listing.entries
                           if entry.vm in selected]
        scans = Scans(
            held=held, references=references,
            headers=session.image_headers(listing.files),
            owners={entry.path: session.memory_image_owner(entry.path)
                    for entry in listing.entries
                    if entry.kind == MEMORY_IMAGE and entry.lstat is not None},
            domains=domains or [], mounts=mount_points(MOUNTINFO),
            failures=failures)
        now = time.time()
        entries = decide(listing, vms, known, scans, now, min_age, kinds)
        # the advisory lock probe, of the overlays it would list as
        # removable only; what it keeps keeps what is under it, so again
        scans.locked = session.image_locks_held(
            [entry.path for entry in entries
             if entry.kind in OVERLAYS and entry.status == REMOVABLE])
        if scans.locked:
            entries = decide(listing, vms, known, scans, now, min_age, kinds)
        self._print_prune(session, scans, dirs, entries, notes, listing,
                          references, bool(getattr(cli_args, 'show_all',
                                                   False)))
        failed = scan_failures(listing, scans)
        if failed:
            raise SnapshotError("nothing was pruned: " + "; ".join(failed))

    def _prune_vm_facts(self, domains: list[tuple[str, str]] | None,
                        held: Held | None) -> dict[str, VmFacts]:
        """Every VM of the project's libvirt clusters, by full name: its
        domain's UUID, the disks it declares, and the names of the disks it
        declares or its domain has (``<vm>_<name>.<type>``)."""
        uuids = dict(domains or [])
        prj_name = f'bprj__{self.config["project"]}__bprj'
        vms: dict[str, VmFacts] = {}
        for cluster_name, cluster in self._vm_clusters.items():
            workdir = os.path.expanduser(cluster['workdir'])
            for vm_name, vm_info in (cluster.get('vms') or {}).items():
                full = f"{prj_name}_{cluster_name}_{vm_name}"
                declared = [disk for disk in (vm_info or {}).get('disks') or []
                            if isinstance(disk, dict)]
                disks = [disk_path_for(workdir, full)] + [
                    disk_path_for(workdir, disk_logical_name(disk),
                                  driver_type=(disk.get('driver') or {}).get(
                                      'type', 'qcow2'),
                                  disk_prefix=full)
                    for disk in declared]
                names = [disk_logical_name(disk) for disk in declared]
                for path, domain in (held.items() if held else ()):
                    stem = os.path.splitext(os.path.basename(path))[0]
                    if domain == full and stem.startswith(f"{full}_"):
                        names.append(stem[len(full) + 1:])
                vms[full] = VmFacts(
                    name=full, cluster=cluster_name, uuid=uuids.get(full),
                    disks=tuple(disks),
                    disk_names=tuple(dict.fromkeys(n for n in names if n)))
        return vms

    def _prune_dirs(self, clusters: list[str], session,
                    vms: list[VmFacts], references: References | None,
                    held: Held | None
                    ) -> tuple[list[ScopeDir], list[str], list[str]]:
        """``(directories, notes, failures)``: see :meth:`snapshot_prune`.
        A directory that does not exist is left out; one that cannot be
        looked up is a failure."""
        project = self.config['project']
        found: list[ScopeDir] = []
        failures: list[str] = []
        for cluster_name in clusters:
            workdir = os.path.expanduser(
                self.config['clusters'][cluster_name]['workdir'])
            found.append(ScopeDir(workdir,
                                  f"the workdir of cluster {cluster_name}"))
            disk_dir = self.cluster_disk_snapshot_dir(cluster_name)
            if disk_dir is not None:
                found.append(ScopeDir(
                    disk_dir, f"the disk_snapshot_dir of cluster "
                              f"{cluster_name}"))
            pools = session.disk_snapshot_pool_dirs(project, cluster_name)
            if pools is None:
                failures.append("the storage pools could not be read, so a "
                                "disk_snapshot_dir set before is not known")
            for name, target in pools or ():
                found.append(ScopeDir(
                    target, f"storage pool {name} is on it, a "
                            f"disk_snapshot_dir of cluster {cluster_name}"))
            found.append(ScopeDir(
                self.cluster_memory_snapshot_dir(cluster_name),
                f"the memory_snapshot_dir of cluster {cluster_name}"))
        for vm in vms:
            snapshots = (references.snapshots.get(vm.name, {})
                         if references is not None else {})
            for snap, record in snapshots.items():
                for target, overlay in record.disks.items():
                    source = record.taken_on.get(target)
                    if source and os.path.basename(overlay) == (
                            SnapshotManager._overlay_name(source, snap)):
                        found.append(ScopeDir(
                            os.path.dirname(overlay),
                            f"snapshot '{snap}' of {vm.name} records an "
                            f"overlay there"))
                if record.memory and os.path.basename(record.memory) == (
                        memory_image_name(vm.name, snap)):
                    found.append(ScopeDir(
                        os.path.dirname(record.memory),
                        f"snapshot '{snap}' of {vm.name} records its memory "
                        f"image there"))
        excluded = self._prune_exclusions(held)
        dirs: list[ScopeDir] = []
        notes: list[str] = []
        seen: set[tuple[int, int]] = set()
        for scope in found:
            try:
                st = os.stat(scope.path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                failures.append(f"{scope.path} could not be looked up "
                                f"({exc})")
                continue
            identity = (st.st_dev, st.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            if identity in excluded:
                notes.append(f"not scanned: {scope.path}, "
                             f"{excluded[identity]}")
                continue
            dirs.append(scope)
        return dirs, notes, failures

    def _prune_exclusions(self, held: Held | None) -> dict[tuple[int, int],
                                                            str]:
        """The directories no prune scans, by identity, and why: the image
        cache, the directory of every domain's media source, and the mount
        point each one is under."""
        excluded: dict[tuple[int, int], str] = {}

        def exclude(path: str, why: str) -> None:
            try:
                st = os.stat(path)
            except OSError:
                return
            excluded.setdefault((st.st_dev, st.st_ino), why)

        cache = ImageCache.from_config(
            (getattr(self, 'app_config', None) or {}).get('cache') or {})
        exclude(cache.cache_dir, "the image cache")
        mounts = mount_points(MOUNTINFO) or set()
        for path, domain in sorted((held.media if held else {}).items()):
            exclude(os.path.dirname(path),
                    f"which holds {path}, a media source of domain {domain}")
            mount = _mount_point_of(path, mounts)
            if mount is not None and mount != os.sep:
                exclude(mount, f"the mount point {path} is under, a media "
                               f"source of domain {domain}")
        return excluded

    def _print_prune(self, session, scans: Scans, dirs: list[ScopeDir],
                     entries: list[Entry], notes: list[str],
                     listing: Listing, references: References | None,
                     show_all: bool) -> None:
        """The listing (design §6): what was scanned, a row per file, the
        totals, and the notes."""
        fmt = self._format_bytes
        uri = session.provider_config.get('uri', 'qemu:///system')
        print(f"scanned {len(scans.domains)} domains ({uri}), {len(dirs)} "
              f"director{'y' if len(dirs) == 1 else 'ies'}: "
              f"{', '.join(scope.path for scope in dirs) or '-'}")
        for note in notes:
            print(f"note: {note}")
        listed = [entry for entry in entries if entry.status != SKIPPED]
        shown = [entry for entry in listed if show_all or not entry.recorded]
        print()
        if shown:
            rows = [("STATUS", "SIZE", "KIND", "PATH", "REASON")] + [
                (entry.status, fmt(entry.size), entry.kind, entry.path,
                 entry.reason)
                for entry in sorted(shown, key=lambda e: (
                    _ORDER.get(e.status, 3), e.path))]
            widths = [max(len(row[i]) for row in rows) for i in range(4)]
            for row in rows:
                print("  ".join([row[0].ljust(widths[0]),
                                 row[1].rjust(widths[1]),
                                 row[2].ljust(widths[2]),
                                 row[3].ljust(widths[3]), row[4]]).rstrip())
        else:
            print("no snapshot file of these VMs is left over")
        print()

        def total(status: str) -> list[Entry]:
            return [entry for entry in shown if entry.status == status]

        removable = total(REMOVABLE)
        parts = [f"would reclaim {fmt(sum(e.size for e in removable))} "
                 f"({len(removable)} file{'' if len(removable) == 1 else 's'})"]
        for status in (KEPT, UNKNOWN):
            group = total(status)
            parts.append(f"{status} {len(group)} "
                         f"({fmt(sum(e.size for e in group))})")
        hidden = len(listed) - len(shown)
        if hidden:
            parts.append(f"{hidden} recorded or in use not shown (--all)")
        print("; ".join(parts))
        for path in listing.stranded:
            print(f"note: {path} is a directory a removal left behind; "
                  f"check what is in it, then remove it by hand")
        if any(entry.kind == PRESERVE_BACKUP for entry in removable):
            print("note: a .preserve copy made with reflinks shares blocks "
                  "with its overlay; removing it frees only those it does "
                  "not share")
        for raw in self._raw_and_zst(entries, references):
            print(f"note: both {raw} and {raw}.zst are there; `boxman "
                  f"storage compress-snapshots` keeps one of them")
        if scans.locked:
            print(f"note: kept {len(scans.locked)} file(s) whose image lock "
                  f"a process holds")
        print("dry run: nothing was changed")

    @staticmethod
    def _raw_and_zst(entries: list[Entry],
                     references: References | None) -> list[str]:
        """The memory images of the listed VMs that a snapshot records and
        that are there both as ``.raw`` and as ``.raw.zst``."""
        vms = {entry.vm for entry in entries}
        found = []
        for vm in sorted(vms):
            snapshots = (references.snapshots.get(vm, {})
                         if references is not None else {})
            for record in snapshots.values():
                raw = record.memory
                if (raw and os.path.isfile(raw)
                        and os.path.isfile(f"{raw}.zst")):
                    found.append(raw)
        return found
