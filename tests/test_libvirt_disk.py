"""
Unit tests for boxman.providers.libvirt.disk.DiskManager.

Covers:
- the attach_only flag from issue #85 item 23: when the image file
  already exists (leftover from a failed earlier run),
  ``configure_from_disk_config`` must skip ``qemu-img create`` and only
  attach the existing file.
- the issue #85 item 38 fixes: the ``if not result.ok`` failure
  branches must be reachable — commands run with ``warn=True`` and a
  failed command returns False instead of raising RuntimeError.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.disk import DiskManager

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


class TestDeadErrorBranches:
    """Issue #85 item 38: each method must run its command with
    warn=True so the not-ok branch is live, and return False on
    failure instead of raising RuntimeError."""

    def test_create_disk_failure_returns_false(self, dm: DiskManager,
                                               tmp_path: Path):
        with patch("boxman.providers.libvirt.disk.LibVirtCommandBase"
                   ) as cmd_cls:
            cmd_cls.return_value.execute_shell.return_value = _result(
                ok=False, stderr="x")
            assert dm.create_disk(str(tmp_path / "d.qcow2"), 1024) is False
        assert cmd_cls.return_value.execute_shell.call_args.kwargs.get(
            "warn") is True

    def test_attach_disk_failure_returns_false(self, dm: DiskManager):
        with patch.object(dm, "execute",
                          return_value=_result(ok=False, stderr="x")) as exe:
            assert dm.attach_disk("/tmp/d.qcow2", "vdb") is False
        assert exe.call_args.kwargs.get("warn") is True

    def test_resize_disk_offline_failure_returns_false(
            self, dm: DiskManager, tmp_path: Path):
        with patch("boxman.providers.libvirt.disk.LibVirtCommandBase"
                   ) as cmd_cls:
            cmd_cls.return_value.execute_shell.return_value = _result(
                ok=False, stderr="x")
            assert dm.resize_disk_offline(
                str(tmp_path / "d.qcow2"), 2048) is False
        assert cmd_cls.return_value.execute_shell.call_args.kwargs.get(
            "warn") is True

    def test_resize_disk_online_failure_returns_false(self, dm: DiskManager):
        with patch.object(dm, "execute",
                          return_value=_result(ok=False, stderr="x")) as exe:
            assert dm.resize_disk_online("vdb", 2048) is False
        assert exe.call_args.kwargs.get("warn") is True
