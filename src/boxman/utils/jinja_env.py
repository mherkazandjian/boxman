"""
Custom Jinja2 environment helpers for boxman config templates.

Provides functions that can be used inside Jinja2 templates rendered
by boxman (e.g. conf.yml):

    {{ env("MY_VAR") }}
    {{ env("MY_VAR", default="fallback") }}
    {{ env_required("MY_VAR") }}
    {{ env_required("MY_VAR", "MY_VAR must be set") }}
    {{ env_is_set("MY_VAR") }}
"""

import os
from jinja2 import Environment, FileSystemLoader, Undefined


def env(var_name: str, default: str = "") -> str:
    """
    Return the value of environment variable *var_name*.

    If the variable is not set, return *default* (empty string by default).
    """
    return os.environ.get(var_name, default)


def env_required(var_name: str, message: str = None) -> str:
    """
    Return the value of environment variable *var_name*.

    Raises ``ValueError`` if the variable is not set or is empty.
    """
    value = os.environ.get(var_name)
    if not value:
        msg = message or f"required environment variable '{var_name}' is not set"
        raise ValueError(msg)
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
