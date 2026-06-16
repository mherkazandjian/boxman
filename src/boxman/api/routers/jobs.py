"""Job status / log / cancel endpoints (RBAC-scoped by project)."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from boxman.api.auth import rbac
from boxman.api.auth.deps import get_current_user
from boxman.api.db.models import Job, User
from boxman.api.db.session import get_db
from boxman.api.jobs import service
from boxman.api.schemas.jobs import JobDetail, JobRef

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _get_job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return job


def _authorize_job(db: Session, user: User, job: Job, min_role: str) -> None:
    """A job inherits its project's access rules; global jobs need a global role."""
    if rbac.is_admin(user):
        return
    if job.project is None:
        if not rbac.has_global_role(user, min_role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        return
    if not rbac.has_project_access(db, user, job.project, min_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


@router.get("", response_model=list[JobRef], summary="list jobs")
def list_jobs(
    project: str | None = Query(None),
    state: str | None = Query(None),
    limit: int = Query(100, le=1000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Job]:
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if project:
        stmt = stmt.where(Job.project == project)
    if state:
        stmt = stmt.where(Job.state == state)

    allowed = rbac.accessible_projects(db, user)  # None → admin sees all
    jobs = list(db.execute(stmt).scalars().all())
    if allowed is None:
        return jobs
    return [j for j in jobs if j.project in allowed]


@router.get("/{job_id}", response_model=JobDetail, summary="job detail")
def get_job(
    job_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Job:
    job = _get_job_or_404(db, job_id)
    _authorize_job(db, user, job, "viewer")
    return job


@router.get("/{job_id}/log", response_class=PlainTextResponse, summary="captured job log")
def get_job_log(
    job_id: str,
    tail: int | None = Query(None, description="return only the last N lines"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> str:
    job = _get_job_or_404(db, job_id)
    _authorize_job(db, user, job, "viewer")
    if not job.log_path or not os.path.isfile(job.log_path):
        return ""
    with open(job.log_path, errors="replace") as fobj:
        if tail is None:
            return fobj.read()
        return "".join(fobj.readlines()[-tail:])


@router.delete("/{job_id}", response_model=JobDetail, summary="cancel a job")
def cancel_job(
    job_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Job:
    job = _get_job_or_404(db, job_id)
    _authorize_job(db, user, job, "operator")
    service.cancel(db, job)
    db.refresh(job)
    return job
