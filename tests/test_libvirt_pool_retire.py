"""
Unit tests for the storage-pool helpers ``destroy`` retires the pools on
the directories it removed with (#277): ``read_pools``, ``pools_under``,
``domain_pool_refs`` and ``retire_pool`` in
boxman.providers.libvirt.storage_pools.

``pool-delete`` removes the directory itself, so no test here may see it
issued (:func:`_never_pool_delete`).
"""

from __future__ import annotations

import os

import pytest

import fake_libvirt_pools
from boxman.providers.libvirt.storage_pools import (
    Pool,
    domain_pool_refs,
    pools_under,
    read_pools,
    retire_pool,
)
from fake_libvirt_pools import FakePools

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _never_pool_delete():
    fake_libvirt_pools.reset()
    yield
    deleted = [c for c in fake_libvirt_pools.ISSUED if c[0] == "pool-delete"]
    assert not deleted, f"pool-delete was issued: {deleted}"


def _pool(name, target, type="dir"):
    return Pool(name=name, uuid=f"uuid-{name}", type=type, target=target,
                active=True, persistent=True)


def _names(pools):
    return [pool.name for pool in pools]


class TestReadPools:

    def test_every_pool_is_read_with_its_state(self, tmp_path):
        virsh = FakePools()
        virsh.define("cluster_1", str(tmp_path / "c1"))
        virsh.define("idle", str(tmp_path / "idle"), active=False)
        virsh.define("scratch", str(tmp_path / "s"), persistent=False)
        virsh.define("ceph", None, type="rbd")

        pools = {pool.name: pool for pool in read_pools(virsh)}

        assert pools["cluster_1"] == Pool(
            name="cluster_1", uuid=virsh.pools["cluster_1"].uuid, type="dir",
            target=str(tmp_path / "c1"), active=True, persistent=True)
        assert (pools["idle"].active, pools["idle"].persistent) == (
            False, True)
        assert (pools["scratch"].active, pools["scratch"].persistent) == (
            True, False)
        assert (pools["ceph"].type, pools["ceph"].target) == ("rbd", None)

    @pytest.mark.parametrize("failing", [
        "pool-list --all --name", "pool-list --name",
        "pool-list --all --transient", "pool-dumpxml"])
    def test_anything_unread_fails_closed(self, tmp_path, failing):
        virsh = FakePools()
        virsh.define("cluster_1", str(tmp_path))
        virsh.failing[failing] = "error: failed to connect to the hypervisor"
        assert read_pools(virsh) is None

    def test_a_pool_undefined_while_it_is_read_fails_closed(self, tmp_path):
        """Listed, then gone before its description is read: what else
        changed meanwhile is not known either."""
        virsh = FakePools()
        virsh.define("cluster_1", str(tmp_path / "c1"))
        virsh.define("cluster_2", str(tmp_path / "c2"))

        def undefine(call):
            if call[0] == "pool-dumpxml":
                virsh.pools.pop("cluster_2", None)
        virsh.before = undefine

        assert read_pools(virsh) is None

    def test_a_description_that_is_not_a_pool_fails_closed(self, tmp_path):
        virsh = FakePools()
        virsh.define("cluster_1", str(tmp_path))
        virsh._pool_dumpxml = lambda args: fake_libvirt_pools._result(
            stdout="<pool type='dir'><name>cluster_1</name>")
        assert read_pools(virsh) is None


