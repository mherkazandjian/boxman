"""
Tests for the boxman update feature: VMStateDiffer, hot/cold CPU/memory,
disk resize, and update orchestration logic.
"""

import os
import types
from unittest.mock import MagicMock, call, patch

import pytest

from boxman.exceptions import ProvisionError
from boxman.manager import BoxmanManager
from boxman.providers.libvirt.disk import DiskManager
from boxman.providers.libvirt.disk_cleanup import remove_vm_disks
from boxman.providers.libvirt.disk_ownership import (
    ROLE_ADOPTED,
    ROLE_DATA,
    DiskRecord,
)
from boxman.providers.libvirt.virsh_edit import VirshEdit
from boxman.providers.libvirt.vm_differ import VMStateDiffer
from conftest import make_bare_manager

pytestmark = pytest.mark.unit

@pytest.fixture(autouse=True)
def _no_disk_ownership_records():
    """diff_vm probes boxman's disk ownership metadata via virsh.

    Default it to "this domain has none" -- what every domain predating
    the record looks like, and which proposes no removals. Tests about
    removals patch it themselves (#164 F2).
    """
    with patch.object(VMStateDiffer, 'get_disk_records', return_value=None):
        yield



@pytest.fixture(autouse=True)
def _default_memballoon_state():
    """diff_vm probes the balloon device via virsh; default it to the
    libvirt defaults (no freePageReporting, no autodeflate, no stats) for every
    test so the existing decorator stacks don't each need another patch."""
    with patch.object(VMStateDiffer, 'get_actual_memballoon',
                      return_value={'free_page_reporting': False,
                                    'autodeflate': False,
                                    'stats_period': None}):
        yield


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
            'setvcpus', 'test-vm', '8', '--live', '--config', warn=True)

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
            'setmem', 'test-vm', str(4096 * 1024), '--live', '--config', warn=True)

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
def _make_disk_manager():
    """Bare DiskManager instance (constructor bypassed) for unit tests."""
    dm = DiskManager.__new__(DiskManager)
    dm.vm_name = 'test-vm'
    dm.logger = MagicMock()
    dm.provider_config = {'uri': 'qemu:///system'}
    dm.virsh = MagicMock()
    return dm


class TestDiskManagerXml:

    def test_generate_disk_xml_includes_bus_virtio(self):
        dm = _make_disk_manager()
        xml = dm._generate_disk_xml(
            disk_path='/data/disk.qcow2',
            target_dev='vdb',
            driver_name='qemu',
            driver_type='qcow2'
        )
        assert "bus='virtio'" in xml

    def test_generate_disk_xml_custom_bus(self):
        dm = _make_disk_manager()
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

    @patch('boxman.providers.libvirt.disk.LibVirtCommandBase')
    def test_resize_disk_offline_success(self, mock_cmd_cls):
        dm = _make_disk_manager()
        mock_instance = MagicMock()
        mock_cmd_cls.return_value = mock_instance
        mock_instance.execute_shell.return_value = MagicMock(ok=True)

        result = dm.resize_disk_offline('/data/disk.qcow2', 4096)
        assert result is True
        mock_instance.execute_shell.assert_called_once_with(
            'qemu-img resize /data/disk.qcow2 4096M', warn=True)

    @patch('boxman.providers.libvirt.disk.LibVirtCommandBase')
    def test_resize_disk_offline_failure(self, mock_cmd_cls):
        dm = _make_disk_manager()
        mock_instance = MagicMock()
        mock_cmd_cls.return_value = mock_instance
        mock_instance.execute_shell.return_value = MagicMock(ok=False, stderr='err')

        result = dm.resize_disk_offline('/data/disk.qcow2', 4096)
        assert result is False

    def test_resize_disk_online_success(self):
        dm = _make_disk_manager()
        dm.virsh.execute.return_value = MagicMock(ok=True)

        result = dm.resize_disk_online('vdb', 4096)
        assert result is True
        dm.virsh.execute.assert_called_once_with(
            'blockresize', 'test-vm', 'vdb', '--size=4096M', warn=True)

    def test_resize_disk_routes_to_online_when_running(self):
        dm = _make_disk_manager()
        dm.resize_disk_online = MagicMock(return_value=True)
        dm.resize_disk_offline = MagicMock(return_value=True)

        result = dm.resize_disk('/data/disk.qcow2', 'vdb', 4096, vm_running=True)
        assert result is True
        dm.resize_disk_online.assert_called_once_with('vdb', 4096)
        dm.resize_disk_offline.assert_not_called()

    def test_resize_disk_routes_to_offline_when_stopped(self):
        dm = _make_disk_manager()
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

    def test_get_actual_disks_raises_on_failure(self):
        """A failed query must not read as "this domain has no disks".

        It used to return [], which is a real answer -- so every declared
        disk looked absent and the diff proposed attaching them all
        (#164 F2).
        """
        differ = VMStateDiffer.__new__(VMStateDiffer)
        differ.virsh = MagicMock()
        differ.logger = MagicMock()
        differ.virsh.execute.return_value = MagicMock(ok=False, stderr='boom')

        with pytest.raises(ProvisionError, match='could not list the disks'):
            differ.get_actual_disks('test-vm')

    def test_get_actual_disks_returns_empty_for_a_diskless_domain(self):
        """The other half: an empty list is still a valid answer."""
        differ = VMStateDiffer.__new__(VMStateDiffer)
        differ.virsh = MagicMock()
        differ.logger = MagicMock()
        differ.virsh.execute.return_value = MagicMock(
            ok=True, stdout='Target   Source\n----------------\n')

        assert differ.get_actual_disks('test-vm') == []


