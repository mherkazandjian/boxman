"""Unit tests for boxman.providers.libvirt.iso_boot_vm.IsoBootVM."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.direct_vm import normalize_disk_size
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
        disk_size="20G",
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


class TestNormalizeDiskSize:
    def test_int_is_gib(self):
        assert normalize_disk_size(50) == "50G"

    def test_bare_numeric_string_is_gib(self):
        assert normalize_disk_size("50") == "50G"

    def test_unit_suffixed_passes_through(self):
        assert normalize_disk_size("51200M") == "51200M"
        assert normalize_disk_size("50G") == "50G"

    def test_none_and_empty_use_default(self):
        assert normalize_disk_size(None) == "20G"
        assert normalize_disk_size("") == "20G"
        assert normalize_disk_size(True) == "20G"


class TestBootDiskSize:
    def test_reads_disk_size_field(self, tmp_path):
        vm = _make_iso_vm(tmp_path, disk_size="50G")
        assert vm._boot_disk_size() == "50G"

    def test_int_disk_size_is_gib(self, tmp_path):
        vm = _make_iso_vm(tmp_path, disk_size=40)
        assert vm._boot_disk_size() == "40G"

    def test_defaults_to_20g_when_absent(self, tmp_path):
        vm = _make_iso_vm(tmp_path)
        vm.info.pop("disk_size", None)
        assert vm._boot_disk_size() == "20G"


class TestNetworks:
    def test_returns_raw_network_names(self, tmp_path):
        vm = _make_iso_vm(tmp_path, networks=[{"name": "talos-net"}])
        assert vm._networks() == ["talos-net"]

    def test_prefers_resolved_networks(self, tmp_path):
        vm = _make_iso_vm(
            tmp_path,
            networks=[{"name": "talos-net"}],
            _resolved_networks=["bprj__p__bprj__clstr__talos__clstr__talos-net"],
        )
        assert vm._networks() == ["bprj__p__bprj__clstr__talos__clstr__talos-net"]

    def test_defaults_to_default_when_no_networks(self, tmp_path):
        vm = _make_iso_vm(tmp_path, networks=[])
        assert vm._networks() == ["default"]


class TestIsoBootVMCreate:
    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_create_success(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path, iso_path="/data/talos.iso")
        assert vm.create() is True
        assert mock_run.call_count == 2

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_virt_install_cmd_contains_cdrom_and_boot(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path, iso_path="/data/talos.iso")
        vm.create()
        virt_install_call = mock_run.call_args_list[1][0][0]
        assert "--cdrom=/data/talos.iso" in virt_install_call
        # hd first so an installed disk is preferred over re-running the installer
        assert "--boot=hd,cdrom" in virt_install_call

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_uses_resolved_network_name(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        full = "bprj__p__bprj__clstr__talos__clstr__talos-net"
        vm = _make_iso_vm(tmp_path, _resolved_networks=[full])
        vm.create()
        virt_install_call = mock_run.call_args_list[1][0][0]
        assert f"--network=network={full},model=virtio" in virt_install_call

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_disk_size_in_qemu_img_cmd(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path, disk_size="50G")
        vm.create()
        qemu_img_call = mock_run.call_args_list[0][0][0]
        assert qemu_img_call.endswith(" 50G")

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_create_fails_on_qemu_img_error(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=False, stderr="no space left")
        vm = _make_iso_vm(tmp_path)
        assert vm.create() is False
        assert mock_run.call_count == 1

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_create_fails_on_virt_install_error(self, mock_run, tmp_path):
        mock_run.side_effect = [_result(ok=True), _result(ok=False, stderr="permission denied")]
        vm = _make_iso_vm(tmp_path)
        assert vm.create() is False
