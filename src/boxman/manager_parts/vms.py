"""VM lifecycle and update flows for BoxmanManager."""

import contextlib
import logging
import os
import time
from multiprocessing import Queue
from typing import Any

from boxman import log
from boxman.exceptions import (
    BoxmanError,
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
        # Abort provision if any clone worker fails. Without this check, the
        # subsequent configure / start / wait-for-IP steps all spam errors
        # against VMs that were never defined, and the wait-for-IP loop in
        # particular looks like a hang. Goes through the shared helper so the
        # fan-out is bounded (one process per VM does not scale to a large
        # cluster) and killed workers are reported, not just non-zero exits.
        _results, failures = self._run_parallel(
            [(new_vm_name, _clone_with_retry,
              (self.provider, cluster, vm_info, new_vm_name))
             for cluster, vm_info, new_vm_name in clone_tasks],
            op_label='clone vm')
        if failures:
            names = ', '.join(sorted(failures))
            raise ProvisionError(
                f"clone failed for {len(failures)} VM(s) ({names}); aborting "
                f"provision. See the preceding clone or guest-sanitizer log "
                f"for the underlying cause and remediation.")

    ### end vms define / remove / destroy
    def _configure_and_start_vm(
        self, cluster_name: str, cluster: dict[str, Any], vm_name: str, vm_info: dict[str, Any]
    ) -> None:
        """
        Configure and start a single VM: cpu/mem, network interfaces, disks, then start.

        Designed to be called in a separate process per VM after cloning is done.

        Every step below returns a bool. They used to be logged at warning
        level and dropped, so a VM whose disks, NICs and start command all
        failed still reported success and the command exited 0 (#164 X3).
        Each failure is now collected and raised once at the end: the
        remaining steps are still attempted, so the operator sees
        everything that is wrong in one run rather than one thing per
        retry, and the VM is still started if it can be.

        Raises:
            ProvisionError: if any configuration step or the start failed.
        """
        prj_name = f'bprj__{self.config["project"]}__bprj'
        full_vm_name = f"{prj_name}_{cluster_name}_{vm_name}"
        vm_info = vm_info.copy()
        problems: list[str] = []

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
                self.logger.error(f"failed to configure cpu and memory for vm {vm_name}")
                problems.append('cpu/memory')
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
                self.logger.error(f"failed to configure memballoon for vm {vm_name}")
                problems.append('memballoon')

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
                self.logger.error(f"some network interfaces could not be configured for vm {vm_name}")
                problems.append('network interfaces')

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
                self.logger.error(f"some disks could not be configured for vm {vm_name}")
                problems.append('disks')

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
                self.logger.error(f"some shared folders could not be configured for vm {vm_name}")
                problems.append('shared folders')

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
                self.logger.error(f"some CDROMs could not be configured for vm {vm_name}")
                problems.append('cdroms')

        # start
        self.logger.info(f"starting vm {full_vm_name}")
        success = self.session_for_cluster(cluster_name).start_vm(full_vm_name)
        if success:
            self.logger.info(f"successfully started the vm {full_vm_name}")
        else:
            self.logger.error(f"failed to start the vm {full_vm_name}")
            problems.append('start')

        if problems:
            raise ProvisionError(
                f"vm {full_vm_name} was not fully brought up "
                f"({', '.join(problems)} failed) — see the preceding "
                f"per-step errors for the cause.")

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
        if not session.confirm_vm_absent(full_vm_name):
            session.destroy_vm(full_vm_name, force=True)

        # Unlinking the disk files is gated on a *positive* confirmation that
        # the domain is gone. destroy_vm()'s return value cannot carry that:
        # it reports success when the libvirt query itself could not be
        # answered, so an outage looked like a finished teardown and the
        # qcow2 files were removed out from under a still-running guest.
        if not session.confirm_vm_absent(full_vm_name):
            raise ProvisionError(
                f"{full_vm_name}: could not confirm the domain was "
                f"undefined; leaving its disks in place rather than removing "
                f"storage under a possibly-live guest")

        # No bool check here on purpose: remove_vm_disks() returns True or
        # raises (its os.remove calls are uncaught), so a filesystem error
        # already reaches _run_parallel's failure handling. Testing the
        # return value would be dead code.
        session.destroy_disks(
            cluster['workdir'],
            vm_name=full_vm_name,
            disks=vm_info.get('disks', [])
        )

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
        #
        # The disk directories come from libvirt, so they must be discovered
        # *before* the domain is undefined.
        disk_dirs = self._vm_disk_dirs(full_vm_name)
        self.provider.destroy_vm(full_vm_name)
        if not self.provider.confirm_vm_absent(full_vm_name):
            self.provider.destroy_vm(full_vm_name, force=True)

        # Same gate as _destroy_vm_and_disks: only a positive confirmation
        # that the domain is gone authorises unlinking its disks.
        if not self.provider.confirm_vm_absent(full_vm_name):
            raise ProvisionError(
                f"{full_vm_name}: could not confirm the domain was "
                f"undefined; leaving its disks in place rather than removing "
                f"storage under a possibly-live guest")

        for workdir in disk_dirs:
            # see _destroy_vm_and_disks: remove_vm_disks() raises rather
            # than returning False
            self.provider.destroy_disks(
                workdir,
                vm_name=full_vm_name,
                disks=[]
            )

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
        _results, failures = self._run_parallel(
            processes, op_label='configure and start vm')
        if failures:
            names = ', '.join(sorted(failures))
            raise ProvisionError(
                f"configure/start failed for {len(failures)} VM(s) ({names}); "
                f"see the preceding per-VM errors for the cause.")

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

    def _confirm_project_torn_down(self) -> tuple[bool, str]:
        """
        Positively confirm that no libvirt VM of this project is left.

        Fails closed, and is provider-aware: a docker-compose-only project has
        no libvirt domains to look for and must not acquire a libvirt
        dependency, so it confirms immediately. Otherwise the evidence has to
        be a **successful** ``virsh list --all --name`` with no domain
        carrying this project's prefix. A query that could not be answered is
        reported as "not confirmed", never as "nothing left" — the callers use
        this to decide whether it is safe to delete the workspace and forget
        the project, and both are unrecoverable if resources actually survived.

        Returns:
            ``(True, '')`` when the teardown is confirmed complete, otherwise
            ``(False, reason)``.
        """
        if not self._has_libvirt_clusters():
            return True, ''

        result = self._virsh().execute(
            "list", "--all", "--name", hide=True, warn=True)
        if not result.ok:
            return False, ("could not query libvirt to confirm the teardown "
                           "completed")

        prj_prefix = f"bprj__{self.config.get('project', '')}__bprj_"
        survivors = sorted(
            v.strip() for v in (result.stdout or "").splitlines()
            if v.strip() and v.strip().startswith(prj_prefix)
        )
        if survivors:
            return False, f"VMs are still defined: {', '.join(survivors)}"
        return True, ''

    #: How libvirt spells a domain that is defined but not running. Both
    #: spellings appear across virsh versions and output modes.
    _SHUT_OFF_STATES = frozenset({'shut off', 'shutoff'})

    def _get_vm_states(self) -> dict[str, str]:
        """
        Query libvirt and return a mapping of project VM name -> state string
        for all project VMs that exist.

        State strings are as returned by ``virsh list --all`` — 'running',
        'shut off', 'paused', … — with one addition boxman makes itself: a
        domain libvirt reports as 'shut off' that also holds managed saved
        state is reported as ``'managedsave'``.

        That addition is the whole point. The State column of
        ``virsh list --all`` has no 'saved' value to report, so without the
        second query every saved guest reads as an ordinary cold 'shut off'
        one, and ``up`` boots it from scratch — silently discarding the
        memory image ``down`` had just written (#164 FB-3).

        Returns:
            Dict mapping full VM name to its state, only for VMs that exist.

        Raises:
            ProvisionError: if libvirt cannot be queried. Returning an empty
                mapping used to read as "no VM exists", which sends ``up``
                down the full-provision path against a project whose domains
                are merely unreachable (#164 FB-3).
        """
        expected = set(self._get_project_vm_names())
        if not expected:
            return {}
        if not self._has_libvirt_clusters():
            return {}

        # Use the table output to get both name and state
        result = self._virsh().execute("list", "--all", hide=True, warn=True)
        if not result.ok:
            raise ProvisionError(
                f"could not query VM states via virsh (exit "
                f"{result.return_code}): "
                f"{(result.stderr or '').strip() or 'no error output'}")

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

        # Only ask about managed saved state when something is actually shut
        # off — a running or paused domain cannot have a managed save waiting
        # for it, so the extra query would be pure cost.
        if any(state in self._SHUT_OFF_STATES for state in states.values()):
            saved = self._managed_save_domains()
            for vm_name, vm_state in states.items():
                if vm_state in self._SHUT_OFF_STATES and vm_name in saved:
                    states[vm_name] = 'managedsave'

        return states

    def _managed_save_domains(self) -> set[str]:
        """
        Names of the domains that currently hold managed saved state.

        One bulk query, not one per VM.

        A failed query raises rather than returning an empty set, because an
        empty set means "nothing is saved" — and acting on that would cold-boot
        a guest whose memory image is sitting on disk. ``virsh list --all
        --managed-save`` would fold this into the first query, but its display
        logic reports a domain whose save-presence probe *failed* as an
        ordinary shut-off one, which is exactly the fail-open answer this
        method exists to avoid (#164 FB-3).
        """
        result = self._virsh().execute(
            "list", "--all", "--with-managed-save", "--name",
            hide=True, warn=True)
        if not result.ok:
            raise ProvisionError(
                f"could not determine which domains hold managed saved state "
                f"(exit {result.return_code}): "
                f"{(result.stderr or '').strip() or 'no error output'}\n"
                f"Refusing to continue: treating this as 'nothing is saved' "
                f"would cold-boot guests whose memory image is on disk.")
        return {
            line.strip() for line in result.stdout.splitlines() if line.strip()
        }

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
        # and the configure/start step below see the resolved values.
        # Scoped to the new VMs: fetching media for guests that already exist
        # can overwrite an ISO one of them currently has open (#164 FB-5).
        self._resolve_iso_config(new_vm_names)
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
            raise ProvisionError(
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

        _results, failures = self._run_parallel(
            [(f"{cluster_name}/{vm_name}", self._configure_and_start_vm,
              (cluster_name, cluster, vm_name, vm_info))
             for cluster_name, cluster, vm_name, vm_info in configure_tasks],
            op_label='configure and start vm')
        if failures:
            names = ', '.join(sorted(failures))
            raise ProvisionError(
                f"configure/start failed for {len(failures)} new VM(s) "
                f"({names}); aborting update.")

    def _update_single_vm(
        self,
        cluster_name: str,
        cluster: dict[str, Any],
        vm_name: str,
        vm_info: dict[str, Any],
        result_queue: Queue,
        dry_run: bool = False,
        allow_restart: bool = False,
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
                diff['memballoon_restart_pending'] or
                diff['removed_disks'] or
                diff['shared_folders_restart_pending']
            )

            # A declaration whose target still holds a different disk that
            # boxman owns is a conflict, not a warning: reconciliation
            # matches the occupant by target, so it would grow the disk the
            # operator renamed away from and report success. Refused before
            # anything is applied -- cpu and memory included (#164 F2
            # review, finding 4).
            if diff['disk_conflicts']:
                detail = '; '.join(
                    f"'{name}' declares target {target}, which still holds "
                    f"{source}"
                    for name, target, source in diff['disk_conflicts'])
                result_queue.put((vm_name, {
                    'status': 'failed',
                    'details': (
                        f"{detail}. Detach it first, or give the new disk a "
                        f"free target -- nothing was changed")
                }))
                return

            # Reported, never acted on: boxman recorded attaching these but
            # what is at the target now is not what it attached, so it has
            # no basis for detaching it (#164 F2).
            for record, reason in diff['refused_disk_removals']:
                self.logger.warning(
                    f"VM {vm_name}: not detaching '{record.name}' -- {reason}")
            for stray in diff['unowned_disks']:
                if diff['has_disk_records']:
                    self.logger.warning(
                        f"VM {vm_name}: {stray['target']} ({stray['source']}) "
                        f"is attached but neither declared nor recorded by "
                        f"boxman -- leaving it alone")
                else:
                    # No record at all: this domain predates the ownership
                    # metadata. It used to report nothing here, so dropping
                    # a disk from its config looked like a no-op (#164 F2
                    # review).
                    self.logger.warning(
                        f"VM {vm_name}: {stray['target']} ({stray['source']}) "
                        f"is attached but not declared. This VM predates "
                        f"boxman's disk ownership records, so boxman will "
                        f"not detach anything on it. To remove it by hand: "
                        f"{self.provider.virsh_invocation()} detach-disk "
                        f"{full_vm_name} {stray['target']} --config "
                        f"(the image file is not deleted)")

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
            if diff['removed_disks']:
                names = [f"{r.name} ({r.target})" for r in diff['removed_disks']]
                changes.append(f"detach disks: {', '.join(names)}")
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
            # A paused guest is active but not running: it cannot be cleanly
            # shut down and restarted, yet a change that needs a restart is
            # just as pending for it.
            vm_active = VMStateDiffer.domain_is_active(diff['vm_state'])
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
                    self.logger.info(
                        f"VM {vm_name}: memballoon changes need a restart to "
                        f"take effect")

            # disks
            #
            # A detach is only ever applied to an inactive domain, because
            # `detach-disk --config` edits the persistent definition and a
            # live guest keeps using the device until it goes down. Three
            # cases (#164 F2 review, amendment 1):
            #
            #   inactive        -> detach now, after the additions succeed
            #   running + --restart -> detach between the shutdown and the
            #                          start, when live IS persistent
            #   paused, or running without --restart -> pending, untouched
            #
            # A paused guest is never shut down for this: it did not ask to
            # be resumed or stopped, and a forced stop is not a clean one.
            detach_plan = diff['removed_disks']
            detach_offline = bool(detach_plan) and not vm_active
            detach_after_restart = (
                bool(detach_plan) and vm_running and allow_restart)
            if detach_plan and not detach_offline and not detach_after_restart:
                restart_needed = True
            if detach_after_restart:
                restart_needed = True

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

            # Detach last of the disk work, and only if what came before
            # it worked: a removal applied after a failed addition detaches
            # a disk whose replacement never arrived (#164 F2).
            detach_deferred = None
            if detach_offline:
                try:
                    outcome = self.provider.remove_vm_disks(
                        full_vm_name, detach_plan)
                except BoxmanError as exc:
                    result_queue.put((vm_name, {
                        'status': 'failed',
                        'details': f"disk detach failed: {exc}"
                    }))
                    return
                # A deferral is an ordinary outcome, not a failure: managed
                # saved state means the detach would not reach the guest
                # that is resumed (#164 F2 review round 2, finding 3).
                detach_deferred = (outcome or {}).get('deferred')
                if detach_deferred:
                    restart_needed = True

            # cdroms
            if diff['new_cdroms'] or diff['removed_cdroms'] or diff['changed_cdroms']:
                # Active, not running: a paused guest is still active, and
                # a persistent-only edit would report success while leaving
                # the old media in place until the next boot (#164 FB-5).
                cdrom_ok = self.provider.update_vm_cdroms(
                    vm_name=full_vm_name,
                    new_cdroms=diff['new_cdroms'],
                    removed_cdroms=diff['removed_cdroms'],
                    changed_cdroms=diff['changed_cdroms'],
                    vm_active=vm_active
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

            # A share that is configured but not live is pending whether or
            # not this run is what configured it -- otherwise an attachment
            # that fell back to config-only last time reported nothing
            # outstanding (#164 C1 review, finding 7).
            if diff['shared_folders_restart_pending']:
                restart_needed = True

            # Every change that cannot reach a live guest, in one place.
            # memballoon only ever landed in the persistent config, and was
            # reported through a separate branch that the other restart
            # sources bypassed (#164 C1).
            if pending_restart:
                restart_needed = True

            # handle restart if needed
            if restart_needed and vm_running and allow_restart:
                self.logger.info(
                    f"VM {vm_name}: restarting to apply changes "
                    f"(live max ceiling cannot be raised)")
                # Both calls return a bool and both were dropped, so a
                # guest that never went down was reported "(restarted)"
                # and `update` exited 0 with the restart-only changes not
                # in effect (#164 X3). The shutdown has to be checked
                # first for a second reason: start_vm() on a guest that is
                # still running returns True, so a lost shutdown would
                # hide itself behind a successful start.
                # A detach must never follow a *forced* stop: force_after
                # runs `virsh destroy` on timeout and still reports success,
                # so a guest that did not shut down cleanly would go on to
                # have a disk removed (#164 F2 review, amendment 1).
                if not self.provider.shutdown_and_wait(
                        full_vm_name,
                        force_after=not detach_after_restart):
                    result_queue.put((vm_name, {
                        'status': 'failed',
                        'details': (
                            '; '.join(changes) +
                            ' — applied, but the VM could not be shut down, '
                            'so the changes that need a restart are not in '
                            'effect')
                    }))
                    return
                if detach_after_restart:
                    # The guest is down, so live is persistent; remove_vm_disks
                    # re-verifies both facts before touching anything.
                    try:
                        self.provider.remove_vm_disks(full_vm_name, detach_plan)
                    except BoxmanError as exc:
                        # start_vm()'s result was discarded and the message
                        # claimed the guest was running again regardless
                        # (#164 F2 review round 2, finding 7).
                        restarted = self.provider.start_vm(full_vm_name)
                        tail = ('the VM was started again' if restarted else
                                'and the VM could not be started again — it '
                                'is still shut off')
                        result_queue.put((vm_name, {
                            'status': 'failed',
                            'details': (
                                f"disk detach failed after shutdown: {exc} — "
                                f"{tail}")
                        }))
                        return
                if not self.provider.start_vm(full_vm_name):
                    result_queue.put((vm_name, {
                        'status': 'failed',
                        'details': (
                            '; '.join(changes) +
                            ' — applied, but the VM did not come back up '
                            'after the restart and is still shut off')
                    }))
                else:
                    result_queue.put((vm_name, {
                        'status': 'updated',
                        'details': '; '.join(changes) + ' (restarted)'
                    }))
            elif restart_needed and vm_active:
                # Deferred, not skipped: the persistent config already has
                # the change, so it takes effect the next time the guest
                # boots. `update` used to power-cycle a running guest for
                # this without being asked (#164 C1).
                result_queue.put((vm_name, {
                    'status': 'needs_restart',
                    'details': (
                        '; '.join(changes) +
                        ' — written to the persistent config; restart the VM '
                        'to apply them, or re-run update with --restart')
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
        # Deliberately not `auto_accept or ...`: --yes answers the VM-removal
        # prompt, and someone passing it to avoid an interactive update has
        # not thereby agreed to have a running guest power-cycled (#164 C1).
        allow_restart = getattr(cli_args, 'restart', False)

        # Collected across the phases below and raised once at the very end:
        # a VM that fails to update must not leave the command reporting
        # success, but it must also not stop the remaining independent work
        # (other VMs, removals, ssh config) from completing.
        update_failures: list[str] = []

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

            # Resolve declared media to real local paths *before* diffing.
            # The differ cannot tell an unresolved entry from a resolved one,
            # and an unresolved cdrom read as "remove the ISO that is
            # attached": os.path.abspath('') is the working directory, which
            # matches nothing, so the guest's install ISO landed in
            # removed_cdroms (#164 FB-5).
            #
            # Resolution here downloads nothing, so --dry-run stays free of
            # side effects and an update that changes only a CPU count does
            # not reach for the network.
            # A dry run resolves paths but never downloads, so previewing
            # an update stays free of side effects.
            media_failures = self._normalize_cdroms_for_update(
                update_vm_names, allow_fetch=not dry_run)

            update_tasks = []
            skipped_media = {}
            for cluster_name, cluster_cfg in self._vm_clusters.items():
                for vm_name, vm_info in cluster_cfg['vms'].items():
                    full = f"{prj_name}_{cluster_name}_{vm_name}"
                    if full not in update_vm_names:
                        continue
                    if full in media_failures:
                        # This VM's media could not be resolved. Fail it on
                        # its own rather than aborting the run — one bad ISO
                        # reference should not stop every other VM from
                        # reconciling.
                        skipped_media[vm_name] = media_failures[full]
                        continue
                    update_tasks.append(
                        (cluster_name, cluster_cfg, vm_name, vm_info))

            # _run_parallel reports raised/killed workers as failures;
            # merge those into the collected results below so a dying
            # worker lands in the failed summary instead of vanishing.
            _res, parallel_failures = self._run_parallel(
                [(f"{cluster_name}/{vm_name}", self._update_single_vm,
                  (cluster_name, cluster_cfg, vm_name, vm_info,
                   result_queue, dry_run, allow_restart))
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
            # VMs whose declared media could not be resolved never ran.
            for vm_name, reason in skipped_media.items():
                results[vm_name] = {
                    'status': 'failed',
                    'details': f"could not resolve declared media: {reason}",
                }

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
                update_failures.extend(
                    f"{vm_name}: {results[vm_name]['details']}"
                    for vm_name in failed)

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
                _res, destroy_failures = self._run_parallel(
                    processes, op_label='destroy removed vm')
                update_failures.extend(
                    f"{label}: {reason}"
                    for label, reason in sorted(destroy_failures.items()))

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

        if update_failures:
            raise ProvisionError(
                f"update finished with {len(update_failures)} failure(s): "
                + "; ".join(update_failures))

    ### end update functions ####
