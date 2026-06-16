"""
The CLI runner — execs the ``boxman`` CLI as a subprocess.

This is the heart of the "thin shim": an API request is validated into a
payload, :mod:`boxman.api.operations` turns it into subcommand argv, and this
module prepends the global flags and runs the real CLI. Reusing the CLI means
the boxman core (manager/provider/runtime construction in
``scripts/app.py:main``) is exercised exactly as a human would, and boxman's
internal ``multiprocessing`` works because it runs in a normal (non-daemonic)
child process — which also sidesteps the Celery-daemon limitation.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

import boxman
from boxman.api.config import get_settings
from boxman.api.operations import Op, build_op_argv


def _runtime_flag(runtime: str | None) -> list[str]:
    """Map a stored runtime name onto the CLI ``--runtime`` choice.

    The CLI only accepts ``local`` | ``docker``; a cached ``docker-compose``
    runtime maps to ``docker``. ``local`` (the default) is omitted.
    """
    if not runtime or runtime == "local":
        return []
    return ["--runtime", "docker"]


def _child_env() -> dict[str, str]:
    """Environment for the boxman child process.

    Ensures the ``src`` directory holding the ``boxman`` package is importable
    by ``python -m boxman.scripts.app`` even in editable/dev checkouts.
    """
    env = dict(os.environ)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(boxman.__file__)))
    existing = env.get("PYTHONPATH", "")
    parts = [pkg_parent] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


@dataclass
class CliResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def json(self) -> Any:
        """Parse stdout as JSON (for ``read_json`` operations)."""
        return json.loads(self.stdout)


def build_full_argv(
    op: Op,
    payload: dict[str, Any],
    *,
    conf_path: str | None = None,
    runtime: str | None = None,
    boxman_conf: str | None = None,
) -> list[str]:
    """Assemble the complete argv: base + global flags + subcommand args.

    Global flags must precede the subcommand token (argparse top-level
    optionals), so they are inserted before :func:`build_op_argv`'s output.
    """
    settings = get_settings()
    argv = list(settings.boxman_argv())
    if op.needs_conf and conf_path:
        argv += ["--conf", conf_path]
    argv += ["--boxman-conf", boxman_conf or settings.boxman_conf_path]
    argv += _runtime_flag(runtime)
    argv += build_op_argv(op, payload)
    return argv


def run_sync(
    op: Op,
    payload: dict[str, Any],
    *,
    conf_path: str | None = None,
    runtime: str | None = None,
    timeout: int | None = None,
) -> CliResult:
    """Run a (fast) operation synchronously and capture its output.

    Used for read endpoints. stdin is closed so any stray prompt fails fast
    instead of hanging.
    """
    settings = get_settings()
    argv = build_full_argv(op, payload, conf_path=conf_path, runtime=runtime)
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout or settings.read_timeout_seconds,
        stdin=subprocess.DEVNULL,
        env=_child_env(),
        check=False,
    )
    return CliResult(argv, proc.returncode, proc.stdout, proc.stderr)


def stream_to_file(
    op: Op,
    payload: dict[str, Any],
    log_path: str,
    *,
    conf_path: str | None = None,
    runtime: str | None = None,
) -> int:
    """Run a (long) operation, streaming combined stdout+stderr to a file.

    Returns the process exit code. Used by the Celery job runner. The child is
    a normal process, so boxman's internal multiprocessing is unrestricted.
    """
    argv = build_full_argv(op, payload, conf_path=conf_path, runtime=runtime)
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, "w", buffering=1) as logf:
        logf.write("$ " + " ".join(argv) + "\n\n")
        logf.flush()
        proc = subprocess.Popen(
            argv,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=_child_env(),
            text=True,
        )
        return proc.wait()
