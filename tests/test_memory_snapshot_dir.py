"""
A cluster's ``memory_snapshot_dir``: where ``snapshot take`` writes its VMs'
memory images, instead of the cluster workdir.

Only the memory images move there. A disk snapshot is an overlay libvirt
names after its disk and creates beside it, and the newest overlay is the
disk the guest writes to. The teardown side (memory images removed from a
memory_snapshot_dir, nothing else there) is in test_vm_teardown_storage.py.
"""

from __future__ import annotations

import os
import stat
import types
from unittest.mock import MagicMock

import pytest

from boxman.exceptions import ConfigError, SnapshotError
from boxman.manager import BoxmanManager
from boxman.providers.libvirt.storage import StorageManager

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
    mgr.logger = MagicMock()
    mgr.cache = MagicMock()
    mgr.cache.projects = {}
    if runtime is not None:
        mgr._runtime_name = runtime
    return mgr


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


class TestTheDirectory:

    def test_unset_is_the_workdir(self, tmp_path):
        mgr = _manager({"workdir": str(tmp_path / "c1")})

        assert mgr.cluster_memory_snapshot_dir("cluster_1") == str(tmp_path / "c1")

    def test_set_is_expanded_and_absolute(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": "~/snaps/../snapshots"})

        assert (mgr.cluster_memory_snapshot_dir("cluster_1")
                == str(tmp_path / "snapshots"))

    @pytest.mark.parametrize("value", ["", "   ", 7, ["/data"], {"p": 1}],
                             ids=["empty", "blank", "int", "list", "dict"])
    def test_a_value_that_is_not_a_path_is_refused(self, tmp_path, value):
        mgr = _manager({"workdir": str(tmp_path), "memory_snapshot_dir": value})

        with pytest.raises(ConfigError,
                           match="clusters.cluster_1.memory_snapshot_dir must be"):
            mgr.cluster_memory_snapshot_dir("cluster_1")

    def test_a_container_runtime_refuses_it(self, tmp_path):
        """The libvirt container sees the workspace and the workdirs, which
        are bind-mounted into it, and not this directory."""
        mgr = _manager({"workdir": str(tmp_path),
                        "memory_snapshot_dir": str(tmp_path / "s")},
                       runtime="docker-compose")

        with pytest.raises(ConfigError, match="'docker-compose' runtime"):
            mgr.cluster_memory_snapshot_dir("cluster_1")

    def test_a_container_runtime_without_it_keeps_the_workdir(self, tmp_path):
        mgr = _manager({"workdir": str(tmp_path)}, runtime="docker-compose")

        assert mgr.cluster_memory_snapshot_dir("cluster_1") == str(tmp_path)

    def test_a_docker_compose_cluster_refuses_it(self, tmp_path):
        mgr = _manager({"workdir": str(tmp_path),
                        "memory_snapshot_dir": str(tmp_path / "s")})
        mgr.provider_type_for_cluster = MagicMock(return_value="docker-compose")

        with pytest.raises(ConfigError, match="docker-compose cluster"):
            mgr.cluster_memory_snapshot_dir("cluster_1")

    def test_validate_checks_every_cluster_up_front(self, tmp_path):
        mgr = _manager({"workdir": str(tmp_path)})
        mgr.config["clusters"]["cluster_2"] = {
            "workdir": str(tmp_path / "c2"), "memory_snapshot_dir": 0, "vms": {}}

        with pytest.raises(ConfigError, match="cluster_2.memory_snapshot_dir"):
            mgr.validate_memory_snapshot_dirs()

    def test_validate_needs_no_workdir_of_a_cluster_that_sets_none(self):
        """A cluster with no workdir and no workspace.path to derive one
        from is warned about, not refused; checking this key must not
        crash on it."""
        mgr = _manager({})

        mgr.validate_memory_snapshot_dirs()

        assert mgr._cluster_memory_snapshot_dirs() == []

    def test_only_the_clusters_that_set_one_are_listed(self, tmp_path):
        mgr = _manager({"workdir": str(tmp_path / "c1")})
        mgr.config["clusters"]["cluster_2"] = {
            "workdir": str(tmp_path / "c2"),
            "memory_snapshot_dir": str(tmp_path / "s2"), "vms": {}}

        assert mgr._cluster_memory_snapshot_dirs() == [str(tmp_path / "s2")]


class TestTake:

    def test_the_memory_image_goes_to_the_memory_snapshot_dir(self, tmp_path,
                                                       monkeypatch):
        snaps = tmp_path / "snaps"
        snaps.mkdir()
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": str(snaps)})

        _take(mgr, monkeypatch)

        kwargs = mgr.provider.snapshot_take.call_args.kwargs
        assert kwargs["vm_name"] == VM
        assert kwargs["vm_dir"] == str(snaps)

    def test_unset_it_goes_to_the_workdir_as_before(self, tmp_path,
                                                    monkeypatch):
        workdir = tmp_path / "c1"
        workdir.mkdir()
        mgr = _manager({"workdir": str(workdir)})

        _take(mgr, monkeypatch)

        assert mgr.provider.snapshot_take.call_args.kwargs["vm_dir"] == str(
            workdir)

    def test_a_missing_memory_snapshot_dir_is_made_0700(self, tmp_path, monkeypatch):
        snaps = tmp_path / "on" / "the" / "way" / "snaps"
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": str(snaps)})

        _take(mgr, monkeypatch)

        assert snaps.is_dir()
        assert stat.S_IMODE(os.stat(snaps).st_mode) == 0o700

    def test_an_existing_memory_snapshot_dir_keeps_its_mode(self, tmp_path,
                                                     monkeypatch):
        snaps = tmp_path / "snaps"
        snaps.mkdir(mode=0o750)
        os.chmod(snaps, 0o750)
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": str(snaps)})

        _take(mgr, monkeypatch)

        assert stat.S_IMODE(os.stat(snaps).st_mode) == 0o750

    def test_a_missing_workdir_is_not_made(self, tmp_path, monkeypatch):
        """Only a memory_snapshot_dir is made on first use: a workdir that is not
        there is not this command's to create."""
        workdir = tmp_path / "c1"
        mgr = _manager({"workdir": str(workdir)})

        _take(mgr, monkeypatch)

        assert not workdir.exists()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
    def test_a_memory_snapshot_dir_that_cannot_be_made_stops_before_any_take(
            self, tmp_path, monkeypatch):
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": str(locked / "snaps")})
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
                        "memory_snapshot_dir": str(tmp_path / "snaps")},
                       runtime="docker-compose")

        with pytest.raises(ConfigError):
            _take(mgr, monkeypatch)

        mgr.provider.snapshot_take.assert_not_called()
        assert not (tmp_path / "snaps").exists()


