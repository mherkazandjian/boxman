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

    def test_unset_is_the_workdir_as_the_take_always_wrote_there(
            self, tmp_path):
        """Not normalized: ``link/..`` is the parent of where ``link``
        points, which only the filesystem can tell."""
        (tmp_path / "actual" / "child").mkdir(parents=True)
        os.symlink(tmp_path / "actual" / "child", tmp_path / "link")
        workdir = f"{tmp_path}/link/../c1"
        mgr = _manager({"workdir": workdir})

        assert mgr.cluster_memory_snapshot_dir("cluster_1") == workdir

    def test_set_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": "~/snapshots"})

        assert (mgr.cluster_memory_snapshot_dir("cluster_1")
                == str(tmp_path / "snapshots"))

    def test_a_dotdot_after_a_symlink_is_left_to_the_filesystem(
            self, tmp_path):
        """Folded by its spelling, ``link/../memory`` named the directory
        beside ``link``: the take wrote there, and a teardown removed from
        there, instead of the directory configured."""
        (tmp_path / "actual" / "child").mkdir(parents=True)
        (tmp_path / "actual" / "memory").mkdir()
        (tmp_path / "memory").mkdir()
        os.symlink(tmp_path / "actual" / "child", tmp_path / "link")
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": f"{tmp_path}/link/../memory"})

        assert os.path.samefile(mgr.cluster_memory_snapshot_dir("cluster_1"),
                                tmp_path / "actual" / "memory")

    def test_a_relative_one_is_refused(self, tmp_path):
        """Relative to wherever boxman runs, a teardown run from elsewhere
        would look in another directory than the take wrote to."""
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": "snaps"})

        with pytest.raises(ConfigError, match="memory_snapshot_dir must be an "
                                              "absolute path"):
            mgr.cluster_memory_snapshot_dir("cluster_1")

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

    @pytest.mark.parametrize("where", ["inside", "equal"])
    def test_one_in_a_missing_workdir_does_not_make_the_workdir(
            self, tmp_path, monkeypatch, where):
        workdir = tmp_path / "c1"
        snaps = workdir / "snaps" if where == "inside" else workdir
        mgr = _manager({"workdir": str(workdir),
                        "memory_snapshot_dir": str(snaps)})

        with pytest.raises(SnapshotError, match=f"would also create {workdir}, "
                                                f"the workdir of cluster "
                                                f"cluster_1"):
            _take(mgr, monkeypatch)

        assert not workdir.exists()
        mgr.provider.snapshot_take.assert_not_called()

    def test_one_reaching_a_missing_workdir_by_another_name_does_not_make_it(
            self, tmp_path, monkeypatch):
        real = tmp_path / "real"
        real.mkdir()
        os.symlink(real, tmp_path / "alias")
        mgr = _manager({"workdir": str(tmp_path / "alias" / "c1"),
                        "memory_snapshot_dir": str(real / "c1" / "snaps")})

        with pytest.raises(SnapshotError, match="would also create"):
            _take(mgr, monkeypatch)

        assert not (real / "c1").exists()

    def test_one_reaching_a_missing_workdir_through_a_dangling_symlink(
            self, tmp_path, monkeypatch):
        """``alias -> target``, with no target yet: making
        ``target/c1/snaps`` makes the workdir ``alias/c1`` too."""
        os.symlink(tmp_path / "target", tmp_path / "alias")
        mgr = _manager({"workdir": str(tmp_path / "alias" / "c1"),
                        "memory_snapshot_dir": str(tmp_path / "target" / "c1"
                                                   / "snaps")})

        with pytest.raises(SnapshotError, match="would also create"):
            _take(mgr, monkeypatch)

        assert not (tmp_path / "target").exists()

    def test_one_reaching_a_missing_workdir_by_a_name_no_path_resolves(
            self, tmp_path, monkeypatch):
        """As a bind mount is: one directory under two names whose resolved
        paths differ — here, with ``realpath`` resolving nothing."""
        real = tmp_path / "real"
        real.mkdir()
        os.symlink(real, tmp_path / "bind")
        monkeypatch.setattr(os.path, "realpath",
                            lambda path, strict=False: os.fspath(path))
        mgr = _manager({"workdir": str(tmp_path / "bind" / "c1"),
                        "memory_snapshot_dir": str(real / "c1" / "snaps")})

        with pytest.raises(SnapshotError, match="would also create"):
            _take(mgr, monkeypatch)

        assert not (real / "c1").exists()

    def test_an_existing_file_is_refused_before_any_snapshot(
            self, tmp_path, monkeypatch):
        """Not a directory: the take would fail at the VM, after the
        docker-compose clusters were snapshotted."""
        snaps = tmp_path / "snaps"
        snaps.write_bytes(b"not a directory")
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": str(snaps)})
        session = self._with_a_compose_cluster(mgr, tmp_path)

        with pytest.raises(SnapshotError, match="is not a directory"):
            _take(mgr, monkeypatch)

        session.snapshot_take_cluster.assert_not_called()
        mgr.provider.snapshot_take.assert_not_called()

    def test_another_clusters_missing_workdir_is_not_made_either(
            self, tmp_path, monkeypatch):
        (tmp_path / "c1").mkdir()
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": str(tmp_path / "c2" / "snaps")})
        mgr.config["clusters"]["cluster_2"] = {
            "workdir": str(tmp_path / "c2"), "vms": {}}

        with pytest.raises(SnapshotError,
                           match="the workdir of cluster cluster_2"):
            _take(mgr, monkeypatch)

        assert not (tmp_path / "c2").exists()

    @staticmethod
    def _with_a_compose_cluster(mgr, tmp_path):
        mgr.config["clusters"]["web"] = {
            "provider": "docker-compose", "workdir": str(tmp_path / "web"),
            "boxes": {}}
        session = MagicMock()
        session.snapshot_take_cluster.return_value = True
        mgr.session_for_cluster = MagicMock(return_value=session)
        return session

    def test_a_compose_only_project_is_refused_before_any_snapshot(
            self, tmp_path, monkeypatch):
        """It has no VM to take, and returned before the check."""
        mgr = _manager({})
        mgr.config["clusters"] = {}
        session = self._with_a_compose_cluster(mgr, tmp_path)
        mgr.config["clusters"]["web"]["memory_snapshot_dir"] = str(
            tmp_path / "snaps")

        with pytest.raises(ConfigError, match="docker-compose cluster takes "
                                              "no memory_snapshot_dir"):
            _take(mgr, monkeypatch)

        session.snapshot_take_cluster.assert_not_called()

    def test_a_mixed_project_is_refused_before_its_containers_are_snapshotted(
            self, tmp_path, monkeypatch):
        mgr = _manager({"workdir": str(tmp_path / "c1"),
                        "memory_snapshot_dir": "snaps"})
        session = self._with_a_compose_cluster(mgr, tmp_path)

        with pytest.raises(ConfigError, match="must be an absolute path"):
            _take(mgr, monkeypatch)

        session.snapshot_take_cluster.assert_not_called()
        mgr.provider.snapshot_take.assert_not_called()

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

    @pytest.mark.parametrize("how", ["symlink", "hard link"])
    def test_one_file_under_two_names_is_counted_once(self, tmp_path,
                                                      monkeypatch, how):
        """The memory_snapshot_dir another name of the workdir, or the image
        linked into it: one file, whatever its names."""
        workdir = tmp_path / "c1"
        workdir.mkdir()
        image = workdir / f"{VM}_snapshot_s1.raw"
        image.write_bytes(b"x" * 1024)
        snaps = tmp_path / "snaps"
        if how == "symlink":
            os.symlink(workdir, snaps)
        else:
            snaps.mkdir()
            os.link(image, snaps / image.name)
        mgr = _manager({"workdir": str(workdir),
                        "memory_snapshot_dir": str(snaps)})

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
