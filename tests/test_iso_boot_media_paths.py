"""
An ISO-boot VM's install media, and the boot disk a failed create leaves
(#294, finding 7).

A ``cdroms:`` ``source:`` of ``~/isos/installer.iso`` reached
virt-install's ``--cdrom`` as written: the other CD-ROMs were expanded and
checked, the boot ISO neither. virt-install looked for it under the current
directory and failed, the failure was retried four more times, and the
empty boot disk the attempts had used outlived the run, so the next one
refused it as something already at its path.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.conftest import make_bare_manager

from boxman.exceptions import ProvisionError
from boxman.manager_parts import vms
from boxman.providers.libvirt import direct_vm
from boxman.providers.libvirt.bare_vm import BareVM
from boxman.providers.libvirt.iso_boot_vm import IsoBootVM
from boxman.providers.libvirt.session import LibVirtSession

pytestmark = pytest.mark.unit

SHELL_RUN = "boxman.providers.libvirt.direct_vm._shell_run"
VM = "bprj__p__bprj_c_iso01"
PROVIDER = {"use_sudo": False, "uri": "qemu:///system"}

#: what virt-install said of a boot ISO given as ``~/isos/installer.iso``
MEDIA_FAILED = (
    "ERROR    Validating install media '~/isos/installer.iso' failed: Could "
    "not start storage pool: cannot open directory '/work/~/isos': No such "
    "file or directory")


def _result(ok: bool = True, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.ok, r.failed = ok, not ok
    r.stdout, r.stderr = stdout, stderr
    r.return_code = 0 if ok else 1
    return r


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory of the test's own, with an ISO in it."""
    home = tmp_path / "home"
    (home / "isos").mkdir(parents=True)
    (home / "isos" / "installer.iso").write_bytes(b"")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _forget_the_boot_disks_made():
    yield
    for fd in direct_vm._boot_disks_made.values():
        os.close(fd)
    direct_vm._boot_disks_made.clear()


@pytest.fixture(autouse=True)
def refreshed(monkeypatch: pytest.MonkeyPatch) -> list:
    """The pool refreshes asked for: the real one asks this host's libvirt."""
    calls: list = []
    monkeypatch.setattr(direct_vm, "refresh_pools_holding",
                        lambda virsh, paths: calls.append(list(paths)) or [],
                        raising=False)
    return calls


def _manager(**vm_info):
    mgr = make_bare_manager({"project": "p", "clusters": {"c": {"vms": {
        "iso01": {"boot_order": ["cdrom", "hd"], **vm_info}}}}})
    mgr.app_config = {}
    return mgr


def _iso_vm(workdir: Path, iso_path: str, **info) -> IsoBootVM:
    return IsoBootVM(vm_name=VM,
                     info={"memory": 2048, "vcpus": 2,
                           "networks": ["default"], **info},
                     provider_config=PROVIDER, workdir=str(workdir),
                     iso_path=iso_path)


def _fresh_process() -> None:
    """What a later run starts with: no boot disk held open."""
    for fd in direct_vm._boot_disks_made.values():
        os.close(fd)
    direct_vm._boot_disks_made.clear()


