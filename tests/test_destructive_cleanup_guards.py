"""
Guardrails on the paths that can delete live state.

Covers the Part 1 findings of the review tracked in issue #164:

- **FB-1** the docker-runtime workdir pass must never sweep a live workdir.
- **X1**   ``destroy``'s recursive delete must validate its target, and must
           validate it *before* teardown starts.
- **FB-4** a VM's disks may only be removed once the domain is positively
           confirmed gone — never on an observation that failed.
- **X2**   a teardown that left resources behind must not go on to delete the
           workspace, the generated files or the cache entry.
- **FBN-6** a refused network removal must be reported, not logged as success.

The common thread: every one of these deletes something unrecoverable, so the
decision to delete has to rest on positive evidence, and a failure to observe
has to read as "stop", never as "nothing there".
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from boxman.exceptions import ProvisionError
from boxman.manager import BoxmanManager
from boxman.providers.libvirt.disk_cleanup import FilesInUse, StorageOutcome
from boxman.utils import retained_tree

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# X1 — the delete-root validator
# --------------------------------------------------------------------------
class TestSafeDeleteTarget:
    """``workspace.path`` is user-supplied configuration that ends up in an
    ``rm -rf``, so a typo like ``/`` or ``~`` must be refused."""

    def test_rejects_an_empty_path(self):
        # realpath('') is the current directory — this has to be caught
        # before the path is resolved
        with pytest.raises(ProvisionError, match="empty or not absolute"):
            BoxmanManager._safe_delete_target("")

    def test_rejects_a_relative_path(self):
        # a relative path would silently become an absolute deletion target
        with pytest.raises(ProvisionError, match="empty or not absolute"):
            BoxmanManager._safe_delete_target("workspaces/demo")

    def test_rejects_a_leaf_symlink(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        with pytest.raises(ProvisionError, match="symlink"):
            BoxmanManager._safe_delete_target(str(link))

    def test_rejects_the_filesystem_root(self):
        with pytest.raises(ProvisionError):
            BoxmanManager._safe_delete_target("/")

    def test_rejects_a_top_level_path(self):
        with pytest.raises(ProvisionError):
            BoxmanManager._safe_delete_target("/srv")

    def test_rejects_the_home_directory(self, tmp_path, monkeypatch):
        home = tmp_path / "home" / "someone"
        home.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        with pytest.raises(ProvisionError, match="home directory"):
            BoxmanManager._safe_delete_target(str(home))

    def test_rejects_a_home_reached_through_a_symlink(self, tmp_path,
                                                     monkeypatch):
        """Comparing against an unresolved ``$HOME`` would miss this: the
        canonical target is what has to be compared."""
        real_home = tmp_path / "real_home"
        real_home.mkdir()
        linked_home = tmp_path / "linked_home"
        linked_home.symlink_to(real_home)
        monkeypatch.setenv("HOME", str(linked_home))
        with pytest.raises(ProvisionError, match="home directory"):
            BoxmanManager._safe_delete_target(str(real_home))

    def test_rejects_a_repo_root_marked_by_a_git_file(self, tmp_path):
        """A linked worktree carries a ``.git`` *file*, not a directory."""
        repo = tmp_path / "worktree"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
        with pytest.raises(ProvisionError, match="repository root"):
            BoxmanManager._safe_delete_target(str(repo))

    def test_rejects_a_mount_point(self, tmp_path, monkeypatch):
        mounted = tmp_path / "mounted"
        mounted.mkdir()
        canonical = os.path.realpath(str(mounted))
        monkeypatch.setattr(
            "boxman.manager_parts.flows.os.path.ismount",
            lambda p: p == canonical)
        with pytest.raises(ProvisionError, match="mount point"):
            BoxmanManager._safe_delete_target(str(mounted))

    def test_accepts_a_workspace_and_returns_the_canonical_target(self,
                                                                  tmp_path):
        """What is validated is what gets deleted."""
        workspace = tmp_path / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        assert (BoxmanManager._safe_delete_target(str(workspace))
                == os.path.realpath(str(workspace)))


class TestForceRmtree:

    def test_a_rejected_path_reaches_neither_rmtree_nor_docker(self,
                                                               monkeypatch):
        calls = []
        monkeypatch.setattr("boxman.manager_parts.flows.shutil.rmtree",
                            lambda *a, **k: calls.append("rmtree"))
        monkeypatch.setattr("boxman.manager_parts.flows.subprocess.run",
                            lambda *a, **k: calls.append("docker"))
        with pytest.raises(ProvisionError):
            BoxmanManager._force_rmtree("/")
        assert calls == []

    @pytest.mark.skipif(os.geteuid() == 0,
                        reason="root searches a mode-000 directory")
    def test_a_directory_that_cannot_be_looked_up_is_not_taken_for_gone(
            self, tmp_path, monkeypatch):
        """#221 review R4: only "no such file" is absence."""
        workspace = tmp_path / "workspaces" / "demo"
        (workspace / "c1").mkdir(parents=True)
        monkeypatch.setattr("boxman.manager_parts.flows.subprocess.run",
                            lambda *a, **k: pytest.fail("docker was run"))
        (tmp_path / "workspaces").chmod(0)
        try:
            with pytest.raises(ProvisionError, match="could not look up"):
                BoxmanManager._force_rmtree(str(workspace))
        finally:
            (tmp_path / "workspaces").chmod(0o755)
        assert (workspace / "c1").is_dir()

    def test_raises_when_the_directory_survives_both_attempts(self, tmp_path,
                                                              monkeypatch):
        """A cleanup that silently failed used to be a warning, so the caller
        went on to unregister the project over a directory that is still
        there."""
        workspace = tmp_path / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        monkeypatch.setattr("boxman.manager_parts.flows.shutil.rmtree",
                            lambda *a, **k: None)
        monkeypatch.setattr(
            "boxman.manager_parts.flows.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0))
        with pytest.raises(ProvisionError, match="could not remove"):
            BoxmanManager._force_rmtree(str(workspace))


# --------------------------------------------------------------------------
# FB-4 — disks are removed only on a confirmed undefine
# --------------------------------------------------------------------------
class _TeardownGate:
    """Shared set-up: the storage inventory and the removal are mocked, so
    only the gate between them is under test."""

    @pytest.fixture(autouse=True)
    def _removal(self, monkeypatch):
        self.remove = MagicMock(return_value=StorageOutcome())
        monkeypatch.setattr("boxman.manager_parts.vms.remove_vm_storage",
                            self.remove)

    def _base_manager(self):
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {"project": "demo"}
        mgr.logger = MagicMock()
        self.inventory = SimpleNamespace(saved_at=None, locator_at=None)
        mgr._capture_vm_storage = MagicMock(return_value=self.inventory)
        return mgr


class TestDestroyVmAndDisksGate(_TeardownGate):

    FULL_NAME = "bprj__demo__bprj_cluster_1_node01"

    def _manager(self):
        mgr = self._base_manager()
        session = MagicMock()
        mgr.session_for_cluster = MagicMock(return_value=session)
        return mgr, session

    def _run(self, mgr):
        mgr._destroy_vm_and_disks(
            "cluster_1", {"workdir": "/ws/c1"}, "node01", {"disks": []})

    def test_disks_are_removed_once_absence_is_confirmed(self):
        mgr, session = self._manager()
        session.confirm_vm_absent.return_value = True
        self._run(mgr)
        self.remove.assert_called_once()
        assert self.remove.call_args.args[0] is self.inventory

    def test_a_forced_undefine_is_tried_before_giving_up(self):
        mgr, session = self._manager()
        session.confirm_vm_absent.side_effect = [False, True]
        self._run(mgr)
        assert session.destroy_vm.call_args_list == [
            call(self.FULL_NAME),
            call(self.FULL_NAME, force=True),
        ]
        self.remove.assert_called_once()

    def test_unconfirmed_absence_preserves_the_disks(self):
        """The libvirt-outage case. ``destroy_vm`` reports success because
        its own probe reads a failed query as "not defined", so the disks
        would have been unlinked out from under a live guest."""
        mgr, session = self._manager()
        session.destroy_vm.return_value = True      # optimistic, and wrong
        session.confirm_vm_absent.return_value = False
        with pytest.raises(ProvisionError, match="could not confirm"):
            self._run(mgr)
        self.remove.assert_not_called()

    def test_storage_is_inventoried_before_the_undefine(self):
        mgr, session = self._manager()
        session.confirm_vm_absent.return_value = True
        parent = MagicMock()
        parent.attach_mock(mgr._capture_vm_storage, "capture")
        parent.attach_mock(session.destroy_vm, "destroy_vm")
        self._run(mgr)
        assert [c[0] for c in parent.mock_calls][:2] == [
            "capture", "destroy_vm"]


class TestDestroyRemovedVmGate(_TeardownGate):

    def _manager(self):
        mgr = self._base_manager()
        mgr.provider = MagicMock()
        return mgr

    def test_unconfirmed_absence_preserves_the_disks(self):
        mgr = self._manager()
        mgr.provider.destroy_vm.return_value = True
        mgr.provider.confirm_vm_absent.return_value = False
        with pytest.raises(ProvisionError, match="could not confirm"):
            mgr._destroy_removed_vm("bprj__demo__bprj_cluster_1_old01")
        self.remove.assert_not_called()

    def test_storage_is_inventoried_before_the_undefine(self):
        """``domblklist`` and the ownership record are gone with the
        domain."""
        mgr = self._manager()
        mgr.provider.confirm_vm_absent.return_value = True
        parent = MagicMock()
        parent.attach_mock(mgr._capture_vm_storage, "capture")
        parent.attach_mock(mgr.provider.destroy_vm, "destroy_vm")
        mgr._destroy_removed_vm("bprj__demo__bprj_cluster_1_old01")
        assert [c[0] for c in parent.mock_calls][:2] == [
            "capture", "destroy_vm"]
        self.remove.assert_called_once()


# --------------------------------------------------------------------------
# FBN-6 — a refused network removal is a failure, not a success
# --------------------------------------------------------------------------
class TestDestroyNetworksWorker:

    NET = "bprj__demo__bprj_cluster_1_net1"

    def _manager(self, tmp_path, removed):
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {
            "project": "demo",
            "clusters": {
                "cluster_1": {
                    "workdir": str(tmp_path),
                    "vms": {"node01": {}},
                    "networks": {"net1": {}},
                },
            },
        }
        mgr.logger = MagicMock()
        session = MagicMock()
        session.remove_network.return_value = removed
        mgr.session_for_cluster = MagicMock(return_value=session)
        mgr.full_network_name = MagicMock(return_value=self.NET)

        def _synchronous(tasks, op_label=''):
            """Run each task in-process, mirroring _run_parallel's contract."""
            results, failures = {}, {}
            for label, target, args in tasks:
                try:
                    results[label] = target(*args)
                except Exception as exc:
                    failures[label] = str(exc)
            return results, failures

        mgr._run_parallel = _synchronous
        return mgr

    def test_a_refused_removal_is_reported_and_keeps_its_xml(self, tmp_path):
        xml = tmp_path / f"{self.NET}_net_define.xml"
        xml.write_text("<network/>")
        mgr = self._manager(tmp_path, removed=False)

        failures = mgr.destroy_networks()

        assert failures, "a refused removal must surface as a failure"
        assert xml.exists(), "the XML must survive so the teardown can retry"
        logged = " ".join(str(c) for c in mgr.logger.info.call_args_list)
        assert "removed network" not in logged, "logged a removal that failed"

    def test_a_successful_removal_drops_the_xml(self, tmp_path):
        xml = tmp_path / f"{self.NET}_net_define.xml"
        xml.write_text("<network/>")
        mgr = self._manager(tmp_path, removed=True)

        assert mgr.destroy_networks() == {}
        assert not xml.exists()


# --------------------------------------------------------------------------
# X2 — destroy only deletes irreversible state on a confirmed teardown
# --------------------------------------------------------------------------
def _destroy_manager(workspace):
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = {
        "project": "demo",
        "workspace": {"path": str(workspace)},
        "clusters": {
            "cluster_1": {
                "workdir": str(workspace / "cluster_1"),
                "vms": {"node01": {}},
            },
        },
    }
    mgr.config_path = "conf.yml"
    mgr.provider = MagicMock()
    mgr.logger = MagicMock()
    mgr.cache = MagicMock()
    mgr.cache.projects = {"demo": {}}
    mgr._runtime_instance = MagicMock()   # not a DockerComposeRuntime
    return mgr


ARGS = SimpleNamespace(auto_accept=True, templates=False)


class TestDestroyPreflight:

    def test_a_symlinked_workspace_aborts_before_any_teardown(self, tmp_path):
        """Discovering a bad delete target after the VMs are gone is the
        failure the preflight exists to prevent."""
        real = tmp_path / "real_ws"
        real.mkdir()
        link = tmp_path / "ws_link"
        link.symlink_to(real)

        mgr = _destroy_manager(link)
        mgr.deprovision = MagicMock()
        mgr.destroy_compose_clusters = MagicMock()

        with pytest.raises(ProvisionError, match="symlink"):
            mgr.destroy(ARGS)

        mgr.deprovision.assert_not_called()
        mgr._runtime_instance.ensure_ready.assert_not_called()


class TestDestroyTeardownGate:

    def _manager(self, tmp_path):
        workspace = tmp_path / "workspaces" / "demo"
        workspace.mkdir(parents=True)
        mgr = _destroy_manager(workspace)
        mgr.deprovision = MagicMock()
        mgr.destroy_compose_clusters = MagicMock()
        mgr.deprovision_files = MagicMock()
        mgr.unregister_from_cache = MagicMock()
        mgr._force_rmtree = MagicMock()
        # step 5, the workspace's removal: with a libvirt project it goes
        # through the in-use scan's removal, not _force_rmtree (#221)
        mgr._remove_workspace = MagicMock()
        mgr._confirm_project_torn_down = MagicMock(return_value=(True, ''))
        return mgr, workspace

    def test_a_clean_teardown_removes_everything_and_unregisters_last(
            self, tmp_path):
        mgr, workspace = self._manager(tmp_path)
        order = MagicMock()
        order.attach_mock(mgr.deprovision_files, "deprovision_files")
        order.attach_mock(mgr._remove_workspace, "remove_workspace")
        order.attach_mock(mgr.unregister_from_cache, "unregister")

        mgr.destroy(ARGS)

        assert mgr._remove_workspace.call_args.args[0] == str(workspace)
        mgr.deprovision_files.assert_called_once()
        mgr.unregister_from_cache.assert_called_once()
        # Order matters: unregistering last is what keeps a failed deletion
        # recoverable, so assert it rather than just the call counts.
        assert [c[0] for c in order.mock_calls] == [
            "deprovision_files", "remove_workspace", "unregister"]

    def test_stale_teardown_locators_go_once_the_workspace_has(
            self, tmp_path):
        """A kept file's inventory goes with the workspace; its locator is
        retired after that, before the project is forgotten (#208)."""
        mgr, workspace = self._manager(tmp_path)
        mgr._retire_stale_teardown_locators = MagicMock()
        order = MagicMock()
        order.attach_mock(mgr._remove_workspace, "remove_workspace")
        order.attach_mock(mgr._retire_stale_teardown_locators, "retire")
        order.attach_mock(mgr.unregister_from_cache, "unregister")

        mgr.destroy(ARGS)

        assert [c[0] for c in order.mock_calls] == [
            "remove_workspace", "retire", "unregister"]

    def test_a_failed_deprovision_preserves_everything(self, tmp_path):
        mgr, _workspace = self._manager(tmp_path)
        mgr._retire_stale_teardown_locators = MagicMock()
        mgr.deprovision.side_effect = ProvisionError(
            "deprovision did not complete — VMs are still defined: node01")

        with pytest.raises(ProvisionError, match="destroy did not complete"):
            mgr.destroy(ARGS)

        mgr._force_rmtree.assert_not_called()
        mgr._remove_workspace.assert_not_called()
        mgr.deprovision_files.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()
        mgr._retire_stale_teardown_locators.assert_not_called()

    def test_a_failed_compose_destroy_preserves_everything(self, tmp_path):
        """``destroy_compose_clusters`` (down --volumes) is a different
        operation from the one deprovision ran, so it needs its own gate."""
        mgr, _workspace = self._manager(tmp_path)
        mgr.destroy_compose_clusters.side_effect = RuntimeError("volumes busy")

        with pytest.raises(ProvisionError, match="docker-compose teardown"):
            mgr.destroy(ARGS)

        mgr._force_rmtree.assert_not_called()
        mgr._remove_workspace.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    def test_an_unanswerable_survivor_query_preserves_everything(self,
                                                                 tmp_path):
        mgr, _workspace = self._manager(tmp_path)
        mgr._confirm_project_torn_down = MagicMock(
            return_value=(False, "could not query libvirt to confirm the "
                                 "teardown completed"))

        with pytest.raises(ProvisionError, match="could not query libvirt"):
            mgr.destroy(ARGS)

        mgr._force_rmtree.assert_not_called()
        mgr._remove_workspace.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    def test_survivors_preserve_everything(self, tmp_path):
        mgr, _workspace = self._manager(tmp_path)
        mgr._confirm_project_torn_down = MagicMock(
            return_value=(False, "VMs are still defined: node01"))

        with pytest.raises(ProvisionError, match="still defined"):
            mgr.destroy(ARGS)

        mgr._force_rmtree.assert_not_called()
        mgr._remove_workspace.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    def test_a_failed_workspace_removal_keeps_the_project_registered(
            self, tmp_path):
        """Unregistering last is what makes a failed deletion recoverable:
        the leftovers stay visible to ``boxman list``."""
        mgr, workspace = self._manager(tmp_path)
        mgr._remove_workspace.side_effect = ProvisionError(
            f"could not remove {workspace}")

        with pytest.raises(ProvisionError, match="could not remove"):
            mgr.destroy(ARGS)

        mgr.unregister_from_cache.assert_not_called()

    def test_an_empty_exception_message_still_blocks_cleanup(self, tmp_path):
        """The failure flag must not be derived from the message: an
        exception raised with an empty one would otherwise read as success."""
        mgr, _workspace = self._manager(tmp_path)
        mgr.destroy_compose_clusters.side_effect = RuntimeError()

        with pytest.raises(ProvisionError, match="destroy did not complete"):
            mgr.destroy(ARGS)

        mgr._force_rmtree.assert_not_called()
        mgr._remove_workspace.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    def test_template_dirs_are_preserved_when_teardown_failed(self, tmp_path):
        """``--templates`` wipes shared template workdirs, which other
        projects clone from — they must survive an incomplete teardown."""
        mgr, _workspace = self._manager(tmp_path)
        templates = tmp_path / "boxman-templates"
        templates.mkdir()
        mgr.config["templates"] = {"tpl1": {"workdir": str(templates)}}
        mgr._confirm_project_torn_down = MagicMock(
            return_value=(False, "VMs are still defined: node01"))

        with pytest.raises(ProvisionError):
            mgr.destroy(SimpleNamespace(auto_accept=True, templates=True))

        mgr._force_rmtree.assert_not_called()
        mgr._remove_workspace.assert_not_called()
        assert templates.is_dir()

    def test_a_failed_runtime_teardown_preserves_everything(self, tmp_path):
        """The docker branch: a runtime whose compose teardown failed still
        holds the libvirt state, so nothing may be deleted over it."""
        from boxman.runtime.docker_compose import DockerComposeRuntime

        mgr, _workspace = self._manager(tmp_path)
        runtime = MagicMock(spec=DockerComposeRuntime)
        runtime.name = "docker-compose"
        runtime.ready_timeout = 60
        runtime.plan_destroy_runtime.return_value = {
            "actions": ["tear down docker-compose environment"],
            "commands": ["docker compose down --volumes"],
            "paths_to_delete": [],
            "container_running": True,
        }
        runtime.destroy_runtime.side_effect = ProvisionError(
            "docker compose down --volumes --remove-orphans failed "
            "(network in use)")
        mgr._runtime_instance = runtime

        with pytest.raises(ProvisionError, match="runtime teardown failed"):
            mgr.destroy(ARGS)

        mgr.deprovision_files.assert_not_called()
        mgr._force_rmtree.assert_not_called()
        mgr._remove_workspace.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    def test_an_unavailable_runtime_preserves_everything(self, tmp_path):
        """If the VMs were never torn down because the runtime would not
        start, nothing may be deleted."""
        mgr, _workspace = self._manager(tmp_path)
        mgr._runtime_instance.ensure_ready.side_effect = RuntimeError(
            "docker daemon unreachable")

        with pytest.raises(ProvisionError, match="never torn down"):
            mgr.destroy(ARGS)

        mgr.deprovision.assert_not_called()
        mgr._force_rmtree.assert_not_called()
        mgr._remove_workspace.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()


# --------------------------------------------------------------------------
# FB-1 — the CLI startup pass never sweeps a workdir
# --------------------------------------------------------------------------
class TestPrepareRuntimeWorkdirs:

    def test_startup_never_sweeps_the_workdir_contents(self):
        """Every docker-runtime command runs this over every workdir,
        including ones holding live VM disks."""
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.logger = MagicMock()
        mgr._ensure_writable_dir = MagicMock()

        mgr.prepare_runtime_workdirs(["/ws/demo", "/ws/demo/cluster_1"])

        assert mgr._ensure_writable_dir.call_args_list == [
            call("/ws/demo", sweep_foreign=False),
            call("/ws/demo/cluster_1", sweep_foreign=False),
        ]

    def test_a_failure_to_prepare_one_workdir_is_not_fatal(self):
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.logger = MagicMock()
        mgr._ensure_writable_dir = MagicMock(
            side_effect=[PermissionError("nope"), None])

        mgr.prepare_runtime_workdirs(["/ws/a", "/ws/b"])

        assert mgr._ensure_writable_dir.call_count == 2
        mgr.logger.warning.assert_called()


# --------------------------------------------------------------------------
# #221 — destroy spares the files another domain uses
# --------------------------------------------------------------------------
unless_root = pytest.mark.skipif(os.geteuid() == 0,
                                 reason="root ignores a directory's mode")


def _tree(root, *names):
    """*root* holding a small file at each of *names*."""
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
    return root


def _left(root):
    """Every path under *root*, relative to it, sorted."""
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _identity(path):
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def _identity_of_fd(fd):
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


def _fake_mounts(monkeypatch, *paths):
    """Report each of *paths*, a directory, as a mount point: its mount id
    is off by a million; everything else keeps its real one (#221 review
    R2b)."""
    mounted = {_identity(path) for path in paths}
    real = retained_tree._mount_id

    def mount_id(fd, shown):
        return real(fd, shown) + (10**6 if _identity_of_fd(fd) in mounted
                                  else 0)

    monkeypatch.setattr(retained_tree, "_mount_id", mount_id)


class TestDestroySparesFilesInUse:
    """``destroy`` removed the whole workspace, so a file a VM teardown had
    kept because another domain uses it -- directly or as a backing file --
    went anyway. The host-wide in-use scan every teardown asks now runs once
    the project's VMs are gone, while libvirt is still there, and the
    workspace goes except those files, each named (#221)."""

    USED = "cluster_1/bprj__demo__bprj_cluster_1_node01.qcow2"

    @pytest.fixture(autouse=True)
    def _no_docker(self, monkeypatch):
        """The user can delete all of these: the fallback is never due."""
        def docker(*args, **kwargs):
            raise AssertionError(f"docker was run: {args}")
        monkeypatch.setattr("boxman.manager_parts.flows.subprocess.run",
                            docker)

    def _manager(self, tmp_path):
        workspace = _tree(
            tmp_path / "workspaces" / "demo", self.USED,
            "cluster_1/seed.iso", "cluster_1/nocloud/user-data",
            "cluster_1/.boxman-teardown-bprj__demo__bprj_cluster_1_node01.json",
            "env.sh", "keys/id_ed25519")
        mgr = _destroy_manager(workspace)
        mgr.deprovision = MagicMock()
        mgr.destroy_compose_clusters = MagicMock()
        mgr.deprovision_files = MagicMock()
        mgr.unregister_from_cache = MagicMock()
        mgr._confirm_project_torn_down = MagicMock(return_value=(True, ''))
        mgr._retire_stale_teardown_locators = MagicMock()
        mgr.provider.disk_paths_in_use.return_value = FilesInUse()
        return mgr, workspace

    @staticmethod
    def _warnings(mgr):
        return " ".join(str(c.args[0])
                        for c in mgr.logger.warning.call_args_list)

    def test_a_file_another_domain_uses_is_kept_and_named(self, tmp_path):
        mgr, workspace = self._manager(tmp_path)
        used = workspace / self.USED
        mgr.provider.disk_paths_in_use.return_value = FilesInUse(
            {os.path.realpath(used): "other-vm"})

        mgr.destroy(ARGS)

        assert _left(workspace) == ["cluster_1", self.USED]
        assert f"left {used} in place" in self._warnings(mgr)
        assert "domain other-vm uses it" in self._warnings(mgr)
        mgr.provider.disk_paths_in_use.assert_called_once_with()
        mgr._retire_stale_teardown_locators.assert_called_once()
        mgr.unregister_from_cache.assert_called_once()

    def test_a_file_used_under_another_name_is_kept(self, tmp_path):
        """A hard link, a bind mount: the file is known by its identity."""
        mgr, workspace = self._manager(tmp_path)
        alias = tmp_path / "elsewhere" / "disk.qcow2"
        alias.parent.mkdir()
        os.link(workspace / self.USED, alias)
        in_use = FilesInUse({str(alias): "other-vm"})
        in_use.identities[_identity(alias)] = "other-vm"
        mgr.provider.disk_paths_in_use.return_value = in_use

        mgr.destroy(ARGS)

        assert _left(workspace) == ["cluster_1", self.USED]

    def test_a_symlink_to_a_file_in_use_is_kept(self, tmp_path):
        """A domain may use the file through it."""
        mgr, workspace = self._manager(tmp_path)
        target = tmp_path / "elsewhere" / "disk.qcow2"
        _tree(target.parent, target.name)
        (workspace / "cluster_1" / "link.qcow2").symlink_to(target)
        mgr.provider.disk_paths_in_use.return_value = FilesInUse(
            {os.path.realpath(target): "other-vm"})

        mgr.destroy(ARGS)

        assert _left(workspace) == ["cluster_1", "cluster_1/link.qcow2"]
        assert target.exists()

    def test_a_symlink_to_a_file_known_only_by_its_identity_is_kept(
            self, tmp_path):
        """The link's target is in use under another name (a hard link):
        the scan follows the link for its identity (#221 review R2)."""
        mgr, workspace = self._manager(tmp_path)
        target = _tree(tmp_path / "elsewhere", "disk.qcow2") / "disk.qcow2"
        alias = tmp_path / "images" / "disk.img"
        alias.parent.mkdir()
        os.link(target, alias)
        (workspace / "cluster_1" / "link.qcow2").symlink_to(target)
        in_use = FilesInUse({str(alias): "other-vm"})
        in_use.identities[_identity(alias)] = "other-vm"
        mgr.provider.disk_paths_in_use.return_value = in_use

        mgr.destroy(ARGS)

        assert _left(workspace) == ["cluster_1", "cluster_1/link.qcow2"]

    def test_nothing_in_use_removes_the_whole_workspace(self, tmp_path):
        mgr, workspace = self._manager(tmp_path)

        mgr.destroy(ARGS)

        assert not workspace.exists()
        assert self._warnings(mgr) == ""

    def test_a_dangling_symlink_is_no_reason_to_stop(self, tmp_path):
        mgr, workspace = self._manager(tmp_path)
        (workspace / "cluster_1" / "old.qcow2").symlink_to(
            tmp_path / "gone.qcow2")
        used = workspace / self.USED
        mgr.provider.disk_paths_in_use.return_value = FilesInUse(
            {os.path.realpath(used): "other-vm"})

        mgr.destroy(ARGS)

        assert _left(workspace) == ["cluster_1", self.USED]

    def _replace_cluster_at_step_4(self, mgr, workspace, tmp_path):
        """Codex's reproduction (#221 review R2): between the scan (2d) and
        the removal (5) -- at step 4 -- cluster_1, on the way to the kept
        disk, is renamed away and replaced by a symlink to a directory
        outside the workspace, which holds a file of the kept disk's name."""
        used = workspace / self.USED
        outside = _tree(tmp_path / "outside", "unrelated.txt", used.name)
        mgr.provider.disk_paths_in_use.return_value = FilesInUse(
            {os.path.realpath(used): "other-vm"})

        def replace(*_scan):
            os.rename(workspace / "cluster_1", workspace / "moved-cluster")
            (workspace / "cluster_1").symlink_to(outside)
        mgr.deprovision_files.side_effect = replace
        return outside

    def test_a_mount_point_in_the_workspace_stops_destroy(self, tmp_path,
                                                          monkeypatch):
        """#221 review R2b: a filesystem or bind mount under the workspace is
        never walked into: destroy stops before removing anything, and says
        to unmount it."""
        mgr, workspace = self._manager(tmp_path)
        before = _left(workspace)
        _fake_mounts(monkeypatch, workspace / "cluster_1" / "nocloud")

        with pytest.raises(ProvisionError, match="unmount it first"):
            mgr.destroy(ARGS)

        assert _left(workspace) == before
        mgr.deprovision_files.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    def test_with_nothing_in_use_a_mount_point_is_still_not_crossed(
            self, tmp_path, monkeypatch):
        """A mount that appears after the scan: the workspace goes through
        the same removal, which never enters it, whether or not anything is
        kept."""
        mgr, workspace = self._manager(tmp_path)
        nocloud = workspace / "cluster_1" / "nocloud"
        mgr.deprovision_files.side_effect = (
            lambda *_: _fake_mounts(monkeypatch, nocloud))

        with pytest.raises(ProvisionError, match="is a mount point"):
            mgr.destroy(ARGS)

        assert (nocloud / "user-data").exists()
        mgr.unregister_from_cache.assert_not_called()

    def test_a_directory_renamed_between_lookup_and_open_is_not_entered(
            self, tmp_path, monkeypatch):
        """Codex's interleaving (#221 review R2a): between the removal's
        lookup of an empty doomed directory and its open, cluster_1 -- on
        the way to the kept disk -- is renamed over it. The removal used to
        empty it through that name, kept disk included, and report success.
        Now nothing in it is touched, and destroy stops with the project
        registered."""
        mgr, workspace = self._manager(tmp_path)
        (workspace / "aa-doomed").mkdir()
        cluster = sorted(p.name for p in (workspace / "cluster_1").iterdir())
        used = workspace / self.USED
        mgr.provider.disk_paths_in_use.return_value = FilesInUse(
            {os.path.realpath(used): "other-vm"})
        real_stat = os.stat
        armed = []

        def stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if armed and path == "aa-doomed" and kwargs.get("dir_fd"):
                armed.clear()
                os.rename(workspace / "cluster_1", workspace / "aa-doomed")
            return st

        # armed at step 4: the scan is done, the removal to come
        mgr.deprovision_files.side_effect = lambda *_: armed.append(True)
        monkeypatch.setattr(os, "stat", stat)
        with pytest.raises(ProvisionError, match="was moved or replaced"):
            mgr.destroy(ARGS)
        monkeypatch.undo()

        assert sorted(p.name for p in (workspace / "aa-doomed").iterdir()) \
            == cluster
        mgr.unregister_from_cache.assert_not_called()

    def test_a_directory_replaced_after_the_scan_stops_the_removal(
            self, tmp_path):
        mgr, workspace = self._manager(tmp_path)
        outside = self._replace_cluster_at_step_4(mgr, workspace, tmp_path)
        before = _left(workspace)

        with pytest.raises(ProvisionError, match="could not open"):
            mgr.destroy(ARGS)

        assert _left(outside) == [
            "bprj__demo__bprj_cluster_1_node01.qcow2", "unrelated.txt"]
        assert (workspace / "moved-cluster" /
                "bprj__demo__bprj_cluster_1_node01.qcow2").exists()
        assert _left(workspace) == sorted(
            [*(p.replace("cluster_1", "moved-cluster", 1)
               for p in before), "cluster_1"])
        mgr._retire_stale_teardown_locators.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    @unless_root
    def test_a_directory_replaced_after_the_scan_stops_the_fallback_too(
            self, tmp_path):
        """... when the user cannot remove everything, so the docker
        fallback would be due: it is never run over a replaced directory
        (the autouse fixture fails the test if it is)."""
        mgr, workspace = self._manager(tmp_path)
        outside = self._replace_cluster_at_step_4(mgr, workspace, tmp_path)
        (workspace / "keys").chmod(0o555)
        try:
            with pytest.raises(ProvisionError, match="could not open"):
                mgr.destroy(ARGS)
        finally:
            (workspace / "keys").chmod(0o755)

        assert _left(outside) == [
            "bprj__demo__bprj_cluster_1_node01.qcow2", "unrelated.txt"]
        assert (workspace / "moved-cluster" /
                "bprj__demo__bprj_cluster_1_node01.qcow2").exists()
        mgr.unregister_from_cache.assert_not_called()

    # -- #221 review R3: the generated-file cleanup (step 4) -------------------

    def test_a_generated_file_in_the_workspace_another_domain_uses_stays(
            self, tmp_path, captured_logs):
        """Codex's reproduction: a file listed in workspace.files is another
        domain's CD-ROM. The real deprovision_files (step 4) removed it
        before the workspace removal could spare it, which then claimed it
        had been left in place. Now step 4 leaves it to step 5, which names
        it, once."""
        mgr, workspace = self._manager(tmp_path)
        del mgr.deprovision_files                # the real step 4
        seed = _tree(workspace / "cluster_1" / "cfg", "seed.iso") / "seed.iso"
        mgr.config["workspace"]["files"] = {"cluster_1/cfg/seed.iso": "",
                                            "env.sh": ""}
        mgr.provider.disk_paths_in_use.return_value = FilesInUse(
            {os.path.realpath(seed): "other-vm"})

        mgr.destroy(ARGS)

        assert _left(workspace) == ["cluster_1", "cluster_1/cfg",
                                    "cluster_1/cfg/seed.iso"]
        assert f"left {os.path.realpath(seed)} in place: domain other-vm " \
               f"uses it" in self._warnings(mgr)
        assert "seed.iso" not in captured_logs.text     # not step 4 too
        mgr.unregister_from_cache.assert_called_once()

    @pytest.mark.parametrize("listed", [
        "workspace.files", "cluster.files", "the admin key",
        "the admin key's .pub", "ssh_config"])
    def test_no_generated_file_another_domain_uses_is_removed(self, tmp_path,
                                                              listed):
        """Every file step 4 removes gets the scan's answer."""
        mgr, workspace = self._manager(tmp_path)
        del mgr.deprovision_files
        name = {"workspace.files": "listed.iso",
                "cluster.files": "cluster_1/listed.iso",
                "the admin key": "id_ed25519_boxman",
                "the admin key's .pub": "id_ed25519_boxman.pub",
                "ssh_config": "ssh_config"}[listed]
        used = _tree(workspace, name) / name
        if listed == "workspace.files":
            mgr.config["workspace"]["files"] = {name: ""}
        if listed == "cluster.files":
            mgr.config["clusters"]["cluster_1"]["files"] = {"listed.iso": ""}
        mgr.provider.disk_paths_in_use.return_value = FilesInUse(
            {os.path.realpath(used): "other-vm"})

        mgr.destroy(ARGS)

        assert used.exists()
        mgr.unregister_from_cache.assert_called_once()

    def test_a_workspace_reached_through_a_symlink_is_still_step_5s(
            self, tmp_path, captured_logs):
        """workspace.path through a symlinked directory: a generated file
        in it is still recognised as the workspace's, and named once."""
        mgr, workspace = self._manager(tmp_path)
        del mgr.deprovision_files
        via = tmp_path / "via"
        via.symlink_to(workspace.parent)
        mgr.config["workspace"]["path"] = str(via / workspace.name)
        seed = workspace / "cluster_1" / "seed.iso"
        mgr.config["workspace"]["files"] = {"cluster_1/seed.iso": ""}
        mgr.provider.disk_paths_in_use.return_value = FilesInUse(
            {os.path.realpath(seed): "other-vm"})

        mgr.destroy(ARGS)

        assert seed.exists()
        assert "seed.iso" not in captured_logs.text
        assert "seed.iso in place" in self._warnings(mgr)

    def _generated_elsewhere(self, mgr, tmp_path):
        """Two generated files outside the workspace, listed in
        cluster.files by absolute path, in a directory whose name begins
        with the workspace's."""
        elsewhere = _tree(tmp_path / "workspaces" / "demo-elsewhere",
                          "used.iso", "unused.cfg")
        mgr.config["clusters"]["cluster_1"]["files"] = {
            str(elsewhere / "used.iso"): "", str(elsewhere / "unused.cfg"): ""}
        return elsewhere

    def test_a_generated_file_elsewhere_another_domain_uses_stays(
            self, tmp_path, captured_logs):
        mgr, workspace = self._manager(tmp_path)
        del mgr.deprovision_files
        elsewhere = self._generated_elsewhere(mgr, tmp_path)
        mgr.provider.disk_paths_in_use.return_value = FilesInUse(
            {os.path.realpath(elsewhere / "used.iso"): "other-vm"})

        mgr.destroy(ARGS)

        assert _left(elsewhere) == ["used.iso"]
        assert f"left {elsewhere / 'used.iso'} in place: domain other-vm " \
               f"uses it" in captured_logs.text
        assert not workspace.exists()

    def test_a_generated_file_used_under_another_name_stays(self, tmp_path):
        mgr, _workspace = self._manager(tmp_path)
        del mgr.deprovision_files
        elsewhere = self._generated_elsewhere(mgr, tmp_path)
        alias = tmp_path / "images" / "disk.img"
        alias.parent.mkdir()
        os.link(elsewhere / "used.iso", alias)
        in_use = FilesInUse({str(alias): "other-vm"})
        in_use.identities[_identity(alias)] = "other-vm"
        mgr.provider.disk_paths_in_use.return_value = in_use

        mgr.destroy(ARGS)

        assert _left(elsewhere) == ["used.iso"]

    @unless_root
    def test_a_generated_file_whose_identity_cannot_be_read_stays(
            self, tmp_path, captured_logs):
        mgr, _workspace = self._manager(tmp_path)
        del mgr.deprovision_files
        elsewhere = self._generated_elsewhere(mgr, tmp_path)
        elsewhere.chmod(0o600)                 # listable, not searchable
        try:
            mgr.destroy(ARGS)
        finally:
            elsewhere.chmod(0o755)

        assert _left(elsewhere) == ["unused.cfg", "used.iso"]
        assert "whether another domain uses it could not be told" in \
            captured_logs.text

    def test_a_symlink_to_a_directory_goes_but_not_what_it_points_to(
            self, tmp_path):
        mgr, workspace = self._manager(tmp_path)
        isos = _tree(tmp_path / "isos", "install.iso")
        (workspace / "cluster_1" / "isos").symlink_to(isos)
        used = workspace / self.USED
        mgr.provider.disk_paths_in_use.return_value = FilesInUse(
            {os.path.realpath(used): "other-vm"})

        mgr.destroy(ARGS)

        assert _left(workspace) == ["cluster_1", self.USED]
        assert _left(isos) == ["install.iso"]

    # -- #221 review R4: absent is not the same as inaccessible ---------------

    @contextlib.contextmanager
    def _unsearchable(self, directory):
        directory.chmod(0)
        try:
            yield
        finally:
            directory.chmod(0o755)

    def _assert_nothing_cleaned_up(self, mgr, workspace):
        mgr.deprovision_files.assert_not_called()
        mgr._retire_stale_teardown_locators.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()
        assert (workspace / self.USED).exists()

    @unless_root
    def test_a_workspace_behind_an_unsearchable_directory_stops_it(
            self, tmp_path):
        """Codex's reproduction: the workspace's parent is mode 000, so the
        workspace could not be looked up -- which is not "it is gone". It
        used to be skipped, the project unregistered, the workspace left."""
        mgr, workspace = self._manager(tmp_path)

        with self._unsearchable(workspace.parent), \
                pytest.raises(ProvisionError, match="could not look up"):
            mgr.destroy(ARGS)

        self._assert_nothing_cleaned_up(mgr, workspace)

    @unless_root
    def test_so_does_one_of_a_project_without_libvirt_clusters(
            self, tmp_path):
        """No domains to ask about, but the same question, answered before
        the runtime, the generated files or the cache entry go."""
        mgr, workspace = self._manager(tmp_path)
        mgr.config["provider"] = {"docker-compose": {}}

        with self._unsearchable(workspace.parent), \
                pytest.raises(ProvisionError, match="could not look up"):
            mgr.destroy(ARGS)

        self._assert_nothing_cleaned_up(mgr, workspace)
        mgr.provider.disk_paths_in_use.assert_not_called()

    @unless_root
    def test_one_that_cannot_be_looked_up_is_something_to_do(self, tmp_path):
        """Not in the cache, nothing else left: an inaccessible workspace
        still is not "nothing to do"."""
        mgr, workspace = self._manager(tmp_path)
        mgr.cache.projects = {}

        with self._unsearchable(workspace.parent), \
                pytest.raises(ProvisionError, match="could not look up"):
            mgr.destroy(ARGS)

        self._assert_nothing_cleaned_up(mgr, workspace)

    def test_a_workspace_already_gone_is_no_reason_to_stop(self, tmp_path):
        """Registered, yet removed by hand: nothing is left to spare."""
        mgr, workspace = self._manager(tmp_path)
        shutil.rmtree(workspace)

        mgr.destroy(ARGS)

        mgr.unregister_from_cache.assert_called_once()

    def test_a_failed_teardown_is_not_followed_by_a_scan(self, tmp_path):
        mgr, _workspace = self._manager(tmp_path)
        mgr._confirm_project_torn_down = MagicMock(
            return_value=(False, "VMs are still defined: node01"))

        with pytest.raises(ProvisionError, match="still defined"):
            mgr.destroy(ARGS)

        mgr.provider.disk_paths_in_use.assert_not_called()

    def test_a_scan_that_cannot_complete_keeps_everything_and_fails(
            self, tmp_path):
        mgr, workspace = self._manager(tmp_path)
        mgr.provider.disk_paths_in_use.return_value = None
        before = _left(workspace)

        with pytest.raises(ProvisionError, match="destroy did not complete"):
            mgr.destroy(ARGS)

        assert _left(workspace) == before
        mgr.deprovision_files.assert_not_called()
        mgr._retire_stale_teardown_locators.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    @unless_root
    def test_a_directory_that_cannot_be_listed_keeps_everything(
            self, tmp_path):
        """What is in it cannot be compared with what other domains use."""
        mgr, workspace = self._manager(tmp_path)
        locked = workspace / "cluster_1" / "nocloud"
        locked.chmod(0)
        try:
            with pytest.raises(ProvisionError, match="could not list"):
                mgr.destroy(ARGS)
        finally:
            locked.chmod(0o700)

        assert (workspace / "cluster_1" / "seed.iso").exists()
        mgr.deprovision_files.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    @unless_root
    def test_a_file_whose_identity_cannot_be_read_keeps_everything(
            self, tmp_path):
        """A directory that lists but cannot be searched: it could be any
        file."""
        mgr, workspace = self._manager(tmp_path)
        blind = workspace / "cluster_1" / "nocloud"
        blind.chmod(0o400)
        try:
            with pytest.raises(ProvisionError, match="identity"):
                mgr.destroy(ARGS)
        finally:
            blind.chmod(0o700)

        assert (workspace / "cluster_1" / "seed.iso").exists()
        mgr.unregister_from_cache.assert_not_called()

    def test_a_directory_the_scan_cannot_list_keeps_everything(
            self, tmp_path, monkeypatch):
        """An I/O error listing an open directory during the scan: nothing
        is taken for empty (#221 review R2)."""
        mgr, workspace = self._manager(tmp_path)
        listdir = os.listdir

        def failing(path=".", *args):
            if isinstance(path, int):
                raise OSError(5, "Input/output error")
            return listdir(path, *args)

        monkeypatch.setattr(os, "listdir", failing)
        with pytest.raises(ProvisionError, match="could not list"):
            mgr.destroy(ARGS)
        monkeypatch.undo()

        assert (workspace / "env.sh").exists()
        mgr.deprovision_files.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    def _docker_manager(self, tmp_path):
        from boxman.runtime.docker_compose import DockerComposeRuntime

        mgr, workspace = self._manager(tmp_path)
        runtime = MagicMock(spec=DockerComposeRuntime)
        runtime.name = "docker-compose"
        runtime.ready_timeout = 60
        runtime.plan_destroy_runtime.return_value = {
            "actions": ["tear down docker-compose environment"],
            "commands": ["docker compose down --volumes"],
            "paths_to_delete": [],
            "container_running": True,
        }
        runtime.destroy_runtime.return_value = None
        mgr._runtime_instance = runtime
        return mgr, workspace, runtime

    def test_the_scan_runs_while_the_runtime_is_still_up(self, tmp_path):
        """Under the docker runtime libvirt lives in the runtime container,
        which step 3 takes down."""
        mgr, workspace, runtime = self._docker_manager(tmp_path)
        order = MagicMock()
        order.attach_mock(mgr.provider.disk_paths_in_use, "scan")
        order.attach_mock(runtime.destroy_runtime, "runtime")
        order.attach_mock(mgr.deprovision_files, "files")

        mgr.destroy(ARGS)

        assert [c[0] for c in order.mock_calls] == ["scan", "runtime",
                                                    "files"]
        assert not workspace.exists()

    def test_a_failed_scan_keeps_the_runtime_too(self, tmp_path):
        mgr, workspace, runtime = self._docker_manager(tmp_path)
        mgr.provider.disk_paths_in_use.return_value = None

        with pytest.raises(ProvisionError, match="destroy did not complete"):
            mgr.destroy(ARGS)

        runtime.destroy_runtime.assert_not_called()
        assert (workspace / self.USED).exists()

    @unless_root
    def test_a_project_without_libvirt_clusters_is_not_scanned(
            self, tmp_path, monkeypatch):
        """... nor its workspace walked: a directory a container made
        unreadable there goes through the docker fallback, as before."""
        mgr, workspace = self._manager(tmp_path)
        mgr.config["provider"] = {"docker-compose": {}}
        data = workspace / "cluster_1" / "pgdata"
        _tree(data, "PG_VERSION")
        data.chmod(0)

        def docker(argv, **_kwargs):          # as root in the container
            data.chmod(0o700)
            for entry in workspace.iterdir():
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr("boxman.manager_parts.flows.subprocess.run",
                            docker)
        try:
            mgr.destroy(ARGS)
        finally:
            if data.exists():
                data.chmod(0o700)

        mgr.provider.disk_paths_in_use.assert_not_called()
        assert not workspace.exists()

    def test_clusters_that_share_a_session_are_scanned_once(self, tmp_path):
        mgr, workspace = self._manager(tmp_path)
        mgr.config["clusters"]["cluster_2"] = {
            "workdir": str(workspace / "cluster_2"), "vms": {}}

        mgr.destroy(ARGS)

        mgr.provider.disk_paths_in_use.assert_called_once_with()


def _scanned(workspace, *kept):
    """The in-use scan of *workspace* that keeps *kept* (relative paths)."""
    root = os.path.realpath(workspace)
    wanted = {os.path.join(root, rel) for rel in kept}
    return retained_tree.scan_tree(
        root, lambda path, _target: ("domain other-vm uses it"
                                     if path in wanted else None))


def _lidentity(path):
    st = os.lstat(path)
    return (st.st_dev, st.st_ino)


class TestForceRmtreeExcept:
    """The sparing removal keeps :meth:`_force_rmtree`'s guard and its
    docker fallback -- aimed at exactly what is left, never at a kept file
    or a directory on the way to one (#221) -- and walks the workspace
    through directory descriptors, stopping at anything on the way, or kept,
    that is no longer what the scan found (#221 review R2)."""

    @pytest.fixture(autouse=True)
    def _no_docker(self, monkeypatch):
        """Unless a test says otherwise, the fallback is never due."""
        def docker(*args, **kwargs):
            raise AssertionError(f"docker was run: {args}")
        monkeypatch.setattr("boxman.manager_parts.flows.subprocess.run",
                            docker)

    def test_the_scan_records_what_the_removal_checks(self, tmp_path):
        workspace = _tree(tmp_path / "ws", "c1/sub/keep.qcow2",
                          "c1/drop.qcow2", "c2/x")
        tree = _scanned(workspace, "c1/sub/keep.qcow2")

        assert tree.root == os.path.realpath(workspace)
        assert tree.root_id == _lidentity(workspace)
        assert tree.dirs == {"c1": _lidentity(workspace / "c1"),
                             "c1/sub": _lidentity(workspace / "c1" / "sub")}
        assert tree.kept == {"c1/sub/keep.qcow2": "domain other-vm uses it"}
        assert tree.kept_ids == {
            "c1/sub/keep.qcow2": _lidentity(workspace / "c1" / "sub" /
                                           "keep.qcow2")}

    def test_everything_else_goes(self, tmp_path):
        workspace = _tree(tmp_path / "ws", "c1/sub/keep.qcow2",
                          "c1/drop.qcow2", "c1/sub/x", "c2/y/z", "top.txt")
        tree = _scanned(workspace, "c1/sub/keep.qcow2")

        BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert _left(workspace) == ["c1", "c1/sub", "c1/sub/keep.qcow2"]

    def test_with_nothing_kept_the_root_goes_too_or_it_is_an_error(
            self, tmp_path, monkeypatch):
        """Nothing kept: the emptied root is removed as well; if it cannot
        be, the removal did not complete (#221 review R2b routes a
        workspace with nothing kept through this removal)."""
        workspace = _tree(tmp_path / "ws", "c1/x", "top.txt")
        tree = _scanned(workspace)
        rmdir = os.rmdir

        def busy(path, *args, **kwargs):
            if path == os.path.realpath(workspace):
                raise OSError(16, "Device or resource busy")
            return rmdir(path, *args, **kwargs)

        monkeypatch.setattr(os, "rmdir", busy)
        with pytest.raises(ProvisionError, match="once it was empty"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)
        monkeypatch.undo()
        assert _left(workspace) == []

        BoxmanManager._force_rmtree_except(str(workspace),
                                           _scanned(workspace))
        assert not workspace.exists()

    def test_a_rejected_path_reaches_nothing_under_it(self, tmp_path):
        workspace = _tree(tmp_path / "real_ws", "c1/keep.qcow2",
                          "c1/drop.qcow2", "top.txt")
        tree = _scanned(workspace, "c1/keep.qcow2")
        link = tmp_path / "ws_link"
        link.symlink_to(workspace)

        with pytest.raises(ProvisionError, match="symlink"):
            BoxmanManager._force_rmtree_except(str(link), tree)

        assert _left(workspace) == ["c1", "c1/drop.qcow2", "c1/keep.qcow2",
                                    "top.txt"]

    def test_a_path_that_leads_elsewhere_than_the_scan_stops_it(
            self, tmp_path):
        """A symlinked ancestor repointed since the scan: the target vetted
        now is not the tree scanned."""
        scanned = _tree(tmp_path / "a" / "ws", "c1/keep.qcow2", "top.txt")
        other = _tree(tmp_path / "b" / "ws", "c1/keep.qcow2", "other.txt")
        via = tmp_path / "via"
        via.symlink_to(tmp_path / "a")
        tree = _scanned(via / "ws", "c1/keep.qcow2")
        via.unlink()
        via.symlink_to(tmp_path / "b")

        with pytest.raises(ProvisionError, match="the in-use scan read"):
            BoxmanManager._force_rmtree_except(str(via / "ws"), tree)

        assert _left(scanned) == ["c1", "c1/keep.qcow2", "top.txt"]
        assert _left(other) == ["c1", "c1/keep.qcow2", "other.txt"]

    def test_a_directory_on_the_way_replaced_by_a_symlink_stops_it(
            self, tmp_path):
        """Codex's reproduction (#221 review R2), without destroy around it:
        nothing inside or outside is removed."""
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "c1/drop.qcow2",
                          "top.txt")
        outside = _tree(tmp_path / "outside", "unrelated.txt", "keep.qcow2")
        tree = _scanned(workspace, "c1/keep.qcow2")
        os.rename(workspace / "c1", workspace / "moved")
        (workspace / "c1").symlink_to(outside)

        with pytest.raises(ProvisionError, match="could not open"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert _left(outside) == ["keep.qcow2", "unrelated.txt"]
        assert _left(workspace) == ["c1", "moved", "moved/drop.qcow2",
                                    "moved/keep.qcow2", "top.txt"]

    def test_a_directory_on_the_way_replaced_by_a_symlink_to_itself(
            self, tmp_path):
        """Even one leading back to the very directory scanned: nothing on
        the way may be a symlink."""
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "c1/drop.qcow2")
        tree = _scanned(workspace, "c1/keep.qcow2")
        os.rename(workspace / "c1", workspace / "moved")
        (workspace / "c1").symlink_to(workspace / "moved")

        with pytest.raises(ProvisionError, match="could not open"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert (workspace / "moved" / "drop.qcow2").exists()

    def test_a_directory_on_the_way_replaced_by_another_stops_it(
            self, tmp_path):
        """A real directory in its place: known by its identity."""
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "c1/drop.qcow2")
        tree = _scanned(workspace, "c1/keep.qcow2")
        os.rename(workspace / "c1", workspace / "moved")
        _tree(workspace / "c1", "keep.qcow2", "planted.txt")

        with pytest.raises(ProvisionError, match="was replaced"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert _left(workspace / "c1") == ["keep.qcow2", "planted.txt"]
        assert (workspace / "moved" / "drop.qcow2").exists()

    @pytest.mark.parametrize("replaced", ["c1", "the root"])
    def test_a_directory_moved_in_with_the_kept_file_stops_it(
            self, tmp_path, replaced):
        """A directory from elsewhere moved into the place of one on the
        way, the kept file itself moved into it: only the directory's
        identity tells that its other files were never the workspace's."""
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "c1/drop.qcow2")
        tree = _scanned(workspace, "c1/keep.qcow2")
        foreign = _tree(tmp_path / "home" / "data", "precious.txt")
        if replaced == "c1":
            os.rename(workspace / "c1" / "keep.qcow2", foreign / "keep.qcow2")
            os.rename(workspace / "c1", tmp_path / "c1-scanned")
            os.rename(foreign, workspace / "c1")
        else:
            (foreign / "c1").mkdir()
            os.rename(workspace / "c1" / "keep.qcow2",
                      foreign / "c1" / "keep.qcow2")
            os.rename(workspace, tmp_path / "ws-scanned")
            os.rename(foreign, workspace)

        with pytest.raises(ProvisionError, match="was replaced"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        moved_in = workspace / "c1" if replaced == "c1" else workspace
        assert (moved_in / "precious.txt").exists()

    def test_a_replaced_root_stops_it(self, tmp_path):
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "top.txt")
        tree = _scanned(workspace, "c1/keep.qcow2")
        os.rename(workspace, tmp_path / "ws-scanned")
        _tree(workspace, "c1/keep.qcow2", "other.txt")

        with pytest.raises(ProvisionError, match="was replaced"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert _left(workspace) == ["c1", "c1/keep.qcow2", "other.txt"]

    @pytest.mark.parametrize("change, message", [
        ("replaced", "was replaced"), ("gone", "could not be found again")])
    def test_a_kept_file_changed_since_the_scan_stops_it(self, tmp_path,
                                                         change, message):
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "c1/drop.qcow2")
        tree = _scanned(workspace, "c1/keep.qcow2")
        os.rename(workspace / "c1" / "keep.qcow2", tmp_path / "keep.moved")
        if change == "replaced":
            _tree(workspace / "c1", "keep.qcow2")

        with pytest.raises(ProvisionError, match=message):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert (workspace / "c1" / "drop.qcow2").exists()

    @unless_root
    def test_a_directory_on_the_way_that_cannot_be_opened_stops_it(
            self, tmp_path):
        workspace = _tree(tmp_path / "workspaces" / "demo", "c1/keep.qcow2",
                          "top.txt")
        tree = _scanned(workspace, "c1/keep.qcow2")
        (workspace / "c1").chmod(0o300)
        try:
            with pytest.raises(ProvisionError, match="could not open"):
                BoxmanManager._force_rmtree_except(str(workspace), tree)
        finally:
            (workspace / "c1").chmod(0o755)

        assert (workspace / "top.txt").exists()

    def test_a_directory_that_cannot_be_listed_again_stops_it(
            self, tmp_path, monkeypatch):
        """An I/O error listing a verified directory: nothing is taken for
        empty."""
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "top.txt")
        tree = _scanned(workspace, "c1/keep.qcow2")
        listdir = os.listdir

        def failing(path=".", *args):
            if isinstance(path, int):
                raise OSError(5, "Input/output error")
            return listdir(path, *args)

        monkeypatch.setattr(os, "listdir", failing)
        with pytest.raises(ProvisionError, match="could not list"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert (workspace / "top.txt").exists()

    def test_a_directory_swapped_for_a_symlink_mid_removal_is_not_followed(
            self, tmp_path, monkeypatch):
        """Between looking an entry that goes up and opening it, it becomes
        a symlink to a directory outside: it is removed as a link, never
        entered."""
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "doomed/inner")
        outside = _tree(tmp_path / "outside", "unrelated.txt")
        tree = _scanned(workspace, "c1/keep.qcow2")
        real_stat = os.stat
        swapped = []

        def stat_then_swap(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if path == "doomed" and not swapped:
                swapped.append(path)
                os.rename(workspace / "doomed", tmp_path / "doomed-away")
                (workspace / "doomed").symlink_to(outside)
            return st

        def docker(argv, **_kwargs):          # as root in the container
            for target in argv[argv.index("--") + 1:]:
                os.unlink(os.path.join(tree.root,
                                       os.path.relpath(target, "/cleanup")))
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(os, "stat", stat_then_swap)
        monkeypatch.setattr("boxman.manager_parts.flows.subprocess.run",
                            docker)
        BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert swapped
        assert _left(outside) == ["unrelated.txt"]
        assert _left(workspace) == ["c1", "c1/keep.qcow2"]

    def _locked(self, tmp_path):
        """A workspace whose ``c1`` the user cannot write: what is directly
        in it stays after the direct attempt."""
        workspace = _tree(tmp_path / "workspaces" / "demo",
                          "c1/keep.qcow2", "c1/drop.qcow2", "c1/sub/x",
                          "top.txt")
        tree = _scanned(workspace, "c1/keep.qcow2")
        (workspace / "c1").chmod(0o555)
        return workspace, tree

    @staticmethod
    def _as_root(workspace, argv):
        """What ``rm -rf`` of *argv*'s targets does as root: c1's mode is
        no obstacle."""
        locked = workspace / "c1"
        locked.chmod(0o755)
        for target in argv[argv.index("--") + 1:]:
            path = os.path.join(os.path.realpath(workspace),
                                os.path.relpath(target, "/cleanup"))
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            elif os.path.lexists(path):
                os.unlink(path)
        locked.chmod(0o555)

    @unless_root
    def test_leftovers_go_through_docker_but_a_kept_file_never(
            self, tmp_path, monkeypatch):
        workspace, tree = self._locked(tmp_path)
        real = os.path.realpath(workspace)
        asked = []

        def docker(argv, **_kwargs):
            asked.append(argv)
            self._as_root(workspace, argv)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr("boxman.manager_parts.flows.subprocess.run",
                            docker)
        try:
            BoxmanManager._force_rmtree_except(str(workspace), tree)
        finally:
            (workspace / "c1").chmod(0o755)

        assert _left(workspace) == ["c1", "c1/keep.qcow2"]
        [argv] = asked
        assert argv[:6] == ["docker", "run", "--rm", "-v",
                            f"{real}:/cleanup", "alpine"]
        assert sorted(argv[argv.index("--") + 1:]) == [
            "/cleanup/c1/drop.qcow2", "/cleanup/c1/sub"]

    @unless_root
    def test_raises_when_a_leftover_survives_both_attempts(
            self, tmp_path, monkeypatch):
        workspace, tree = self._locked(tmp_path)
        monkeypatch.setattr(
            "boxman.manager_parts.flows.subprocess.run",
            lambda *a, **k: SimpleNamespace(returncode=0))
        try:
            with pytest.raises(ProvisionError, match="could not remove"):
                BoxmanManager._force_rmtree_except(str(workspace), tree)
        finally:
            (workspace / "c1").chmod(0o755)

        assert (workspace / "c1" / "keep.qcow2").exists()

    @unless_root
    def test_the_tree_is_verified_again_right_before_the_fallback(
            self, tmp_path, monkeypatch):
        """Replaced between the direct removal and the fallback: the
        fallback, which resolves paths by name, is never run over it."""
        workspace, tree = self._locked(tmp_path)
        outside = _tree(tmp_path / "outside", "unrelated.txt", "drop.qcow2")
        sweep = retained_tree._sweep
        sweeps = []

        def swept(t, remove):
            sweeps.append(remove)
            left = sweep(t, remove)
            if remove:
                (workspace / "c1").chmod(0o755)
                os.rename(workspace / "c1", workspace / "moved")
                (workspace / "c1").symlink_to(outside)
            return left

        monkeypatch.setattr(retained_tree, "_sweep", swept)
        try:
            with pytest.raises(ProvisionError, match="could not open"):
                BoxmanManager._force_rmtree_except(str(workspace), tree)
        finally:
            (workspace / "moved").chmod(0o755)

        assert sweeps == [True, False]
        assert _left(outside) == ["drop.qcow2", "unrelated.txt"]
        assert (workspace / "moved" / "keep.qcow2").exists()

    @unless_root
    def test_the_tree_is_verified_again_after_the_fallback(
            self, tmp_path, monkeypatch):
        """The fallback resolves paths by name: a kept file it took with it
        through a replaced directory is found out once it is done."""
        workspace, tree = self._locked(tmp_path)

        def docker(argv, **_kwargs):
            self._as_root(workspace, argv)
            (workspace / "c1").chmod(0o755)
            (workspace / "c1" / "keep.qcow2").unlink()
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr("boxman.manager_parts.flows.subprocess.run",
                            docker)
        with pytest.raises(ProvisionError, match="could not be found again"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

    # -- #221 review R2b: never cross a mount point --------------------------

    def test_a_directory_mounted_after_the_scan_is_not_entered(
            self, tmp_path, monkeypatch):
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "aa/x", "top.txt")
        tree = _scanned(workspace, "c1/keep.qcow2")
        _fake_mounts(monkeypatch, workspace / "aa")

        with pytest.raises(ProvisionError, match="is a mount point"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert (workspace / "aa" / "x").exists()

    @pytest.mark.parametrize("where", ["c1", "the root"])
    def test_a_retained_directory_mounted_over_stops_it(
            self, tmp_path, monkeypatch, where):
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "top.txt")
        tree = _scanned(workspace, "c1/keep.qcow2")
        _fake_mounts(monkeypatch,
                     workspace / "c1" if where == "c1" else workspace)

        with pytest.raises(ProvisionError, match="is a mount point"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert (workspace / "top.txt").exists()

    def test_a_mount_id_that_cannot_be_read_stops_the_scan(
            self, tmp_path, monkeypatch):
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2")
        monkeypatch.setattr(retained_tree, "_FDINFO",
                            str(tmp_path / "no-such-dir" / "{fd}"))

        with pytest.raises(ProvisionError, match="is a mount point"):
            _scanned(workspace, "c1/keep.qcow2")

    def test_fdinfo_that_names_no_mount_id_stops_the_scan(self, tmp_path,
                                                          monkeypatch):
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2")
        (tmp_path / "fdinfo").write_text("pos:\t0\nflags:\t02004000\n")
        monkeypatch.setattr(retained_tree, "_FDINFO",
                            str(tmp_path / "fdinfo"))

        with pytest.raises(ProvisionError, match="names no mnt_id"):
            _scanned(workspace, "c1/keep.qcow2")

    def test_mountinfo_is_read_with_its_escapes(self, tmp_path, monkeypatch):
        (tmp_path / "mountinfo").write_bytes(
            b"22 1 0:21 / / rw - ext4 /dev/vda1 rw\n"
            b"36 22 0:21 /src /ws/a\\040b\\134c rw,relatime - ext4 /dev/vda1 "
            b"rw\n")
        monkeypatch.setattr(retained_tree, "_MOUNTINFO",
                            str(tmp_path / "mountinfo"))

        assert retained_tree._mount_points() == ["/", "/ws/a b\\c"]

    @pytest.mark.parametrize("mountinfo", ["unreadable", "malformed"])
    def test_mountinfo_that_cannot_be_read_stops_the_fallback(
            self, tmp_path, monkeypatch, mountinfo):
        workspace, tree = self._locked(tmp_path)
        if mountinfo == "malformed":
            (tmp_path / "mountinfo").write_text("36 22 0:21\n")
        monkeypatch.setattr(retained_tree, "_MOUNTINFO",
                            str(tmp_path / "mountinfo"))
        try:
            with pytest.raises(ProvisionError, match="mountinfo"):
                BoxmanManager._force_rmtree_except(str(workspace), tree)
        finally:
            (workspace / "c1").chmod(0o755)

    @unless_root
    @pytest.mark.parametrize("at", ["c1/sub/m", "c1/drop.qcow2"])
    def test_the_fallback_never_runs_over_a_mount_point(self, tmp_path,
                                                        monkeypatch, at):
        """The container's ``rm -rf`` would cross it: the leftovers are
        checked against every mount point of this namespace first, and the
        refusal names the mount point and, when it is inside one, the
        leftover it is in."""
        workspace, tree = self._locked(tmp_path)
        real = os.path.realpath(workspace)
        monkeypatch.setattr(retained_tree, "_mount_points", lambda: [
            "/", os.path.join(real, at)])
        try:
            with pytest.raises(ProvisionError) as raised:
                BoxmanManager._force_rmtree_except(str(workspace), tree)
        finally:
            (workspace / "c1").chmod(0o755)

        shown = {"c1/sub/m": f"{real}/c1/sub/m, inside {real}/c1/sub",
                 "c1/drop.qcow2": f"{real}/c1/drop.qcow2"}[at]
        assert (f"a filesystem is mounted at {shown}, which the "
                f"containerised fallback would have to remove"
                in str(raised.value))

    @unless_root
    def test_a_mount_point_beside_a_leftover_does_not_stop_the_fallback(
            self, tmp_path, monkeypatch):
        workspace, tree = self._locked(tmp_path)
        real = os.path.realpath(workspace)
        monkeypatch.setattr(retained_tree, "_mount_points", lambda: [
            "/", os.path.join(real, "c1", "sub-other"),
            os.path.join(real, "c1", "drop.qcow2.d")])

        def docker(argv, **_kwargs):
            self._as_root(workspace, argv)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr("boxman.manager_parts.flows.subprocess.run",
                            docker)
        try:
            BoxmanManager._force_rmtree_except(str(workspace), tree)
        finally:
            (workspace / "c1").chmod(0o755)

        assert _left(workspace) == ["c1", "c1/keep.qcow2"]

    # -- #221 review R2a: what is kept, under any name, all through ----------

    @staticmethod
    def _after_verification(monkeypatch, action):
        """Run *action* once, right after the removal has checked the kept
        entry -- the tree verified, nothing removed yet."""
        check = retained_tree._check_kept
        done = []

        def checked(*args):
            check(*args)
            if not done:
                done.append(True)
                action()

        monkeypatch.setattr(retained_tree, "_check_kept", checked)

    def test_a_kept_file_moved_into_a_doomed_directory_stays(
            self, tmp_path, monkeypatch):
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "doomed/x")
        tree = _scanned(workspace, "c1/keep.qcow2")
        self._after_verification(monkeypatch, lambda: os.rename(
            workspace / "c1" / "keep.qcow2", workspace / "doomed" / "keep"))

        with pytest.raises(ProvisionError, match="could not be found again"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert (workspace / "doomed" / "keep").exists()

    def test_a_retained_directory_moved_into_a_doomed_one_is_not_entered(
            self, tmp_path, monkeypatch):
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "c1/drop.qcow2",
                          "doomed/x")
        tree = _scanned(workspace, "c1/keep.qcow2")
        self._after_verification(monkeypatch, lambda: os.rename(
            workspace / "c1", workspace / "doomed" / "c1"))

        with pytest.raises(ProvisionError, match="was moved or replaced"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert _left(workspace / "doomed" / "c1") == ["drop.qcow2",
                                                      "keep.qcow2"]

    @pytest.mark.parametrize("change", ["moved", "replaced"])
    def test_a_retained_directory_changed_mid_removal_is_not_emptied(
            self, tmp_path, monkeypatch, change):
        """Its path no longer leads to it: its own pass, through the
        descriptor still open on it, does not run."""
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "c1/drop.qcow2")
        tree = _scanned(workspace, "c1/keep.qcow2")

        def change_c1():
            os.rename(workspace / "c1", tmp_path / "c1-old")
            if change == "replaced":
                (workspace / "c1").mkdir()

        self._after_verification(monkeypatch, change_c1)
        with pytest.raises(ProvisionError, match="was moved or replaced"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert _left(tmp_path / "c1-old") == ["drop.qcow2", "keep.qcow2"]

    @pytest.mark.parametrize("change", ["moved", "replaced"])
    def test_a_root_changed_mid_removal_stops_it(self, tmp_path, monkeypatch,
                                                 change):
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "c1/drop.qcow2")
        tree = _scanned(workspace, "c1/keep.qcow2")

        def change_root():
            os.rename(workspace, tmp_path / "ws-old")
            if change == "replaced":
                workspace.mkdir()

        self._after_verification(monkeypatch, change_root)
        with pytest.raises(ProvisionError, match="was moved or replaced"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)

        assert _left(tmp_path / "ws-old" / "c1") == ["drop.qcow2",
                                                     "keep.qcow2"]

    def test_success_is_verified_even_when_nothing_was_left(
            self, tmp_path, monkeypatch):
        """The retained directory is moved out of the workspace during its
        own pass, after its last doomed file went: nothing is left, but what
        destroy says it kept is no longer there."""
        workspace = _tree(tmp_path / "ws", "c1/keep.qcow2", "c1/drop.qcow2",
                          "top.txt")
        tree = _scanned(workspace, "c1/keep.qcow2")
        unlink = os.unlink

        def unlinked(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if path == "drop.qcow2":
                os.rename(workspace / "c1", tmp_path / "c1-elsewhere")

        monkeypatch.setattr(os, "unlink", unlinked)
        with pytest.raises(ProvisionError, match="could not open"):
            BoxmanManager._force_rmtree_except(str(workspace), tree)
        monkeypatch.undo()

        assert (tmp_path / "c1-elsewhere" / "keep.qcow2").exists()


# --------------------------------------------------------------------------
# #221 review R2b, for real: bind mounts in a user and mount namespace
# --------------------------------------------------------------------------

#: what runs in the namespace: builds ``<base>/ws`` keeping c1/keep.qcow2,
#: bind-mounts per *case*, runs the scan and the removal, prints the outcome
_IN_NAMESPACE = r'''
import json, os, subprocess, sys
from boxman.exceptions import ProvisionError
from boxman.utils import retained_tree

case, base = sys.argv[1], os.path.realpath(sys.argv[2])
ws = os.path.join(base, "ws")
keep = os.path.join(ws, "c1", "keep.qcow2")
outside = os.path.join(base, "outside", "unrelated.txt")
for path in (keep, outside, os.path.join(ws, "doomed", "f.txt")):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("data")
os.makedirs(os.path.join(ws, "aa-mounted"))
source, target = {
    "outside": (os.path.dirname(outside), "aa-mounted"),
    "retained": (os.path.join(ws, "c1"), "aa-mounted"),
    "at-scan": (os.path.dirname(outside), "aa-mounted"),
    "a file": (outside, os.path.join("doomed", "f.txt")),
}[case]
target = os.path.join(ws, target)
fallback, result = [], {}
try:
    if case != "at-scan":
        tree = retained_tree.scan_tree(
            ws, lambda path, _: "in use" if path == keep else None)
    subprocess.run(["mount", "--bind", source, target], check=True)
    try:
        if case == "at-scan":
            retained_tree.scan_tree(ws, lambda path, _: None)
        else:
            retained_tree.remove_except(tree, fallback.append)
        result["raised"] = None
    except ProvisionError as exc:
        result["raised"] = str(exc)
    finally:
        subprocess.run(["umount", target], check=True)
finally:
    result["unrelated"] = os.path.exists(outside)
    result["keep"] = os.path.exists(keep)
    result["fallback"] = fallback
    print(json.dumps(result))
'''


@pytest.fixture(scope="module")
def _user_namespaces(tmp_path_factory):
    """Skip unless this user can bind-mount in a user and mount namespace
    (Ubuntu 24.04 forbids unprivileged user namespaces by default)."""
    probe = tmp_path_factory.mktemp("userns")
    (probe / "a").mkdir()
    (probe / "b").mkdir()
    try:
        done = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount",
             "--propagation", "private", "mount", "--bind",
             str(probe / "a"), str(probe / "b")],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"no user namespaces here: {exc}")
    if done.returncode != 0:
        pytest.skip(f"no user namespaces here: {done.stderr.strip()}")