# ---------------------------------------------------------------------------
# Removed-VM destruction (update() path) — regression tests for the
# undefined-`workdir` NameError (issue #81)
# ---------------------------------------------------------------------------
class TestDestroyRemovedVm:

    def _make_manager(self):
        """Bare BoxmanManager instance with a mocked provider."""
        mgr = make_bare_manager()
        mgr.provider = MagicMock()
        mgr.provider.provider_config = {'uri': 'qemu:///system'}
        return mgr

    @patch("boxman.manager_parts.vms.VirshCommand")
    def test_vm_disk_dirs_from_domblklist(self, mock_virsh_cls):
        """Disk directories come from libvirt, cdrom sources are ignored."""
        mgr = self._make_manager()
        mock_virsh_cls.return_value.execute.return_value = MagicMock(
            ok=True, stdout=SAMPLE_DOMBLKLIST_OUTPUT)

        dirs = mgr._vm_disk_dirs('test-vm')

        assert dirs == ['/data', '/var/lib/libvirt/images']

    @patch("boxman.manager_parts.vms.VirshCommand")
    def test_vm_disk_dirs_fallback_when_query_fails(self, mock_virsh_cls):
        """If domblklist fails, fall back to the configured workdirs."""
        mgr = self._make_manager()
        mock_virsh_cls.return_value.execute.return_value = MagicMock(ok=False)
        mgr.collect_workdirs = MagicMock(return_value=['/fallback'])

        dirs = mgr._vm_disk_dirs('test-vm')

        assert dirs == ['/fallback']

    @patch("boxman.manager_parts.vms.VirshCommand")
    def test_destroy_removed_vm_sweeps_libvirt_disk_dirs(self, mock_virsh_cls):
        """destroy_disks runs once per libvirt-reported disk directory."""
        mgr = self._make_manager()
        mock_virsh_cls.return_value.execute.return_value = MagicMock(
            ok=True, stdout=SAMPLE_DOMBLKLIST_OUTPUT)
        # Not gone after the graceful undefine, gone after the forced one:
        # the disks may only be removed once absence is confirmed.
        mgr.provider.confirm_vm_absent.side_effect = [False, True]
        mgr._vm_disk_records = MagicMock(return_value=None)
        mgr.provider.backing_chain_files.return_value = [
            '/data/test-vm_disk01.qcow2']
        # the sample paths are absolute; never let a unit test unlink them
        mgr._remove_leftover_disk_files = MagicMock()

        mgr._destroy_removed_vm('test-vm')

        swept = sorted(
            c.args[0] for c in mgr.provider.destroy_disks.call_args_list)
        assert swept == ['/data', '/var/lib/libvirt/images']
        for c in mgr.provider.destroy_disks.call_args_list:
            # the attached extra disk is kept out of the name sweep; the
            # boot disk (<vm>.qcow2) is not
            assert c.kwargs == {'vm_name': 'test-vm', 'disks': [],
                                'protected': ['/data/test-vm_disk01.qcow2']}
        # only the extra disk's chain is read, the boot disk's is not
        mgr.provider.backing_chain_files.assert_called_once_with(
            ['/data/test-vm_disk01.qcow2'])
        # the domain's own disk list and ownership records, read before
        # undefining, are handed on
        mgr._remove_leftover_disk_files.assert_called_once()
        assert mgr._remove_leftover_disk_files.call_args.args[:3] == (
            'test-vm',
            ['/var/lib/libvirt/images/test-vm.qcow2',
             '/data/test-vm_disk01.qcow2'],
            None)
        # domain undefined both gracefully and with force
        assert mgr.provider.destroy_vm.call_args_list == [
            call('test-vm'),
            call('test-vm', force=True),
        ]

    @patch("boxman.manager_parts.vms.VirshCommand")
    def test_vm_disk_dirs_handles_paths_with_spaces(self, mock_virsh_cls):
        """A disk path containing spaces must survive domblklist parsing."""
        mgr = self._make_manager()
        mock_virsh_cls.return_value.execute.return_value = MagicMock(
            ok=True,
            stdout=(
                " Type   Device   Target   Source\n"
                "-------------------------------------------\n"
                " file   disk     vda      /vm images/test-vm.qcow2\n"
            )
        )

        dirs = mgr._vm_disk_dirs('test-vm')

        assert dirs == ['/vm images']

    @patch("boxman.manager_parts.vms.VirshCommand")
    def test_vm_disk_dirs_fallback_when_no_disks(self, mock_virsh_cls):
        """ok=True with zero disk rows (e.g. diskless VM) also falls back
        to the configured workdirs."""
        mgr = self._make_manager()
        mock_virsh_cls.return_value.execute.return_value = MagicMock(
            ok=True, stdout=" Type   Device   Target   Source\n---\n")
        mgr.collect_workdirs = MagicMock(return_value=['/fallback'])

        dirs = mgr._vm_disk_dirs('test-vm')

        assert dirs == ['/fallback']

    @patch("boxman.manager_parts.vms.VirshCommand")
    def test_destroy_removed_vm_queries_disks_before_undefining(
            self, mock_virsh_cls):
        """domblklist must run before destroy_vm — after the domain is
        undefined the query returns nothing (ordering regression guard)."""
        mgr = self._make_manager()
        virsh = mock_virsh_cls.return_value
        virsh.execute.return_value = MagicMock(
            ok=True, stdout=SAMPLE_DOMBLKLIST_OUTPUT)
        mgr.provider.confirm_vm_absent.side_effect = [False, True]
        mgr._vm_disk_records = MagicMock(return_value=None)
        mgr._remove_leftover_disk_files = MagicMock()

        parent = MagicMock()
        parent.attach_mock(virsh.execute, 'virsh_execute')
        parent.attach_mock(mgr._vm_disk_records, 'disk_records')
        parent.attach_mock(mgr.provider.destroy_vm, 'destroy_vm')

        mgr._destroy_removed_vm('test-vm')

        ordered = [c[0] for c in parent.mock_calls]
        assert ordered == [
            'virsh_execute', 'disk_records', 'destroy_vm', 'destroy_vm']
        # first destroy_vm is the graceful one, second is force=True
        assert parent.mock_calls[2] == call.destroy_vm('test-vm')
        assert parent.mock_calls[3] == call.destroy_vm('test-vm', force=True)

    @patch("boxman.manager_parts.vms.VirshCommand")
    def test_vm_disk_files_from_domblklist(self, mock_virsh_cls):
        """Disk files come from libvirt, cdrom sources are left out."""
        mgr = self._make_manager()
        mock_virsh_cls.return_value.execute.return_value = MagicMock(
            ok=True, stdout=SAMPLE_DOMBLKLIST_OUTPUT)

        assert mgr._vm_disk_files('test-vm') == [
            '/var/lib/libvirt/images/test-vm.qcow2',
            '/data/test-vm_disk01.qcow2']

    @patch("boxman.manager_parts.vms.VirshCommand")
    def test_vm_disk_files_empty_when_query_fails(self, mock_virsh_cls):
        mgr = self._make_manager()
        mock_virsh_cls.return_value.execute.return_value = MagicMock(ok=False)

        assert mgr._vm_disk_files('test-vm') == []

    @patch("boxman.manager_parts.vms.VirshCommand")
    def test_vm_disk_records_read_from_the_domain_metadata(
            self, mock_virsh_cls):
        mgr = self._make_manager()
        mock_virsh_cls.return_value.execute.return_value = MagicMock(
            ok=True, stderr='',
            stdout=('<disks><disk name="disk01" target="vdb" role="data" '
                    'source="/ws/test-vm_disk01.qcow2"/></disks>'))

        assert mgr._vm_disk_records('test-vm') == [DiskRecord(
            name='disk01', target='vdb', role=ROLE_DATA,
            source='/ws/test-vm_disk01.qcow2')]

    @patch("boxman.manager_parts.vms.VirshCommand")
    def test_vm_disk_records_none_when_the_domain_has_none(
            self, mock_virsh_cls):
        mgr = self._make_manager()
        mock_virsh_cls.return_value.execute.return_value = MagicMock(
            ok=False, stdout='',
            stderr='error: metadata not found: Requested metadata element '
                   'is not present')

        assert mgr._vm_disk_records('test-vm') is None

    @patch("boxman.manager_parts.vms.VirshCommand")
    def test_unreadable_vm_disk_records_are_none_and_reported(
            self, mock_virsh_cls):
        """Unreadable must not read as "boxman attached nothing"; None
        keeps every file."""
        mgr = self._make_manager()
        mock_virsh_cls.return_value.execute.return_value = MagicMock(
            ok=True, stderr='', stdout='<disks><disk name="x"')

        assert mgr._vm_disk_records('test-vm') is None
        mgr.logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# Extra disks libvirt left behind (issue #207)
