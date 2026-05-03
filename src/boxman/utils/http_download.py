"""HTTP/HTTPS download helper with wget -> curl -> urllib fallbacks."""

import os
import urllib.request

from boxman import log
from boxman.utils.shell import run as _shell_run


def download_url(url: str, dst_path: str) -> bool:
    """Download *url* to *dst_path*; return True on success.

    Tries wget first (best progress + redirect handling), then curl, and
    finally a urllib fallback. A partial *dst_path* left by a failed
    attempt is removed before the next attempt.
    """
    log.info(f"downloading {url} -> {dst_path}")

    # wget: handles redirects, proxies, SSL well; prints chunky progress.
    result = _shell_run(
        f'wget --progress=dot:mega -O "{dst_path}" "{url}"',
        hide=False, warn=True,
    )
    if result.ok and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
        log.info("download complete (wget)")
        return True
    if os.path.exists(dst_path):
        os.remove(dst_path)

    # curl fallback.
    result = _shell_run(
        f'curl -L --progress-bar -o "{dst_path}" "{url}"',
        hide=False, warn=True,
    )
    if result.ok and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
        log.info("download complete (curl)")
        return True
    if os.path.exists(dst_path):
        os.remove(dst_path)

    # urllib last resort (always available, no shell deps).
    try:
        log.info("falling back to urllib download (timeout=120s)...")
        req = urllib.request.Request(url, headers={"User-Agent": "boxman/1.0"})
        with urllib.request.urlopen(req, timeout=120) as response:
            total = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            with open(dst_path, "wb") as out_file:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 // total
                        log.info(
                            f"  downloaded {downloaded // (1024*1024)} MB "
                            f"/ {total // (1024*1024)} MB ({pct}%)")
        log.info("download complete (urllib)")
        return True
    except Exception as exc:
        log.error(f"failed to download {url}: {exc}")
        if os.path.exists(dst_path):
            os.remove(dst_path)
        return False