class TestMountPointsForReal:
    """Codex's two reproductions (#221 review R2b), with real bind mounts:
    after the scan, another directory -- one outside the workspace, or the
    retained ``c1`` itself -- is bind-mounted on a doomed one. The removal
    emptied it. Now it stops at the mount point, and nothing on the far side
    of it is touched."""

    @staticmethod
    def _run(tmp_path, case):
        src = os.path.dirname(os.path.dirname(retained_tree.__file__))
        done = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount",
             "--propagation", "private", sys.executable, "-c", _IN_NAMESPACE,
             case, str(tmp_path)],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": os.path.dirname(src),
                 "PYTHONDONTWRITEBYTECODE": "1"})
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout.strip().splitlines()[-1])

    @pytest.mark.parametrize("case", ["outside", "retained"])
    def test_a_directory_mounted_after_the_scan_is_not_entered(
            self, tmp_path, _user_namespaces, case):
        result = self._run(tmp_path, case)

        assert result["raised"]
        assert result["unrelated"] and result["keep"]
        assert result["fallback"] == []

    def test_a_mount_point_found_by_the_scan_stops_it(self, tmp_path,
                                                      _user_namespaces):
        result = self._run(tmp_path, "at-scan")

        assert "unmount it first" in result["raised"]
        assert result["unrelated"]

    def test_the_fallback_never_runs_over_a_mounted_file(self, tmp_path,
                                                         _user_namespaces):
        """A file bind-mounted on a doomed one cannot be unlinked, so it is
        left for the fallback, whose ``rm -rf`` must not be asked to cross
        it."""
        result = self._run(tmp_path, "a file")

        assert "mounted at" in result["raised"]
        assert result["unrelated"] and result["keep"]
        assert result["fallback"] == []
