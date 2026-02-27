"""
Utility for sourcing shell environment files (.sh) and capturing exported variables.

This allows boxman to load environment files (like env.sh) that set variables
such as INVENTORY, SSH_CONFIG, GATEWAYHOST, etc., and make them available
for task execution.
"""

import os
import shlex
import subprocess
from typing import Dict, Optional

from boxman import log


def source_env_file(env_file_path: str) -> Dict[str, str]:
    """
    Source a shell environment file and return the exported variables.

    Runs the file inside a bash subshell with ``set -a`` (auto-export),
    then captures the resulting environment.  Only variables that are
    **new or changed** compared to the current process environment are
    returned.

    Args:
        env_file_path: Path to the shell file to source (``~`` is expanded).

    Returns:
        A dict of variable names to values that were set or changed by
        the sourced file.

    Raises:
        FileNotFoundError: If the env file does not exist.
        RuntimeError: If sourcing the file fails.
    """
    expanded = os.path.expanduser(env_file_path)
    if not os.path.isfile(expanded):
        raise FileNotFoundError(f"env file not found: {expanded}")

    # Run bash: enable auto-export, source the file, dump env
    cmd = f"set -a && source {shlex.quote(expanded)} && env -0"
    result = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"failed to source env file {expanded}: {result.stderr.strip()}"
        )

    # Parse null-delimited env output (handles values with newlines)
    sourced_env: Dict[str, str] = {}
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        key, sep, value = entry.partition("=")
        if sep == "=":
            sourced_env[key] = value

    # Return only new/changed variables
    current_env = os.environ
    new_vars: Dict[str, str] = {}
    for key, value in sourced_env.items():
        if current_env.get(key) != value:
            new_vars[key] = value

    return new_vars


def load_workspace_env(
    cluster_config: Dict,
    workspace_config: Optional[Dict] = None,
) -> Dict[str, str]:
    """
    Load environment variables for a workspace from the cluster and
    workspace configuration.

    Resolution order:

    1. If ``workspace_config`` has an ``env_file`` key, source that file.
    2. If the cluster's ``files`` section contains ``env.sh``, write it to
       the workdir (if not already present) and source it.
    3. Explicit keys in ``workspace_config`` (``gateway_host``, ``ssh_config``,
       ``inventory``, ``salt_master``) override any sourced values.

    Args:
        cluster_config: A single cluster's configuration dict (from conf.yml).
        workspace_config: Optional ``workspace`` section from conf.yml.

    Returns:
        A merged dict of environment variables ready for task execution.
    """
    env_vars: Dict[str, str] = {}
    workspace_config = workspace_config or {}
    workdir = os.path.expanduser(cluster_config.get("workdir", "."))

    # 1. Source explicit env_file from workspace config
    env_file = workspace_config.get("env_file")
    if env_file:
        env_file = os.path.expanduser(env_file)
        log.info(f"sourcing workspace env_file: {env_file}")
        env_vars.update(source_env_file(env_file))

    # 2. Fall back to cluster files.env.sh if no explicit env_file
    elif "files" in cluster_config and "env.sh" in cluster_config["files"]:
        env_sh_path = os.path.join(workdir, "env.sh")
        if os.path.isfile(env_sh_path):
            log.info(f"sourcing cluster env.sh: {env_sh_path}")
            env_vars.update(source_env_file(env_sh_path))
        else:
            log.warning(
                f"cluster defines files.env.sh but {env_sh_path} does not exist "
                f"(run 'boxman provision' first to generate it)"
            )

    # 3. Explicit workspace overrides
    override_map = {
        "gateway_host": "GATEWAYHOST",
        "salt_master": "SALTMASTER",
        "ssh_config": "SSH_CONFIG",
        "inventory": "INVENTORY",
        "ansible_config": "ANSIBLE_CONFIG",
    }
    for yaml_key, env_key in override_map.items():
        if yaml_key in workspace_config:
            env_vars[env_key] = os.path.expanduser(
                str(workspace_config[yaml_key])
            )

    return env_vars
