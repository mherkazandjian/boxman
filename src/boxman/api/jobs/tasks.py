"""
The Celery task that executes a job.

The task receives only a ``job_id``; everything it needs (operation, params,
conf path, runtime, log path) is read from the persisted :class:`Job` row. It
marks the job running, runs the boxman CLI in a subprocess streaming output to
the job's log file, then records the exit code / final state.
"""

from __future__ import annotations

import datetime

from boxman.api.cli_runner import stream_to_file
from boxman.api.db.models import Job, JobState
from boxman.api.db.session import session_scope
from boxman.api.jobs.celery_app import celery_app
from boxman.api.jobs.locks import ProjectBusy, project_lock
from boxman.api.operations import OPERATIONS


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@celery_app.task(bind=True, name="boxman.run_operation")
def run_operation(self, job_id: str) -> dict:
    # ── mark running, snapshot what we need ──────────────────────────
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return {"job_id": job_id, "state": "missing"}
        if job.state == JobState.canceled.value:
            return {"job_id": job_id, "state": job.state}
        job.celery_id = self.request.id
        job.state = JobState.running.value
        job.started_at = _now()
        op_name = job.operation
        project = job.project
        params = dict(job.params or {})
        log_path = job.log_path
        conf_path = job.conf_path
        runtime = job.runtime

    op = OPERATIONS[op_name]
    exit_code: int | None = None
    error: str | None = None

    # ── run the operation under the per-project lock ─────────────────
    try:
        with project_lock(project):
            exit_code = stream_to_file(
                op, params, log_path, conf_path=conf_path, runtime=runtime
            )
    except ProjectBusy as exc:
        error = str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        error = f"{type(exc).__name__}: {exc}"

    # ── record final state (unless canceled mid-flight) ──────────────
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None or job.state == JobState.canceled.value:
            return {"job_id": job_id, "state": "canceled"}
        job.exit_code = exit_code
        job.error = error
        job.finished_at = _now()
        if error is not None:
            job.state = JobState.failed.value
        else:
            job.state = (
                JobState.completed.value if exit_code == 0 else JobState.failed.value
            )
        final = job.state

    return {"job_id": job_id, "state": final, "exit_code": exit_code}
