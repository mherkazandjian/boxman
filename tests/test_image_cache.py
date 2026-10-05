"""
Unit tests for boxman.image_cache.ImageCache.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
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


IMAGE_URL = "https://example.com/distro.qcow2"
IMAGE = b"the whole image " * 64


def _download_whole(url, dst):
    Path(dst).write_bytes(IMAGE)
    return True


class TestADownloadCutShortIsNeverTheImage:
    """
    #227: ``ensure`` downloaded straight to the cache path and removed what
    was there only when the download reported failure. A Ctrl-C or a kill
    mid-download never got that far, and ``is_cached`` takes any non-empty
    file for the image, so every later run built from the part it had got.
    """

    def test_a_download_stopped_by_ctrl_c(self, tmp_path: Path):
        cache = ImageCache(cache_dir=str(tmp_path / "cache"))

        def interrupted(url, dst):
            Path(dst).write_bytes(IMAGE[:100])
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            cache.ensure(IMAGE_URL, interrupted)

        assert cache.is_cached(IMAGE_URL) is False
        # nothing of it left in the cache either
        assert list((tmp_path / "cache").iterdir()) == []
        # so the next run downloads the image
        path = cache.ensure(IMAGE_URL, _download_whole)
        assert Path(path).read_bytes() == IMAGE

    def test_a_download_killed_outright(self, tmp_path: Path):
        """SIGKILL runs no cleanup: only where the download writes can help."""
        cache_dir = tmp_path / "cache"
        killed = subprocess.run(
            [sys.executable, "-c", (
                "import os, signal, sys\n"
                "from boxman.image_cache import ImageCache\n"
                "def download(url, dst):\n"
                "    open(dst, 'wb').write(b'the first part')\n"
                "    os.kill(os.getpid(), signal.SIGKILL)\n"
                f"ImageCache(cache_dir={str(cache_dir)!r}).ensure({IMAGE_URL!r}, download)\n"
            )],
            env={**os.environ,
                 "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True, check=False, timeout=60)
        assert killed.returncode == -signal.SIGKILL, killed.stderr

        cache = ImageCache(cache_dir=str(cache_dir))
        assert cache.is_cached(IMAGE_URL) is False
        path = cache.ensure(IMAGE_URL, _download_whole)
        assert Path(path).read_bytes() == IMAGE

    def test_the_download_never_writes_the_cache_path(self, tmp_path: Path):
        cache_dir = tmp_path / "cache"
        cache = ImageCache(cache_dir=str(cache_dir))
        cached = cache.cache_path_for(IMAGE_URL)
        seen = []

        def download(url, dst):
            # while it runs, there is nothing at the cache path to find
            seen.append((dst, os.path.lexists(cached)))
            return _download_whole(url, dst)

        assert cache.ensure(IMAGE_URL, download) == cached

        [(dst, cache_path_taken)] = seen
        assert (dst != cached, cache_path_taken) == (True, False)
        # beside it, so that putting it in place is a rename
        assert os.path.dirname(dst) == str(cache_dir)
        assert os.listdir(cache_dir) == ["distro.qcow2"]
        assert Path(cached).read_bytes() == IMAGE

    def test_two_downloads_at_once_do_not_share_a_file(self, tmp_path: Path):
        """A second call while the first downloads: a thread, or a run in
        another PID namespace sharing the cache, can have the same PID, so
        a name made of the PID alone was both calls' (#227 review). The
        second one's failure then removed the first one's download, or the
        first published the second one's part."""
        cache = ImageCache(cache_dir=str(tmp_path / "cache"))

        def fails_after_a_part(url, dst):
            Path(dst).write_bytes(IMAGE[:100])
            return False

        def downloads_whole(url, dst):
            _download_whole(url, dst)
            # meanwhile, another call downloads the same image, and fails
            assert cache.ensure(IMAGE_URL, fails_after_a_part) is None
            return True

        path = cache.ensure(IMAGE_URL, downloads_whole)

        assert Path(path).read_bytes() == IMAGE
        assert os.listdir(tmp_path / "cache") == ["distro.qcow2"]

    def test_a_failed_download_leaves_nothing_in_the_cache(self, tmp_path: Path):
        cache = ImageCache(cache_dir=str(tmp_path / "cache"))

        def failed(url, dst):
            Path(dst).write_bytes(IMAGE[:100])
            return False

        assert cache.ensure(IMAGE_URL, failed) is None
        assert list((tmp_path / "cache").iterdir()) == []


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