class TestPoolsUnder:

    def test_a_pool_at_or_under_a_tree_is_found_and_one_beside_it_not(
            self, tmp_path):
        ws = tmp_path / "ws"
        for path in (ws / "cluster_1", tmp_path / "ws2"):
            path.mkdir(parents=True)
        pools = [_pool("ws", str(ws)), _pool("c1", str(ws / "cluster_1")),
                 _pool("ws2", str(tmp_path / "ws2")),
                 _pool("parent", str(tmp_path))]
        assert _names(pools_under(pools, [str(ws)])) == ["ws", "c1"]

    def test_a_symlink_in_the_tree_that_leads_out_is_not_under_it(
            self, tmp_path):
        """The removal takes the link, not what it points to."""
        ws, outside = tmp_path / "ws", tmp_path / "outside"
        ws.mkdir()
        outside.mkdir()
        (ws / "link").symlink_to(outside, target_is_directory=True)
        pools = [_pool("via-link", str(ws / "link")),
                 _pool("outside", str(outside))]
        assert pools_under(pools, [str(ws)]) == []

    def test_a_target_that_leads_into_the_tree_is_under_it(self, tmp_path):
        ws = tmp_path / "ws"
        (ws / "cluster_1").mkdir(parents=True)
        (tmp_path / "alias").symlink_to(ws / "cluster_1",
                                        target_is_directory=True)
        pools = [_pool("alias", str(tmp_path / "alias"))]
        assert _names(pools_under(pools, [str(ws)])) == ["alias"]

    def test_a_tree_reached_through_a_symlink_is_matched_by_where_it_is(
            self, tmp_path):
        real = tmp_path / "data" / "ws"
        (real / "cluster_1").mkdir(parents=True)
        (tmp_path / "ws").symlink_to(real, target_is_directory=True)
        pools = [_pool("c1", str(real / "cluster_1"))]
        assert _names(pools_under(pools, [str(tmp_path / "ws")])) == ["c1"]

    def test_a_target_already_gone_counts_by_its_spelling(self, tmp_path):
        """A retried destroy: the first one removed the directory, and was
        interrupted before the pool went."""
        real = tmp_path / "data" / "ws"
        real.mkdir(parents=True)
        (tmp_path / "ws").symlink_to(real, target_is_directory=True)
        pools = [_pool("as-configured", str(tmp_path / "ws" / "c1")),
                 _pool("as-resolved", str(real / "c2")),
                 _pool("unnormalized", str(real) + "/x/../c3"),
                 _pool("elsewhere", str(tmp_path / "other" / "c1"))]
        assert _names(pools_under(pools, [str(tmp_path / "ws")])) == [
            "as-configured", "as-resolved", "unnormalized"]

    def test_a_tree_already_gone_still_matches_its_spelling(self, tmp_path):
        pools = [_pool("c1", str(tmp_path / "ws" / "cluster_1"))]
        assert _names(pools_under(pools, [str(tmp_path / "ws")])) == ["c1"]

    @pytest.mark.skipif(os.geteuid() == 0,
                        reason="root searches a mode-000 directory")
    def test_a_target_that_cannot_be_resolved_is_not_under_it(self, tmp_path):
        ws = tmp_path / "ws"
        (ws / "locked" / "c1").mkdir(parents=True)
        (ws / "locked").chmod(0)
        try:
            pools = [_pool("c1", str(ws / "locked" / "c1"))]
            assert pools_under(pools, [str(ws)]) == []
        finally:
            (ws / "locked").chmod(0o755)

    def test_a_pool_of_another_type_or_with_no_directory_is_not_under_it(
            self, tmp_path):
        ws = tmp_path / "ws"
        (ws / "mnt").mkdir(parents=True)
        pools = [_pool("nfs", str(ws / "mnt"), type="netfs"),
                 _pool("ceph", None, type="rbd"),
                 _pool("relative", "ws/mnt")]
        assert pools_under(pools, [str(ws)]) == []


class TestDomainPoolRefs:

    def test_live_and_persistent_definitions_are_both_read(self):
        virsh = FakePools()
        virsh.domain("stopped", "pool_a")
        virsh.domain("running", "pool_b", running=True, live_pools=("pool_c",))
        virsh.domain("other", "pool_a", running=True)
        virsh.domain("no-volumes")

        assert domain_pool_refs(virsh) == {
            "pool_a": ["stopped", "other"], "pool_b": ["running"],
            "pool_c": ["running"]}

    def test_a_domain_shut_off_is_read_once(self):
        virsh = FakePools()
        virsh.domain("stopped", "pool_a")
        domain_pool_refs(virsh)
        assert virsh.verbs("dumpxml") == [
            ("dumpxml", "--inactive", virsh.domains["stopped"].uuid)]

    @pytest.mark.parametrize("failing", [
        "list --all", "list --uuid", "dumpxml"])
    def test_anything_unread_fails_closed(self, failing):
        virsh = FakePools()
        virsh.domain("running", "pool_a", running=True)
        virsh.failing[failing] = "error: failed to connect to the hypervisor"
        assert domain_pool_refs(virsh) is None

    def test_a_listing_it_cannot_parse_fails_closed(self):
        virsh = FakePools()
        virsh._list = lambda args: fake_libvirt_pools._result(
            stdout="not-a-uuid vm\n")
        assert domain_pool_refs(virsh) is None


