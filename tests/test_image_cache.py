"""
Unit tests for boxman.image_cache.ImageCache.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from boxman.image_cache import ImageCache


pytestmark = pytest.mark.unit


class TestFromConfig:

    def test_defaults(self):
        cache = ImageCache.from_config({})
        assert cache.enabled is True
        assert cache.cache_dir.endswith("/boxman/images")

    def test_disabled_when_config_says_so(self, tmp_path: Path):
        cache = ImageCache.from_config({"enabled": False, "cache_dir": str(tmp_path)})
        assert cache.enabled is False
        assert cache.cache_dir == str(tmp_path)

    def test_custom_cache_dir(self, tmp_path: Path):
        cache = ImageCache.from_config({"cache_dir": str(tmp_path / "imgs")})
        assert cache.cache_dir == str(tmp_path / "imgs")


class TestCachePathFor:

    def test_extracts_filename_from_url(self, tmp_path: Path):
        cache = ImageCache(cache_dir=str(tmp_path))
        p = cache.cache_path_for("https://example.com/images/ubuntu-24.04.img")
        assert p == str(tmp_path / "ubuntu-24.04.img")

    def test_falls_back_to_image_when_no_filename(self, tmp_path: Path):
        cache = ImageCache(cache_dir=str(tmp_path))
        p = cache.cache_path_for("https://example.com/")
        assert p == str(tmp_path / "image")


class TestIsCached:

    def test_false_when_disabled(self, tmp_path: Path):
        cache = ImageCache(enabled=False, cache_dir=str(tmp_path))
        (tmp_path / "foo.img").write_bytes(b"data")
        assert cache.is_cached("https://example.com/foo.img") is False

    def test_false_when_file_missing(self, tmp_path: Path):
        cache = ImageCache(cache_dir=str(tmp_path))
        assert cache.is_cached("https://example.com/missing.img") is False

    def test_false_when_file_is_empty(self, tmp_path: Path):
        cache = ImageCache(cache_dir=str(tmp_path))
        (tmp_path / "empty.img").write_bytes(b"")
        assert cache.is_cached("https://example.com/empty.img") is False

    def test_true_when_file_present_and_nonempty(self, tmp_path: Path):
        cache = ImageCache(cache_dir=str(tmp_path))
        (tmp_path / "ok.img").write_bytes(b"content")
        assert cache.is_cached("https://example.com/ok.img") is True


class TestEnsure:

    def test_returns_none_when_disabled(self, tmp_path: Path):
        cache = ImageCache(enabled=False, cache_dir=str(tmp_path))
        result = cache.ensure("https://example.com/x.img", lambda u, d: True)
        assert result is None

    def test_cache_hit_does_not_call_download(self, tmp_path: Path):
        cache = ImageCache(cache_dir=str(tmp_path))
        # pre-populate
        (tmp_path).mkdir(exist_ok=True)
        (tmp_path / "hit.img").write_bytes(b"cached")

        calls = []

        def fake_download(u, d):
            calls.append((u, d))
            return True

        result = cache.ensure("https://example.com/hit.img", fake_download)
        assert result == str(tmp_path / "hit.img")
        assert calls == []

    def test_cache_miss_invokes_download(self, tmp_path: Path):
        cache_dir = tmp_path / "cache"
        cache = ImageCache(cache_dir=str(cache_dir))

        def fake_download(url, dst):
            Path(dst).write_bytes(b"downloaded")
            return True

        result = cache.ensure("https://example.com/new.img", fake_download)
        assert result == str(cache_dir / "new.img")
        assert (cache_dir / "new.img").read_bytes() == b"downloaded"

    def test_failed_download_cleans_up_partial_file(self, tmp_path: Path):
        cache_dir = tmp_path / "cache"
        cache = ImageCache(cache_dir=str(cache_dir))

        def flaky_download(url, dst):
            Path(dst).write_bytes(b"partial")
            return False  # signal failure after writing partial

        result = cache.ensure("https://example.com/bad.img", flaky_download)
        assert result is None
        assert not (cache_dir / "bad.img").exists()

    def test_failed_download_without_partial_returns_none(self, tmp_path: Path):
        cache_dir = tmp_path / "cache"
        cache = ImageCache(cache_dir=str(cache_dir))
        result = cache.ensure("https://example.com/nf.img", lambda u, d: False)
        assert result is None
        assert not (cache_dir / "nf.img").exists()


class TestVerifyChecksum:

    def _mkfile(self, tmp_path: Path, data: bytes = b"hello\n") -> Path:
        f = tmp_path / "sample.bin"
        f.write_bytes(data)
        return f

    def test_matches_known_sha256(self, tmp_path: Path):
        f = self._mkfile(tmp_path, b"hello\n")
        expected = hashlib.sha256(b"hello\n").hexdigest()
        assert ImageCache.verify_checksum(str(f), f"sha256:{expected}") is True

    def test_matches_known_md5(self, tmp_path: Path):
        f = self._mkfile(tmp_path, b"hello\n")
        expected = hashlib.md5(b"hello\n").hexdigest()
        assert ImageCache.verify_checksum(str(f), f"md5:{expected}") is True

    def test_mismatch_returns_false(self, tmp_path: Path):
        f = self._mkfile(tmp_path, b"hello\n")
        assert ImageCache.verify_checksum(str(f), "sha256:" + "0" * 64) is False

    def test_accepts_uppercase_hexdigest(self, tmp_path: Path):
        f = self._mkfile(tmp_path, b"hello\n")
        expected = hashlib.sha256(b"hello\n").hexdigest().upper()
        assert ImageCache.verify_checksum(str(f), f"sha256:{expected}") is True

    def test_missing_colon_raises(self, tmp_path: Path):
        f = self._mkfile(tmp_path)
        with pytest.raises(ValueError, match="invalid checksum spec"):
            ImageCache.verify_checksum(str(f), "sha256abc")

    def test_unknown_algorithm_raises(self, tmp_path: Path):
        f = self._mkfile(tmp_path)
        with pytest.raises(ValueError, match="unknown checksum algorithm"):
            ImageCache.verify_checksum(str(f), "bogus-algo:abc")
