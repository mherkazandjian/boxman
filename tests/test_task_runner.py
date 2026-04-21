"""
Tests for boxman.utils.env_loader and boxman.task_runner.
"""

import os
import stat
import subprocess
import textwrap
import types
from unittest.mock import patch, MagicMock

import pytest

from boxman.utils.env_loader import source_env_file, load_workspace_env
from boxman.task_runner import TaskRunner
from boxman.manager import BoxmanManager


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

    @patch("boxman.task_runner.subprocess.run")
    def test_run_command_adhoc(self, mock_run, basic_config, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        runner = TaskRunner(basic_config)
        exit_code = runner.run_command("whoami")
        assert exit_code == 0
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd == 'ansible all -m ansible.builtin.shell -a "whoami"'

    @patch("boxman.task_runner.subprocess.run")
    def test_run_command_adhoc_with_ansible_flags(self, mock_run, basic_config, tmp_path):
        mock_run.return_value = MagicMock(returncode=0)
        runner = TaskRunner(basic_config)
        exit_code = runner.run_command("whoami", ansible_flags="--limit node01")
        assert exit_code == 0
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd == 'ansible all -m ansible.builtin.shell -a "whoami" --limit node01'

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

    def test_extract_placeholders(self):
        assert TaskRunner.extract_placeholders(
            "ansible all {flags} -m shell {more_flags} -a"
        ) == ["flags", "more_flags"]
        assert TaskRunner.extract_placeholders("echo hello") == []
        assert TaskRunner.extract_placeholders("{a} {b} {c}") == ["a", "b", "c"]

    @patch("boxman.task_runner.subprocess.run")
    def test_run_with_task_flags(self, mock_run, basic_config):
        mock_run.return_value = MagicMock(returncode=0)
        basic_config["tasks"]["cmd"] = {
            "description": "run a shell command",
            "command": "ansible all {flags} -m ansible.builtin.shell {more_flags} -a",
        }
        runner = TaskRunner(basic_config)
        exit_code = runner.run(
            "cmd",
            extra_args=["hostname"],
            task_flags={"flags": "--limit node01", "more_flags": "--become"},
        )
        assert exit_code == 0
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd == (
            "ansible all --limit node01 -m ansible.builtin.shell --become -a hostname"
        )

    @patch("boxman.task_runner.subprocess.run")
    def test_run_with_missing_task_flags(self, mock_run, basic_config):
        """Unfilled placeholders are removed cleanly."""
        mock_run.return_value = MagicMock(returncode=0)
        basic_config["tasks"]["cmd"] = {
            "description": "run a shell command",
            "command": "ansible all {flags} -m ansible.builtin.shell -a",
        }
        runner = TaskRunner(basic_config)
        exit_code = runner.run("cmd", extra_args=["hostname"])
        assert exit_code == 0
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd == "ansible all -m ansible.builtin.shell -a hostname"

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


# ---------------------------------------------------------------------------
# Flag-passing scenarios: {flags}, {more_flags}, and -- extra args
# ---------------------------------------------------------------------------


class TestTaskRunnerFlagsPassing:
    """
    Tests for {placeholder} flag substitution and -- extra_args in task commands.

    Covers the scenarios documented in task_runner.py::

        boxman run cmd --flags "--limit node01" --more-flags "--become" -- hostname
        # resolves to:
        # ansible all --limit node01 -m ansible.builtin.shell --become -a hostname
    """

    @pytest.fixture
    def config(self, tmp_path):
        """Minimal config with tasks that use {placeholder} markers."""
        env_file = tmp_path / "env.sh"
        env_file.write_text("export INVENTORY=inv\n")
        return {
            "project": "flagtest",
            "clusters": {"c1": {"workdir": str(tmp_path)}},
            "workspace": {"env_file": str(env_file)},
            "tasks": {},
        }

    def _add_task(self, config, name, command):
        config["tasks"][name] = {"description": name, "command": command}

    def _run(self, config, task_name, extra_args=None, task_flags=None):
        with patch("boxman.task_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            runner = TaskRunner(config)
            runner.run(task_name, extra_args=extra_args, task_flags=task_flags)
            return mock_run.call_args[0][0]

    # ------------------------------------------------------------------
    # 1. Both {flags} and {more_flags} filled
    # ------------------------------------------------------------------

    def test_both_flags_filled(self, config):
        """Both placeholders filled → values interpolated in place."""
        self._add_task(
            config, "cmd",
            "ansible all {flags} -m ansible.builtin.shell {more_flags} -a",
        )
        cmd = self._run(
            config, "cmd",
            extra_args=["hostname"],
            task_flags={"flags": "--limit node01", "more_flags": "--become"},
        )
        assert cmd == (
            "ansible all --limit node01 -m ansible.builtin.shell --become -a hostname"
        )

    # ------------------------------------------------------------------
    # 2. Only {flags} filled; {more_flags} absent → collapsed cleanly
    # ------------------------------------------------------------------

    def test_only_first_flag_filled(self, config):
        """{more_flags} not provided → removed, no double-space left behind."""
        self._add_task(
            config, "cmd",
            "ansible all {flags} -m ansible.builtin.shell {more_flags} -a",
        )
        cmd = self._run(
            config, "cmd",
            extra_args=["hostname"],
            task_flags={"flags": "--limit node01"},
        )
        assert cmd == (
            "ansible all --limit node01 -m ansible.builtin.shell -a hostname"
        )

    # ------------------------------------------------------------------
    # 3. Neither placeholder provided → both removed, no double-spaces
    # ------------------------------------------------------------------

    def test_no_flags_provided(self, config):
        """No task_flags passed → all placeholders removed, spacing normalised."""
        self._add_task(
            config, "cmd",
            "ansible all {flags} -m ansible.builtin.shell {more_flags} -a",
        )
        cmd = self._run(config, "cmd", extra_args=["hostname"])
        assert cmd == "ansible all -m ansible.builtin.shell -a hostname"

    # ------------------------------------------------------------------
    # 4. Flag value contains spaces (multi-word value)
    # ------------------------------------------------------------------

    def test_flag_value_with_spaces(self, config):
        """A flag value like '--limit node01 node02' is passed as a single token."""
        self._add_task(config, "cmd", "ansible all {flags} -m ping")
        cmd = self._run(
            config, "cmd",
            task_flags={"flags": "--limit node01 node02"},
        )
        assert cmd == "ansible all --limit node01 node02 -m ping"

    # ------------------------------------------------------------------
    # 5. Flag value contains dashes (--dry-run style options)
    # ------------------------------------------------------------------

    def test_flag_value_with_dashes(self, config):
        """Flag values that are themselves dash-prefixed options work correctly."""
        self._add_task(config, "cmd", "ansible-playbook {flags} site.yml")
        cmd = self._run(
            config, "cmd",
            task_flags={"flags": "--dry-run --check"},
        )
        assert cmd == "ansible-playbook --dry-run --check site.yml"

    # ------------------------------------------------------------------
    # 6. -- extra_args only; command has no {placeholder}
    # ------------------------------------------------------------------

    def test_double_dash_extra_args_no_placeholders(self, config):
        """
        Multiple tokens after -- are joined and quoted as ONE shell argument
        when ``extra_args_mode: quoted`` is set on the task. This is the
        correct behaviour for ansible -a which takes a single string.

            boxman run cmd -- curl ifconfig.me
            → ansible ... -a 'curl ifconfig.me'   ✓  (-a gets one arg)
        """
        self._add_task(config, "cmd", "ansible all -m ansible.builtin.shell -a")
        config["tasks"]["cmd"]["extra_args_mode"] = "quoted"
        cmd = self._run(
            config, "cmd",
            extra_args=["curl", "ifconfig.me"],
        )
        assert cmd == "ansible all -m ansible.builtin.shell -a 'curl ifconfig.me'"

    def test_extra_arg_single_word_no_quoting(self, config):
        """A single, simple extra_arg needs no quoting."""
        self._add_task(config, "site", "ansible-playbook site.yml")
        cmd = self._run(config, "site", extra_args=["hostname"])
        assert cmd == "ansible-playbook site.yml hostname"

    def test_extra_arg_with_embedded_space_is_shell_quoted(self, config):
        """
        Under ``extra_args_mode: quoted``, a single extra_arg that
        already contains a space (from bash 'curl ifconfig.me') is
        also quoted correctly.
        """
        self._add_task(config, "cmd", "ansible all -m ansible.builtin.shell -a")
        config["tasks"]["cmd"]["extra_args_mode"] = "quoted"
        cmd = self._run(
            config, "cmd",
            extra_args=["curl ifconfig.me"],  # single arg with a space
        )
        assert cmd == "ansible all -m ansible.builtin.shell -a 'curl ifconfig.me'"

    # ------------------------------------------------------------------
    # 7. Both {flags} task_flags AND -- extra_args combined
    # ------------------------------------------------------------------

    def test_flags_and_double_dash_extra_args_combined(self, config):
        """
        Simulates: boxman run cmd --flags "--limit node01" -- hostname
        {flags} is replaced; then extra_args are appended at the end.
        """
        self._add_task(
            config, "cmd",
            "ansible all {flags} -m ansible.builtin.shell -a",
        )
        cmd = self._run(
            config, "cmd",
            extra_args=["hostname"],
            task_flags={"flags": "--limit node01"},
        )
        assert cmd == "ansible all --limit node01 -m ansible.builtin.shell -a hostname"

    # ------------------------------------------------------------------
    # 8. {placeholder} at end of command (no trailing text)
    # ------------------------------------------------------------------

    def test_placeholder_at_end_of_command(self, config):
        """Placeholder at the very end is replaced without trailing space."""
        self._add_task(config, "cmd", "echo {message}")
        cmd = self._run(config, "cmd", task_flags={"message": "hello world"})
        assert cmd == "echo hello world"

    def test_placeholder_at_end_missing(self, config):
        """Missing placeholder at the very end leaves no trailing space."""
        self._add_task(config, "cmd", "echo {message}")
        cmd = self._run(config, "cmd")
        assert cmd == "echo"

    # ------------------------------------------------------------------
    # 9. Three placeholders; only the middle one filled
    # ------------------------------------------------------------------

    def test_three_placeholders_only_middle_filled(self, config):
        """First and last placeholders absent; middle filled → clean result."""
        self._add_task(
            config, "cmd",
            "{pre_flags} ansible all {flags} -m ping {post_flags}",
        )
        cmd = self._run(config, "cmd", task_flags={"flags": "--limit node01"})
        assert cmd == "ansible all --limit node01 -m ping"

    # ------------------------------------------------------------------
    # 10. Flag value is an empty string (explicitly silencing a placeholder)
    # ------------------------------------------------------------------

    def test_flag_value_empty_string(self, config):
        """Explicitly passing '' for a flag removes the placeholder cleanly."""
        self._add_task(
            config, "cmd",
            "ansible all {flags} -m ping",
        )
        cmd = self._run(config, "cmd", task_flags={"flags": ""})
        assert cmd == "ansible all -m ping"

    # ------------------------------------------------------------------
    # 11. Hyphen-to-underscore key mapping (mirrors CLI behaviour in manager.py)
    # ------------------------------------------------------------------

    def test_hyphen_key_does_not_match_underscore_placeholder(self, config):
        """
        TaskRunner.run() receives the already-normalised key (underscore).
        The CLI layer (manager.py) converts --more-flags → more_flags before
        calling run().  A key still containing a hyphen would NOT match the
        {more_flags} placeholder.
        """
        self._add_task(
            config, "cmd",
            "ansible all {more_flags} -m ping",
        )
        # hyphen key → no match → placeholder removed
        cmd_no_match = self._run(
            config, "cmd",
            task_flags={"more-flags": "--limit node01"},
        )
        assert cmd_no_match == "ansible all -m ping"

        # underscore key → matches → value inserted
        cmd_match = self._run(
            config, "cmd",
            task_flags={"more_flags": "--limit node01"},
        )
        assert cmd_match == "ansible all --limit node01 -m ping"

    # ------------------------------------------------------------------
    # 12. -- extra_args with multiple tokens; flags also present
    # ------------------------------------------------------------------

    def test_multiple_extra_args_joined_as_one(self, config):
        """
        Simulates: boxman run cmd --flags "-v" -- arg1 arg2 arg3
        Under ``extra_args_mode: quoted``, all post-'--' tokens are joined
        and quoted as a single shell argument.
        """
        self._add_task(config, "cmd", "mycommand {flags}")
        config["tasks"]["cmd"]["extra_args_mode"] = "quoted"
        cmd = self._run(
            config, "cmd",
            extra_args=["arg1", "arg2", "arg3"],
            task_flags={"flags": "-v"},
        )
        assert cmd == "mycommand -v 'arg1 arg2 arg3'"


# ---------------------------------------------------------------------------
# Manager-level flag parsing: argparse positional mis-classification fix
#
# Python's argparse._parse_optional() returns None (positional) for any
# argument string that contains a space.  This means a bash-quoted value
# like '--limit cluster_1_control01' (a single token with a space) lands
# in cli_args.extra_args instead of remaining_args, which caused:
#
#   boxman run cmd --flags '--limit cluster_1_control01' -- hostname
#   → ERROR: argument --flags: expected a value
#
# The tests below simulate the exact (remaining_args, extra_args) pairs
# that argparse produces and verify that BoxmanManager.run_task correctly
# recovers the flag value from extra_args when necessary.
# ---------------------------------------------------------------------------


def _make_cli_args(task_name, remaining_args, extra_args, cluster=None):
    """Build a minimal Namespace that BoxmanManager.run_task expects."""
    ns = types.SimpleNamespace(
        task_name=task_name,
        remaining_args=remaining_args,
        extra_args=list(extra_args),
        cmd=None,
        list_tasks=False,
        cluster=cluster,
    )
    return ns


class TestRunTaskArgparseMisclassification:
    """
    Tests for the argparse positional-mis-classification workaround in
    BoxmanManager.run_task.

    In each scenario we construct the exact (remaining_args, extra_args)
    that argparse produces for a given CLI invocation and verify that the
    correct command is assembled by TaskRunner.run().
    """

    @pytest.fixture
    def manager(self, tmp_path):
        env_file = tmp_path / "env.sh"
        env_file.write_text("export INVENTORY=inv\n")
        config = {
            "project": "flagtest",
            "clusters": {"c1": {"workdir": str(tmp_path)}},
            "workspace": {"env_file": str(env_file)},
            "tasks": {
                "cmd": {
                    "description": "run ansible",
                    "command": "ansible all {flags} -m ansible.builtin.shell {more_flags} -a",
                },
            },
        }
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = config
        mgr.logger = MagicMock()
        return mgr

    def _run_task(self, manager, cli_args):
        """Invoke run_task and return the command string passed to subprocess."""
        with patch("boxman.task_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            BoxmanManager.run_task(manager, cli_args)
            return mock_run.call_args[0][0]

    # ------------------------------------------------------------------
    # 1. Value contains a space → argparse puts it in extra_args
    #    CLI: boxman run cmd --flags '--limit cluster_1_control01' -- hostname
    # ------------------------------------------------------------------

    def test_space_in_flag_value_single_flag(self, manager):
        """
        argparse classifies '--limit cluster_1_control01' (contains space)
        as a positional, so remaining=['--flags'] and extra_args starts with
        the misplaced value.  run_task must recover the value from extra_args.
        """
        cli_args = _make_cli_args(
            task_name="cmd",
            remaining_args=["--flags"],
            extra_args=["--limit cluster_1_control01", "hostname"],
        )
        cmd = self._run_task(manager, cli_args)
        assert cmd == (
            "ansible all --limit cluster_1_control01 -m ansible.builtin.shell -a hostname"
        )

    # ------------------------------------------------------------------
    # 2. Two flags; first value has a space (misclassified), second is ok
    #    CLI: boxman run cmd --flags '--limit node01' --more-flags '--become' -- hostname
    # ------------------------------------------------------------------

    def test_first_flag_value_misclassified_second_normal(self, manager):
        """
        '--limit node01' has a space → extra_args[0].
        '--become' has no space → stays in remaining as the value of --more-flags.
        remaining=['--flags', '--more-flags', '--become']
        extra_args=['--limit node01', 'hostname']
        """
        cli_args = _make_cli_args(
            task_name="cmd",
            remaining_args=["--flags", "--more-flags", "--become"],
            extra_args=["--limit node01", "hostname"],
        )
        cmd = self._run_task(manager, cli_args)
        assert cmd == (
            "ansible all --limit node01 -m ansible.builtin.shell --become -a hostname"
        )

    # ------------------------------------------------------------------
    # 3. Both flag values contain spaces → both misclassified
    #    CLI: boxman run cmd --flags '--limit node01' --more-flags '--become --check' -- hostname
    # ------------------------------------------------------------------

    def test_both_flag_values_misclassified(self, manager):
        """
        Both '--limit node01' and '--become --check' contain spaces.
        remaining=['--flags', '--more-flags']
        extra_args=['--limit node01', '--become --check', 'hostname']
        """
        cli_args = _make_cli_args(
            task_name="cmd",
            remaining_args=["--flags", "--more-flags"],
            extra_args=["--limit node01", "--become --check", "hostname"],
        )
        cmd = self._run_task(manager, cli_args)
        assert cmd == (
            "ansible all --limit node01 -m ansible.builtin.shell --become --check -a hostname"
        )

    # ------------------------------------------------------------------
    # 4. Normal case: value starts with -- but has no space → stays in remaining
    #    CLI: boxman run cmd --flags '--limit' -- hostname
    # ------------------------------------------------------------------

    def test_flag_value_starts_with_dashes_no_space(self, manager):
        """
        '--limit' has no space → argparse keeps it in remaining.
        remaining=['--flags', '--limit']
        extra_args=['hostname']
        This tests the normal parsing path is unaffected by the fix.
        """
        cli_args = _make_cli_args(
            task_name="cmd",
            remaining_args=["--flags", "--limit"],
            extra_args=["hostname"],
        )
        cmd = self._run_task(manager, cli_args)
        assert cmd == (
            "ansible all --limit -m ansible.builtin.shell -a hostname"
        )

    # ------------------------------------------------------------------
    # 5. Both flags normal (no spaces in values, both start with --)
    #    CLI: boxman run cmd --flags '--limit' --more-flags '--become' -- hostname
    # ------------------------------------------------------------------

    def test_both_flags_normal_dashed_values(self, manager):
        """
        Neither value has a space; both land in remaining correctly.
        remaining=['--flags', '--limit', '--more-flags', '--become']
        extra_args=['hostname']
        """
        cli_args = _make_cli_args(
            task_name="cmd",
            remaining_args=["--flags", "--limit", "--more-flags", "--become"],
            extra_args=["hostname"],
        )
        cmd = self._run_task(manager, cli_args)
        assert cmd == (
            "ansible all --limit -m ansible.builtin.shell --become -a hostname"
        )
