"""Snapshot mutation endpoints (take / restore / delete / collapse)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from boxman.api.auth.deps import require_project_access
from boxman.api.db.models import User
from boxman.api.db.session import get_db
from boxman.api.deps import resolve_project, submit_operation
from boxman.api.schemas.jobs import JobRef
from boxman.api.schemas.operations import (
    SnapshotCollapseRequest,
    SnapshotScopeRequest,
    SnapshotTakeRequest,
)

router = APIRouter(prefix="/projects/{name}/snapshots", tags=["snapshots"])
_operator = require_project_access("operator")
_ACCEPTED = status.HTTP_202_ACCEPTED


@router.post("", response_model=JobRef, status_code=_ACCEPTED, summary="take a snapshot")
def take(name: str, req: SnapshotTakeRequest, db: Session = Depends(get_db),
         user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "snapshot_take", resolve_project(name), req.model_dump(),
                            requested_by=user.username)


@router.post("/collapse", response_model=JobRef, status_code=_ACCEPTED,
             summary="merge snapshots newer than --to into the live head")
def collapse(name: str, req: SnapshotCollapseRequest, db: Session = Depends(get_db),
             user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "snapshot_collapse", resolve_project(name), req.model_dump(),
                            requested_by=user.username)


@router.post("/{snap}/restore", response_model=JobRef, status_code=_ACCEPTED,
             summary="restore boxes to a snapshot")
def restore(name: str, snap: str, req: SnapshotScopeRequest, db: Session = Depends(get_db),
            user: User = Depends(_operator)) -> JobRef:
    payload = req.model_dump()
    payload["name"] = snap
    return submit_operation(db, "snapshot_restore", resolve_project(name), payload,
                            requested_by=user.username)


@router.delete("/{snap}", response_model=JobRef, status_code=_ACCEPTED,
               summary="delete a snapshot")
def delete(name: str, snap: str, boxes: str = Query("all"), db: Session = Depends(get_db),
           user: User = Depends(_operator)) -> JobRef:
    payload = {"boxes": boxes, "name": snap}
    return submit_operation(db, "snapshot_delete", resolve_project(name), payload,
                            requested_by=user.username)
