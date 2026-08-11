"""
Custom Jinja2 environment helpers for boxman config templates.

Provides functions that can be used inside Jinja2 templates rendered
by boxman (e.g. conf.yml):

    {{ env("MY_VAR") }}
    {{ env("MY_VAR", default="fallback") }}
    {{ env_required("MY_VAR") }}
    {{ env_required("MY_VAR", "MY_VAR must be set") }}
    {{ env_is_set("MY_VAR") }}

Also owns the legacy inline ``${env:VAR}`` placeholder substitution
(:func:`substitute_env`) used outside Jinja2 contexts (cloud-init
userdata).
"""

import os
import re

from jinja2 import Environment, FileSystemLoader

from boxman.exceptions import ConfigError

_ENV_PLACEHOLDER = re.compile(r"\$\{env:([A-Za-z0-9_]+)\}")


def env(var_name: str, default: str = "") -> str:
    """
    Return the value of environment variable *var_name*.

    If the variable is not set, return *default* (empty string by default).
    """
    return os.environ.get(var_name, default)


def substitute_env(text: str) -> str:
    """
    Replace every ``${env:VAR}`` placeholder in *text* with the variable's
    value (empty string when unset).

    This is the legacy inline syntax, kept for non-Jinja2 payloads such as
    cloud-init userdata where ``{{ env(...) }}`` is not rendered.
    """
    return _ENV_PLACEHOLDER.sub(lambda m: env(m.group(1)), text)


def env_required(var_name: str, message: str = None) -> str:
    """
    Return the value of environment variable *var_name*.

    Raises :class:`~boxman.exceptions.ConfigError` if the variable is not
    set or is empty.
    """
    value = os.environ.get(var_name)
    if not value:
        msg = message or f"required environment variable '{var_name}' is not set"
        raise ConfigError(msg)
    return value


def env_is_set(var_name: str) -> bool:
    """
    Return ``True`` if the environment variable *var_name* is set and non-empty.
    """
    return bool(os.environ.get(var_name))


def create_jinja_env(search_path: str) -> Environment:
    """
    Create a Jinja2 :class:`Environment` with the boxman helper functions
    registered as globals.

    Args:
        search_path: Directory to use as the Jinja2 template search path.

    Returns:
        A configured :class:`jinja2.Environment`.
    """
    jinja_env = Environment(loader=FileSystemLoader(search_path))
    jinja_env.globals["env"] = env
    jinja_env.globals["env_required"] = env_required
    jinja_env.globals["env_is_set"] = env_is_set
    return jinja_env
