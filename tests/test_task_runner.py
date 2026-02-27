"""
Tests for boxman.utils.env_loader and boxman.task_runner.
"""

import os
import stat
import subprocess
import textwrap

import pytest

from boxman.utils.env_loader import source_env_file, load_workspace_env
from boxman.task_runner import TaskRunner


# ---------------------------------------------------------------------------
# env_loader tests
# ---------------------------------------------------------------------------


class TestSourceEnvFile:

    def test_sources_exported_vars(self, tmp_path):
        env_file = tmp_path / "env.sh"
        env_file.write_text(
            "export MY_TEST_VAR=hello_world\n"
            "export MY_OTHER_VAR=42\n"
        )
        result = source_env_file(str(env_file))
        assert result["MY_TEST_VAR"] == "hello_world"
        assert result["MY_OTHER_VAR"] == "42"

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError, match="env file not found"):
            source_env_file("/nonexistent/env.sh")

    def test_expands_tilde(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        env_file = tmp_path / "env.sh"
        env_file.write_text("export TILDE_TEST=works\n")
        result = source_env_file("~/env.sh")
        assert result["TILDE_TEST"] == "works"

    def test_only_returns_new_or_changed_vars(self, tmp_path, monkeypatch):
        # Set a var that the env file will NOT change
        monkeypatch.setenv("EXISTING_UNCHANGED", "original")
        env_file = tmp_path / "env.sh"
        env_file.write_text(
            "export EXISTING_UNCHANGED=original\n"  # same value
            "export BRAND_NEW_VAR=new\n"
        )
        result = source_env_file(str(env_file))
        assert "EXISTING_UNCHANGED" not in result
        assert result["BRAND_NEW_VAR"] == "new"

    def test_handles_values_with_spaces(self, tmp_path):
        env_file = tmp_path / "env.sh"
        env_file.write_text('export SPACED_VAR="hello world"\n')
        result = source_env_file(str(env_file))
        assert result["SPACED_VAR"] == "hello world"

    def test_handles_shell_expansion(self, tmp_path):
        env_file = tmp_path / "env.sh"
        env_file.write_text("export EXPANDED_VAR=${HOME}/subdir\n")
        result = source_env_file(str(env_file))
        assert result["EXPANDED_VAR"] == os.environ["HOME"] + "/subdir"

    def test_bad_script_raises_runtime_error(self, tmp_path):
        env_file = tmp_path / "bad.sh"
        env_file.write_text("exit 1\n")
        with pytest.raises(RuntimeError, match="failed to source"):
            source_env_file(str(env_file))


class TestLoadWorkspaceEnv:

    def test_loads_from_explicit_env_file(self, tmp_path):
        env_file = tmp_path / "env.sh"
        env_file.write_text("export INVENTORY=my_inventory\n")
        cluster = {"workdir": str(tmp_path)}
        workspace = {"env_file": str(env_file)}
        result = load_workspace_env(cluster, workspace)
        assert result["INVENTORY"] == "my_inventory"

    def test_falls_back_to_workspace_files_env_sh(self, tmp_path):
        env_file = tmp_path / "env.sh"
        env_file.write_text("export SSH_CONFIG=my_ssh_config\n")
        cluster = {"workdir": str(tmp_path)}
        workspace = {
            "path": str(tmp_path),
            "files": {"env.sh": "export SSH_CONFIG=my_ssh_config\n"},
        }
        result = load_workspace_env(cluster, workspace_config=workspace)
        assert result["SSH_CONFIG"] == "my_ssh_config"

    def test_explicit_overrides_take_precedence(self, tmp_path):
        env_file = tmp_path / "env.sh"
        env_file.write_text("export GATEWAYHOST=from_env\n")
        cluster = {"workdir": str(tmp_path)}
        workspace = {
            "env_file": str(env_file),
            "gateway_host": "from_override",
        }
        result = load_workspace_env(cluster, workspace)
        assert result["GATEWAYHOST"] == "from_override"

    def test_no_env_file_returns_only_overrides(self, tmp_path):
        cluster = {"workdir": str(tmp_path)}
        workspace = {"salt_master": "salt01"}
        result = load_workspace_env(cluster, workspace)
        assert result["SALTMASTER"] == "salt01"
        assert len(result) == 1


# ---------------------------------------------------------------------------
# TaskRunner tests
# ---------------------------------------------------------------------------


class TestTaskRunner:

    @pytest.fixture
    def basic_config(self, tmp_path):
        env_file = tmp_path / "env.sh"
        env_file.write_text(
            "export INVENTORY=test_inventory\n"
            "export SSH_CONFIG=test_ssh_config\n"
        )
        return {
            "project": "testprj",
            "clusters": {
                "cluster_1": {
                    "workdir": str(tmp_path),
                    "files": {"env.sh": "..."},
                }
            },
            "workspace": {
                "env_file": str(env_file),
            },
            "tasks": {
                "ping": {
                    "description": "ping all hosts",
                    "command": "echo pinging with ${INVENTORY}",
                },
                "greet": {
                    "description": "say hello",
                    "command": "echo hello",
                },
            },
        }

    def test_list_tasks(self, basic_config):
        runner = TaskRunner(basic_config)
        tasks = runner.list_tasks()
        assert len(tasks) == 2
        names = {t["name"] for t in tasks}
        assert names == {"ping", "greet"}

    def test_run_nonexistent_task_raises(self, basic_config):
        runner = TaskRunner(basic_config)
        with pytest.raises(KeyError, match="not found"):
            runner.run("nonexistent")

    def test_run_task_executes_command(self, basic_config, tmp_path):
        runner = TaskRunner(basic_config)
        exit_code = runner.run("greet")
        assert exit_code == 0

    def test_run_task_has_env_vars(self, basic_config, tmp_path):
        # Change the task to output env var to a file for verification
        out_file = tmp_path / "out.txt"
        basic_config["tasks"]["check_env"] = {
            "description": "check env",
            "command": f"echo $INVENTORY > {out_file}",
        }
        runner = TaskRunner(basic_config)
        runner.run("check_env")
        assert out_file.read_text().strip() == "test_inventory"

    def test_run_task_with_extra_args(self, basic_config, tmp_path):
        out_file = tmp_path / "args_out.txt"
        basic_config["tasks"]["echo_args"] = {
            "description": "echo args",
            "command": f"echo",
        }
        runner = TaskRunner(basic_config)
        exit_code = runner.run("echo_args", extra_args=["hello", "world"])
        assert exit_code == 0

    def test_run_command_adhoc(self, basic_config, tmp_path):
        out_file = tmp_path / "adhoc_out.txt"
        runner = TaskRunner(basic_config)
        exit_code = runner.run_command(f"echo adhoc > {out_file}")
        assert exit_code == 0
        assert out_file.read_text().strip() == "adhoc"

    def test_sets_infra_from_project(self, basic_config):
        runner = TaskRunner(basic_config)
        assert runner.env.get("INFRA") == "testprj"

    def test_cluster_selection(self, basic_config):
        runner = TaskRunner(basic_config, cluster_name="cluster_1")
        assert runner.cluster_name == "cluster_1"

    def test_invalid_cluster_raises(self, basic_config):
        with pytest.raises(ValueError, match="not found"):
            TaskRunner(basic_config, cluster_name="nonexistent")

    def test_no_tasks_section(self, tmp_path):
        config = {
            "project": "empty",
            "clusters": {
                "c1": {"workdir": str(tmp_path)},
            },
        }
        runner = TaskRunner(config)
        assert runner.list_tasks() == []

    def test_task_workdir(self, basic_config, tmp_path):
        """Task-level workdir overrides cluster workdir."""
        task_dir = tmp_path / "taskwd"
        task_dir.mkdir()
        out_file = tmp_path / "pwd_out.txt"
        basic_config["tasks"]["check_pwd"] = {
            "description": "check pwd",
            "command": f"pwd > {out_file}",
            "workdir": str(task_dir),
        }
        runner = TaskRunner(basic_config)
        runner.run("check_pwd")
        assert out_file.read_text().strip() == str(task_dir)
