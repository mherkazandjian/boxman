"""
Unit tests for boxman.providers.libvirt.storage_pools (#208): after boxman
removes a VM's files itself, the pools that listed them are refreshed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from boxman.providers.libvirt.storage_pools import refresh_pools_holding

pytestmark = pytest.mark.unit


def _result(stdout="", ok=True, stderr=""):
    return MagicMock(stdout=stdout, ok=ok, stderr=stderr)


def _virsh(pools, refresh_ok=True, list_ok=True):
    """*pools* maps pool name -> target path (``None``: unreadable XML)."""
    virsh = MagicMock()

    def execute(*args, **kwargs):
        if args[0] == "pool-list":
            return _result(stdout="".join(f"{p}\n" for p in pools),
                           ok=list_ok, stderr="error: no connection")
        if args[0] == "pool-dumpxml":
            path = pools[args[1]]
            if path is None:
                return _result(ok=False)
            return _result(stdout=f"<pool type='dir'><name>{args[1]}</name>"
                                  f"<target><path>{path}</path></target></pool>")
        if args[0] == "pool-refresh":
            return _result(ok=refresh_ok, stderr="error: refresh failed")
        raise AssertionError(args)

    virsh.execute.side_effect = execute
    return virsh


def _refreshes(virsh):
    return [c.args[1] for c in virsh.execute.call_args_list
            if c.args[0] == "pool-refresh"]


def test_only_the_pools_that_held_a_removed_file_are_refreshed(tmp_path):
    virsh = _virsh({"cluster_1": str(tmp_path / "c1"),
                    "templates": str(tmp_path / "tpl")})
    refreshed = refresh_pools_holding(
        virsh, [str(tmp_path / "c1" / "vm.qcow2"),
                str(tmp_path / "c1" / "vm.s1")])
    assert refreshed == ["cluster_1"]
    assert _refreshes(virsh) == ["cluster_1"]


def test_a_pool_reached_through_a_symlink_matches(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    virsh = _virsh({"cluster_1": str(tmp_path / "alias")})
    assert refresh_pools_holding(virsh, [str(real / "vm.qcow2")]) == [
        "cluster_1"]


def test_nothing_removed_asks_nothing():
    virsh = _virsh({})
    assert refresh_pools_holding(virsh, []) == []
    virsh.execute.assert_not_called()


def test_failures_are_warnings_not_errors(tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr("boxman.providers.libvirt.storage_pools.log.warning",
                        warnings.append)
    virsh = _virsh({"cluster_1": str(tmp_path), "broken": None},
                   refresh_ok=False)
    assert refresh_pools_holding(virsh, [str(tmp_path / "vm.qcow2")]) == []
    assert any("cluster_1" in w for w in warnings)
    assert any("broken" in w for w in warnings)


def test_an_unlistable_pool_set_is_a_warning(tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr("boxman.providers.libvirt.storage_pools.log.warning",
                        warnings.append)
    virsh = _virsh({"cluster_1": str(tmp_path)}, list_ok=False)
    assert refresh_pools_holding(virsh, [str(tmp_path / "vm.qcow2")]) == []
    assert warnings
