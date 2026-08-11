"""
Unit tests for boxman.providers.libvirt.disk.DiskManager.

Currently pins the attach_only flag from issue #85 item 23: when the
image file already exists (leftover from a failed earlier run),
``configure_from_disk_config`` must skip ``qemu-img create`` and only
attach the existing file.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from boxman.providers.libvirt.disk import DiskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def dm() -> DiskManager:
    return DiskManager(vm_name="vm01",
                       provider_config={"use_sudo": False,
                                        "uri": "qemu:///system"})


class TestConfigureFromDiskConfig:

    def test_creates_then_attaches_by_default(self, dm: DiskManager,
                                              tmp_path: Path):
        config = {"name": "data", "target": "vdb", "size": 1024}
        with patch.object(dm, "create_disk", return_value=True) as create, \
             patch.object(dm, "attach_disk", return_value=True) as attach:
            assert dm.configure_from_disk_config(config, str(tmp_path),
                                                 "vm01") is True
        create.assert_called_once()
        attach.assert_called_once()

    def test_attach_only_skips_create(self, dm: DiskManager,
                                      tmp_path: Path):
        """Regression for issue #85 item 23: attach_only must not run
        qemu-img create — that would wipe the existing image."""
        (tmp_path / "vm01_data.qcow2").write_bytes(b"x")
        config = {"name": "data", "target": "vdb", "size": 1024,
                  "attach_only": True}
        with patch.object(dm, "create_disk") as create, \
             patch.object(dm, "attach_disk", return_value=True) as attach:
            assert dm.configure_from_disk_config(config, str(tmp_path),
                                                 "vm01") is True
        create.assert_not_called()
        attach.assert_called_once()
        assert (tmp_path / "vm01_data.qcow2").read_bytes() == b"x"
