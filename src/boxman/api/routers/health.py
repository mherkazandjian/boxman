"""Liveness / readiness / version endpoints."""

from __future__ import annotations

import boxman
from fastapi import APIRouter

from boxman.api.schemas.common import Health

router = APIRouter(tags=["meta"])


def _version() -> str | None:
    try:
        return boxman.metadata.version
    except Exception:  # pragma: no cover - metadata always present in practice
        return None


@router.get("/healthz", response_model=Health, summary="liveness probe")
def healthz() -> Health:
    return Health(status="ok", version=_version())


@router.get("/version", summary="boxman version")
def version() -> dict[str, str | None]:
    return {"version": _version()}
