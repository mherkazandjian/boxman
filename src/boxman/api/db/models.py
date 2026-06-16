"""ORM models: User, ProjectGrant, Job."""

from __future__ import annotations

import datetime
import enum
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from boxman.api.db.session import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Role(str, enum.Enum):
    admin = "admin"
    operator = "operator"
    viewer = "viewer"


#: ordering used for "at least this role" checks (higher = more privilege)
ROLE_RANK = {Role.viewer.value: 0, Role.operator.value: 1, Role.admin.value: 2}


class JobState(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


ACTIVE_JOB_STATES = (JobState.pending.value, JobState.running.value)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    # global role: admin bypasses per-project grants; operator/viewer need grants
    role: Mapped[str] = mapped_column(String(16), default=Role.viewer.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    grants: Mapped[list["ProjectGrant"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ProjectGrant(Base):
    __tablename__ = "project_grants"
    __table_args__ = (UniqueConstraint("user_id", "project", name="uq_user_project"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(16), default=Role.viewer.value)

    user: Mapped["User"] = relationship(back_populates="grants")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    celery_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation: Mapped[str] = mapped_column(String(64), index=True)
    project: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)

    # resolved at enqueue time so the worker needs only the job id
    conf_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    runtime: Mapped[str | None] = mapped_column(String(32), nullable=True)

    state: Mapped[str] = mapped_column(
        String(16), default=JobState.pending.value, index=True
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    log_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
