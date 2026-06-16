"""Project-scoped request/response models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProjectRef(BaseModel):
    name: str
    conf: str
    runtime: str


class RegisterProjectRequest(BaseModel):
    name: str = Field(..., description="unique project name")
    conf: str = Field(..., description="path to the project's conf.yml")
    runtime: str = Field("local", description="runtime: local | docker-compose")


class Capabilities(BaseModel):
    provider: str
    caps: list[str]


class ProjectStatus(BaseModel):
    """Output of ``boxman ps --json`` — a list of per-box state records.

    The exact record shape is provider-defined, so it is passed through as a
    list of dicts rather than over-constrained here.
    """

    project: str
    boxes: list[dict[str, Any]]


# ── lifecycle operation request bodies ────────────────────────────────


class ProvisionRequest(BaseModel):
    force: bool = False
    rebuild_templates: bool = False
    docker_compose: bool = False


class UpRequest(ProvisionRequest):
    pass


class DownRequest(BaseModel):
    suspend: bool = Field(False, description="pause instead of saving state to disk")


class DeprovisionRequest(BaseModel):
    cleanup: bool = Field(False, description="also remove files, SSH keys, dirs")
    docker_compose: bool = False


class DestroyRequest(BaseModel):
    confirm: bool = Field(..., description="must be true — destroy is irreversible")
    templates: bool = Field(False, description="also remove template workdirs")


class UpdateRequest(BaseModel):
    dry_run: bool = False
    docker_compose: bool = False
