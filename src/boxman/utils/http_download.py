"""HTTP/HTTPS download helper with wget -> curl -> urllib fallbacks."""

import logging
import os
import re
import shlex
import signal
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
    copy. An HTTP transfer cannot end with exit 0 and 000: since curl 7.66
    a response with no status line (HTTP/0.9) fails unless ``--http0.9``
    allows it, which boxman never passes, and a status line of 000 fails
    anyway. An older curl takes HTTP/0.9 as 000.
    """
    return re.fullmatch(r"000|2[0-9][0-9]", status.strip()) is not None


#: a status line among what ``wget -S`` printed: wget indents the headers of
#: each response it got, the status line first
_WGET_STATUS_LINE = re.compile(r"^[ \t]*HTTP/[0-9.]+[ \t]+([0-9]{3})\b", re.MULTILINE)


def wget_final_status_ok(output: str) -> bool:
    """Whether a download wget finished may count, by the last status it printed.

    *output* is what ``wget -S`` printed: the status line and headers of
    each response it got, a redirect's among them, so the last status line
    is the final one. wget saves the page of a 300 that names nowhere to go
    as the download and exits 0 (#227); a 302 without a Location, and an
    HTTP error, it refuses on its own. A 2xx counts. With no status line at
    all, as for an ftp:// download, wget's exit status decides, as before.
    """
    statuses = _WGET_STATUS_LINE.findall(output)
    return not statuses or statuses[-1].startswith("2")


def _stopped_by_ctrl_c(result) -> bool:
    """Whether a command ended because the user pressed Ctrl-C.

    invoke swallows the KeyboardInterrupt and returns the command's result:
    a death by SIGINT, or a shell's 130 for one. Read as a failed download,
    it sent ``download_url`` on to the next downloader, so a Ctrl-C started
    the download over instead of stopping it (#227).
    """
    return getattr(result, "exited", None) in (-signal.SIGINT, 128 + signal.SIGINT)


def download_url(url: str, dst_path: str) -> bool:
    """Download *url* to *dst_path*; return True on success.

    Tries wget first (best progress + redirect handling), then curl, and
    finally a urllib fallback. A partial *dst_path* left by a failed
    attempt is removed before the next attempt. A Ctrl-C or a kill can
    still leave part of a download at *dst_path*: a caller that keeps what
    it downloads, as the image cache does, downloads beside where it keeps
    it and renames the file into place (#227).

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
    # -S prints the status line of every response it got, as exit 0 alone
    # lets the page of a 300 through (#227).
    result = _shell_run(
        f'wget -S --progress=dot:mega -O {q_dst} {q_url}',
        hide=not is_verbose(logging.DEBUG), warn=True,
    )
    if (result.ok and wget_final_status_ok(result.stderr)
            and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0):
        log.info("download complete (wget)")
        return True
    if os.path.exists(dst_path):
        os.remove(dst_path)
    if _stopped_by_ctrl_c(result):
        raise KeyboardInterrupt

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
    if _stopped_by_ctrl_c(result):
        raise KeyboardInterrupt

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
        if total and downloaded != total:
            # read() returns nothing when the server hangs up early, as it
            # does at the end of the body: only the length tells (#227)
            raise ValueError(
                f"the response ended after {downloaded} of its {total} bytes")
        log.info("download complete (urllib)")
        return True
    except Exception as exc:
        log.error(f"failed to download {url}: {exc}")
        if os.path.exists(dst_path):
            os.remove(dst_path)
        return False
