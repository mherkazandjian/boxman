"""Template creation (project-scoped) and image import/push (global)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from boxman.api.auth.deps import require_operator, require_project_access
from boxman.api.db.models import User
from boxman.api.db.session import get_db
from boxman.api.deps import resolve_project, submit_operation
from boxman.api.schemas.jobs import JobRef
from boxman.api.schemas.operations import (
    CreateTemplatesRequest,
    ImportImageRequest,
    PushImageRequest,
)

_ACCEPTED = status.HTTP_202_ACCEPTED

# Project-scoped: create template VMs from cloud images declared in conf.yml.
templates_router = APIRouter(prefix="/projects/{name}/templates", tags=["templates"])


@templates_router.post("", response_model=JobRef, status_code=_ACCEPTED,
                       summary="create template VMs from cloud images")
def create_templates(
    name: str, req: CreateTemplatesRequest, db: Session = Depends(get_db),
    user: User = Depends(require_project_access("operator")),
) -> JobRef:
    return submit_operation(db, "create_templates", resolve_project(name), req.model_dump(),
                            requested_by=user.username)


# Global: import/push images into provider storage / an OCI registry.
images_router = APIRouter(prefix="/images", tags=["images"])


@images_router.post("/import", response_model=JobRef, status_code=_ACCEPTED,
                    summary="import an image from a manifest")
def import_image(req: ImportImageRequest, db: Session = Depends(get_db),
                 user: User = Depends(require_operator)) -> JobRef:
    return submit_operation(db, "import_image", None, req.model_dump(),
                            requested_by=user.username)


@images_router.post("/push", response_model=JobRef, status_code=_ACCEPTED,
                    summary="push a qcow2 image to an OCI registry")
def push_image(req: PushImageRequest, db: Session = Depends(get_db),
               user: User = Depends(require_operator)) -> JobRef:
    return submit_operation(db, "push_image", None, req.model_dump(),
                            requested_by=user.username)
