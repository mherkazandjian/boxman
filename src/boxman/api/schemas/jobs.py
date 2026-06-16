"""Job request/response models."""

from __future__ import annotations

import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from boxman.api.redact import redact


class JobRef(BaseModel):
    """Minimal reference returned by the 202 of a mutating endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    state: str
    operation: str
    project: str | None = None


class JobDetail(JobRef):
    model_config = ConfigDict(from_attributes=True)

    celery_id: str | None = None
    params: dict[str, Any] = {}
    exit_code: int | None = None
    error: str | None = None
    requested_by: str | None = None
    created_at: datetime.datetime | None = None
    started_at: datetime.datetime | None = None
    finished_at: datetime.datetime | None = None

    @model_validator(mode="after")
    def _redact_params(self) -> "JobDetail":
        self.params = redact(self.params)
        return self
