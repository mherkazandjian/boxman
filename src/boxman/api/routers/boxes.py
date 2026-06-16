"""
Box (VM) read + control endpoints.

"Box" is boxman's provider-neutral noun; ``boxes=`` selectors map to the CLI's
``--vms`` flag. Reads go through ``ps``; control/pxe are dispatched as jobs.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from boxman.api.auth.deps import require_project_access
from boxman.api.db.models import User
from boxman.api.db.session import get_db
from boxman.api.deps import resolve_project, run_read, submit_operation
from boxman.api.operations import OPERATIONS
from boxman.api.schemas.jobs import JobRef
from boxman.api.schemas.operations import ControlRequest, PxeBootRequest

router = APIRouter(prefix="/projects/{name}", tags=["boxes"])

_CONTROL_ACTIONS = {"start", "suspend", "resume", "save"}
_viewer = require_project_access("viewer")
_operator = require_project_access("operator")


def _ps_records(name: str) -> list[dict[str, Any]]:
    data = run_read(OPERATIONS["ps"], resolve_project(name)).json()
    if isinstance(data, list):
        return data
    return data.get("vms", []) if isinstance(data, dict) else []


def _box_key(record: dict) -> str | None:
    return record.get("vm") or record.get("name") or record.get("box")


@router.get("/boxes", summary="list boxes with state")
def list_boxes(name: str, user: User = Depends(_viewer)) -> list[dict[str, Any]]:
    return _ps_records(name)


@router.get("/boxes/{box}", summary="single box state record")
def get_box(name: str, box: str, user: User = Depends(_viewer)) -> dict[str, Any]:
    for rec in _ps_records(name):
        if _box_key(rec) == box:
            return rec
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"box '{box}' not found")


@router.get(
    "/boxes/{box}/connection-info",
    summary="box connection info (best-effort, from ps)",
)
def connection_info(name: str, box: str, user: User = Depends(_viewer)) -> dict[str, Any]:
    # boxman has no dedicated machine-readable connection command; the ps
    # record carries addresses/state when available. Returned as-is.
    return get_box(name, box, user)


def _control(name: str, action: str, payload: dict, db: Session) -> JobRef:
    if action not in _CONTROL_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown control action '{action}'",
        )
    return submit_operation(db, f"control_{action}", resolve_project(name), payload)


@router.post("/control/{action}", response_model=JobRef, status_code=202,
             summary="control all/selected boxes (start|suspend|resume|save)")
def control_project(
    name: str, action: str, req: ControlRequest,
    db: Session = Depends(get_db), user: User = Depends(_operator),
) -> JobRef:
    job = _control(name, action, req.model_dump(), db)
    return job


@router.post("/boxes/{box}/control/{action}", response_model=JobRef, status_code=202,
             summary="control a single box")
def control_box(
    name: str, box: str, action: str, req: ControlRequest | None = None,
    db: Session = Depends(get_db), user: User = Depends(_operator),
) -> JobRef:
    payload = (req.model_dump() if req else {"boxes": "all", "restore": False})
    payload["boxes"] = [box]
    return _control(name, action, payload, db)


@router.post("/boxes/{box}/pxe-boot", response_model=JobRef, status_code=202,
             summary="set a box to network-boot")
def pxe_boot(
    name: str, box: str, req: PxeBootRequest | None = None,
    db: Session = Depends(get_db), user: User = Depends(_operator),
) -> JobRef:
    payload = req.model_dump() if req else {}
    payload["vm"] = box  # path param is the authoritative target
    return submit_operation(db, "pxe_boot", resolve_project(name), payload)
