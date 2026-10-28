"""
Unit tests for boxman.utils.io.mkstemp_in.

It stands in for ``tempfile.mkstemp(dir=...)`` wherever a file is staged
beside the one it replaces: since Python 3.12 mkstemp folds ``link/..`` out
of its directory before it creates the file (#260).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from boxman.utils import io

pytestmark = pytest.mark.unit


class TestMkstempIn:

    def test_a_name_already_taken_is_never_opened(
            self, tmp_path: Path, monkeypatch):
        taken = tmp_path / ".x.taken.tmp"
        taken.write_text("someone else's")
        names = iter(["taken", "fresh"])
        monkeypatch.setattr(io.secrets, "token_hex", lambda _n: next(names))

        fd, path = io.mkstemp_in(str(tmp_path), prefix=".x.", suffix=".tmp")
        os.close(fd)

        assert path == str(tmp_path / ".x.fresh.tmp")
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert taken.read_text() == "someone else's"

    def test_a_relative_directory_is_made_absolute_with_nothing_folded(
            self, tmp_path: Path, monkeypatch):
        actual = tmp_path / "actual"
        (actual / "deep").mkdir(parents=True)
        (actual / "sub").mkdir()
        (tmp_path / "link").symlink_to(actual / "deep",
                                       target_is_directory=True)
        monkeypatch.chdir(tmp_path)

        fd, path = io.mkstemp_in(os.path.join("link", "..", "sub"))
        os.close(fd)

        assert os.path.dirname(path) == os.path.join(
            os.getcwd(), "link", "..", "sub")
        assert [p.name for p in (actual / "sub").iterdir()] == [
            os.path.basename(path)]
