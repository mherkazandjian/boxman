"""
Shared project-name sanitizer for docker-compose contexts (#85 item 34).

Two call sites sanitize a project/cluster name into something docker or
``docker compose -p`` accepts, with **deliberately different rules** —
changing either one would rename existing compose projects / containers
and orphan the running state:

* the docker-compose *runtime* (:class:`~boxman.runtime.docker_compose.
  DockerComposeRuntime`, the libvirt-in-a-container wrapper) maps every
  disallowed character — including ``_`` — to ``-`` and strips leading
  and trailing ``-``;
* the docker-compose *provider* (:mod:`boxman.providers.docker_compose.
  session`, one compose project per cluster) keeps ``_``, maps other
  disallowed characters to ``_``, strips leading ``_``/``-`` only, and
  falls back to ``boxman`` when nothing is left.

:func:`sanitize_project_name` is the single implementation; the
``allow_underscore`` flag selects the rule set so each call site keeps
its observable output.
"""

from __future__ import annotations

import re


def sanitize_project_name(name: str, *, allow_underscore: bool = False) -> str:
    """
    Coerce *name* into a docker-compose-safe project name.

    Args:
        name: the raw project/cluster name.
        allow_underscore: select the rule set (see the module docstring):

            - ``False`` (docker-compose *runtime*): ``[^a-z0-9-]`` → ``-``,
              leading/trailing ``-`` stripped. May return ``""``.
            - ``True`` (docker-compose *provider*): ``[^a-z0-9_-]`` → ``_``,
              leading ``_-`` stripped, ``"boxman"`` when empty.
    """
    if allow_underscore:
        slug = re.sub(r"[^a-z0-9_-]", "_", name.lower()).lstrip("_-")
        return slug or "boxman"
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
