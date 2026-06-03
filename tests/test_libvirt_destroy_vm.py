"""
Unit tests for boxman.providers.libvirt.destroy_vm.DestroyVM.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.destroy_vm import DestroyVM


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
def dv() -> DestroyVM:
    return DestroyVM(name="vm01", provider_config={"use_sudo": False})


class TestStateProbes:

    def test_is_vm_running_true_when_running(self, dv: DestroyVM):
        with patch.object(dv, "execute", return_value=_result(stdout="running\n")):
            assert dv.is_vm_running() is True

    def test_is_vm_running_false_when_shut_off(self, dv: DestroyVM):
        with patch.object(dv, "execute", return_value=_result(stdout="shut off\n")):
            assert dv.is_vm_running() is False

    def test_is_vm_running_false_on_runtime_error(self, dv: DestroyVM):
        with patch.object(dv, "execute", side_effect=RuntimeError("no such domain")):
            assert dv.is_vm_running() is False

    def test_is_vm_shut_off_true_when_shut_off(self, dv: DestroyVM):
        with patch.object(dv, "execute", return_value=_result(stdout="shut off\n")):
            assert dv.is_vm_shut_off() is True

    def test_is_vm_shut_off_false_when_running(self, dv: DestroyVM):
        with patch.object(dv, "execute", return_value=_result(stdout="running\n")):
            assert dv.is_vm_shut_off() is False

    def test_is_vm_shut_off_true_when_domstate_fails(self, dv: DestroyVM):
        """domain gone → effectively stopped per the module's contract."""
        with patch.object(dv, "execute", return_value=_result(ok=False)):
            assert dv.is_vm_shut_off() is True

    def test_is_vm_shut_off_true_on_runtime_error(self, dv: DestroyVM):
        with patch.object(dv, "execute", side_effect=RuntimeError("x")):
            assert dv.is_vm_shut_off() is True

    def test_is_vm_defined_true_when_dominfo_ok(self, dv: DestroyVM):
        with patch.object(dv, "execute", return_value=_result(ok=True)):
            assert dv.is_vm_defined() is True

    def test_is_vm_defined_false_when_dominfo_fails(self, dv: DestroyVM):
        with patch.object(dv, "execute", return_value=_result(ok=False)):
            assert dv.is_vm_defined() is False


