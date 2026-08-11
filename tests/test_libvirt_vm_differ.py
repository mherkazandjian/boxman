"""
Unit tests for boxman.providers.libvirt.vm_differ.VMStateDiffer.

Currently pins the disk-diff regression from issue #85 item 23: a
desired disk whose image file already exists (leftover from a failed
earlier run) but which is not attached must be reported as an
attach-only new disk — not silently skipped.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from boxman.providers.libvirt.vm_differ import VMStateDiffer

pytestmark = pytest.mark.unit


@pytest.fixture
def differ() -> VMStateDiffer:
    return VMStateDiffer(provider_config={"use_sudo": False,
                                          "uri": "qemu:///system"})


def _diff_disks(differ: VMStateDiffer,
                desired_disks: list[dict],
                actual_disks: list[dict],
                workdir: str) -> dict:
    """Run diff_vm with every non-disk probe mocked out."""
    with patch.object(differ, "get_vm_state", return_value="running"), \
         patch.object(differ, "get_actual_cpu",
                      return_value={"sockets": 1, "cores": 1, "threads": 1,
                                    "total_vcpus": 1, "current_vcpus": 1}), \
         patch.object(differ, "get_max_vcpus", return_value=1), \
         patch.object(differ, "get_actual_memory_mb", return_value=1024), \
         patch.object(differ, "get_max_memory_mb", return_value=1024), \
         patch.object(differ, "get_actual_disks", return_value=actual_disks), \
         patch.object(differ, "get_actual_cdroms", return_value=[]), \
         patch.object(differ, "get_actual_shared_folders", return_value=[]):
        return differ.diff_vm(
            domain_name="vm01",
            desired_cpus=None,
            desired_memory_mb=None,
            desired_disks=desired_disks,
            workdir=workdir,
            disk_prefix="vm01",
        )


class TestDiskDiff:

    def test_new_disk_when_file_missing(self, differ: VMStateDiffer,
                                        tmp_path: Path):
        desired = [{"name": "data", "target": "vdb", "size": 1024}]
        diff = _diff_disks(differ, desired, [], str(tmp_path))
        assert diff["new_disks"] == desired
        assert "attach_only" not in diff["new_disks"][0]

    def test_stale_file_becomes_attach_only(self, differ: VMStateDiffer,
                                            tmp_path: Path):
        """Regression for issue #85 item 23: image exists but is not
        attached — must surface as attach-only, not vanish silently."""
        (tmp_path / "vm01_data.qcow2").write_bytes(b"x")
        desired = [{"name": "data", "target": "vdb", "size": 1024}]
        diff = _diff_disks(differ, desired, [], str(tmp_path))
        assert len(diff["new_disks"]) == 1
        assert diff["new_disks"][0]["attach_only"] is True
        assert diff["new_disks"][0]["name"] == "data"

    def test_stale_file_logs_warning(self, differ: VMStateDiffer,
                                     tmp_path: Path, caplog):
        (tmp_path / "vm01_data.qcow2").write_bytes(b"x")
        desired = [{"name": "data", "target": "vdb", "size": 1024}]
        with caplog.at_level("WARNING"):
            _diff_disks(differ, desired, [], str(tmp_path))
        assert any("attach" in rec.message and "vdb" in rec.message
                   for rec in caplog.records)

    def test_attached_disk_not_in_new_disks(self, differ: VMStateDiffer,
                                            tmp_path: Path):
        desired = [{"name": "data", "target": "vdb", "size": 1024}]
        actual = [{"target": "vdb",
                   "source": str(tmp_path / "vm01_data.qcow2"),
                   "size_mb": 1024}]
        diff = _diff_disks(differ, desired, actual, str(tmp_path))
        assert diff["new_disks"] == []
        assert diff["resize_disks"] == []