class TestEveryCdromsSourceIsResolvedToOnePath:

    def test_a_tilde_in_the_boot_iso_is_expanded(self, home):
        mgr = _manager(cdroms=[{"source": "~/isos/installer.iso",
                                "install_only": True}])

        mgr._resolve_iso_config()

        vm = mgr.config["clusters"]["c"]["vms"]["iso01"]
        iso = str(home / "isos" / "installer.iso")
        assert vm["_resolved_iso_path"] == iso
        assert vm["cdroms"] == [{"source": iso, "install_only": True}]

    def test_and_in_every_other_cdrom(self, home):
        result = _manager()._inject_resolved_iso(
            {"boot_order": ["cdrom", "hd"],
             "cdroms": [{"name": "installer", "source": "/isos/installer.iso"},
                        {"name": "drivers", "source": "~/isos/drivers.iso",
                         "target": "sdc"}]},
            {})

        assert result["cdroms"][1] == {
            "name": "drivers", "source": str(home / "isos" / "drivers.iso"),
            "target": "sdc"}

    def test_a_relative_source_is_taken_from_the_current_directory(
            self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        result = _manager()._inject_resolved_iso(
            {"boot_order": ["cdrom", "hd"],
             "cdroms": [{"source": "isos/installer.iso"}]},
            {})

        assert result["_resolved_iso_path"] == str(
            tmp_path / "isos" / "installer.iso")

    def test_update_resolves_them_the_same_way(self, home):
        mgr = _manager(cdroms=[{"source": "~/isos/installer.iso"}])

        assert mgr._normalize_cdroms_for_update({VM}) == {}

        vm = mgr.config["clusters"]["c"]["vms"]["iso01"]
        assert vm["cdroms"] == [
            {"source": str(home / "isos" / "installer.iso")}]


class TestTheBootIsoReachesVirtInstallExpanded:

    @patch(SHELL_RUN, return_value=_result())
    def test_a_tilde_path_is_given_to_cdrom_expanded(self, run, home,
                                                     tmp_path):
        vm = _iso_vm(tmp_path / "wd", "~/isos/installer.iso",
                     cdroms=[{"source": "~/isos/installer.iso"}])

        assert vm.create() is True

        install = run.call_args_list[1].args[0]
        assert f"--cdrom={home}/isos/installer.iso " in install
        assert "~" not in install

    @patch(SHELL_RUN, return_value=_result())
    def test_the_boot_iso_spelt_two_ways_is_given_once(self, run, home,
                                                       tmp_path):
        iso = str(home / "isos" / "installer.iso")
        vm = _iso_vm(tmp_path / "wd", iso,
                     cdroms=[{"source": "~/isos/installer.iso"}])

        vm.create()

        install = run.call_args_list[1].args[0]
        assert install.count(iso) == 1
        assert "device=cdrom" not in install


class TestAMissingBootIsoIsRefusedBeforeTheBootDisk:

    @patch(SHELL_RUN, return_value=_result())
    def test_it_names_the_vm_and_the_path(self, run, home, tmp_path):
        wd = tmp_path / "wd"

        with pytest.raises(ProvisionError) as raised:
            _iso_vm(wd, "~/isos/missing.iso",
                    cdroms=[{"source": "~/isos/missing.iso"}]).create()

        message = str(raised.value)
        assert f"vm {VM}: the boot ISO does not exist" in message
        assert str(home / "isos" / "missing.iso") in message
        # neither qemu-img nor virt-install ran, and nothing was made
        assert run.call_count == 0
        assert not wd.exists() or list(wd.iterdir()) == []

    @patch(SHELL_RUN, return_value=_result())
    def test_a_directory_in_its_place_is_refused_too(self, run, home,
                                                     tmp_path):
        with pytest.raises(ProvisionError, match="is a directory"):
            _iso_vm(tmp_path / "wd", "~/isos").create()
        assert run.call_count == 0

    @patch(SHELL_RUN, return_value=_result())
    def test_one_this_user_cannot_look_up_is_left_to_virt_install(
            self, run, home, tmp_path):
        """virt-install may run as root, and open what boxman cannot."""
        locked = home / "locked"
        locked.mkdir()
        (locked / "installer.iso").write_bytes(b"")
        locked.chmod(0)
        try:
            assert _iso_vm(tmp_path / "wd",
                           str(locked / "installer.iso")).create() is True
        finally:
            locked.chmod(0o755)
        assert (f"--cdrom={locked}/installer.iso "
                in run.call_args_list[1].args[0])


class TestARefusedIsoIsNotRetried:
    """A refusal that comes from the configuration is the same on every
    attempt; retried, it cost four more attempts and 20 s of sleeps."""

    @staticmethod
    def _virt_install_fails(cmd: str, **_kwargs) -> MagicMock:
        # as on the ~ path, and with no domain left defined after it
        if "virt-install" in cmd or " domstate " in cmd:
            return _result(ok=False, stderr=MEDIA_FAILED)
        return _result()

    def _clone(self, vm_info: dict, workdir: Path, monkeypatch):
        sleeps: list = []
        monkeypatch.setattr(vms.time, "sleep", sleeps.append)
        session = LibVirtSession(config={"provider": {"libvirt": PROVIDER}})
        with patch(SHELL_RUN, side_effect=self._virt_install_fails) as run, \
                pytest.raises(ProvisionError) as raised:
            vms._clone_with_retry(session, {"workdir": str(workdir)},
                                  vm_info, VM)
        return raised.value, sleeps, run

    def test_a_missing_boot_iso_fails_on_the_first_attempt(
            self, home, tmp_path, monkeypatch):
        missing = str(home / "isos" / "missing.iso")
        error, sleeps, run = self._clone(
            {"boot_order": ["cdrom", "hd"], "networks": ["default"],
             "_resolved_iso_path": missing,
             "cdroms": [{"source": missing}]},
            tmp_path / "wd", monkeypatch)

        assert sleeps == []
        assert missing in str(error)
        assert run.call_count == 0
        assert not (tmp_path / "wd").exists()

    def test_and_so_does_a_missing_extra_cdrom(self, home, tmp_path,
                                               monkeypatch):
        iso = str(home / "isos" / "installer.iso")
        error, sleeps, run = self._clone(
            {"boot_order": ["cdrom", "hd"], "networks": ["default"],
             "_resolved_iso_path": iso,
             "cdroms": [{"source": iso},
                        {"name": "drivers",
                         "source": str(home / "isos" / "drivers.iso")}]},
            tmp_path / "wd", monkeypatch)

        assert sleeps == []
        assert "drivers.iso" in str(error)
        assert run.call_count == 0

    def test_a_refused_install_is_not_retried(self, monkeypatch):
        from boxman.exceptions import InstallRefusedError

        sleeps: list = []
        monkeypatch.setattr(vms.time, "sleep", sleeps.append)
        provider = MagicMock()
        provider.clone_vm.side_effect = InstallRefusedError(
            f"vm {VM}: the boot ISO does not exist: /isos/missing.iso")

        with pytest.raises(InstallRefusedError):
            vms._clone_with_retry(provider, {"workdir": "/w"},
                                  {"boot_order": ["cdrom", "hd"]}, VM)

        assert provider.clone_vm.call_count == 1
        assert sleeps == []


class TestTheBootDiskOfAFailedCreate:
    """virt-install failed before it defined the domain: the empty boot
    disk this run made is no one's, and the next run must be able to make
    it again."""

    @staticmethod
    def _failing(listed: bool | None = False, defined: bool = False,
                 said: str = MEDIA_FAILED):
        """``_shell_run`` for a create whose virt-install fails. libvirt
        then has the domain (*defined*); or lists the domain by its name
        (*listed* true), lists none by it (false), or cannot be asked
        (None)."""
        def run(cmd: str, **_kwargs) -> MagicMock:
            if "virt-install" in cmd:
                return _result(ok=False, stderr=said)
            if " domstate " in cmd:
                return _result(ok=defined)
            if " list " in cmd:
                if listed is None:
                    return _result(ok=False, stderr="failed to connect")
                return _result(stdout="other-vm\n"
                               + (f"{VM}\n" if listed else ""))
            return _result()        # qemu-img
        return run

    @staticmethod
    def _paths(home: Path, tmp_path: Path) -> tuple[Path, Path, str]:
        wd = tmp_path / "wd"
        return wd, wd / f"{VM}.qcow2", str(home / "isos" / "installer.iso")

    def test_it_is_removed_when_libvirt_lists_no_domain_by_its_name(
            self, home, tmp_path, refreshed):
        wd, disk, iso = self._paths(home, tmp_path)

        with patch(SHELL_RUN, side_effect=self._failing()):
            assert _iso_vm(wd, iso).create() is False

        assert os.listdir(wd) == []
        assert refreshed == [[str(disk)]]
        # and let go of: nothing holds the removed image open
        assert str(disk) not in direct_vm._boot_disks_made

    def test_the_next_run_makes_it_again(self, home, tmp_path):
        wd, disk, iso = self._paths(home, tmp_path)
        with patch(SHELL_RUN, side_effect=self._failing()):
            assert _iso_vm(wd, iso).create() is False
        _fresh_process()

        with patch(SHELL_RUN, return_value=_result()):
            assert _iso_vm(wd, iso).create() is True

        assert disk.is_file()

    def test_it_is_removed_when_virt_install_refused_the_name(
            self, home, tmp_path):
        """virt-install refuses a name in use before it defines anything."""
        wd, disk, iso = self._paths(home, tmp_path)
        in_use = f"ERROR    Guest name '{VM}' is already in use."

        with patch(SHELL_RUN, side_effect=self._failing(said=in_use)), \
                pytest.raises(ProvisionError, match="already defined"):
            _iso_vm(wd, iso).create()

        assert not disk.exists()

    def test_a_pxe_vm_s_is_removed_too(self, tmp_path):
        wd = tmp_path / "wd"
        vm = BareVM(vm_name=VM, info={"networks": ["default"]},
                    provider_config=PROVIDER, workdir=str(wd))

        with patch(SHELL_RUN, side_effect=self._failing(said="ERROR x")):
            assert vm.create() is False

        assert os.listdir(wd) == []

    def test_no_attempt_of_a_retried_create_leaves_its_disk(
            self, home, tmp_path, monkeypatch):
        wd, disk, iso = self._paths(home, tmp_path)
        monkeypatch.setattr(vms.time, "sleep", lambda _s: None)
        session = LibVirtSession(config={"provider": {"libvirt": PROVIDER}})

        with patch(SHELL_RUN, side_effect=self._failing()) as run, \
                pytest.raises(RuntimeError):
            vms._clone_with_retry(
                session, {"workdir": str(wd)},
                {"boot_order": ["cdrom", "hd"], "networks": ["default"],
                 "_resolved_iso_path": iso, "cdroms": [{"source": iso}]},
                VM)

        installs = [c for c in run.call_args_list if "virt-install" in c.args[0]]
        assert len(installs) == 5
        assert os.listdir(wd) == []

    def test_it_is_kept_when_libvirt_cannot_be_asked(self, home, tmp_path,
                                                     refreshed):
        """A domain virt-install may have defined would use it; a retry in
        this process takes it over."""
        wd, disk, iso = self._paths(home, tmp_path)

        with patch(SHELL_RUN, side_effect=self._failing(listed=None)):
            assert _iso_vm(wd, iso).create() is False
        assert disk.is_file()
        assert refreshed == []

        with patch(SHELL_RUN, return_value=_result()) as run:
            assert _iso_vm(wd, iso).create() is True
        assert not any("qemu-img" in c.args[0] for c in run.call_args_list)

    def test_it_is_kept_when_the_listing_shows_the_domain(self, home,
                                                          tmp_path):
        wd, disk, iso = self._paths(home, tmp_path)

        with patch(SHELL_RUN, side_effect=self._failing(listed=True)):
            assert _iso_vm(wd, iso).create() is False

        assert disk.is_file()

    def test_it_is_kept_with_the_domain_virt_install_left_defined(
            self, home, tmp_path):
        wd, disk, iso = self._paths(home, tmp_path)

        with patch(SHELL_RUN, side_effect=self._failing(defined=True)), \
                pytest.raises(ProvisionError, match="left as it is"):
            _iso_vm(wd, iso).create()

        assert disk.is_file()

    def test_a_file_put_in_its_place_meanwhile_is_left_as_it_is(
            self, home, tmp_path, refreshed):
        wd, disk, iso = self._paths(home, tmp_path)
        failing = self._failing()

        def replaced_before_the_removal(cmd: str, **kwargs) -> MagicMock:
            if " list " in cmd:
                disk.unlink()
                disk.write_bytes(b"someone else's")
            return failing(cmd, **kwargs)

        with patch(SHELL_RUN, side_effect=replaced_before_the_removal):
            assert _iso_vm(wd, iso).create() is False

        assert disk.read_bytes() == b"someone else's"
        assert refreshed == []
