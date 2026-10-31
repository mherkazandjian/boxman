"""
``boxman storage pools [--stale] [--prune [POOL ...]] [--dry-run] [-y]``
(#277): libvirt's storage pools, the ones left on directories that are
gone, and retiring those -- with or without a project's conf.yml.

Each target's status comes from the real probes (they run no virsh) on
directories under ``tmp_path``; libvirt is :class:`FakePools`, which raises
on ``pool-delete`` -- and no test here may see one issued.
"""

from __future__ import annotations

import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import fake_libvirt_pools
from boxman.exceptions import ConfigError, ProvisionError
from boxman.manager import BoxmanManager
from boxman.scripts.app import main
from fake_libvirt_pools import FakePools

pytestmark = pytest.mark.unit

PROVIDER = {"uri": "qemu:///system", "use_sudo": False, "runtime": "local"}

unless_root = pytest.mark.skipif(os.geteuid() == 0,
                                 reason="root searches a mode-000 directory")


@pytest.fixture(autouse=True)
def _libvirt(monkeypatch):
    fake_libvirt_pools.reset()
    virsh = FakePools()
    virsh.uri = "qemu:///system"
    seen = []

    def virsh_command(provider_config=None):
        seen.append(provider_config)
        return virsh
    monkeypatch.setattr("boxman.manager_parts.storage_pools.VirshCommand",
                        virsh_command)
    virsh.provider_configs = seen
    yield virsh
    deleted = [c for c in fake_libvirt_pools.ISSUED if c[0] == "pool-delete"]
    assert not deleted, f"pool-delete was issued: {deleted}"


@pytest.fixture
def host(_libvirt, tmp_path):
    """A host with a pool for every case: on a directory there, on one gone
    (twice: once used by a domain), this project's workdir, an RBD pool."""
    virsh = _libvirt
    (tmp_path / "there").mkdir()
    (tmp_path / "ws" / "cluster_1").mkdir(parents=True)
    virsh.define("cluster_1", str(tmp_path / "there"))
    virsh.define("cluster_1-11", str(tmp_path / "gone"), autostart=True)
    virsh.define("cluster_1-3", str(tmp_path / "gone-too"), active=False)
    virsh.define("bprj__demo__bprj_cluster_1_workdir",
                 str(tmp_path / "ws" / "cluster_1"))
    virsh.define("held", str(tmp_path / "gone-held"))
    virsh.define("ceph", None, type="rbd")
    virsh.domain("other-vm", "held")
    virsh.calls.clear()
    return virsh


def _manager(tmp_path, config=None):
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = config
    mgr._libvirt_provider_config = lambda: dict(PROVIDER)
    return mgr


def _project(tmp_path):
    return {"project": "demo", "workspace": {"path": str(tmp_path / "ws")},
            "clusters": {"cluster_1": {
                "workdir": str(tmp_path / "ws" / "cluster_1")}}}


def _args(**kwargs):
    defaults = {"stale": False, "prune": None, "dry_run": False, "yes": False}
    return SimpleNamespace(**{**defaults, **kwargs})


def _rows(out):
    """``{name: [state, autostart, target, status, note...]}`` of a
    listing."""
    lines = [line.split() for line in out.splitlines() if line.strip()]
    assert lines[0][:6] == ["NAME", "STATE", "AUTOSTART", "TARGET", "STATUS",
                            "NOTE"]
    return {line[0]: line[1:] for line in lines[1:]}


def _no_prompt(monkeypatch):
    monkeypatch.setattr("builtins.input",
                        lambda *a: pytest.fail("prompted"))