# ---------------------------------------------------------------------------
class TestRemovedVmLeftoverDisks:
    """``update`` removing a VM left its extra disk behind and still exited
    0 (#207). ``undefine --remove-all-storage`` skips a disk its storage
    pool does not list ("not managed by libvirt") -- boxman creates extra
    disks with qemu-img, outside the pool -- and the name glob in
    destroy_disks cannot name them for a VM that is gone from conf.yml.
    Observed on the test-runner VM with
    ``boxes/tiny-libvirt-ubuntu-24.04-cloudinit``.

    What libvirt left is removed on recorded ownership, never on the file
    name: being attached and named ``<vm>_...`` proves neither that boxman
    created a file nor that nothing else uses it (#207 review, findings 1
    and 3)."""

    VM = 'bprj__demo__bprj_cluster_1_web'
    OTHER = 'bprj__demo__bprj_cluster_1_db'

    def _manager(self, workdir, attached, records, in_use=None):
        mgr = make_bare_manager(
            {'project': 'demo',
             'clusters': {'cluster_1': {'workdir': str(workdir)}}})
        mgr.provider = MagicMock()
        mgr.provider.confirm_vm_absent.return_value = True
        mgr.provider.disk_paths_in_use.return_value = in_use or {}
        mgr._vm_disk_files = MagicMock(
            return_value=[str(p) for p in attached])
        mgr._vm_disk_records = MagicMock(return_value=records)
        # the real name sweep, so that its ordering against the ownership
        # decision is exercised too (#212 review round 2, R2-1)
        mgr.provider.destroy_disks.side_effect = (
            lambda workdir, vm_name, disks, **kwargs:
                remove_vm_disks(workdir, vm_name, disks, **kwargs))
        # standalone images by default: each chain is just the file itself
        mgr.provider.backing_chain_files.side_effect = (
            lambda sources: sorted(str(s) for s in sources))
        return mgr

    @staticmethod
    def _record(name, path, role=ROLE_DATA, target='vdb'):
        return DiskRecord(name=name, target=target, role=role,
                          source=str(path))

    @staticmethod
    def _file(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'qcow2')
        return path

    @staticmethod
    def _warnings(mgr):
        return ' '.join(str(c.args[0])
                        for c in mgr.logger.warning.call_args_list)

    def test_a_recorded_data_disk_is_removed(self, tmp_path):
        extra = self._file(tmp_path / f'{self.VM}_disk01.qcow2')
        mgr = self._manager(tmp_path,
                            [tmp_path / f'{self.VM}.qcow2', extra],
                            [self._record('disk01', extra)])

        mgr._destroy_removed_vm(self.VM)

        assert not extra.exists()

    def test_an_attached_neighbour_named_like_an_extra_disk_is_kept(
            self, tmp_path):
        """VM ``web_2``'s boot disk, attached to ``web``, matches ``web_*``
        (the probe from the #207 review)."""
        extra = self._file(tmp_path / f'{self.VM}_disk01.qcow2')
        neighbour = self._file(tmp_path / f'{self.VM}_2.qcow2')
        mgr = self._manager(tmp_path, [extra, neighbour],
                            [self._record('disk01', extra)],
                            in_use={str(neighbour): f'{self.VM}_2'})

        mgr._destroy_removed_vm(self.VM)

        assert not extra.exists()
        assert neighbour.exists()
        assert str(neighbour) in self._warnings(mgr)

    def test_an_adopted_disk_is_kept(self, tmp_path):
        """attach_only: the image existed already, so boxman did not make
        it (disk_ownership.ROLE_ADOPTED)."""
        adopted = self._file(tmp_path / f'{self.VM}_data.qcow2')
        mgr = self._manager(tmp_path, [adopted],
                            [self._record('data', adopted, ROLE_ADOPTED)])

        mgr._destroy_removed_vm(self.VM)

        assert adopted.exists()
        assert str(adopted) in self._warnings(mgr)

    def test_a_domain_without_ownership_records_keeps_its_disks(
            self, tmp_path):
        """No metadata (a domain predating it): boxman does not know what
        it attached, which is never grounds for deleting anything."""
        extra = self._file(tmp_path / f'{self.VM}_disk01.qcow2')
        mgr = self._manager(tmp_path, [extra], records=None)

        mgr._destroy_removed_vm(self.VM)

        assert extra.exists()
        assert str(extra) in self._warnings(mgr)

    def test_a_disk_another_domain_uses_is_kept(self, tmp_path):
        """Directly or as a backing file: disk_paths_in_use covers both."""
        extra = self._file(tmp_path / f'{self.VM}_disk01.qcow2')
        mgr = self._manager(tmp_path, [extra],
                            [self._record('disk01', extra)],
                            in_use={str(extra): self.OTHER})

        mgr._destroy_removed_vm(self.VM)

        assert extra.exists()
        assert self.OTHER in self._warnings(mgr)

    def test_nothing_is_removed_when_other_domains_cannot_be_checked(
            self, tmp_path):
        extra = self._file(tmp_path / f'{self.VM}_disk01.qcow2')
        mgr = self._manager(tmp_path, [extra],
                            [self._record('disk01', extra)])
        mgr.provider.disk_paths_in_use.return_value = None

        mgr._destroy_removed_vm(self.VM)

        assert extra.exists()
        assert str(extra) in self._warnings(mgr)

    def test_a_disk_outside_every_cluster_workdir_is_kept(self, tmp_path):
        workdir = tmp_path / 'ws'
        workdir.mkdir()
        elsewhere = self._file(
            tmp_path / 'elsewhere' / f'{self.VM}_disk01.qcow2')
        mgr = self._manager(workdir, [elsewhere],
                            [self._record('disk01', elsewhere)])

        mgr._destroy_removed_vm(self.VM)

        assert elsewhere.exists()

    def test_a_symlinked_directory_cannot_lead_outside_the_workdir(
            self, tmp_path):
        workdir = tmp_path / 'ws'
        workdir.mkdir()
        outside = tmp_path / 'outside'
        target = self._file(outside / f'{self.VM}_disk01.qcow2')
        (workdir / 'sub').symlink_to(outside, target_is_directory=True)
        via_link = workdir / 'sub' / f'{self.VM}_disk01.qcow2'
        mgr = self._manager(workdir, [via_link],
                            [self._record('disk01', via_link)])

        mgr._destroy_removed_vm(self.VM)

        assert target.exists()

    def test_a_symlinked_disk_file_is_kept(self, tmp_path):
        """boxman never creates one; unlinking it would report a disk as
        removed while its data lives on elsewhere."""
        target = self._file(tmp_path / 'outside' / 'data.qcow2')
        link = tmp_path / f'{self.VM}_disk01.qcow2'
        link.symlink_to(target)
        mgr = self._manager(tmp_path, [link], [self._record('disk01', link)])

        mgr._destroy_removed_vm(self.VM)

        assert link.is_symlink()
        assert target.exists()

    def test_a_workdir_reached_through_a_symlink_is_still_its_own(
            self, tmp_path):
        real = tmp_path / 'real'
        real.mkdir()
        alias = tmp_path / 'alias'
        alias.symlink_to(real, target_is_directory=True)
        extra = self._file(alias / f'{self.VM}_disk01.qcow2')
        mgr = self._manager(alias, [extra], [self._record('disk01', extra)])

        mgr._destroy_removed_vm(self.VM)

        assert not (real / f'{self.VM}_disk01.qcow2').exists()

    def test_a_record_not_named_for_its_disk_is_not_trusted(self, tmp_path):
        """boxman names a data disk ``<vm>_<name>.<type>``; a record whose
        source says otherwise was not written by the attach path."""
        other = self._file(tmp_path / 'shared-data.qcow2')
        mgr = self._manager(tmp_path, [other],
                            [self._record('disk01', other)])

        mgr._destroy_removed_vm(self.VM)

        assert other.exists()
        assert str(other) in self._warnings(mgr)

    def test_a_file_replaced_after_the_capture_is_kept(self, tmp_path):
        extra = self._file(tmp_path / f'{self.VM}_disk01.qcow2')
        mgr = self._manager(tmp_path, [extra],
                            [self._record('disk01', extra)])

        def replace_during_undefine(*_args, **_kwargs):
            # a new inode at the same path, allocated while the old one
            # still exists so the number cannot be reused
            fresh = tmp_path / 'fresh'
            fresh.write_bytes(b'someone else')
            os.replace(fresh, extra)

        mgr.provider.destroy_vm.side_effect = replace_during_undefine

        mgr._destroy_removed_vm(self.VM)

        assert extra.read_bytes() == b'someone else'
        # the replacement went back under its name; nothing is left over
        assert sorted(p.name for p in tmp_path.iterdir()) == [extra.name]

    def test_an_adopted_disk_named_like_a_memory_snapshot_is_kept(
            self, tmp_path):
        """Logical name ``snapshot_data`` gives ``<vm>_snapshot_data.qcow2``,
        which the name sweep's ``<vm>_snapshot_*`` pattern took for a
        memory-snapshot file and unlinked before the ownership decision ran
        (#212 review round 2, R2-1)."""
        adopted = self._file(tmp_path / f'{self.VM}_snapshot_data.qcow2')
        mgr = self._manager(
            tmp_path, [adopted],
            [self._record('snapshot_data', adopted, ROLE_ADOPTED)])

        mgr._destroy_removed_vm(self.VM)

        assert adopted.exists()
        assert str(adopted) in self._warnings(mgr)

    def test_a_snapshot_named_disk_another_domain_uses_is_kept(
            self, tmp_path):
        extra = self._file(tmp_path / f'{self.VM}_snapshot_data.qcow2')
        mgr = self._manager(tmp_path, [extra],
                            [self._record('snapshot_data', extra)],
                            in_use={str(extra): self.OTHER})

        mgr._destroy_removed_vm(self.VM)

        assert extra.exists()
        assert self.OTHER in self._warnings(mgr)

    def test_the_sweep_still_removes_the_boot_disk_and_memory_files(
            self, tmp_path):
        boot = self._file(tmp_path / f'{self.VM}.qcow2')
        memory = self._file(tmp_path / f'{self.VM}_snapshot_s1.raw')
        mgr = self._manager(tmp_path, [boot], [])

        mgr._destroy_removed_vm(self.VM)

        assert not boot.exists()
        assert not memory.exists()

    def test_a_file_replaced_right_before_the_unlink_is_kept(
            self, tmp_path, monkeypatch):
        """The replacement lands after the identity check and before the
        unlink: the review's probe runs it from the log call that sat
        between the two (#212 review round 2, R2-4)."""
        extra = self._file(tmp_path / f'{self.VM}_disk01.qcow2')
        mgr = self._manager(tmp_path, [extra],
                            [self._record('disk01', extra)])

        replaced = []

        def replace(*_args, **_kwargs):
            if not replaced:
                replaced.append(True)
                fresh = tmp_path / 'fresh'
                fresh.write_bytes(b'someone else')
                os.replace(fresh, extra)

        monkeypatch.setattr(
            'boxman.providers.libvirt.disk_cleanup.log.info', replace)

        mgr._destroy_removed_vm(self.VM)

        assert extra.read_bytes() == b'someone else'

    def test_a_file_recreated_after_it_was_claimed_is_kept(
            self, tmp_path, monkeypatch):
        """Only the entry moved into the private directory is ever
        unlinked: a writer recreating the name right after the move keeps
        its file."""
        extra = self._file(tmp_path / f'{self.VM}_disk01.qcow2')
        mgr = self._manager(tmp_path, [extra],
                            [self._record('disk01', extra)])
        real_rename = os.rename

        def rename_then_recreate(src, dst, *args, **kwargs):
            real_rename(src, dst, *args, **kwargs)
            if str(src) == str(extra):
                extra.write_bytes(b'someone else')

        monkeypatch.setattr(os, 'rename', rename_then_recreate)

        mgr._destroy_removed_vm(self.VM)

        assert extra.read_bytes() == b'someone else'
        assert sorted(p.name for p in tmp_path.iterdir()) == [extra.name]

    def test_a_replacement_that_cannot_be_put_back_is_named(
            self, tmp_path, monkeypatch):
        """The file moved aside turns out not to be the attached one, and
        the name has been taken again meanwhile: it stays in the private
        directory, which the warning names."""
        extra = self._file(tmp_path / f'{self.VM}_disk01.qcow2')
        mgr = self._manager(tmp_path, [extra],
                            [self._record('disk01', extra)])

        def replace_during_undefine(*_args, **_kwargs):
            fresh = tmp_path / 'fresh'
            fresh.write_bytes(b'someone else')
            os.replace(fresh, extra)

        mgr.provider.destroy_vm.side_effect = replace_during_undefine
        real_link = os.link

        def name_taken_again(src, dst, *args, **kwargs):
            if str(dst) == str(extra):
                extra.write_bytes(b'a third writer')
            return real_link(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, 'link', name_taken_again)

        mgr._destroy_removed_vm(self.VM)

        assert extra.read_bytes() == b'a third writer'
        stranded = [p for p in tmp_path.rglob(extra.name) if p != extra]
        assert [p.read_bytes() for p in stranded] == [b'someone else']
        assert str(stranded[0]) in self._warnings(mgr)

    def test_a_snapshot_moved_disk_is_kept_whole_and_named(self, tmp_path):
        """An external snapshot moved the head to an overlay. The record
        names only the base; nothing recorded names the overlays, so boxman
        removes neither rather than half a chain (#207 review, finding 3)."""
        base = self._file(tmp_path / f'{self.VM}_disk01.qcow2')
        head = self._file(tmp_path / f'{self.VM}_disk01.snap1')
        mgr = self._manager(tmp_path, [head],
                            [self._record('disk01', base)])

        mgr._destroy_removed_vm(self.VM)

        assert base.exists()
        assert head.exists()
        warnings = self._warnings(mgr)
        assert str(base) in warnings
        assert str(head) in warnings

    def test_a_snapshot_moved_disk_named_snapshot_is_kept_whole(
            self, tmp_path):
        """The same, for logical name ``snapshot_disk01``: the recorded
        base is no longer attached, and ``<vm>_snapshot_disk01.qcow2``
        matches the memory-snapshot sweep, which deleted it before the
        ownership decision could keep the chain (#212 review round 3,
        R3-2)."""
        base = self._file(tmp_path / f'{self.VM}_snapshot_disk01.qcow2')
        head = self._file(tmp_path / f'{self.VM}_snapshot_disk01.snap1')
        mgr = self._manager(tmp_path, [head],
                            [self._record('snapshot_disk01', base)])

        mgr._destroy_removed_vm(self.VM)

        assert base.exists()
        assert head.exists()
        warnings = self._warnings(mgr)
        assert str(base) in warnings
        assert str(head) in warnings

    def test_every_layer_under_a_kept_disk_survives_the_sweep(
            self, tmp_path):
        """Two snapshots: the middle layer is neither attached nor recorded,
        but it is in the attached head's backing chain, read before
        undefining."""
        base = self._file(tmp_path / f'{self.VM}_snapshot_disk01.qcow2')
        middle = self._file(tmp_path / f'{self.VM}_snapshot_disk01.snap1')
        head = self._file(tmp_path / f'{self.VM}_snapshot_disk01.snap2')
        memory = self._file(tmp_path / f'{self.VM}_snapshot_snap1.raw')
        mgr = self._manager(tmp_path, [head],
                            [self._record('snapshot_disk01', base)])
        mgr.provider.backing_chain_files.side_effect = None
        mgr.provider.backing_chain_files.return_value = [
            str(head), str(middle), str(base)]

        mgr._destroy_removed_vm(self.VM)

        assert head.exists() and middle.exists() and base.exists()
        # a memory-snapshot file is not part of any chain: still swept
        assert not memory.exists()

    def test_an_unreadable_chain_keeps_every_snapshot_named_file(
            self, tmp_path):
        """Without the chain, which ``<vm>_snapshot_*`` files are layers of
        a kept disk cannot be told apart from memory-snapshot files, so the
        sweep leaves all of them."""
        middle = self._file(tmp_path / f'{self.VM}_snapshot_disk01.snap1')
        head = self._file(tmp_path / f'{self.VM}_snapshot_disk01.snap2')
        memory = self._file(tmp_path / f'{self.VM}_snapshot_snap1.raw')
        mgr = self._manager(tmp_path, [head], [])
        mgr.provider.backing_chain_files.side_effect = None
        mgr.provider.backing_chain_files.return_value = None

        mgr._destroy_removed_vm(self.VM)

        assert middle.exists() and head.exists() and memory.exists()
        assert 'backing chain' in self._warnings(mgr)

    def test_unconfirmed_absence_leaves_every_disk(self, tmp_path):
        extra = self._file(tmp_path / f'{self.VM}_disk01.qcow2')
        mgr = self._manager(tmp_path, [extra],
                            [self._record('disk01', extra)])
        mgr.provider.confirm_vm_absent.return_value = False

        with pytest.raises(ProvisionError, match='could not confirm'):
            mgr._destroy_removed_vm(self.VM)

        assert extra.exists()


