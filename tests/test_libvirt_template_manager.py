"""
Unit tests for boxman.providers.libvirt.template_manager.TemplateManager.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).

These tests cover the guarded methods (``_resolve_image_path``,
``template_exists``, early-exit branches of ``create_template``, and
``_wait_and_shutdown``). The full ``create_template`` orchestration is
still broken (see the module docstring): ``ci.build_seed_iso()`` is
invoked with no args but ``CloudInitTemplate.build_seed_iso`` requires two.
Fixing that is a Phase 2.7 follow-up.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.template_manager import TemplateManager


pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True, stderr: str = "", return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


@pytest.fixture
def tm() -> TemplateManager:
    return TemplateManager(provider_config={"use_sudo": False, "uri": "qemu:///system"})


class TestResolveImagePath:

    def test_file_scheme_is_stripped(self, tm: TemplateManager, tmp_path: Path):
        src = tmp_path / "base.img"
        src.write_bytes(b"x")
        dest = tmp_path / "dest"
        dest.mkdir()
        with patch("boxman.providers.libvirt.template_manager.run",
                   return_value=_result(ok=True)):
            out = tm._resolve_image_path(f"file://{src}", str(dest))
        # .img basename rewritten to .qcow2 per module contract
        assert out == str(dest / "base.qcow2")

    def test_raises_when_file_missing(self, tm: TemplateManager, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="base image not found"):
            tm._resolve_image_path(str(tmp_path / "missing.img"), str(tmp_path))

    def test_raises_when_uri_empty(self, tm: TemplateManager, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            tm._resolve_image_path("", str(tmp_path))

    def test_noop_when_src_equals_dest(self, tm: TemplateManager, tmp_path: Path):
        # .img → .qcow2 rewrite means src != dest even if dir same; use .qcow2 for idempotency
        src = tmp_path / "base.qcow2"
        src.write_bytes(b"x")
        with patch("boxman.providers.libvirt.template_manager.run") as run_fn:
            out = tm._resolve_image_path(str(src), str(tmp_path))
        assert out == str(src)
        run_fn.assert_not_called()   # same dir → no copy

    def test_rsync_failure_falls_back_to_shutil(
        self, tm: TemplateManager, tmp_path: Path
    ):
        src = tmp_path / "base.qcow2"
        src.write_bytes(b"x")
        dest_dir = tmp_path / "out"
        dest_dir.mkdir()
        with patch(
            "boxman.providers.libvirt.template_manager.run",
            return_value=_result(ok=False, stderr="no rsync"),
        ), patch(
            "boxman.providers.libvirt.template_manager.shutil.copy2"
        ) as shutil_copy:
            out = tm._resolve_image_path(str(src), str(dest_dir))
        shutil_copy.assert_called_once()
        assert out == str(dest_dir / "base.qcow2")


class TestTemplateExists:

    def test_returns_true_when_vm_in_list(self, tm: TemplateManager):
        with patch.object(tm.virsh, "execute",
                          return_value=_result(stdout="vm-a\ntemplate-x\nvm-b\n")):
            assert tm.template_exists("template-x") is True

    def test_returns_false_when_not_in_list(self, tm: TemplateManager):
        with patch.object(tm.virsh, "execute",
                          return_value=_result(stdout="vm-a\nvm-b\n")):
            assert tm.template_exists("template-x") is False

    def test_returns_false_when_execute_fails(self, tm: TemplateManager):
        with patch.object(tm.virsh, "execute", return_value=_result(ok=False)):
            assert tm.template_exists("anything") is False


class TestCreateTemplateEarlyExits:
    """Only cover early-exit guards here; full flow covered in integration tests."""

    def test_existing_template_without_force_returns_false(
        self, tm: TemplateManager, tmp_path: Path
    ):
        with patch.object(tm, "template_exists", return_value=True):
            ok = tm.create_template(
                "t", {"name": "existing"}, str(tmp_path), force=False,
            )
        assert ok is False

    def test_missing_image_returns_false(self, tm: TemplateManager, tmp_path: Path):
        with patch.object(tm, "template_exists", return_value=False):
            ok = tm.create_template(
                "t", {"name": "t", "image": ""}, str(tmp_path),
            )
        assert ok is False

    def test_missing_cloudinit_returns_false(self, tm: TemplateManager, tmp_path: Path):
        src = tmp_path / "base.qcow2"
        src.write_bytes(b"x")
        with patch.object(tm, "template_exists", return_value=False), \
             patch.object(tm, "_resolve_image_path", return_value=str(src)):
            ok = tm.create_template(
                "t",
                {"name": "t", "image": str(src), "cloudinit": ""},
                str(tmp_path),
            )
        assert ok is False

    def test_force_flag_triggers_destroy_and_undefine(
        self, tm: TemplateManager, tmp_path: Path
    ):
        calls = []

        def capture(*args, **_kwargs):
            calls.append(args)
            if args and args[0] == "list":
                return _result(stdout="already-here\n")
            return _result()

        with patch.object(tm.virsh, "execute", side_effect=capture), \
             patch.object(tm, "_resolve_image_path",
                          side_effect=FileNotFoundError("skip after destroy")):
            tm.create_template(
                "t",
                {"name": "already-here", "image": "/tmp/x.qcow2"},
                str(tmp_path),
                force=True,
            )

        op_names = [c[0] for c in calls if c]
        assert "destroy" in op_names
        assert "undefine" in op_names


class TestWaitAndShutdown:

    def test_returns_when_vm_shuts_off_by_itself(self, tm: TemplateManager):
        states = iter(["running\n", "shut off\n"])

        def fake(*_args, **_kwargs):
            return _result(stdout=next(states))

        with patch.object(tm.virsh, "execute", side_effect=fake) as execute, \
             patch("time.sleep"):
            tm._wait_and_shutdown("vm01", {"cloudinit_timeout": 20,
                                           "cloudinit_poll_interval": 10})
        # first poll: running, second: shut off → loop exits before timeout
        calls = [c.args[0] for c in execute.call_args_list]
        assert "domstate" in calls
        assert "shutdown" not in calls

    def test_timeout_then_shutdown_then_destroy_if_still_up(
        self, tm: TemplateManager
    ):
        """Loop times out, attempts shutdown, fails, force-destroys."""
        def fake(*args, **_kwargs):
            return _result(stdout="running\n")

        with patch.object(tm.virsh, "execute", side_effect=fake) as execute, \
             patch("time.sleep"):
            tm._wait_and_shutdown("vm01", {"cloudinit_timeout": 5,
                                           "cloudinit_poll_interval": 5})
        calls = [c.args[0] for c in execute.call_args_list]
        assert "shutdown" in calls
        assert "destroy" in calls  # force-destroy fallback after graceful shutdown fails
