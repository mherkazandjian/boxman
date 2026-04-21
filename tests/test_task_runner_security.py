"""
Security-hardening tests for boxman.task_runner.

Added in Phase 2.8 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).

Pin the behavior of :func:`_validate_safe_name` and the guard in
``ssh_to_host`` — a regression that dropped the validation would
re-open a shell-metacharacter injection path for externally-derived
VM / host names.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from boxman.task_runner import SAFE_NAME_RE, TaskRunner, _validate_safe_name


pytestmark = pytest.mark.unit


class TestSafeNameRegex:

    @pytest.mark.parametrize("name", [
        "vm01",
        "cluster-name",
        "node_01",
        "with.dot",
        "a",
        "prj1_cluster1_vm1",
        "ansible.cfg",
        "1234",
    ])
    def test_accepts_safe_names(self, name: str):
        assert SAFE_NAME_RE.fullmatch(name) is not None

    @pytest.mark.parametrize("name", [
        "",
        "vm;rm -rf /",
        "vm$(id)",
        "vm`whoami`",
        "vm | cat",
        "vm with space",
        "vm\nother",
        "vm&sleep 1",
        "vm>output",
        "vm<input",
        "vm*glob",
        "vm?glob",
        "vm!bang",
        "vm\"quoted",
        "vm'quoted",
        "vm\\escape",
        "../traversal",
        "/absolute",
    ])
    def test_rejects_unsafe_names(self, name: str):
        assert SAFE_NAME_RE.fullmatch(name) is None


class TestValidateSafeName:

    def test_accepts_valid_name(self):
        _validate_safe_name("vm01", "vm")   # must not raise

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="unsafe vm"):
            _validate_safe_name("", kind="vm")

    def test_shell_metachar_raises(self):
        with pytest.raises(ValueError, match="must match"):
            _validate_safe_name("vm; rm -rf /", kind="vm")

    def test_command_substitution_raises(self):
        with pytest.raises(ValueError):
            _validate_safe_name("vm$(id)", kind="vm")

    def test_error_mentions_the_offending_value(self):
        with pytest.raises(ValueError) as excinfo:
            _validate_safe_name("bad;name", kind="vm")
        assert "bad;name" in str(excinfo.value)

    def test_custom_kind_in_error_message(self):
        with pytest.raises(ValueError, match="unsafe cluster"):
            _validate_safe_name("bad;name", kind="cluster")


class TestSshToHostGuard:

    def _runner(self) -> TaskRunner:
        config = {
            "workspace": {"workdir": "/tmp"},
            "clusters": {
                "c1": {
                    "workdir": "/tmp",
                    "admin_user": "admin",
                    "vms": {"vm1": {}},
                }
            },
        }
        runner = TaskRunner(config, cluster_name="c1")
        # Avoid loading a real env file
        runner._env = {"SSH_CONFIG": "/tmp/ssh_config"}
        runner._workspace_vars = {}
        return runner

    def test_malicious_vm_name_raises_before_subprocess(self):
        runner = self._runner()
        with patch("boxman.task_runner.subprocess.run") as fake_run:
            with pytest.raises(ValueError, match="unsafe"):
                runner.ssh_to_host("vm01; rm -rf /")
        # The shell was never invoked
        fake_run.assert_not_called()

    def test_valid_vm_name_allows_subprocess_run(self):
        runner = self._runner()
        with patch("boxman.task_runner.subprocess.run") as fake_run:
            fake_run.return_value.returncode = 0
            assert runner.ssh_to_host("vm01") == 0
        fake_run.assert_called_once()