class TestShutdownVM:

    def test_noop_when_already_stopped(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", return_value=False):
            assert dv.shutdown_vm() is True

    def test_graceful_shutdown_success(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", return_value=True), \
             patch.object(dv, "execute", return_value=_result()), \
             patch.object(dv, "is_vm_shut_off", side_effect=[False, True]), \
             patch("boxman.providers.libvirt.destroy_vm.time.sleep"):
            assert dv.shutdown_vm(timeout=5) is True

    def test_graceful_timeout_without_force_returns_false(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", return_value=True), \
             patch.object(dv, "execute", return_value=_result()), \
             patch.object(dv, "is_vm_shut_off", return_value=False), \
             patch("boxman.providers.libvirt.destroy_vm.time.sleep"):
            assert dv.shutdown_vm(timeout=2, force=False) is False

    def test_graceful_timeout_with_force_calls_force_shutdown(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", return_value=True), \
             patch.object(dv, "execute", return_value=_result()), \
             patch.object(dv, "is_vm_shut_off", return_value=False), \
             patch("boxman.providers.libvirt.destroy_vm.time.sleep"), \
             patch.object(dv, "force_shutdown_vm", return_value=True) as force:
            assert dv.shutdown_vm(timeout=2, force=True) is True
            force.assert_called_once()

    def test_runtime_error_returns_false(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", return_value=True), \
             patch.object(dv, "execute", side_effect=RuntimeError("boom")):
            assert dv.shutdown_vm(timeout=1) is False


class TestForceShutdown:

    def test_noop_when_not_running(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", return_value=False):
            assert dv.force_shutdown_vm() is True

    def test_success_when_destroy_stops_the_vm(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", side_effect=[True, False]), \
             patch.object(dv, "execute", return_value=_result()):
            assert dv.force_shutdown_vm() is True

    def test_failure_when_vm_still_running_after_destroy(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", side_effect=[True, True]), \
             patch.object(dv, "execute", return_value=_result()):
            assert dv.force_shutdown_vm() is False

    def test_runtime_error_returns_false(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", return_value=True), \
             patch.object(dv, "execute", side_effect=RuntimeError("boom")):
            assert dv.force_shutdown_vm() is False


class TestDestroyVMDispatch:

    def test_force_true_goes_straight_to_force_shutdown(self, dv: DestroyVM):
        with patch.object(dv, "force_shutdown_vm", return_value=True) as f, \
             patch.object(dv, "shutdown_vm") as graceful:
            assert dv.destroy_vm(force=True) is True
            f.assert_called_once()
            graceful.assert_not_called()

    def test_force_none_tries_graceful_with_force_fallback(self, dv: DestroyVM):
        with patch.object(dv, "shutdown_vm", return_value=True) as graceful:
            assert dv.destroy_vm(force=None) is True
            _args, kwargs = graceful.call_args
            assert kwargs["force"] is True   # None → force is not False → True

    def test_force_false_disables_fallback(self, dv: DestroyVM):
        with patch.object(dv, "shutdown_vm", return_value=True) as graceful:
            dv.destroy_vm(force=False)
            _args, kwargs = graceful.call_args
            assert kwargs["force"] is False


class TestUndefine:

    def test_noop_when_not_defined(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_defined", return_value=False):
            assert dv.undefine_vm() is True

    def test_success_when_undefine_removes_domain(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_defined", side_effect=[True, False]), \
             patch.object(dv, "execute", return_value=_result()):
            assert dv.undefine_vm() is True

    def test_failure_when_domain_still_defined_after(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_defined", side_effect=[True, True]), \
             patch.object(dv, "execute", return_value=_result()):
            assert dv.undefine_vm() is False


class TestForceUndefine:

    def test_kills_running_domain_before_undefine(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_defined", side_effect=[True, False]), \
             patch.object(dv, "is_vm_shut_off", return_value=False), \
             patch.object(dv, "execute", return_value=_result()) as execute:
            assert dv.force_undefine_vm() is True
        calls = [c.args[0] for c in execute.call_args_list]
        assert "destroy" in calls  # force-kill first
        assert any("undefine --remove-all-storage" in c for c in calls)

    def test_skips_kill_when_already_shut_off(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_defined", side_effect=[True, False]), \
             patch.object(dv, "is_vm_shut_off", return_value=True), \
             patch.object(dv, "execute", return_value=_result()) as execute:
            dv.force_undefine_vm()
        calls = [c.args[0] for c in execute.call_args_list]
        assert "destroy" not in calls


class TestRemove:
    """`remove()` delegates undefine to force_undefine_vm (atomic
    --snapshots-metadata) and must never iterate per-snapshot
    `virsh snapshot-delete`, which wedges running/paused domains with saved
    external snapshots (regression: the 10-VM teardown lock-timeout pileup)."""

    def test_delegates_to_force_undefine(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", return_value=False), \
             patch.object(dv, "force_undefine_vm", return_value=True) as fu:
            assert dv.remove() is True
            fu.assert_called_once()

    def test_non_force_attempts_graceful_shutdown_first(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", return_value=True), \
             patch.object(dv, "shutdown_vm", return_value=True) as graceful, \
             patch.object(dv, "force_undefine_vm", return_value=True) as fu:
            assert dv.remove(force=None) is True
            _a, kwargs = graceful.call_args
            assert kwargs["force"] is False  # graceful only; fu force-kills the rest
            fu.assert_called_once()

    def test_force_skips_graceful_shutdown(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", return_value=True), \
             patch.object(dv, "shutdown_vm") as graceful, \
             patch.object(dv, "force_undefine_vm", return_value=True) as fu:
            assert dv.remove(force=True) is True
            graceful.assert_not_called()
            fu.assert_called_once()

    def test_paused_vm_is_force_killed_and_undefined(self, dv: DestroyVM):
        """A paused VM (is_vm_running False, is_vm_shut_off False) must still be
        force-killed and undefined — the bug that left boxman04 wedged."""
        # is_vm_running False -> graceful skipped; force_undefine_vm runs for real
        with patch.object(dv, "is_vm_running", return_value=False), \
             patch.object(dv, "is_vm_defined", side_effect=[True, False]), \
             patch.object(dv, "is_vm_shut_off", return_value=False), \
             patch.object(dv, "execute", return_value=_result()) as execute:
            assert dv.remove() is True
        calls = [c.args[0] for c in execute.call_args_list]
        assert "destroy" in calls  # force-killed the paused domain
        assert any("undefine" in c and "--snapshots-metadata" in c for c in calls)

    def test_never_issues_per_snapshot_snapshot_delete(self, dv: DestroyVM):
        with patch.object(dv, "is_vm_running", return_value=False), \
             patch.object(dv, "is_vm_defined", side_effect=[True, False]), \
             patch.object(dv, "is_vm_shut_off", return_value=True), \
             patch.object(dv, "execute", return_value=_result()) as execute:
            assert dv.remove() is True
        commands = [c.args[0] for c in execute.call_args_list]
        assert not any(c.startswith("snapshot-delete") for c in commands)
        assert any("--snapshots-metadata" in c for c in commands)
