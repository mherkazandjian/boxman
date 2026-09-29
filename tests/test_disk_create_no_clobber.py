"""
Issue #215: a new disk image is never created over anything at its path.

``qemu-img create`` writes through whatever holds its path -- it truncates a
file, follows a symlink to truncate what it points at, and creates a
dangling one's target -- and :meth:`DiskManager.create_disk` ran it on the
target path with no check at all. A provision (or an ``update`` adding a
VM) silently replaced a disk a teardown had kept, another VM's disk or the
base of a snapshot chain with an empty image.

The tests run the production code through a real shell against stubs put
first on ``PATH``. The ``qemu-img`` stub opens its path the way the real one
does -- ``O_RDWR | O_CREAT``: through a symlink, creating a dangling one's
target, failing only on a directory -- so an unguarded call really destroys
the fixture, and it records its argv, so quoting is checked on what the
shell actually passed. The class marked for the real ``qemu-img`` runs it
when it is installed.

The new exception is reached as ``exceptions.DiskPathOccupiedError`` inside
each test, not imported, so that this module also runs against a tree
without it: there the reproductions fail on the damage, which is the point.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman import exceptions
from boxman.exceptions import ProvisionError
from boxman.providers.libvirt import direct_vm
from boxman.providers.libvirt.bare_vm import BareVM
from boxman.providers.libvirt.disk import DiskManager
from boxman.providers.libvirt.disk_ownership import (
    read_disk_records,
    records_from_xml,
)
from boxman.providers.libvirt.session import LibVirtSession
from boxman.providers.libvirt.vm_differ import VMStateDiffer

pytestmark = pytest.mark.unit

#: what a kept disk holds
PRECIOUS = b"KEEP ME: the data disk a teardown kept\n" * 64

#: slash-free, so a legal file name, and a shell that expands it leaves a
#: file named `pwned` in the cwd (each test runs in its own tmp_path)
LIVE_PAYLOAD = "a$(touch pwned)b"

_QEMU_IMG = r'''#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(here, "qemu-img.log"), "a") as fh:
    fh.write(chr(31).join(sys.argv[1:]) + chr(10))
ctl = {}
if os.path.exists(os.path.join(here, "qemu-img.json")):
    with open(os.path.join(here, "qemu-img.json")) as fh:
        ctl = json.load(fh)
if ctl.get("fail"):
    sys.stderr.write("qemu-img: stub failure" + chr(10))
    sys.exit(1)
if sys.argv[1] != "create":
    sys.exit(0)
fmt, path, size = sys.argv[3], sys.argv[4], sys.argv[5]
race = ctl.get("race")
if race:
    # something takes the disk's path while the image is being made
    if ctl["race_kind"] == "file":
        with open(race, "wb") as fh:
            fh.write(ctl["race_content"].encode())
    elif ctl["race_kind"] == "dangling":
        os.symlink(ctl["race_content"], race)
    else:
        os.mkdir(race)
try:
    # what qemu-img's file driver does with its path
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
except OSError as exc:
    sys.stderr.write("qemu-img: " + path + ": " + exc.strerror + chr(10))
    sys.exit(1)
os.ftruncate(fd, 0)
os.write(fd, ("QFI" + fmt + ":" + size).encode())
os.close(fd)
'''

_VIRT_INSTALL = r'''#!/usr/bin/env python3
import os, sys
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(here, "virt-install.log"), "a") as fh:
    fh.write(chr(31).join(sys.argv[1:]) + chr(10))
failures = os.path.join(here, "virt-install.failures")
if os.path.exists(failures):
    left = int(open(failures).read())
    if left > 0:
        open(failures, "w").write(str(left - 1))
        sys.stderr.write("virt-install: stub failure" + chr(10))
        sys.exit(1)
'''

# the runtime container runs the `bash -c` it is handed on the workdir,
# which is bind-mounted at the same path
_DOCKER = r'''#!/usr/bin/env python3
import os, sys
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(here, "docker.log"), "a") as fh:
    fh.write(chr(31).join(sys.argv[1:]) + chr(10))
args = sys.argv[1:]
if args[:4] != ["exec", "--user", "root", "boxman-rt"] or args[4:6] != ["bash", "-c"]:
    sys.exit(125)
os.execvp("bash", ["bash", "-c", args[6]])
'''


class Stubs:
    """The stubs on ``PATH``: what they were called with, and their knobs."""

    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"

    def calls(self, name: str = "qemu-img") -> list[list[str]]:
        log = self.root / f"{name}.log"
        if not log.exists():
            return []
        return [line.split(chr(31))
                for line in log.read_text().splitlines() if line]

    def fail_qemu_img(self) -> None:
        (self.root / "qemu-img.json").write_text(json.dumps({"fail": True}))

    def race(self, path: Path, kind: str, content: str = "") -> None:
        (self.root / "qemu-img.json").write_text(json.dumps(
            {"race": str(path), "race_kind": kind, "race_content": content}))

    def fail_virt_install(self, times: int) -> None:
        (self.root / "virt-install.failures").write_text(str(times))

    def add(self, name: str, body: str) -> None:
        script = self.bin / name
        script.write_text(body)
        script.chmod(0o755)


@pytest.fixture
def stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stubs:
    found = Stubs(tmp_path / "stubs")
    found.bin.mkdir(parents=True)
    found.add("qemu-img", _QEMU_IMG)
    found.add("virt-install", _VIRT_INSTALL)
    monkeypatch.setenv("PATH", f"{found.bin}{os.pathsep}{os.environ['PATH']}")
    # whatever a badly quoted command executes lands here, where it is seen
    monkeypatch.chdir(tmp_path)
    return found


def _dm(**provider) -> DiskManager:
    return DiskManager(vm_name="vm01",
                       provider_config={"use_sudo": False,
                                        "uri": "qemu:///system", **provider})


def _no_record():
    """Keep the ownership bookkeeping after an attach off the real virsh:
    a disk only gets that far when a guard failed."""
    return patch("boxman.providers.libvirt.disk.record_attached_disk")


def _outcome(call):
    """What *call* returned, or the exception it raised."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - the outcome is under test
        return exc


