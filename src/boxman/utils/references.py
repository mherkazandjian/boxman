"""
Helpers for resolving value references found in boxman config files.

A config value can be any of:

- A literal string (returned unchanged).
- ``${env:VAR}`` — looked up in the process environment.
- ``file:///absolute/path`` or ``file://~/relative/path`` — reads the
  file's contents, trailing newline stripped.
- Anything non-string (``int``, ``None``, ``dict``, ...) — returned
  unchanged.

This used to be a classmethod on :class:`boxman.manager.BoxmanManager`
(``fetch_value``) before Phase 2.4 of the review plan split it out.
``BoxmanManager.fetch_value`` remains as a thin wrapper so callers that
reach in via the class still work.
"""

from __future__ import annotations

import os
import re
from typing import Any


_ENV_PATTERN = re.compile(r"\$\{env:(.+)\}")


def resolve_reference(value: Any) -> Any:
    """
    Resolve *value* if it's a reference string, otherwise return it as-is.

    Raises:
        ValueError: If ``${env:VAR}`` names an unset variable.
        FileNotFoundError: If ``file://…`` points at a missing file.
    """
    if not isinstance(value, str):
        return value

    env_match = _ENV_PATTERN.fullmatch(value)
    if env_match:
        var = env_match.group(1)
        if var not in os.environ:
            raise ValueError(f"environment variable '{var}' is not set")
        return os.environ[var]

    if value.startswith("file://"):
        path = os.path.expanduser(value[len("file://"):])
        if not os.path.isfile(path):
            raise FileNotFoundError(f"referenced file does not exist: {path}")
        with open(path) as fobj:
            return fobj.read().rstrip("\n")

    return value
