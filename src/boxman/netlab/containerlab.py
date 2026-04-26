"""Containerlab integration.

Thin wrapper around the ``containerlab`` Go CLI. Boxman renders a lab's
``conf.yml`` ``containerlab:`` block into a real containerlab topology
YAML (applying Jinja2 to any ``startup-config: *.j2`` paths along the
way), then shells out to ``containerlab deploy|destroy|inspect``.

No REST API / SDK — containerlab's integration surface is its CLI and
its ``--format json`` inspect output.

The manager is deliberately stateless: constructed per operation with
the currently-loaded config, no long-lived client.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment

from boxman import log
from boxman.utils.shell import run


class ContainerlabNotInstalled(RuntimeError):
    """Raised when the ``containerlab`` binary or Docker isn't available."""


INSTALL_HINT = (
    "\n\n    containerlab: bash -c \"$(curl -sL https://get.containerlab.dev)\"\n"
    "    docker:       https://docs.docker.com/engine/install/\n"
)


class ContainerlabManager:
    """Drive a containerlab topology from a boxman ``conf.yml`` block.

    Parameters
    ----------
    lab_config
        The ``containerlab:`` sub-dict from ``conf.yml``. Expected keys:
        ``lab_name`` (str), ``topology`` (dict with ``nodes`` and ``links``),
        and optional ``enabled`` (bool, default True).
    workdir
        Directory under which rendered files are written
        (``<workdir>/netlab/<lab_name>.clab.yml`` plus rendered
        ``<workdir>/netlab/configs/<node>.cfg``).
    jinja_env
        Jinja2 environment used to render ``startup-config: *.j2`` paths.
        Must already have boxman's standard globals (``env``,
        ``env_required``, …). Pass ``None`` to disable Jinja rendering
        (startup-config paths are then copied as-is).
    """

    def __init__(self,
                 lab_config: dict[str, Any],
                 workdir: str | Path,
                 jinja_env: Environment | None = None) -> None:
        self.lab_config = lab_config
        self.workdir = Path(workdir)
        self.jinja_env = jinja_env
        self.logger = log

    @property
    def lab_name(self) -> str:
        name = self.lab_config.get("lab_name")
        if not name:
            raise ValueError("containerlab.lab_name is required")
        return name

    @property
    def enabled(self) -> bool:
        return bool(self.lab_config.get("enabled", True))

    @property
    def netlab_dir(self) -> Path:
        return self.workdir / "netlab"

    @property
    def topology_path(self) -> Path:
        return self.netlab_dir / f"{self.lab_name}.clab.yml"

    # ------------------------------------------------------------------
    # preflight
    # ------------------------------------------------------------------
    def preflight(self) -> None:
        """Verify ``containerlab`` and ``docker`` are on ``PATH``.

        Raises
        ------
        ContainerlabNotInstalled
            If either binary is missing.
        """
        missing = [tool for tool in ("containerlab", "docker")
                   if shutil.which(tool) is None]
        if missing:
            raise ContainerlabNotInstalled(
                f"missing required tool(s) on PATH: {', '.join(missing)}."
                f"{INSTALL_HINT}"
            )

    # ------------------------------------------------------------------
    # topology rendering
    # ------------------------------------------------------------------
    def render_topology(self, source_root: str | Path | None = None) -> Path:
        """Render boxman's ``containerlab:`` block to a clab topology file.

        * Startup-config paths ending in ``.j2`` are resolved relative to
          *source_root* (defaults to ``self.workdir``), rendered through
          ``self.jinja_env``, written under ``netlab/configs/`` with the
          ``.j2`` suffix stripped, and rewritten in the emitted topology
          to point at the rendered file.
        * Non-Jinja startup-config paths pass through unchanged.
        * ``host:<bridge>`` / ``bridge:<name>`` endpoints are left alone
          — containerlab handles them natively.

        Returns
        -------
        Path
            Absolute path to the written ``<lab_name>.clab.yml``.
        """
        source_root = Path(source_root) if source_root else self.workdir
        self.netlab_dir.mkdir(parents=True, exist_ok=True)
        configs_dir = self.netlab_dir / "configs"

        topology = self.lab_config.get("topology", {}) or {}
        nodes_in = topology.get("nodes", {}) or {}
        nodes_out: dict[str, dict[str, Any]] = {}

        for node_name, node_info in nodes_in.items():
            node_copy = dict(node_info)
            startup = node_copy.get("startup-config")
            if startup and str(startup).endswith(".j2"):
                rendered_path = self._render_startup_config(
                    node_name=node_name,
                    template_path=Path(startup),
                    source_root=source_root,
                    configs_dir=configs_dir,
                )
                node_copy["startup-config"] = str(rendered_path)
            nodes_out[node_name] = node_copy

        rendered_topology: dict[str, Any] = {
            "name": self.lab_name,
            "topology": {
                "nodes": nodes_out,
            },
        }
        links = topology.get("links")
        if links:
            rendered_topology["topology"]["links"] = links

        # Pass through optional top-level keys containerlab understands.
        for passthrough in ("prefix", "mgmt"):
            if passthrough in self.lab_config:
                rendered_topology[passthrough] = self.lab_config[passthrough]

        self.topology_path.write_text(
            yaml.safe_dump(rendered_topology, sort_keys=False)
        )
        self.logger.info(f"rendered containerlab topology to {self.topology_path}")
        return self.topology_path

    def _render_startup_config(self,
                               node_name: str,
                               template_path: Path,
                               source_root: Path,
                               configs_dir: Path) -> Path:
        """Render a single startup-config Jinja2 template."""
        if self.jinja_env is None:
            raise RuntimeError(
                f"node {node_name!r} has a .j2 startup-config "
                f"but no jinja_env was provided"
            )

        abs_template = (source_root / template_path).resolve()
        if not abs_template.exists():
            raise FileNotFoundError(
                f"startup-config template not found for node "
                f"{node_name!r}: {abs_template}"
            )

        # Render using jinja_env with the template file's directory as the
        # loader root, so {% include %} in the template resolves relative
        # paths. The jinja_env passed in is used for globals; we create a
        # short-lived child env scoped to the template dir.
        template_dir = abs_template.parent
        from jinja2 import Environment, FileSystemLoader
        child = Environment(
            loader=FileSystemLoader(str(template_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        child.globals.update(self.jinja_env.globals)

        template = child.get_template(abs_template.name)
        rendered = template.render()

        configs_dir.mkdir(parents=True, exist_ok=True)
        # Strip .j2 suffix; fallback to <node>.cfg if suffix is oddly placed.
        out_name = abs_template.name[:-3] if abs_template.name.endswith(".j2") \
            else f"{node_name}.cfg"
        out_path = configs_dir / out_name
        out_path.write_text(rendered)
        return out_path

    # ------------------------------------------------------------------
    # lifecycle (CLI shell-outs)
    # ------------------------------------------------------------------
    def deploy(self) -> None:
        """Run ``containerlab deploy -t <topology>``."""
        topology = self.topology_path
        if not topology.exists():
            raise FileNotFoundError(
                f"topology file not found: {topology} "
                f"(did you call render_topology()?)"
            )
        self.logger.info(f"deploying containerlab lab {self.lab_name!r}")
        run(f"containerlab deploy -t {topology}")

    def ensure_up(self) -> None:
        """Idempotent: deploy the lab if absent, else start any stopped nodes.

        Safe to call repeatedly from ``boxman up``. Lists existing Docker
        containers matching the lab's ``clab-<lab>-*`` naming prefix:

        - No matches → render + deploy fresh.
        - All matches running → no-op.
        - Some matches stopped (e.g. after host reboot) → ``docker start``
          each one. No need to re-render or re-deploy.
        """
        prefix = f"clab-{self.lab_name}-"
        result = run(
            f"docker ps -a --filter name={prefix} "
            f"--format '{{{{.Names}}}} {{{{.State}}}}'",
            hide=True, warn=True,
        )
        if not result.ok or not result.stdout.strip():
            if not self.topology_path.exists():
                self.render_topology()
            self.deploy()
            return

        stopped: list[str] = []
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name, state = parts[0], parts[1]
            if state != "running":
                stopped.append(name)

        if not stopped:
            self.logger.info(
                f"containerlab lab {self.lab_name!r} already running "
                f"({len(result.stdout.strip().splitlines())} node(s))"
            )
            return

        for name in stopped:
            self.logger.info(f"starting stopped lab container {name!r}")
            run(f"docker start {name}", warn=True)

    def destroy(self) -> None:
        """Run ``containerlab destroy --name <lab_name>`` (graceful if absent)."""
        self.logger.info(f"destroying containerlab lab {self.lab_name!r}")
        topology = self.topology_path
        if topology.exists():
            run(f"containerlab destroy -t {topology} --cleanup", warn=True)
        else:
            # Fall back to name-based destroy if the rendered topology is gone.
            run(f"containerlab destroy --name {self.lab_name} --cleanup",
                warn=True)

    def inspect(self) -> dict[str, Any]:
        """Return ``containerlab inspect --format json`` output for the lab."""
        result = run(
            f"containerlab inspect --name {self.lab_name} --format json",
            hide=True, warn=True,
        )
        if not result.ok:
            return {}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            self.logger.warning("containerlab inspect returned non-JSON output")
            return {}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def ssh_command(self, node_name: str, user: str | None = None) -> str:
        """Return ``ssh <user>@clab-<lab>-<node>`` for a lab node.

        *user* defaults to the node's ``login-user`` if declared in the
        topology, else ``'admin'`` (matches most vrnetlab Cisco/Arista
        defaults).
        """
        node_info = (self.lab_config.get("topology", {})
                     .get("nodes", {})
                     .get(node_name))
        if node_info is None:
            raise KeyError(f"node {node_name!r} not declared in lab {self.lab_name!r}")
        resolved_user = user or node_info.get("login-user") or "admin"
        return f"ssh {resolved_user}@clab-{self.lab_name}-{node_name}"
