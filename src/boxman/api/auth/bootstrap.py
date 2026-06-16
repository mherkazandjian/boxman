"""
First-run admin bootstrap.

If the users table is empty, create an admin so the API is usable. The password
comes from ``BOXMAN_API_BOOTSTRAP_ADMIN_PASSWORD`` when set, otherwise a random
one is generated and logged once (the only time it is ever shown).
"""

from __future__ import annotations

import secrets

from sqlalchemy import select

from boxman import log
from boxman.api.auth.security import hash_password
from boxman.api.config import get_settings
from boxman.api.db.models import Role, User
from boxman.api.db.session import session_scope


def bootstrap_admin() -> None:
    settings = get_settings()
    with session_scope() as session:
        if session.execute(select(User.id).limit(1)).first() is not None:
            return  # users already exist — nothing to do

        password = settings.bootstrap_admin_password or secrets.token_urlsafe(16)
        generated = not settings.bootstrap_admin_password

        admin = User(
            username=settings.bootstrap_admin_user,
            hashed_password=hash_password(password),
            role=Role.admin.value,
        )
        session.add(admin)

    if generated:
        log.warning(
            "bootstrap admin created: username=%s password=%s "
            "(shown once — set BOXMAN_API_BOOTSTRAP_ADMIN_PASSWORD to control it)",
            settings.bootstrap_admin_user,
            password,
        )
    else:
        log.info("bootstrap admin '%s' created", settings.bootstrap_admin_user)
