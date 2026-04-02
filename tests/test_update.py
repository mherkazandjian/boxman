"""
Tests for the boxman update feature: VMStateDiffer, hot/cold CPU/memory,
disk resize, and update orchestration logic.
"""

import os
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from boxman.providers.libvirt.vm_differ import VMStateDiffer
from boxman.providers.libvirt.virsh_edit import VirshEdit
from boxman.providers.libvirt.disk import DiskManager


# ---------------------------------------------------------------------------
# Sample XML for testing
# ---------------------------------------------------------------------------
SAMPLE_DOMAIN_XML = """\
<domain type='kvm'>
  <name>test-vm</name>
  <memory unit='KiB'>2097152</memory>
  <currentMemory unit='KiB'>2097152</currentMemory>
  <vcpu placement='static'>4</vcpu>
  <cpu mode='host-passthrough'>
    <topology sockets='1' cores='2' threads='2'/>
  </cpu>
</domain>
"""

SAMPLE_DOMAIN_XML_WITH_MAX = """\
<domain type='kvm'>
  <name>test-vm</name>
  <memory unit='KiB'>16777216</memory>
  <currentMemory unit='KiB'>2097152</currentMemory>
  <vcpu placement='static' current='4'>16</vcpu>
  <cpu mode='host-passthrough'>
    <topology sockets='1' cores='2' threads='2'/>
  </cpu>
</domain>
"""

SAMPLE_DOMBLKLIST_OUTPUT = """\
 Type   Device   Target   Source
-------------------------------------------
 file   disk     vda      /var/lib/libvirt/images/test-vm.qcow2
 file   disk     vdb      /data/test-vm_disk01.qcow2
 file   cdrom    hda      /data/seed.iso
"""


