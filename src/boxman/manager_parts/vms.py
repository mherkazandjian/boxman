"""VM lifecycle and update flows for BoxmanManager."""

import contextlib
import logging
import os
import time
from multiprocessing import Process, Queue
from typing import Any

from boxman import log
from boxman.exceptions import (
    CloneSanitizerError,
    ConfigError,
    ProvisionError,
)
from boxman.loggers.logger import suppressed
from boxman.manager_parts.images import ImagesMixin
from boxman.providers.libvirt.clone_vm import CLONE_DEGRADATION_NOTICES_KEY
from boxman.providers.libvirt.commands import VirshCommand
from boxman.providers.libvirt.virsh_parse import parse_domblklist


def _clone_with_retry(provider, cluster, vm_info, new_vm_name,
                      max_retries: int = 5) -> None:
    """
    Clone one VM, retrying transient (e.g. storage-pool-busy) failures.

    Module-level — not a method or closure — so it stays picklable as a
    ``multiprocessing.Process`` target. Shared by
    :meth:`BoxmanManager.clone_vms` and
    :meth:`BoxmanManager._clone_and_configure_new_vms`.
    """
    for attempt in range(1, max_retries + 1):
        last_attempt = attempt == max_retries
        degradation_notices: list[str] = []
        attempt_info = vm_info.copy()
        attempt_info[CLONE_DEGRADATION_NOTICES_KEY] = degradation_notices
        # Suppress error-level logs on all retryable attempts so that
        # transient pool-busy failures don't appear as errors; only the
        # final attempt logs errors normally. suppressed() restores the
        # prior level (not a hardcoded DEBUG) so -v/-vv survives retries.
        _cm = (contextlib.nullcontext() if last_attempt
               else suppressed(logging.CRITICAL))
        try:
            with _cm:
                src_vm_name = vm_info.get('base_image') or cluster.get('base_image')
                if not src_vm_name and not ImagesMixin._is_diskless_boot(vm_info):
                    raise ValueError(
                        f"no base_image for VM '{new_vm_name}': "
                        f"set base_image at the cluster or VM level"
                    )
                provider.clone_vm(
                    src_vm_name=src_vm_name,
                    new_vm_name=new_vm_name,
                    info=attempt_info,
                    workdir=cluster['workdir']
                )
            # A successful auto-policy clone never reaches a later,
            # unsuppressed retry. Re-emit only its degradation notice after
            # leaving the suppression context so duplicate identity is never
            # silent while transient attempt noise remains hidden.
            for notice in degradation_notices:
                log.warning(notice)
            return
        except (CloneSanitizerError, ConfigError):
            # Required sanitizer and invalid-policy failures are permanent.
            # Retrying would either repeat the same inspection or run into an
            # unsafe clone whose cleanup already failed.
            raise
        except Exception:
            if not last_attempt:
                delay = attempt * 2
                log.warning(
                    f"clone {new_vm_name} failed (attempt {attempt}/{max_retries}), "
                    f"retrying in {delay}s"
                )
                time.sleep(delay)
            else:
                raise

