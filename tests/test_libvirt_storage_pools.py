"""
Unit tests for boxman.providers.libvirt.storage_pools (#208): after boxman
removes a VM's files itself, the pools that listed them are refreshed.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from boxman.providers.libvirt.storage_pools import (
    ensure_pool_on,
    refresh_pools_holding,
)

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


class TestEnsurePoolOn:
    """A storage pool on a cluster's disk_snapshot_dir: without one, the
    overlays there cannot be read under ``use_sudo: false`` (#221), and
    every teardown keeps everything."""

    @staticmethod
    def _virsh(pools, active=None, define_ok=True, list_ok=True):
        """*pools*: name -> target path; *active*: the running ones (all
        by default)."""
        pools = dict(pools)
        running = set(pools) if active is None else set(active)
        virsh = MagicMock()

        def execute(*args, **kwargs):
            verb = args[0]
            if verb == "pool-list":
                names = pools if "--all" in args else running
                return _result(stdout="".join(f"{p}\n" for p in names),
                               ok=list_ok, stderr="error: no connection")
            if verb == "pool-dumpxml":
                return _result(stdout=f"<pool type='dir'><name>{args[1]}"
                                      f"</name><target><path>"
                                      f"{pools[args[1]]}</path></target>"
                                      f"</pool>")
            if verb == "pool-define-as":
                if define_ok:
                    pools[args[1]] = args[args.index("--target") + 1]
                return _result(ok=define_ok, stderr="error: define failed")
            if verb in ("pool-start", "pool-autostart"):
                if verb == "pool-start":
                    running.add(args[1])
                return _result()
            raise AssertionError(args)

        virsh.execute.side_effect = execute
        return virsh

    @staticmethod
    def _verbs(virsh, *wanted):
        return [c.args for c in virsh.execute.call_args_list
                if c.args[0] in wanted]

    def test_one_already_on_it_is_used_whatever_its_name(self, tmp_path):
        ov = tmp_path / "ov"
        ov.mkdir()
        virsh = self._virsh({"web": str(tmp_path / "web"), "mine": str(ov)})

        assert ensure_pool_on(virsh, str(ov), "bprj_ov") == "mine"
        assert self._verbs(virsh, "pool-define-as", "pool-start") == []

    def test_an_inactive_one_on_it_is_started(self, tmp_path):
        ov = tmp_path / "ov"
        ov.mkdir()
        virsh = self._virsh({"mine": str(ov)}, active=[])

        assert ensure_pool_on(virsh, str(ov), "bprj_ov") == "mine"
        assert self._verbs(virsh, "pool-start") == [("pool-start", "mine")]

    def test_with_none_one_is_defined_started_and_autostarted(self, tmp_path):
        ov = tmp_path / "ov"
        ov.mkdir()
        virsh = self._virsh({"web": str(tmp_path / "web")})

        assert ensure_pool_on(virsh, str(ov), "bprj_ov") == "bprj_ov"
        assert self._verbs(virsh, "pool-define-as", "pool-start",
                           "pool-autostart") == [
            ("pool-define-as", "bprj_ov", "dir", "--target", str(ov)),
            ("pool-start", "bprj_ov"),
            ("pool-autostart", "bprj_ov"),
        ]

    def test_a_pool_of_that_name_on_another_directory_is_not_taken(
            self, tmp_path):
        """A workdir ``.../ws/web`` and an overlay directory ``.../ov/web``
        share a basename: a pool found by name would not be on it."""
        ov = tmp_path / "ov" / "web"
        ov.mkdir(parents=True)
        virsh = self._virsh({"web": str(tmp_path / "ws" / "web")})

        assert ensure_pool_on(virsh, str(ov), "web") is None
        assert self._verbs(virsh, "pool-define-as") == []

    def test_one_on_it_under_another_name_is_used(self, tmp_path):
        ov = tmp_path / "ov"
        ov.mkdir()
        (tmp_path / "alias").symlink_to(ov)
        virsh = self._virsh({"mine": str(tmp_path / "alias")})

        assert ensure_pool_on(virsh, str(ov), "bprj_ov") == "mine"

    @pytest.mark.skipif(os.geteuid() == 0,
                        reason="root searches a mode-000 directory")
    def test_one_whose_directory_cannot_be_looked_up_is_not_taken_for_it(
            self, tmp_path):
        """``hidden/link/../../ov``, ``link`` leading into ``actual/a/b``, is
        ``actual/ov``; under an unsearchable ``hidden``, ``realpath`` folds
        it as text into the directory asked for."""
        ov = tmp_path / "ov"
        ov.mkdir()
        (tmp_path / "actual" / "a" / "b").mkdir(parents=True)
        (tmp_path / "actual" / "ov").mkdir()
        hidden = tmp_path / "hidden"
        hidden.mkdir()
        (hidden / "link").symlink_to(tmp_path / "actual" / "a" / "b")
        virsh = self._virsh(
            {"other": os.path.join(hidden, "link", "..", "..", "ov")})
        hidden.chmod(0)
        try:
            pool = ensure_pool_on(virsh, str(ov), "bprj_ov")
        finally:
            hidden.chmod(0o700)

        assert pool == "bprj_ov"
        assert self._verbs(virsh, "pool-define-as") == [
            ("pool-define-as", "bprj_ov", "dir", "--target", str(ov))]

    def test_pools_that_cannot_be_listed_give_none(self, tmp_path):
        virsh = self._virsh({}, list_ok=False)

        assert ensure_pool_on(virsh, str(tmp_path), "bprj_ov") is None

    def test_a_definition_that_fails_gives_none(self, tmp_path):
        virsh = self._virsh({}, define_ok=False)

        assert ensure_pool_on(virsh, str(tmp_path), "bprj_ov") is None
