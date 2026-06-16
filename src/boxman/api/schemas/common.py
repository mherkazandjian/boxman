"""Shared response/request models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Health(BaseModel):
    status: str = "ok"
    version: str | None = None


class ErrorEnvelope(BaseModel):
    detail: str
    code: str | None = None
    # Captured CLI output, when an operation failed while shelling out.
    output: str | None = None


class CommandResult(BaseModel):
    """Result of a synchronous (read) operation that shelled out to the CLI."""

    ok: bool
    returncode: int
    data: Any | None = None
    stdout: str | None = None
    stderr: str | None = None


class BoxSelector(BaseModel):
    """Selects which boxes (VMs) an operation targets.

    Mirrors the CLI ``--vms`` flag: ``"all"`` or an explicit list of names.
    """

    boxes: str | list[str] = "all"

    def as_payload(self) -> dict[str, Any]:
        return {"boxes": self.boxes}
