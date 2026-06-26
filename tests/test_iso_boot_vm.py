"""Unit tests for boxman.providers.libvirt.iso_boot_vm.IsoBootVM."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.iso_boot_vm import IsoBootVM

pytestmark = pytest.mark.unit


def _result(ok: bool = True, stderr: str = "") -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.ok = ok
    r.stderr = stderr
    r.failed = not ok
    return r


def _make_iso_vm(tmp_path: Path, iso_path: str = "/fake/talos.iso", **info_overrides) -> IsoBootVM:
    info = dict(
        memory=2048,
        vcpus=2,
        disks=[{"name": "disk01", "size": 20}],
        networks=[{"name": "default"}],
    )
    info.update(info_overrides)
    return IsoBootVM(
        vm_name="iso-test01",
        info=info,
        provider_config={"use_sudo": False, "uri": "qemu:///system"},
        workdir=str(tmp_path),
        iso_path=iso_path,
    )


class TestGetDiskSizeGb:
    def test_returns_first_disk_size(self, tmp_path):
        vm = _make_iso_vm(tmp_path, disks=[{"name": "disk01", "size": 50}])
        assert vm._get_disk_size_gb() == 50

    def test_defaults_to_20_when_no_disks(self, tmp_path):
        vm = _make_iso_vm(tmp_path, disks=[])
        assert vm._get_disk_size_gb() == 20

    def test_defaults_to_20_when_size_missing(self, tmp_path):
        vm = _make_iso_vm(tmp_path, disks=[{"name": "disk01"}])
        assert vm._get_disk_size_gb() == 20


class TestGetNetwork:
    def test_returns_first_network_name(self, tmp_path):
        vm = _make_iso_vm(tmp_path, networks=[{"name": "talos-net"}])
        assert vm._get_network() == "talos-net"

    def test_defaults_to_default_when_no_networks(self, tmp_path):
        vm = _make_iso_vm(tmp_path, networks=[])
        assert vm._get_network() == "default"

    def test_defaults_to_default_when_name_missing(self, tmp_path):
        vm = _make_iso_vm(tmp_path, networks=[{}])
        assert vm._get_network() == "default"


class TestIsoBootVMCreate:
    @patch("boxman.providers.libvirt.iso_boot_vm._shell_run")
    def test_create_success(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path, iso_path="/data/talos.iso")
        assert vm.create() is True
        assert mock_run.call_count == 2

    @patch("boxman.providers.libvirt.iso_boot_vm._shell_run")
    def test_virt_install_cmd_contains_cdrom_and_boot(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path, iso_path="/data/talos.iso")
        vm.create()
        virt_install_call = mock_run.call_args_list[1][0][0]
        assert "--cdrom=/data/talos.iso" in virt_install_call
        assert "--boot=cdrom,hd" in virt_install_call

    @patch("boxman.providers.libvirt.iso_boot_vm._shell_run")
    def test_create_fails_on_qemu_img_error(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=False, stderr="no space left")
        vm = _make_iso_vm(tmp_path)
        assert vm.create() is False
        assert mock_run.call_count == 1

    @patch("boxman.providers.libvirt.iso_boot_vm._shell_run")
    def test_create_fails_on_virt_install_error(self, mock_run, tmp_path):
        mock_run.side_effect = [_result(ok=True), _result(ok=False, stderr="permission denied")]
        vm = _make_iso_vm(tmp_path)
        assert vm.create() is False