# ---------------------------------------------------------------------------
# Persistent memballoon updates
# ---------------------------------------------------------------------------
class TestMemballoonUpdateResult:

    @staticmethod
    def _balloon_only_diff(vm_state):
        return {
            'cpu_changed': False,
            'memory_changed': False,
            'max_vcpus_changed': False,
            'max_memory_changed': False,
            'new_disks': [],
            'resize_disks': [],
            'removed_disks': [],
            'refused_disk_removals': [],
            'unowned_disks': [],
            'disk_conflicts': [],
            'shared_folders_restart_pending': False,
            'new_cdroms': [],
            'removed_cdroms': [],
            'changed_cdroms': [],
            'new_shared_folders': [],
            'removed_shared_folders': [],
            'changed_shared_folders': [],
            'memballoon_changed': True,
            # Keep this manager fixture independent of VMStateDiffer's state
            # classification; differ-level tests own that contract.
            'memballoon_restart_pending': (
                vm_state in ('running', 'paused', 'crashed')),
            'actual_memballoon': {
                'free_page_reporting': False,
                'autodeflate': False,
                'stats_period': None,
            },
            'desired_memballoon': {
                'free_page_reporting': False,
                'autodeflate': True,
                'stats_period': None,
            },
            'live_memballoon': {
                'free_page_reporting': False,
                'autodeflate': False,
                'stats_period': None,
            },
            'vm_state': vm_state,
        }

    def _run_update(self, vm_state):
        mgr = make_bare_manager({'project': 'demo'})
        mgr.provider = MagicMock()
        mgr.provider.provider_config = {'uri': 'qemu:///system'}
        mgr.provider.configure_vm_memballoon.return_value = True
        result_queue = MagicMock()

        with patch.object(
                VMStateDiffer, 'diff_vm',
                return_value=self._balloon_only_diff(vm_state)):
            mgr._update_single_vm(
                'cluster1', {'workdir': '/tmp'}, 'node01',
                {'memballoon': {'autodeflate': True}}, result_queue)

        result_queue.put.assert_called_once()
        return mgr, result_queue.put.call_args.args[0][1]

    def test_running_vm_reports_restart_required(self):
        mgr, result = self._run_update('running')

        assert result['status'] == 'needs_restart'
        assert 'restart the VM to apply them' in result['details']
        mgr.provider.shutdown_and_wait.assert_not_called()
        mgr.provider.start_vm.assert_not_called()

    def test_paused_vm_reports_restart_required(self):
        mgr, result = self._run_update('paused')

        assert result['status'] == 'needs_restart'
        assert 'restart the VM to apply them' in result['details']
        mgr.provider.shutdown_and_wait.assert_not_called()
        mgr.provider.start_vm.assert_not_called()

    def test_crash_preserved_vm_reports_restart_required(self):
        mgr, result = self._run_update('crashed')

        assert result['status'] == 'needs_restart'
        assert 'restart the VM to apply them' in result['details']
        mgr.provider.shutdown_and_wait.assert_not_called()
        mgr.provider.start_vm.assert_not_called()

    def test_stopped_vm_reports_updated(self):
        _mgr, result = self._run_update('shut off')

        assert result['status'] == 'updated'
        assert 'restart required' not in result['details']

    def test_persistent_match_still_reports_live_restart_pending(self):
        diff = self._balloon_only_diff('running')
        diff['memballoon_changed'] = False
        mgr = make_bare_manager({'project': 'demo'})
        mgr.provider = MagicMock()
        mgr.provider.provider_config = {'uri': 'qemu:///system'}
        result_queue = MagicMock()

        with patch.object(VMStateDiffer, 'diff_vm', return_value=diff):
            mgr._update_single_vm(
                'cluster1', {'workdir': '/tmp'}, 'node01',
                {'memballoon': {'autodeflate': True}}, result_queue)

        result = result_queue.put.call_args.args[0][1]
        assert result['status'] == 'needs_restart'
        mgr.provider.configure_vm_memballoon.assert_not_called()


