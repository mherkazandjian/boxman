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


class TestCorruptCache:
    """Regression: a corrupt projects.json (e.g. truncated by a crashed
    writer) must not raise JSONDecodeError through every boxman command —
    it is moved aside and treated as empty."""

    def test_read_treats_corrupt_as_empty_and_backs_up(self, cache: BoxmanCache):
        Path(cache.projects_cache_file).write_text("{not json")
        assert cache.read_projects_cache() == {}
        backup = Path(cache.projects_cache_file + ".corrupt")
        assert backup.read_text() == "{not json"
        assert not Path(cache.projects_cache_file).exists()

    def test_register_project_survives_corrupt_cache(
            self, cache: BoxmanCache, tmp_path: Path):
        Path(cache.projects_cache_file).write_text("garbage")
        conf = tmp_path / "p.yml"
        conf.write_text("x")
        assert cache.register_project("p", str(conf)) is True
        on_disk = json.loads(Path(cache.projects_cache_file).read_text())
        assert "p" in on_disk

    def test_list_projects_survives_corrupt_cache(self, cache: BoxmanCache):
        Path(cache.projects_cache_file).write_text("]")
        assert cache.list_projects() == {}


class TestAtomicWrite:
    """Regression: cache writes must go through tmp-file + os.replace so a
    crashed or concurrent writer can never truncate projects.json."""

    def test_failed_dump_leaves_existing_file_intact(
            self, cache: BoxmanCache, monkeypatch):
        original = {"p": {"conf": "/tmp/p.yml", "runtime": "local"}}
        Path(cache.projects_cache_file).write_text(json.dumps(original))
        cache.projects = {"q": {}}

        def _crash(*_a, **_k):
            raise RuntimeError("crash mid-write")

        monkeypatch.setattr(json, "dump", _crash)
        with pytest.raises(RuntimeError, match="crash mid-write"):
            cache.write_projects_cache()
        assert json.loads(Path(cache.projects_cache_file).read_text()) == original

    def test_no_tmp_files_left_behind(self, cache: BoxmanCache):
        cache.projects = {"x": {}}
        cache.write_projects_cache()
        leftovers = [p.name for p in Path(cache.cache_dir).iterdir()]
        assert leftovers == ["projects.json"]

    def test_concurrent_writers_keep_file_valid(
            self, cache: BoxmanCache, tmp_path: Path):
        import threading
        conf = tmp_path / "c.yml"
        conf.write_text("x")
        errors = []

        def worker(i):
            try:
                for n in range(25):
                    name = f"p{i}-{n}"
                    cache.register_project(name, str(conf))
                    cache.unregister_project(name)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # whatever the interleaving, the file must still be valid JSON
        json.loads(Path(cache.projects_cache_file).read_text())


class TestRegisterProjectReturn:

    def test_returns_true_on_success(self, cache: BoxmanCache, tmp_path: Path):
        conf = tmp_path / "p.yml"
        conf.write_text("x")
        assert cache.register_project("p", str(conf)) is True


class TestUnregisterNetwork:

    def _seed(self, cache: BoxmanCache, tmp_path: Path):
        conf = tmp_path / "p.yml"
        conf.write_text("x")
        cache.register_project("proj", str(conf))
        cache.projects["proj"]["networks"] = {
            "net_a": {"ip_address": "10.0.0.1"},
            "net_b": {"ip_address": "10.0.1.1"},
        }
        cache.write_projects_cache()

    def test_removes_entry_and_keeps_others(
            self, cache: BoxmanCache, tmp_path: Path):
        self._seed(cache, tmp_path)
        assert cache.unregister_network("proj", "net_a") is True
        on_disk = json.loads(Path(cache.projects_cache_file).read_text())
        assert on_disk["proj"]["networks"] == {
            "net_b": {"ip_address": "10.0.1.1"}}
        # the rest of the project record is untouched
        assert on_disk["proj"]["runtime"] == "local"

    def test_false_when_network_not_cached(
            self, cache: BoxmanCache, tmp_path: Path):
        self._seed(cache, tmp_path)
        before = Path(cache.projects_cache_file).read_text()
        assert cache.unregister_network("proj", "net_x") is False
        assert Path(cache.projects_cache_file).read_text() == before

    def test_false_when_project_not_cached(self, cache: BoxmanCache):
        assert cache.unregister_network("nope", "net_a") is False