class TestList:

    def test_every_pool_is_listed_with_its_target_status_and_notes(
            self, tmp_path, host, capsys):
        _manager(tmp_path, _project(tmp_path)).storage_pools(_args())

        rows = _rows(capsys.readouterr().out)
        assert set(rows) == set(host.pools)
        assert rows["cluster_1"][:4] == ["active", "no",
                                         str(tmp_path / "there"), "there"]
        assert rows["cluster_1-11"][1:4] == ["yes", str(tmp_path / "gone"),
                                             "gone"]
        assert rows["cluster_1-3"][:1] == ["inactive"]
        assert rows["ceph"][2:4] == ["-", "-"]
        assert " ".join(rows["bprj__demo__bprj_cluster_1_workdir"][4:]) == (
            "workdir of this project; bprj-named")
        assert " ".join(rows["held"][4:]) == "used by other-vm"

    def test_without_a_project_no_pool_is_called_its_workdir(
            self, tmp_path, host, capsys):
        _manager(tmp_path).storage_pools(_args())
        rows = _rows(capsys.readouterr().out)
        assert rows["bprj__demo__bprj_cluster_1_workdir"][4:] == [
            "bprj-named"]

    @unless_root
    def test_a_target_that_cannot_be_searched_is_unknown(
            self, tmp_path, host, capsys):
        locked = tmp_path / "locked"
        (locked / "c1").mkdir(parents=True)
        host.define("hidden", str(locked / "c1"))
        locked.chmod(0)
        try:
            _manager(tmp_path).storage_pools(_args())
        finally:
            locked.chmod(0o755)
        assert _rows(capsys.readouterr().out)["hidden"][3] == "unknown"

    def test_stale_lists_only_directory_pools_whose_target_is_gone(
            self, tmp_path, host, capsys):
        _manager(tmp_path).storage_pools(_args(stale=True))
        assert set(_rows(capsys.readouterr().out)) == {
            "cluster_1-11", "cluster_1-3", "held"}

    def test_stale_with_none_says_so(self, tmp_path, _libvirt, capsys):
        (tmp_path / "there").mkdir()
        _libvirt.define("cluster_1", str(tmp_path / "there"))
        _manager(tmp_path).storage_pools(_args(stale=True))
        assert "no storage pool is left" in capsys.readouterr().out

    def test_listing_changes_nothing(self, tmp_path, host):
        _manager(tmp_path).storage_pools(_args())
        _manager(tmp_path).storage_pools(_args(stale=True))
        assert host.verbs("pool-destroy", "pool-undefine") == []

    def test_pools_it_cannot_read_are_an_error(self, tmp_path, host):
        host.failing["pool-list"] = "error: failed to connect"
        with pytest.raises(ProvisionError, match="could not read"):
            _manager(tmp_path).storage_pools(_args())

    @pytest.mark.parametrize("flag", ["dry_run", "yes"])
    def test_dry_run_and_yes_only_go_with_prune(self, tmp_path, host, flag):
        with pytest.raises(ConfigError, match="only apply to --prune"):
            _manager(tmp_path).storage_pools(_args(**{flag: True}))


