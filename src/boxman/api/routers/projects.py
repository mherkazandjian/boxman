"""
Project registration and project-scoped read endpoints.

Reads run synchronously off the request thread (via the CLI shim). Mutating /
long-running lifecycle endpoints (provision/up/down/destroy/...) are added in
the jobs phase and return ``202 {job_id}``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from boxman.api import cache
from boxman.api.auth import rbac
from boxman.api.auth.deps import get_current_user, require_operator, require_project_access
from boxman.api.capabilities import caps_for
from boxman.api.db.models import User
from boxman.api.db.session import get_db
from boxman.api.deps import detect_provider, resolve_project, run_read, submit_operation
from boxman.api.operations import OPERATIONS
from boxman.api.schemas.common import BoxSelector
from boxman.api.schemas.jobs import JobRef
from boxman.api.schemas.projects import (
    Capabilities,
    DeprovisionRequest,
    DestroyRequest,
    DownRequest,
    ProjectRef,
    ProjectStatus,
    ProvisionRequest,
    RegisterProjectRequest,
    UpdateRequest,
    UpRequest,
)

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectRef], summary="list registered projects")
def list_projects(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[ProjectRef]:
    allowed = rbac.accessible_projects(db, user)  # None → admin sees all
    return [
        ProjectRef(name=e.name, conf=e.conf, runtime=e.runtime)
        for e in cache.list_projects()
        if allowed is None or e.name in allowed
    ]


@router.post(
    "",
    response_model=ProjectRef,
    status_code=status.HTTP_201_CREATED,
    summary="register a project (conf path + runtime)",
)
def register_project(
    req: RegisterProjectRequest,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> ProjectRef:
    try:
        entry = cache.register_project(req.name, req.conf, req.runtime)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    # Grant the creator operator access so they can manage what they registered.
    rbac.grant_project(db, user, entry.name, rbac.Role.operator.value)
    db.commit()
    return ProjectRef(name=entry.name, conf=entry.conf, runtime=entry.runtime)


@router.delete(
    "/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="unregister a project",
)
def unregister_project(
    name: str, user: User = Depends(require_project_access("operator"))
) -> Response:
    if not cache.unregister_project(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"project '{name}' is not registered",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{name}", summary="effective merged configuration")
def get_project_conf(
    name: str, user: User = Depends(require_project_access("viewer"))
) -> Any:
    entry = resolve_project(name)
    return run_read(OPERATIONS["show_conf"], entry).json()


@router.get("/{name}/capabilities", response_model=Capabilities, summary="provider capabilities")
def get_capabilities(
    name: str, user: User = Depends(require_project_access("viewer"))
) -> Capabilities:
    entry = resolve_project(name)
    provider = detect_provider(entry)
    return Capabilities(provider=provider, caps=sorted(caps_for(provider)))


@router.get("/{name}/status", response_model=ProjectStatus, summary="per-box state (ps)")
def get_status(
    name: str, user: User = Depends(require_project_access("viewer"))
) -> ProjectStatus:
    entry = resolve_project(name)
    data = run_read(OPERATIONS["ps"], entry).json()
    boxes = data if isinstance(data, list) else data.get("vms", data)
    return ProjectStatus(project=name, boxes=boxes)


@router.get("/{name}/snapshots", summary="aggregated snapshot log")
def get_snapshots(
    name: str, boxes: str = "all", user: User = Depends(require_project_access("viewer"))
) -> Any:
    entry = resolve_project(name)
    payload = BoxSelector(boxes=boxes).as_payload()
    return run_read(OPERATIONS["snapshot_log"], entry, payload).json()


# ── lifecycle (mutating → jobs) ───────────────────────────────────────

_ACCEPTED = status.HTTP_202_ACCEPTED
_operator = require_project_access("operator")


@router.post("/{name}/provision", response_model=JobRef, status_code=_ACCEPTED,
             summary="provision the project")
def provision(name: str, req: ProvisionRequest, db: Session = Depends(get_db),
              user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "provision", resolve_project(name), req.model_dump(),
                            requested_by=user.username)


@router.post("/{name}/up", response_model=JobRef, status_code=_ACCEPTED,
             summary="bring the project up (provision or start)")
def up(name: str, req: UpRequest, db: Session = Depends(get_db),
       user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "up", resolve_project(name), req.model_dump(),
                            requested_by=user.username)


@router.post("/{name}/down", response_model=JobRef, status_code=_ACCEPTED,
             summary="bring the project down (save or suspend)")
def down(name: str, req: DownRequest, db: Session = Depends(get_db),
         user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "down", resolve_project(name), req.model_dump(),
                            requested_by=user.username)


@router.post("/{name}/deprovision", response_model=JobRef, status_code=_ACCEPTED,
             summary="destroy VMs and networks")
def deprovision(name: str, req: DeprovisionRequest, db: Session = Depends(get_db),
                user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "deprovision", resolve_project(name), req.model_dump(),
                            requested_by=user.username)


@router.post("/{name}/update", response_model=JobRef, status_code=_ACCEPTED,
             summary="apply config changes to running VMs")
def update(name: str, req: UpdateRequest, db: Session = Depends(get_db),
           user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "update", resolve_project(name), req.model_dump(),
                            requested_by=user.username)


@router.post("/{name}/destroy", response_model=JobRef, status_code=_ACCEPTED,
             summary="nuke everything (requires confirm=true)")
def destroy(name: str, req: DestroyRequest, db: Session = Depends(get_db),
            user: User = Depends(_operator)) -> JobRef:
    return submit_operation(db, "destroy", resolve_project(name), req.model_dump(),
                            requested_by=user.username)
