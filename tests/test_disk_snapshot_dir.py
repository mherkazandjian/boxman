"""
A cluster's ``disk_snapshot_dir``: where ``snapshot take`` creates its VMs'
disk overlays, instead of beside each disk.

After a take the newest overlay is the disk the guest writes to, so this
directory holds live VM disks, not backups. The overlays keep libvirt's
names (``<vm>.<snapshot>``, ``<vm>_<disk>.<snapshot>``), so every
name-anchored rule still applies. The teardown side is in
test_vm_teardown_storage.py.
"""

from __future__ import annotations

import os
import stat
import types
from unittest.mock import MagicMock

import pytest

from boxman.exceptions import ConfigError, SnapshotError
from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit

VM = "bprj__demo__bprj_cluster_1_node01"


def _manager(cluster: dict, runtime: str | None = None) -> BoxmanManager:
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = {
        "project": "demo",
        "clusters": {"cluster_1": {"vms": {"node01": {}}, **cluster}},
    }
    mgr.provider = MagicMock()
    mgr.provider.has_managed_save.return_value = False
    mgr.provider.ensure_pool_on.return_value = "a-pool"
    mgr.logger = MagicMock()
    mgr.cache = MagicMock()
    mgr.cache.projects = {}
    if runtime is not None:
        mgr._runtime_name = runtime
    return mgr


class TestTheDirectory:

    def test_unset_is_none_beside_the_disks(self, tmp_path):
        mgr = _manager({"workdir": str(tmp_path / "c1")})

        assert mgr.cluster_disk_snapshot_dir("cluster_1") is None

    def test_set_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "disk_snapshot_dir": "~/overlays"})

        assert (mgr.cluster_disk_snapshot_dir("cluster_1")
                == str(tmp_path / "overlays"))

    def test_a_dotdot_after_a_symlink_is_left_to_the_filesystem(
            self, tmp_path):
        """Folded by its spelling, ``link/../ov`` names the directory beside
        ``link``, not the one the filesystem reaches."""
        (tmp_path / "actual" / "child").mkdir(parents=True)
        (tmp_path / "actual" / "ov").mkdir()
        (tmp_path / "ov").mkdir()
        os.symlink(tmp_path / "actual" / "child", tmp_path / "link")
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "disk_snapshot_dir": f"{tmp_path}/link/../ov"})

        assert os.path.samefile(mgr.cluster_disk_snapshot_dir("cluster_1"),
                                tmp_path / "actual" / "ov")

    def test_a_relative_one_is_refused(self, tmp_path):
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "disk_snapshot_dir": "overlays"})

        with pytest.raises(ConfigError, match="disk_snapshot_dir must be an "
                                              "absolute path"):
            mgr.cluster_disk_snapshot_dir("cluster_1")

    @pytest.mark.parametrize("value", ["", "   ", 7, ["/data"], {"p": 1}],
                             ids=["empty", "blank", "int", "list", "dict"])
    def test_a_value_that_is_not_a_path_is_refused(self, tmp_path, value):
        mgr = _manager({"workdir": str(tmp_path), "disk_snapshot_dir": value})

        with pytest.raises(ConfigError,
                           match="clusters.cluster_1.disk_snapshot_dir must be"):
            mgr.cluster_disk_snapshot_dir("cluster_1")

    def test_a_container_runtime_refuses_it(self, tmp_path):
        mgr = _manager({"workdir": str(tmp_path),
                        "disk_snapshot_dir": str(tmp_path / "o")},
                       runtime="docker-compose")

        with pytest.raises(ConfigError, match="disk_snapshot_dir is not "
                                              "supported under the "
                                              "'docker-compose' runtime"):
            mgr.cluster_disk_snapshot_dir("cluster_1")

    def test_a_docker_compose_cluster_refuses_it(self, tmp_path):
        mgr = _manager({"workdir": str(tmp_path),
                        "disk_snapshot_dir": str(tmp_path / "o")})
        mgr.provider_type_for_cluster = MagicMock(return_value="docker-compose")

        with pytest.raises(ConfigError, match="docker-compose cluster takes "
                                              "no disk_snapshot_dir"):
            mgr.cluster_disk_snapshot_dir("cluster_1")

    def test_validate_checks_both_keys_of_every_cluster(self, tmp_path):
        mgr = _manager({"workdir": str(tmp_path)})
        mgr.config["clusters"]["cluster_2"] = {
            "workdir": str(tmp_path / "c2"),
            "memory_snapshot_dir": str(tmp_path / "m2"),
            "disk_snapshot_dir": 0, "vms": {}}

        with pytest.raises(ConfigError, match="cluster_2.disk_snapshot_dir"):
            mgr.validate_snapshot_dirs()

    def test_only_the_clusters_that_set_one_are_listed(self, tmp_path):
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": str(tmp_path / "m1")})
        mgr.config["clusters"]["cluster_2"] = {
            "workdir": str(tmp_path / "c2"),
            "disk_snapshot_dir": str(tmp_path / "o2"), "vms": {}}

        assert mgr._cluster_disk_snapshot_dirs() == [str(tmp_path / "o2")]


