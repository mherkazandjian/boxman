"""
Shared pytest fixtures for the boxman test suite.

Added 2026-04-21 as part of Phase 1.1 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).

Fixtures:

    tmp_workspace    — an isolated boxman workspace rooted in ``tmp_path``
                       with a minimal ``boxman.yml`` and an empty cache dir.
    fake_virsh       — a ``MagicMock`` that emits canned virsh-style output
                       on demand. Used by libvirt unit tests to avoid
                       needing a live libvirtd.
    fake_runtime     — a stub Runtime whose ``wrap_command`` is identity
                       and whose ``ensure_ready`` is a no-op. Lets provider
                       tests skip docker entirely.
    captured_logs    — thin wrapper around pytest's ``caplog`` that attaches
                       to boxman's module-level ``log`` singleton, so tests
                       can assert on what the code logged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Workspace fixture
# ---------------------------------------------------------------------------

MINIMAL_BOXMAN_YML = """\
version: '1.0'
workdir: {workdir}
provider:
  libvirt:
    uri: qemu:///system
    use_sudo: false
    virsh_cmd: /usr/bin/virsh
runtime:
  name: local
"""


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """
    Build an isolated boxman workspace under *tmp_path*.

    Layout:

        <tmp_path>/
            boxman.yml
            cache/
                projects.json   (empty {})
            workdir/
    """
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "projects.json").write_text("{}\n")
    (tmp_path / "boxman.yml").write_text(
        MINIMAL_BOXMAN_YML.format(workdir=str(workdir))
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Fake virsh — canned output for libvirt unit tests
# ---------------------------------------------------------------------------

class FakeVirshResult:
    """Mimic the shape of an ``invoke.Result``: .stdout/.stderr/.ok/.return_code."""

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        return_code: int = 0,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code
        self.ok = return_code == 0
        self.failed = not self.ok


@pytest.fixture
def fake_virsh() -> MagicMock:
    """
    Return a MagicMock preconfigured with common virsh outputs.

    Use ``fake_virsh.set_output(pattern, stdout)`` to register responses.
    The mock's ``.run(cmd)`` method matches ``cmd`` against registered
    patterns (substring match) and returns a ``FakeVirshResult``.

    Default responses cover ``virsh list --all`` (empty) and
    ``virsh net-list --all`` (empty).
    """
    mock = MagicMock(name="fake_virsh")
    responses: dict[str, FakeVirshResult] = {
        "list --all": FakeVirshResult(stdout=" Id   Name   State\n-----------------\n"),
        "net-list --all": FakeVirshResult(stdout=" Name   State   Autostart   Persistent\n"),
    }

    def set_output(pattern: str, stdout: str = "", return_code: int = 0) -> None:
        responses[pattern] = FakeVirshResult(stdout=stdout, return_code=return_code)

    def run(command: str, *_args: Any, **_kwargs: Any) -> FakeVirshResult:
        for pattern, result in responses.items():
            if pattern in command:
                return result
        return FakeVirshResult(stdout="", return_code=0)

    mock.set_output = set_output
    mock.run = run
    mock.responses = responses
    return mock


# ---------------------------------------------------------------------------
# Fake runtime — decouple provider tests from runtime classes
# ---------------------------------------------------------------------------

class FakeRuntime:
    """A Runtime stub that passes commands through unchanged."""

    name: str = "fake"

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.ensure_ready_calls = 0

    def wrap_command(self, command: str) -> str:
        return command

    def ensure_ready(self) -> None:
        self.ensure_ready_calls += 1

    def inject_into_provider_config(
        self, provider_config: dict[str, Any]
    ) -> dict[str, Any]:
        cfg = dict(provider_config)
        cfg["runtime"] = self.name
        return cfg


@pytest.fixture
def fake_runtime() -> FakeRuntime:
    return FakeRuntime()


# ---------------------------------------------------------------------------
# Captured logs — attach caplog to boxman's module-level singleton
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """
    Configure ``caplog`` to capture records from the ``boxman`` logger.

    The boxman package exposes a module-level ``log`` (see
    ``src/boxman/__init__.py`` → ``loggers/logger.py``) with
    ``propagate = False``, which means pytest's default ``caplog`` misses
    its records. This fixture re-enables propagation for the duration of
    the test so assertions against log output work.
    """
    boxman_logger = logging.getLogger("boxman")
    previous_propagate = boxman_logger.propagate
    boxman_logger.propagate = True
    caplog.set_level(logging.DEBUG, logger="boxman")
    try:
        yield caplog
    finally:
        boxman_logger.propagate = previous_propagate


# ---------------------------------------------------------------------------
# Small helpers reusable across tests
# ---------------------------------------------------------------------------

@pytest.fixture
def write_json(tmp_path: Path):
    """Return a helper that writes a JSON blob to a tmp path."""

    def _write(name: str, payload: Any) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(payload))
        return path

    return _write
