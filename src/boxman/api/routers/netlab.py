"""Containerlab (netlab) deploy/destroy/inspect/ssh endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from boxman.api.auth.deps import require_project_access
from boxman.api.db.models import User
from boxman.api.db.session import get_db
from boxman.api.deps import resolve_project, run_read, submit_operation
from boxman.api.operations import OPERATIONS
from boxman.api.schemas.common import CommandResult
from boxman.api.schemas.jobs import JobRef
from boxman.api.schemas.operations import NetlabDestroyRequest

router = APIRouter(prefix="/projects/{name}/netlab", tags=["netlab"])
_viewer = require_project_access("viewer")
_operator = require_project_access("operator")
_ACCEPTED = status.HTTP_202_ACCEPTED


@router.get("", summary="containerlab inspect (json)")
def inspect(name: str, user: User = Depends(_viewer)) -> Any:
    return run_read(OPERATIONS["netlab_inspect"], resolve_project(name)).json()


@router.get("/{node}/ssh-command", response_model=CommandResult,
            summary="ssh command for a lab node")
def ssh_command(name: str, node: str, user: User = Depends(_viewer)) -> CommandResult:
    result = run_read(OPERATIONS["netlab_ssh"], resolve_project(name), {"node": node})
    return CommandResult(ok=result.ok, returncode=result.returncode, stdout=result.stdout)


@router.post("/deploy", response_model=JobRef, status_code=_ACCEPTED,
             summary="deploy the containerlab lab")
def deploy(name: str, db: Session = Depends(get_db), user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "netlab_deploy", resolve_project(name), {},
                            requested_by=user.username)


@router.post("/destroy", response_model=JobRef, status_code=_ACCEPTED,
             summary="tear down the lab (requires confirm=true)")
def destroy(name: str, req: NetlabDestroyRequest, db: Session = Depends(get_db),
            user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "netlab_destroy", resolve_project(name), req.model_dump(),
                            requested_by=user.username)
