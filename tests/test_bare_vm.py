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
        disk_size='20G',
        networks=[{'name': 'default'}],
    )
    info.update(info_overrides)
    return BareVM(
        vm_name="pxe-test01",
        info=info,
        provider_config={"use_sudo": False, "uri": "qemu:///system"},
        workdir=str(tmp_path),
    )


class TestBootDiskSize:

    def test_reads_disk_size_field(self, tmp_path):
        vm = _make_bare_vm(tmp_path, disk_size='40G')
        assert vm._boot_disk_size() == '40G'

    def test_int_disk_size_is_gib(self, tmp_path):
        vm = _make_bare_vm(tmp_path, disk_size=30)
        assert vm._boot_disk_size() == '30G'

    def test_defaults_to_20g_when_absent(self, tmp_path):
        vm = _make_bare_vm(tmp_path)
        vm.info.pop('disk_size', None)
        assert vm._boot_disk_size() == '20G'


class TestNetworks:

    def test_returns_raw_network_names(self, tmp_path):
        vm = _make_bare_vm(tmp_path, networks=[{'name': 'mgmt'}])
        assert vm._networks() == ['mgmt']

    def test_prefers_resolved_networks(self, tmp_path):
        vm = _make_bare_vm(
            tmp_path, networks=[{'name': 'mgmt'}], _resolved_networks=['bprj__p__mgmt'])
        assert vm._networks() == ['bprj__p__mgmt']

    def test_defaults_to_default_when_no_networks(self, tmp_path):
        vm = _make_bare_vm(tmp_path, networks=[])
        assert vm._networks() == ['default']


class TestBareVMCreate:

    @patch('boxman.providers.libvirt.direct_vm._shell_run')
    def test_create_success(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_bare_vm(tmp_path)
        result = vm.create()
        assert result is True
        assert mock_run.call_count == 2

    @patch('boxman.providers.libvirt.direct_vm._shell_run')
    def test_create_fails_on_qemu_img_error(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=False, stderr="no space left")
        vm = _make_bare_vm(tmp_path)
        result = vm.create()
        assert result is False
        assert mock_run.call_count == 1  # stops after qemu-img failure

    @patch('boxman.providers.libvirt.direct_vm._shell_run')
    def test_create_fails_on_virt_install_error(self, mock_run, tmp_path):
        ok_result = _result(ok=True)
        fail_result = _result(ok=False, stderr="virt-install error")
        mock_run.side_effect = [ok_result, fail_result]
        vm = _make_bare_vm(tmp_path)
        result = vm.create()
        assert result is False
        assert mock_run.call_count == 2

    @patch('boxman.providers.libvirt.direct_vm._shell_run')
    def test_qemu_img_command_contains_disk_path(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_bare_vm(tmp_path, disk_size='30G')
        vm.create()
        first_call_cmd = mock_run.call_args_list[0][0][0]
        assert "qemu-img create" in first_call_cmd
        assert "30G" in first_call_cmd
        assert "pxe-test01.qcow2" in first_call_cmd

    @patch('boxman.providers.libvirt.direct_vm._shell_run')
    def test_virt_install_command_contains_pxe_flags(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_bare_vm(tmp_path)
        vm.create()
        second_call_cmd = mock_run.call_args_list[1][0][0]
        assert "--boot=network,hd" in second_call_cmd
        assert "--noautoconsole" in second_call_cmd
        assert "--wait=0" in second_call_cmd