def _tree(root: Path) -> dict[str, tuple]:
    """Every entry under *root*, without following a symlink."""
    found: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            rel = str(path.relative_to(root))
            if path.is_symlink():
                found[rel] = ("symlink", os.readlink(path))
            elif path.is_dir():
                found[rel] = ("directory",)
            else:
                found[rel] = ("file", path.read_bytes())
    return found


def _outside(path: Path) -> bytes | None:
    return path.read_bytes() if os.path.lexists(path) else None


def _occupy(target: Path, kind: str, outside: Path) -> None:
    """Put *kind* at *target*; a symlink points at *outside*."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        target.write_bytes(PRECIOUS)
    elif kind == "symlink":
        outside.write_bytes(PRECIOUS)
        target.symlink_to(outside)
    elif kind == "dangling":
        target.symlink_to(outside)
    else:
        target.mkdir()
        (target / "inside.qcow2").write_bytes(PRECIOUS)


_KIND_WORDS = {"file": "a file", "symlink": "a symlink",
               "dangling": "a symlink", "directory": "a directory"}


class TestAnExistingEntryIsNeverWritten:
    """The reproduction: every one of these was written through before."""

    @pytest.mark.parametrize("kind", sorted(_KIND_WORDS))
    def test_create_disk_refuses_and_leaves_it_as_it_was(
            self, stubs: Stubs, tmp_path: Path, kind: str):
        target = tmp_path / "wd" / "vm01_data.qcow2"
        outside = tmp_path / "elsewhere.qcow2"
        _occupy(target, kind, outside)
        before = _tree(tmp_path / "wd"), _outside(outside)

        outcome = _outcome(lambda: _dm().create_disk(str(target), 16))

        assert (_tree(tmp_path / "wd"), _outside(outside)) == before, (
            "what was at the disk's path, or what it points at, changed")
        assert isinstance(outcome, exceptions.DiskPathOccupiedError), outcome
        message = str(outcome)
        assert str(target) in message
        assert _KIND_WORDS[kind] in message
        assert "attach_only" in message and "remove or rename" in message
        # refused before qemu-img was run at all
        assert stubs.calls() == []


@pytest.mark.skipif(shutil.which("qemu-img") is None, reason="needs qemu-img")
class TestWithTheRealQemuImg:

    def test_an_existing_file_survives(self, tmp_path: Path):
        target = tmp_path / "vm01_data.qcow2"
        target.write_bytes(PRECIOUS)

        outcome = _outcome(lambda: _dm().create_disk(str(target), 16))

        assert target.read_bytes() == PRECIOUS
        assert isinstance(outcome, exceptions.DiskPathOccupiedError), outcome
        assert sorted(os.listdir(tmp_path)) == [target.name]

    def test_a_new_image_is_made_where_nothing_was(self, tmp_path: Path):
        target = tmp_path / "not" / "yet" / "vm01_data.qcow2"

        assert _dm().create_disk(str(target), 16) is True

        info = json.loads(subprocess.run(
            ["qemu-img", "info", "--output=json", str(target)],
            capture_output=True, text=True, check=True).stdout)
        assert info["format"] == "qcow2"
        assert info["virtual-size"] == 16 * 1024 * 1024
        assert os.listdir(target.parent) == [target.name]
        assert target.stat().st_nlink == 1
        umask = os.umask(0)
        os.umask(umask)
        # the mode `qemu-img create` gives a file it creates
        assert stat.S_IMODE(target.stat().st_mode) == 0o644 & ~umask


class TestTheLookupFailsClosed:
    """Only "no such file" is absence; anything else is not."""

    @pytest.mark.skipif(os.geteuid() == 0, reason="root searches anything")
    def test_a_directory_that_cannot_be_searched(self, stubs: Stubs,
                                                 tmp_path: Path):
        wd = tmp_path / "wd"
        wd.mkdir()
        wd.chmod(0o600)
        try:
            outcome = _outcome(
                lambda: _dm().create_disk(str(wd / "vm01_data.qcow2"), 16))
        finally:
            wd.chmod(0o755)

        assert isinstance(outcome, ProvisionError), outcome
        assert not isinstance(outcome, exceptions.DiskPathOccupiedError)
        assert "cannot be told" in str(outcome)
        assert isinstance(outcome.__cause__, PermissionError)
        assert os.listdir(wd) == []
        assert stubs.calls() == []

    def test_any_other_lookup_error(self, stubs: Stubs, tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch):
        target = tmp_path / "wd" / "vm01_data.qcow2"
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if os.fspath(path) == str(target):
                raise OSError(errno.EIO, os.strerror(errno.EIO), str(target))
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", lstat)
        outcome = _outcome(lambda: _dm().create_disk(str(target), 16))

        assert isinstance(outcome, ProvisionError), outcome
        assert not isinstance(outcome, exceptions.DiskPathOccupiedError)
        assert outcome.__cause__.errno == errno.EIO
        assert os.listdir(target.parent) == []
        assert stubs.calls() == []


class TestAnEntryThatAppearsMeanwhile:
    """The lookup is not the guarantee: the link is, in the same step that
    creates the name."""

    @pytest.mark.parametrize("kind", ["file", "dangling", "directory"])
    def test_it_is_refused_and_left_as_it_was(self, stubs: Stubs,
                                              tmp_path: Path, kind: str):
        target = tmp_path / "wd" / "vm01_data.qcow2"
        nowhere = tmp_path / "nowhere.qcow2"
        stubs.race(target, kind,
                   "RACED" if kind == "file" else str(nowhere))

        outcome = _outcome(lambda: _dm().create_disk(str(target), 16))

        assert isinstance(outcome, exceptions.DiskPathOccupiedError), outcome
        assert isinstance(outcome.__cause__, FileExistsError)
        assert "appeared while it was being made" in str(outcome)
        if kind == "file":
            assert target.read_bytes() == b"RACED"
        elif kind == "dangling":
            assert os.readlink(target) == str(nowhere)
            assert not os.path.lexists(nowhere)
        else:
            assert os.listdir(target) == []
        # and the image made meanwhile is gone
        assert os.listdir(target.parent) == [target.name]
        assert len(stubs.calls()) == 1


class TestNothingIsLeftBehind:

    def test_when_qemu_img_fails(self, stubs: Stubs, tmp_path: Path):
        stubs.fail_qemu_img()
        wd = tmp_path / "wd"

        assert _dm().create_disk(str(wd / "vm01_data.qcow2"), 16) is False

        assert os.listdir(wd) == []
        assert len(stubs.calls()) == 1

    def test_when_the_link_fails_for_another_reason(
            self, stubs: Stubs, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch):
        def link(*args, **kwargs):
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

        monkeypatch.setattr(os, "link", link)
        target = tmp_path / "wd" / "vm01_data.qcow2"

        outcome = _outcome(lambda: _dm().create_disk(str(target), 16))

        assert isinstance(outcome, ProvisionError), outcome
        assert not isinstance(outcome, exceptions.DiskPathOccupiedError)
        assert isinstance(outcome.__cause__, PermissionError)
        assert os.listdir(target.parent) == []

    def test_when_it_works_only_the_disk_is_left(self, stubs: Stubs,
                                                 tmp_path: Path):
        target = tmp_path / "wd" / "vm01_data.qcow2"

        assert _dm().create_disk(str(target), 16) is True

        assert os.listdir(target.parent) == [target.name]
        assert target.read_bytes() == b"QFIqcow2:16M"
        assert target.stat().st_nlink == 1
        umask = os.umask(0)
        os.umask(umask)
        assert stat.S_IMODE(target.stat().st_mode) == 0o644 & ~umask
        (argv,) = stubs.calls()
        assert argv[:3] == ["create", "-f", "qcow2"] and argv[4] == "16M"
        # made under another name in the same directory, then linked
        assert Path(argv[3]).parent == target.parent
        assert argv[3] != str(target)

    def test_a_private_name_taken_beforehand_is_never_followed(
            self, stubs: Stubs, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "boxman.providers.libvirt.disk.secrets.token_hex",
            lambda n: "feedface")
        target = tmp_path / "wd" / "vm01_data.qcow2"
        target.parent.mkdir()
        planted = target.parent / (
            f".boxman-new.{os.getpid()}.feedface.{target.name}")
        precious = tmp_path / "precious.qcow2"
        precious.write_bytes(PRECIOUS)
        planted.symlink_to(precious)

        outcome = _outcome(lambda: _dm().create_disk(str(target), 16))

        assert isinstance(outcome, ProvisionError), outcome
        assert precious.read_bytes() == PRECIOUS
        # and what it did not make, it does not remove
        assert os.readlink(planted) == str(precious)
        assert sorted(os.listdir(target.parent)) == [planted.name]
        assert stubs.calls() == []

    def test_a_private_file_that_cannot_be_removed_is_named(
            self, stubs: Stubs, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch, captured_logs):
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if ".boxman-new." in os.fspath(path):
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", unlink)
        target = tmp_path / "wd" / "vm01_data.qcow2"

        assert _dm().create_disk(str(target), 16) is True

        (argv,) = stubs.calls()
        warnings = [r.getMessage() for r in captured_logs.records
                    if r.levelno == logging.WARNING]
        assert any(argv[3] in w and "remove it by hand" in w
                   for w in warnings), warnings


class TestEveryArgumentIsQuoted:

    def test_a_hostile_path(self, stubs: Stubs, tmp_path: Path):
        wd = tmp_path / f"it's {LIVE_PAYLOAD}"
        target = wd / f"vm01 {LIVE_PAYLOAD}.qcow2"

        assert _dm().create_disk(str(target), 16) is True

        (argv,) = stubs.calls()
        assert Path(argv[3]).parent == wd
        assert target.read_bytes() == b"QFIqcow2:16M"
        assert not list(tmp_path.rglob("pwned"))

    def test_a_hostile_format(self, stubs: Stubs, tmp_path: Path):
        fmt = f"qcow2 {LIVE_PAYLOAD}"
        target = tmp_path / "wd" / "vm01_data.qcow2"

        assert _dm().create_disk(str(target), 16, format=fmt) is True

        (argv,) = stubs.calls()
        assert argv[:3] == ["create", "-f", fmt]
        assert not list(tmp_path.rglob("pwned"))

    def test_through_the_docker_compose_runtime(self, stubs: Stubs,
                                                tmp_path: Path):
        stubs.add("docker", _DOCKER)
        target = tmp_path / f"it's {LIVE_PAYLOAD}" / "vm01_data.qcow2"
        dm = _dm(runtime="docker-compose", runtime_container="boxman-rt")

        assert dm.create_disk(str(target), 16) is True

        assert len(stubs.calls("docker")) == 1
        (argv,) = stubs.calls()
        assert Path(argv[3]).parent == target.parent
        assert os.listdir(target.parent) == [target.name]
        assert not list(tmp_path.rglob("pwned"))

    def test_resize(self, stubs: Stubs, tmp_path: Path):
        target = tmp_path / f"it's {LIVE_PAYLOAD}.qcow2"
        target.write_bytes(b"image")

        assert _dm().resize_disk_offline(str(target), 2048) is True

        assert stubs.calls() == [["resize", str(target), "2048M"]]
        assert not list(tmp_path.rglob("pwned"))


class TestTheCallersFail:
    """A refusal fails the step that creates the disk, never passes as a
    warning. The manager turns that into a ProvisionError (exit 2):
    tests/test_failure_exit_codes.py (a failed disk step) and
    tests/test_disk_ownership.py (a failed update of the disks)."""

    def _kept(self, tmp_path: Path) -> tuple[Path, Path]:
        wd = tmp_path / "wd"
        wd.mkdir()
        target = wd / "vm01_data.qcow2"
        target.write_bytes(PRECIOUS)
        return wd, target

    def test_configuring_a_declared_disk(self, stubs: Stubs, tmp_path: Path,
                                         captured_logs):
        wd, target = self._kept(tmp_path)
        dm = _dm()

        with patch.object(dm, "attach_disk") as attach, _no_record():
            assert dm.configure_from_disk_config(
                {"name": "data", "target": "vdb", "size": 16},
                str(wd), "vm01") is False

        attach.assert_not_called()
        assert target.read_bytes() == PRECIOUS
        errors = [r.getMessage() for r in captured_logs.records
                  if r.levelno == logging.ERROR]
        assert any(e.startswith("VM vm01: refusing") and str(target) in e
                   for e in errors), errors

    def test_provision_configuring_the_disks(self, stubs: Stubs,
                                             tmp_path: Path):
        wd, target = self._kept(tmp_path)
        session = LibVirtSession(
            config={"provider": {"libvirt": {"use_sudo": False}}})

        with patch.object(DiskManager, "attach_disk") as attach, _no_record():
            assert session.configure_vm_disks(
                vm_name="vm01",
                disks=[{"name": "data", "target": "vdb", "size": 16}],
                workdir=str(wd), disk_prefix="vm01") is False

        attach.assert_not_called()
        assert target.read_bytes() == PRECIOUS

    def test_update_adding_a_disk(self, stubs: Stubs, tmp_path: Path):
        wd, target = self._kept(tmp_path)
        session = LibVirtSession(
            config={"provider": {"libvirt": {"use_sudo": False}}})

        with patch.object(DiskManager, "attach_disk") as attach, _no_record():
            assert session.update_vm_disks(
                vm_name="vm01",
                new_disks=[{"name": "data", "target": "vdb", "size": 16}],
                resize_disks=[], workdir=str(wd), disk_prefix="vm01",
                vm_running=True) is False

        attach.assert_not_called()
        assert target.read_bytes() == PRECIOUS


class TestADirectInstallBootDisk:
    """PXE and ISO VMs made their empty boot disk with the same
    ``qemu-img create`` over whatever was at ``<workdir>/<vm>.qcow2``."""

    @pytest.fixture(autouse=True)
    def _forget_the_images_made(self):
        yield
        for fd in direct_vm._boot_disks_made.values():
            os.close(fd)
        direct_vm._boot_disks_made.clear()

    @staticmethod
    def _vm(workdir) -> BareVM:
        return BareVM(vm_name="pxe01",
                      info={"disk_size": "1G",
                            "networks": [{"name": "default"}]},
                      provider_config={"use_sudo": False,
                                       "uri": "qemu:///system"},
                      workdir=str(workdir))

    def test_an_existing_boot_disk_is_refused(self, stubs: Stubs,
                                              tmp_path: Path):
        disk = tmp_path / "wd" / "pxe01.qcow2"
        disk.parent.mkdir()
        disk.write_bytes(PRECIOUS)

        outcome = _outcome(self._vm(disk.parent).create)

        assert isinstance(outcome, exceptions.DiskPathOccupiedError), outcome
        assert "remove or rename" in str(outcome)
        assert "attach_only" not in str(outcome)
        assert disk.read_bytes() == PRECIOUS
        assert stubs.calls() == [] and stubs.calls("virt-install") == []

    def test_a_retry_uses_the_image_its_earlier_attempt_made(
            self, stubs: Stubs, tmp_path: Path):
        wd = tmp_path / "wd"
        stubs.fail_virt_install(times=1)

        assert self._vm(wd).create() is False
        assert self._vm(wd).create() is True

        assert len(stubs.calls()) == 1
        installs = stubs.calls("virt-install")
        assert len(installs) == 2
        disk = str(wd / "pxe01.qcow2")
        assert all(any(arg.startswith(f"--disk=path={disk},") for arg in call)
                   for call in installs)
        assert os.listdir(wd) == ["pxe01.qcow2"]

    def test_an_image_replaced_since_is_refused(self, stubs: Stubs,
                                                tmp_path: Path):
        wd = tmp_path / "wd"
        stubs.fail_virt_install(times=1)
        assert self._vm(wd).create() is False
        disk = wd / "pxe01.qcow2"
        disk.unlink()
        disk.write_bytes(PRECIOUS)

        outcome = _outcome(self._vm(wd).create)

        assert isinstance(outcome, exceptions.DiskPathOccupiedError), outcome
        assert disk.read_bytes() == PRECIOUS
        assert len(stubs.calls("virt-install")) == 1

    def test_only_the_image_made_is_taken_for_its_own(
            self, stubs: Stubs, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch):
        real = direct_vm.create_image_exclusive

        def made_another(*args, **kwargs):
            real(*args, **kwargs)
            return (0, 0)       # not the image that is at the path now

        wd = tmp_path / "wd"
        stubs.fail_virt_install(times=1)
        monkeypatch.setattr(direct_vm, "create_image_exclusive", made_another)
        assert self._vm(wd).create() is False
        monkeypatch.setattr(direct_vm, "create_image_exclusive", real)

        outcome = _outcome(self._vm(wd).create)

        assert isinstance(outcome, exceptions.DiskPathOccupiedError), outcome
        assert len(stubs.calls("virt-install")) == 1

    def test_a_pin_on_an_image_that_is_gone_is_released(self, stubs: Stubs,
                                                        tmp_path: Path):
        wd = tmp_path / "wd"
        stubs.fail_virt_install(times=1)
        assert self._vm(wd).create() is False
        disk = wd / "pxe01.qcow2"
        first = disk.stat()
        disk.unlink()

        assert self._vm(wd).create() is True

        second = disk.stat()
        held = set()
        for fd in os.listdir("/proc/self/fd"):
            try:
                st = os.stat(f"/proc/self/fd/{fd}")
            except OSError:
                continue
            held.add((st.st_dev, st.st_ino))
        assert (second.st_dev, second.st_ino) in held
        assert (first.st_dev, first.st_ino) not in held

    def test_a_hostile_disk_size_is_one_argument(self, stubs: Stubs,
                                                 tmp_path: Path):
        vm = self._vm(tmp_path / "wd")
        vm.info["disk_size"] = f"1G {LIVE_PAYLOAD}"

        assert vm.create() is True

        (argv,) = stubs.calls()
        assert argv[4] == f"1G {LIVE_PAYLOAD}"
        assert not list(tmp_path.rglob("pwned"))

    def test_the_image_and_virt_install_name_one_absolute_path(
            self, stubs: Stubs, tmp_path: Path):
        assert self._vm("rel/wd").create() is True

        disk = tmp_path / "rel" / "wd" / "pxe01.qcow2"
        assert disk.read_bytes() == b"QFIqcow2:1G"
        (install,) = stubs.calls("virt-install")
        assert (f"--disk=path={disk},format=qcow2,driver.type=qcow2,"
                f"bus=virtio,discard=unmap") in install


class TestARefusedCloneIsNotRetried:

    def test_it_fails_on_the_first_attempt(self,
                                           monkeypatch: pytest.MonkeyPatch):
        from boxman.manager_parts import vms

        sleeps: list = []
        monkeypatch.setattr(vms.time, "sleep", sleeps.append)
        provider = MagicMock()
        provider.clone_vm.side_effect = exceptions.DiskPathOccupiedError(
            "refusing to create the disk image /w/pxe01.qcow2")

        with pytest.raises(exceptions.DiskPathOccupiedError):
            vms._clone_with_retry(provider,
                                  {"workdir": "/w", "base_image": "tpl"},
                                  {"boot_order": ["network"]}, "pxe01")

        assert provider.clone_vm.call_count == 1
        assert sleeps == []


# ---------------------------------------------------------------------------
# `update` finds an image already at a new disk's path. It attaches it only
# when boxman's ownership record says boxman created that very file for this
# VM -- a run stopped between creating and attaching it -- and refuses it
# otherwise, as provision does; `attach_only: true` stays the explicit way
# to adopt a file (#215). The differ used to adopt any file it found there.
# ---------------------------------------------------------------------------

#: the extended attribute the image carries the token of its record in
MARK = "user.boxman.disk"

DATA = {"name": "data", "target": "vdb", "size": 16}


def _result(stdout: str = "", ok: bool = True, stderr: str = "") -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout, r.stderr, r.ok, r.failed = stdout, stderr, ok, not ok
    r.return_code = 0 if ok else 1
    return r


class FakeLibvirt:
    """What libvirt keeps for the domains that the disk path touches: each
    domain's persistent boxman metadata and the files attached to it, and
    every ``virsh`` call in order. ``metadata`` and ``attach-device`` are
    all creating and attaching a disk runs."""

    def __init__(self):
        self.metadata: dict[str, str] = {}
        self.attached: dict[str, list[str]] = {}
        self.calls: list[str] = []
        self.attach_failures = 0
        self.unreadable = False
        self.unwritable = False

    def execute(self, cmd, *args, **kwargs):
        domain = args[0]
        if cmd == "metadata" and "set" in kwargs:
            self.calls.append(f"record {domain}")
            if self.unwritable:
                return _result(ok=False, stderr="error: metadata write failed")
            self.metadata[domain] = kwargs["set"]
            return _result()
        if cmd == "metadata":
            self.calls.append(f"read {domain}")
            if self.unreadable:
                return _result(ok=False, stderr="error: failed to connect")
            if domain not in self.metadata:
                return _result(ok=False, stderr=(
                    "error: metadata not found: Requested metadata element "
                    "is not present"))
            return _result(stdout=self.metadata[domain])
        if cmd == "attach-device":
            self.calls.append(f"attach {domain}")
            if self.attach_failures:
                self.attach_failures -= 1
                return _result(ok=False, stderr="error: stub attach failure")
            xml = Path(args[1]).read_text()
            source = re.search(r"<source file='([^']*)'/>", xml).group(1)
            self.attached.setdefault(domain, []).append(source)
            return _result()
        raise AssertionError(f"unexpected virsh call: {cmd} {args} {kwargs}")

    def records(self, domain: str):
        return read_disk_records(self, domain)

    def record(self, domain: str, **attributes) -> None:
        """Put one record on *domain*, as its metadata XML."""
        attributes = {"name": "data", "target": "vdb", "role": "data",
                      **attributes}
        root = ET.Element("disks")
        ET.SubElement(root, "disk", attributes)
        self.metadata[domain] = ET.tostring(root, encoding="unicode")


@pytest.fixture
def libvirt() -> FakeLibvirt:
    return FakeLibvirt()


@pytest.fixture
def marks(tmp_path: Path) -> None:
    """Skip where the filesystem under the test takes no user xattrs."""
    probe = tmp_path / "xattr-probe"
    probe.write_bytes(b"")
    try:
        os.setxattr(probe, MARK, b"probe")
    except OSError as exc:
        pytest.skip(f"no user extended attributes here ({exc.strerror})")
    finally:
        probe.unlink()


def _mark(path: Path, token: str) -> None:
    os.setxattr(path, MARK, token.encode(), follow_symlinks=False)


def _differ(libvirt: FakeLibvirt, wd: Path, disks, attached=()):
    """The real differ, with every probe but the records stubbed."""
    differ = VMStateDiffer(provider_config={"use_sudo": False,
                                            "uri": "qemu:///system"})
    differ.virsh = libvirt
    with patch.object(differ, "get_vm_state", return_value="running"), \
         patch.object(differ, "get_actual_cpu",
                      return_value={"sockets": 1, "cores": 1, "threads": 1,
                                    "total_vcpus": 1, "current_vcpus": 1}), \
         patch.object(differ, "get_max_vcpus", return_value=1), \
         patch.object(differ, "get_actual_memory_mb", return_value=1024), \
         patch.object(differ, "get_max_memory_mb", return_value=1024), \
         patch.object(differ, "get_actual_disks",
                      return_value=list(attached)), \
         patch.object(differ, "get_actual_memballoon",
                      return_value={"free_page_reporting": False,
                                    "autodeflate": False,
                                    "stats_period": None}), \
         patch.object(differ, "get_actual_cdroms", return_value=[]), \
         patch.object(differ, "get_actual_shared_folders", return_value=[]):
        return differ.diff_vm(
            domain_name="vm01", desired_cpus=None, desired_memory_mb=None,
            desired_disks=[dict(d) for d in disks], workdir=str(wd),
            disk_prefix="vm01")


def _session() -> LibVirtSession:
    return LibVirtSession(
        config={"provider": {"libvirt": {"use_sudo": False}}})


def _update(libvirt: FakeLibvirt, wd: Path, disks=(DATA,)) -> bool:
    """`update` adding *disks* to vm01: the real differ decides what is new
    (nothing is attached), the real session adds it."""
    diff = _differ(libvirt, wd, disks)
    with patch("boxman.providers.libvirt.disk.VirshCommand",
               return_value=libvirt):
        return _session().update_vm_disks(
            vm_name="vm01", new_disks=diff["new_disks"],
            resize_disks=diff["resize_disks"], workdir=str(wd),
            disk_prefix="vm01", vm_running=True)


def _provision(libvirt: FakeLibvirt, wd: Path, disks=(DATA,)) -> bool:
    """provision configuring vm01's disks (each in a child process, so
    what it does to *libvirt* is not seen here)."""
    with patch("boxman.providers.libvirt.disk.VirshCommand",
               return_value=libvirt):
        return _session().configure_vm_disks(
            vm_name="vm01", disks=[dict(d) for d in disks],
            workdir=str(wd), disk_prefix="vm01")


def _errors(captured_logs) -> list[str]:
    return [r.getMessage() for r in captured_logs.records
            if r.levelno == logging.ERROR]


class TestUpdateAttachesOnlyItsOwnImage:

    def _kept(self, tmp_path: Path) -> tuple[Path, Path]:
        wd = tmp_path / "wd"
        wd.mkdir()
        image = wd / "vm01_data.qcow2"
        image.write_bytes(PRECIOUS)
        return wd, image

    def _interrupted(self, libvirt: FakeLibvirt, stubs: Stubs,
                     tmp_path: Path) -> tuple[Path, Path]:
        """An `update` that created vm01's data disk and stopped before it
        was attached."""
        wd = tmp_path / "wd"
        libvirt.attach_failures = 1
        assert _update(libvirt, wd) is False
        image = wd / "vm01_data.qcow2"
        assert image.is_file() and len(stubs.calls()) == 1
        return wd, image

    def test_an_image_with_no_record_is_refused(self, stubs: Stubs,
                                                tmp_path: Path,
                                                libvirt: FakeLibvirt,
                                                captured_logs):
        wd, image = self._kept(tmp_path)

        assert _update(libvirt, wd) is False

        assert image.read_bytes() == PRECIOUS
        assert libvirt.attached == {} and stubs.calls() == []
        assert any(str(image) in e and "attach_only" in e
                   for e in _errors(captured_logs)), _errors(captured_logs)

    def test_a_refused_provision_is_refused_again_by_update(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt):
        wd, image = self._kept(tmp_path)

        assert _provision(libvirt, wd) is False
        assert _update(libvirt, wd) is False

        assert image.read_bytes() == PRECIOUS
        assert libvirt.attached == {} and stubs.calls() == []

    def test_the_own_image_of_an_interrupted_run_is_attached(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt, marks):
        wd, image = self._interrupted(libvirt, stubs, tmp_path)
        made = image.read_bytes()
        # recorded before the attach was tried, not after it succeeded
        assert libvirt.calls.index("record vm01") < \
            libvirt.calls.index("attach vm01")

        assert _update(libvirt, wd) is True

        assert libvirt.attached == {"vm01": [str(image)]}
        assert len(stubs.calls()) == 1          # attached, not made again
        assert image.read_bytes() == made
        (record,) = libvirt.records("vm01")
        # still boxman's own: a teardown removes it with the VM
        assert (record.name, record.target, record.role, record.source) == \
            ("data", "vdb", "data", str(image))

    def test_an_own_image_replaced_since_is_refused(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt, marks):
        wd, image = self._interrupted(libvirt, stubs, tmp_path)
        image.unlink()
        image.write_bytes(PRECIOUS)     # may even reuse the inode number

        assert _update(libvirt, wd) is False

        assert image.read_bytes() == PRECIOUS
        assert libvirt.attached == {}

    def test_a_record_of_another_vm_does_not_vouch(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt, marks):
        wd, image = self._kept(tmp_path)
        _mark(image, "t0k3n")
        libvirt.record("vm02", source=str(image), token="t0k3n")

        assert _update(libvirt, wd) is False

        assert image.read_bytes() == PRECIOUS
        assert libvirt.attached == {}

    @pytest.mark.parametrize("mismatch", [
        pytest.param({"source": "/elsewhere/vm01_data.qcow2"}, id="path"),
        pytest.param({"role": "adopted"}, id="role"),
        pytest.param({"name": "logs"}, id="name"),
        pytest.param({"target": "vdc"}, id="target"),
        pytest.param({"token": "an0th3r"}, id="token"),
        pytest.param({"token": None}, id="no-token"),
    ])
    def test_a_record_that_is_not_for_this_file_does_not_vouch(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt, marks,
            mismatch):
        wd, image = self._kept(tmp_path)
        _mark(image, "t0k3n")
        attributes = {"source": str(image), "token": "t0k3n", **mismatch}
        libvirt.record("vm01", **{k: v for k, v in attributes.items()
                                  if v is not None})

        assert _update(libvirt, wd) is False

        assert image.read_bytes() == PRECIOUS
        assert libvirt.attached == {}

    def test_an_image_that_lost_its_mark_does_not_vouch(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt, marks,
            captured_logs):
        wd, image = self._kept(tmp_path)
        libvirt.record("vm01", source=str(image), token="t0k3n")

        assert _update(libvirt, wd) is False
        assert libvirt.attached == {}
        # refused as an occupied path, not failed on the missing mark
        assert any(e.startswith("VM vm01: refusing") and str(image) in e
                   for e in _errors(captured_logs)), _errors(captured_logs)

    def test_a_record_without_a_token_is_no_match_for_an_empty_mark(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt, marks):
        wd, image = self._kept(tmp_path)
        os.setxattr(image, MARK, b"")
        libvirt.record("vm01", source=str(image))

        assert _update(libvirt, wd) is False
        assert libvirt.attached == {}

    def test_a_symlink_swapped_in_after_the_lookup_is_not_followed(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt, marks,
            monkeypatch: pytest.MonkeyPatch):
        wd = tmp_path / "wd"
        wd.mkdir()
        own = tmp_path / "own.qcow2"
        own.write_bytes(PRECIOUS)
        _mark(own, "t0k3n")
        entry = wd / "vm01_data.qcow2"
        entry.symlink_to(own)
        libvirt.record("vm01", source=str(entry), token="t0k3n")
        # the lookup saw the plain file that was there a moment before
        real_lstat, plain = os.lstat, os.lstat(own)

        def lstat(path, *args, **kwargs):
            if os.fspath(path) == str(entry):
                return plain
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", lstat)

        assert _update(libvirt, wd) is False
        assert libvirt.attached == {}
        assert own.read_bytes() == PRECIOUS

    @pytest.mark.parametrize("kind", ["symlink", "directory"])
    def test_what_is_not_a_plain_file_is_refused_even_if_recorded(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt, marks,
            kind):
        wd = tmp_path / "wd"
        wd.mkdir()
        entry = wd / "vm01_data.qcow2"
        if kind == "symlink":
            target = tmp_path / "elsewhere.qcow2"
            target.write_bytes(PRECIOUS)
            _mark(target, "t0k3n")
            entry.symlink_to(target)
        else:
            entry.mkdir()
            _mark(entry, "t0k3n")
        libvirt.record("vm01", source=str(entry), token="t0k3n")
        before = _tree(tmp_path)

        assert _update(libvirt, wd) is False

        assert _tree(tmp_path) == before
        assert libvirt.attached == {}

    def test_attach_only_still_adopts_the_file(self, stubs: Stubs,
                                               tmp_path: Path,
                                               libvirt: FakeLibvirt):
        wd, image = self._kept(tmp_path)

        assert _update(libvirt, wd, [dict(DATA, attach_only=True)]) is True

        assert libvirt.attached == {"vm01": [str(image)]}
        assert image.read_bytes() == PRECIOUS and stubs.calls() == []
        (record,) = libvirt.records("vm01")
        assert record.role == "adopted"

    def test_a_new_disk_is_recorded_before_it_is_attached(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt, marks):
        wd = tmp_path / "wd"

        assert _update(libvirt, wd) is True

        image = wd / "vm01_data.qcow2"
        assert libvirt.calls.index("record vm01") < \
            libvirt.calls.index("attach vm01")
        (record,) = libvirt.records("vm01")
        assert (record.role, record.source) == ("data", str(image))
        token = re.search(r'token="([^"]+)"', libvirt.metadata["vm01"])
        assert token and os.getxattr(image, MARK) == token.group(1).encode()


class TestTheOwnImageCheck:
    """DiskManager's side of it, where the domain's records are read."""

    def test_records_that_cannot_be_read_do_not_vouch(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt, marks,
            captured_logs):
        wd = tmp_path / "wd"
        wd.mkdir()
        image = wd / "vm01_data.qcow2"
        image.write_bytes(PRECIOUS)
        _mark(image, "t0k3n")
        libvirt.record("vm01", source=str(image), token="t0k3n")
        libvirt.unreadable = True
        dm = _dm()
        dm.virsh = libvirt

        assert dm.configure_from_disk_config(dict(DATA), str(wd),
                                             "vm01") is False

        assert image.read_bytes() == PRECIOUS
        assert libvirt.attached == {}
        # refused as an occupied path, naming it and what to do
        assert any(e.startswith("VM vm01: refusing") and str(image) in e
                   for e in _errors(captured_logs)), _errors(captured_logs)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root searches anything")
    def test_a_path_that_cannot_be_looked_up_is_refused(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt,
            captured_logs):
        wd = tmp_path / "wd"
        wd.mkdir()
        libvirt.record("vm01", source=str(wd / "vm01_data.qcow2"),
                       token="t0k3n")
        dm = _dm()
        dm.virsh = libvirt
        wd.chmod(0o600)
        try:
            assert dm.configure_from_disk_config(dict(DATA), str(wd),
                                                 "vm01") is False
        finally:
            wd.chmod(0o755)

        assert libvirt.attached == {} and os.listdir(wd) == []
        assert any("cannot be told" in e for e in _errors(captured_logs))

    def test_a_record_that_cannot_be_written_does_not_stop_the_attach(
            self, stubs: Stubs, tmp_path: Path, libvirt: FakeLibvirt,
            captured_logs):
        wd = tmp_path / "wd"
        libvirt.unwritable = True
        dm = _dm()
        dm.virsh = libvirt

        assert dm.configure_from_disk_config(dict(DATA), str(wd),
                                             "vm01") is True

        assert libvirt.attached == {"vm01": [str(wd / "vm01_data.qcow2")]}
        warnings = [r.getMessage() for r in captured_logs.records
                    if r.levelno == logging.WARNING]
        assert any("could not record" in w for w in warnings), warnings


# a `virsh` that keeps each domain's metadata in a file, attaches anything,
# and takes a moment to answer a read, as a loaded host does
_VIRSH = r'''#!/usr/bin/env python3
import fcntl, json, os, sys, time
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
store = os.path.join(here, "virsh-metadata.json")
args = sys.argv[1:]
if args[:1] == ["-c"]:
    args = args[2:]
cmd, domain = args[0], args[1]
if cmd == "attach-device":
    sys.exit(0)
if cmd != "metadata":
    sys.exit(3)
with open(store + ".lock", "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    data = {}
    if os.path.exists(store):
        with open(store) as fh:
            data = json.load(fh)
    sets = [a[len("--set="):] for a in args if a.startswith("--set=")]
    if sets:
        data[domain] = sets[0]
        with open(store, "w") as fh:
            json.dump(data, fh)
        sys.exit(0)
time.sleep(0.5)
if domain not in data:
    sys.stderr.write("error: metadata not found: Requested metadata "
                     "element is not present" + chr(10))
    sys.exit(1)
print(data[domain])
'''


class TestTheDisksOfOneVmKeepEveryRecord:
    """provision configures a VM's disks in parallel processes, and each one
    records its disk by reading the domain's records and writing them back
    with its own added. Two at once kept one of the two -- and a disk
    without its record is refused by the next run instead of attached, and
    kept by a teardown instead of removed (#215)."""

    def test_two_disks_configured_together(self, stubs: Stubs,
                                           tmp_path: Path, marks):
        stubs.add("virsh", _VIRSH)
        wd = tmp_path / "wd"
        disks = [dict(DATA), {"name": "logs", "target": "vdc", "size": 16}]

        assert _session().configure_vm_disks(
            vm_name="vm01", disks=disks, workdir=str(wd),
            disk_prefix="vm01") is True

        store = json.loads((stubs.root / "virsh-metadata.json").read_text())
        records = {r.name: r for r in records_from_xml(store["vm01"])}
        assert sorted(records) == ["data", "logs"]
        for name, record in records.items():
            image = wd / f"vm01_{name}.qcow2"
            assert os.getxattr(image, MARK) == record.token.encode()


class TestTheTeardownInventoryKeepsItsLayout:
    """The creation token rides on the domain's record, not in a teardown's
    saved inventory, which the teardown decides with: an inventory saved by
    this version reads the same as one saved before it, either way."""

    def test_a_record_is_saved_and_read_without_its_token(self,
                                                          tmp_path: Path):
        from boxman.providers.libvirt import disk_cleanup
        from boxman.providers.libvirt.disk_ownership import DiskRecord

        source = str(tmp_path / "vm01_data.qcow2")
        path = disk_cleanup.save_teardown_inventory(
            disk_cleanup.StorageInventory(
                vm_name="vm01", disk_sources=[], media_sources=[],
                records=[DiskRecord("data", "vdb", "data", source,
                                    token="t0k3n")],
                records_state="present", chains={}, boot_family=[],
                legacy_disks=None),
            str(tmp_path))

        saved = json.loads(Path(path).read_text())
        assert saved["records"] == [{"name": "data", "target": "vdb",
                                     "role": "data", "source": source}]
        loaded = disk_cleanup.load_teardown_inventory(path, "vm01")
        assert loaded.records == [DiskRecord("data", "vdb", "data", source)]