def _sync_run_parallel(self, tasks, op_label='parallel task', max_workers=None):
    """Run the worker targets in-process, with _run_parallel's contract."""
    results, failures = {}, {}
    for label, target, args in tasks:
        try:
            results[label] = target(*args)
        except Exception as exc:
            failures[label] = f"{type(exc).__name__}: {exc}"
    return results, failures


def _take(mgr: BoxmanManager, monkeypatch) -> None:
    monkeypatch.setattr(BoxmanManager, "_run_parallel", _sync_run_parallel)
    mgr.provider.snapshot_take.return_value = True
    mgr.provider.validate_snapshot.return_value = (True, [])
    mgr.snapshot_take(types.SimpleNamespace(
        snapshot_name="s1", snapshot_descr="", vms="all", cluster=None))


POOL = "bprj__demo__bprj_cluster_1_disk_snapshots"


class TestTake:

    def test_the_overlays_go_to_the_disk_snapshot_dir(self, tmp_path,
                                                      monkeypatch):
        ov = tmp_path / "ov"
        ov.mkdir()
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "disk_snapshot_dir": str(ov)})

        _take(mgr, monkeypatch)

        kwargs = mgr.provider.snapshot_take.call_args.kwargs
        assert kwargs["vm_name"] == VM
        assert kwargs["disk_dir"] == str(ov)

    def test_unset_they_go_beside_the_disks_as_before(self, tmp_path,
                                                       monkeypatch):
        mgr = _manager({"workdir": str(tmp_path / "c1")})

        _take(mgr, monkeypatch)

        assert mgr.provider.snapshot_take.call_args.kwargs["disk_dir"] is None
        mgr.provider.ensure_pool_on.assert_not_called()

    def test_a_missing_one_is_made_0711_whatever_the_umask(self, tmp_path,
                                                           monkeypatch):
        """QEMU, running as another user under qemu:///system, opens the
        overlays there: the directory must be searchable, not listable."""
        ov = tmp_path / "on" / "the" / "way" / "ov"
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "disk_snapshot_dir": str(ov)})
        old = os.umask(0o077)
        try:
            _take(mgr, monkeypatch)
        finally:
            os.umask(old)

        assert stat.S_IMODE(os.stat(ov).st_mode) == 0o711

    def test_the_directories_made_on_the_way_are_searchable(self, tmp_path,
                                                            monkeypatch):
        """QEMU reaches the overlays through every directory above them; one
        made on the way under umask 077 would stop it there."""
        (tmp_path / "on").mkdir(mode=0o750)
        ov = tmp_path / "on" / "the" / "way" / "ov"
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "disk_snapshot_dir": str(ov)})
        old = os.umask(0o077)
        try:
            _take(mgr, monkeypatch)
        finally:
            os.umask(old)

        for made in (ov.parent, ov.parent.parent):
            assert stat.S_IMODE(os.stat(made).st_mode) == 0o711
        # one that was there already is not this take's to change
        assert stat.S_IMODE(os.stat(tmp_path / "on").st_mode) == 0o750

    @staticmethod
    def _take_under_umask_077(tmp_path, monkeypatch, memory, disk):
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": str(memory),
                        "disk_snapshot_dir": str(disk)})
        old = os.umask(0o077)
        try:
            _take(mgr, monkeypatch)
        finally:
            os.umask(old)

    @staticmethod
    def _mode(path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_one_new_directory_for_both_is_made_searchable(self, tmp_path,
                                                           monkeypatch):
        """Made ``0700`` for the memory images first, it would stop QEMU
        before the overlays; ``0711`` lets it search, never list."""
        snaps = tmp_path / "snaps"

        self._take_under_umask_077(tmp_path, monkeypatch, snaps, snaps)

        assert self._mode(snaps) == 0o711

    def test_a_disk_snapshot_dir_in_a_new_memory_snapshot_dir(self, tmp_path,
                                                              monkeypatch):
        there = tmp_path / "there"
        there.mkdir()
        there.chmod(0o700)
        memory = there / "mem"
        overlays = memory / "ov"

        self._take_under_umask_077(tmp_path, monkeypatch, memory, overlays)

        assert self._mode(memory) == 0o711
        assert self._mode(overlays) == 0o711
        # there already: not this take's to change
        assert self._mode(there) == 0o700

    def test_a_memory_snapshot_dir_in_a_new_disk_snapshot_dir(self, tmp_path,
                                                              monkeypatch):
        """Made on the way to the memory images, the disk_snapshot_dir
        still has to be searchable; the memory images' own stays 0700."""
        overlays = tmp_path / "ov"
        memory = overlays / "mem"

        self._take_under_umask_077(tmp_path, monkeypatch, memory, overlays)

        assert self._mode(overlays) == 0o711
        assert self._mode(memory) == 0o700

    def test_one_spelled_through_a_new_memory_snapshot_dir(self, tmp_path,
                                                           monkeypatch):
        """``memory/../disk``: QEMU searches ``memory`` for its ``..``,
        which walking up from ``disk`` never meets."""
        memory = tmp_path / "memory"
        disk = os.path.join(memory, os.pardir, "disk")

        self._take_under_umask_077(tmp_path, monkeypatch, memory, disk)

        assert self._mode(memory) == 0o711
        assert self._mode(tmp_path / "disk") == 0o711

    def test_one_spelled_through_a_symlink_to_it(self, tmp_path, monkeypatch):
        """``link/../disk``, ``link`` leading to the new memory directory:
        resolving it searches what ``link`` points to as well."""
        memory = tmp_path / "memory"
        (tmp_path / "link").symlink_to(memory)
        disk = os.path.join(tmp_path, "link", os.pardir, "disk")

        self._take_under_umask_077(tmp_path, monkeypatch, memory, disk)

        assert self._mode(memory) == 0o711

    def test_an_existing_one_for_both_keeps_its_mode(self, tmp_path,
                                                     monkeypatch):
        """Not this take's to change, as for one set for the overlays
        alone."""
        snaps = tmp_path / "snaps"
        snaps.mkdir(mode=0o700)

        self._take_under_umask_077(tmp_path, monkeypatch, snaps, snaps)

        assert self._mode(snaps) == 0o700

    def test_an_existing_one_keeps_its_mode(self, tmp_path, monkeypatch):
        ov = tmp_path / "ov"
        ov.mkdir()
        os.chmod(ov, 0o750)
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "disk_snapshot_dir": str(ov)})

        _take(mgr, monkeypatch)

        assert stat.S_IMODE(os.stat(ov).st_mode) == 0o750

    def test_a_storage_pool_is_ensured_on_it(self, tmp_path, monkeypatch):
        ov = tmp_path / "ov"
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "disk_snapshot_dir": str(ov)})

        _take(mgr, monkeypatch)

        mgr.provider.ensure_pool_on.assert_called_once_with(str(ov), POOL)

    def test_without_a_pool_the_take_goes_on_and_says_why_it_matters(
            self, tmp_path, monkeypatch):
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "disk_snapshot_dir": str(tmp_path / "ov")})
        mgr.provider.ensure_pool_on.return_value = None

        _take(mgr, monkeypatch)

        mgr.provider.snapshot_take.assert_called_once()
        warned = " ".join(str(c.args[0])
                          for c in mgr.logger.warning.call_args_list)
        assert "no storage pool" in warned and "use_sudo: false" in warned

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_one_that_cannot_be_made_stops_before_any_take(self, tmp_path,
                                                           monkeypatch):
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "disk_snapshot_dir": str(locked / "ov")})
        try:
            with pytest.raises(SnapshotError,
                               match="could not create the snapshot directory"):
                _take(mgr, monkeypatch)
        finally:
            locked.chmod(0o700)

        mgr.provider.snapshot_take.assert_not_called()

    def test_a_container_runtime_refuses_before_any_take(self, tmp_path,
                                                         monkeypatch):
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "disk_snapshot_dir": str(tmp_path / "ov")},
                       runtime="docker-compose")

        with pytest.raises(ConfigError):
            _take(mgr, monkeypatch)

        mgr.provider.snapshot_take.assert_not_called()
        assert not (tmp_path / "ov").exists()
