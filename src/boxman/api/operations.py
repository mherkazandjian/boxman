"""
The operation registry — the single source of truth that maps an API
operation onto a ``boxman`` CLI invocation.

This module is deliberately pure (no IO, no FastAPI, no subprocess) so it can
be unit-tested without a hypervisor or a running server. The argv it produces
is fed to :mod:`boxman.api.cli_runner`, which prepends the global flags
(``--conf``/``--runtime``/``--boxman-conf``) and execs the CLI.

Field names in :class:`ArgSpec` mirror the request schemas; the ``flag`` values
mirror exactly what ``boxman.scripts.cli_parser`` defines, so the synthesized
argv is identical to what a human would type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ArgSpec:
    """Maps one request field onto one CLI argument.

    kind:
      - ``value``      → ``[flag, str(val)]`` when val is not None
      - ``flag``       → ``[flag]`` when val is truthy (store_true)
      - ``csv``        → ``[flag, "a,b,c"]`` (accepts list or str)
      - ``positional`` → ``[str(val)]`` (no flag)
      - ``bool_pair``  → ``[flag]`` if True, ``[false_flag]`` if False, omit if None
    """

    field: str
    flag: str | None = None
    kind: str = "value"
    false_flag: str | None = None


@dataclass(frozen=True)
class Op:
    """A single API operation and how it maps to the boxman CLI.

    Attributes:
        subcommand:   CLI subcommand tokens, e.g. ``["snapshot", "take"]``.
        cap:          capability key used for provider gating (see capabilities).
        long:         True → dispatched to Celery; False → run synchronously.
        read_json:    True → a read that emits JSON on stdout (``--json`` appended).
        needs_conf:   True → requires ``--conf <project conf>`` (almost all do).
        destructive:  True → API requires an explicit ``confirm: true``.
        auto_accept:  flags appended to silence interactive prompts (e.g. ``-y``).
        args:         the per-field argv mapping.
    """

    subcommand: tuple[str, ...]
    cap: str
    long: bool = False
    read_json: bool = False
    needs_conf: bool = True
    destructive: bool = False
    auto_accept: tuple[str, ...] = ()
    args: tuple[ArgSpec, ...] = ()


def build_op_argv(op: Op, payload: dict[str, Any]) -> list[str]:
    """Build the subcommand-and-args portion of the argv from a payload.

    Global flags (``--conf`` etc.) are NOT included here — the runner prepends
    them. For read operations the ``--json`` flag is appended automatically.
    """
    argv: list[str] = list(op.subcommand)
    positionals: list[str] = []

    for spec in op.args:
        val = payload.get(spec.field, None)
        if val is None:
            continue
        if spec.kind == "flag":
            if val:
                argv.append(spec.flag)  # type: ignore[arg-type]
        elif spec.kind == "bool_pair":
            argv.append(spec.flag if val else spec.false_flag)  # type: ignore[arg-type]
        elif spec.kind == "csv":
            csv = ",".join(val) if isinstance(val, (list, tuple)) else str(val)
            argv.extend([spec.flag, csv])  # type: ignore[list-item]
        elif spec.kind == "positional":
            positionals.append(str(val))
        else:  # value
            argv.extend([spec.flag, str(val)])  # type: ignore[list-item]

    argv.extend(positionals)

    if op.read_json:
        argv.append("--json")

    argv.extend(op.auto_accept)
    return argv


# Common selector: --vms accepts "all" or a csv of box names.
_VMS = ArgSpec(field="boxes", flag="--vms", kind="csv")


#: The full operation surface. Keys are stable API operation ids.
OPERATIONS: dict[str, Op] = {
    # ── project lifecycle ────────────────────────────────────────────
    "provision": Op(
        subcommand=("provision",), cap="lifecycle", long=True,
        args=(
            ArgSpec("force", "--force", "flag"),
            ArgSpec("rebuild_templates", "--rebuild-templates", "flag"),
            ArgSpec("docker_compose", "--docker-compose", "flag"),
        ),
    ),
    "up": Op(
        subcommand=("up",), cap="lifecycle", long=True,
        args=(
            ArgSpec("force", "--force", "flag"),
            ArgSpec("rebuild_templates", "--rebuild-templates", "flag"),
            ArgSpec("docker_compose", "--docker-compose", "flag"),
        ),
    ),
    "down": Op(
        subcommand=("down",), cap="lifecycle", long=True,
        args=(ArgSpec("suspend", "--suspend", "flag"),),
    ),
    "deprovision": Op(
        subcommand=("deprovision",), cap="lifecycle", long=True,
        args=(
            ArgSpec("cleanup", "--cleanup", "flag"),
            ArgSpec("docker_compose", "--docker-compose", "flag"),
        ),
    ),
    "destroy": Op(
        subcommand=("destroy",), cap="lifecycle", long=True, destructive=True,
        auto_accept=("-y",),
        args=(ArgSpec("templates", "--templates", "flag"),),
    ),
    "destroy_runtime": Op(
        subcommand=("destroy-runtime",), cap="lifecycle", long=True,
        destructive=True, auto_accept=("-y",),
    ),
    "update": Op(
        subcommand=("update",), cap="lifecycle", long=True, auto_accept=("-y",),
        args=(
            ArgSpec("dry_run", "--dry-run", "flag"),
            ArgSpec("docker_compose", "--docker-compose", "flag"),
        ),
    ),

    # ── templates / images ───────────────────────────────────────────
    "create_templates": Op(
        subcommand=("create-templates",), cap="templates", long=True,
        needs_conf=True,
        args=(
            ArgSpec("template_names", "--templates", "value"),
            ArgSpec("force", "--force", "flag"),
        ),
    ),
    "import_image": Op(
        subcommand=("import-image",), cap="image", long=True, needs_conf=False,
        args=(
            ArgSpec("manifest_uri", "--uri", "value"),
            ArgSpec("vm_name", "--name", "value"),
            ArgSpec("vm_dir", "--directory", "value"),
            ArgSpec("provider", "--provider", "value"),
        ),
    ),
    "push_image": Op(
        subcommand=("image", "push"), cap="image", long=True, needs_conf=False,
        args=(
            ArgSpec("image_ref", None, "positional"),
            ArgSpec("qcow2", "--qcow2", "value"),
            ArgSpec("metadata", "--metadata", "value"),
        ),
    ),

    # ── snapshots ─────────────────────────────────────────────────────
    "snapshot_take": Op(
        subcommand=("snapshot", "take"), cap="snapshot", long=True,
        args=(
            _VMS,
            ArgSpec("name", "--name", "value"),
            ArgSpec("description", "--description", "value"),
            ArgSpec("live", "--live", "bool_pair", false_flag="--no-live"),
            ArgSpec("compress_memory", "--compress-memory", "flag"),
            ArgSpec("memory_compress_level", "--memory-compress-level", "value"),
        ),
    ),
    "snapshot_list": Op(
        subcommand=("snapshot", "list"), cap="snapshot", read_json=False,
        args=(_VMS,),
    ),
    "snapshot_log": Op(
        subcommand=("snapshot", "log"), cap="snapshot", read_json=True,
        args=(
            _VMS,
            ArgSpec("max_count", "--max", "value"),
            ArgSpec("reverse", "--reverse", "flag"),
            ArgSpec("no_graph", "--no-graph", "flag"),
        ),
    ),
    "snapshot_restore": Op(
        subcommand=("snapshot", "restore"), cap="snapshot", long=True,
        args=(_VMS, ArgSpec("name", "--name", "value")),
    ),
    "snapshot_delete": Op(
        subcommand=("snapshot", "delete"), cap="snapshot", long=True,
        args=(_VMS, ArgSpec("name", "--name", "value")),
    ),
    "snapshot_collapse": Op(
        subcommand=("snapshot", "collapse"), cap="snapshot", long=True,
        auto_accept=("-y",),
        args=(
            _VMS,
            ArgSpec("to", "--to", "value"),
            ArgSpec("no_shutdown", "--no-shutdown", "flag"),
            ArgSpec("dry_run", "--dry-run", "flag"),
        ),
    ),

    # ── storage ───────────────────────────────────────────────────────
    "storage_df": Op(
        subcommand=("storage", "df"), cap="storage", read_json=False,
        args=(_VMS,),
    ),
    "storage_trim": Op(
        subcommand=("storage", "trim"), cap="storage.guest-agent", long=True,
        args=(_VMS, ArgSpec("dry_run", "--dry-run", "flag")),
    ),
    "storage_compact": Op(
        subcommand=("storage", "compact"), cap="storage.qcow2", long=True,
        args=(
            _VMS,
            ArgSpec("method", "--method", "value"),
            ArgSpec("no_shutdown", "--no-shutdown", "flag"),
            ArgSpec("drop_snapshots", "--drop-snapshots", "flag"),
            ArgSpec("dry_run", "--dry-run", "flag"),
        ),
    ),
    "storage_optimize": Op(
        subcommand=("storage", "optimize"), cap="storage.qcow2", long=True,
        args=(
            _VMS,
            ArgSpec("method", "--method", "value"),
            ArgSpec("skip_trim", "--skip-trim", "flag"),
            ArgSpec("skip_compact", "--skip-compact", "flag"),
            ArgSpec("no_shutdown", "--no-shutdown", "flag"),
            ArgSpec("drop_snapshots", "--drop-snapshots", "flag"),
            ArgSpec("dry_run", "--dry-run", "flag"),
        ),
    ),
    "storage_compress_snapshots": Op(
        subcommand=("storage", "compress-snapshots"), cap="storage.qcow2", long=True,
        args=(
            _VMS,
            ArgSpec("level", "--level", "value"),
            ArgSpec("decompress", "--decompress", "flag"),
        ),
    ),

    # ── control ───────────────────────────────────────────────────────
    "control_suspend": Op(
        subcommand=("control", "suspend"), cap="control", long=True, args=(_VMS,),
    ),
    "control_resume": Op(
        subcommand=("control", "resume"), cap="control", long=True, args=(_VMS,),
    ),
    "control_save": Op(
        subcommand=("control", "save"), cap="control", long=True, args=(_VMS,),
    ),
    "control_start": Op(
        subcommand=("control", "start"), cap="control", long=True,
        args=(_VMS, ArgSpec("restore", "--restore", "flag")),
    ),
    "pxe_boot": Op(
        subcommand=("pxe-boot",), cap="pxe", long=True,
        args=(
            ArgSpec("vm", "--vm", "value"),
            ArgSpec("expected_ip", "--expected-ip", "value"),
            ArgSpec("wait_timeout", "--wait-timeout", "value"),
            ArgSpec("restore_after", "--restore-after", "flag"),
        ),
    ),

    # ── run / tasks ───────────────────────────────────────────────────
    "run_task": Op(
        subcommand=("run",), cap="run", long=True,
        args=(
            ArgSpec("task_name", None, "positional"),
            ArgSpec("cmd", "--cmd", "value"),
            ArgSpec("ansible_flags", "--ansible-flags", "value"),
            ArgSpec("cluster", "--cluster", "value"),
        ),
    ),
    "list_tasks": Op(
        subcommand=("run",), cap="run", read_json=False,
        args=(ArgSpec("list_tasks", "--list", "flag"),),
    ),

    # ── netlab (containerlab) ─────────────────────────────────────────
    "netlab_deploy": Op(
        subcommand=("netlab", "deploy"), cap="netlab", long=True,
    ),
    "netlab_destroy": Op(
        subcommand=("netlab", "destroy"), cap="netlab", long=True, destructive=True,
    ),
    "netlab_inspect": Op(
        subcommand=("netlab", "inspect"), cap="netlab", read_json=True,
    ),
    "netlab_ssh": Op(
        subcommand=("netlab", "ssh"), cap="netlab", read_json=False,
        args=(ArgSpec("node", None, "positional"), ArgSpec("user", "--user", "value")),
    ),

    # ── reads that don't need a project conf ──────────────────────────
    "list_projects": Op(
        subcommand=("list",), cap="meta", read_json=True, needs_conf=False,
    ),

    # ── project-scoped reads ──────────────────────────────────────────
    "ps": Op(
        subcommand=("ps",), cap="meta", read_json=True,
        args=(ArgSpec("provider_info", "-p", "flag"),),
    ),
    "show_conf": Op(
        subcommand=("conf",), cap="meta", read_json=True,
    ),
}
