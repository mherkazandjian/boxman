"""
Read/write access to boxman's project cache (``~/.config/boxman/cache/projects.json``).

The API needs to resolve a project name → (conf path, runtime) to build CLI
invocations, and to register/unregister projects. boxman's own
:class:`~boxman.config_cache.BoxmanCache` has TOCTOU races (it read-modify-writes
the JSON with no locking), so registration/unregistration here is wrapped in an
``fcntl`` file lock. Plain reads tolerate a missing file.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass

DEFAULT_CACHE_FILE = "~/.config/boxman/cache/projects.json"


@dataclass
class ProjectEntry:
    name: str
    conf: str
    runtime: str

    @property
    def conf_dir(self) -> str:
        return os.path.dirname(self.conf)


def _cache_file() -> str:
    return os.path.expanduser(
        os.environ.get("BOXMAN_API_CACHE_FILE", DEFAULT_CACHE_FILE)
    )


@contextmanager
def _locked_cache():
    """Yield (path, dict) under an exclusive lock; persist on clean exit."""
    import fcntl

    path = _cache_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Open (creating if absent) for read+write and lock it.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 1 << 24).decode() or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        yield data
        encoded = json.dumps(data, indent=4).encode()
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, encoded)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_projects() -> dict[str, dict]:
    """Return the raw projects mapping (empty if the cache is absent)."""
    path = _cache_file()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as fobj:
            return json.load(fobj)
    except (json.JSONDecodeError, OSError):
        return {}


def list_projects() -> list[ProjectEntry]:
    return [
        ProjectEntry(name=name, conf=info.get("conf", ""), runtime=info.get("runtime", "local"))
        for name, info in read_projects().items()
    ]


def get_project(name: str) -> ProjectEntry | None:
    info = read_projects().get(name)
    if info is None:
        return None
    return ProjectEntry(name=name, conf=info.get("conf", ""), runtime=info.get("runtime", "local"))


def register_project(name: str, conf_path: str, runtime: str = "local") -> ProjectEntry:
    """Register a project; raises ValueError if the name already exists."""
    abs_conf = os.path.abspath(os.path.expanduser(conf_path))
    with _locked_cache() as data:
        if name in data:
            raise ValueError(f"project '{name}' already registered")
        data[name] = {"conf": abs_conf, "runtime": runtime}
    return ProjectEntry(name=name, conf=abs_conf, runtime=runtime)


def unregister_project(name: str) -> bool:
    """Remove a project from the cache; returns False if it was absent."""
    with _locked_cache() as data:
        return data.pop(name, None) is not None
