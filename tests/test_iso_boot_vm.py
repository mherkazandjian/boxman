"""Unit tests for boxman.providers.libvirt.iso_boot_vm.IsoBootVM."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import PartialInstallError, ProvisionError
from boxman.providers.libvirt.direct_vm import normalize_disk_size
from boxman.providers.libvirt.iso_boot_vm import IsoBootVM

pytestmark = pytest.mark.unit


def _result(ok: bool = True, stderr: str = "", stdout: str = "") -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.ok = ok
    r.stdout = stdout
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
        # qemu-img, virt-install, then `virsh domstate`: no domain was left
        mock_run.side_effect = [_result(ok=True),
                                _result(ok=False, stderr="permission denied"),
                                _result(ok=False)]
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


class TestEveryCdromReachesTheInstaller:
    """G1: a cdroms: entry after the boot ISO was attached by the configure
    step, once virt-install had started the installer, and so reached only
    the persistent definition (libvirt cannot hot-plug a SATA/IDE drive). An
    installer that needs a second medium (Windows Setup: drivers, an answer
    file) never saw it."""

    @staticmethod
    def _iso(tmp_path, name):
        path = tmp_path / name
        path.write_bytes(b"")
        return str(path)

    def _vm(self, tmp_path, *extra):
        boot = self._iso(tmp_path, "boot.iso")
        return _make_iso_vm(tmp_path, iso_path=boot,
                            cdroms=[{"name": "boot", "source": boot}, *extra])

    @staticmethod
    def _virt_install(mock_run):
        return mock_run.call_args_list[1][0][0]

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_extra_cdroms_go_to_virt_install_in_declared_order(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        drivers = self._iso(tmp_path, "drivers.iso")
        answer = self._iso(tmp_path, "answer.iso")
        vm = self._vm(tmp_path, {"name": "drivers", "source": drivers},
                      {"source": answer})
        assert vm.create() is True
        cmd = self._virt_install(mock_run)
        positions = [cmd.index(arg) for arg in (
            "--cdrom=",
            f"--disk=path={drivers},device=cdrom,readonly=on",
            f"--disk=path={answer},device=cdrom,readonly=on")]
        assert positions == sorted(positions)

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_the_boot_iso_is_not_attached_twice(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = self._vm(tmp_path)
        vm.create()
        cmd = self._virt_install(mock_run)
        assert cmd.count(vm.iso_path) == 1
        assert "device=cdrom" not in cmd

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_an_explicit_target_and_its_bus_are_passed(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        sata = self._iso(tmp_path, "sata.iso")
        ide = self._iso(tmp_path, "ide.iso")
        vm = self._vm(tmp_path, {"source": sata, "target": "sdc"},
                      {"source": ide, "target": "hdd"})
        vm.create()
        cmd = self._virt_install(mock_run)
        assert f"path={sata},device=cdrom,readonly=on,target.dev=sdc,target.bus=sata" in cmd
        assert f"path={ide},device=cdrom,readonly=on,target.dev=hdd,target.bus=ide" in cmd

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_a_missing_iso_is_refused_before_the_boot_disk_is_made(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = self._vm(tmp_path, {"name": "drivers",
                                 "source": str(tmp_path / "absent.iso")})
        with pytest.raises(ProvisionError, match="absent.iso"):
            vm.create()
        assert mock_run.call_count == 0

    def test_an_unresolved_entry_is_refused(self, tmp_path):
        vm = self._vm(tmp_path, {"name": "drivers"})
        with pytest.raises(ProvisionError, match="no resolved source"):
            vm._media_args()


class TestTheConfigureStepLeavesIsoBootCdromsAlone:
    """IsoBootVM hands every cdrom to virt-install, so the configure step
    that follows the create must not attach them a second time."""

    @staticmethod
    def _configure(vm_info):
        from boxman.manager import BoxmanManager
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {"project": "demo", "clusters": {
            "c1": {"workdir": "/tmp/ws", "vms": {"vm1": vm_info}}}}
        mgr.provider = MagicMock()
        mgr.provider.has_managed_save.return_value = False
        mgr.logger = MagicMock()
        mgr.cache = MagicMock()
        mgr.cache.projects = {}
        mgr._configure_and_start_vm(
            "c1", mgr.config["clusters"]["c1"], "vm1", vm_info)
        return mgr.provider

    def test_an_iso_boot_vm_gets_none_attached_after_create(self):
        provider = self._configure({
            "boot_order": ["cdrom", "hd"],
            "_resolved_iso_path": "/c/boot.iso",
            "cdroms": [{"name": "boot", "source": "/c/boot.iso"},
                       {"name": "drivers", "source": "/c/drivers.iso"}],
        })
        provider.configure_vm_cdroms.assert_not_called()

    def test_a_cloned_vm_still_gets_its_cdroms_attached(self):
        cdroms = [{"name": "tools", "source": "/c/tools.iso"}]
        provider = self._configure({"cdroms": cdroms})
        provider.configure_vm_cdroms.assert_called_once()
        assert provider.configure_vm_cdroms.call_args.kwargs["cdroms"] == cdroms


class TestTheGuestAgentChannel:
    """G3: virt-install adds the qemu-guest-agent channel only for OSes it
    knows ship virtio-serial, so a Windows guest had none and `boxman
    storage trim` and agent IP lookup failed on it."""

    CHANNEL = "--channel=unix,target.type=virtio,name=org.qemu.guest_agent.0"

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_every_direct_install_vm_gets_one(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        vm = _make_iso_vm(tmp_path)
        vm.create()
        assert mock_run.call_args_list[1][0][0].count(self.CHANNEL) == 1

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_one_the_box_adds_itself_is_not_doubled(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        own = "--channel unix,target_type=virtio,name=org.qemu.guest_agent.0"
        vm = _make_iso_vm(tmp_path, virt_install_extra_args=[own])
        vm.create()
        cmd = mock_run.call_args_list[1][0][0]
        assert cmd.count("org.qemu.guest_agent.0") == 1
        assert own in cmd


class TestThereIsNoInstallPhase:
    """G2: `--cdrom` alone made virt-install run an install phase, whose
    definition boots the CD-ROM only and has on_reboot=destroy. With
    boxman's --wait=0 nothing restarted it, so the guest stayed off after
    its installer's first reboot (Windows Setup's, 85 s in)."""

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_virt_install_is_told_not_to_run_one(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        _make_iso_vm(tmp_path, iso_path="/data/talos.iso").create()
        assert "--install=no_install=yes" in mock_run.call_args_list[1][0][0]

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_the_iso_stays_a_cdrom_so_the_os_is_still_detected(self, mock_run,
                                                                tmp_path):
        """virt-install detects the OS only from `--cdrom`/`--location`
        media; the same ISO as a CD-ROM `--disk` lost the OS id, and with it
        Windows' localtime clock (virt-install dry run)."""
        mock_run.return_value = _result(ok=True)
        _make_iso_vm(tmp_path, iso_path="/data/talos.iso").create()
        cmd = mock_run.call_args_list[1][0][0]
        assert "--cdrom=/data/talos.iso" in cmd
        assert "--os-variant=detect=on,require=off" in cmd
        assert "device=cdrom" not in cmd


