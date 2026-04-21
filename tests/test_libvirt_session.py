"""
Unit tests for boxman.providers.libvirt.session.LibVirtSession.

Focus on the high-value surface:
  - Project-level config precedence (provider_config property)
  - uri / use_sudo property delegation + setters
  - update_provider_config_with_runtime
  - destroy_disks filesystem cleanup including snapshot leftovers
  - Simple delegators (destroy_vm, start_vm)

The huge orchestration methods (configure_vm_*, update_vm_*, verify_*,
save/restore, snapshot wiring) are covered by integration tests
(test_provision_boxes.py + Phase 1.5 E2E) not by unit tests.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.session import LibVirtSession


pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True, stderr: str = "", return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


def _session(provider: dict | None = None) -> LibVirtSession:
    cfg = {"provider": {"libvirt": provider or {}}}
    return LibVirtSession(config=cfg)


class TestConfigPrecedence:
    """Project-level config must always win over base config — this guards
    the explicit design in LibVirtSession.provider_config."""

    def test_defaults_on_empty_provider(self):
        s = _session({})
        assert s.provider_config == {}

    def test_reads_project_provider(self):
        s = _session({"uri": "qemu:///system", "use_sudo": True})
        assert s.provider_config["uri"] == "qemu:///system"
        assert s.provider_config["use_sudo"] is True

    def test_project_wins_over_base_via_update(self):
        s = _session({"use_sudo": True})   # project says True
        s.update_provider_config({"use_sudo": False})   # app tries to override
        # Project wins
        assert s.provider_config["use_sudo"] is True

    def test_base_fills_in_for_missing_project_keys(self):
        s = _session({"use_sudo": True})
        s.update_provider_config({"uri": "qemu:///custom"})
        assert s.provider_config["uri"] == "qemu:///custom"
        assert s.provider_config["use_sudo"] is True


class TestUriAndUseSudoProperties:

    def test_uri_default(self):
        assert _session({}).uri == "qemu:///system"

    def test_uri_getter_and_setter(self):
        s = _session({})
        s.uri = "qemu+ssh://host"
        assert s.uri == "qemu+ssh://host"
        assert s.provider_config["uri"] == "qemu+ssh://host"

    def test_use_sudo_default_false(self):
        assert _session({}).use_sudo is False

    def test_use_sudo_setter(self):
        s = _session({})
        s.use_sudo = True
        assert s.use_sudo is True

    def test_project_use_sudo_wins_over_setter(self):
        """If project said False, we cannot flip it to True via the setter."""
        s = _session({"use_sudo": False})
        s.use_sudo = True    # sets base only
        # Project still wins
        assert s.use_sudo is False


class TestUpdateProviderConfigWithRuntime:

    def test_noop_when_manager_is_none(self):
        s = _session({"uri": "qemu:///system"})
        s.update_provider_config_with_runtime()
        assert s.provider_config["uri"] == "qemu:///system"

    def test_delegates_to_manager_and_preserves_project_keys(self):
        s = _session({"use_sudo": True})
        manager = MagicMock()
        manager.get_provider_config_with_runtime.return_value = {
            "use_sudo": False, "runtime": "docker-compose",
        }
        s.manager = manager

        s.update_provider_config_with_runtime()
        # Runtime was applied
        assert s.provider_config["runtime"] == "docker-compose"
        # Project-level use_sudo still wins
        assert s.provider_config["use_sudo"] is True


class TestDestroyDisks:

    def test_removes_boot_disk_and_named_extras_and_snapshot_leftovers(
        self, tmp_path: Path
    ):
        # set up fake workdir
        (tmp_path / "vm01.qcow2").write_bytes(b"x")
        (tmp_path / "vm01_data.qcow2").write_bytes(b"x")
        (tmp_path / "vm01.2026-04-21T08:00:00").write_bytes(b"x")
        (tmp_path / "vm01_snapshot_s1.raw").write_bytes(b"x")
        (tmp_path / "other-vm.qcow2").write_bytes(b"x")   # untouched

        s = _session({"use_sudo": False})
        assert s.destroy_disks(
            str(tmp_path), "vm01", [{"name": "data"}],
        ) is True

        # vm01-prefixed files are gone
        assert not (tmp_path / "vm01.qcow2").exists()
        assert not (tmp_path / "vm01_data.qcow2").exists()
        assert not (tmp_path / "vm01.2026-04-21T08:00:00").exists()
        assert not (tmp_path / "vm01_snapshot_s1.raw").exists()
        # other-vm left alone
        assert (tmp_path / "other-vm.qcow2").exists()

    def test_missing_files_are_silently_ignored(self, tmp_path: Path):
        s = _session({})
        # nothing in tmp_path — should not raise
        assert s.destroy_disks(str(tmp_path), "no-vm", []) is True


class TestDestroyVMDelegation:

    def test_force_false_uses_remove(self):
        s = _session({})
        mock_destroyer = MagicMock()
        mock_destroyer.remove.return_value = True
        with patch("boxman.providers.libvirt.session.DestroyVM",
                   return_value=mock_destroyer):
            assert s.destroy_vm("vm01", force=False) is True
        mock_destroyer.remove.assert_called_once()
        mock_destroyer.force_undefine_vm.assert_not_called()

    def test_force_true_uses_force_undefine(self):
        s = _session({})
        mock_destroyer = MagicMock()
        mock_destroyer.force_undefine_vm.return_value = True
        with patch("boxman.providers.libvirt.session.DestroyVM",
                   return_value=mock_destroyer):
            assert s.destroy_vm("vm01", force=True) is True
        mock_destroyer.force_undefine_vm.assert_called_once()
        mock_destroyer.remove.assert_not_called()


class TestStartVM:

    def test_noop_when_already_running(self):
        s = _session({})
        mock_virsh = MagicMock()
        mock_virsh.execute.return_value = _result(stdout="running\n")
        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=mock_virsh):
            assert s.start_vm("vm01") is True
        # only the state probe was called, not the start
        first_call = mock_virsh.execute.call_args_list[0]
        assert first_call.args[0] == "domstate"

    def test_starts_when_shut_off_and_verifies(self):
        s = _session({})
        mock_virsh = MagicMock()
        mock_virsh.execute.side_effect = [
            _result(stdout="shut off\n"),   # first domstate
            _result(ok=True),               # start
            _result(stdout="running\n"),    # verify
        ]
        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=mock_virsh):
            assert s.start_vm("vm01") is True

    def test_start_failure_returns_false(self):
        s = _session({})
        mock_virsh = MagicMock()
        mock_virsh.execute.side_effect = [
            _result(stdout="shut off\n"),
            _result(ok=False, stderr="nope"),
        ]
        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=mock_virsh):
            assert s.start_vm("vm01") is False

    def test_still_not_running_after_start_returns_false(self):
        s = _session({})
        mock_virsh = MagicMock()
        mock_virsh.execute.side_effect = [
            _result(stdout="shut off\n"),
            _result(ok=True),
            _result(stdout="shut off\n"),  # verify still shut off
        ]
        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=mock_virsh):
            assert s.start_vm("vm01") is False

    def test_exception_returns_false(self):
        s = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand",
                   side_effect=RuntimeError("x")):
            assert s.start_vm("vm01") is False


class TestCloneVMDelegation:

    def test_calls_cloneVM_with_expected_args(self, tmp_path: Path):
        s = _session({"use_sudo": False})
        mock_cloner = MagicMock()
        mock_cloner.clone.return_value = True
        with patch("boxman.providers.libvirt.session.CloneVM",
                   return_value=mock_cloner) as clone_cls:
            s.clone_vm("new-vm", "src-vm", {"info": "x"}, str(tmp_path))

        _args, kwargs = clone_cls.call_args
        assert kwargs["new_vm_name"] == "new-vm"
        assert kwargs["src_vm_name"] == "src-vm"
        assert kwargs["workdir"] == str(tmp_path)
        mock_cloner.clone.assert_called_once()

    def test_raises_when_clone_fails(self, tmp_path: Path):
        s = _session({})
        mock_cloner = MagicMock()
        mock_cloner.clone.return_value = False
        with patch("boxman.providers.libvirt.session.CloneVM",
                   return_value=mock_cloner):
            with pytest.raises(RuntimeError, match="Failed to clone"):
                s.clone_vm("new-vm", "src", {}, str(tmp_path))
