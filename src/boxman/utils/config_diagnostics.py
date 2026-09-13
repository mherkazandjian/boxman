"""One-line diagnostics for the config load path.

A malformed ``conf.yml`` used to escape the typed-error boundary in
:func:`boxman.scripts.app.main`: PyYAML and Jinja2 raise their own
exception types, so the CLI printed a multi-line traceback and exited 1
instead of one line and exit 2 (#164 X6). These helpers turn either into
a :class:`~boxman.exceptions.ConfigError` whose message names the file
and, where the library reports one, the position.
"""

from __future__ import annotations

import jinja2
import yaml

from boxman.exceptions import ConfigError


def _one_line(text: object) -> str:
    """Collapse a multi-line library message onto one line."""
    return ' '.join(str(text).split())


def yaml_config_error(exc: yaml.YAMLError,
                      path: str,
                      rendered_path: str | None = None) -> ConfigError:
    """A ConfigError naming the file and where PyYAML gave up.

    Args:
        exc: the error PyYAML raised.
        path: the config file the user named, as they typed it.
        rendered_path: the on-disk rendered copy, when one was written.
            Positions come from *that* text — Jinja2 control blocks shift
            the line numbering — so it is named when it is available.
    """
    problem = getattr(exc, 'problem', None)
    mark = getattr(exc, 'problem_mark', None)
    if problem:
        detail = _one_line(problem)
        if mark is not None:
            # PyYAML counts from 0; editors count from 1.
            detail += f" at line {mark.line + 1}, column {mark.column + 1}"
    else:
        detail = _one_line(exc)

    message = f"invalid YAML in {path}: {detail}"
    if rendered_path:
        message += f" (positions refer to the rendered copy: {rendered_path})"
    return ConfigError(message)


def template_config_error(exc: jinja2.TemplateError, path: str) -> ConfigError:
    """A ConfigError naming the file and the template line, when Jinja2
    reports one — it does for syntax errors, not for every runtime error.
    """
    lineno = getattr(exc, 'lineno', None)
    where = f" at line {lineno}" if lineno else ""
    detail = _one_line(getattr(exc, 'message', None) or exc)
    return ConfigError(f"could not render {path}{where}: {detail}")
