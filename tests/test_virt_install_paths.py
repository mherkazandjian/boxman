"""File paths in the strings virt-install parses itself.

virt-install splits each ``--disk`` value into sub-options on commas, so a
path interpolated there with shell quoting only lost everything after a
comma in it: a project directory ``lab,v2`` gave ``Unknown --disk options:
['v2/…']``. A quote or backslash was read by the same parser, and the five
characters ``& ' " < >`` reach the domain definition escaped twice by any
option (virt-install 5.0.0, ``--print-xml``), so the domain names another
file. Those are refused before virt-install runs.
"""
from __future__ import annotations

import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import ProvisionError
from boxman.providers.libvirt import commands
from boxman.providers.libvirt.bare_vm import BareVM
from boxman.providers.libvirt.cloudinit import CloudInitTemplate
from boxman.providers.libvirt.iso_boot_vm import IsoBootVM

pytestmark = pytest.mark.unit

PROVIDER_CONFIG = {"use_sudo": False, "uri": "qemu:///system"}
DIRECT_SHELL_RUN = "boxman.providers.libvirt.direct_vm._shell_run"
CLOUDINIT_SHELL_RUN = "boxman.providers.libvirt.cloudinit._shell_run"


def _result(ok: bool = True) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.ok = ok
    r.failed = not ok
    r.stdout = r.stderr = ""
    return r


def _virt_install_reads(optstr: str) -> list[tuple[str, str | None]]:
    """The sub-options virt-install reads in *optstr*, in order.

    virtinst's own ``cli.parse_optstr_tuples`` (virt-install 5.0.0): shlex
    in POSIX mode, with ``,`` as the only separator.
    """
    lexer = shlex.shlex(optstr, posix=True)
    lexer.commenters = ""
    lexer.whitespace = ","
    lexer.whitespace_split = True
    return [tuple(opt.split("=", 1)) if "=" in opt else (opt, None)
            for opt in lexer]


def _argv(cmd: str) -> list[str]:
    """The arguments the shell hands virt-install for *cmd*."""
    return shlex.split(cmd)


def _disk_paths(cmd: str) -> list[str]:
    """The ``path=`` virt-install reads in each ``--disk`` of *cmd*; an
    option it would not know (what a comma split off) fails the test."""
    paths = []
    for arg in _argv(cmd):
        if not arg.startswith("--disk="):
            continue
        options = _virt_install_reads(arg[len("--disk="):])
        names = [name for name, _value in options]
        assert all(name and "/" not in name for name in names), options
        paths.append(dict(options)["path"])
    return paths


def _iso(directory: Path, name: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"")
    return str(path)


class TestVirtInstallReadsThePathBack:
    """virt_install_path gives virt-install a path it reads as itself."""

    @pytest.mark.parametrize("path", [
        "/w/lab,v2/x.iso", "/w/a,,b.iso", "/w/back\\slash.iso",
        "/w/sp ace/x.iso", "/w/eq=sign;semi.iso"])
    def test_as_a_sub_option_value(self, path):
        value = commands.virt_install_path(path, "vm1")
        assert _virt_install_reads(f"path={value},device=cdrom") == [
            ("path", path), ("device", "cdrom")]

    def test_a_path_its_parser_reads_as_itself_is_left_as_it_is(self):
        # so the commands for ordinary paths are the ones they always were
        assert commands.virt_install_path("/w/sp ace/x.iso", "vm1") \
            == "/w/sp ace/x.iso"

    def test_a_whole_option_value_is_not_quoted(self):
        # --cdrom=<path> is not split, so a quote would be part of the path
        assert commands.virt_install_path(
            "/w/lab,v2/x.iso", "vm1", sub_option=False) == "/w/lab,v2/x.iso"


class TestWhatVirtInstallEscapesTwiceIsRefused:
    """``& ' " < >`` reach the domain definition escaped a second time
    (``'`` as ``&apos;``) whichever option carries the path."""

    @pytest.mark.parametrize("char", list("&'\"<>"))
    @pytest.mark.parametrize("sub_option", [True, False])
    def test_it_raises_naming_the_vm_and_the_path(self, char, sub_option):
        path = f"/w/lab{char}v2/x.iso"
        with pytest.raises(ProvisionError) as raised:
            commands.virt_install_path(path, "vm1", sub_option=sub_option)
        assert "vm vm1" in str(raised.value)
        assert path in str(raised.value)


