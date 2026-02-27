"""
Task runner for executing named tasks defined in boxman conf.yml.

Tasks are shell commands defined in the ``tasks`` section of a project's
conf.yml.  They run with environment variables loaded from the workspace
env file, giving them access to variables like INVENTORY, SSH_CONFIG,
GATEWAYHOST, etc.

Task commands may contain ``{{ placeholder }}`` markers that are filled
from CLI flags at invocation time.  Placeholder names use underscores;
the corresponding CLI flags use hyphens::

    # conf.yml
    tasks:
      cmd:
        description: "run a shell command on all hosts"
        command: ansible all {{ flags }} -m ansible.builtin.shell {{ more_flags }} -a

    # CLI
    boxman run cmd --flags "--limit node01" --more-flags "--become" -- hostname

    # resolves to:
    # ansible all --limit node01 -m ansible.builtin.shell --become -a hostname

Placeholders that are not provided on the CLI are removed (replaced with
the empty string).  The ``{{ name }}`` syntax is converted to ``{name}``
before Jinja2 rendering so it does not conflict with Jinja2 expressions
like ``{{ env("VAR") }}``.

Example conf.yml::

    workspace:
      env_file: ~/workspaces/myinfra/env.sh

    tasks:
      ping:
        description: "Ping all hosts via ansible"
        command: ansible all {{ flags }} -m ansible.builtin.ping

      site:
        description: "Run ansible site playbook"
        command: ansible-playbook --become ansible/site.yml

Usage::

    boxman run ping
    boxman run ping --flags "--limit node01"
    boxman run site -- --limit foo --tags=bar
"""

import os
import re
import subprocess
import sys
from typing import Dict, Any, List, Optional

from boxman import log
from boxman.utils.env_loader import load_workspace_env


class TaskRunner:
    """
    Resolves and executes tasks from a boxman project configuration.

    Args:
        config: The full project configuration (parsed conf.yml).
        cluster_name: Optional cluster name to scope the workspace env to.
            If not given, uses the first cluster.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        cluster_name: Optional[str] = None,
    ):
        self.config = config
        self.tasks: Dict[str, Dict[str, Any]] = config.get("tasks", {})
        self.workspace_config: Dict[str, Any] = config.get("workspace", {})

        # Resolve cluster for env loading
        clusters = config.get("clusters", {})
        if cluster_name:
            if cluster_name not in clusters:
                raise ValueError(f"cluster '{cluster_name}' not found in config")
            self.cluster_name = cluster_name
        else:
            self.cluster_name = next(iter(clusters)) if clusters else None

        self.cluster_config = (
            clusters.get(self.cluster_name, {}) if self.cluster_name else {}
        )

        # Lazily loaded
        self._env: Optional[Dict[str, str]] = None

    @property
    def env(self) -> Dict[str, str]:
        """
        Return the merged environment for task execution.

        Combines the current process environment with variables sourced
        from the workspace env file.  Cached after first access.
        """
        if self._env is None:
            workspace_vars = load_workspace_env(
                self.cluster_config, self.workspace_config
            )
            self._env = os.environ.copy()
            self._env.update(workspace_vars)

            # Also set INFRA to the project name if not already set
            if "INFRA" not in self._env and "project" in self.config:
                self._env["INFRA"] = self.config["project"]

        return self._env

    def list_tasks(self) -> List[Dict[str, str]]:
        """
        Return a list of task summaries.

        Returns:
            A list of dicts with ``name`` and ``description`` keys.
        """
        result = []
        for name, task in self.tasks.items():
            result.append({
                "name": name,
                "description": task.get("description", ""),
            })
        return result

    @staticmethod
    def extract_placeholders(command: str) -> List[str]:
        """Return placeholder names found in *command*.

        Placeholders use the ``{name}`` syntax (single curly braces).
        """
        return re.findall(r"\{(\w+)\}", command)

    def run(
        self,
        task_name: str,
        extra_args: Optional[List[str]] = None,
        task_flags: Optional[Dict[str, str]] = None,
    ) -> int:
        """
        Execute a named task.

        Args:
            task_name: The task key from the ``tasks`` section.
            extra_args: Additional arguments appended to the command.
            task_flags: Values for ``{placeholder}`` markers in the command.

        Returns:
            The exit code of the task process.

        Raises:
            KeyError: If the task is not defined.
        """
        if task_name not in self.tasks:
            available = ", ".join(self.tasks.keys()) if self.tasks else "(none)"
            raise KeyError(
                f"task '{task_name}' not found. Available tasks: {available}"
            )

        task = self.tasks[task_name]
        command = task["command"].strip()

        # Substitute {placeholder} markers with values from task_flags
        def _replace(match):
            name = match.group(1)
            if task_flags and name in task_flags:
                return task_flags[name]
            return ""

        command = re.sub(r"\{(\w+)\}", _replace, command)
        command = re.sub(r" {2,}", " ", command).strip()

        # Append extra args
        if extra_args:
            command = command + " " + " ".join(extra_args)

        log.info(f"running task '{task_name}'")
        log.info(f"command: {command}")

        # Resolve workdir: task-level > workspace.workdir > workspace.path > cluster workdir > cwd
        workdir = task.get(
            "workdir",
            self.workspace_config.get(
                "workdir",
                self.workspace_config.get(
                    "path",
                    self.cluster_config.get("workdir", None),
                ),
            ),
        )
        if workdir:
            workdir = os.path.expanduser(workdir)

        result = subprocess.run(
            command,
            shell=True,
            env=self.env,
            cwd=workdir,
        )

        return result.returncode

    def run_command(
        self,
        command: str,
        extra_args: Optional[List[str]] = None,
        ansible_flags: Optional[str] = None,
    ) -> int:
        """
        Execute an ad-hoc command with the workspace environment loaded.

        The command is wrapped with ``ansible all -m ansible.builtin.shell``
        so it runs on all inventory hosts.

        Args:
            command: The shell command to run.
            extra_args: Additional arguments appended to the command.
            ansible_flags: Extra flags passed to the ansible command
                (e.g. ``--limit cluster_1_node01``).

        Returns:
            The exit code of the process.
        """
        if extra_args:
            command = command + " " + " ".join(extra_args)

        ansible_cmd = f'ansible all -m ansible.builtin.shell -a "{command}"'
        if ansible_flags:
            ansible_cmd = ansible_cmd + " " + ansible_flags

        log.info(f"running ad-hoc command")
        log.info(f"command: {ansible_cmd}")
        command = ansible_cmd

        workdir = self.workspace_config.get(
            "workdir",
            self.workspace_config.get(
                "path",
                self.cluster_config.get("workdir", None),
            ),
        )
        if workdir:
            workdir = os.path.expanduser(workdir)

        result = subprocess.run(
            command,
            shell=True,
            env=self.env,
            cwd=workdir,
        )

        return result.returncode
