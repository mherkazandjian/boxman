"""Snapshot and storage flows for BoxmanManager."""

import json
import os
import time

from boxman import log
from boxman.exceptions import SnapshotError


class SnapshotsMixin:

    ### start snapshot functions ####
    def snapshot_list(self, cli_args):
        """
        List snapshots of the VMs and docker-compose clusters in the project.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        for full_vm_name, cluster_name, _vm_name, _workdir in (
                self._select_vm_targets(cli_args)):
            self.session_for_cluster(cluster_name).snapshot_list(full_vm_name)
        # docker-compose clusters (docker commit-backed, D3)
        for cname, cluster in self._select_dc_clusters(cli_args):
            snaps = self.session_for_cluster(cname).snapshot_list_cluster(cname, cluster)
            self.logger.info(f"cluster: {cname} (docker-compose)")
            if not snaps:
                self.logger.info("  (no snapshots)")
            for name in sorted(snaps, key=lambda k: snaps[k].get('created', '')):
                snap = snaps[name]
                self.logger.info(
                    f"  {name}  created={snap.get('created', '?')}  "
                    f"{snap.get('description', '')}".rstrip())
                for box, tag in (snap.get('boxes') or {}).items():
                    self.logger.info(f"      {box}: {tag}")

    def snapshot_log(self, cli_args):
        """
        Aggregated git-log-style view of snapshots across every VM.

        Each unique snapshot name becomes one row showing description,
        creation time, the list of VMs that have it, and a ``← current``
        marker if it's the current snapshot for any VM. Default ordering
        is newest-first by chain depth (with creation_time as tiebreaker);
        ``--reverse`` flips it. ``--no-graph`` suppresses the leftmost
        ``*``/``|`` column; ``--json`` emits machine-readable output
        matching the shape of ``boxman ps --json``.
        """
        from boxman.utils.snapshot_graph import render_graph

        prj_name = f'bprj__{self.config["project"]}__bprj'
        prj_prefix = f'{prj_name}_'

        # 1. Per-VM data.
        per_vm: dict[str, dict] = {}
        for full_vm_name, cluster_name, _vm_name, _workdir in (
                self._select_vm_targets(cli_args)):
            data = self.session_for_cluster(cluster_name).snapshot_log_data(full_vm_name)
            per_vm[full_vm_name] = data

        if not any(d.get('chain') for d in per_vm.values()):
            self.logger.info("no snapshots found")
            return

        # 2. Aggregate by snapshot name.
        aggregated: dict[str, dict] = {}
        for full_vm_name, data in per_vm.items():
            short_vm = full_vm_name[len(prj_prefix):] \
                if full_vm_name.startswith(prj_prefix) else full_vm_name
            current = data.get('current')
            for snap in data.get('chain', []):
                name = snap['name']
                entry = aggregated.setdefault(name, {
                    'name': name,
                    'description': snap.get('description', ''),
                    'creation_time': snap.get('creation_time'),
                    'parent': snap.get('parent'),
                    'depth': snap.get('depth', 0),
                    'vms': [],
                    'current_for': [],
                })
                entry['vms'].append(short_vm)
                # Take the max depth seen across VMs (handles partial-take
                # divergence where some VMs have a deeper chain).
                entry['depth'] = max(entry['depth'], snap.get('depth', 0))
                ct = snap.get('creation_time')
                if ct and (not entry.get('creation_time')
                           or ct > entry['creation_time']):
                    entry['creation_time'] = ct
                # Description and parent: first-write-wins; usually
                # consistent across VMs for a given snapshot name.
                if not entry.get('description'):
                    entry['description'] = snap.get('description', '')
                if not entry.get('parent'):
                    entry['parent'] = snap.get('parent')
                if current == name:
                    entry['current_for'].append(short_vm)

        # 3. Sort: newest-first by depth desc, then creation_time desc.
        rows = sorted(
            aggregated.values(),
            key=lambda r: (r['depth'], r.get('creation_time') or ''),
            reverse=True,
        )

        max_count = getattr(cli_args, 'max_count', None)
        if max_count is not None and max_count >= 0:
            rows = rows[:max_count]

        if getattr(cli_args, 'reverse', False):
            rows = list(reversed(rows))

        # 4. Render.
        if getattr(cli_args, 'as_json', False):
            payload = [
                {
                    'name': r['name'],
                    'description': r['description'],
                    'creation_time': r.get('creation_time'),
                    'parent': r.get('parent'),
                    'depth': r['depth'],
                    'vms': sorted(r['vms']),
                    'current_for': sorted(r['current_for']),
                }
                for r in rows
            ]
            print(json.dumps(payload, indent=2))
            return

        if getattr(cli_args, 'no_graph', False):
            entries: list[tuple[str, dict | None]] = [('', r) for r in rows]
        else:
            entries = render_graph(rows)

        # Column widths (only over real rows — transitions skip the columns).
        real = [r for _, r in entries if r is not None]
        name_w = max((len(r['name']) for r in real), default=4)
        time_w = max((len(r.get('creation_time') or '') for r in real),
                     default=10)

        for prefix, row in entries:
            if row is None:
                print(prefix)
                continue
            current_for = row['current_for']
            vms_total = len(row['vms'])
            cur_marker = ''
            if current_for:
                if len(current_for) == vms_total:
                    cur_marker = '  ← current'
                else:
                    cur_marker = f"  ← current ({len(current_for)}/{vms_total})"
            vm_list = ','.join(sorted(row['vms']))
            description = row.get('description') or ''
            ctime = row.get('creation_time') or '?'
            print(
                f"{prefix}{row['name']:<{name_w}}  "
                f"{ctime:<{time_w}}  "
                f"\"{description}\"  "
                f"[{vm_list}]{cur_marker}"
            )

    def _select_vm_targets(self, cli_args):
        """
        Resolve the VMs selected by the ``--cluster`` / ``--vms`` flags.

        Returns a list of ``(full_vm_name, cluster_name, vm_name, workdir)``
        tuples, filtered by:

        * ``--cluster <name>`` — restrict to a single cluster (raises
          :class:`ValueError` if the cluster is unknown);
        * ``--vms <csv>`` — restrict to specific VMs, each matched against
          either the bare VM name (``node01``) or the cluster-qualified short
          name (``cluster_1_node01``). The default ``'all'`` selects every VM.

        Both filters compose: ``--cluster cluster_2 --vms node01`` selects
        only ``cluster_2``'s ``node01``. With neither flag the result is every
        VM in every cluster — preserving the previous whole-project behaviour.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'

        cluster_filter = getattr(cli_args, 'cluster', None)
        vms_raw = getattr(cli_args, 'vms', None)
        if vms_raw is None:
            vms_raw = 'all'

        vm_filter: set | None = None
        if isinstance(vms_raw, (list, tuple)):
            vm_filter = {str(v).strip() for v in vms_raw if str(v).strip()}
        elif str(vms_raw).strip().lower() != 'all':
            vm_filter = {v.strip() for v in str(vms_raw).split(',') if v.strip()}
        if vm_filter is not None and not vm_filter:
            vm_filter = None

        clusters = self.config['clusters']
        if cluster_filter is not None and cluster_filter not in clusters:
            raise ValueError(
                f"cluster '{cluster_filter}' not found in config "
                f"(available: {', '.join(clusters) or '(none)'})"
            )

        targets = []
        for cluster_name, cluster in clusters.items():
            if cluster_filter is not None and cluster_name != cluster_filter:
                continue
            if self._is_compose_cluster(cluster_name):
                # docker-compose clusters carry ``boxes:``, not ``vms:`` —
                # they are selected via ``_select_dc_clusters`` instead.
                continue
            workdir = os.path.expanduser(cluster['workdir'])
            for vm_name in cluster.get('vms', {}):
                short_name = f"{cluster_name}_{vm_name}"
                if vm_filter is not None and not (
                    vm_name in vm_filter or short_name in vm_filter
                ):
                    continue
                full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                targets.append((full_vm_name, cluster_name, vm_name, workdir))
        return targets

    def snapshot_take(self, cli_args):
        """
        Take a snapshot of the selected VMs (parallel), then verify each one.
        docker-compose clusters are snapshotted per-cluster via ``docker
        commit`` (decision D3; named volumes are NOT captured).

        Honours ``--cluster`` / ``--vms`` so a single cluster (or VM) can be
        snapshotted independently in a multi-cluster project.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        # docker-compose clusters first (cluster-scoped, D3). Failures are
        # isolated per cluster so a dc cluster that isn't up can't stop the
        # VMs of a mixed project from being snapshotted.
        dc_done, dc_failed = self._for_each_dc_cluster(
            cli_args, 'take',
            lambda cname, cluster: self.session_for_cluster(cname).snapshot_take_cluster(
                cname, cluster, cli_args.snapshot_name,
                getattr(cli_args, 'snapshot_descr', '') or ''))

        vm_targets = [
            (full_vm_name, workdir)
            for full_vm_name, _cluster_name, _vm_name, workdir
            in self._select_vm_targets(cli_args)
        ]
        if not vm_targets:
            if not dc_done:
                self.logger.warning(
                    "no VMs or containers matched the given "
                    "--cluster/--vms selection")
            self._exit_if_dc_failed(dc_failed, 'take')
            return

        compress_memory = getattr(cli_args, 'compress_memory', False)
        compress_level = getattr(cli_args, 'memory_compress_level', 3)
        force = getattr(cli_args, 'force', False)

        def _take(full_vm_name, vm_dir, snapshot_name, description):
            self.session_for_vm(full_vm_name).snapshot_take(
                vm_name=full_vm_name,
                vm_dir=vm_dir,
                snapshot_name=snapshot_name,
                description=description,
                compress_memory=compress_memory,
                compress_level=compress_level,
                force=force)

        processes = [
            (full_vm_name, _take,
             (full_vm_name, vm_dir,
              cli_args.snapshot_name, cli_args.snapshot_descr))
            for full_vm_name, vm_dir in vm_targets
        ]
        self._run_parallel(processes, op_label='snapshot take')

        # Verify every snapshot in the main process after all takes complete.
        self.logger.info("verifying snapshots after take...")
        all_ok = True
        for full_vm_name, _ in vm_targets:
            valid, errors = self.session_for_vm(full_vm_name).validate_snapshot(
                full_vm_name, cli_args.snapshot_name)
            if valid:
                self.logger.info(f"snapshot ok: {full_vm_name} / '{cli_args.snapshot_name}'")
            else:
                all_ok = False
                for err in errors:
                    self.logger.error(
                        f"snapshot invalid: {full_vm_name} / '{cli_args.snapshot_name}': {err}")

        if all_ok:
            self.logger.info("all snapshots verified successfully")
        else:
            raise SnapshotError(
                "one or more snapshots failed verification — check errors above")
        self._exit_if_dc_failed(dc_failed, 'take')

    def snapshot_restore(self, cli_args):
        """
        Restore the state of the selected VMs from a snapshot (parallel).

        Honours ``--cluster`` / ``--vms`` so a single cluster (or VM) can be
        rolled back independently in a multi-cluster project.

        Workflow
        --------
        1. Resolve snapshot names in the main process (use latest if not specified).
        2. Pre-validate ALL resolved snapshots; abort if any are invalid.
        3. Run parallel restores, tracking per-VM success via a Queue.
        4. Retry failed VMs in subsequent rounds until ALL succeed.

        Both provider types are resolved and validated **before either is
        mutated**: a docker-compose restore is a destructive
        ``up --force-recreate``, so running it ahead of the VM pre-validation
        would let an invalid VM snapshot abort the command with the containers
        already recreated — a partial restore, despite the pre-validation
        contract above.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        # ── 0. Resolve + validate docker-compose clusters (cluster-scoped,
        #      D3). Nothing is mutated here — the recreate happens in step 3
        #      once the VM snapshots have been validated too.
        dc_plan = []      # [(cluster_name, cluster_cfg, resolved_snapshot)]
        dc_selected = False
        dc_abort = False
        for cname, cluster in self._select_dc_clusters(cli_args):
            dc_selected = True
            session = self.session_for_cluster(cname)
            snap = session.snapshot_resolve_cluster(
                cname, cluster, cli_args.snapshot_name)
            if snap is None:
                self.logger.error(f"[{cname}] no snapshots to restore")
                continue
            if not cli_args.snapshot_name:
                self.logger.info(f"[{cname}] resolved latest snapshot: '{snap}'")
            valid, errors = session.validate_snapshot_cluster(cname, cluster, snap)
            if valid:
                self.logger.info(f"snapshot ok: [{cname}] / '{snap}'")
                dc_plan.append((cname, cluster, snap))
            else:
                dc_abort = True
                for err in errors:
                    self.logger.error(
                        f"snapshot invalid: [{cname}] / '{snap}': {err}")

        selected = self._select_vm_targets(cli_args)
        if not selected and not dc_plan:
            if not dc_selected:
                self.logger.warning(
                    "no VMs or containers matched the given "
                    "--cluster/--vms selection")
            if dc_abort:
                self.logger.error(
                    "aborting restore — one or more snapshots have errors "
                    "(see above)")
            return

        if not selected:
            # containers only: nothing to pre-validate on the libvirt side
            if dc_abort:
                self.logger.error(
                    "aborting restore — one or more snapshots have errors "
                    "(see above)")
                return
            self._exit_if_dc_failed(self._restore_dc_plan(dc_plan), 'restore')
            return

        # ── 1. Resolve snapshot names ────────────────────────────────────────
        vm_targets = []  # list of (full_vm_name, resolved_snapshot_name)
        for full_vm_name, _cluster_name, _vm_name, _workdir in selected:
            snap_name = cli_args.snapshot_name
            if not snap_name:
                snap_name = self.session_for_vm(full_vm_name).get_latest_snapshot(full_vm_name)
                if snap_name is None:
                    raise SnapshotError(
                        f"no snapshot found for {full_vm_name}, aborting restore")
                self.logger.info(
                    f"resolved latest snapshot for {full_vm_name}: '{snap_name}'")
            vm_targets.append((full_vm_name, snap_name))

        # ── 2. Pre-validate all snapshots ────────────────────────────────────
        self.logger.info("pre-validating snapshots before restore...")
        abort = dc_abort   # a bad container snapshot aborts the whole restore
        for full_vm_name, snap_name in vm_targets:
            valid, errors = self.session_for_vm(full_vm_name).validate_snapshot(full_vm_name, snap_name)
            if valid:
                self.logger.info(f"snapshot ok: {full_vm_name} / '{snap_name}'")
            else:
                abort = True
                for err in errors:
                    self.logger.error(
                        f"snapshot invalid: {full_vm_name} / '{snap_name}': {err}")

        if abort:
            raise SnapshotError(
                "aborting restore — one or more snapshots have errors (see above)")

        # ── 3. Everything validated: mutate. Containers first (fast, coarse),
        #      then the parallel VM restores below. A dc failure is reported
        #      now but only exits after the VMs have had their turn.
        dc_failed = self._restore_dc_plan(dc_plan)

        # ── 3 & 4. Parallel restore with retry until all succeed ─────────────
        def _restore(full_vm_name, snapshot_name):
            return bool(self.session_for_vm(full_vm_name).snapshot_restore(
                full_vm_name, snapshot_name))

        pending = list(vm_targets)
        max_rounds = 20

        for round_num in range(1, max_rounds + 1):
            self.logger.info(
                f"restore round {round_num}: {len(pending)} VM(s) to restore")

            # _run_parallel reports raised/killed workers as failures too, so
            # a dying child can no longer look like a successful restore.
            results, failures = self._run_parallel(
                [(vm, _restore, (vm, snap)) for vm, snap in pending],
                op_label='snapshot restore')

            failed = []
            for vm, snap in pending:
                if vm not in failures and results.get(vm):
                    self.logger.info(f"restored: {vm} to '{snap}'")
                else:
                    self.logger.warning(f"failed: {vm} to '{snap}', will retry")
                    failed.append((vm, snap))

            if not failed:
                self.logger.info("all VMs restored successfully")
                self._exit_if_dc_failed(dc_failed, 'restore')
                return

            pending = failed
            if round_num < max_rounds:
                self.logger.info(f"{len(failed)} VM(s) failed, retrying in 3s...")
                time.sleep(3)

        self.logger.error(
            f"restore gave up after {max_rounds} rounds. "
            f"still failing: {[vm for vm, _ in pending]}")
        self._exit_if_dc_failed(dc_failed, 'restore')

    def snapshot_delete(self, cli_args):
        """
        Delete a snapshot of the selected VMs.

        Honours ``--cluster`` / ``--vms`` so a snapshot can be removed from a
        single cluster (or VM) in a multi-cluster project.
        """
        # Ensure provider configs reflect runtime settings
        self._update_sessions_with_runtime()

        if not cli_args.snapshot_name:
            self.logger.error("error: Snapshot name is required")
            return

        # docker-compose clusters (cluster-scoped, D3), failures isolated so
        # one cluster can't strand the others or the VMs.
        dc_done, dc_failed = self._for_each_dc_cluster(
            cli_args, 'delete',
            lambda cname, cluster: self.session_for_cluster(cname).snapshot_delete_cluster(
                cname, cluster, cli_args.snapshot_name))

        targets = self._select_vm_targets(cli_args)
        if not targets:
            if not dc_done:
                self.logger.warning(
                    "no VMs or containers matched the given "
                    "--cluster/--vms selection")
            self._exit_if_dc_failed(dc_failed, 'delete')
            return

        for full_vm_name, _cluster_name, _vm_name, _workdir in targets:
            self.session_for_cluster(_cluster_name).snapshot_delete(full_vm_name, cli_args.snapshot_name)
            self.logger.info(f"Snapshot {cli_args.snapshot_name} deleted for VM {full_vm_name}")
        self._exit_if_dc_failed(dc_failed, 'delete')

    @staticmethod
    def _collapse_one_vm(provider_config, full_vm_name, workdir, vm_info,
                         target, no_shutdown, dry_run):
        """Worker target for parallel snapshot collapse — must be picklable."""
        from boxman.providers.libvirt.snapshot import SnapshotManager
        from boxman.providers.libvirt.storage import StorageManager

        snapshot_mgr = SnapshotManager(provider_config)
        storage = StorageManager(provider_config)

        if dry_run:
            snapshot_mgr.collapse_to(full_vm_name, target, dry_run=True)
            return

        was_running = storage.is_running(full_vm_name)
        if was_running:
            if no_shutdown:
                log.error(
                    f"collapse: vm {full_vm_name} is running and "
                    f"--no-shutdown was passed; skipping")
                return
            if not storage.shutdown_and_wait(full_vm_name):
                log.error(
                    f"collapse: shutdown failed for {full_vm_name}, skipping")
                return

        ok = snapshot_mgr.collapse_to(full_vm_name, target, dry_run=False)

        if was_running:
            if not storage.start(full_vm_name):
                log.error(f"collapse: failed to restart {full_vm_name}")

        if ok:
            log.info(
                f"collapse ok: {full_vm_name} — kept '{target}' and older")
        else:
            log.error(f"collapse failed: {full_vm_name}")

    def snapshot_collapse(self, cli_args):
        """
        Collapse snapshots newer than ``--to`` into the live head per VM.

        Auto-shuts down running VMs by default (qemu-img rebase is
        offline-only). Use ``--no-shutdown`` to skip running VMs instead.
        Snapshots older than the target remain revertable; everything
        between target and head is merged into head and dropped.
        """
        target = cli_args.target
        dry_run = getattr(cli_args, 'dry_run', False)
        no_shutdown = getattr(cli_args, 'no_shutdown', False)
        yes = getattr(cli_args, 'yes', False)

        targets = []
        for full_vm_name, cluster_name, vm_name, workdir in (
                self._select_vm_targets(cli_args)):
            vm_info = self.config['clusters'][cluster_name]['vms'][vm_name] or {}
            targets.append((full_vm_name, workdir, vm_info))

        if not yes and not dry_run:
            self.logger.warning(
                f"about to collapse all snapshots newer than '{target}' "
                f"on {len(targets)} vm(s). This is irreversible — run "
                f"with --dry-run first if unsure, or pass --yes to skip "
                f"this prompt.")
            try:
                confirm = input("continue? [y/N]: ").strip().lower()
            except EOFError:
                confirm = ''
            if confirm != 'y':
                self.logger.info("aborted")
                return

        # Phase 1 (#49): snapshot collapse stays on the default session —
        # it manipulates qcow2 chains via libvirt-specific managers.
        provider_config = self.provider.provider_config
        processes = [
            (full_vm_name, SnapshotsMixin._collapse_one_vm,
             (provider_config, full_vm_name, workdir, vm_info,
              target, no_shutdown, dry_run))
            for full_vm_name, workdir, vm_info in targets
        ]
        self._run_parallel(processes, op_label='snapshot collapse')

    ### end snapshot functions ####
    ### start storage functions ####
    @staticmethod
    def _format_bytes(num: int | None) -> str:
        if num is None:
            return "-"
        for unit in ("B", "K", "M", "G", "T"):
            if abs(num) < 1024.0:
                return f"{num:.1f}{unit}"
            num /= 1024.0
        return f"{num:.1f}P"

    def storage_df(self, cli_args):
        """
        Per-VM disk usage table: virtual size, allocated, chain depth,
        snapshots, snapshot memory (.raw) total, estimated reclaim.
        """
        from boxman.providers.libvirt.storage import vm_disk_paths

        storage = self.provider.storage  # Phase 1 (#49): storage_df stays on the default session until Phase 3
        rows = []
        snap_mem_total_per_vm: dict[str, int] = {}
        for full_vm_name, cluster_name, vm_name, workdir in (
                self._select_vm_targets(cli_args)):
            vm_info = self.config['clusters'][cluster_name]['vms'][vm_name] or {}
            disks = vm_disk_paths(workdir, full_vm_name, vm_info)
            snap_count = storage.count_snapshots(full_vm_name)
            mem_files = storage.snapshot_memory_files(workdir, full_vm_name)
            mem_total = sum(os.path.getsize(p) for p in mem_files if os.path.isfile(p))
            snap_mem_total_per_vm[full_vm_name] = mem_total
            for disk_path in disks:
                if not os.path.isfile(disk_path):
                    continue
                info = storage.disk_info(disk_path)
                chain = storage.disk_chain(disk_path)
                measure = storage.disk_measure(disk_path)
                disk_size = info.get('actual-size')
                virtual = info.get('virtual-size')
                required = measure.get('required')
                reclaim_est = (disk_size - required
                               if disk_size is not None and required is not None
                               else None)
                rows.append({
                    'vm': full_vm_name,
                    'disk': os.path.basename(disk_path),
                    'virtual': virtual,
                    'allocated': disk_size,
                    'chain': len(chain),
                    'snapshots': snap_count,
                    'snap_mem': mem_total,
                    'reclaim_est': reclaim_est,
                })

        # render
        header = (f"{'VM':<48}{'DISK':<28}{'VIRTUAL':>10}{'ALLOC':>10}"
                  f"{'CHAIN':>6}{'SNAPS':>7}{'SNAPMEM':>10}{'RECLAIM~':>10}")
        self.logger.info(header)
        self.logger.info("-" * len(header))
        for row in rows:
            line = (
                f"{row['vm']:<48}"
                f"{row['disk']:<28}"
                f"{self._format_bytes(row['virtual']):>10}"
                f"{self._format_bytes(row['allocated']):>10}"
                f"{row['chain']:>6}"
                f"{row['snapshots']:>7}"
                f"{self._format_bytes(row['snap_mem']):>10}"
                f"{self._format_bytes(row['reclaim_est']):>10}"
            )
            self.logger.info(line)
        if not rows:
            self.logger.info("(no qcow2 disks found on host)")

    def storage_trim(self, cli_args):
        """
        Run ``virsh domfstrim`` (qemu-guest-agent) on every running VM.
        Warns when a VM's disks lack ``discard='unmap'`` — fstrim will succeed
        but nothing will be returned to the host.
        """
        storage = self.provider.storage  # Phase 1 (#49): storage_trim stays on the default session until Phase 3
        for full_vm_name, _c, _v, _workdir in self._select_vm_targets(cli_args):
            if not storage.is_running(full_vm_name):
                self.logger.warning(
                    f"skip trim: vm {full_vm_name} is not running")
                continue
            if not storage.has_discard_unmap(full_vm_name):
                self.logger.warning(
                    f"vm {full_vm_name}: no discard='unmap' on disks — fstrim "
                    f"will not reclaim host space. fix: edit the domain XML "
                    f"(`virsh edit {full_vm_name}`) or recreate via "
                    f"`boxman destroy && boxman up`.")
            if getattr(cli_args, 'dry_run', False):
                self.logger.info(f"[dry-run] would fstrim: {full_vm_name}")
                continue
            storage.fstrim_guest(full_vm_name)

    @staticmethod
    def _compact_one_vm(provider_config, full_vm_name, workdir, vm_info,
                        method, drop_snapshots, no_shutdown, dry_run):
        """Worker target for parallel compact — must be picklable."""
        from boxman.providers.libvirt.storage import StorageManager, vm_disk_paths

        storage = StorageManager(provider_config)
        disks = [p for p in vm_disk_paths(workdir, full_vm_name, vm_info)
                 if os.path.isfile(p)]
        if not disks:
            log.info(f"compact: no disks found for {full_vm_name}, skipping")
            return

        was_running = storage.is_running(full_vm_name)
        if was_running:
            if no_shutdown:
                log.error(
                    f"compact: vm {full_vm_name} is running and --no-shutdown "
                    f"was passed; skipping")
                return
            if dry_run:
                log.info(f"[dry-run] would shutdown {full_vm_name}")
            else:
                if not storage.shutdown_and_wait(full_vm_name):
                    log.error(f"compact: shutdown failed for {full_vm_name}, skipping")
                    return

        has_snapshots = storage.count_snapshots(full_vm_name) > 0
        for disk_path in disks:
            before = storage.disk_info(disk_path).get('actual-size', 0)
            if dry_run:
                measure = storage.disk_measure(disk_path)
                est = measure.get('required')
                log.info(
                    f"[dry-run] {full_vm_name}: would compact {os.path.basename(disk_path)} "
                    f"method={method} allocated={before} estimated_after={est}")
                continue
            ok = storage.compact_disk(
                disk_path,
                method=method,
                has_snapshots=has_snapshots,
                drop_snapshots=drop_snapshots)
            after = storage.disk_info(disk_path).get('actual-size', 0)
            if ok:
                log.info(
                    f"compact ok: {full_vm_name}/{os.path.basename(disk_path)} "
                    f"{before} -> {after}")
            else:
                log.error(
                    f"compact failed: {full_vm_name}/{os.path.basename(disk_path)}")

        if was_running and not no_shutdown and not dry_run:
            if not storage.start(full_vm_name):
                log.error(f"compact: failed to restart {full_vm_name}")

    def storage_compact(self, cli_args):
        """
        Compact every VM's qcow2 file(s). Auto-shuts down running VMs by
        default (use ``--no-shutdown`` to skip running VMs instead). Refuses
        chain-flattening methods when snapshots exist unless
        ``--drop-snapshots`` is passed.
        """
        targets = []
        for full_vm_name, cluster_name, vm_name, workdir in (
                self._select_vm_targets(cli_args)):
            vm_info = self.config['clusters'][cluster_name]['vms'][vm_name] or {}
            targets.append((full_vm_name, workdir, vm_info))
        method = getattr(cli_args, 'method', 'auto')
        drop_snapshots = getattr(cli_args, 'drop_snapshots', False)
        no_shutdown = getattr(cli_args, 'no_shutdown', False)
        dry_run = getattr(cli_args, 'dry_run', False)

        provider_config = self.provider.provider_config  # Phase 1 (#49): storage_compact stays on the default session until Phase 3
        processes = [
            (full_vm_name, SnapshotsMixin._compact_one_vm,
             (provider_config, full_vm_name, workdir, vm_info,
              method, drop_snapshots, no_shutdown, dry_run))
            for full_vm_name, workdir, vm_info in targets
        ]
        self._run_parallel(processes, op_label='storage compact')

    def storage_optimize(self, cli_args):
        """
        Trim every running VM (via guest agent) and compact every VM's
        qcow2 file(s). Auto-shutdown semantics from ``storage_compact`` apply.
        """
        if not getattr(cli_args, 'skip_trim', False):
            self.logger.info("storage optimize: phase 1 — trim (guest fstrim)")
            self.storage_trim(cli_args)
        else:
            self.logger.info("storage optimize: skipping trim phase (--skip-trim)")

        if not getattr(cli_args, 'skip_compact', False):
            self.logger.info("storage optimize: phase 2 — compact (host qcow2)")
            self.storage_compact(cli_args)
        else:
            self.logger.info("storage optimize: skipping compact phase (--skip-compact)")

    def storage_compress_snapshots(self, cli_args):
        """
        zstd-compress every snapshot's memory ``.raw`` file (or decompress
        with ``--decompress``). Use this retroactively on snapshots that
        were taken without ``--compress-memory``.
        """
        decompress = getattr(cli_args, 'decompress', False)
        level = getattr(cli_args, 'level', 3)
        action = "decompress" if decompress else "compress"
        for full_vm_name, _c, _v, _workdir in self._select_vm_targets(cli_args):
            self.logger.info(f"storage {action}-snapshots: {full_vm_name}")
            processed, total = self.session_for_vm(full_vm_name).compress_snapshots_memory(
                full_vm_name, level=level, decompress=decompress)
            self.logger.info(
                f"  {action}ed {processed}/{total} snapshot memory file(s) "
                f"for {full_vm_name}")
