"""Unit tests for boxman.providers.libvirt.iso_boot_vm.IsoBootVM."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import ProvisionError
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
    def test_iso_path_with_space_is_shell_quoted(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path, iso_path="/data/my isos/talos.iso")
        vm.create()
        virt_install_call = mock_run.call_args_list[1][0][0]
        assert "--cdrom='/data/my isos/talos.iso'" in virt_install_call

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


class TestNetworkMacPinning:
    """``networks[].mac`` pins the first NIC's MAC at virt-install time."""

    def test_mac_from_raw_networks(self, tmp_path):
        vm = _make_iso_vm(
            tmp_path, networks=[{"name": "pvenet", "mac": "52:54:00:77:00:11"}])
        assert vm._network_specs() == [{"name": "pvenet", "mac": "52:54:00:77:00:11"}]
        assert vm._networks() == ["pvenet"]

    def test_mac_from_resolved_dict_entries(self, tmp_path):
        full = "bprj__p__bprj__clstr__pve__clstr__pvenet"
        vm = _make_iso_vm(
            tmp_path,
            networks=[{"name": "pvenet", "mac": "00:00:00:00:00:01"}],
            _resolved_networks=[{"name": full, "mac": "52:54:00:77:00:11"}],
        )
        assert vm._network_specs() == [{"name": full, "mac": "52:54:00:77:00:11"}]

    def test_resolved_string_entries_have_no_mac(self, tmp_path):
        vm = _make_iso_vm(tmp_path, _resolved_networks=["bprj__p__net"])
        assert vm._network_specs() == [{"name": "bprj__p__net", "mac": None}]

    def test_empty_resolved_and_raw_lists_fall_back_to_default(self, tmp_path):
        vm = _make_iso_vm(tmp_path, networks=[], _resolved_networks=[])
        assert vm._network_specs() == [{"name": "default", "mac": None}]

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_mac_is_passed_to_virt_install(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        full = "bprj__p__bprj__clstr__pve__clstr__pvenet"
        vm = _make_iso_vm(
            tmp_path, _resolved_networks=[{"name": full, "mac": "52:54:00:77:00:11"}])
        vm.create()
        virt_install_call = mock_run.call_args_list[1][0][0]
        assert (f"--network=network={full},model=virtio,mac=52:54:00:77:00:11"
                in virt_install_call)

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_no_mac_keeps_plain_network_arg(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path, _resolved_networks=[{"name": "n1", "mac": None}])
        vm.create()
        virt_install_call = mock_run.call_args_list[1][0][0]
        assert "--network=network=n1,model=virtio" in virt_install_call
        assert ",mac=" not in virt_install_call


class TestDeclaredButUnresolvableNetworks:
    """#171 A4, the runtime half.

    `networks: [""]` passed validation, was skipped at resolution, and fell
    through to libvirt's shared `default` network with nothing reported --
    the libvirt twin of #164 NET-C1.
    """

    def test_a_declared_list_that_resolves_to_nothing_is_refused(self, tmp_path):
        vm = _make_iso_vm(tmp_path, networks=[""])
        with pytest.raises(ProvisionError, match="declared"):
            vm._networks()

    def test_the_refusal_names_the_vm_and_what_was_declared(self, tmp_path):
        vm = _make_iso_vm(tmp_path, networks=[{"mac": "52:54:00:0c:01:01"}])
        with pytest.raises(ProvisionError) as exc:
            vm._networks()
        assert vm.vm_name in str(exc.value)

    def test_an_omitted_list_still_selects_the_default_network(self, tmp_path):
        """`_resolve_iso_config` writes `_resolved_networks: []` even when no
        `networks:` was given, so an empty list must mean "ask the next
        source", not "declared and unresolvable"."""
        vm = _make_iso_vm(tmp_path, networks=[])
        assert vm._networks() == ["default"]

    def test_an_empty_resolved_list_falls_through_to_the_raw_names(self, tmp_path):
        vm = _make_iso_vm(tmp_path, networks=[{"name": "talos-net"}],
                          _resolved_networks=[])
        assert vm._networks() == ["talos-net"]

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_no_disk_is_created_when_the_networks_are_refused(self, mock_run, tmp_path):
        """The refusal has to come before qemu-img, or a rejected VM still
        leaves a boot disk behind."""
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path, networks=[""], iso_path="/data/talos.iso")
        with pytest.raises(ProvisionError):
            vm.create()
        assert mock_run.call_count == 0, (
            f"{mock_run.call_count} shell command(s) ran before the refusal")


class TestTheBootDiskIsDeclaredQcow2:
    """#171 C4. `format=` governs volume *creation*; for a file that already
    exists libvirt consults its pool record, and a stale one gave a qcow2 boot
    disk `<driver type='raw'>` -- the guest then saw a 64 GiB image as 197 KiB.
    `driver.type=qcow2` states it outright. Nothing covered it.
    """

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_virt_install_is_told_the_driver_type(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path, iso_path="/data/talos.iso")
        assert vm.create() is True

        cmd = " ".join(str(c.args[0]) for c in mock_run.call_args_list)
        disk_args = [a for a in cmd.split() if a.startswith("--disk=")]
        assert disk_args, cmd
        assert "driver.type=qcow2" in disk_args[0], disk_args[0]

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_the_image_is_created_as_qcow2_too(self, mock_run, tmp_path):
        """The two have to agree: declaring qcow2 over a raw file is the same
        class of mismatch, pointing the other way."""
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path, iso_path="/data/talos.iso")
        vm.create()

        qemu_img = str(mock_run.call_args_list[0].args[0])
        assert "qemu-img create" in qemu_img
        assert "-f qcow2" in qemu_img, qemu_img
