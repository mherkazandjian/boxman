"""
Per-project distributed lock (Redis ``SETNX``).

Prevents two mutating operations from running against the same project at once
(e.g. provision racing destroy). Global operations (no project) are not locked.
The lock has a generous TTL so a crashed worker eventually releases it.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from collections.abc import Iterator

from boxman.api.config import get_settings


class ProjectBusy(Exception):
    """Raised when another operation already holds the project lock."""

    def __init__(self, project: str):
        self.project = project
        super().__init__(f"another operation is already running for project '{project}'")


def _redis():
    import redis  # imported lazily so the package imports without redis present

    return redis.Redis.from_url(get_settings().redis_url)


@contextmanager
def project_lock(project: str | None, ttl_seconds: int = 6 * 3600) -> Iterator[None]:
    if not project:
        yield
        return

    client = _redis()
    key = f"boxman:lock:project:{project}"
    token = uuid.uuid4().hex
    if not client.set(key, token, nx=True, ex=ttl_seconds):
        raise ProjectBusy(project)
    try:
        yield
    finally:
        # Best-effort release; only delete if we still own it.
        try:
            if client.get(key) == token.encode():
                client.delete(key)
        except Exception:  # pragma: no cover - cleanup must not mask errors
            pass
