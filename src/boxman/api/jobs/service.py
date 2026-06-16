"""
Job orchestration helpers used by the routers: enqueue an operation, query
jobs, and cancel a job.
"""

from __future__ import annotations

import os
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from boxman.api import cache
from boxman.api.config import get_settings
from boxman.api.db.models import ACTIVE_JOB_STATES, Job, JobState
from boxman.api.operations import OPERATIONS


class JobConflict(Exception):
    """Raised when a project already has an active (pending/running) job."""

    def __init__(self, project: str, job_id: str):
        self.project = project
        self.job_id = job_id
        super().__init__(
            f"project '{project}' already has an active job ({job_id})"
        )


def active_job_for_project(db: Session, project: str | None) -> Job | None:
    if not project:
        return None
    stmt = select(Job).where(
        Job.project == project, Job.state.in_(ACTIVE_JOB_STATES)
    )
    return db.execute(stmt).scalars().first()


def enqueue(
    db: Session,
    op_name: str,
    *,
    project_entry: "cache.ProjectEntry | None" = None,
    payload: dict | None = None,
    requested_by: str | None = None,
) -> Job:
    """Create a Job row and dispatch the Celery task.

    Raises :class:`JobConflict` if the project already has an active job.
    The Celery task fills in ``celery_id`` and the terminal state itself, so we
    don't write back to the job after dispatch (avoids clobbering eager-mode
    results that run inline).
    """
    op = OPERATIONS[op_name]
    project_name = project_entry.name if project_entry else None

    existing = active_job_for_project(db, project_name)
    if existing is not None:
        raise JobConflict(project_name, existing.id)

    settings = get_settings()
    job_id = uuid.uuid4().hex
    os.makedirs(settings.job_log_path, exist_ok=True)

    job = Job(
        id=job_id,
        operation=op_name,
        project=project_name,
        params=payload or {},
        conf_path=(project_entry.conf if (project_entry and op.needs_conf) else None),
        runtime=(project_entry.runtime if project_entry else None),
        requested_by=requested_by,
        log_path=os.path.join(settings.job_log_path, f"{job_id}.log"),
        state=JobState.pending.value,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Imported lazily so importing this module doesn't require Celery/redis.
    from boxman.api.jobs.tasks import run_operation

    run_operation.delay(job_id)
    return job


def cancel(db: Session, job: Job) -> None:
    """Revoke the Celery task and mark the job canceled (best-effort)."""
    if job.state not in ACTIVE_JOB_STATES:
        return
    if job.celery_id:
        try:
            from boxman.api.jobs.celery_app import celery_app

            celery_app.control.revoke(job.celery_id, terminate=True, signal="SIGTERM")
        except Exception:  # pragma: no cover - revoke is best-effort
            pass
    job.state = JobState.canceled.value
    db.commit()
