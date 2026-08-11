"""
Unit tests for boxman.providers.libvirt.virsh_edit.VirshEdit.

Currently pins the issue #85 item 38 fixes: the ``if not result.ok``
failure branches in the hot-update helpers must be reachable — the
virsh calls are made with ``warn=True`` and a failed command returns
False instead of raising RuntimeError.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.virsh_edit import VirshEdit

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
def ve() -> VirshEdit:
    return VirshEdit(provider_config={"use_sudo": False,
                                      "uri": "qemu:///system"})


class TestDeadErrorBranches:
    """Issue #85 item 38: each helper must pass warn=True so its
    not-ok branch is live, and must return False on failure."""

    def test_hot_set_vcpus_failure_returns_false(self, ve: VirshEdit):
        with patch.object(ve.virsh, "execute",
                          return_value=_result(ok=False, stderr="x")) as exe:
            assert ve.hot_set_vcpus("vm01", 4) is False
        assert exe.call_args.kwargs.get("warn") is True

    def test_hot_set_memory_failure_returns_false(self, ve: VirshEdit):
        with patch.object(ve.virsh, "execute",
                          return_value=_result(ok=False, stderr="x")) as exe:
            assert ve.hot_set_memory("vm01", 1024) is False
        assert exe.call_args.kwargs.get("warn") is True

    def test_hot_set_vcpus_success_returns_true(self, ve: VirshEdit):
        with patch.object(ve.virsh, "execute", return_value=_result()):
            assert ve.hot_set_vcpus("vm01", 4) is True

    def test_hot_set_memory_success_returns_true(self, ve: VirshEdit):
        with patch.object(ve.virsh, "execute", return_value=_result()):
            assert ve.hot_set_memory("vm01", 1024) is True
