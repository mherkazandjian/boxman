import os
from typing import Any

from boxman import log

from .commands import VirshCommand
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

    def get_actual_disks(self, domain_name: str) -> list[dict[str, Any]]:
        """
        Get actual disk info from virsh domblklist + virsh domblkinfo.

        Uses virsh domblkinfo to query disk sizes through the hypervisor,
        which works on running VMs (unlike qemu-img info which fails due
        to write locks held by QEMU).

        Returns:
            List of dicts with 'target', 'source', 'size_mb' keys.
            Only includes file-backed disk devices (excludes cdroms, etc.).
        """
        result = self.virsh.execute('domblklist', domain_name, '--details', warn=True)
        if not result.ok:
            self.logger.warning(f"failed to get disk list for {domain_name}")
            return []

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
        """
        from .disk import disk_path_for
        disk_name = disk_config.get("name", "disk")
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

    def get_actual_shared_folders(self, domain_name: str) -> list[dict[str, Any]]:
        """
        Get actual filesystem (shared folder) devices from domain XML.

        Returns:
            List of dicts with 'name', 'host_path', and 'readonly' keys.
        """
        from .shared_folder import SharedFolderManager
        return SharedFolderManager(
            domain_name,
            provider_config=self.provider_config).get_attached_shared_folders()

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
        actual_targets = {d['target'] for d in actual_disks}
        actual_by_target = {d['target']: d for d in actual_disks}

        new_disks = []
        resize_disks = []

        for disk_config in (desired_disks or []):
            target = disk_config.get('target', 'vdb')
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

        # --- CDROM diff ---
        actual_cdroms = self.get_actual_cdroms(domain_name)
        actual_cdrom_by_source = {c['source']: c for c in actual_cdroms}
        actual_cdrom_sources = set(actual_cdrom_by_source.keys())

        new_cdroms = []
        changed_cdroms = []
        desired_cdrom_sources = set()

        for cdrom_config in (desired_cdroms or []):
            source = os.path.abspath(os.path.expanduser(cdrom_config.get('source', '')))
            desired_cdrom_sources.add(source)

            if source not in actual_cdrom_sources:
                # check if there's an existing cdrom with a different source
                # that should be swapped (match by target if specified)
                target = cdrom_config.get('target')
                if target:
                    actual_for_target = next(
                        (c for c in actual_cdroms if c['target'] == target), None)
                    if actual_for_target and actual_for_target['source'] != source:
                        changed_cdroms.append({
                            'target': target,
                            'source': source,
                        })
                        continue
                new_cdroms.append(cdrom_config)

        removed_cdroms = [
            c for c in actual_cdroms
            if c['source'] not in desired_cdrom_sources
            and not any(
                ch['target'] == c['target'] for ch in changed_cdroms
            )
        ]

        # --- Shared folder diff ---
        actual_folders = self.get_actual_shared_folders(domain_name)
        actual_folder_by_name = {f['name']: f for f in actual_folders}
        actual_folder_names = set(actual_folder_by_name.keys())

        new_shared_folders = []
        changed_shared_folders = []
        desired_folder_names = set()

        for folder_config in (desired_shared_folders or []):
            name = folder_config.get('name', '')
            desired_folder_names.add(name)
            host_path = os.path.abspath(
                os.path.expanduser(folder_config.get('host_path', '')))
            readonly = folder_config.get('readonly', False)

            if name not in actual_folder_names:
                new_shared_folders.append(folder_config)
            else:
                actual = actual_folder_by_name[name]
                if (actual['host_path'] != host_path or
                        actual['readonly'] != readonly):
                    changed_shared_folders.append(folder_config)

        removed_shared_folders = [
            f for f in actual_folders
            if f['name'] not in desired_folder_names
        ]

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
            'new_cdroms': new_cdroms,
            'removed_cdroms': removed_cdroms,
            'changed_cdroms': changed_cdroms,
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
