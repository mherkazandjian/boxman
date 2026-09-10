import os
from typing import Any

from boxman import log
from boxman.exceptions import ProvisionError

from .commands import VirshCommand
from .disk_ownership import (
    DEFAULT_DISK_TARGET,
    disk_logical_name,
    occupied_target_conflicts,
    plan_disk_removals,
    read_disk_records,
    unowned_disks,
)
from .virsh_edit import VirshEdit
from .virsh_parse import parse_domblklist


class VMStateDiffer:
    """
    Compares desired VM state (from conf.yml) against actual state (from libvirt)
    and produces a structured diff describing what needs to change.
    """

    # States with an active domain and therefore distinct live XML. A paused,
    # blocked, shutting-down, power-managed, or crash-preserved guest still
    # needs a later boot before persistent-only device edits become live.
    _LIVE_DOMAIN_STATES = frozenset({
        'running', 'blocked', 'paused', 'in shutdown', 'pmsuspended', 'crashed',
    })

    #: States with no active domain. Anything in neither set is unknown —
    #: including the 'unknown' get_vm_state() returns when domstate fails.
    _INACTIVE_DOMAIN_STATES = frozenset({'shut off', 'shutoff'})

    @classmethod
    def domain_is_active(cls, vm_state: str) -> bool:
        """
        Whether *vm_state* describes an active domain.

        Active is not the same as running: a paused guest is still active,
        and device edits applied only to its persistent configuration would
        report success while leaving the running guest untouched until its
        next boot (#164 FB-5).

        Raises:
            ProvisionError: for a state in neither set. Guessing either way
                picks a wrong virsh flag, and 'unknown' is what
                :meth:`get_vm_state` returns when the query failed.
        """
        state = (vm_state or '').strip().lower()
        if state in cls._LIVE_DOMAIN_STATES:
            return True
        if state in cls._INACTIVE_DOMAIN_STATES:
            return False
        raise ProvisionError(
            f"cannot tell whether the domain is active from state "
            f"'{vm_state}'; refusing to guess which virsh flags to use")

    def __init__(self, provider_config: dict[str, Any] | None = None):
        self.virsh = VirshCommand(provider_config)
        self.virsh_edit = VirshEdit(provider_config)
        self.provider_config = provider_config
        self.logger = log

    def get_vm_state(self, domain_name: str) -> str:
        """
        Get the current state of a VM.

        Returns:
            State string: 'running', 'shut off', 'paused', etc.
        """
        result = self.virsh.execute('domstate', domain_name, warn=True)
        if not result.ok:
            return 'unknown'
        return result.stdout.strip()

    def get_actual_cpu(self, domain_name: str) -> dict[str, int]:
        """
        Get actual CPU topology from the domain XML.

        Returns:
            Dict with 'sockets', 'cores', 'threads', 'total_vcpus',
            'current_vcpus' keys.  ``total_vcpus`` is the max ceiling
            (``//vcpu`` text) and ``current_vcpus`` is the active count
            (``//vcpu/@current``, falling back to ``total_vcpus``).
        """
        xml_content = self.virsh_edit.get_domain_xml(domain_name)

        vcpu_values = self.virsh_edit.find_xpath_values(xml_content, '//vcpu')
        total_vcpus = int(vcpu_values[0]) if vcpu_values else 1

        from lxml import etree
        tree = etree.fromstring(xml_content.encode('utf-8'))

        # current active vCPU count (falls back to max when not set)
        vcpu_elements = tree.xpath('//vcpu')
        current_vcpus = total_vcpus
        if vcpu_elements:
            current_attr = vcpu_elements[0].get('current')
            if current_attr is not None:
                current_vcpus = int(current_attr)

        sockets = 1
        cores = 1
        threads = 1

        topology = tree.xpath('//cpu/topology')
        if topology:
            sockets = int(topology[0].get('sockets', '1'))
            cores = int(topology[0].get('cores', '1'))
            threads = int(topology[0].get('threads', '1'))

        return {
            'sockets': sockets,
            'cores': cores,
            'threads': threads,
            'total_vcpus': total_vcpus,
            'current_vcpus': current_vcpus,
        }

    def get_actual_memory_mb(self, domain_name: str) -> int:
        """
        Get actual current memory in MiB from the domain XML.

        Reads //currentMemory which reflects the active memory allocation,
        as opposed to //memory which is the maximum ceiling.
        """
        xml_content = self.virsh_edit.get_domain_xml(domain_name)
        memory_values = self.virsh_edit.find_xpath_values(
            xml_content, '//currentMemory')
        if not memory_values:
            # fall back to //memory for VMs without currentMemory element
            memory_values = self.virsh_edit.find_xpath_values(
                xml_content, '//memory')
        if not memory_values:
            return 0
        # memory in XML is in KiB by default
        memory_kib = int(memory_values[0])
        return memory_kib // 1024

    def get_max_vcpus(self, domain_name: str) -> int:
        """
        Get the maximum vCPU count from the domain XML (the //vcpu ceiling).
        """
        xml_content = self.virsh_edit.get_domain_xml(domain_name)
        vcpu_values = self.virsh_edit.find_xpath_values(xml_content, '//vcpu')
        return int(vcpu_values[0]) if vcpu_values else 1

    def get_max_memory_mb(self, domain_name: str) -> int:
        """
        Get the maximum memory in MiB from the domain XML (the //memory ceiling).
        """
        xml_content = self.virsh_edit.get_domain_xml(domain_name)
        memory_values = self.virsh_edit.find_xpath_values(xml_content, '//memory')
        if not memory_values:
            return 0
        return int(memory_values[0]) // 1024

    def get_actual_memballoon(
            self, domain_name: str, inactive: bool = True) -> dict[str, Any]:
        """
        Get the actual memballoon state from persistent or live XML.

        Args:
            domain_name: libvirt domain name.
            inactive: read persistent XML when True; read the running guest's
                live XML when False.

        Returns:
            Dict with 'free_page_reporting' and 'autodeflate' (bools; a
            missing attribute or memballoon element counts as False), plus
            'stats_period' (int seconds, or None when no <stats> element is
            present).
        """
        from lxml import etree

        xml_content = self.virsh_edit.get_domain_xml(
            domain_name, inactive=inactive)
        tree = etree.fromstring(xml_content.encode('utf-8'))
        matches = tree.xpath('//devices/memballoon')
        if not matches:
            return {
                'free_page_reporting': False,
                'autodeflate': False,
                'stats_period': None,
            }
        memballoon = matches[0]
        stats = memballoon.find('stats')
        stats_period = None
        if stats is not None and stats.get('period'):
            stats_period = int(stats.get('period'))
        return {
            'free_page_reporting': memballoon.get('freePageReporting') == 'on',
            'autodeflate': memballoon.get('autodeflate') == 'on',
            'stats_period': stats_period,
        }

    @staticmethod
    def normalize_memballoon_config(
            config: dict[str, Any] | None) -> dict[str, Any]:
        """
        Normalize a memballoon config block into a fully-explicit desired
        state. A missing block (None) maps to the libvirt defaults, so
        removing the block from conf.yml reconciles back to
        ``freePageReporting`` and ``autodeflate`` off and no ``<stats>``
        element. Validate before normalizing so YAML strings and other
        malformed values cannot be truthiness-coerced during ``update``.
        """
        if config is None:
            config = {}
        VirshEdit.validate_memballoon_config(config)
        return {
            'free_page_reporting': config.get('free_page_reporting', False),
            'autodeflate': config.get('autodeflate', False),
            'stats_period': config.get('stats_period'),
        }

    def get_disk_records(self, domain_name: str):
        """
        The disks boxman recorded attaching to *domain_name*.

        A probe of its own, like :meth:`get_actual_disks`, so it can be
        stubbed the same way. ``None`` means the domain carries no boxman
        ownership metadata -- which is never grounds for detaching
        anything (#164 F2).
        """
        return read_disk_records(self.virsh, domain_name)

    def get_actual_disks(self, domain_name: str,
                         inactive: bool = False) -> list[dict[str, Any]]:
        """
        Get actual disk info from virsh domblklist + virsh domblkinfo.

        Uses virsh domblkinfo to query disk sizes through the hypervisor,
        which works on running VMs (unlike qemu-img info which fails due
        to write locks held by QEMU).

        Returns:
            List of dicts with 'target', 'source', 'size_mb' keys.
            Only includes file-backed disk devices (excludes cdroms, etc.).

        Raises:
            ProvisionError: if the domain's disks cannot be listed. An empty
                list is a real answer -- "this domain has no file-backed
                disks" -- and returning it for "the query failed" made every
                declared disk look absent, so the diff proposed attaching
                them all. Once a removal path reads this list, the same
                empty result would read as "nothing is attached" (#164 F2).
        """
        flags = ['--details'] + (['--inactive'] if inactive else [])
        result = self.virsh.execute(
            'domblklist', domain_name, *flags, warn=True)
        if not result.ok:
            raise ProvisionError(
                f"could not list the disks of {domain_name}: "
                f"{(result.stderr or '').strip() or 'virsh domblklist failed'}"
            )

        disks = []
        for row in parse_domblklist(result.stdout):
            if row.device != 'disk' or row.type != 'file':
                continue
            source = row.source
            if source is None or source == '-':
                continue

            size_mb = self._get_disk_size_mb(domain_name, row.target)
            disks.append({
                'target': row.target,
                'source': source,
                'size_mb': size_mb
            })

        return disks

    def _get_disk_size_mb(self, domain_name: str, target: str) -> int:
        """
        Get disk virtual size in MiB via virsh domblkinfo.

        This queries through the hypervisor so it works on running VMs
        without hitting write-lock issues.
        """
        try:
            result = self.virsh.execute(
                'domblkinfo', domain_name, target, warn=True)
            if not result.ok:
                self.logger.warning(
                    f"failed to get disk info for {target} on {domain_name}")
                return 0

            # parse domblkinfo output — look for "Capacity:" line (in bytes)
            for line in result.stdout.strip().split('\n'):
                if line.strip().startswith('Capacity:'):
                    size_bytes = int(line.split(':')[1].strip())
                    return size_bytes // (1024 * 1024)

            self.logger.warning(
                f"no Capacity found in domblkinfo for {target} on {domain_name}")
            return 0
        except Exception as exc:
            self.logger.warning(
                f"error getting disk size for {target} on {domain_name}: {exc}")
            return 0

    @staticmethod
    def _expected_disk_path(disk_config: dict[str, Any],
                            workdir: str,
                            disk_prefix: str) -> str:
        """
        Compute the expected disk file path, matching DiskManager.configure_from_disk_config logic.

        Through the same normalisation DiskManager uses -- not a second
        copy of it. ``.get("name", "disk")`` defaults only an *absent*
        key, so ``name: null`` predicted ``<prefix>_None.qcow2`` and
        ``name: ""`` predicted ``<prefix>_.qcow2`` while creation resolved
        both to ``disk``. The predicted file was absent, so the entry was
        not marked attach_only, and creation then ran ``qemu-img create``
        over the existing ``<prefix>_disk.qcow2`` and destroyed it. No
        race, no detach (#164 F2 review round 4).
        """
        from .disk import disk_path_for
        disk_name = disk_logical_name(disk_config)
        driver = disk_config.get("driver", {})
        driver_type = driver.get("type", "qcow2")
        return disk_path_for(workdir, disk_name,
                             driver_type=driver_type,
                             disk_prefix=disk_prefix)

    def get_actual_cdroms(self, domain_name: str) -> list[dict[str, Any]]:
        """
        Get actual CDROM devices attached to a VM, excluding seed ISOs.

        Returns:
            List of dicts with 'target' and 'source' keys.
        """
        from .cdrom import CDROMManager
        return CDROMManager(
            domain_name, provider_config=self.provider_config).get_attached_cdroms()

    def get_actual_shared_folders(self, domain_name: str,
                                  inactive: bool = False) -> list[dict[str, Any]]:
        """
        Get actual filesystem (shared folder) devices from domain XML.

        Args:
            inactive: read the persistent definition instead of the live
                domain. See
                :meth:`SharedFolderManager.get_attached_shared_folders`.

        Returns:
            List of dicts with 'name', 'host_path', and 'readonly' keys.
        """
        from .shared_folder import SharedFolderManager
        return SharedFolderManager(
            domain_name,
            provider_config=self.provider_config).get_attached_shared_folders(
                inactive=inactive)

    def diff_vm(self,
                domain_name: str,
                desired_cpus: dict[str, int] | None,
                desired_memory_mb: int | None,
                desired_disks: list[dict[str, Any]] | None,
                workdir: str,
                disk_prefix: str,
                desired_max_vcpus: int | None = None,
                desired_max_memory_mb: int | None = None,
                desired_shared_folders: list[dict[str, Any]] | None = None,
                desired_cdroms: list[dict[str, Any]] | None = None,
                desired_memballoon: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Compute the diff between desired config and actual VM state.

        Returns:
            Dict with keys:
              - cpu_changed, desired_cpus, actual_cpus
              - memory_changed, desired_memory_mb, actual_memory_mb
              - max_vcpus_changed, desired_max_vcpus, actual_max_vcpus
              - max_memory_changed, desired_max_memory_mb, actual_max_memory_mb
              - new_disks: list of disk configs to create and attach
                (a config carries ``attach_only: True`` when its image
                file already exists on disk — attach it as-is instead of
                recreating it)
              - resize_disks: list of dicts with target, source, current_size_mb, desired_size_mb
              - new_cdroms, removed_cdroms, changed_cdroms
              - new_shared_folders, removed_shared_folders, changed_shared_folders
              - memballoon_changed, memballoon_restart_pending,
                desired_memballoon (normalized), actual_memballoon, and
                live_memballoon
              - vm_state: current VM state string
        """
        vm_state = self.get_vm_state(domain_name)

        # --- CPU diff ---
        actual_cpus = self.get_actual_cpu(domain_name)
        cpu_changed = False
        if desired_cpus:
            desired_total = (desired_cpus.get('sockets', 1) *
                             desired_cpus.get('cores', 1) *
                             desired_cpus.get('threads', 1))
            # Compare effective vCPU count and core/thread shape.
            # Sockets in XML may be scaled up to satisfy max_vcpus, so
            # comparing raw sockets would produce false positives.
            actual_current = actual_cpus.get(
                'current_vcpus', actual_cpus['total_vcpus'])
            cpu_changed = (
                desired_total != actual_current or
                desired_cpus.get('cores', 1) != actual_cpus['cores'] or
                desired_cpus.get('threads', 1) != actual_cpus['threads']
            )

        # --- Max vCPU diff ---
        actual_max_vcpus = self.get_max_vcpus(domain_name)
        max_vcpus_changed = False
        if desired_max_vcpus is not None:
            max_vcpus_changed = desired_max_vcpus != actual_max_vcpus

        # --- Memory diff ---
        actual_memory_mb = self.get_actual_memory_mb(domain_name)
        memory_changed = False
        if desired_memory_mb is not None:
            memory_changed = desired_memory_mb != actual_memory_mb

        # --- Max memory diff ---
        actual_max_memory_mb = self.get_max_memory_mb(domain_name)
        max_memory_changed = False
        if desired_max_memory_mb is not None:
            max_memory_changed = desired_max_memory_mb != actual_max_memory_mb

        # --- Memballoon diff ---
        actual_memballoon = self.get_actual_memballoon(domain_name)
        normalized_memballoon = self.normalize_memballoon_config(desired_memballoon)
        memballoon_changed = normalized_memballoon != actual_memballoon
        live_memballoon = actual_memballoon
        if vm_state in self._LIVE_DOMAIN_STATES:
            live_memballoon = self.get_actual_memballoon(
                domain_name, inactive=False)
        memballoon_restart_pending = (
            vm_state in self._LIVE_DOMAIN_STATES
            and normalized_memballoon != live_memballoon)

        # --- Disk diff ---
        actual_disks = self.get_actual_disks(domain_name)
        # The disk the VM boots from, so it can be excluded from the
        # "attached but neither declared nor recorded" report rather than
        # shown to the operator as a stray every single run.
        root_disk_source = actual_disks[0]['source'] if actual_disks else None
        actual_targets = {d['target'] for d in actual_disks}
        actual_by_target = {d['target']: d for d in actual_disks}

        new_disks = []
        resize_disks = []

        for disk_config in (desired_disks or []):
            target = disk_config.get('target', DEFAULT_DISK_TARGET)
            desired_size = disk_config.get('size', 1024)
            expected_path = self._expected_disk_path(disk_config, workdir, disk_prefix)

            if target not in actual_targets:
                if os.path.exists(expected_path):
                    # Leftover image from a failed earlier run — the disk
                    # is neither new nor a resize. Attach the existing
                    # file as-is (skip create) rather than silently
                    # dropping the desired disk.
                    self.logger.warning(
                        f"disk {target} on {domain_name}: image "
                        f"{expected_path} exists but is not attached — "
                        f"attaching existing file (skipping create)")
                    new_disks.append({**disk_config, 'attach_only': True})
                else:
                    # new disk — not attached and file doesn't exist
                    new_disks.append(disk_config)
            elif target in actual_targets:
                # disk exists — check if resize needed
                actual_disk = actual_by_target[target]
                if desired_size > actual_disk['size_mb']:
                    resize_disks.append({
                        'target': target,
                        'source': actual_disk['source'],
                        'current_size_mb': actual_disk['size_mb'],
                        'desired_size_mb': desired_size
                    })
                elif desired_size < actual_disk['size_mb']:
                    self.logger.warning(
                        f"disk {target} on {domain_name}: desired size "
                        f"({desired_size}M) < actual size ({actual_disk['size_mb']}M). "
                        f"Shrinking is not supported, skipping."
                    )

        # --- Disk removals ---
        #
        # Decided against what boxman recorded attaching, never inferred
        # from what is attached: get_actual_disks() returns the root disk
        # too, so "attached but not declared" starts by removing it
        # (#164 F2). A domain with no ownership record yields nothing.
        disk_records = self.get_disk_records(domain_name)
        # Against the PERSISTENT definition, which is what
        # `detach-disk --config` edits. For a running domain the live view
        # can differ -- an earlier config-only replacement is enough -- and
        # deciding from it let a record for the disk running at a target
        # authorise detaching the different disk configured there (#164 F2
        # review, finding 3).
        persistent_disks = self.get_actual_disks(domain_name, inactive=True)
        removed_disks, refused_disk_removals = plan_disk_removals(
            disk_records, desired_disks or [], persistent_disks)
        # Against the view the add/resize path acts on -- the live one for
        # a running guest -- so preflight and reconciliation cannot
        # disagree about whether a target is occupied (#164 F2 review
        # round 2, finding 2).
        disk_conflicts = occupied_target_conflicts(
            disk_records, desired_disks or [], actual_disks,
            expected_paths={
                disk_logical_name(d):
                    self._expected_disk_path(d, workdir, disk_prefix)
                for d in (desired_disks or [])
            })
        unowned = unowned_disks(
            disk_records, desired_disks or [], persistent_disks,
            root_source=(persistent_disks[0]['source']
                         if persistent_disks else root_disk_source))

        # --- CDROM diff ---
        #
        # Explicit targets are matched *before* source membership. The other
        # order meant that when the desired ISO happened to be attached at
        # some other target, no swap was generated and the requested target
        # was simply removed: with hdc=A.iso and hdd=B.iso attached and
        # hdc=B.iso desired, hdc was detached and B.iso left on hdd. Swapping
        # two ISOs between explicit targets produced no changes at all
        # (#164 FB-5).
        actual_cdroms = self.get_actual_cdroms(domain_name)
        actual_by_target = {
            c['target']: c for c in actual_cdroms if c.get('target')
        }

        new_cdroms = []
        changed_cdroms = []
        # Actual drives accounted for by a desired entry. Anything left over
        # is what gets removed — computed from what was *matched* rather than
        # from source membership, so a drive holding the right media at the
        # wrong target is still reconciled.
        matched_targets: set = set()
        claimed_targets: set = set()

        entries = []
        for cdrom_config in (desired_cdroms or []):
            raw_source = cdrom_config.get('source')
            if not raw_source:
                # os.path.abspath('') is the current working directory. A
                # cdrom entry that had not been resolved was therefore turned
                # into a plausible-looking path matching nothing, so the ISO
                # genuinely attached to the guest fell into removed_cdroms and
                # was detached (#164 FB-5). Refuse instead of inventing a path.
                raise ProvisionError(
                    f"cdrom entry {cdrom_config!r} on domain '{domain_name}' "
                    f"has no resolved source. Declared media must be resolved "
                    f"to a local path before it can be compared with what is "
                    f"attached; refusing to guess one.")
            entries.append(
                (cdrom_config,
                 os.path.abspath(os.path.expanduser(raw_source))))

        # Every explicit target is reserved before any matching happens.
        # Doing it inside a single loop made the result depend on declaration
        # order: a targetless entry could match the very drive a later
        # explicit entry was about to overwrite, so with hdc=a.iso and
        # hdd=b.iso attached and `[{source: a.iso}, {target: hdc, source:
        # b.iso}]` declared, hdc became b.iso, hdd was removed, and a.iso —
        # still declared — ended up attached nowhere, with the update
        # reporting success (#164 FB-5).
        for cdrom_config, _source in entries:
            target = cdrom_config.get('target')
            if not target:
                continue
            if target in claimed_targets:
                raise ProvisionError(
                    f"domain '{domain_name}' declares more than one cdrom on "
                    f"target '{target}'. Each target holds one device.")
            claimed_targets.add(target)

        for cdrom_config, source in entries:
            target = cdrom_config.get('target')

            if target:
                actual_for_target = actual_by_target.get(target)
                if actual_for_target is None:
                    new_cdroms.append(cdrom_config)
                    continue
                matched_targets.add(target)
                if actual_for_target['source'] != source:
                    # Covers an empty drive too (source None): inserting media
                    # into a drive that already exists is a media change, not
                    # a second device.
                    changed_cdroms.append({'target': target, 'source': source})
                continue

            # Targetless: match by source, one-to-one, and never against a
            # drive some explicit entry has reserved.
            actual = next(
                (c for c in actual_cdroms
                 if c['source'] == source
                 and c['target'] not in matched_targets
                 and c['target'] not in claimed_targets), None)
            if actual is None:
                new_cdroms.append(cdrom_config)
            else:
                matched_targets.add(actual['target'])

        removed_cdroms = [
            c for c in actual_cdroms
            if c['target'] not in matched_targets
            # An empty drive holds no media to remove, and dropping it would
            # silently change the domain's topology.
            and c.get('source') is not None
        ]

        # --- Shared folder diff ---
        # What boxman *configures* is the persistent definition, so that is
        # what the reconcile compares against. Reading the live domain
        # instead meant an attachment that had fallen back to config-only
        # was invisible next run: it was proposed again, and libvirt
        # rejected the duplicate persistent target -- so even a follow-up
        # `update --restart` failed before it could restart. It also missed
        # cancellation entirely: a pending share removed from the config
        # before the restart produced no removal, because it had never
        # appeared live (#164 C1 review, finding 7).
        persistent_folders = self.get_actual_shared_folders(
            domain_name, inactive=True)
        live_folders = (
            self.get_actual_shared_folders(domain_name)
            if vm_state in self._LIVE_DOMAIN_STATES else persistent_folders)

        def _normalised(folder):
            return (
                os.path.abspath(os.path.expanduser(folder.get('host_path', ''))),
                bool(folder.get('readonly', False)),
            )

        persistent_by_name = {f['name']: f for f in persistent_folders}
        live_by_name = {f['name']: f for f in live_folders}

        new_shared_folders = []
        changed_shared_folders = []
        desired_folder_names = set()
        shared_folders_restart_pending = False

        for folder_config in (desired_shared_folders or []):
            name = folder_config.get('name', '')
            desired_folder_names.add(name)
            desired_state = _normalised(folder_config)

            configured = persistent_by_name.get(name)
            if configured is None:
                new_shared_folders.append(folder_config)
            elif _normalised(configured) != desired_state:
                changed_shared_folders.append(folder_config)

            # ...and, separately, whether the *live* domain reflects it yet
            attached = live_by_name.get(name)
            if attached is None or _normalised(attached) != desired_state:
                shared_folders_restart_pending = True

        removed_shared_folders = [
            f for f in persistent_folders
            if f['name'] not in desired_folder_names
        ]
        # a share still live but no longer configured also waits for a boot
        if any(f['name'] not in desired_folder_names for f in live_folders):
            shared_folders_restart_pending = True

        return {
            'cpu_changed': cpu_changed,
            'desired_cpus': desired_cpus,
            'actual_cpus': actual_cpus,
            'max_vcpus_changed': max_vcpus_changed,
            'desired_max_vcpus': desired_max_vcpus,
            'actual_max_vcpus': actual_max_vcpus,
            'memory_changed': memory_changed,
            'desired_memory_mb': desired_memory_mb,
            'actual_memory_mb': actual_memory_mb,
            'max_memory_changed': max_memory_changed,
            'desired_max_memory_mb': desired_max_memory_mb,
            'actual_max_memory_mb': actual_max_memory_mb,
            'new_disks': new_disks,
            'resize_disks': resize_disks,
            'removed_disks': removed_disks,
            'has_disk_records': disk_records is not None,
            'disk_conflicts': disk_conflicts,
            'refused_disk_removals': refused_disk_removals,
            'unowned_disks': unowned,
            'new_cdroms': new_cdroms,
            'removed_cdroms': removed_cdroms,
            'changed_cdroms': changed_cdroms,
            'shared_folders_restart_pending': shared_folders_restart_pending,
            'new_shared_folders': new_shared_folders,
            'removed_shared_folders': removed_shared_folders,
            'changed_shared_folders': changed_shared_folders,
            'memballoon_changed': memballoon_changed,
            'memballoon_restart_pending': memballoon_restart_pending,
            'desired_memballoon': normalized_memballoon,
            'actual_memballoon': actual_memballoon,
            'live_memballoon': live_memballoon,
            'vm_state': vm_state
        }
