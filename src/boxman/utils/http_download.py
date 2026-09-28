"""HTTP/HTTPS download helper with wget -> curl -> urllib fallbacks."""

import logging
import os
import re
import shlex
import urllib.request

from boxman import log
from boxman.loggers.logger import is_verbose
from boxman.utils.http_opener import build_opener
from boxman.utils.shell import run as _shell_run


def curl_final_status_ok(status: str) -> bool:
    """Whether a transfer curl finished may count, by its final status.

    *status* is what curl's ``-w '%{http_code}'`` printed: the code of the
    last response the transfer got. That code decides, not the URL's
    scheme: curl fetches a URL with no scheme as http, and an ftp:// one
    through an http proxy can be redirected to an http origin (#224).
    ``--fail`` refuses a 4xx or 5xx but lets a 3xx through, and ``-L``
    follows only a 3xx that says where to go, so a 302 without a Location
    ends the transfer on its own page, with exit 0. A 2xx counts, FTP's 226
    among them, and so does 000: no response code at all, as for a file://
    copy.
    """
    return re.fullmatch(r"000|2[0-9][0-9]", status.strip()) is not None


def download_url(url: str, dst_path: str) -> bool:
    """Download *url* to *dst_path*; return True on success.

    Tries wget first (best progress + redirect handling), then curl, and
    finally a urllib fallback. A partial *dst_path* left by a failed
    attempt is removed before the next attempt.

    Both operands are shell-quoted. These commands run through a shell, and
    ``$(…)`` inside double quotes is still evaluated by it — so a URL or
    destination carrying a command substitution would execute it. That is
    survivable while every caller passes a value from the project's own
    config; it stops being survivable the moment a URL comes from a
    *remote manifest* (#164 F1).
    """
    log.status(f"downloading {url} -> {dst_path}")
    q_url = shlex.quote(url)
    q_dst = shlex.quote(dst_path)

    # wget: handles redirects, proxies, SSL well; prints chunky progress.
    result = _shell_run(
        f'wget --progress=dot:mega -O {q_dst} {q_url}',
        hide=not is_verbose(logging.DEBUG), warn=True,
    )
    if result.ok and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
        log.info("download complete (wget)")
        return True
    if os.path.exists(dst_path):
        os.remove(dst_path)

    # curl fallback. --fail so an HTTP 4xx/5xx error page is not written
    # and accepted as a valid download (wget already fails on HTTP errors),
    # and the final status must count too, as --fail lets a 3xx through.
    result = _shell_run(
        f"curl -fL --progress-bar -w '%{{http_code}}' -o {q_dst} {q_url}",
        hide=not is_verbose(logging.DEBUG), warn=True,
    )
    if (result.ok and curl_final_status_ok(result.stdout)
            and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0):
        log.info("download complete (curl)")
        return True
    if os.path.exists(dst_path):
        os.remove(dst_path)

    # urllib last resort (always available, no shell deps). Not urlopen():
    # its stock opener hands the proxy password to wherever a proxied
    # mirror redirects (#216).
    try:
        log.info("falling back to urllib download (timeout=120s)...")
        req = urllib.request.Request(url, headers={"User-Agent": "boxman/1.0"})
        with build_opener().open(req, timeout=120) as response:
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
        if not downloaded:
            # a 2xx with nothing in it is no download (#224)
            raise ValueError("the response was empty")
        log.info("download complete (urllib)")
        return True
    except Exception as exc:
        log.error(f"failed to download {url}: {exc}")
        if os.path.exists(dst_path):
            os.remove(dst_path)
        return False
