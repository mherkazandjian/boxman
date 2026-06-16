"""Task listing and task/ad-hoc-command execution (run)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from boxman.api.auth.deps import require_project_access
from boxman.api.db.models import User
from boxman.api.db.session import get_db
from boxman.api.deps import resolve_project, run_read, submit_operation
from boxman.api.operations import OPERATIONS
from boxman.api.schemas.common import CommandResult
from boxman.api.schemas.jobs import JobRef
from boxman.api.schemas.operations import RunRequest

router = APIRouter(prefix="/projects/{name}", tags=["run"])


@router.get("/tasks", response_model=CommandResult, summary="list workspace tasks")
def list_tasks(
    name: str, user: User = Depends(require_project_access("viewer"))
) -> CommandResult:
    result = run_read(OPERATIONS["list_tasks"], resolve_project(name), {"list_tasks": True})
    return CommandResult(ok=result.ok, returncode=result.returncode, stdout=result.stdout)


@router.post("/run", response_model=JobRef, status_code=status.HTTP_202_ACCEPTED,
             summary="run a named task or ad-hoc command (output captured)")
def run(
    name: str, req: RunRequest, db: Session = Depends(get_db),
    user: User = Depends(require_project_access("operator")),
) -> JobRef:
    if not req.task_name and not req.cmd:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="provide either 'task_name' or 'cmd'",
        )
    return submit_operation(db, "run_task", resolve_project(name), req.model_dump(),
                            requested_by=user.username)
