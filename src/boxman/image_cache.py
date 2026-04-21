"""
Local cache for downloaded cloud base images.

Images are keyed by the filename component of their URL so that the same
image referenced from multiple projects is only downloaded once.

Usage
-----
    cache = ImageCache.from_config(app_config.get('cache', {}))

    # Get the local path, downloading if necessary.
    local_path = cache.ensure(url, download_fn)

    # Verify a checksum spec like 'sha256:<hex>'.
    ok = ImageCache.verify_checksum(local_path, 'sha256:abc123...')
"""

import hashlib
import os
from collections.abc import Callable
from urllib.parse import urlparse

from boxman import log


class ImageCache:
    """Manages a local directory of cached cloud base images."""

    DEFAULT_CACHE_DIR = "~/.cache/boxman/images"

    def __init__(self, enabled: bool = True, cache_dir: str = DEFAULT_CACHE_DIR):
        self.enabled = enabled
        self.cache_dir = os.path.expanduser(cache_dir)
        self.logger = log

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, cache_conf: dict) -> "ImageCache":
        """Build an ImageCache from a ``cache:`` config dict."""
        return cls(
            enabled=cache_conf.get("enabled", True),
            cache_dir=cache_conf.get("cache_dir", cls.DEFAULT_CACHE_DIR),
        )

    # ── public interface ────────────────────────────────────────────────────

    def cache_path_for(self, url: str) -> str:
        """Return the local path where *url* would be cached."""
        filename = os.path.basename(urlparse(url).path) or "image"
        return os.path.join(self.cache_dir, filename)

    def is_cached(self, url: str) -> bool:
        """Return True if a non-empty cached file exists for *url*."""
        if not self.enabled:
            return False
        p = self.cache_path_for(url)
        return os.path.isfile(p) and os.path.getsize(p) > 0

    def ensure(
        self,
        url: str,
        download_fn: Callable[[str, str], bool],
    ) -> str | None:
        """
        Return the local path of the cached image for *url*.

        If the image is not yet cached, call ``download_fn(url, dst_path)``
        to download it.  Returns ``None`` if the download fails or cache is
        disabled (callers handle the no-cache path themselves).
        """
        if not self.enabled:
            return None

        os.makedirs(self.cache_dir, exist_ok=True)
        dst = self.cache_path_for(url)

        if self.is_cached(url):
            self.logger.info(f"cache hit: {dst}")
            return dst

        self.logger.info(f"cache miss — downloading to cache: {dst}")
        if download_fn(url, dst):
            return dst

        # Download failed; make sure no partial file lingers in the cache.
        if os.path.exists(dst):
            os.remove(dst)
        return None

    # ── checksum ────────────────────────────────────────────────────────────

    @staticmethod
    def verify_checksum(file_path: str, checksum_spec: str) -> bool:
        """
        Verify *file_path* against *checksum_spec* (``'algorithm:hexdigest'``).

        Returns True on match, False on mismatch.
        Raises ValueError for malformed specs or unknown algorithms.
        """
        if ":" not in checksum_spec:
            raise ValueError(
                f"invalid checksum spec '{checksum_spec}' — "
                "expected format: 'algorithm:hexdigest' (e.g. 'sha256:abc123...')"
            )
        algorithm, expected = checksum_spec.split(":", 1)
        try:
            h = hashlib.new(algorithm)
        except ValueError:
            raise ValueError(f"unknown checksum algorithm: '{algorithm}'")

        log.info(f"computing {algorithm} checksum of {file_path} ...")
        with open(file_path, "rb") as fobj:
            for chunk in iter(lambda: fobj.read(8 * 1024 * 1024), b""):
                h.update(chunk)

        actual = h.hexdigest()
        if actual == expected.lower():
            log.info(f"checksum ok  ({algorithm}: {actual[:16]}...)")
            return True

        log.error(
            f"checksum mismatch for {file_path}:\n"
            f"  expected : {expected.lower()}\n"
            f"  actual   : {actual}"
        )
        return False
