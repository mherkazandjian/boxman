"""
Unit tests for boxman.providers.libvirt.clone_vm.CloneVM.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.clone_vm import CloneVM


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
def clone(tmp_path: Path) -> CloneVM:
    return CloneVM(
        src_vm_name="template-base",
        new_vm_name="vm01",
        info={"network_adapters": [{"network": "default"}]},
        workdir=str(tmp_path),
        provider_config={"use_sudo": False},
    )


class TestConstruction:

    def test_image_path_derived_from_workdir(self, tmp_path: Path):
        c = CloneVM(
            src_vm_name="base",
            new_vm_name="vm-new",
            info={},
            workdir=str(tmp_path),
            provider_config=None,
        )
        assert c.new_image_path == str(tmp_path / "vm-new.qcow2")

    def test_workdir_tilde_is_expanded(self):
        c = CloneVM(
            src_vm_name="base",
            new_vm_name="vm-new",
            info={},
            workdir="~/fake-boxman-workdir",
            provider_config=None,
        )
        assert "~" not in c.new_image_path
        assert c.new_image_path.endswith("/vm-new.qcow2")


class TestCreateClone:

    def test_success_path_calls_virt_clone_with_correct_args(self, clone: CloneVM):
        with patch.object(clone.virt_clone, "execute") as virt_clone_exec, \
             patch.object(clone, "remove_network_interfaces", return_value=True):
            virt_clone_exec.return_value = _result()
            assert clone.create_clone() is True

        (_args, kwargs) = virt_clone_exec.call_args
        assert kwargs["original"] == "template-base"
        assert kwargs["name"] == "vm01"
        assert kwargs["file"].endswith("vm01.qcow2")
        assert kwargs["auto_clone"] is True

    def test_skips_iface_removal_when_no_network_adapters(self, tmp_path: Path):
        c = CloneVM(
            src_vm_name="base",
            new_vm_name="vmx",
            info={},  # no network_adapters
            workdir=str(tmp_path),
            provider_config=None,
        )
        with patch.object(c.virt_clone, "execute", return_value=_result()) as virt_clone, \
             patch.object(c, "remove_network_interfaces") as remove_ifaces:
            assert c.create_clone() is True
            virt_clone.assert_called_once()
            remove_ifaces.assert_not_called()

    def test_runtime_error_returns_false(self, clone: CloneVM):
        with patch.object(clone.virt_clone, "execute", side_effect=RuntimeError("boom")):
            assert clone.create_clone() is False

    def test_remove_ifaces_failure_logs_warning_but_still_succeeds(
        self, clone: CloneVM, captured_logs
    ):
        with patch.object(clone.virt_clone, "execute", return_value=_result()), \
             patch.object(clone, "remove_network_interfaces", return_value=False):
            assert clone.create_clone() is True
        assert any(
            "failed to remove network interfaces" in rec.message
            for rec in captured_logs.records
        )


class TestRemoveNetworkInterfaces:

    DOMIFLIST_SAMPLE = (
        "Interface   Type       Source     Model       MAC\n"
        "------------------------------------------------------\n"
        "vnet0       network    default    virtio      52:54:00:aa:bb:cc\n"
        "vnet1       network    extra      virtio      52:54:00:aa:bb:dd\n"
    )

    def test_parses_and_detaches_each_interface(self, clone: CloneVM):
        calls: list[tuple[tuple, dict]] = []

        def fake_execute(*args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == "domiflist":
                return _result(stdout=self.DOMIFLIST_SAMPLE)
            # detach-interface returns ok
            return _result()

        with patch.object(clone.virsh, "execute", side_effect=fake_execute):
            assert clone.remove_network_interfaces() is True

        detaches = [
            args for args, _kwargs in calls if args and args[0] == "detach-interface"
        ]
        assert len(detaches) == 2
        macs = [a for args in detaches for a in args if str(a).startswith("--mac=")]
        assert "--mac=52:54:00:aa:bb:cc" in macs
        assert "--mac=52:54:00:aa:bb:dd" in macs

    def test_empty_interface_list_returns_true(self, clone: CloneVM):
        with patch.object(
            clone.virsh, "execute",
            return_value=_result(stdout="Interface   Type\n-----------------\n"),
        ):
            assert clone.remove_network_interfaces() is True

    def test_domiflist_failure_returns_false(self, clone: CloneVM, captured_logs):
        with patch.object(clone.virsh, "execute", return_value=_result(ok=False)):
            assert clone.remove_network_interfaces() is False

    def test_detach_failure_is_logged_but_loop_continues(
        self, clone: CloneVM, captured_logs
    ):
        """Individual failures warn, overall returns True (best-effort)."""
        def fake_execute(*args, **_kwargs):
            if args[0] == "domiflist":
                return _result(stdout=self.DOMIFLIST_SAMPLE)
            return _result(ok=False, stderr="detach failed")

        with patch.object(clone.virsh, "execute", side_effect=fake_execute):
            assert clone.remove_network_interfaces() is True

        assert any(
            "failed to remove interface" in rec.message for rec in captured_logs.records
        )

    def test_unexpected_exception_returns_false(self, clone: CloneVM, captured_logs):
        with patch.object(clone.virsh, "execute", side_effect=ValueError("weird")):
            assert clone.remove_network_interfaces() is False


class TestCloneWrapper:

    def test_returns_false_when_create_clone_fails(self, clone: CloneVM):
        with patch.object(clone, "create_clone", return_value=False):
            assert clone.clone() is False

    def test_returns_true_on_successful_create(self, clone: CloneVM):
        with patch.object(clone, "create_clone", return_value=True):
            assert clone.clone() is True