class TestRetirePool:

    @staticmethod
    def _virsh(tmp_path, **kwargs):
        virsh = FakePools()
        virsh.define("cluster_1", str(tmp_path / "gone"), **kwargs)
        pool = next(p for p in read_pools(virsh) if p.name == "cluster_1")
        virsh.calls.clear()
        return virsh, pool

    def test_it_is_destroyed_then_undefined_by_its_uuid(self, tmp_path):
        virsh, pool = self._virsh(tmp_path)

        assert retire_pool(virsh, pool) is None

        assert [c for c in virsh.calls if c[0] != "pool-list"] == [
            ("pool-dumpxml", pool.uuid), ("pool-destroy", pool.uuid),
            ("pool-undefine", pool.uuid)]
        assert virsh.pools == {}

    def test_an_inactive_pool_is_only_undefined(self, tmp_path):
        virsh, pool = self._virsh(tmp_path, active=False)
        assert retire_pool(virsh, pool) is None
        assert virsh.verbs("pool-destroy") == []
        assert virsh.verbs("pool-undefine") == [("pool-undefine", pool.uuid)]

    def test_a_transient_pool_is_only_destroyed(self, tmp_path):
        virsh, pool = self._virsh(tmp_path, persistent=False)
        assert retire_pool(virsh, pool) is None
        assert virsh.verbs("pool-destroy") == [("pool-destroy", pool.uuid)]
        assert virsh.verbs("pool-undefine") == []

    def test_a_failed_destroy_is_not_followed_by_an_undefine(self, tmp_path):
        virsh, pool = self._virsh(tmp_path)
        virsh.failing["pool-destroy"] = "error: device or resource busy"

        why = retire_pool(virsh, pool)

        assert "still active and defined" in why
        assert "device or resource busy" in why
        assert virsh.verbs("pool-undefine") == []
        assert "cluster_1" in virsh.pools

    def test_a_failed_undefine_leaves_it_stopped_and_says_so(self, tmp_path):
        virsh, pool = self._virsh(tmp_path)
        virsh.failing["pool-undefine"] = "error: permission denied"

        why = retire_pool(virsh, pool)

        assert "stopped but still defined" in why
        assert not virsh.pools["cluster_1"].active

    def test_a_pool_that_vanished_meanwhile_counts_as_retired(self, tmp_path):
        virsh, pool = self._virsh(tmp_path)
        del virsh.pools["cluster_1"]

        assert retire_pool(virsh, pool) is None
        assert virsh.verbs("pool-destroy", "pool-undefine") == []

    def test_one_that_vanishes_between_its_reads_counts_as_retired(
            self, tmp_path):
        virsh, pool = self._virsh(tmp_path)

        def undefine_elsewhere(call):
            if call[0] == "pool-destroy":
                virsh.pools.pop("cluster_1", None)
        virsh.before = undefine_elsewhere

        assert retire_pool(virsh, pool) is None
        assert virsh.verbs("pool-undefine") == []

    def test_one_redefined_under_its_name_since_is_not_touched(self,
                                                               tmp_path):
        """By UUID, not by name: the new pool is another directory's."""
        virsh, pool = self._virsh(tmp_path)
        del virsh.pools["cluster_1"]
        virsh.define("cluster_1", str(tmp_path / "another"))
        virsh.pools["cluster_1"].uuid = "22222222-0000-4000-8000-000000000001"

        assert retire_pool(virsh, pool) is None
        assert virsh.verbs("pool-destroy", "pool-undefine") == []
        assert virsh.pools["cluster_1"].active

    def test_one_whose_target_changed_is_not_touched(self, tmp_path):
        virsh, pool = self._virsh(tmp_path)
        virsh.pools["cluster_1"].target = str(tmp_path / "elsewhere")

        why = retire_pool(virsh, pool)

        assert "no longer what it was" in why
        assert virsh.verbs("pool-destroy", "pool-undefine") == []

    def test_one_that_cannot_be_described_again_is_not_touched(self,
                                                               tmp_path):
        virsh, pool = self._virsh(tmp_path)
        virsh.failing["pool-dumpxml"] = "error: no connection"
        virsh.failing["pool-list"] = "error: no connection"

        assert "could not be described again" in retire_pool(virsh, pool)
        assert virsh.verbs("pool-destroy", "pool-undefine") == []

    def test_one_whose_state_cannot_be_read_is_not_touched(self, tmp_path):
        virsh, pool = self._virsh(tmp_path)
        virsh.failing["pool-list --all --transient"] = "error: no connection"

        assert "could not be read" in retire_pool(virsh, pool)
        assert virsh.verbs("pool-destroy", "pool-undefine") == []
