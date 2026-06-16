"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import boxman
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from boxman.api.auth.bootstrap import bootstrap_admin
from boxman.api.config import get_settings
from boxman.api.db.session import init_db
from boxman.api.routers import (
    auth,
    boxes,
    health,
    images,
    jobs,
    netlab,
    projects,
    run,
    snapshots,
    storage,
)

DESCRIPTION = (
    "HTTP API over the boxman declarative VM manager. Drives the boxman CLI "
    "under the hood, so it stays provider-agnostic (libvirt today; virtualbox "
    "/ containers later). Long-running operations are dispatched as jobs."
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Side effects (table creation, admin bootstrap) happen at startup, not at
    # import, so building the app is cheap and test-isolatable.
    init_db()
    bootstrap_admin()
    yield


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Boxman API",
        version=getattr(boxman.metadata, "version", "0"),
        description=DESCRIPTION,
        lifespan=lifespan,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(boxes.router)
    app.include_router(snapshots.router)
    app.include_router(storage.router)
    app.include_router(images.templates_router)
    app.include_router(images.images_router)
    app.include_router(run.router)
    app.include_router(netlab.router)
    app.include_router(jobs.router)

    return app


app = create_app()