class TestPrune:

    def test_the_prompt_lists_them_and_no_changes_nothing(
            self, tmp_path, host, capsys, monkeypatch):
        prompts = []
        monkeypatch.setattr("builtins.input",
                            lambda prompt: prompts.append(prompt) or "n")

        _manager(tmp_path).storage_pools(_args(prune=[]))

        out = capsys.readouterr().out
        assert prompts == ["Proceed? [y/N] "]
        assert f"cluster_1-11  {tmp_path / 'gone'}" in out
        assert f"cluster_1-3   {tmp_path / 'gone-too'}" in out
        assert "Aborted." in out
        assert host.verbs("pool-destroy", "pool-undefine") == []

    def test_yes_retires_every_stale_pool_no_domain_uses(
            self, tmp_path, host, capsys, monkeypatch):
        _no_prompt(monkeypatch)

        _manager(tmp_path).storage_pools(_args(prune=[], yes=True))

        assert host.retired() == ["cluster_1-11", "cluster_1-3"]
        assert set(host.pools) == {
            "cluster_1", "bprj__demo__bprj_cluster_1_workdir", "held", "ceph"}
        out = capsys.readouterr().out
        assert "skipping storage pool held: other-vm name(s) it" in out
        assert "removed storage pool cluster_1-11" in out

    def test_an_answer_of_yes_retires_them(self, tmp_path, host,
                                           monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        _manager(tmp_path).storage_pools(_args(prune=[]))
        assert host.retired() == ["cluster_1-11", "cluster_1-3"]

    def test_dry_run_lists_them_and_changes_nothing(
            self, tmp_path, host, capsys, monkeypatch):
        _no_prompt(monkeypatch)

        _manager(tmp_path).storage_pools(_args(prune=[], dry_run=True))

        out = capsys.readouterr().out
        assert "cluster_1-11" in out and "dry run: nothing was changed" in out
        assert host.verbs("pool-destroy", "pool-undefine") == []

    def test_named_pools_are_the_only_ones_retired(self, tmp_path, host,
                                                   monkeypatch):
        _no_prompt(monkeypatch)
        _manager(tmp_path).storage_pools(_args(prune=["cluster_1-3"],
                                               yes=True))
        assert host.retired() == ["cluster_1-3"]

    @pytest.mark.parametrize("name, why", [
        ("cluster_1", "is still there"),
        ("held", "other-vm name(s) it in a volume disk"),
        ("ceph", "it is a rbd pool"),
        ("missing", "no such storage pool"),
    ])
    def test_a_named_pool_that_may_not_be_pruned_stops_it_all(
            self, tmp_path, host, monkeypatch, name, why):
        _no_prompt(monkeypatch)
        with pytest.raises(ProvisionError, match=re.escape(why)):
            _manager(tmp_path).storage_pools(
                _args(prune=["cluster_1-11", name], yes=True))
        assert host.verbs("pool-destroy", "pool-undefine") == []

    @unless_root
    def test_an_unknown_target_is_never_pruned(self, tmp_path, host,
                                               monkeypatch, capsys):
        _no_prompt(monkeypatch)
        locked = tmp_path / "locked"
        locked.mkdir()
        host.define("hidden", str(locked / "gone"))
        locked.chmod(0)
        try:
            _manager(tmp_path).storage_pools(_args(prune=[], yes=True))
            with pytest.raises(ProvisionError, match="cannot be told"):
                _manager(tmp_path).storage_pools(_args(prune=["hidden"],
                                                       yes=True))
        finally:
            locked.chmod(0o755)
        assert "hidden" in host.pools
        assert "hidden" not in host.retired()

    def test_unreadable_domains_prune_nothing(self, tmp_path, host,
                                              monkeypatch):
        _no_prompt(monkeypatch)
        host.failing["dumpxml"] = "error: failed to connect"
        with pytest.raises(ProvisionError, match="which domains use"):
            _manager(tmp_path).storage_pools(_args(prune=[], yes=True))
        assert host.verbs("pool-destroy", "pool-undefine") == []

    def test_a_failed_retire_fails_the_command_after_the_rest(
            self, tmp_path, host, monkeypatch):
        _no_prompt(monkeypatch)
        host.failing[f"pool-destroy {host.pools['cluster_1-11'].uuid}"] = (
            "error: device busy")
        with pytest.raises(ProvisionError, match="cluster_1-11: pool-destroy"):
            _manager(tmp_path).storage_pools(_args(prune=[], yes=True))
        assert "cluster_1-3" not in host.pools
        assert "cluster_1-11" in host.pools

    def test_nothing_stale_is_nothing_to_do(self, tmp_path, _libvirt,
                                            monkeypatch, capsys):
        _no_prompt(monkeypatch)
        (tmp_path / "there").mkdir()
        _libvirt.define("cluster_1", str(tmp_path / "there"))
        _manager(tmp_path).storage_pools(_args(prune=[], yes=True))
        assert "no storage pool to prune" in capsys.readouterr().out


class TestDispatch:
    """The CLI: no conf.yml needed (#277)."""

    @staticmethod
    def _run(argv, tmp_path):
        with patch.object(sys, "argv", ["boxman", *argv]), \
                patch("boxman.config_cache.DEFAULT_CACHE_DIR",
                      str(tmp_path / "cache")):
            try:
                main()
                return 0
            except SystemExit as exc:
                return exc.code if isinstance(exc.code, int) else 0

    @staticmethod
    def _boxman_yml(tmp_path, runtime="local"):
        path = tmp_path / "boxman.yml"
        path.write_text(
            f"runtime: {runtime}\n"
            "providers:\n  libvirt:\n    uri: qemu+ssh://hv/system\n"
            "    use_sudo: false\n")
        return str(path)

    def test_without_a_conf_yml_boxman_yml_on_the_local_runtime_is_used(
            self, tmp_path, host, capsys):
        code = self._run(["--conf", str(tmp_path / "no-conf.yml"),
                          "--boxman-conf", self._boxman_yml(tmp_path,
                                                            "docker"),
                          "storage", "pools", "--stale"], tmp_path)
        assert code == 0
        assert host.provider_configs[0]["uri"] == "qemu+ssh://hv/system"
        assert host.provider_configs[0]["runtime"] == "local"
        assert "cluster_1-11" in capsys.readouterr().out

    def test_the_docker_runtime_without_a_conf_yml_is_refused(
            self, tmp_path, host):
        code = self._run(["--conf", str(tmp_path / "no-conf.yml"),
                          "--boxman-conf", self._boxman_yml(tmp_path),
                          "--runtime", "docker", "storage", "pools"],
                         tmp_path)
        assert code == 2
        assert host.calls == []

    def test_a_project_conf_yml_supplies_the_provider(self, tmp_path, host):
        conf = tmp_path / "conf.yml"
        conf.write_text(
            "version: '1.0'\nproject: demo\n"
            "provider:\n  libvirt:\n    uri: qemu:///session\n"
            f"workspace:\n  path: {tmp_path / 'ws'}\n"
            "clusters:\n  cluster_1:\n"
            f"    workdir: {tmp_path / 'ws' / 'cluster_1'}\n    vms: {{}}\n")
        code = self._run(["--conf", str(conf),
                          "--boxman-conf", self._boxman_yml(tmp_path),
                          "storage", "pools"], tmp_path)
        assert code == 0
        assert host.provider_configs[0]["uri"] == "qemu:///session"

    def test_a_failed_prune_exits_non_zero(self, tmp_path, host):
        code = self._run(["--conf", str(tmp_path / "no-conf.yml"),
                          "--boxman-conf", self._boxman_yml(tmp_path),
                          "storage", "pools", "--prune", "cluster_1", "-y"],
                         tmp_path)
        assert code == 2
        assert host.verbs("pool-destroy", "pool-undefine") == []
