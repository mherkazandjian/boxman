"""
API configuration via pydantic-settings.

All settings can be overridden through ``BOXMAN_API_*`` environment variables
or a ``.env`` file. Defaults are chosen so the API is safe out of the box
(binds to localhost, sqlite store) and matches boxman's own conventions
(boxman.yml at ``~/.config/boxman/boxman.yml``).
"""

from __future__ import annotations

import os
import shlex
import sys
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BOXMAN_API_",
        env_file=".env",
        extra="ignore",
    )

    # ── network / server ──────────────────────────────────────────────
    host: str = "127.0.0.1"
    port: int = 8080
    cors_origins: list[str] = []

    # ── how to invoke the boxman CLI ──────────────────────────────────
    # Empty → `python -m boxman.scripts.app` (works installed and in dev mode).
    # Override with e.g. "boxman" or "sudo boxman" via BOXMAN_API_BOXMAN_CMD.
    boxman_cmd: str = ""
    boxman_conf: str = "~/.config/boxman/boxman.yml"

    # ── persistence / jobs ────────────────────────────────────────────
    database_url: str = "sqlite:///./boxman_api.db"
    redis_url: str = "redis://localhost:6379/0"
    job_log_dir: str = "~/.config/boxman/api/jobs"

    # ── auth ──────────────────────────────────────────────────────────
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    # First-run bootstrap admin (created if no users exist). Leave password
    # empty to disable auto-bootstrap.
    bootstrap_admin_user: str = "admin"
    bootstrap_admin_password: str = ""

    # ── operational ───────────────────────────────────────────────────
    # Read (synchronous) operations are killed after this many seconds.
    read_timeout_seconds: int = 120

    def boxman_argv(self) -> list[str]:
        """Return the base argv used to invoke the boxman CLI."""
        if self.boxman_cmd.strip():
            return shlex.split(self.boxman_cmd)
        return [sys.executable, "-m", "boxman.scripts.app"]

    @property
    def boxman_conf_path(self) -> str:
        return os.path.expanduser(self.boxman_conf)

    @property
    def job_log_path(self) -> str:
        return os.path.expanduser(self.job_log_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings()