class TestUpdateRestartFailures:
    """#164 X3 — the restart branch ignored both ``shutdown_and_wait()`` and
    ``start_vm()`` and then queued ``status='updated'`` with "(restarted)".

    Two ways that lied. A lost shutdown left the restart-only changes
    unapplied while ``update`` exited 0 — and ``start_vm()`` then returned
    True *because* the guest was still running, so the failure hid behind a
    successful start. A lost start left the guest shut off, also at exit 0.
    """

    @staticmethod
    def _cpu_restart_diff():
        return {
            'cpu_changed': True,
            'memory_changed': False,
            'max_vcpus_changed': False,
            'max_memory_changed': False,
            'new_disks': [],
            'resize_disks': [],
            'removed_disks': [],
            'refused_disk_removals': [],
            'unowned_disks': [],
            'disk_conflicts': [],
            'shared_folders_restart_pending': False,
            'new_cdroms': [],
            'removed_cdroms': [],
            'changed_cdroms': [],
            'new_shared_folders': [],
            'removed_shared_folders': [],
            'changed_shared_folders': [],
            'memballoon_changed': False,
            'memballoon_restart_pending': False,
            'actual_cpus': 2,
            'desired_cpus': 4,
            'actual_memory_mb': 2048,
            'desired_memory_mb': 2048,
            'desired_max_vcpus': None,
            'desired_max_memory_mb': None,
            'vm_state': 'running',
        }

    def _run(self, shutdown_ok, start_ok):
        mgr = make_bare_manager({'project': 'demo'})
        mgr.provider = MagicMock()
        mgr.provider.provider_config = {'uri': 'qemu:///system'}
        # the cold-only change that makes the restart necessary
        mgr.provider.update_vm_cpu_memory.return_value = {
            'success': True, 'restart_needed': True}
        mgr.provider.shutdown_and_wait.return_value = shutdown_ok
        mgr.provider.start_vm.return_value = start_ok
        result_queue = MagicMock()

        with patch.object(VMStateDiffer, 'diff_vm',
                          return_value=self._cpu_restart_diff()):
            mgr._update_single_vm(
                'cluster1', {'workdir': '/tmp'}, 'node01',
                {'cpus': 4}, result_queue,
                # the restart is opt-in now; this class is about what
                # happens once it is authorised (#164 C1)
                dry_run=False, allow_restart=True)

        result_queue.put.assert_called_once()
        return mgr, result_queue.put.call_args.args[0][1]

    def test_failed_shutdown_is_reported_and_start_is_skipped(self):
        mgr, result = self._run(shutdown_ok=False, start_ok=True)

        assert result['status'] == 'failed'
        assert 'could not be shut down' in result['details']
        # Starting a guest that never went down would report success and
        # bury the real failure.
        mgr.provider.start_vm.assert_not_called()

    def test_failed_start_is_reported(self):
        mgr, result = self._run(shutdown_ok=True, start_ok=False)

        assert result['status'] == 'failed'
        assert 'did not come back up' in result['details']
        mgr.provider.start_vm.assert_called_once()

    def test_successful_restart_reports_updated(self):
        mgr, result = self._run(shutdown_ok=True, start_ok=True)

        assert result['status'] == 'updated'
        assert '(restarted)' in result['details']
        assert 'CPU: 2 -> 4' in result['details']
        mgr.provider.shutdown_and_wait.assert_called_once()
        mgr.provider.start_vm.assert_called_once()