class TestOsVariant:
    """G4: virt-install detects the OS with the host's osinfo-db, and two
    hosts read the same Windows Server 2022 ISO as 2022 and as 2025.
    `os_variant:` names it, as a template's does."""

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_one_given_is_passed_instead_of_detection(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        _make_iso_vm(tmp_path, os_variant="win2k22").create()
        cmd = mock_run.call_args_list[1][0][0]
        assert "--os-variant=win2k22" in cmd
        assert "detect=on" not in cmd

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_it_is_shell_quoted(self, mock_run, tmp_path):
        mock_run.return_value = _result(ok=True)
        _make_iso_vm(tmp_path, os_variant="win2k22; true").create()
        assert "--os-variant='win2k22; true'" in mock_run.call_args_list[1][0][0]


class TestAFailedInstallThatLeftItsDomain:
    """A virt-install that failed after defining the domain (a --wait that
    ran out) was retried four more times. Each retry failed on the name
    being in use, and those errors were what the operator saw."""

    FAILED = "Domain installation does not appear to have been successful."
    TIMED_OUT = ("Installation has exceeded specified time limit. "
                 "Exiting application.")

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_it_raises_with_virt_install_s_own_words(self, mock_run, tmp_path):
        # qemu-img, virt-install, then `virsh domstate`: the domain is there
        mock_run.side_effect = [_result(ok=True),
                                _result(ok=False, stderr=self.FAILED),
                                _result(ok=True)]
        with pytest.raises(PartialInstallError) as exc:
            _make_iso_vm(tmp_path).create()
        assert self.FAILED in str(exc.value)
        assert "boxman destroy" in str(exc.value)

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_a_timeout_reported_on_stdout_is_in_the_message(self, mock_run,
                                                            tmp_path):
        """virt-install prints why a --wait gave up to stdout; stderr held
        only osinfo warnings (seen on a real `--wait=1`)."""
        mock_run.side_effect = [
            _result(ok=True),
            _result(ok=False, stdout=self.TIMED_OUT,
                    stderr="WARNING  Using --osinfo generic"),
            _result(ok=True)]
        with pytest.raises(PartialInstallError, match="exceeded specified time"):
            _make_iso_vm(tmp_path).create()

    @patch("boxman.providers.libvirt.direct_vm._shell_run")
    def test_the_domain_is_looked_up_by_the_vm_s_name(self, mock_run, tmp_path):
        mock_run.side_effect = [_result(ok=True),
                                _result(ok=False, stderr=self.FAILED),
                                _result(ok=False)]
        # no domain: an ordinary failure, which the caller may retry
        assert _make_iso_vm(tmp_path).create() is False
        lookup = mock_run.call_args_list[2][0][0]
        assert " domstate " in lookup
        assert lookup.endswith(" iso-test01")

    def test_it_is_not_retried(self, monkeypatch):
        from boxman.manager_parts import vms

        sleeps: list = []
        monkeypatch.setattr(vms.time, "sleep", sleeps.append)
        provider = MagicMock()
        provider.clone_vm.side_effect = PartialInstallError(self.FAILED)

        with pytest.raises(PartialInstallError):
            vms._clone_with_retry(provider, {"workdir": "/w"},
                                  {"boot_order": ["cdrom", "hd"]}, "win01")

        assert provider.clone_vm.call_count == 1
        assert sleeps == []
