"""``boxman snapshot prune``: the snapshot files of this project's VMs that
no snapshot records and no domain uses any more (#294), listed, and removed
after a prompt."""

from __future__ import annotations

import os
import re
import time

from boxman.exceptions import ConfigError, SnapshotError
from boxman.image_cache import ImageCache
from boxman.providers.libvirt.disk import disk_path_for
from boxman.providers.libvirt.disk_cleanup import (
    MOUNTINFO,
    FilesInUse,
    last_scan_failure,
    memory_image_name,
    mount_points,
)
from boxman.providers.libvirt.disk_ownership import disk_logical_name
from boxman.providers.libvirt.pending_overlays import (
    PendingOverlays,
    identity_of,
    ledger_scope,
)
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
    keep_unnamed,
    prune,
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
        ``boxman snapshot prune``: list the files a snapshot of the
        selected VMs left in the project's snapshot directories, each
        ``removable``, ``kept`` with the reason, or ``unknown``
        (:func:`snapshot_prune.decide`), and what removing the removable
        ones would reclaim; then remove those, after a prompt that lists
        them unless ``-y`` (:func:`snapshot_prune.prune`: each only while
        it is still the file listed, newest layer first). ``--dry-run``
        lists them and changes nothing.

        Given *PATH*\\ s, only those, all or nothing: one that is not
        removable -- a removable one not named counts as staying, so what
        it is built on is not removable -- stops everything.

        Without a path, and unless ``--kind`` leaves overlays out, the
        overlays of the selected VMs' CD-ROM images that their restores
        left for later removal (``pending_overlays``) go too: through the
        removal a ``snapshot take`` or a teardown runs, behind its own
        checks, on one fresh scan of every domain
        (``LibVirtSession.remove_pending_iso_overlays``). Prune itself
        never removes a file beside an image, nor one such a record names.

        The directories, de-duplicated by identity, are each selected
        cluster's workdir, its ``disk_snapshot_dir`` and
        ``memory_snapshot_dir``, those set before -- a storage pool boxman
        defined for its ``disk_snapshot_dir`` is still on one, or a snapshot
        records an overlay or a memory image there under the name a take
        gives it -- but never the image cache, the directory of a domain's
        CD-ROM or other media source, or a mount point one is under.

        Raises:
            ConfigError: an unknown ``--cluster``.
            SnapshotError: a directory could not be listed, or a scan failed:
                every file is ``unknown``, the table is printed all the same,
                and nothing is pruned; a path named is not removable, and
                nothing is pruned; or a removal failed, after all the others.
        """
        dry_run = bool(getattr(cli_args, 'dry_run', False))
        named = [os.path.abspath(os.path.expanduser(path))
                 for path in getattr(cli_args, 'paths', None) or []]
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
            if named:
                raise SnapshotError(
                    "nothing was pruned: " + "; ".join(
                        f"{path}: no VM is selected" for path in named))
            return
        clusters = list(dict.fromkeys(cluster for _f, cluster, _v, _w
                                      in targets))
        session = self.session_for_cluster(clusters[0])
        domains = session.domains()
        references = session.snapshot_references()
        held = session.files_held()
        # why G1 failed, where the in-use scan behind it did and said why
        held_failure = last_scan_failure() if held is None else None
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
        ledger = PendingOverlays(scope=ledger_scope(session.provider_config))
        scans = Scans(
            held=held, references=references,
            headers=session.image_headers(listing.files),
            owners={entry.path: session.memory_image_owner(entry.path)
                    for entry in listing.entries
                    if entry.kind == MEMORY_IMAGE and entry.lstat is not None},
            domains=domains or [], mounts=mount_points(MOUNTINFO),
            failures=failures, pending=self._pending_files(ledger),
            held_failure=held_failure)
        now = time.time()
        entries = decide(listing, vms, known, scans, now, min_age, kinds)
        # the advisory lock probe, of the overlays it would list as
        # removable only; what it keeps keeps what is under it, so again
        scans.locked = session.image_locks_held(
            [entry.path for entry in entries
             if entry.kind in OVERLAYS and entry.status == REMOVABLE])
        if scans.locked:
            entries = decide(listing, vms, known, scans, now, min_age, kinds)
        refused, chosen_named = [], {}
        if named:
            chosen_named = self._named_entries(named, entries)
            keep_unnamed(entries, listing, scans,
                         {entry.path for entry in chosen_named.values()
                          if entry is not None})
            refused = [f"{path}: {self._why_not_removable(entry)}"
                       for path, entry in chosen_named.items()
                       if entry is None or entry.status != REMOVABLE]
        drain = (not named and (kinds is None or any(
            kind in kinds for kind in OVERLAYS)))
        waiting = (self._waiting_iso_overlays(ledger, selected, vms)
                   if drain else {})
        self._print_prune(
            session, scans, dirs, entries, notes, listing, references,
            bool(getattr(cli_args, 'show_all', False)),
            waiting=self._pending_iso_overlay_notes(
                selected, held, references, domains,
                ledger_scope(session.provider_config)),
            only=({entry.path for entry in chosen_named.values()
                   if entry is not None} if named else None))
        if dry_run:
            print("dry run: nothing was changed")
        failed = scan_failures(listing, scans)
        if failed:
            raise SnapshotError("nothing was pruned: " + "; ".join(failed))
        if refused:
            raise SnapshotError("nothing was pruned: " + "; ".join(refused))
        if dry_run:
            return
        chosen = [entry for entry in entries if entry.status == REMOVABLE]
        if not chosen and not waiting:
            print("nothing to prune")
            return
        if not self._prune_confirmed(cli_args, chosen, waiting):
            return
        self._prune_now(session, chosen, waiting)

    @staticmethod
    def _pending_files(ledger: PendingOverlays) -> FilesInUse:
        """Every overlay of a CD-ROM image a restore on this connection left
        for later removal, by path and by identity as recorded, mapped to
        what says so: prune never removes one itself."""
        pending = FilesInUse()
        for chain in ledger.chains():
            why = (f"a restore of {chain.vm} left it for later removal, as "
                   f"an overlay of {chain.iso}: its next take, its destroy, "
                   f"or a prune with no path removes it")
            for layer, identity in zip(chain.layers, chain.identities,
                                       strict=True):
                pending.setdefault(layer, why)
                pending.setdefault(os.path.realpath(layer), why)
                if identity is not None:
                    pending.identities.setdefault(tuple(identity), why)
        return pending

    @staticmethod
    def _waiting_iso_overlays(ledger: PendingOverlays, selected: set[str],
                              vms: dict[str, VmFacts]) -> dict[str, list[str]]:
        """``{vm: [overlay, ...]}``: the overlays of its CD-ROM images that
        restores left for later removal, there now, of each of *selected*
        that has a domain (one that has none is destroy's)."""
        waiting: dict[str, list[str]] = {}
        for chain in ledger.chains():
            if chain.vm in selected and vms[chain.vm].uuid is not None:
                waiting.setdefault(chain.vm, []).extend(
                    layer for layer in chain.layers
                    if os.path.lexists(layer))
        return {vm: list(dict.fromkeys(layers))
                for vm, layers in waiting.items() if layers}

    @staticmethod
    def _named_entries(named: list[str],
                       entries: list[Entry]) -> dict[str, Entry | None]:
        """``{path named: the entry it names, or None}``, a path matched as
        given and with its directory resolved."""
        def key(path: str) -> str:
            return os.path.join(os.path.realpath(os.path.dirname(path)),
                                os.path.basename(path))
        by_key = {key(entry.path): entry for entry in entries}
        by_path = {entry.path: entry for entry in entries}
        return {path: by_path.get(path) or by_key.get(key(path))
                for path in dict.fromkeys(named)}

    @staticmethod
    def _why_not_removable(entry: Entry | None) -> str:
        """Why a path named may not be pruned: see :meth:`snapshot_prune`."""
        if entry is None:
            return ("it is no snapshot file of the selected VMs in a "
                    "directory prune scans (see `boxman snapshot prune "
                    "--dry-run --all`)")
        if entry.status == SKIPPED:
            return f"--kind leaves its kind, {entry.kind}, out"
        return f"{entry.status}: {entry.reason}"

    def _prune_confirmed(self, cli_args, chosen: list[Entry],
                         waiting: dict[str, list[str]]) -> bool:
        """List what is about to be removed, and ask, unless ``-y``."""
        fmt = self._format_bytes
        if chosen:
            print(f"These files will be removed (reclaiming "
                  f"{fmt(sum(entry.size for entry in chosen))}):\n")
            for entry in sorted(chosen, key=lambda e: e.path):
                print(f"  {entry.path}")
            print()
        if waiting:
            print(f"{'And these' if chosen else 'These'} overlays of CD-ROM "
                  f"images, which restores left for later removal, each once "
                  f"a scan of every domain finds nothing uses it, as `boxman "
                  f"snapshot take` removes them:\n")
            for vm in sorted(waiting):
                for layer in waiting[vm]:
                    print(f"  {layer}")
            print()
        if getattr(cli_args, 'yes', False):
            return True
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            print("No input available, aborted.")
            return False
        if answer not in ("y", "yes"):
            print("Aborted.")
            return False
        return True

    def _prune_now(self, session, chosen: list[Entry],
                   waiting: dict[str, list[str]]) -> None:
        """Remove *chosen* (:func:`snapshot_prune.prune`), then what
        *waiting* lists through the removal a take runs, and say what went.

        Raises:
            SnapshotError: a removal failed, or what the domains use could
                not be told for the overlays waiting: after all the others.
        """
        fmt = self._format_bytes
        result = prune(chosen)
        for entry in result.removed:
            print(f"removed {entry.path} ({fmt(entry.size)})")
        for entry in result.gone:
            print(f"{entry.path} was gone already")
        problems = []
        for entry, why in result.failed + result.kept:
            print(f"could not remove {entry.path}: {why}")
            problems.append(f"{entry.path}: {why}")
        if result.removed:
            session.refresh_pools_holding(
                [entry.path for entry in result.removed])
        reclaimed, count = result.reclaimed, len(result.removed)
        if waiting:
            drained, gone, more = self._drain_iso_overlays(session, waiting)
            reclaimed, count = reclaimed + drained, count + gone
            problems += more
        print(f"reclaimed {fmt(reclaimed)} ({count} "
              f"file{'' if count == 1 else 's'})")
        if problems:
            raise SnapshotError(
                f"could not prune {len(problems)} file(s): "
                + "; ".join(problems))

    def _drain_iso_overlays(self, session, waiting: dict[str, list[str]]
                            ) -> tuple[int, int, list[str]]:
        """
        Remove the overlays *waiting* lists -- ``{vm: [overlay, ...]}``,
        of each VM's CD-ROM images, left for later removal by its restores
        -- as its next take would (``remove_pending_iso_overlays``), on one
        scan of every domain taken now, shared by the VMs. ``(what it
        reclaimed, how many went, problems)``: an overlay it could not
        remove, or VMs for which nothing could be told. One a domain uses,
        or a snapshot names, stays on record, and is said to.
        """
        fmt = self._format_bytes
        sizes = {}
        for layers in waiting.values():
            for layer in layers:
                try:
                    sizes[layer] = os.lstat(layer).st_blocks * 512
                except OSError:
                    sizes[layer] = 0
        taken: list = []

        def scan():
            if not taken:
                taken.append(session.disk_paths_in_use())
            return taken[0]

        reclaimed, gone, problems = 0, 0, []
        for vm in sorted(waiting):
            try:
                outcome = session.remove_pending_iso_overlays(
                    vm, scan=scan, only=set(waiting[vm]))
            except Exception as exc:
                problems.append(f"the overlays of the CD-ROM images of {vm} "
                                f"({type(exc).__name__}: {exc})")
                continue
            for layer in outcome.removed:
                print(f"removed {layer} ({fmt(sizes.get(layer, 0))})")
                reclaimed += sizes.get(layer, 0)
                gone += 1
            if not outcome.decided:
                # why this VM's were not, as the removal told it: the scan
                # failed, or this VM's snapshots could not be read
                problems.append(
                    f"the overlays of the CD-ROM images of {vm}: "
                    + (outcome.undecided or "whether a snapshot names them, "
                       "or a domain uses them, could not be told"))
            problems += [f"{layer}: it could not be removed"
                         for layer in outcome.failed]
        return reclaimed, gone, problems

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
                     show_all: bool, waiting: list[str] = (),
                     only: set[str] | None = None) -> None:
        """The listing (design §6): what was scanned, a row per file, the
        totals, and the notes -- *waiting* among them, the overlays of CD-ROM
        images left for later removal. With *only*, the paths named, just
        their rows, recorded or not."""
        fmt = self._format_bytes
        uri = session.provider_config.get('uri', 'qemu:///system')
        print(f"scanned {len(scans.domains)} domains ({uri}), {len(dirs)} "
              f"director{'y' if len(dirs) == 1 else 'ies'}: "
              f"{', '.join(scope.path for scope in dirs) or '-'}")
        for note in notes:
            print(f"note: {note}")
        listed = [entry for entry in entries if entry.status != SKIPPED]
        shown = [entry for entry in listed if show_all or not entry.recorded]
        if only is not None:
            listed = shown = [entry for entry in entries
                              if entry.path in only]
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
        for line in waiting:
            print(f"note: {line}")

    @staticmethod
    def _pending_iso_overlay_notes(selected: set[str], held: Held | None,
                                   references: References | None,
                                   domains: list[tuple[str, str]] | None,
                                   scope: str) -> list[str]:
        """
        Lines for what restores left beside CD-ROM images, which the table
        does not list: an image's directory is no snapshot directory.

        - each overlay libvirt made of a CD-ROM image of one of *selected*
          that a restore recorded for later removal (``pending_overlays``),
          and what would keep it now: a domain uses it (G1), a snapshot
          names it (G2), or another file has taken its name since. The VM's
          next ``snapshot take``, or its destroy, removes it;
        - each one recorded for a VM that has no domain on this connection
          any more, of any project: nothing will remove it;
        - each record that cannot be read;
        - an interrupted restore's backup of such an overlay, ``.preserve``,
          its scratch copy or a reservation scaffold, beside one of those
          images or a CD-ROM image of one of *selected*.
        """
        def who(scan, path: str, identity) -> str | None:
            if scan is None:
                return None
            return (scan.get(path) or scan.get(os.path.realpath(path))
                    or (scan.identities.get(identity) if identity else None))

        ledger = PendingOverlays(scope=scope)
        defined = ({name for name, _uuid in domains}
                   if domains is not None else None)
        lines = []
        isos = {}
        for chain in ledger.chains():
            orphan = defined is not None and chain.vm not in defined
            if chain.vm not in selected and not orphan:
                continue
            isos[chain.iso] = chain.vm
            for layer, recorded in zip(chain.layers, chain.identities,
                                       strict=True):
                if not os.path.lexists(layer):
                    continue
                identity = identity_of(layer)
                if orphan:
                    lines.append(
                        f"{layer} was left for removal by a restore of "
                        f"{chain.vm}, which has no domain any more: nothing "
                        f"removes it. Check that no domain uses it, then "
                        f"remove it and its record, {chain.path}")
                    continue
                why = []
                if recorded is None or identity != recorded:
                    why.append("another file has taken its name since: it "
                               "stays")
                else:
                    domain = who(held, layer, identity)
                    if domain:
                        why.append(f"domain {domain} uses it")
                    named = who(references, layer, identity)
                    if named:
                        why.append(f"{named} names it")
                lines.append(
                    f"{layer} waits for removal: an overlay libvirt made of "
                    f"{chain.iso} for a CD-ROM of {chain.vm}; a prune with "
                    f"no path, its next `boxman snapshot take`, or its "
                    f"destroy, removes it once nothing uses it"
                    + (f" ({'; '.join(why)})" if why else ""))
        lines += [f"{path}, a record of CD-ROM overlays left for later "
                  f"removal, cannot be read ({why}): check the overlays it "
                  f"names by hand, then remove it"
                  for path, why in sorted(ledger.unreadable.items())]
        for path, domain in (held.media.items() if held is not None else ()):
            if domain in selected:
                isos.setdefault(path, domain)
        lines += SnapshotPruneMixin._preserves_beside(isos)
        return lines

    @staticmethod
    def _preserves_beside(isos: dict[str, str]) -> list[str]:
        """A line for each restore's leftover backup of an overlay of one
        of *isos* -- ``{image: a VM it is a CD-ROM of}`` -- beside it: an
        interrupted restore leaves them, and a restore refuses on one."""
        lines = []
        seen = set()
        for iso in sorted(isos):
            directory = os.path.dirname(iso)
            stem = re.escape(os.path.splitext(os.path.basename(iso))[0])
            left = re.compile(stem + r"\.[0-9]+\.preserve(\.copy)?"
                              r"(\.[0-9]+\.[0-9a-f]{12})?")
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                continue
            for name in names:
                path = os.path.join(directory, name)
                if path in seen or not left.fullmatch(name):
                    continue
                seen.add(path)
                overlay = path[:path.index(".preserve")]
                state = ("which is in place" if os.path.lexists(overlay)
                         else f"which is missing: mv -fT {path} {overlay}")
                lines.append(
                    f"{path} is an interrupted restore's backup of {overlay}, "
                    f"an overlay of {iso} for a CD-ROM of {isos[iso]}, "
                    f"{state}. A restore of the VM refuses while it is "
                    f"there: check it, then remove it")
        return lines

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
