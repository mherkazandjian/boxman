"""
Shared FastAPI dependencies and helpers.

Auth dependencies (current user / RBAC) are added in the auth phase; this module
currently provides project resolution, provider detection, and a helper to run
a synchronous read operation and surface CLI failures as HTTP errors.
"""

from __future__ import annotations

import subprocess
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from boxman.api import cache
from boxman.api.capabilities import supports, universal_caps
from boxman.api.cli_runner import CliResult, run_sync
from boxman.api.db.models import Job
from boxman.api.jobs import service
from boxman.api.operations import OPERATIONS, Op


def resolve_project(name: str) -> cache.ProjectEntry:
    entry = cache.get_project(name)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"project '{name}' is not registered",
        )
    return entry


def detect_provider(entry: cache.ProjectEntry) -> str:
    """Best-effort detection of a project's provider (defaults to libvirt).

    Runs ``boxman conf --json`` and looks for a ``provider`` mapping. Any
    failure falls back to ``libvirt`` (the primary provider).
    """
    from boxman.api.operations import OPERATIONS

    try:
        result = run_sync(
            OPERATIONS["show_conf"], {}, conf_path=entry.conf, runtime=entry.runtime
        )
        if result.ok:
            data = result.json()
            provider = data.get("provider") if isinstance(data, dict) else None
            if isinstance(provider, dict) and provider:
                return next(iter(provider.keys()))
    except (subprocess.SubprocessError, ValueError, KeyError):
        pass
    return "libvirt"


def ensure_capability(provider: str, op: Op) -> None:
    """Reject operations whose capability the provider does not support."""
    if not supports(provider, op.cap):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                f"operation requires capability '{op.cap}' which provider "
                f"'{provider}' does not support"
            ),
        )


def run_read(op: Op, entry: cache.ProjectEntry, payload: dict[str, Any] | None = None) -> CliResult:
    """Run a synchronous read op for a project, raising HTTP 502 on failure."""
    try:
        result = run_sync(
            op,
            payload or {},
            conf_path=entry.conf if op.needs_conf else None,
            runtime=entry.runtime,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="boxman command timed out",
        )
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"boxman command failed (exit {result.returncode})\n{result.stderr}",
        )
    return result


def submit_operation(
    db: Session,
    op_name: str,
    entry: cache.ProjectEntry | None,
    payload: dict | None = None,
    *,
    requested_by: str | None = None,
) -> Job:
    """Validate + enqueue a mutating/long operation, returning the Job.

    - destructive ops require ``confirm: true`` in the payload (→ 400 otherwise)
    - provider-specific ops are capability-gated (→ 501 if unsupported); common
      caps skip the (expensive) provider-detection step
    - a project that already has an active job → 409
    """
    op = OPERATIONS[op_name]
    payload = payload or {}

    if op.destructive and not payload.get("confirm"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="this operation is destructive; pass confirm=true to proceed",
        )

    if op.cap not in universal_caps():
        provider = detect_provider(entry) if entry else "libvirt"
        ensure_capability(provider, op)

    try:
        return service.enqueue(
            db, op_name, project_entry=entry, payload=payload, requested_by=requested_by
        )
    except service.JobConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
