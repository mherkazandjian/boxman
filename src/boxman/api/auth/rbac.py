"""
Role-based access control helpers.

Two layers of authorization:
  - a *global* role on the user (admin / operator / viewer)
  - per-project *grants* (a role scoped to one project)

``admin`` bypasses all checks. For everyone else, access to a project requires
a grant whose role rank meets the required minimum.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from boxman.api.db.models import ROLE_RANK, ProjectGrant, Role, User


def rank(role: str) -> int:
    return ROLE_RANK.get(role, -1)


def is_admin(user: User) -> bool:
    return user.role == Role.admin.value


def project_role(db: Session, user: User, project: str) -> str | None:
    """The user's effective role on a project, or None if no access."""
    if is_admin(user):
        return Role.admin.value
    stmt = select(ProjectGrant).where(
        ProjectGrant.user_id == user.id, ProjectGrant.project == project
    )
    grant = db.execute(stmt).scalars().first()
    return grant.role if grant else None


def has_project_access(db: Session, user: User, project: str, min_role: str) -> bool:
    role = project_role(db, user, project)
    return role is not None and rank(role) >= rank(min_role)


def has_global_role(user: User, min_role: str) -> bool:
    return rank(user.role) >= rank(min_role)


def accessible_projects(db: Session, user: User) -> set[str] | None:
    """Projects the user may see. ``None`` means 'all' (admin)."""
    if is_admin(user):
        return None
    stmt = select(ProjectGrant.project).where(ProjectGrant.user_id == user.id)
    return set(db.execute(stmt).scalars().all())


def grant_project(db: Session, user: User, project: str, role: str) -> ProjectGrant:
    """Idempotently grant (or upgrade) a user's role on a project."""
    stmt = select(ProjectGrant).where(
        ProjectGrant.user_id == user.id, ProjectGrant.project == project
    )
    grant = db.execute(stmt).scalars().first()
    if grant is None:
        grant = ProjectGrant(user_id=user.id, project=project, role=role)
        db.add(grant)
    elif rank(role) > rank(grant.role):
        grant.role = role
    return grant
