"""
Utility for sourcing shell environment files (.sh) and capturing exported variables.

This allows boxman to load environment files (like env.sh) that set variables
such as INVENTORY, SSH_CONFIG, GATEWAYHOST, etc., and make them available
for task execution.
"""

import os
import shlex
import subprocess

from boxman import log


def source_env_file(env_file_path: str) -> dict[str, str]:
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
    sourced_env: dict[str, str] = {}
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        key, sep, value = entry.partition("=")
        if sep == "=":
            sourced_env[key] = value

    # Return only new/changed variables
    current_env = os.environ
    new_vars: dict[str, str] = {}
    for key, value in sourced_env.items():
        if current_env.get(key) != value:
            new_vars[key] = value

    return new_vars


def load_workspace_env(
    cluster_config: dict,
    workspace_config: dict | None = None,
) -> dict[str, str]:
    """
    Load environment variables for a workspace from the cluster and
    workspace configuration.

    Resolution order:

    1. If ``workspace_config`` has an ``env_file`` key, source that file.
    2. If the cluster's ``files`` section contains ``env.sh``, write it to
       the workdir (if not already present) and source it.
    3. Explicit keys in ``workspace_config`` (``gateway_host``, ``ssh_config``,
       ``inventory``, ``salt_master``, ``ansible_config``) override any
       sourced values.
    4. The same keys declared under the *cluster* (``cluster_config``) are
       layered on top of the workspace ones, so a ``run --cluster <name>``
       (or ``ssh --cluster``) targets that cluster's own inventory / gateway
       / ssh_config rather than the shared workspace defaults. This is what
       makes multi-cluster projects inventory-isolated at the operations
       layer. A relative cluster ``inventory`` / ``ssh_config`` /
       ``ansible_config`` is resolved against the cluster's ``workdir`` (tasks
       usually run from ``workspace.path``, so a bare relative path would
       otherwise miss), and a repointed ``INVENTORY`` is mirrored to
       ``ANSIBLE_INVENTORY`` (which ansible actually reads).

    Args:
        cluster_config: A single cluster's configuration dict (from conf.yml).
        workspace_config: Optional ``workspace`` section from conf.yml.

    Returns:
        A merged dict of environment variables ready for task execution.
    """
    env_vars: dict[str, str] = {}
    workspace_config = workspace_config or {}
    workdir = os.path.expanduser(cluster_config.get("workdir", "."))

    # 1. Source explicit env_file from workspace config
    env_file = workspace_config.get("env_file")
    if env_file:
        env_file = os.path.expanduser(env_file)
        log.info(f"sourcing workspace env_file: {env_file}")
        env_vars.update(source_env_file(env_file))

    # 2. Fall back to workspace files.env.sh if no explicit env_file
    elif "files" in workspace_config and "env.sh" in workspace_config["files"]:
        ws_path = os.path.expanduser(workspace_config.get("path", workdir))
        env_sh_path = os.path.join(ws_path, "env.sh")
        if os.path.isfile(env_sh_path):
            log.info(f"sourcing workspace env.sh: {env_sh_path}")
            env_vars.update(source_env_file(env_sh_path))
        else:
            log.warning(
                f"workspace defines files.env.sh but {env_sh_path} does not exist "
                f"(run 'boxman provision' first to generate it)"
            )

    # 3. Explicit overrides. Workspace-level keys apply first; the same keys
    #    declared on the cluster are layered on top (cluster is more specific,
    #    so it wins). This lets each cluster in a multi-cluster project point
    #    at its own inventory / gateway / ssh_config tree.
    #
    #    Path-valued keys are resolved against the *cluster* workdir when they
    #    come from the cluster and are relative — a cluster's `inventory: foo`
    #    lives under its workdir, but tasks usually run from workspace.path, so
    #    leaving it relative would point at the wrong place. Workspace-level
    #    keys keep their historical expanduser-only handling. Plain values
    #    (gateway/salt host) are never path-resolved.
    value_overrides = {
        "gateway_host": "GATEWAYHOST",
        "salt_master": "SALTMASTER",
    }
    path_overrides = {
        "ssh_config": "SSH_CONFIG",
        "inventory": "INVENTORY",
        "ansible_config": "ANSIBLE_CONFIG",
    }

    def _apply(source: dict, base: str | None) -> None:
        for yaml_key, env_key in value_overrides.items():
            if yaml_key in source:
                env_vars[env_key] = os.path.expanduser(str(source[yaml_key]))
        for yaml_key, env_key in path_overrides.items():
            if yaml_key not in source:
                continue
            val = os.path.expanduser(str(source[yaml_key]))
            if base and not os.path.isabs(val):
                val = os.path.normpath(os.path.join(base, val))
            env_vars[env_key] = val
            # ansible consults ANSIBLE_INVENTORY (which env.sh seeds from
            # INVENTORY); keep it in step so a repointed inventory actually
            # takes effect for `run --cluster`.
            if env_key == "INVENTORY":
                env_vars["ANSIBLE_INVENTORY"] = val

    _apply(workspace_config, None)
    _apply(cluster_config, workdir)

    return env_vars
