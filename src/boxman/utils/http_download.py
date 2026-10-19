"""HTTP/HTTPS download helper with wget -> curl -> urllib fallbacks."""

import contextlib
import logging
import os
import re
import shlex
import signal
import urllib.request
from urllib.parse import urlsplit

from boxman import log
from boxman.loggers.logger import is_verbose
from boxman.utils.http_opener import build_opener
from boxman.utils.shell import run_stoppable as _shell_run


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

    Except 206: curl is never asked for a range here, so a 206 is a part
    of the image, whatever its Content-Range says (#227).
    """
    status = status.strip()
    return status != "206" and re.fullmatch(r"000|2[0-9][0-9]", status) is not None


#: a status line among what ``wget -S`` printed, and a header line after it:
#: wget indents the status line and headers of each response it got
_WGET_STATUS_LINE = re.compile(r"[ \t]*HTTP/[0-9.]+[ \t]+([0-9]{3})\b")
_WGET_HEADER_LINE = re.compile(r"[ \t]+([!#$%&'*+.^_`|~0-9A-Za-z-]+):[ \t]*(.*)")
#: the whole length a Content-Range gives, ``bytes <first>-<last>/<length>``
_CONTENT_RANGE = re.compile(r"bytes[ \t]+[0-9]+-[0-9]+/([0-9]+)", re.IGNORECASE)


def _wget_final_response(output: str) -> tuple[str, dict[str, str]] | None:
    """The status and headers of the last response ``wget -S`` printed, if any.

    wget's own messages are not indented, so an indented header line after
    the last status line is that response's.
    """
    final = None
    for line in output.splitlines():
        status = _WGET_STATUS_LINE.match(line)
        if status:
            final = (status.group(1), {})
        elif final and (header := _WGET_HEADER_LINE.fullmatch(line)):
            final[1][header.group(1).lower()] = header.group(2).strip()
    return final


def wget_download_ok(output: str, url: str, size: int) -> bool:
    """Whether a download wget finished may count, by what ``wget -S`` printed.

    *output* holds the status line and headers of each response wget got,
    a redirect's among them, so the last status line is the final one's.
    wget saves the page of a 300 that names nowhere to go and exits 0
    (#227); a 302 without a Location, and an HTTP error, it refuses on its
    own. So the final status must be a 2xx. A 206 is part of the image:
    wget asks for one to resume a download that was cut off, and it counts
    only when *size*, what wget has written, is the whole length its
    Content-Range gives. An http(s) download whose status cannot be seen
    does not count. An ftp:// one, which prints none, is left to wget's
    exit status, as before.
    """
    final = _wget_final_response(output)
    if final is None:
        return urlsplit(url).scheme.lower() in ("ftp", "ftps")
    status, headers = final
    if status == "206":
        whole = _CONTENT_RANGE.fullmatch(headers.get("content-range", ""))
        return whole is not None and int(whole.group(1)) == size
    return status.startswith("2")


def _stopped_by_ctrl_c(result) -> bool:
    """Whether a command ended because the user pressed Ctrl-C.

    invoke swallows the KeyboardInterrupt and returns the command's result:
    a death by SIGINT, or a shell's 130 for one. Read as a failed download,
    it sent ``download_url`` on to the next downloader, so a Ctrl-C started
    the download over instead of stopping it (#227). ``run_stoppable``
    raises the KeyboardInterrupt boxman itself got; this is for a SIGINT
    that reached the downloader alone.
    """
    return getattr(result, "exited", None) in (-signal.SIGINT, 128 + signal.SIGINT)


def download_url(url: str, dst_path: str) -> bool:
    """Download *url* to *dst_path*; return True on success.

    Tries wget first (best progress + redirect handling), then curl, and
    finally a urllib fallback. A partial *dst_path* left by a failed
    attempt is removed before the next attempt. A Ctrl-C stops the
    download, removes what it left, and is raised as KeyboardInterrupt. A
    kill can still leave part of a download at *dst_path*: a caller that
    keeps what it downloads, as the image cache does, downloads beside
    where it keeps it and renames the file into place (#227).

    Both operands are shell-quoted. These commands run through a shell, and
    ``$(…)`` inside double quotes is still evaluated by it — so a URL or
    destination carrying a command substitution would execute it. That is
    survivable while every caller passes a value from the project's own
    config; it stops being survivable the moment a URL comes from a
    *remote manifest* (#164 F1).
    """
    log.status(f"downloading {url} -> {dst_path}")
    try:
        return _download(url, dst_path)
    except KeyboardInterrupt:
        # stopped part-way: what is there is not the download
        with contextlib.suppress(OSError):
            os.remove(dst_path)
        raise


def _download(url: str, dst_path: str) -> bool:
    q_url = shlex.quote(url)
    q_dst = shlex.quote(dst_path)

    # wget: handles redirects, proxies, SSL well; prints chunky progress.
    # -S prints the status line and headers of every response it got, as
    # exit 0 alone lets the page of a 300 through (#227). -o puts them, and
    # the progress, on stderr, where a logfile set in a wgetrc would not.
    result = _shell_run(
        f'wget -S -o /dev/stderr --progress=dot:mega -O {q_dst} {q_url}',
        hide=not is_verbose(logging.DEBUG), warn=True,
    )
    if (result.ok and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0
            and wget_download_ok(result.stderr, url, os.path.getsize(dst_path))):
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
            if getattr(response, "status", None) == 206:
                # no range was asked for: this is part of the image (#227)
                raise ValueError("the server sent only a part of the file (206)")
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
