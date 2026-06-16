"""Storage inspection (df) and reclamation (trim/compact/optimize/compress)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from boxman.api.auth.deps import require_project_access
from boxman.api.db.models import User
from boxman.api.db.session import get_db
from boxman.api.deps import resolve_project, run_read, submit_operation
from boxman.api.operations import OPERATIONS
from boxman.api.schemas.common import CommandResult
from boxman.api.schemas.jobs import JobRef
from boxman.api.schemas.operations import (
    StorageCompactRequest,
    StorageCompressRequest,
    StorageOptimizeRequest,
    StorageTrimRequest,
)

router = APIRouter(prefix="/projects/{name}/storage", tags=["storage"])
_viewer = require_project_access("viewer")
_operator = require_project_access("operator")
_ACCEPTED = status.HTTP_202_ACCEPTED


@router.get("/df", response_model=CommandResult, summary="per-box disk usage + reclaim estimate")
def df(name: str, boxes: str = Query("all"), user: User = Depends(_viewer)) -> CommandResult:
    # `storage df` has no --json; return captured text.
    result = run_read(OPERATIONS["storage_df"], resolve_project(name), {"boxes": boxes})
    return CommandResult(ok=result.ok, returncode=result.returncode, stdout=result.stdout)


@router.post("/trim", response_model=JobRef, status_code=_ACCEPTED,
             summary="fstrim guests via qemu-guest-agent")
def trim(name: str, req: StorageTrimRequest, db: Session = Depends(get_db),
         user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "storage_trim", resolve_project(name), req.model_dump(),
                            requested_by=user.username)


@router.post("/compact", response_model=JobRef, status_code=_ACCEPTED,
             summary="reclaim qcow2 space")
def compact(name: str, req: StorageCompactRequest, db: Session = Depends(get_db),
            user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "storage_compact", resolve_project(name), req.model_dump(),
                            requested_by=user.username)


@router.post("/optimize", response_model=JobRef, status_code=_ACCEPTED,
             summary="trim then compact (orchestrator)")
def optimize(name: str, req: StorageOptimizeRequest, db: Session = Depends(get_db),
             user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "storage_optimize", resolve_project(name), req.model_dump(),
                            requested_by=user.username)


@router.post("/compress-snapshots", response_model=JobRef, status_code=_ACCEPTED,
             summary="zstd-(de)compress snapshot memory files")
def compress_snapshots(name: str, req: StorageCompressRequest, db: Session = Depends(get_db),
                       user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "storage_compress_snapshots", resolve_project(name),
                            req.model_dump(), requested_by=user.username)