class VMsMixin:

    ### end networks define / remove / destroy
    ### vms define / remove / destroy
    def _ensure_libvirt_storage_pool(self, workdir: str, cluster_name: str) -> None:
        """
        Ensure the libvirt directory storage pool exists for the given workdir.

        virt-clone automatically tries to define a pool for the target directory
        when given a --file path. When multiple VMs are cloned in parallel into
        the same directory, every process races to define the same pool and all
        but the first fail with "pool already exists". Pre-defining the pool
        here (sequentially, before parallel cloning begins) eliminates the race.

        Uses *cluster_name*'s own libvirt session for the virsh connection so a
        compose-primary mixed project (where the default ``self.provider`` is
        the docker-compose session) still targets the configured libvirt
        URI/runtime instead of silently falling back to local qemu:///system.
        """
        workdir = os.path.abspath(os.path.expanduser(workdir))
        pool_name = os.path.basename(workdir)
        virsh = VirshCommand(self.session_for_cluster(cluster_name).provider_config)

        result = virsh.execute("pool-info", pool_name, warn=True)
        if result.ok:
            self.logger.info(f"storage pool '{pool_name}' already exists")
            return

        self.logger.info(f"defining storage pool '{pool_name}' for {workdir}")
        virsh.execute("pool-define-as", pool_name, "dir", "--target", workdir)
        virsh.execute("pool-build", pool_name, warn=True)
        virsh.execute("pool-start", pool_name, warn=True)

    def clone_vms(self) -> None:
        """
        Clone the VMs defined in the configuration.

        The following is done for every vm in every cluster

            - remove the vm
            - clone the vm
        """
        def vm_clone_tasks():
            prj_name = f'bprj__{self.config["project"]}__bprj'
            for cluster_name, cluster in self._vm_clusters.items():
                for vm_name, vm_info in cluster['vms'].items():
                    vm_info = vm_info.copy()
                    new_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
                    yield cluster, vm_info, new_vm_name

        # Pre-define storage pools for all cluster workdirs before parallel
        # cloning. virt-clone tries to auto-define a pool for the target
        # directory; doing it here sequentially prevents the race condition
        # where N parallel processes all try to define the same pool at once.
        seen_workdirs: set = set()
        for cluster_name, cluster in self._vm_clusters.items():
            workdir = os.path.abspath(os.path.expanduser(cluster['workdir']))
            if workdir not in seen_workdirs:
                seen_workdirs.add(workdir)
                self._ensure_libvirt_storage_pool(workdir, cluster_name)

        # resolve isos/cdroms/networks in place so both the clone subprocesses
        # and the later configure/start step see the resolved values
        self._resolve_iso_config()
        clone_tasks = [
            (cluster, vm_info, new_vm_name)
            for cluster, vm_info, new_vm_name in vm_clone_tasks()
        ]
        processes = [
            Process(target=_clone_with_retry,
                    args=(self.provider, cluster, vm_info, new_vm_name))
            for cluster, vm_info, new_vm_name in clone_tasks
        ]
        [p.start() for p in processes]
        [p.join() for p in processes]

        # Abort provision if any clone subprocess exited non-zero. Without
        # this check, the subsequent configure / start / wait-for-IP steps
        # all spam errors against VMs that were never defined, and the
        # wait-for-IP loop in particular looks like a hang.
        failed = [
            (task[2], p.exitcode)
            for task, p in zip(clone_tasks, processes, strict=False)
            if p.exitcode != 0
        ]
        if failed:
            names = ', '.join(name for name, _ in failed)
            raise RuntimeError(
                f"clone failed for {len(failed)} VM(s) ({names}); aborting "
                f"provision. See the preceding clone or guest-sanitizer log "
                f"for the underlying cause and remediation.")

    ### end vms define / remove / destroy
    def _configure_and_start_vm(
        self, cluster_name: str, cluster: dict[str, Any], vm_name: str, vm_info: dict[str, Any]
    ) -> None:
        """
        Configure and start a single VM: cpu/mem, network interfaces, disks, then start.

        Designed to be called in a separate process per VM after cloning is done.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
        vm_info = vm_info.copy()

        # cpu / memory
        cpus = vm_info.get('cpus')
        memory = vm_info.get('memory')
        max_vcpus = vm_info.get('max_vcpus')
        max_memory = vm_info.get('max_memory')
        if cpus or memory:
            self.logger.info(f"configuring cpu and memory for vm {vm_name}")
            success = self.session_for_cluster(cluster_name).configure_vm_cpu_memory(
                vm_name=full_vm_name, cpus=cpus, memory_mb=memory,
                max_vcpus=max_vcpus, max_memory_mb=max_memory
            )
            if success:
                self.logger.info(f"successfully configured cpu and memory for vm {vm_name}")
            else:
                self.logger.warning(f"failed to configure cpu and memory for vm {vm_name}")
        else:
            self.logger.warning(f"no cpu or memory configuration for vm {vm_name}, skipping")

        # memballoon (virtio-balloon: free-page reporting, autodeflate, stats)
        memballoon = vm_info.get('memballoon')
        if memballoon:
            self.logger.info(f"configuring memballoon for vm {vm_name}")
            success = self.session_for_cluster(cluster_name).configure_vm_memballoon(
                vm_name=full_vm_name, memballoon=memballoon
            )
            if success:
                self.logger.info(f"successfully configured memballoon for vm {vm_name}")
            else:
                self.logger.warning(f"failed to configure memballoon for vm {vm_name}")

        # network interfaces
        if 'network_adapters' not in vm_info:
            self.logger.warning(f"no network adapters defined for vm {vm_name}, skipping")
        else:
            for adapter in vm_info['network_adapters']:
                self.resolve_adapter_network(adapter, cluster_name)

            self.logger.info(f"configuring network interfaces for vm {vm_name}")
            success = self.session_for_cluster(cluster_name).configure_vm_network_interfaces(
                vm_name=full_vm_name,
                network_adapters=vm_info['network_adapters']
            )
            if success:
                self.logger.info(f"network interfaces configured for vm {vm_name}")
            else:
                self.logger.warning(f"some network interfaces could not be configured for vm {vm_name}")

        # disks
        workdir = cluster.get('workdir', '.')
        if 'disks' not in vm_info or not vm_info['disks']:
            self.logger.warning(f"no disks defined for vm {vm_name}, skipping")
        else:
            self.logger.info(f"configuring disks for vm {vm_name}")
            success = self.session_for_cluster(cluster_name).configure_vm_disks(
                vm_name=full_vm_name,
                disks=vm_info['disks'],
                workdir=workdir,
                disk_prefix=full_vm_name
            )
            if success:
                self.logger.info(f"all disks configured for vm {vm_name}")
            else:
                self.logger.warning(f"some disks could not be configured for vm {vm_name}")

        # shared folders (must be before start for virtiofs memfd backing)
        if vm_info.get('shared_folders'):
            self.logger.info(f"configuring shared folders for vm {vm_name}")
            success = self.session_for_cluster(cluster_name).configure_vm_shared_folders(
                vm_name=full_vm_name,
                shared_folders=vm_info['shared_folders']
            )
            if success:
                self.logger.info(f"shared folders configured for vm {vm_name}")
            else:
                self.logger.warning(f"some shared folders could not be configured for vm {vm_name}")

        # cdroms — the cdrom-boot ISO is already attached by virt-install at
        # create time, so attach only any *additional* cdroms here (avoids a
        # spurious "missing source" failure and a duplicate attach)
        boot_iso = vm_info.get('_resolved_iso_path')
        extra_cdroms = [
            c for c in (vm_info.get('cdroms') or [])
            if not (isinstance(c, dict) and boot_iso and c.get('source') == boot_iso)
        ]
        if extra_cdroms:
            self.logger.info(f"configuring CDROMs for vm {vm_name}")
            success = self.session_for_cluster(cluster_name).configure_vm_cdroms(
                vm_name=full_vm_name,
                cdroms=extra_cdroms
            )
            if success:
                self.logger.info(f"CDROMs configured for vm {vm_name}")
            else:
                self.logger.warning(f"some CDROMs could not be configured for vm {vm_name}")

        # start
        self.logger.info(f"starting vm {full_vm_name}")
        success = self.session_for_cluster(cluster_name).start_vm(full_vm_name)
        if success:
            self.logger.info(f"successfully started the vm {full_vm_name}")
        else:
            self.logger.warning(f"failed to start the vm {full_vm_name}")

    def _destroy_vm_and_disks(
        self, cluster_name: str, cluster: dict[str, Any], vm_name: str, vm_info: dict[str, Any]
    ) -> None:
        """
        Fully destroy a single VM: stop/undefine, remove disks, force-cleanup.

        Designed to be called in a separate process per VM during deprovision.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
        vm_info = vm_info.copy()

        self.logger.info(f"destroying vm {full_vm_name}")
        session = self.session_for_cluster(cluster_name)
        session.destroy_vm(full_vm_name)
        session.destroy_disks(
            cluster['workdir'],
            vm_name=full_vm_name,
            disks=vm_info.get('disks', [])
        )
        session.destroy_vm(full_vm_name, force=True)

    def _vm_disk_dirs(self, full_vm_name: str) -> list[str]:
        """
        Return the directories that hold *full_vm_name*'s disk files,
        discovered from libvirt itself (``virsh domblklist``).

        Used for VMs that no longer appear in conf.yml, whose workdir
        can therefore not be resolved from the config. Falls back to
        every workdir known to the project config when the domain
        cannot be queried (e.g. already undefined) — the destroy_disks
        glob is anchored at the VM name, so sweeping extra directories
        is harmless.
        """
        virsh = VirshCommand(
            provider_config=self.provider.provider_config)
        result = virsh.execute(
            "domblklist", full_vm_name, "--details", warn=True)
        dirs = set()
        if result.ok:
            for row in parse_domblklist(result.stdout):
                if row.device == 'disk' and row.source not in (None, '-'):
                    dirs.add(os.path.dirname(row.source))
        if not dirs:
            self.logger.warning(
                f"could not query disk paths for {full_vm_name} from "
                f"libvirt; falling back to all configured workdirs")
            dirs.update(self.collect_workdirs())
        return sorted(dirs)

    def _destroy_removed_vm(self, full_vm_name: str) -> None:
        """
        Destroy a VM that has been removed from the config.

        Uses destroy_vm + destroy_disks with an empty disk list since we
        no longer have the disk config for this VM. The disk directories
        are read from libvirt before the domain is undefined (see
        ``_vm_disk_dirs``); the glob-based cleanup in destroy_disks
        catches all {vm_name}* artifacts.
        """
        self.logger.info(f"removing VM {full_vm_name} (no longer in config)")
        # Phase 1 (#49): stays on the default session — the VM is gone
        # from the config, so its cluster (and provider) can no longer be
        # resolved. Revisited in Phase 3 (#51).
        disk_dirs = self._vm_disk_dirs(full_vm_name)
        self.provider.destroy_vm(full_vm_name)
        for workdir in disk_dirs:
            self.provider.destroy_disks(
                workdir,
                vm_name=full_vm_name,
                disks=[]
            )
        self.provider.destroy_vm(full_vm_name, force=True)

    def configure_and_start_vms(self) -> None:
        """
        Configure (cpu/mem, network interfaces, disks) and start all VMs in parallel.

        Each VM is handled in its own process so all VMs are configured and
        started concurrently.
        """
        processes = [
            (f"{cluster_name}/{vm_name}", self._configure_and_start_vm,
             (cluster_name, cluster, vm_name, vm_info))
            for cluster_name, cluster in self._vm_clusters.items()
            for vm_name, vm_info in cluster['vms'].items()
        ]
        self._run_parallel(processes, op_label='configure and start vm')

    def _get_project_vm_names(self) -> list[str]:
        """
        Return the list of fully-qualified VM names that would be
        created by provisioning the current config.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        vm_names = []
        for cluster_name, cluster in self.config.get('clusters', {}).items():
            for vm_name in cluster.get('vms', {}).keys():
                vm_names.append(f"{prj_name}_{cluster_name}_{vm_name}")
        return vm_names

    def _find_existing_project_vms(self) -> list[str]:
        """
        Query libvirt and return the subset of project VM names that
        already exist (in any state).
        """
        expected = self._get_project_vm_names()
        if not expected:
            return []
        if not self._has_libvirt_clusters():
            return []

        result = self._virsh().execute("list", "--all", "--name", hide=True, warn=True)
        if not result.ok:
            self.logger.warning("could not query existing VMs via virsh")
            return []

        existing = {
            v.strip() for v in result.stdout.strip().split("\n") if v.strip()
        }
        return [vm for vm in expected if vm in existing]

    def _find_all_existing_project_vms(self) -> list[str]:
        """
        Query libvirt and return ALL VM names that belong to this project
        (match the project prefix), regardless of whether they appear in
        the current config.
        """
        project = self.config.get("project", "")
        prj_prefix = f"bprj__{project}__bprj_"

        if not self._has_libvirt_clusters():
            return []

        result = self._virsh().execute("list", "--all", "--name", hide=True, warn=True)
        if not result.ok:
            self.logger.warning("could not query existing VMs via virsh")
            return []

        return [
            v.strip() for v in result.stdout.strip().split("\n")
            if v.strip() and v.strip().startswith(prj_prefix)
        ]

    def _get_vm_states(self) -> dict[str, str]:
        """
        Query libvirt and return a mapping of project VM name -> state string
        for all project VMs that exist.

        State strings are as returned by ``virsh list --all``, e.g.
        'running', 'shut off', 'paused', 'saved', etc.

        Returns:
            Dict mapping full VM name to its state, only for VMs that exist.
        """
        expected = set(self._get_project_vm_names())
        if not expected:
            return {}
        if not self._has_libvirt_clusters():
            return {}

        # Use the table output to get both name and state
        result = self._virsh().execute("list", "--all", hide=True, warn=True)
        if not result.ok:
            self.logger.warning("could not query VM states via virsh")
            return {}

        states: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            # Skip header and separator lines
            line = line.strip()
            if not line or line.startswith('---') or line.startswith('Id'):
                continue
            # Format: " Id   Name                 State"
            # e.g.  " -    my-vm                shut off"
            #       " 3    my-vm                running"
            parts = line.split(None, 2)
            if len(parts) >= 3:
                vm_name = parts[1]
                vm_state = parts[2].strip()
                if vm_name in expected:
                    states[vm_name] = vm_state

        return states

    ### end netlab CLI handlers ####
    ### update (runtime modification) functions ####
    def _clone_and_configure_new_vms(self, new_vm_names: set) -> None:
        """
        Clone and configure only the VMs whose full names are in new_vm_names.

        Reuses the same logic as provision (clone + configure + start) but
        filters to just the specified VMs.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'

        # pre-define storage pools for workdirs of new VMs
        seen_workdirs: set = set()
        for cluster_name, cluster in self._vm_clusters.items():
            for vm_name in cluster['vms']:
                full = f"{prj_name}_{cluster_name}_{vm_name}"
                if full in new_vm_names:
                    workdir = os.path.abspath(os.path.expanduser(cluster['workdir']))
                    if workdir not in seen_workdirs:
                        seen_workdirs.add(workdir)
                        self._ensure_libvirt_storage_pool(workdir, cluster_name)

        # clone new VMs (parallel with retry)
        # resolve isos/cdroms/networks in place so both the clone subprocesses
        # and the configure/start step below see the resolved values
        self._resolve_iso_config()
        clone_tasks = []
        for cluster_name, cluster in self._vm_clusters.items():
            for vm_name, vm_info in cluster['vms'].items():
                full = f"{prj_name}_{cluster_name}_{vm_name}"
                if full in new_vm_names:
                    clone_tasks.append((cluster, vm_info, full))

        # Abort the update if any clone worker fails — same guard as
        # :meth:`clone_vms` to prevent downstream configure/start steps
        # from running against VMs that were never defined. _run_parallel
        # reports raised/killed workers as failures, not just non-zero
        # exitcodes.
        _results, failures = self._run_parallel(
            [(new_vm_name, _clone_with_retry,
              (self.provider, cluster, vm_info, new_vm_name))
             for cluster, vm_info, new_vm_name in clone_tasks],
            op_label='clone vm')
        if failures:
            names = ', '.join(sorted(failures))
            raise RuntimeError(
                f"clone failed for {len(failures)} new VM(s) ({names}); "
                f"aborting update. See the preceding clone or "
                f"guest-sanitizer log for the underlying cause and "
                f"remediation.")

        # configure and start new VMs (parallel)
        configure_tasks = []
        for cluster_name, cluster in self._vm_clusters.items():
            for vm_name, vm_info in cluster['vms'].items():
                full = f"{prj_name}_{cluster_name}_{vm_name}"
                if full in new_vm_names:
                    configure_tasks.append((cluster_name, cluster, vm_name, vm_info))

        self._run_parallel(
            [(f"{cluster_name}/{vm_name}", self._configure_and_start_vm,
              (cluster_name, cluster, vm_name, vm_info))
             for cluster_name, cluster, vm_name, vm_info in configure_tasks],
            op_label='configure and start vm')

    def _update_single_vm(
        self,
        cluster_name: str,
        cluster: dict[str, Any],
        vm_name: str,
        vm_info: dict[str, Any],
        result_queue: Queue,
        dry_run: bool = False,
    ) -> None:
        """
        Diff and apply updates to a single existing VM. Runs in its own process.

        Results are put into result_queue as:
            (vm_name, {'status': 'no_change'|'updated'|'needs_restart'|'failed', 'details': str})
        """
        from boxman.providers.libvirt.vm_differ import VMStateDiffer

        prj_name = f'bprj__{self.config["project"]}__bprj'
        full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
        workdir = os.path.abspath(os.path.expanduser(cluster.get('workdir', '.')))

        try:
            differ = VMStateDiffer(provider_config=self.provider.provider_config)
            diff = differ.diff_vm(
                domain_name=full_vm_name,
                desired_cpus=vm_info.get('cpus'),
                desired_memory_mb=vm_info.get('memory'),
                desired_disks=vm_info.get('disks'),
                workdir=workdir,
                disk_prefix=full_vm_name,
                desired_max_vcpus=vm_info.get('max_vcpus'),
                desired_max_memory_mb=vm_info.get('max_memory'),
                desired_shared_folders=vm_info.get('shared_folders'),
                desired_cdroms=vm_info.get('cdroms'),
                desired_memballoon=vm_info.get('memballoon'),
            )

            has_changes = (
                diff['cpu_changed'] or
                diff['memory_changed'] or
                diff['max_vcpus_changed'] or
                diff['max_memory_changed'] or
                diff['new_disks'] or
                diff['resize_disks'] or
                diff['new_cdroms'] or
                diff['removed_cdroms'] or
                diff['changed_cdroms'] or
                diff['new_shared_folders'] or
                diff['removed_shared_folders'] or
                diff['changed_shared_folders'] or
                diff['memballoon_changed'] or
                diff['memballoon_restart_pending']
            )

            if not has_changes:
                self.logger.info(f"VM {vm_name}: no changes detected")
                result_queue.put((vm_name, {'status': 'no_change', 'details': ''}))
                return

            # log the diff
            changes = []
            if diff['cpu_changed']:
                changes.append(
                    f"CPU: {diff['actual_cpus']} -> {diff['desired_cpus']}")
            if diff['memory_changed']:
                changes.append(
                    f"memory: {diff['actual_memory_mb']}M -> {diff['desired_memory_mb']}M")
            if diff['max_vcpus_changed']:
                changes.append(
                    f"max_vcpus: {diff['actual_max_vcpus']} -> {diff['desired_max_vcpus']}")
            if diff['max_memory_changed']:
                changes.append(
                    f"max_memory: {diff['actual_max_memory_mb']}M -> {diff['desired_max_memory_mb']}M")
            if diff['new_disks']:
                names = [d.get('name', '?') for d in diff['new_disks']]
                changes.append(f"new disks: {', '.join(names)}")
            if diff['resize_disks']:
                resizes = [
                    f"{r['target']} {r['current_size_mb']}M->{r['desired_size_mb']}M"
                    for r in diff['resize_disks']
                ]
                changes.append(f"resize disks: {', '.join(resizes)}")
            if diff['new_cdroms']:
                names = [c.get('name', '?') for c in diff['new_cdroms']]
                changes.append(f"new cdroms: {', '.join(names)}")
            if diff['removed_cdroms']:
                targets = [c['target'] for c in diff['removed_cdroms']]
                changes.append(f"remove cdroms: {', '.join(targets)}")
            if diff['changed_cdroms']:
                swaps = [f"{c['target']}->{c['source']}" for c in diff['changed_cdroms']]
                changes.append(f"change cdroms: {', '.join(swaps)}")
            if diff['new_shared_folders']:
                names = [f.get('name', '?') for f in diff['new_shared_folders']]
                changes.append(f"new shared folders: {', '.join(names)}")
            if diff['removed_shared_folders']:
                names = [f['name'] for f in diff['removed_shared_folders']]
                changes.append(f"remove shared folders: {', '.join(names)}")
            if diff['changed_shared_folders']:
                names = [f.get('name', '?') for f in diff['changed_shared_folders']]
                changes.append(f"change shared folders: {', '.join(names)}")
            if diff['memballoon_changed']:
                changes.append(
                    f"memballoon: {diff['actual_memballoon']} -> "
                    f"{diff['desired_memballoon']}")
            elif diff['memballoon_restart_pending']:
                changes.append(
                    f"memballoon live state: {diff['live_memballoon']} -> "
                    f"{diff['desired_memballoon']}")
            self.logger.info(f"VM {vm_name}: changes detected: {'; '.join(changes)}")

            if dry_run:
                result_queue.put((vm_name, {
                    'status': 'dry_run',
                    'details': '; '.join(changes)
                }))
                return

            # apply changes
            vm_running = diff['vm_state'] == 'running'
            restart_needed = False
            pending_restart = diff['memballoon_restart_pending']

            # CPU / memory / max ceilings
            if (diff['cpu_changed'] or diff['memory_changed'] or
                    diff['max_vcpus_changed'] or diff['max_memory_changed']):
                cpu_mem_result = self.provider.update_vm_cpu_memory(
                    vm_name=full_vm_name,
                    cpus=diff['desired_cpus'] if diff['cpu_changed'] else None,
                    memory_mb=diff['desired_memory_mb'] if diff['memory_changed'] else None,
                    vm_state=diff['vm_state'],
                    actual_cpus=diff['actual_cpus'],
                    actual_memory_mb=diff['actual_memory_mb'],
                    max_vcpus=diff.get('desired_max_vcpus'),
                    max_memory_mb=diff.get('desired_max_memory_mb')
                )
                if not cpu_mem_result['success']:
                    result_queue.put((vm_name, {
                        'status': 'failed',
                        'details': 'CPU/memory update failed'
                    }))
                    return
                if cpu_mem_result['restart_needed']:
                    restart_needed = True

            # memballoon (persistent config; the normalized desired state
            # covers both enabling and reconciling back to defaults)
            if diff['memballoon_changed']:
                balloon_ok = self.provider.configure_vm_memballoon(
                    vm_name=full_vm_name, memballoon=diff['desired_memballoon'])
                if not balloon_ok:
                    result_queue.put((vm_name, {
                        'status': 'failed',
                        'details': 'memballoon update failed'
                    }))
                    return
                if pending_restart:
                    self.logger.warning(
                        f"VM {vm_name}: restart required to apply memballoon changes")

            # disks
            if diff['new_disks'] or diff['resize_disks']:
                disk_ok = self.provider.update_vm_disks(
                    vm_name=full_vm_name,
                    new_disks=diff['new_disks'],
                    resize_disks=diff['resize_disks'],
                    workdir=workdir,
                    disk_prefix=full_vm_name,
                    vm_running=vm_running
                )
                if not disk_ok:
                    result_queue.put((vm_name, {
                        'status': 'failed',
                        'details': 'disk update failed'
                    }))
                    return

            # cdroms
            if diff['new_cdroms'] or diff['removed_cdroms'] or diff['changed_cdroms']:
                cdrom_ok = self.provider.update_vm_cdroms(
                    vm_name=full_vm_name,
                    new_cdroms=diff['new_cdroms'],
                    removed_cdroms=diff['removed_cdroms'],
                    changed_cdroms=diff['changed_cdroms'],
                    vm_running=vm_running
                )
                if not cdrom_ok:
                    result_queue.put((vm_name, {
                        'status': 'failed',
                        'details': 'CDROM update failed'
                    }))
                    return

            # shared folders
            if (diff['new_shared_folders'] or diff['removed_shared_folders'] or
                    diff['changed_shared_folders']):
                folder_result = self.provider.update_vm_shared_folders(
                    vm_name=full_vm_name,
                    new_folders=diff['new_shared_folders'],
                    removed_folders=diff['removed_shared_folders'],
                    changed_folders=diff['changed_shared_folders'],
                    vm_running=vm_running
                )
                if not folder_result['success']:
                    result_queue.put((vm_name, {
                        'status': 'failed',
                        'details': 'shared folder update failed'
                    }))
                    return
                if folder_result.get('restart_needed'):
                    restart_needed = True

            # handle restart if needed
            if restart_needed and vm_running:
                self.logger.info(
                    f"VM {vm_name}: restarting to apply changes "
                    f"(live max ceiling cannot be raised)")
                self.provider.shutdown_and_wait(full_vm_name)
                self.provider.start_vm(full_vm_name)
                result_queue.put((vm_name, {
                    'status': 'updated',
                    'details': '; '.join(changes) + ' (restarted)'
                }))
            elif pending_restart:
                result_queue.put((vm_name, {
                    'status': 'needs_restart',
                    'details': (
                        '; '.join(changes) +
                        ' (restart required to apply memballoon changes)')
                }))
            else:
                result_queue.put((vm_name, {
                    'status': 'updated',
                    'details': '; '.join(changes)
                }))

        except Exception as exc:
            self.logger.error(f"VM {vm_name}: update failed: {exc}")
            result_queue.put((vm_name, {
                'status': 'failed',
                'details': str(exc)
            }))

    def update(self, cli_args):
        """
        Apply config changes to already-provisioned VMs.

        Compares the desired state in conf.yml against actual VM state in
        libvirt and applies only the changes needed:
          - New VMs in config are cloned, configured, and started
          - CPU/memory changes are applied (hot if possible, cold otherwise)
          - New disks are created and attached
          - Existing disks are resized (grow only)
          - Removed VMs (in libvirt but no longer in config) are destroyed

        Use --dry-run to preview changes without applying them.
        Use --yes to skip the confirmation prompt for VM removal.
        """
        config = self.config
        dry_run = getattr(cli_args, 'dry_run', False)
        auto_accept = getattr(cli_args, 'yes', False)

        # ensure provider configs reflect runtime settings
        # Phase 1 (#49): the update/diff flow below stays on the default
        # session — it is deeply libvirt-shaped (VMStateDiffer, virsh
        # edits) and only libvirt clusters can exist until Phase 3 (#51).
        self._update_sessions_with_runtime()

        # --- networks first ---
        # a new VM further down may be wired to a network that does not exist
        # yet, and this runs before the early return below so that a change
        # which only touches networks is not silently a no-op
        self.ensure_shared_bridges()
        network_results = self.reconcile_networks(
            dry_run=dry_run,
            allow_recreate=getattr(cli_args, 'recreate_networks', False),
            auto_accept=auto_accept)

        self.report_network_results(network_results)

        self.raise_on_network_failures(network_results)

        # --- categorize VMs ---
        expected_vms = set(self._get_project_vm_names())
        all_existing_vms = set(self._find_all_existing_project_vms())

        new_vm_names = expected_vms - all_existing_vms
        update_vm_names = expected_vms & all_existing_vms
        removed_vm_names = all_existing_vms - expected_vms

        if not new_vm_names and not update_vm_names and not removed_vm_names:
            self.logger.info("no VMs to add, update, or remove")
            return

        # --- summary ---
        if new_vm_names:
            short = [n.split('_')[-1] for n in sorted(new_vm_names)]
            self.logger.info(f"VM(s) to add: {', '.join(short)}")
        if update_vm_names:
            short = [n.split('_')[-1] for n in sorted(update_vm_names)]
            self.logger.info(f"VM(s) to update: {', '.join(short)}")
        if removed_vm_names:
            short = [n.split('_')[-1] for n in sorted(removed_vm_names)]
            self.logger.info(f"VM(s) to remove: {', '.join(short)}")

        # --- confirmation for destructive removal ---
        if removed_vm_names and not dry_run and not auto_accept:
            short = [n.split('_')[-1] for n in sorted(removed_vm_names)]
            print(
                f"\nThe following VM(s) will be permanently destroyed: "
                f"{', '.join(short)}")
            print(
                "This will stop the VM(s), remove their disks, and "
                "clean up all associated resources.\n")
            try:
                answer = input("Proceed? [y/N] ").strip().lower()
            except EOFError:
                # nothing is attached to stdin (a cron run, a pipeline):
                # treat that as a no rather than a traceback — same guard
                # as destroy/destroy_runtime (#85 item 16)
                print("No input available, aborted.")
                return
            if answer not in ("y", "yes"):
                print("Aborted.")
                return

        # --- handle new VMs ---
        if new_vm_names:
            if dry_run:
                self.logger.info("[dry-run] would clone and configure new VMs")
            else:
                # expand any `base_image: oci://…` into implicit templates
                # before resolving/cloning (the clone path needs a VM name).
                self._expand_oci_base_images()
                # ensure templates exist -- cloning from one that failed to
                # build produces VMs whose cloud-init never ran
                if not self.ensure_templates_exist():
                    raise ProvisionError(
                        "aborting: not every template could be created")
                try:
                    self.validate_base_images()
                except ValueError as exc:
                    raise ConfigError(str(exc)) from exc

                self._clone_and_configure_new_vms(new_vm_names)

                # wait for IPs on new VMs
                self.logger.info("waiting for new VMs to get IP addresses...")
                self.wait_for_vm_ips(new_vm_names, max_wait=300)

                # eject cdrom on new VMs
                for vm_name in new_vm_names:
                    self.provider.eject_cdrom(vm_name)

        # --- handle existing VMs ---
        if update_vm_names:
            prj_name = f'bprj__{config["project"]}__bprj'
            result_queue: Queue = Queue()

            update_tasks = []
            for cluster_name, cluster_cfg in self._vm_clusters.items():
                for vm_name, vm_info in cluster_cfg['vms'].items():
                    full = f"{prj_name}_{cluster_name}_{vm_name}"
                    if full in update_vm_names:
                        update_tasks.append(
                            (cluster_name, cluster_cfg, vm_name, vm_info))

            # _run_parallel reports raised/killed workers as failures;
            # merge those into the collected results below so a dying
            # worker lands in the failed summary instead of vanishing.
            _res, parallel_failures = self._run_parallel(
                [(f"{cluster_name}/{vm_name}", self._update_single_vm,
                  (cluster_name, cluster_cfg, vm_name, vm_info,
                   result_queue, dry_run))
                 for cluster_name, cluster_cfg, vm_name, vm_info in update_tasks],
                op_label='update vm')

            # collect and print results
            results = {}
            while not result_queue.empty():
                vm_name, result = result_queue.get()
                results[vm_name] = result
            for label, reason in parallel_failures.items():
                results.setdefault(label.split('/')[-1], {
                    'status': 'failed',
                    'details': reason,
                })

            # print summary
            no_change = [n for n, r in results.items() if r['status'] == 'no_change']
            updated = [n for n, r in results.items() if r['status'] == 'updated']
            needs_restart = [
                n for n, r in results.items()
                if r['status'] == 'needs_restart']
            failed = [n for n, r in results.items() if r['status'] == 'failed']
            dry_run_items = [n for n, r in results.items() if r['status'] == 'dry_run']

            if dry_run_items:
                self.logger.info("--- dry-run summary ---")
                for vm_name in dry_run_items:
                    self.logger.info(f"  {vm_name}: {results[vm_name]['details']}")

            if no_change:
                self.logger.info(f"no changes: {', '.join(no_change)}")
            if updated:
                self.logger.info(f"updated: {', '.join(updated)}")
                for vm_name in updated:
                    self.logger.info(f"  {vm_name}: {results[vm_name]['details']}")
            if needs_restart:
                self.logger.warning(
                    f"restart required: {', '.join(needs_restart)}")
                for vm_name in needs_restart:
                    self.logger.warning(
                        f"  {vm_name}: {results[vm_name]['details']}")
            if failed:
                self.logger.error(f"failed: {', '.join(failed)}")
                for vm_name in failed:
                    self.logger.error(f"  {vm_name}: {results[vm_name]['details']}")

        # --- handle removed VMs ---
        if removed_vm_names:
            if dry_run:
                self.logger.info("[dry-run] would destroy the following VMs:")
                for vm_name in sorted(removed_vm_names):
                    self.logger.info(f"  {vm_name}")
            else:
                processes = [
                    (vm_name, self._destroy_removed_vm, (vm_name,))
                    for vm_name in removed_vm_names
                ]
                self._run_parallel(processes, op_label='destroy removed vm')

                short = [n.split('_')[-1] for n in sorted(removed_vm_names)]
                self.logger.info(
                    f"removed {len(removed_vm_names)} VM(s): "
                    f"{', '.join(short)}")

        # regenerate SSH config and display connect info
        if not dry_run:
            # a recreated network power-cycles the guests attached to it, so
            # the addresses the ssh config is written from do not exist yet
            if any(outcome in ('recreated', 'partial')
                   for outcome in network_results.values()):
                self.wait_for_vm_ips(self._vms_worth_waiting_for())
            self.setup_ssh_access()
            self.connect_info()

    ### end update functions ####
