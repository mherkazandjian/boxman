"""
Console-script entry points: ``boxman-api`` (uvicorn server) and
``boxman-worker`` (Celery worker).
"""

from __future__ import annotations

import sys


def serve() -> None:
    """Run the API server (``boxman-api``)."""
    import uvicorn

    from boxman.api.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "boxman.api.main:app",
        host=settings.host,
        port=settings.port,
        reload="--reload" in sys.argv,
    )


def worker() -> None:
    """Run a Celery worker (``boxman-worker``)."""
    try:
        from boxman.api.jobs.celery_app import celery_app
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Celery jobs are not available — install the 'api' extra "
            f"(celery, redis): {exc}"
        )

    argv = ["worker", "--loglevel=info"] + sys.argv[1:]
    celery_app.worker_main(argv)
