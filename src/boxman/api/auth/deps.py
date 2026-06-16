"""FastAPI auth dependencies: current user + role/project guards."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from jose import JWTError
from sqlalchemy.orm import Session

from boxman.api.auth import rbac
from boxman.api.auth.security import decode_token, oauth2_scheme
from boxman.api.db.models import Role, User
from boxman.api.db.session import get_db

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
    except JWTError:
        raise _CREDENTIALS_ERROR
    if not user_id:
        raise _CREDENTIALS_ERROR
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _CREDENTIALS_ERROR
    return user


def require_global_role(min_role: str) -> Callable[..., User]:
    """Dependency factory: caller must have at least ``min_role`` globally."""

    def dependency(user: User = Depends(get_current_user)) -> User:
        if not rbac.has_global_role(user, min_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires global role '{min_role}'",
            )
        return user

    return dependency


def require_project_access(min_role: str) -> Callable[..., User]:
    """Dependency factory: caller must have ``min_role`` on path param ``name``."""

    def dependency(
        name: str,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if not rbac.has_project_access(db, user, name, min_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires role '{min_role}' on project '{name}'",
            )
        return user

    return dependency


# Convenience singletons for the common levels.
require_admin = require_global_role(Role.admin.value)
require_operator = require_global_role(Role.operator.value)
