"""Auth request/response models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boxman.api.db.models import Role


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1)
    role: Role = Role.viewer


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    role: str
    is_active: bool


class GrantCreate(BaseModel):
    project: str
    role: Role = Role.viewer


class GrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project: str
    role: str
