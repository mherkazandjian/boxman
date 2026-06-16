"""Password hashing and JWT issue/verify."""

from __future__ import annotations

import datetime
from typing import Any

import bcrypt
from fastapi.security import OAuth2PasswordBearer
from jose import jwt

from boxman.api.config import get_settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")


def hash_password(plain: str) -> str:
    """Hash a password with bcrypt.

    bcrypt only considers the first 72 bytes; longer inputs are rejected by the
    library, which is fine for an operator-facing tool.
    """
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):  # pragma: no cover - malformed hash
        return False


def create_access_token(
    subject: str, role: str, expires_minutes: int | None = None
) -> str:
    settings = get_settings()
    now = datetime.datetime.now(datetime.timezone.utc)
    expire = now + datetime.timedelta(
        minutes=expires_minutes or settings.jwt_expire_minutes
    )
    payload: dict[str, Any] = {"sub": subject, "role": role, "iat": now, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
