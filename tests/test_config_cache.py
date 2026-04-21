"""
Unit tests for boxman.config_cache.BoxmanCache.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).

The BoxmanCache __init__ expands DEFAULT_CACHE_DIR and creates the
directory at construction time, so tests patch DEFAULT_CACHE_DIR to
point at ``tmp_path`` before instantiating.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from boxman.config_cache import BoxmanCache


pytestmark = pytest.mark.unit


@pytest.fixture
def cache(tmp_path: Path) -> BoxmanCache:
    with patch("boxman.config_cache.DEFAULT_CACHE_DIR", str(tmp_path / "cache")):
        return BoxmanCache()


class TestInit:

    def test_creates_cache_dir_if_missing(self, tmp_path: Path):
        target = tmp_path / "new-cache-dir"
        assert not target.exists()
        with patch("boxman.config_cache.DEFAULT_CACHE_DIR", str(target)):
            BoxmanCache()
        assert target.is_dir()

    def test_does_not_fail_if_dir_already_exists(self, tmp_path: Path):
        (tmp_path / "cache").mkdir()
        with patch("boxman.config_cache.DEFAULT_CACHE_DIR", str(tmp_path / "cache")):
            BoxmanCache()  # must not raise

    def test_projects_cache_file_path(self, cache: BoxmanCache):
        assert cache.projects_cache_file.endswith("/projects.json")


class TestReadProjectsCache:

    def test_returns_empty_when_file_missing(self, cache: BoxmanCache):
        assert cache.read_projects_cache() == {}

    def test_reads_existing_json(self, cache: BoxmanCache):
        payload = {"p1": {"conf": "/tmp/p1.yml", "runtime": "local"}}
        Path(cache.projects_cache_file).write_text(json.dumps(payload))
        assert cache.read_projects_cache() == payload
        assert cache.projects == payload


class TestRegisterProject:

    def test_first_registration_creates_file(self, cache: BoxmanCache, tmp_path: Path):
        conf = tmp_path / "myproj.yml"
        conf.write_text("version: '1.0'\n")
        cache.register_project("myproj", str(conf), runtime="local")

        on_disk = json.loads(Path(cache.projects_cache_file).read_text())
        assert "myproj" in on_disk
        assert on_disk["myproj"]["runtime"] == "local"
        assert on_disk["myproj"]["conf"].endswith("myproj.yml")

    def test_registers_multiple_projects(self, cache: BoxmanCache, tmp_path: Path):
        (tmp_path / "a.yml").write_text("x")
        (tmp_path / "b.yml").write_text("x")
        cache.register_project("a", str(tmp_path / "a.yml"))
        cache.register_project("b", str(tmp_path / "b.yml"), runtime="docker-compose")

        on_disk = json.loads(Path(cache.projects_cache_file).read_text())
        assert set(on_disk.keys()) == {"a", "b"}
        assert on_disk["b"]["runtime"] == "docker-compose"

    def test_duplicate_registration_returns_false_and_preserves_state(
        self, cache: BoxmanCache, tmp_path: Path
    ):
        conf = tmp_path / "dup.yml"
        conf.write_text("x")
        cache.register_project("dup", str(conf))

        before = Path(cache.projects_cache_file).read_text()
        result = cache.register_project("dup", str(conf))
        after = Path(cache.projects_cache_file).read_text()

        assert result is False
        assert before == after

    def test_stores_absolute_path(self, cache: BoxmanCache, tmp_path: Path, monkeypatch):
        # Relative path should be resolved to absolute
        conf = tmp_path / "rel.yml"
        conf.write_text("x")
        monkeypatch.chdir(tmp_path)
        cache.register_project("rel", "rel.yml")

        on_disk = json.loads(Path(cache.projects_cache_file).read_text())
        assert Path(on_disk["rel"]["conf"]).is_absolute()


class TestUnregisterProject:

    def test_removes_existing_project(self, cache: BoxmanCache, tmp_path: Path):
        conf = tmp_path / "p.yml"
        conf.write_text("x")
        cache.register_project("p", str(conf))

        assert cache.unregister_project("p") is True
        on_disk = json.loads(Path(cache.projects_cache_file).read_text())
        assert "p" not in on_disk

    def test_returns_false_when_project_not_in_cache(
        self, cache: BoxmanCache, tmp_path: Path
    ):
        # Ensure cache file exists but is empty
        Path(cache.projects_cache_file).write_text("{}")
        assert cache.unregister_project("nothing") is False

    def test_leaves_other_projects_intact(self, cache: BoxmanCache, tmp_path: Path):
        (tmp_path / "a.yml").write_text("x")
        (tmp_path / "b.yml").write_text("x")
        cache.register_project("a", str(tmp_path / "a.yml"))
        cache.register_project("b", str(tmp_path / "b.yml"))

        cache.unregister_project("a")
        on_disk = json.loads(Path(cache.projects_cache_file).read_text())
        assert list(on_disk.keys()) == ["b"]


class TestListProjects:

    def test_empty_when_nothing_registered(self, cache: BoxmanCache):
        assert cache.list_projects() == {}

    def test_returns_all_registered_projects(self, cache: BoxmanCache, tmp_path: Path):
        (tmp_path / "a.yml").write_text("x")
        (tmp_path / "b.yml").write_text("x")
        cache.register_project("a", str(tmp_path / "a.yml"))
        cache.register_project("b", str(tmp_path / "b.yml"), runtime="docker-compose")

        projects = cache.list_projects()
        assert set(projects.keys()) == {"a", "b"}
        assert projects["b"]["runtime"] == "docker-compose"


class TestWriteProjectsCache:

    def test_skips_when_projects_is_none(self, cache: BoxmanCache, captured_logs):
        # Fresh BoxmanCache has projects = None; write should warn + noop
        assert cache.projects is None
        cache.write_projects_cache()
        assert any("no projects to write" in rec.message for rec in captured_logs.records)

    def test_writes_json_when_projects_set(self, cache: BoxmanCache):
        cache.projects = {"x": {"conf": "/tmp/x.yml", "runtime": "local"}}
        cache.write_projects_cache()
        on_disk = json.loads(Path(cache.projects_cache_file).read_text())
        assert on_disk == cache.projects