def _needs_restart_update_worker(_self, _cluster_name, _cluster_cfg, vm_name,
                                 _vm_info, result_queue, _dry_run=False,
                                 _allow_restart=False):
    """Stand-in for ``_update_single_vm`` reporting a pending restart."""
    result_queue.put((vm_name, {
        'status': 'needs_restart',
        'details': ("memballoon live state: {'autodeflate': False} -> "
                    "{'autodeflate': True} (restart required to apply "
                    "memballoon changes)"),
    }))


class TestMemballoonUpdateSummary:
    """A pending restart must survive into ``update``'s closing summary.

    ``_update_single_vm`` reports ``needs_restart`` per VM, but that is only
    actionable if the run-level summary repeats it. Folded into the plain
    ``updated`` list, a memballoon change still waiting for a boot reads
    exactly like one that already took effect.
    """

    def test_needs_restart_vms_are_called_out_in_the_summary(self, monkeypatch):
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {
            'project': 'demo',
            'clusters': {
                'cluster_1': {
                    'workdir': '/tmp/ws/c1',
                    'vms': {'node01': {'memballoon': {'autodeflate': True}}},
                },
            },
        }
        mgr.provider = MagicMock()
        mgr.logger = MagicMock()
        full = 'bprj__demo__bprj_cluster_1_node01'
        for name, replacement in (
                ('_update_sessions_with_runtime', lambda self: None),
                ('ensure_shared_bridges', lambda self: None),
                ('reconcile_networks', lambda self, **kw: {}),
                ('report_network_results', lambda self, r: None),
                ('_find_all_existing_project_vms', lambda self: [full]),
                ('setup_ssh_access', lambda self: None),
                ('connect_info', lambda self: None),
                ('_update_single_vm', _needs_restart_update_worker)):
            monkeypatch.setattr(BoxmanManager, name, replacement)

        mgr.update(types.SimpleNamespace(
            dry_run=False, yes=True, recreate_networks=False))

        warnings = [c.args[0] for c in mgr.logger.warning.call_args_list
                    if c.args]
        assert any(w.startswith('restart required:') and 'node01' in w
                   for w in warnings)
        assert any('node01' in w and 'memballoon' in w for w in warnings)
        # and it must not also be reported as a finished update
        infos = [c.args[0] for c in mgr.logger.info.call_args_list if c.args]
        assert not any(m.startswith('updated:') for m in infos)
