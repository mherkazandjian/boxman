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

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from boxman.exceptions import ProvisionError
from boxman.manager import BoxmanManager

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
class TestDestroyVmAndDisksGate:

    FULL_NAME = "bprj__demo__bprj_cluster_1_node01"

    def _manager(self):
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {"project": "demo"}
        mgr.logger = MagicMock()
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
        session.destroy_disks.assert_called_once()

    def test_a_forced_undefine_is_tried_before_giving_up(self):
        mgr, session = self._manager()
        session.confirm_vm_absent.side_effect = [False, True]
        self._run(mgr)
        assert session.destroy_vm.call_args_list == [
            call(self.FULL_NAME),
            call(self.FULL_NAME, force=True),
        ]
        session.destroy_disks.assert_called_once()

    def test_unconfirmed_absence_preserves_the_disks(self):
        """The libvirt-outage case. ``destroy_vm`` reports success because
        its own probe reads a failed query as "not defined", so the disks
        would have been unlinked out from under a live guest."""
        mgr, session = self._manager()
        session.destroy_vm.return_value = True      # optimistic, and wrong
        session.confirm_vm_absent.return_value = False
        with pytest.raises(ProvisionError, match="could not confirm"):
            self._run(mgr)
        session.destroy_disks.assert_not_called()


class TestDestroyRemovedVmGate:

    def _manager(self):
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {"project": "demo"}
        mgr.logger = MagicMock()
        mgr.provider = MagicMock()
        mgr._vm_disk_dirs = MagicMock(return_value=["/ws/c1"])
        return mgr

    def test_unconfirmed_absence_preserves_the_disks(self):
        mgr = self._manager()
        mgr.provider.destroy_vm.return_value = True
        mgr.provider.confirm_vm_absent.return_value = False
        with pytest.raises(ProvisionError, match="could not confirm"):
            mgr._destroy_removed_vm("bprj__demo__bprj_cluster_1_old01")
        mgr.provider.destroy_disks.assert_not_called()

    def test_disk_dirs_are_read_before_the_domain_is_undefined(self):
        """``domblklist`` returns nothing once the domain is gone."""
        mgr = self._manager()
        mgr.provider.confirm_vm_absent.return_value = True
        parent = MagicMock()
        parent.attach_mock(mgr._vm_disk_dirs, "disk_dirs")
        parent.attach_mock(mgr.provider.destroy_vm, "destroy_vm")
        mgr._destroy_removed_vm("bprj__demo__bprj_cluster_1_old01")
        assert [c[0] for c in parent.mock_calls][:2] == [
            "disk_dirs", "destroy_vm"]


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
        mgr._confirm_project_torn_down = MagicMock(return_value=(True, ''))
        return mgr, workspace

    def test_a_clean_teardown_removes_everything_and_unregisters_last(
            self, tmp_path):
        mgr, workspace = self._manager(tmp_path)
        order = MagicMock()
        order.attach_mock(mgr.deprovision_files, "deprovision_files")
        order.attach_mock(mgr._force_rmtree, "force_rmtree")
        order.attach_mock(mgr.unregister_from_cache, "unregister")

        mgr.destroy(ARGS)

        mgr._force_rmtree.assert_any_call(str(workspace))
        mgr.deprovision_files.assert_called_once()
        mgr.unregister_from_cache.assert_called_once()
        # Order matters: unregistering last is what keeps a failed deletion
        # recoverable, so assert it rather than just the call counts.
        assert [c[0] for c in order.mock_calls] == [
            "deprovision_files", "force_rmtree", "unregister"]

    def test_a_failed_deprovision_preserves_everything(self, tmp_path):
        mgr, _workspace = self._manager(tmp_path)
        mgr.deprovision.side_effect = ProvisionError(
            "deprovision did not complete — VMs are still defined: node01")

        with pytest.raises(ProvisionError, match="destroy did not complete"):
            mgr.destroy(ARGS)

        mgr._force_rmtree.assert_not_called()
        mgr.deprovision_files.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    def test_a_failed_compose_destroy_preserves_everything(self, tmp_path):
        """``destroy_compose_clusters`` (down --volumes) is a different
        operation from the one deprovision ran, so it needs its own gate."""
        mgr, _workspace = self._manager(tmp_path)
        mgr.destroy_compose_clusters.side_effect = RuntimeError("volumes busy")

        with pytest.raises(ProvisionError, match="docker-compose teardown"):
            mgr.destroy(ARGS)

        mgr._force_rmtree.assert_not_called()
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
        mgr.unregister_from_cache.assert_not_called()

    def test_survivors_preserve_everything(self, tmp_path):
        mgr, _workspace = self._manager(tmp_path)
        mgr._confirm_project_torn_down = MagicMock(
            return_value=(False, "VMs are still defined: node01"))

        with pytest.raises(ProvisionError, match="still defined"):
            mgr.destroy(ARGS)

        mgr._force_rmtree.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    def test_a_failed_workspace_removal_keeps_the_project_registered(
            self, tmp_path):
        """Unregistering last is what makes a failed deletion recoverable:
        the leftovers stay visible to ``boxman list``."""
        mgr, workspace = self._manager(tmp_path)
        mgr._force_rmtree.side_effect = ProvisionError(
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
