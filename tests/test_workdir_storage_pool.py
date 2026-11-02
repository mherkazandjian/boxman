"""
The storage pool on a cluster's workdir, which ``provision``, ``up`` and
``update`` see to before cloning into it (#277): found by its target, never
by its name, and named after the project and the cluster when boxman
defines one.

It used to be named after the workdir's basename and looked up by that
name, so another project's ``cluster_1`` on its own directory was taken for
this one ("already exists"), and virt-install then defined a
``cluster_1-<n>`` on the real workdir, which leaked. The libvirt host here
is :class:`FakePools`, which answers as libvirt 10 does; it does not know
``pool-build``, so a test that issued one fails: boxman makes the workdir
itself.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

import fake_libvirt_pools
from boxman.exceptions import ProvisionError
from boxman.manager import BoxmanManager
from boxman.manager_parts import vms as vms_mod
from fake_libvirt_pools import FakePools

pytestmark = pytest.mark.unit

POOL = "bprj__demo__bprj_cluster_1_workdir"
VM = "bprj__demo__bprj_cluster_1_node01"

#: what changes a pool, or makes one
MUTATING = ("pool-define-as", "pool-start", "pool-autostart", "pool-destroy",
            "pool-undefine", "pool-refresh", "pool-delete")


@pytest.fixture(autouse=True)
def _never_pool_delete():
    fake_libvirt_pools.reset()
    yield
    deleted = [c for c in fake_libvirt_pools.ISSUED if c[0] == "pool-delete"]
    assert not deleted, f"pool-delete was issued: {deleted}"


@pytest.fixture
def virsh(monkeypatch):
    fake = FakePools()
    monkeypatch.setattr(vms_mod, "VirshCommand", lambda *a, **k: fake)
    return fake


def _manager(clusters: dict) -> BoxmanManager:
    with patch("boxman.manager.BoxmanCache"):
        mgr = BoxmanManager()
    mgr.config = {"project": "demo", "clusters": clusters}
    mgr._provider = MagicMock()
    mgr._provider.provider_config = {}
    mgr.logger = MagicMock()
    return mgr


def _cluster(workdir) -> dict:
    return {"workdir": str(workdir), "base_image": "tpl",
            "vms": {"node01": {}}}


def _logged(mgr, level: str) -> list[str]:
    return [str(c.args[0]) for c in getattr(mgr.logger, level).call_args_list]


def _touched(virsh, *names: str) -> list[tuple[str, ...]]:
    """The calls that changed one of the pools *names*."""
    return [c for c in virsh.verbs(*MUTATING) if c[1] in names]


def _state(virsh) -> dict:
    return {name: (p.target, p.active, p.autostart)
            for name, p in virsh.pools.items()}


class TestFoundByItsTarget:

    def test_with_none_on_it_one_named_after_project_and_cluster_is_defined(
            self, tmp_path, virsh):
        """Started, and set to start with the host."""
        workdir = tmp_path / "ws" / "cluster_1"
        mgr = _manager({"cluster_1": _cluster(workdir)})

        assert mgr._ensure_libvirt_storage_pool(
            str(workdir), "cluster_1") == POOL

        assert workdir.is_dir()      # made by boxman, not by pool-build
        assert virsh.verbs("pool-define-as") == [
            ("pool-define-as", POOL, "dir", "--target", str(workdir))]
        assert _state(virsh) == {POOL: (str(workdir), True, True)}

    @pytest.mark.parametrize("name", [
        "cluster_1",     # an older boxman's, named after the basename
        "cluster_1-3",   # virt-install's, when that name was taken
    ])
    def test_a_pool_already_on_it_is_used_whatever_its_name(
            self, tmp_path, virsh, name):
        workdir = tmp_path / "ws" / "cluster_1"
        workdir.mkdir(parents=True)
        virsh.define(name, str(workdir))
        mgr = _manager({"cluster_1": _cluster(workdir)})

        assert mgr._ensure_libvirt_storage_pool(
            str(workdir), "cluster_1") == name

        assert virsh.verbs(*MUTATING) == []
        assert list(virsh.pools) == [name]

    def test_an_inactive_one_on_it_is_started(self, tmp_path, virsh):
        workdir = tmp_path / "ws" / "cluster_1"
        workdir.mkdir(parents=True)
        virsh.define("cluster_1", str(workdir), active=False)
        mgr = _manager({"cluster_1": _cluster(workdir)})

        assert mgr._ensure_libvirt_storage_pool(
            str(workdir), "cluster_1") == "cluster_1"

        assert virsh.verbs(*MUTATING) == [("pool-start", "cluster_1")]
        assert virsh.pools["cluster_1"].active

    def test_one_of_boxmans_that_does_not_start_with_the_host_is_set_to(
            self, tmp_path, virsh):
        """As a definition whose ``pool-start`` failed leaves it: defined,
        down, and not set to start with the host."""
        workdir = tmp_path / "ws" / "cluster_1"
        workdir.mkdir(parents=True)
        virsh.define(POOL, str(workdir), active=False, autostart=False)
        mgr = _manager({"cluster_1": _cluster(workdir)})

        assert mgr._ensure_libvirt_storage_pool(
            str(workdir), "cluster_1") == POOL

        assert virsh.verbs(*MUTATING) == [("pool-start", POOL),
                                          ("pool-autostart", POOL)]
        assert _state(virsh) == {POOL: (str(workdir), True, True)}

    def test_one_boxman_did_not_name_keeps_its_autostart_setting(
            self, tmp_path, virsh):
        workdir = tmp_path / "ws" / "cluster_1"
        workdir.mkdir(parents=True)
        virsh.define("cluster_1", str(workdir), autostart=False)
        mgr = _manager({"cluster_1": _cluster(workdir)})

        mgr._ensure_libvirt_storage_pool(str(workdir), "cluster_1")

        assert virsh.verbs("pool-autostart") == []
        assert not virsh.pools["cluster_1"].autostart

    def test_its_name_taken_on_another_directory_gives_the_next_free_one(
            self, tmp_path, virsh):
        """The project's workdir moved, with the pool still on the old
        one: that pool is left as it is."""
        old = tmp_path / "old-ws" / "cluster_1"
        old.mkdir(parents=True)
        virsh.define(POOL, str(old), autostart=True)
        workdir = tmp_path / "ws" / "cluster_1"
        mgr = _manager({"cluster_1": _cluster(workdir)})

        assert mgr._ensure_libvirt_storage_pool(
            str(workdir), "cluster_1") == f"{POOL}_2"

        assert _touched(virsh, POOL) == []
        assert _state(virsh) == {POOL: (str(old), True, True),
                                 f"{POOL}_2": (str(workdir), True, True)}

    def test_another_projects_cluster_1_on_its_own_directory_is_never_taken(
            self, tmp_path, virsh):
        foreign = {}
        for name in ("cluster_1", "cluster_1-1", "cluster_1-2"):
            directory = tmp_path / "other" / name / "cluster_1"
            directory.mkdir(parents=True)
            virsh.define(name, str(directory), autostart=True)
            foreign[name] = (str(directory), True, True)
        workdir = tmp_path / "ws" / "cluster_1"
        mgr = _manager({"cluster_1": _cluster(workdir)})

        assert mgr._ensure_libvirt_storage_pool(
            str(workdir), "cluster_1") == POOL

        assert _touched(virsh, *foreign) == []
        assert _state(virsh) == {**foreign, POOL: (str(workdir), True, True)}

    def test_clusters_sharing_a_workdir_share_one_pool(self, tmp_path, virsh):
        """The first cluster to see to it names it; the other finds it by
        its target, in this run or a later one (an ``update`` adding a VM to
        it). The same holds for a workspace path used as a workdir."""
        shared = tmp_path / "ws"
        mgr = _manager({"cluster_1": _cluster(shared),
                        "cluster_2": _cluster(shared)})

        with patch.object(mgr, "_run_parallel", return_value=({}, {})):
            mgr.clone_vms()
        assert mgr._ensure_libvirt_storage_pool(
            str(shared), "cluster_2") == POOL

        assert virsh.verbs("pool-define-as") == [
            ("pool-define-as", POOL, "dir", "--target", str(shared))]
        assert list(virsh.pools) == [POOL]


class TestWithoutOne:
    """No pool on the workdir is a warning: virt-install and virt-clone
    define one themselves, and the clones go on."""

    @pytest.mark.parametrize("failing", [
        "pool-list", "pool-define-as", "pool-start"])
    def test_a_failure_is_a_warning_and_the_clone_goes_on(
            self, tmp_path, virsh, failing):
        virsh.failing[failing] = "error: failed to connect to the hypervisor"
        workdir = tmp_path / "ws" / "cluster_1"
        mgr = _manager({"cluster_1": _cluster(workdir)})

        with patch.object(mgr, "_run_parallel",
                          return_value=({}, {})) as parallel:
            mgr.clone_vms()

        assert any(str(workdir) in w and "no storage pool" in w
                   for w in _logged(mgr, "warning"))
        cloned = parallel.call_args_list[0].args[0]
        assert [task[0] for task in cloned] == [VM]

    def test_a_workdir_that_cannot_be_made_stops_the_clone_with_why(
            self, tmp_path, virsh):
        blocker = tmp_path / "ws"
        blocker.write_text("a file, not a directory\n")
        workdir = blocker / "cluster_1"
        mgr = _manager({"cluster_1": _cluster(workdir)})

        with patch.object(mgr, "_run_parallel",
                          return_value=({}, {})) as parallel, \
                pytest.raises(ProvisionError, match=str(workdir)):
            mgr.clone_vms()

        parallel.assert_not_called()
        assert virsh.calls == []


class TestIssue277:

    @pytest.mark.parametrize("caller", ["provision", "update"])
    def test_a_foreign_cluster_1_pool_no_longer_stands_in_for_the_workdir(
            self, tmp_path, virsh, caller):
        """The host has another project's ``cluster_1``, and the
        ``cluster_1-<n>`` virt-install left: the workdir still gets a pool
        of its own, so virt-install finds one on it and defines none."""
        foreign = {}
        for name in ("cluster_1", "cluster_1-1"):
            directory = tmp_path / "another-project" / name / "cluster_1"
            directory.mkdir(parents=True)
            virsh.define(name, str(directory), autostart=True)
            foreign[name] = (str(directory), True, True)
        workdir = tmp_path / "ws" / "cluster_1"
        mgr = _manager({"cluster_1": _cluster(workdir)})

        with patch.object(mgr, "_run_parallel", return_value=({}, {})):
            if caller == "provision":
                mgr.clone_vms()
            else:
                mgr._clone_and_configure_new_vms({VM})

        assert _state(virsh) == {**foreign, POOL: (str(workdir), True, True)}
        assert _touched(virsh, *foreign) == []
        assert not [line for level in ("info", "warning")
                    for line in _logged(mgr, level) if "already exists" in line]
        assert f"storage pool '{POOL}' is on {workdir}" in _logged(mgr, "info")


def test_the_pool_is_named_from_the_fallback_when_the_project_cannot_be(
        tmp_path, virsh):
    workdir = tmp_path / "ws" / "c1"
    mgr = _manager({"c1": _cluster(workdir)})
    mgr.config["project"] = "team/demo"

    pool = mgr._ensure_libvirt_storage_pool(str(workdir), "c1")

    assert pool.startswith("bprj__") and pool.endswith("_workdir")
    assert "/" not in pool
    assert virsh.pools[pool].target == os.fspath(workdir)