class TestAnIsoBootVMsPathsReachVirtInstallWhole:

    @staticmethod
    def _vm(tmp_path: Path, boot: str, *extra: str, workdir: Path | None = None):
        return IsoBootVM(
            vm_name="iso01",
            info={"memory": 2048, "vcpus": 2, "networks": ["default"],
                  "cdroms": [{"name": "boot", "source": boot},
                             *({"source": s} for s in extra)]},
            provider_config=PROVIDER_CONFIG,
            workdir=str(workdir or tmp_path),
            iso_path=boot,
        )

    @patch(DIRECT_SHELL_RUN, return_value=_result())
    def test_a_comma_in_a_cdroms_source(self, run, tmp_path):
        boot = _iso(tmp_path, "boot.iso")
        drivers = _iso(tmp_path / "lab,v2", "drivers.iso")
        assert self._vm(tmp_path, boot, drivers).create() is True
        assert _disk_paths(run.call_args_list[1].args[0])[1:] == [drivers]

    @patch(DIRECT_SHELL_RUN, return_value=_result())
    def test_a_comma_in_the_boot_iso_and_the_workdir(self, run, tmp_path):
        work = tmp_path / "lab,v2"
        boot = _iso(work, "boot.iso")
        assert self._vm(tmp_path, boot, workdir=work).create() is True
        argv = _argv(run.call_args_list[1].args[0])
        assert f"--cdrom={boot}" in argv
        assert _disk_paths(run.call_args_list[1].args[0]) == [
            str(work / "iso01.qcow2")]

    @pytest.mark.parametrize("char", ["'", '"', "&"])
    @patch(DIRECT_SHELL_RUN, return_value=_result())
    def test_a_quote_in_a_cdroms_source_is_refused_before_the_boot_disk(
            self, run, char, tmp_path):
        boot = _iso(tmp_path, "boot.iso")
        answer = _iso(tmp_path / f"it{char}s", "answer.iso")
        with pytest.raises(ProvisionError, match="vm iso01") as raised:
            self._vm(tmp_path, boot, answer).create()
        assert answer in str(raised.value)
        assert run.call_count == 0   # neither qemu-img nor virt-install

    @patch(DIRECT_SHELL_RUN, return_value=_result())
    def test_a_quote_in_the_boot_iso_is_refused(self, run, tmp_path):
        boot = _iso(tmp_path / "it's", "boot.iso")
        with pytest.raises(ProvisionError, match="vm iso01"):
            self._vm(tmp_path, boot).create()
        assert run.call_count == 0


class TestAVMsOwnDiskReachesVirtInstallWhole:
    """The boot disk every direct-install VM (PXE and ISO) is made with,
    under its workdir."""

    @staticmethod
    def _vm(workdir: Path) -> BareVM:
        return BareVM(vm_name="pxe01", info={"networks": ["default"]},
                      provider_config=PROVIDER_CONFIG, workdir=str(workdir))

    @patch(DIRECT_SHELL_RUN, return_value=_result())
    def test_a_comma_in_the_workdir(self, run, tmp_path):
        work = tmp_path / "lab,v2"
        assert self._vm(work).create() is True
        assert _disk_paths(run.call_args_list[1].args[0]) == [
            str(work / "pxe01.qcow2")]

    @patch(DIRECT_SHELL_RUN, return_value=_result())
    def test_a_quote_in_the_workdir_is_refused_before_qemu_img(self, run,
                                                               tmp_path):
        work = tmp_path / 'say "hi"'
        with pytest.raises(ProvisionError, match="vm pxe01") as raised:
            self._vm(work).create()
        assert str(work / "pxe01.qcow2") in str(raised.value)
        assert run.call_count == 0   # neither qemu-img nor virt-install


class TestATemplatesDiskAndSeedReachVirtInstallWhole:

    @staticmethod
    def _template(workdir: Path) -> CloudInitTemplate:
        t = CloudInitTemplate(template_name="tmpl", image_path="/base.qcow2",
                              workdir=str(workdir),
                              provider_config=PROVIDER_CONFIG)
        t._check_vm_exists = MagicMock(return_value=False)
        t._resolve_bridge = MagicMock(return_value="virbr0")
        t._verify_dhcp_on_network = MagicMock(return_value=True)
        t.copy_base_image = MagicMock(
            side_effect=lambda dst: Path(dst).write_bytes(b"disk") or True)
        t.prepare_nocloud_dir = MagicMock(return_value="/nocloud")
        t.build_seed_iso = MagicMock(
            side_effect=lambda _nocloud, iso: Path(iso).write_bytes(b"seed")
            or True)
        t.verify_and_shutdown = MagicMock(return_value=True)
        return t

    def test_a_comma_in_the_workdir(self, tmp_path):
        work = tmp_path / "lab,v2"
        t = self._template(work)
        with patch.object(t.virsh, "execute", return_value=_result()), \
                patch(CLOUDINIT_SHELL_RUN, return_value=_result()) as run:
            assert t.create_template() is True
        assert _disk_paths(run.call_args.args[0]) == [
            str(work / "tmpl" / "tmpl.qcow2"), str(work / "tmpl" / "seed.iso")]

    def test_a_quote_in_the_workdir_fails_it_before_the_base_image_is_had(
            self, tmp_path, captured_logs):
        work = tmp_path / "it's"
        t = self._template(work)
        with patch.object(t.virsh, "execute", return_value=_result()), \
                patch(CLOUDINIT_SHELL_RUN, return_value=_result()) as run:
            assert t.create_template() is False
        run.assert_not_called()
        t.copy_base_image.assert_not_called()
        errors = " ".join(r.getMessage() for r in captured_logs.records)
        assert "cannot create template 'tmpl'" in errors
        assert str(work / "tmpl" / "tmpl.qcow2") in errors
