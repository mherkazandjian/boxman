"""
Tests for file provisioning, deprovision (removal), env.sh skip behaviour,
and extra_args_mode in TaskRunner.
"""

import os
from unittest.mock import patch, MagicMock

import pytest

from boxman.utils.io import write_files
from boxman.manager import BoxmanManager
from boxman.task_runner import TaskRunner


# ---------------------------------------------------------------------------
# write_files: env.sh skip behaviour
# ---------------------------------------------------------------------------


class TestWriteFilesEnvShSkip:

    def test_creates_env_sh_when_not_exists(self, tmp_path):
        write_files({"env.sh": "export FOO=bar\n"}, rootdir=str(tmp_path))
        env_file = tmp_path / "env.sh"
        assert env_file.exists()
        assert env_file.read_text() == "export FOO=bar\n"

    def test_skips_env_sh_when_exists(self, tmp_path):
        env_file = tmp_path / "env.sh"
        env_file.write_text("original content\n")
        write_files({"env.sh": "new content\n"}, rootdir=str(tmp_path))
        assert env_file.read_text() == "original content\n"

    def test_skips_env_sh_in_subdirectory(self, tmp_path):
        subdir = tmp_path / "workspace"
        subdir.mkdir()
        env_file = subdir / "env.sh"
        env_file.write_text("original\n")
        write_files({"env.sh": "new\n"}, rootdir=str(subdir))
        assert env_file.read_text() == "original\n"

    def test_writes_other_files_when_env_sh_skipped(self, tmp_path):
        env_file = tmp_path / "env.sh"
        env_file.write_text("original\n")
        write_files({
            "env.sh": "new\n",
            "ansible.cfg": "[defaults]\n",
        }, rootdir=str(tmp_path))
        assert env_file.read_text() == "original\n"
        assert (tmp_path / "ansible.cfg").read_text() == "[defaults]\n"

    def test_non_special_files_always_overwritten(self, tmp_path):
        """
        Files that are NOT in the preserve-on-exists list (env.sh /
        ansible.cfg — see commit b7fb700) should be overwritten on every
        write_files call. Use inventory/hosts.yml as a representative
        "ordinary" file.
        """
        (tmp_path / "inventory").mkdir()
        hosts = tmp_path / "inventory" / "hosts.yml"
        hosts.write_text("old content\n")
        write_files(
            {"inventory/hosts.yml": "new content\n"},
            rootdir=str(tmp_path),
        )
        assert hosts.read_text() == "new content\n"

    def test_ansible_cfg_preserved_when_exists(self, tmp_path):
        """
        ``ansible.cfg`` is preserved the same way as ``env.sh`` (added
        in commit b7fb700). Regression guard so a future refactor that
        narrows the preserve list doesn't silently clobber user-edited
        ansible.cfg files.
        """
        cfg = tmp_path / "ansible.cfg"
        cfg.write_text("[defaults]\nhost_key_checking = False\n")
        write_files({"ansible.cfg": "[defaults]\n"}, rootdir=str(tmp_path))
        # Original content survives — not overwritten.
        assert "host_key_checking = False" in cfg.read_text()


# ---------------------------------------------------------------------------
# _remove_files and deprovision_files
# ---------------------------------------------------------------------------