class TestStorageDf:
    """``storage df`` counts a VM's memory images in its memory_snapshot_dir and in
    its workdir, where those taken before a memory_snapshot_dir was set stay."""

    @staticmethod
    def _df(mgr, monkeypatch, workdir):
        disk = workdir / f"{VM}.qcow2"
        disk.write_bytes(b"d")
        monkeypatch.setattr(
            "boxman.providers.libvirt.storage.vm_disk_paths",
            lambda wd, vm, info: [str(disk)])
        storage = mgr.provider.storage
        storage.count_snapshots.return_value = 1
        storage.disk_info.return_value = {"actual-size": 1, "virtual-size": 1}
        storage.disk_chain.return_value = [str(disk)]
        storage.disk_measure.return_value = {"required": 1}
        storage.snapshot_memory_files.side_effect = (
            lambda directory, vm: StorageManager.snapshot_memory_files(
                None, directory, vm))
        mgr.storage_df(types.SimpleNamespace(vms="all", cluster=None))
        lines = [str(c.args[0]) for c in mgr.logger.info.call_args_list]
        return next(line for line in lines if line.startswith(VM))

    def test_both_directories_are_counted(self, tmp_path, monkeypatch):
        workdir, snaps = tmp_path / "c1", tmp_path / "snaps"
        workdir.mkdir()
        snaps.mkdir()
        (workdir / f"{VM}_snapshot_old.raw.zst").write_bytes(b"x" * 1024)
        (snaps / f"{VM}_snapshot_new.raw").write_bytes(b"x" * 2048)
        mgr = _manager({"workdir": str(workdir), "memory_snapshot_dir": str(snaps)})

        line = self._df(mgr, monkeypatch, workdir)

        assert "3.0K" in line

    def test_one_directory_is_counted_once(self, tmp_path, monkeypatch):
        workdir = tmp_path / "c1"
        workdir.mkdir()
        (workdir / f"{VM}_snapshot_s1.raw").write_bytes(b"x" * 1024)
        mgr = _manager({"workdir": str(workdir)})

        line = self._df(mgr, monkeypatch, workdir)

        assert "1.0K" in line and "2.0K" not in line


class TestCheckedUpFront:
    """``provision`` checks every cluster's memory_snapshot_dir before it
    builds anything. ``up`` on a project that is already there, and
    ``update``, never reach those checks, so they make the same one first:
    a directory the libvirt container would not see, here."""

    @staticmethod
    def _refused(tmp_path):
        return _manager({"workdir": str(tmp_path),
                         "memory_snapshot_dir": str(tmp_path / "snaps")},
                        runtime="docker-compose")

    def test_up_on_an_existing_project_refuses_it_before_reading_a_vm(
            self, tmp_path, monkeypatch):
        mgr = self._refused(tmp_path)
        states = MagicMock(return_value={VM: "shut off"})
        monkeypatch.setattr(BoxmanManager, "_get_vm_states",
                            lambda _self: states())

        with pytest.raises(ConfigError, match="memory_snapshot_dir"):
            mgr.up(types.SimpleNamespace(force=False, vms="all", cluster=None))

        states.assert_not_called()

    def test_update_refuses_it(self, tmp_path):
        mgr = self._refused(tmp_path)

        with pytest.raises(ConfigError, match="memory_snapshot_dir"):
            mgr._update(types.SimpleNamespace(dry_run=True, yes=False,
                                              restart=False))
