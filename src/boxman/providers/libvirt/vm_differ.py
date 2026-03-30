import os
from typing import Dict, Any, Optional, List

from boxman import log
from .commands import VirshCommand
from .virsh_edit import VirshEdit


class VMStateDiffer:
    """
    Compares desired VM state (from conf.yml) against actual state (from libvirt)
    and produces a structured diff describing what needs to change.
    """

    def __init__(self, provider_config: Optional[Dict[str, Any]] = None):
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

    def get_actual_cpu(self, domain_name: str) -> Dict[str, int]:
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

    def get_actual_disks(self, domain_name: str) -> List[Dict[str, Any]]:
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
        lines = result.stdout.strip().split('\n')
        # skip header lines (Type Device Target Source)
        for line in lines[2:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            disk_type = parts[0]   # file, block, etc.
            device = parts[1]      # disk, cdrom
            target = parts[2]      # vda, vdb, hda, sda
            source = parts[3]      # /path/to/file or -

            if device != 'disk' or disk_type != 'file' or source == '-':
                continue

            size_mb = self._get_disk_size_mb(domain_name, target)
            disks.append({
                'target': target,
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
    def _expected_disk_path(disk_config: Dict[str, Any],
                            workdir: str,
                            disk_prefix: str) -> str:
        """
        Compute the expected disk file path, matching DiskManager.configure_from_disk_config logic.
        """
        disk_name = disk_config.get("name", "disk")
        driver = disk_config.get("driver", {})
        driver_type = driver.get("type", "qcow2")

        if disk_prefix:
            disk_path = os.path.join(workdir, f"{disk_prefix}_{disk_name}.{driver_type}")
        else:
            disk_path = os.path.join(workdir, f"{disk_name}.{driver_type}")

        return os.path.expanduser(disk_path)

    def diff_vm(self,
                domain_name: str,
                desired_cpus: Optional[Dict[str, int]],
                desired_memory_mb: Optional[int],
                desired_disks: Optional[List[Dict[str, Any]]],
                workdir: str,
                disk_prefix: str,
                desired_max_vcpus: Optional[int] = None,
                desired_max_memory_mb: Optional[int] = None) -> Dict[str, Any]:
        """
        Compute the diff between desired config and actual VM state.

        Returns:
            Dict with keys:
              - cpu_changed, desired_cpus, actual_cpus
              - memory_changed, desired_memory_mb, actual_memory_mb
              - max_vcpus_changed, desired_max_vcpus, actual_max_vcpus
              - max_memory_changed, desired_max_memory_mb, actual_max_memory_mb
              - new_disks: list of disk configs to create and attach
              - resize_disks: list of dicts with target, source, current_size_mb, desired_size_mb
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

            if target not in actual_targets and not os.path.exists(expected_path):
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
            'vm_state': vm_state
        }