class TestRemoveFiles:

    def test_removes_existing_files(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("a")
        f2.write_text("b")
        BoxmanManager._remove_files(
            {"a.txt": "", "b.txt": ""},
            rootdir=str(tmp_path),
        )
        assert not f1.exists()
        assert not f2.exists()

    def test_ignores_nonexistent_files(self, tmp_path):
        # Should not raise
        BoxmanManager._remove_files(
            {"missing.txt": ""},
            rootdir=str(tmp_path),
        )

    def test_removes_files_in_subdirectory(self, tmp_path):
        subdir = tmp_path / "inventory"
        subdir.mkdir()
        hosts = subdir / "hosts.yml"
        hosts.write_text("all:")
        BoxmanManager._remove_files(
            {"inventory/hosts.yml": ""},
            rootdir=str(tmp_path),
        )
        assert not hosts.exists()

    def test_removes_empty_parent_directory(self, tmp_path):
        subdir = tmp_path / "inventory"
        subdir.mkdir()
        hosts = subdir / "hosts.yml"
        hosts.write_text("all:")
        BoxmanManager._remove_files(
            {"inventory/hosts.yml": ""},
            rootdir=str(tmp_path),
        )
        assert not subdir.exists()

    def test_preserves_non_empty_parent_directory(self, tmp_path):
        subdir = tmp_path / "inventory"
        subdir.mkdir()
        (subdir / "hosts.yml").write_text("all:")
        (subdir / "groups.yml").write_text("groups:")
        BoxmanManager._remove_files(
            {"inventory/hosts.yml": ""},
            rootdir=str(tmp_path),
        )
        assert subdir.exists()
        assert (subdir / "groups.yml").exists()

    def test_removes_nested_empty_directories(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        f = nested / "file.txt"
        f.write_text("data")
        BoxmanManager._remove_files(
            {"a/b/c/file.txt": ""},
            rootdir=str(tmp_path),
        )
        assert not f.exists()
        assert not nested.exists()
        assert not (tmp_path / "a" / "b").exists()
        assert not (tmp_path / "a").exists()

    def test_removes_sibling_subdirs_and_parent(self, tmp_path):
        """Simulates the inventory/ case: group_vars/ and host_vars/ are siblings.
        After removing files from both, inventory/ itself should be removed."""
        inv = tmp_path / "inventory"
        (inv / "group_vars").mkdir(parents=True)
        (inv / "host_vars").mkdir(parents=True)
        (inv / "group_vars" / "all.yml").write_text("data")
        (inv / "host_vars" / "node01.yml").write_text("data")
        (inv / "host_vars" / "node02.yml").write_text("data")

        BoxmanManager._remove_files({
            "inventory/group_vars/all.yml": "",
            "inventory/host_vars/node01.yml": "",
            "inventory/host_vars/node02.yml": "",
        }, rootdir=str(tmp_path))

        assert not (inv / "group_vars").exists()
        assert not (inv / "host_vars").exists()
        assert not inv.exists()

    def test_removes_ansible_subdir(self, tmp_path):
        """ansible/site.yml removal should also remove empty ansible/ dir."""
        ansible = tmp_path / "ansible"
        ansible.mkdir()
        (ansible / "site.yml").write_text("---")

        BoxmanManager._remove_files(
            {"ansible/site.yml": ""},
            rootdir=str(tmp_path),
        )
        assert not ansible.exists()

    def test_stops_at_rootdir(self, tmp_path):
        """Cleanup should not try to remove the rootdir itself."""
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "file.txt").write_text("data")

        BoxmanManager._remove_files(
            {"sub/file.txt": ""},
            rootdir=str(tmp_path),
        )
        assert not subdir.exists()
        assert tmp_path.exists()

    def test_stops_at_non_empty_ancestor(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "keep.txt").write_text("keep")
        (tmp_path / "a" / "b").mkdir()
        (tmp_path / "a" / "b" / "remove.txt").write_text("remove")
        BoxmanManager._remove_files(
            {"a/b/remove.txt": ""},
            rootdir=str(tmp_path),
        )
        assert not (tmp_path / "a" / "b").exists()
        assert (tmp_path / "a").exists()
        assert (tmp_path / "a" / "keep.txt").exists()


class TestDeprovisionFiles:

    def test_removes_workspace_and_cluster_files(self, tmp_path):
        ws_path = tmp_path / "workspace"
        ws_path.mkdir()
        cluster_dir = tmp_path / "workspace" / "cluster_1"
        cluster_dir.mkdir()

        (ws_path / "env.sh").write_text("export FOO=1\n")
        (cluster_dir / "env.sh").write_text("export BAR=2\n")

        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {
            "workspace": {
                "path": str(ws_path),
                "files": {"env.sh": "export FOO=1\n"},
            },
            "clusters": {
                "c1": {
                    "workdir": str(cluster_dir),
                    "files": {"env.sh": "export BAR=2\n"},
                },
            },
        }
        mgr.deprovision_files()
        assert not (ws_path / "env.sh").exists()
        assert not (cluster_dir / "env.sh").exists()

    def test_removes_ssh_keys(self, tmp_path):
        ws_path = tmp_path / "workspace"
        ws_path.mkdir()
        cluster_dir = ws_path / "cluster_1"
        cluster_dir.mkdir()

        (ws_path / "id_ed25519_boxman").write_text("private")
        (ws_path / "id_ed25519_boxman.pub").write_text("public")

        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {
            "workspace": {"path": str(ws_path)},
            "clusters": {
                "c1": {"workdir": str(cluster_dir)},
            },
        }
        mgr.deprovision_files()
        assert not (ws_path / "id_ed25519_boxman").exists()
        assert not (ws_path / "id_ed25519_boxman.pub").exists()

    def test_removes_custom_ssh_key_name(self, tmp_path):
        ws_path = tmp_path / "workspace"
        ws_path.mkdir()
        cluster_dir = ws_path / "cluster_1"
        cluster_dir.mkdir()

        (ws_path / "my_key").write_text("private")
        (ws_path / "my_key.pub").write_text("public")

        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {
            "workspace": {"path": str(ws_path)},
            "clusters": {
                "c1": {
                    "workdir": str(cluster_dir),
                    "admin_key_name": "my_key",
                },
            },
        }
        mgr.deprovision_files()
        assert not (ws_path / "my_key").exists()
        assert not (ws_path / "my_key.pub").exists()

    def test_removes_ssh_config(self, tmp_path):
        ws_path = tmp_path / "workspace"
        ws_path.mkdir()
        cluster_dir = ws_path / "cluster_1"
        cluster_dir.mkdir()

        (ws_path / "ssh_config").write_text("Host *\n")

        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {
            "workspace": {"path": str(ws_path)},
            "clusters": {
                "c1": {"workdir": str(cluster_dir)},
            },
        }
        mgr.deprovision_files()
        assert not (ws_path / "ssh_config").exists()

    def test_removes_empty_cluster_workdir(self, tmp_path):
        ws_path = tmp_path / "workspace"
        ws_path.mkdir()
        cluster_dir = ws_path / "cluster_1"
        cluster_dir.mkdir()

        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {
            "workspace": {"path": str(ws_path)},
            "clusters": {
                "c1": {"workdir": str(cluster_dir)},
            },
        }
        mgr.deprovision_files()
        assert not cluster_dir.exists()

    def test_preserves_non_empty_cluster_workdir(self, tmp_path):
        ws_path = tmp_path / "workspace"
        ws_path.mkdir()
        cluster_dir = ws_path / "cluster_1"
        cluster_dir.mkdir()
        (cluster_dir / "keep_me.txt").write_text("important")

        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {
            "workspace": {"path": str(ws_path)},
            "clusters": {
                "c1": {"workdir": str(cluster_dir)},
            },
        }
        mgr.deprovision_files()
        assert cluster_dir.exists()

    def test_no_files_key_is_fine(self, tmp_path):
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {
            "workspace": {"path": str(tmp_path)},
            "clusters": {
                "c1": {"workdir": str(tmp_path)},
            },
        }
        # Should not raise
        mgr.deprovision_files()


# ---------------------------------------------------------------------------
# extra_args_mode: quoted
# ---------------------------------------------------------------------------


class TestExtraArgsMode:

    @pytest.fixture
    def config(self, tmp_path):
        env_file = tmp_path / "env.sh"
        env_file.write_text("export INVENTORY=inv\n")
        return {
            "project": "test",
            "clusters": {"c1": {"workdir": str(tmp_path)}},
            "workspace": {"env_file": str(env_file)},
            "tasks": {},
        }

    def _run(self, config, task_name, extra_args=None):
        with patch("boxman.task_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            runner = TaskRunner(config)
            runner.run(task_name, extra_args=extra_args)
            return mock_run.call_args[0][0]

    def test_quoted_mode_joins_args(self, config):
        config["tasks"]["cmd"] = {
            "description": "ad-hoc",
            "command": "ansible all -m ansible.builtin.shell -a",
            "extra_args_mode": "quoted",
        }
        cmd = self._run(config, "cmd", extra_args=["ip", "a"])
        assert cmd == "ansible all -m ansible.builtin.shell -a 'ip a'"

    def test_quoted_mode_single_arg(self, config):
        config["tasks"]["cmd"] = {
            "description": "ad-hoc",
            "command": "ansible all -m shell -a",
            "extra_args_mode": "quoted",
        }
        cmd = self._run(config, "cmd", extra_args=["hostname"])
        assert cmd == "ansible all -m shell -a hostname"

    def test_quoted_mode_with_special_chars(self, config):
        config["tasks"]["cmd"] = {
            "description": "ad-hoc",
            "command": "ansible all -m shell -a",
            "extra_args_mode": "quoted",
        }
        cmd = self._run(config, "cmd", extra_args=["echo", "hello world"])
        assert cmd == "ansible all -m shell -a 'echo hello world'"

    def test_append_mode_keeps_args_separate(self, config):
        config["tasks"]["site"] = {
            "description": "playbook",
            "command": "ansible-playbook site.yml",
            "extra_args_mode": "append",
        }
        cmd = self._run(config, "site", extra_args=["--tags", "foo"])
        assert cmd == "ansible-playbook site.yml --tags foo"

    def test_default_mode_is_append(self, config):
        config["tasks"]["site"] = {
            "description": "playbook",
            "command": "ansible-playbook site.yml",
        }
        cmd = self._run(config, "site", extra_args=["--tags", "foo"])
        assert cmd == "ansible-playbook site.yml --tags foo"

    def test_no_extra_args_unaffected_by_mode(self, config):
        config["tasks"]["cmd"] = {
            "description": "ad-hoc",
            "command": "echo hello",
            "extra_args_mode": "quoted",
        }
        cmd = self._run(config, "cmd")
        assert cmd == "echo hello"
