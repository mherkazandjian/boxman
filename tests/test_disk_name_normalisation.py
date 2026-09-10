"""
Predicted and created disk paths must be the same string (#164 F2 review 4).

``_expected_disk_path`` used ``.get("name", "disk")``, which defaults only
an *absent* key, while creation used a normaliser that also defaults null
and empty. So with ``name: null`` the differ predicted
``<prefix>_None.qcow2`` and with ``name: ""`` it predicted
``<prefix>_.qcow2`` — neither of which exists — so the entry was not
marked ``attach_only``, and creation then ran ``qemu-img create`` over the
existing ``<prefix>_disk.qcow2``, destroying its contents.

No race, no detach. Introduced by the fix for round-3 finding 2, which
changed one side of a documented "matching" pair and not the other.

Each test asserts **reachability as well as outcome**: that creation was
actually attempted for the disks that should be created, so replacing the
operation with a no-op fails the test rather than passing it vacuously.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.disk import DiskManager, disk_path_for
from boxman.providers.libvirt.disk_ownership import disk_logical_name
from boxman.providers.libvirt.vm_differ import VMStateDiffer

pytestmark = pytest.mark.unit

SPELLINGS = [
    pytest.param({}, id="omitted"),
    pytest.param({'name': None}, id="null"),
    pytest.param({'name': ''}, id="empty"),
    pytest.param({'name': 'data'}, id="named"),
]


@pytest.mark.parametrize("declared", SPELLINGS)
def test_prediction_and_creation_agree(declared, tmp_path: Path):
    predicted = VMStateDiffer._expected_disk_path(
        dict(declared), str(tmp_path), 'vm01')
    created = disk_path_for(
        str(tmp_path), disk_logical_name(dict(declared)), disk_prefix='vm01')

    assert predicted == created


@pytest.mark.parametrize("declared", SPELLINGS)
def test_an_existing_image_is_attached_not_recreated(declared, tmp_path: Path):
    """The differ-to-application path, with the destination already there."""
    config = dict(declared, target='vdb', size=1024)
    image = Path(disk_path_for(str(tmp_path), disk_logical_name(config),
                               disk_prefix='vm01'))
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"existing-data")

    differ = VMStateDiffer(provider_config={"use_sudo": False,
                                            "uri": "qemu:///system"})
    with patch.object(differ, "get_vm_state", return_value="shut off"), \
         patch.object(differ, "get_actual_cpu",
                      return_value={"sockets": 1, "cores": 1, "threads": 1,
                                    "total_vcpus": 1, "current_vcpus": 1}), \
         patch.object(differ, "get_max_vcpus", return_value=1), \
         patch.object(differ, "get_actual_memory_mb", return_value=1024), \
         patch.object(differ, "get_max_memory_mb", return_value=1024), \
         patch.object(differ, "get_actual_disks", return_value=[]), \
         patch.object(differ, "get_disk_records", return_value=None), \
         patch.object(differ, "get_actual_memballoon",
                      return_value={'free_page_reporting': False,
                                    'autodeflate': False,
                                    'stats_period': None}), \
         patch.object(differ, "get_actual_cdroms", return_value=[]), \
         patch.object(differ, "get_actual_shared_folders", return_value=[]):
        diff = differ.diff_vm(
            domain_name="vm01", desired_cpus=None, desired_memory_mb=None,
            desired_disks=[config], workdir=str(tmp_path), disk_prefix="vm01")

    assert len(diff['new_disks']) == 1
    assert diff['new_disks'][0].get('attach_only') is True, (
        'the existing image was not recognised, so creation would overwrite it')

    # ...and the application half honours it
    manager = DiskManager.__new__(DiskManager)
    manager.logger = MagicMock()
    manager.virsh = MagicMock()
    manager.vm_name = 'vm01'
    manager.provider_config = {}
    manager.create_disk = MagicMock(return_value=True)
    manager.attach_disk = MagicMock(return_value=True)

    with patch('boxman.providers.libvirt.disk.record_attached_disk'):
        assert manager.configure_from_disk_config(
            disk_config=diff['new_disks'][0], workdir=str(tmp_path),
            disk_prefix='vm01') is True

    manager.create_disk.assert_not_called()
    # reachability: the attach *did* happen, so this is not passing because
    # nothing ran at all
    manager.attach_disk.assert_called_once()
    assert manager.attach_disk.call_args.kwargs['disk_path'] == str(image)
    assert image.read_bytes() == b"existing-data"


@pytest.mark.parametrize("declared", SPELLINGS)
def test_a_missing_image_is_still_created(declared, tmp_path: Path):
    """The negative control: creation is reached when it should be."""
    config = dict(declared, target='vdb', size=1024)

    manager = DiskManager.__new__(DiskManager)
    manager.logger = MagicMock()
    manager.virsh = MagicMock()
    manager.vm_name = 'vm01'
    manager.provider_config = {}
    manager.create_disk = MagicMock(return_value=True)
    manager.attach_disk = MagicMock(return_value=True)

    with patch('boxman.providers.libvirt.disk.record_attached_disk'):
        manager.configure_from_disk_config(
            disk_config=config, workdir=str(tmp_path), disk_prefix='vm01')

    manager.create_disk.assert_called_once()
    expected = disk_path_for(str(tmp_path), disk_logical_name(config),
                             disk_prefix='vm01')
    assert manager.create_disk.call_args.args[0] == expected
