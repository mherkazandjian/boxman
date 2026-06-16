"""
Redaction of sensitive-looking values before they leave the API.

Most operations carry no secrets (flags, names, paths), but `run` commands and
future operations might, and job params are returned by ``GET /jobs/{id}``. This
redacts values whose key hints at a secret, defensively.
"""

from __future__ import annotations

from typing import Any

_SENSITIVE_HINTS = ("password", "passwd", "secret", "token", "credential", "apikey", "api_key")
_REDACTED = "***redacted***"


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    return any(hint in k for hint in _SENSITIVE_HINTS)


def redact(params: dict[str, Any] | None) -> dict[str, Any]:
    if not params:
        return {}
    return {k: (_REDACTED if _is_sensitive(k) else v) for k, v in params.items()}
