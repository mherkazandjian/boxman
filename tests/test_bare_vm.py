"""
Unit tests for boxman.providers.libvirt.bare_vm.BareVM.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.bare_vm import BareVM


pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True, stderr: str = "",
            return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


def _make_bare_vm(tmp_path: Path, **info_overrides) -> BareVM:
    info = dict(
        memory=2048,
        vcpus=2,
        disks=[{'name': 'disk01', 'size': 20}],
        networks=[{'name': 'default'}],
    )
    info.update(info_overrides)
    return BareVM(
        vm_name="pxe-test01",
        info=info,
        provider_config={"use_sudo": False, "uri": "qemu:///system"},
        workdir=str(tmp_path),
    )


class TestGetDiskSizeGb:

    def test_returns_first_disk_size(self, tmp_path):
        vm = _make_bare_vm(tmp_path, disks=[{'name': 'disk01', 'size': 40}])
        assert vm._get_disk_size_gb() == 40

    def test_defaults_to_20_when_no_disks(self, tmp_path):
        vm = _make_bare_vm(tmp_path, disks=[])
        assert vm._get_disk_size_gb() == 20

    def test_defaults_to_20_when_size_missing(self, tmp_path):
        vm = _make_bare_vm(tmp_path, disks=[{'name': 'disk01'}])
        assert vm._get_disk_size_gb() == 20


class TestGetNetwork:

    def test_returns_first_network_name(self, tmp_path):
        vm = _make_bare_vm(tmp_path, networks=[{'name': 'mgmt'}])
        assert vm._get_network() == 'mgmt'

    def test_defaults_to_default_when_no_networks(self, tmp_path):
        vm = _make_bare_vm(tmp_path, networks=[])
        assert vm._get_network() == 'default'

    def test_defaults_to_default_when_name_missing(self, tmp_path):
        vm = _make_bare_vm(tmp_path, networks=[{}])
        assert vm._get_network() == 'default'


class TestBareVMCreate:

    @patch('boxman.providers.libvirt.bare_vm._shell_run')
    def test_create_success(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_bare_vm(tmp_path)
        result = vm.create()
        assert result is True
        assert mock_run.call_count == 2

    @patch('boxman.providers.libvirt.bare_vm._shell_run')
    def test_create_fails_on_qemu_img_error(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=False, stderr="no space left")
        vm = _make_bare_vm(tmp_path)
        result = vm.create()
        assert result is False
        assert mock_run.call_count == 1  # stops after qemu-img failure

    @patch('boxman.providers.libvirt.bare_vm._shell_run')
    def test_create_fails_on_virt_install_error(self, mock_run, tmp_path):
        ok_result = _result(ok=True)
        fail_result = _result(ok=False, stderr="virt-install error")
        mock_run.side_effect = [ok_result, fail_result]
        vm = _make_bare_vm(tmp_path)
        result = vm.create()
        assert result is False
        assert mock_run.call_count == 2

    @patch('boxman.providers.libvirt.bare_vm._shell_run')
    def test_qemu_img_command_contains_disk_path(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_bare_vm(tmp_path, disks=[{'name': 'disk01', 'size': 30}])
        vm.create()
        first_call_cmd = mock_run.call_args_list[0][0][0]
        assert "qemu-img create" in first_call_cmd
        assert "30G" in first_call_cmd
        assert "pxe-test01.qcow2" in first_call_cmd

    @patch('boxman.providers.libvirt.bare_vm._shell_run')
    def test_virt_install_command_contains_pxe_flags(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_bare_vm(tmp_path)
        vm.create()
        second_call_cmd = mock_run.call_args_list[1][0][0]
        assert "--boot=network,hd" in second_call_cmd
        assert "--noautoconsole" in second_call_cmd
        assert "--wait=0" in second_call_cmd
