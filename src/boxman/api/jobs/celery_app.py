"""
Celery application.

Broker and result backend both default to Redis (``BOXMAN_API_REDIS_URL``).
Tasks run boxman via :mod:`boxman.api.cli_runner` in a *subprocess*, so the
default prefork (daemonic) worker pool is fine — boxman's own multiprocessing
runs in that non-daemonic child, not in the worker process itself.
"""

from __future__ import annotations

from celery import Celery

from boxman.api.config import get_settings


def make_celery() -> Celery:
    settings = get_settings()
    app = Celery(
        "boxman",
        broker=settings.redis_url,
        backend=settings.redis_url,
        include=["boxman.api.jobs.tasks"],
    )
    app.conf.update(
        task_track_started=True,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_acks_late=True,
        worker_hijack_root_logger=False,
        # Eager mode (tests) runs tasks inline and propagates exceptions.
        task_eager_propagates=True,
    )
    return app


celery_app = make_celery()