# ---------------------------------------------------------------------------
# VMStateDiffer tests
# ---------------------------------------------------------------------------
class TestVMStateDiffer:

    def _make_differ(self):
        return VMStateDiffer(provider_config={'uri': 'qemu:///system'})

    @patch.object(VirshEdit, 'get_domain_xml', return_value=SAMPLE_DOMAIN_XML)
    def test_get_actual_cpu(self, mock_xml):
        differ = self._make_differ()
        cpu = differ.get_actual_cpu('test-vm')
        assert cpu == {
            'sockets': 1,
            'cores': 2,
            'threads': 2,
            'total_vcpus': 4,
            'current_vcpus': 4,  # no @current attr → falls back to total
        }

    @patch.object(VirshEdit, 'get_domain_xml', return_value=SAMPLE_DOMAIN_XML_WITH_MAX)
    def test_get_actual_cpu_with_max(self, mock_xml):
        """When //vcpu has current='4' and text=16, current_vcpus should be 4."""
        differ = self._make_differ()
        cpu = differ.get_actual_cpu('test-vm')
        assert cpu['total_vcpus'] == 16
        assert cpu['current_vcpus'] == 4

    @patch.object(VirshEdit, 'get_domain_xml', return_value=SAMPLE_DOMAIN_XML)
    def test_get_actual_memory_mb(self, mock_xml):
        differ = self._make_differ()
        mem = differ.get_actual_memory_mb('test-vm')
        assert mem == 2048  # 2097152 KiB / 1024

    @patch.object(VirshEdit, 'get_domain_xml', return_value=SAMPLE_DOMAIN_XML)
    def test_get_max_vcpus(self, mock_xml):
        differ = self._make_differ()
        assert differ.get_max_vcpus('test-vm') == 4

    @patch.object(VirshEdit, 'get_domain_xml', return_value=SAMPLE_DOMAIN_XML)
    def test_get_max_memory_mb(self, mock_xml):
        differ = self._make_differ()
        assert differ.get_max_memory_mb('test-vm') == 2048

    def test_expected_disk_path_with_prefix(self):
        config = {'name': 'disk01', 'driver': {'type': 'qcow2'}}
        path = VMStateDiffer._expected_disk_path(config, '/data', 'myvm')
        assert path == '/data/myvm_disk01.qcow2'

    def test_expected_disk_path_without_prefix(self):
        config = {'name': 'disk01', 'driver': {'type': 'qcow2'}}
        path = VMStateDiffer._expected_disk_path(config, '/data', '')
        assert path == '/data/disk01.qcow2'

    def test_expected_disk_path_default_driver(self):
        config = {'name': 'data'}
        path = VMStateDiffer._expected_disk_path(config, '/tmp', 'vm1')
        assert path == '/tmp/vm1_data.qcow2'

    @patch.object(VMStateDiffer, 'get_vm_state', return_value='running')
    @patch.object(VMStateDiffer, 'get_actual_cpu', return_value={
        'sockets': 1, 'cores': 2, 'threads': 2, 'total_vcpus': 4, 'current_vcpus': 4})
    @patch.object(VMStateDiffer, 'get_actual_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_max_vcpus', return_value=4)
    @patch.object(VMStateDiffer, 'get_max_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_actual_disks', return_value=[
        {'target': 'vdb', 'source': '/data/vm_disk01.qcow2', 'size_mb': 2048}
    ])
    @patch.object(VMStateDiffer, 'get_actual_cdroms', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_shared_folders', return_value=[])
    def test_diff_vm_no_changes(self, mock_folders, mock_cdroms, mock_disks,
                                 mock_max_mem, mock_max_cpu,
                                 mock_mem, mock_cpu, mock_state):
        differ = self._make_differ()
        diff = differ.diff_vm(
            domain_name='test-vm',
            desired_cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            desired_memory_mb=2048,
            desired_disks=[{
                'name': 'disk01', 'target': 'vdb', 'size': 2048,
                'driver': {'name': 'qemu', 'type': 'qcow2'}
            }],
            workdir='/data',
            disk_prefix='vm'
        )
        assert diff['cpu_changed'] is False
        assert diff['memory_changed'] is False
        assert diff['new_disks'] == []
        assert diff['resize_disks'] == []
        assert diff['vm_state'] == 'running'

    @patch.object(VMStateDiffer, 'get_vm_state', return_value='running')
    @patch.object(VMStateDiffer, 'get_actual_cpu', return_value={
        'sockets': 1, 'cores': 2, 'threads': 2, 'total_vcpus': 4, 'current_vcpus': 4})
    @patch.object(VMStateDiffer, 'get_actual_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_max_vcpus', return_value=4)
    @patch.object(VMStateDiffer, 'get_max_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_actual_disks', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_cdroms', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_shared_folders', return_value=[])
    def test_diff_vm_cpu_changed(self, mock_folders, mock_cdroms, mock_disks,
                                  mock_max_mem, mock_max_cpu,
                                  mock_mem, mock_cpu, mock_state):
        differ = self._make_differ()
        diff = differ.diff_vm(
            domain_name='test-vm',
            desired_cpus={'sockets': 1, 'cores': 4, 'threads': 2},
            desired_memory_mb=2048,
            desired_disks=[],
            workdir='/data',
            disk_prefix='vm'
        )
        assert diff['cpu_changed'] is True
        assert diff['memory_changed'] is False

    @patch.object(VMStateDiffer, 'get_vm_state', return_value='shut off')
    @patch.object(VMStateDiffer, 'get_actual_cpu', return_value={
        'sockets': 1, 'cores': 2, 'threads': 2, 'total_vcpus': 4, 'current_vcpus': 4})
    @patch.object(VMStateDiffer, 'get_actual_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_max_vcpus', return_value=4)
    @patch.object(VMStateDiffer, 'get_max_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_actual_disks', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_cdroms', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_shared_folders', return_value=[])
    def test_diff_vm_memory_changed(self, mock_folders, mock_cdroms, mock_disks,
                                     mock_max_mem, mock_max_cpu,
                                     mock_mem, mock_cpu, mock_state):
        differ = self._make_differ()
        diff = differ.diff_vm(
            domain_name='test-vm',
            desired_cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            desired_memory_mb=4096,
            desired_disks=[],
            workdir='/data',
            disk_prefix='vm'
        )
        assert diff['cpu_changed'] is False
        assert diff['memory_changed'] is True
        assert diff['desired_memory_mb'] == 4096
        assert diff['actual_memory_mb'] == 2048

    @patch.object(VMStateDiffer, 'get_vm_state', return_value='running')
    @patch.object(VMStateDiffer, 'get_actual_cpu', return_value={
        'sockets': 1, 'cores': 2, 'threads': 2, 'total_vcpus': 4, 'current_vcpus': 4})
    @patch.object(VMStateDiffer, 'get_actual_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_max_vcpus', return_value=4)
    @patch.object(VMStateDiffer, 'get_max_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_actual_disks', return_value=[
        {'target': 'vdb', 'source': '/data/vm_disk01.qcow2', 'size_mb': 2048}
    ])
    @patch.object(VMStateDiffer, 'get_actual_cdroms', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_shared_folders', return_value=[])
    def test_diff_vm_new_disk(self, mock_folders, mock_cdroms, mock_disks,
                               mock_max_mem, mock_max_cpu,
                               mock_mem, mock_cpu, mock_state):
        differ = self._make_differ()
        diff = differ.diff_vm(
            domain_name='test-vm',
            desired_cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            desired_memory_mb=2048,
            desired_disks=[
                {'name': 'disk01', 'target': 'vdb', 'size': 2048,
                 'driver': {'name': 'qemu', 'type': 'qcow2'}},
                {'name': 'disk02', 'target': 'vdc', 'size': 4096,
                 'driver': {'name': 'qemu', 'type': 'qcow2'}}
            ],
            workdir='/data',
            disk_prefix='vm'
        )
        assert len(diff['new_disks']) == 1
        assert diff['new_disks'][0]['name'] == 'disk02'
        assert diff['resize_disks'] == []

    @patch.object(VMStateDiffer, 'get_vm_state', return_value='running')
    @patch.object(VMStateDiffer, 'get_actual_cpu', return_value={
        'sockets': 1, 'cores': 2, 'threads': 2, 'total_vcpus': 4, 'current_vcpus': 4})
    @patch.object(VMStateDiffer, 'get_actual_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_max_vcpus', return_value=4)
    @patch.object(VMStateDiffer, 'get_max_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_actual_disks', return_value=[
        {'target': 'vdb', 'source': '/data/vm_disk01.qcow2', 'size_mb': 2048}
    ])
    @patch.object(VMStateDiffer, 'get_actual_cdroms', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_shared_folders', return_value=[])
    def test_diff_vm_resize_disk(self, mock_folders, mock_cdroms, mock_disks,
                                  mock_max_mem, mock_max_cpu,
                                  mock_mem, mock_cpu, mock_state):
        differ = self._make_differ()
        diff = differ.diff_vm(
            domain_name='test-vm',
            desired_cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            desired_memory_mb=2048,
            desired_disks=[
                {'name': 'disk01', 'target': 'vdb', 'size': 4096,
                 'driver': {'name': 'qemu', 'type': 'qcow2'}}
            ],
            workdir='/data',
            disk_prefix='vm'
        )
        assert diff['new_disks'] == []
        assert len(diff['resize_disks']) == 1
        assert diff['resize_disks'][0]['target'] == 'vdb'
        assert diff['resize_disks'][0]['current_size_mb'] == 2048
        assert diff['resize_disks'][0]['desired_size_mb'] == 4096

    @patch.object(VMStateDiffer, 'get_vm_state', return_value='running')
    @patch.object(VMStateDiffer, 'get_actual_cpu', return_value={
        'sockets': 1, 'cores': 2, 'threads': 2, 'total_vcpus': 4, 'current_vcpus': 4})
    @patch.object(VMStateDiffer, 'get_actual_memory_mb', return_value=4096)
    @patch.object(VMStateDiffer, 'get_max_vcpus', return_value=4)
    @patch.object(VMStateDiffer, 'get_max_memory_mb', return_value=4096)
    @patch.object(VMStateDiffer, 'get_actual_disks', return_value=[
        {'target': 'vdb', 'source': '/data/vm_disk01.qcow2', 'size_mb': 4096}
    ])
    @patch.object(VMStateDiffer, 'get_actual_cdroms', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_shared_folders', return_value=[])
    def test_diff_vm_shrink_disk_ignored(self, mock_folders, mock_cdroms, mock_disks,
                                          mock_max_mem, mock_max_cpu,
                                          mock_mem, mock_cpu, mock_state):
        """Shrinking disks should be skipped with a warning."""
        differ = self._make_differ()
        diff = differ.diff_vm(
            domain_name='test-vm',
            desired_cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            desired_memory_mb=4096,
            desired_disks=[
                {'name': 'disk01', 'target': 'vdb', 'size': 2048,
                 'driver': {'name': 'qemu', 'type': 'qcow2'}}
            ],
            workdir='/data',
            disk_prefix='vm'
        )
        # shrink should not be in resize_disks
        assert diff['resize_disks'] == []
        assert diff['new_disks'] == []


# ---------------------------------------------------------------------------
# VirshEdit hot methods tests
# ---------------------------------------------------------------------------
class TestVirshEditHotMethods:

    def _make_editor(self):
        return VirshEdit(provider_config={'uri': 'qemu:///system'})

    @patch.object(VirshEdit, '__init__', lambda self, **kwargs: setattr(self, 'virsh', MagicMock()) or setattr(self, 'logger', MagicMock()))
    def test_hot_set_vcpus_success(self):
        editor = VirshEdit.__new__(VirshEdit)
        editor.virsh = MagicMock()
        editor.logger = MagicMock()
        editor.virsh.execute.return_value = MagicMock(ok=True)

        result = editor.hot_set_vcpus('test-vm', 8)
        assert result is True
        editor.virsh.execute.assert_called_once_with(
            'setvcpus', 'test-vm', '8', '--live', '--config')

    @patch.object(VirshEdit, '__init__', lambda self, **kwargs: setattr(self, 'virsh', MagicMock()) or setattr(self, 'logger', MagicMock()))
    def test_hot_set_vcpus_failure(self):
        editor = VirshEdit.__new__(VirshEdit)
        editor.virsh = MagicMock()
        editor.logger = MagicMock()
        editor.virsh.execute.return_value = MagicMock(ok=False, stderr='error')

        result = editor.hot_set_vcpus('test-vm', 8)
        assert result is False

    @patch.object(VirshEdit, '__init__', lambda self, **kwargs: setattr(self, 'virsh', MagicMock()) or setattr(self, 'logger', MagicMock()))
    def test_hot_set_memory_success(self):
        editor = VirshEdit.__new__(VirshEdit)
        editor.virsh = MagicMock()
        editor.logger = MagicMock()
        editor.virsh.execute.return_value = MagicMock(ok=True)

        result = editor.hot_set_memory('test-vm', 4096)
        assert result is True
        editor.virsh.execute.assert_called_once_with(
            'setmem', 'test-vm', str(4096 * 1024), '--live', '--config')

    @patch.object(VirshEdit, '__init__', lambda self, **kwargs: setattr(self, 'virsh', MagicMock()) or setattr(self, 'logger', MagicMock()))
    def test_hot_set_memory_failure(self):
        editor = VirshEdit.__new__(VirshEdit)
        editor.virsh = MagicMock()
        editor.logger = MagicMock()
        editor.virsh.execute.return_value = MagicMock(ok=False, stderr='error')

        result = editor.hot_set_memory('test-vm', 4096)
        assert result is False


# ---------------------------------------------------------------------------
# VirshEdit configure_cpu_memory with max values tests
# ---------------------------------------------------------------------------
class TestConfigureCpuMemoryMaxValues:

    def _make_editor(self):
        editor = VirshEdit.__new__(VirshEdit)
        editor.virsh = MagicMock()
        editor.logger = MagicMock()
        return editor

    @patch.object(VirshEdit, 'redefine_domain', return_value=True)
    @patch.object(VirshEdit, 'get_domain_xml', return_value=SAMPLE_DOMAIN_XML)
    def test_configure_with_max_vcpus(self, mock_xml, mock_redefine):
        editor = self._make_editor()
        result = editor.configure_cpu_memory(
            'test-vm',
            cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            memory_mb=2048,
            max_vcpus=16
        )
        assert result is True
        # check the XML passed to redefine_domain
        xml_arg = mock_redefine.call_args[0][1]
        from lxml import etree
        tree = etree.fromstring(xml_arg.encode('utf-8'))
        vcpu = tree.xpath('//vcpu')[0]
        assert vcpu.text == '16'
        assert vcpu.get('current') == '4'
        # topology sockets must be scaled to match max: 16 / (2*2) = 4
        topo = tree.xpath('//cpu/topology')[0]
        assert topo.get('sockets') == '4'
        assert topo.get('cores') == '2'
        assert topo.get('threads') == '2'

    @patch.object(VirshEdit, 'redefine_domain', return_value=True)
    @patch.object(VirshEdit, 'get_domain_xml', return_value=SAMPLE_DOMAIN_XML)
    def test_configure_with_max_memory(self, mock_xml, mock_redefine):
        editor = self._make_editor()
        result = editor.configure_cpu_memory(
            'test-vm',
            cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            memory_mb=2048,
            max_memory_mb=16384
        )
        assert result is True
        xml_arg = mock_redefine.call_args[0][1]
        from lxml import etree
        tree = etree.fromstring(xml_arg.encode('utf-8'))
        memory = tree.xpath('//memory')[0]
        current_memory = tree.xpath('//currentMemory')[0]
        assert int(memory.text) == 16384 * 1024
        assert int(current_memory.text) == 2048 * 1024

    @patch.object(VirshEdit, 'redefine_domain', return_value=True)
    @patch.object(VirshEdit, 'get_domain_xml', return_value=SAMPLE_DOMAIN_XML)
    def test_configure_without_max_values_backward_compat(self, mock_xml, mock_redefine):
        """When max values are not specified, behavior matches legacy: max == current."""
        editor = self._make_editor()
        result = editor.configure_cpu_memory(
            'test-vm',
            cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            memory_mb=2048
        )
        assert result is True
        xml_arg = mock_redefine.call_args[0][1]
        from lxml import etree
        tree = etree.fromstring(xml_arg.encode('utf-8'))
        vcpu = tree.xpath('//vcpu')[0]
        assert vcpu.text == '4'
        assert vcpu.get('current') is None  # no current attr when max == current
        memory = tree.xpath('//memory')[0]
        current_memory = tree.xpath('//currentMemory')[0]
        assert memory.text == current_memory.text

    @patch.object(VirshEdit, 'redefine_domain', return_value=True)
    @patch.object(VirshEdit, 'get_domain_xml', return_value=SAMPLE_DOMAIN_XML)
    def test_configure_max_less_than_current_clamps(self, mock_xml, mock_redefine):
        """max_vcpus < total_vcpus should clamp max to current."""
        editor = self._make_editor()
        result = editor.configure_cpu_memory(
            'test-vm',
            cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            memory_mb=4096,
            max_vcpus=2,
            max_memory_mb=1024
        )
        assert result is True
        xml_arg = mock_redefine.call_args[0][1]
        from lxml import etree
        tree = etree.fromstring(xml_arg.encode('utf-8'))
        vcpu = tree.xpath('//vcpu')[0]
        assert vcpu.text == '4'  # clamped to total_vcpus=4
        assert vcpu.get('current') is None  # max == current, no attr
        memory = tree.xpath('//memory')[0]
        assert int(memory.text) == 4096 * 1024  # clamped to memory_mb


# ---------------------------------------------------------------------------
# VMStateDiffer max values diff tests
# ---------------------------------------------------------------------------
class TestVMStateDifferMaxDiff:

    def _make_differ(self):
        return VMStateDiffer(provider_config={'uri': 'qemu:///system'})

    @patch.object(VirshEdit, 'get_domain_xml', return_value=SAMPLE_DOMAIN_XML_WITH_MAX)
    def test_get_actual_memory_reads_current_memory(self, mock_xml):
        """get_actual_memory_mb should read //currentMemory, not //memory."""
        differ = self._make_differ()
        mem = differ.get_actual_memory_mb('test-vm')
        assert mem == 2048  # currentMemory=2097152 KiB = 2048 MiB

    @patch.object(VirshEdit, 'get_domain_xml', return_value=SAMPLE_DOMAIN_XML_WITH_MAX)
    def test_get_max_memory_reads_memory_element(self, mock_xml):
        differ = self._make_differ()
        max_mem = differ.get_max_memory_mb('test-vm')
        assert max_mem == 16384  # memory=16777216 KiB = 16384 MiB

    @patch.object(VirshEdit, 'get_domain_xml', return_value=SAMPLE_DOMAIN_XML_WITH_MAX)
    def test_get_max_vcpus_reads_vcpu_text(self, mock_xml):
        differ = self._make_differ()
        max_vcpus = differ.get_max_vcpus('test-vm')
        assert max_vcpus == 16

    @patch.object(VMStateDiffer, 'get_vm_state', return_value='running')
    @patch.object(VMStateDiffer, 'get_actual_cpu', return_value={
        'sockets': 1, 'cores': 2, 'threads': 2, 'total_vcpus': 4, 'current_vcpus': 4})
    @patch.object(VMStateDiffer, 'get_actual_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_max_vcpus', return_value=16)
    @patch.object(VMStateDiffer, 'get_max_memory_mb', return_value=16384)
    @patch.object(VMStateDiffer, 'get_actual_disks', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_cdroms', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_shared_folders', return_value=[])
    def test_diff_vm_no_max_change(self, mock_folders, mock_cdroms, mock_disks,
                                    mock_max_mem, mock_max_cpu,
                                    mock_mem, mock_cpu, mock_state):
        differ = self._make_differ()
        diff = differ.diff_vm(
            domain_name='test-vm',
            desired_cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            desired_memory_mb=2048,
            desired_disks=[],
            workdir='/data',
            disk_prefix='vm',
            desired_max_vcpus=16,
            desired_max_memory_mb=16384
        )
        assert diff['max_vcpus_changed'] is False
        assert diff['max_memory_changed'] is False

    @patch.object(VMStateDiffer, 'get_vm_state', return_value='running')
    @patch.object(VMStateDiffer, 'get_actual_cpu', return_value={
        'sockets': 1, 'cores': 2, 'threads': 2, 'total_vcpus': 4, 'current_vcpus': 4})
    @patch.object(VMStateDiffer, 'get_actual_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_max_vcpus', return_value=4)
    @patch.object(VMStateDiffer, 'get_max_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_actual_disks', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_cdroms', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_shared_folders', return_value=[])
    def test_diff_vm_max_vcpus_changed(self, mock_folders, mock_cdroms, mock_disks,
                                       mock_max_mem, mock_max_cpu,
                                       mock_mem, mock_cpu, mock_state):
        differ = self._make_differ()
        diff = differ.diff_vm(
            domain_name='test-vm',
            desired_cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            desired_memory_mb=2048,
            desired_disks=[],
            workdir='/data',
            disk_prefix='vm',
            desired_max_vcpus=16
        )
        assert diff['max_vcpus_changed'] is True
        assert diff['desired_max_vcpus'] == 16
        assert diff['actual_max_vcpus'] == 4

    @patch.object(VMStateDiffer, 'get_vm_state', return_value='running')
    @patch.object(VMStateDiffer, 'get_actual_cpu', return_value={
        'sockets': 1, 'cores': 2, 'threads': 2, 'total_vcpus': 4, 'current_vcpus': 4})
    @patch.object(VMStateDiffer, 'get_actual_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_max_vcpus', return_value=4)
    @patch.object(VMStateDiffer, 'get_max_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_actual_disks', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_cdroms', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_shared_folders', return_value=[])
    def test_diff_vm_max_memory_changed(self, mock_folders, mock_cdroms, mock_disks,
                                        mock_max_mem, mock_max_cpu,
                                        mock_mem, mock_cpu, mock_state):
        differ = self._make_differ()
        diff = differ.diff_vm(
            domain_name='test-vm',
            desired_cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            desired_memory_mb=2048,
            desired_disks=[],
            workdir='/data',
            disk_prefix='vm',
            desired_max_memory_mb=16384
        )
        assert diff['max_memory_changed'] is True
        assert diff['desired_max_memory_mb'] == 16384
        assert diff['actual_max_memory_mb'] == 2048

    @patch.object(VMStateDiffer, 'get_vm_state', return_value='running')
    @patch.object(VMStateDiffer, 'get_actual_cpu', return_value={
        'sockets': 1, 'cores': 2, 'threads': 2, 'total_vcpus': 4, 'current_vcpus': 4})
    @patch.object(VMStateDiffer, 'get_actual_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_max_vcpus', return_value=4)
    @patch.object(VMStateDiffer, 'get_max_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_actual_disks', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_cdroms', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_shared_folders', return_value=[])
    def test_diff_vm_no_max_change_when_omitted(self, mock_folders, mock_cdroms, mock_disks,
                                                 mock_max_mem, mock_max_cpu,
                                                 mock_mem, mock_cpu, mock_state):
        """When max values are not specified in config, no max change should be detected."""
        differ = self._make_differ()
        diff = differ.diff_vm(
            domain_name='test-vm',
            desired_cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            desired_memory_mb=2048,
            desired_disks=[],
            workdir='/data',
            disk_prefix='vm'
            # no desired_max_vcpus or desired_max_memory_mb
        )
        assert diff['max_vcpus_changed'] is False
        assert diff['max_memory_changed'] is False

    @patch.object(VMStateDiffer, 'get_vm_state', return_value='running')
    @patch.object(VMStateDiffer, 'get_actual_cpu', return_value={
        'sockets': 4, 'cores': 2, 'threads': 2, 'total_vcpus': 16, 'current_vcpus': 4})
    @patch.object(VMStateDiffer, 'get_actual_memory_mb', return_value=2048)
    @patch.object(VMStateDiffer, 'get_max_vcpus', return_value=16)
    @patch.object(VMStateDiffer, 'get_max_memory_mb', return_value=16384)
    @patch.object(VMStateDiffer, 'get_actual_disks', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_cdroms', return_value=[])
    @patch.object(VMStateDiffer, 'get_actual_shared_folders', return_value=[])
    def test_diff_vm_scaled_sockets_no_false_cpu_change(
            self, mock_folders, mock_cdroms, mock_disks, mock_max_mem, mock_max_cpu,
            mock_mem, mock_cpu, mock_state):
        """When XML sockets are scaled for max_vcpus (4*2*2=16) but desired
        sockets=1 with same cores/threads (1*2*2=4 current), cpu_changed
        should be False — the effective vCPU count hasn't changed."""
        differ = self._make_differ()
        diff = differ.diff_vm(
            domain_name='test-vm',
            desired_cpus={'sockets': 1, 'cores': 2, 'threads': 2},
            desired_memory_mb=2048,
            desired_disks=[],
            workdir='/data',
            disk_prefix='vm',
            desired_max_vcpus=16,
            desired_max_memory_mb=16384
        )
        assert diff['cpu_changed'] is False
        assert diff['max_vcpus_changed'] is False


# ---------------------------------------------------------------------------
# DiskManager XML generation tests
# ---------------------------------------------------------------------------
class TestDiskManagerXml:

    def _make_manager(self):
        dm = DiskManager.__new__(DiskManager)
        dm.vm_name = 'test-vm'
        dm.logger = MagicMock()
        dm.provider_config = {'uri': 'qemu:///system'}
        return dm

    def test_generate_disk_xml_includes_bus_virtio(self):
        dm = self._make_manager()
        xml = dm._generate_disk_xml(
            disk_path='/data/disk.qcow2',
            target_dev='vdb',
            driver_name='qemu',
            driver_type='qcow2'
        )
        assert "bus='virtio'" in xml

    def test_generate_disk_xml_custom_bus(self):
        dm = self._make_manager()
        xml = dm._generate_disk_xml(
            disk_path='/data/disk.qcow2',
            target_dev='sdb',
            driver_name='qemu',
            driver_type='qcow2',
            bus='scsi'
        )
        assert "bus='scsi'" in xml


# ---------------------------------------------------------------------------
# DiskManager resize tests
# ---------------------------------------------------------------------------
class TestDiskManagerResize:

    def _make_manager(self):
        dm = DiskManager.__new__(DiskManager)
        dm.vm_name = 'test-vm'
        dm.logger = MagicMock()
        dm.provider_config = {'uri': 'qemu:///system'}
        return dm

    @patch('boxman.providers.libvirt.disk.LibVirtCommandBase')
    def test_resize_disk_offline_success(self, mock_cmd_cls):
        dm = self._make_manager()
        mock_instance = MagicMock()
        mock_cmd_cls.return_value = mock_instance
        mock_instance.execute_shell.return_value = MagicMock(ok=True)

        result = dm.resize_disk_offline('/data/disk.qcow2', 4096)
        assert result is True
        mock_instance.execute_shell.assert_called_once_with(
            'qemu-img resize /data/disk.qcow2 4096M')

    @patch('boxman.providers.libvirt.disk.LibVirtCommandBase')
    def test_resize_disk_offline_failure(self, mock_cmd_cls):
        dm = self._make_manager()
        mock_instance = MagicMock()
        mock_cmd_cls.return_value = mock_instance
        mock_instance.execute_shell.return_value = MagicMock(ok=False, stderr='err')

        result = dm.resize_disk_offline('/data/disk.qcow2', 4096)
        assert result is False

    def test_resize_disk_online_success(self):
        dm = self._make_manager()
        dm.execute = MagicMock(return_value=MagicMock(ok=True))

        result = dm.resize_disk_online('vdb', 4096)
        assert result is True
        dm.execute.assert_called_once_with(
            'blockresize', 'test-vm', 'vdb', '--size=4096M')

    def test_resize_disk_routes_to_online_when_running(self):
        dm = self._make_manager()
        dm.resize_disk_online = MagicMock(return_value=True)
        dm.resize_disk_offline = MagicMock(return_value=True)

        result = dm.resize_disk('/data/disk.qcow2', 'vdb', 4096, vm_running=True)
        assert result is True
        dm.resize_disk_online.assert_called_once_with('vdb', 4096)
        dm.resize_disk_offline.assert_not_called()

    def test_resize_disk_routes_to_offline_when_stopped(self):
        dm = self._make_manager()
        dm.resize_disk_online = MagicMock(return_value=True)
        dm.resize_disk_offline = MagicMock(return_value=True)

        result = dm.resize_disk('/data/disk.qcow2', 'vdb', 4096, vm_running=False)
        assert result is True
        dm.resize_disk_offline.assert_called_once_with('/data/disk.qcow2', 4096)
        dm.resize_disk_online.assert_not_called()


# ---------------------------------------------------------------------------
# VMStateDiffer disk parsing tests
# ---------------------------------------------------------------------------
class TestVMStateDifferDiskParsing:

    @patch.object(VMStateDiffer, '_get_disk_size_mb', return_value=10240)
    def test_get_actual_disks_parses_domblklist(self, mock_size):
        differ = VMStateDiffer.__new__(VMStateDiffer)
        differ.virsh = MagicMock()
        differ.logger = MagicMock()

        differ.virsh.execute.return_value = MagicMock(
            ok=True,
            stdout=SAMPLE_DOMBLKLIST_OUTPUT
        )

        disks = differ.get_actual_disks('test-vm')

        # should find vda and vdb (disk type), not hda (cdrom)
        assert len(disks) == 2
        assert disks[0]['target'] == 'vda'
        assert disks[0]['source'] == '/var/lib/libvirt/images/test-vm.qcow2'
        assert disks[1]['target'] == 'vdb'
        assert disks[1]['source'] == '/data/test-vm_disk01.qcow2'

        # verify _get_disk_size_mb is called with (domain_name, target)
        mock_size.assert_any_call('test-vm', 'vda')
        mock_size.assert_any_call('test-vm', 'vdb')

    def test_get_actual_disks_handles_failure(self):
        differ = VMStateDiffer.__new__(VMStateDiffer)
        differ.virsh = MagicMock()
        differ.logger = MagicMock()
        differ.virsh.execute.return_value = MagicMock(ok=False)

        disks = differ.get_actual_disks('test-vm')
        assert disks == []
