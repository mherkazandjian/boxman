"""
#224 -- nothing but the image is taken for the image.

``CloudInitTemplate._download_image`` downloads the base image of every
cloud-image template. When wget failed, it ran ``curl -L`` without
``--fail``: curl saved the error page the server answered with and exited
0, so the page was accepted as the image and, with the image cache on,
stored in the cache and reused from there, unless the template set an
``image_checksum``. ``download_url`` already ran ``curl -fL``; it is
covered too, as a guard.

``--fail`` alone did not settle it, in either downloader. It lets a 3xx
through, and ``-L`` follows only a 3xx that says where to go, so a 302
without a Location left its page behind, with exit 0. curl's download now
counts only when the final status curl reports is a 2xx, or 000 for a
transfer with no status at all, such as a file:// copy. The status decides,
not the URL's scheme: a URL with no scheme, or an ftp:// one through an
http proxy, can end on an http response too. The urllib fallback, for its
part, took an empty 200 for the image. The ``-L`` is pinned as well:
without it, curl saves the page a server sends with a redirect and exits
0, and that page is taken for the image.

#227 -- nor is part of it, or a page wget saved. The urllib fallback read
until the server stopped sending; ``read()`` returns nothing when a server
hangs up early, as at the end of the body, so a cut-off body was taken for
the image. It is held to its Content-Length now. wget saves the page of a
300 that names nowhere to go and exits 0, so its download counts only when
the last status ``wget -S`` printed is a 2xx. The template's downloader is
``download_url`` since then; both are still asked.

Nothing here leaves the host. The server runs on a 127.0.0.1 ephemeral
port, as in test_http_download_proxy.py. Every ``*_proxy`` variable is
cleared and curl reads an empty ``.curlrc``, so curl goes to the server
directly, and looking up any name but 127.0.0.1 fails in this process,
where the urllib fallback runs. wget is stubbed to fail, except in the
tests that ask for the real one, which reads an empty ``.wgetrc``. curl is
the real one, and so is the urllib fallback after it unless a test says
otherwise.
"""

from __future__ import annotations

import http.server
import os
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from boxman.image_cache import ImageCache
from boxman.providers.libvirt import cloudinit
from boxman.utils import http_download

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("curl") is None, reason="needs the curl CLI"),
]

IMAGE = "/images/distro.qcow2"
#: answers that are not the image: the status, the body, and curl's exit on
#: it. --fail refuses an HTTP error with 22, like the 14-byte 401 page that
#: #216's probe saw saved. A 302 that says nowhere to go, and an empty 200,
#: curl ends with 0.
NOT_THE_IMAGE = {
    "http404": (404, b"<html><body><h1>404 Not Found</h1></body></html>\n", 22),
    "http401": (401, b"Unauthorized!\n", 22),
    "http302-no-location": (302, b"<html><body>302 Found, no Location</body></html>\n", 0),
    "http200-empty": (200, b"", 0),
}
#: an image, and how much of it a server that hangs up part-way sends
PAYLOAD = bytes(range(256)) * 400
CUT = 4096
#: where a server that moved the image redirects to, and the page it sends with the 302
MOVED = "/mirror/distro.qcow2"
MOVED_PAGE = b"<html><body>302 Found</body></html>\n"
#: the http origin an http proxy redirects an ftp:// download to
FINAL = "/final/distro.qcow2"


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Record who asked, then answer with the server's status and body.

    A server with a ``cut`` promises the whole body but sends only its
    first ``cut`` bytes, then hangs up. A path in its ``moved`` is answered
    with a 302 to where it moved, and a page saying so. A server that
    ``stalls`` sends the first request part of the body, sets ``stalled``,
    and sends no more until the server stops; the requests after it get
    the whole answer. A server that ``resumes`` cuts its first answer off
    like one with a ``cut``, and answers a request for the rest of the body
    (``Range: bytes=<first>-``) with a 206 and that rest. ``headers`` are
    sent with every answer but a 302.
    """

    def do_GET(self):
        server = self.server
        asked_range = self.headers.get("Range")
        server.requests.append(SimpleNamespace(
            target=self.path, agent=self.headers.get("User-Agent", ""), range=asked_range))
        if server.resume and asked_range:
            first = int(asked_range.removeprefix("bytes=").rstrip("-"))
            rest, whole = server.body[first:], len(server.body)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {first}-{whole - 1}/{whole}")
            self.send_header("Content-Length", str(len(rest)))
            self.end_headers()
            self.wfile.write(rest)
            self.close_connection = True
            return
        if server.stall and not server.stalled.is_set():
            self.send_response(200)
            self.send_header("Content-Length", str(len(server.body)))
            self.end_headers()
            self.wfile.write(server.body[:CUT])
            self.wfile.flush()
            server.stalled.set()
            server.released.wait()
            self.close_connection = True
            return
        location = server.moved.get(self.path)
        status, body, cut = ((302, MOVED_PAGE, None) if location
                             else (server.status, server.body, server.cut))
        if server.resume:
            cut = CUT
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        else:
            for name, value in server.headers.items():
                self.send_header(name, value)
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="images"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body[:cut])
        self.close_connection = True

    def log_message(self, *_args):
        pass  # keep the test output clean


class _Server(http.server.ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind would look the address up with getfqdn()
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


@contextmanager
def _serving(status, body, cut=None, moved=None, stall=False, resume=False, headers=None):
    """Answer requests with *status* and *body* from a 127.0.0.1 ephemeral port.

    *moved* maps a path to where it moved; a request for it gets a 302.
    """
    server = _Server(("127.0.0.1", 0), _Recorder)
    server.status, server.body, server.cut, server.requests = status, body, cut, []
    server.moved, server.headers, server.resume = moved or {}, headers or {}, resume
    server.stall, server.stalled, server.released = stall, threading.Event(), threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.released.set()
        server.shutdown()
        server.server_close()
        thread.join()


def _url(server):
    return f"http://127.0.0.1:{server.server_port}{IMAGE}"


def _asked_by(server):
    """The program behind each request *server* got: curl, or boxman's urllib."""
    return [request.agent.split("/")[0] for request in server.requests]


def _left_at(path):
    """What a download left at *path*: its bytes, or None."""
    return path.read_bytes() if path.exists() else None


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    """Fail a lookup of any name but 127.0.0.1: nothing here may leave the host."""
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host != "127.0.0.1":
            raise socket.gaierror(socket.EAI_NONAME, f"loopback-only test looked up {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def _curl_goes_direct(monkeypatch, tmp_path):
    """Clear every ``*_proxy`` variable, and give curl an empty ``.curlrc``.

    A proxy would stand between curl and the server. A ``.curlrc`` of the
    user's could add one, or a ``--fail`` that hid the bug; curl reads the
    first ``.curlrc`` it finds, and it looks in ``$CURL_HOME`` first.
    """
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    curl_home = tmp_path / "curl-home"
    curl_home.mkdir()
    (curl_home / ".curlrc").write_text("")
    monkeypatch.setenv("CURL_HOME", str(curl_home))


def _fail_wget(monkeypatch, module):
    """Make *module*'s wget fail, and hand its curl to the real ``_shell_run``.

    Returns the ``(program, exit status)`` of each command the module ran,
    in order; the stubbed wget has no exit status.
    """
    real_run = module._shell_run
    ran = []

    def shell_run(command, **kwargs):
        program = command.split()[0]
        if program == "wget":
            ran.append(("wget", None))
            return SimpleNamespace(ok=False)
        assert program == "curl", f"unexpected command: {command}"
        result = real_run(command, **kwargs)
        ran.append(("curl", result.exited))
        return result

    monkeypatch.setattr(module, "_shell_run", shell_run)
    return ran


def _template(tmp_path, **overrides):
    return cloudinit.CloudInitTemplate(
        template_name="t", image_path=str(tmp_path / "base.qcow2"),
        workdir=str(tmp_path / "workdir"),
        provider_config={"use_sudo": False, "uri": "qemu:///system"}, **overrides)


@pytest.fixture(params=["CloudInitTemplate._download_image", "download_url"])
def downloader(request, tmp_path):
    """``(module, download)``: a downloader, and the module whose shell it runs.

    The template's is ``download_url`` since #227; it kept a copy of it
    before, so both are still asked.
    """
    if request.param == "download_url":
        return http_download, http_download.download_url
    return http_download, _template(tmp_path)._download_image


@pytest.fixture(params=list(NOT_THE_IMAGE))
def not_the_image(request):
    """A server that answers every request with something that is not the image.

    Its ``curl_exit`` is the exit status curl ends that answer with.
    """
    status, body, curl_exit = NOT_THE_IMAGE[request.param]
    with _serving(status, body) as server:
        server.curl_exit = curl_exit
        yield server


def test_a_response_that_is_not_the_image_is_refused(
        downloader, not_the_image, monkeypatch, tmp_path):
    module, download = downloader
    ran = _fail_wget(monkeypatch, module)
    dst = tmp_path / "distro.qcow2"

    ok = download(_url(not_the_image), str(dst))

    assert (ok, _left_at(dst)) == (False, None)
    # 22: --fail refused an HTTP error and wrote none of it. 0: curl ended on
    # the answer, a 302's page or nothing at all, and that was refused too
    assert ran == [("wget", None), ("curl", not_the_image.curl_exit)]
    # the urllib fallback then asked the same server, and refused it as well
    assert _asked_by(not_the_image) == ["curl", "boxman"]


def test_a_response_that_is_not_the_image_is_not_cached(not_the_image, monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    template = _template(tmp_path, image_cache=ImageCache(cache_dir=str(cache_dir)))
    ran = _fail_wget(monkeypatch, http_download)
    dst = tmp_path / "distro.qcow2"

    ok = template._fetch_remote_image(_url(not_the_image), str(dst))

    # nothing in the cache for the next run to take as the image
    cached = {path.name: path.read_bytes() for path in cache_dir.iterdir()}
    assert (ok, cached, _left_at(dst)) == (False, {}, None)
    assert ran == [("wget", None), ("curl", not_the_image.curl_exit)]
    assert _asked_by(not_the_image) == ["curl", "boxman"]


def test_a_cut_off_download_is_removed_before_the_urllib_fallback(
        downloader, monkeypatch, tmp_path):
    module, download = downloader
    ran = _fail_wget(monkeypatch, module)
    dst = tmp_path / "distro.qcow2"
    at_fallback = []

    def urllib_fallback():
        # how much of the image the fallback would have found at dst
        at_fallback.append(dst.stat().st_size if dst.exists() else None)
        raise OSError("the urllib fallback is not under test here")

    monkeypatch.setattr(module, "build_opener", urllib_fallback)
    with _serving(200, PAYLOAD, cut=CUT) as server:
        ok = download(_url(server), str(dst))

    # 18: the transfer was cut off, and curl kept the part it got...
    assert ran == [("wget", None), ("curl", 18)]
    # ...which was gone by the time the urllib fallback began
    assert at_fallback == [None]
    assert (ok, _left_at(dst)) == (False, None)
    assert _asked_by(server) == ["curl"]


def test_curl_follows_a_redirect_to_the_image(downloader, monkeypatch, tmp_path):
    module, download = downloader
    ran = _fail_wget(monkeypatch, module)
    real_build_opener, fallback = module.build_opener, []

    def urllib_fallback():
        fallback.append("began")
        return real_build_opener()

    monkeypatch.setattr(module, "build_opener", urllib_fallback)
    dst = tmp_path / "distro.qcow2"
    with _serving(200, PAYLOAD, moved={IMAGE: MOVED}) as server:
        ok = download(_url(server), str(dst))

    assert ok is True
    # the image, not the page that came with the 302
    assert dst.read_bytes() == PAYLOAD
    # curl did it: it asked for the image, then where it moved, and exited 0...
    assert ran == [("wget", None), ("curl", 0)]
    assert [request.target for request in server.requests] == [IMAGE, MOVED]
    assert _asked_by(server) == ["curl", "curl"]
    # ...and the urllib fallback never began
    assert fallback == []


@pytest.mark.parametrize("start", ["no-scheme", "ftp-through-an-http-proxy"])
def test_a_transfer_that_ends_on_http_is_held_to_a_2xx(downloader, start, monkeypatch, tmp_path):
    module, download = downloader
    ran = _fail_wget(monkeypatch, module)
    dst = tmp_path / "distro.qcow2"
    page = NOT_THE_IMAGE["http302-no-location"][1]

    with _serving(302, page) as server:
        origin = f"127.0.0.1:{server.server_port}"
        if start == "no-scheme":
            # curl fetches a URL without a scheme as http; urllib cannot
            url, route, fallback_route = f"{origin}{IMAGE}", [IMAGE], []
        else:
            # the loopback server is the http proxy of the ftp:// download,
            # and the http origin the proxy redirects it to
            url = f"ftp://{origin}{IMAGE}"
            monkeypatch.setenv("ftp_proxy", f"http://{origin}")
            server.moved[url] = f"http://{origin}{FINAL}"
            route = fallback_route = [url, FINAL]
        ok = download(url, str(dst))

    assert (ok, _left_at(dst)) == (False, None)
    # curl went where the URL led, and ended on the 302's page with exit 0...
    assert ran == [("wget", None), ("curl", 0)]
    assert [r.target for r in server.requests if r.agent.startswith("curl/")] == route
    # ...and the urllib fallback, where it could follow, was refused as well
    assert [r.target for r in server.requests
            if r.agent.startswith("boxman/")] == fallback_route


def _fail_wget_and_curl(monkeypatch, module):
    """Make *module*'s wget and curl both fail, so that urllib downloads."""
    ran = []

    def shell_run(command, **_kwargs):
        ran.append(command.split()[0])
        return SimpleNamespace(ok=False)

    monkeypatch.setattr(module, "_shell_run", shell_run)
    return ran


def _run_for_real(monkeypatch, module):
    """Run *module*'s wget and curl for real, recording ``(program, exit status)``."""
    real_run = module._shell_run
    ran = []

    def shell_run(command, **kwargs):
        program = command.split()[0]
        assert program in ("wget", "curl"), f"unexpected command: {command}"
        result = real_run(command, **kwargs)
        ran.append((program, result.exited))
        return result

    monkeypatch.setattr(module, "_shell_run", shell_run)
    return ran


@pytest.fixture(params=["empty", "logfile"], ids=["wgetrc-empty", "wgetrc-logfile"])
def real_wget(request, monkeypatch, tmp_path):
    """The real wget, reading a ``.wgetrc`` of the test's rather than the user's.

    One is empty. The other sends wget's messages to a log file, the
    status lines among them, where boxman would not see them (#227 review).
    """
    if shutil.which("wget") is None:
        pytest.skip("needs the wget CLI")
    wgetrc = tmp_path / "wgetrc"
    wgetrc.write_text(f"logfile = {tmp_path / 'wget.log'}\n" if request.param == "logfile" else "")
    monkeypatch.setenv("WGETRC", str(wgetrc))


def test_a_body_cut_short_of_its_length_is_refused(downloader, monkeypatch, tmp_path):
    """#227: the urllib fallback read to the end of what came and took that.

    ``read()`` returns nothing when a server hangs up early, as it does at
    the end of the body, so only the Content-Length tells the two apart.
    """
    module, download = downloader
    ran = _fail_wget(monkeypatch, module)
    dst = tmp_path / "distro.qcow2"

    with _serving(200, PAYLOAD, cut=CUT) as server:
        ok = download(_url(server), str(dst))

    assert (ok, _left_at(dst)) == (False, None)
    # curl saw the cut (18), and the urllib fallback after it got the same
    assert ran == [("wget", None), ("curl", 18)]
    assert _asked_by(server) == ["curl", "boxman"]


def test_a_body_cut_short_is_not_cached(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    template = _template(tmp_path, image_cache=ImageCache(cache_dir=str(cache_dir)))
    _fail_wget(monkeypatch, http_download)
    dst = tmp_path / "distro.qcow2"

    with _serving(200, PAYLOAD, cut=CUT) as server:
        ok = template._fetch_remote_image(_url(server), str(dst))

    cached = {path.name: path.read_bytes() for path in cache_dir.iterdir()}
    assert (ok, cached, _left_at(dst)) == (False, {}, None)


def test_the_urllib_fallback_takes_a_whole_body(downloader, monkeypatch, tmp_path):
    module, download = downloader
    ran = _fail_wget_and_curl(monkeypatch, module)
    dst = tmp_path / "distro.qcow2"

    with _serving(200, PAYLOAD) as server:
        ok = download(_url(server), str(dst))

    assert (ok, _left_at(dst)) == (True, PAYLOAD)
    assert ran == ["wget", "curl"]
    assert _asked_by(server) == ["boxman"]


#: what a server sends with a 300 that names no choice to follow
MULTIPLE_CHOICES_PAGE = b"<html><body>300 Multiple Choices</body></html>\n"


def test_wget_does_not_take_a_300s_page_for_the_image(
        downloader, real_wget, monkeypatch, tmp_path):
    """wget saves the page of a 300 with no Location, and exits 0 (#227).

    A 302 with no Location wget refuses on its own (exit 8); a 300 it does
    not. Its exit status cannot tell, so the last status it printed must.
    """
    module, download = downloader
    ran = _run_for_real(monkeypatch, module)
    dst = tmp_path / "distro.qcow2"

    with _serving(300, MULTIPLE_CHOICES_PAGE) as server:
        ok = download(_url(server), str(dst))

    assert (ok, _left_at(dst)) == (False, None)
    # wget and curl both ended on the page with exit 0, and neither counted;
    # the urllib fallback refused it too
    assert ran == [("wget", 0), ("curl", 0)]
    assert _asked_by(server) == ["Wget", "curl", "boxman"]


def test_wget_follows_a_redirect_to_the_image(downloader, real_wget, monkeypatch, tmp_path):
    module, download = downloader
    ran = _run_for_real(monkeypatch, module)
    dst = tmp_path / "distro.qcow2"

    with _serving(200, PAYLOAD, moved={IMAGE: MOVED}) as server:
        ok = download(_url(server), str(dst))

    assert (ok, _left_at(dst)) == (True, PAYLOAD)
    assert ran == [("wget", 0)]
    assert [request.target for request in server.requests] == [IMAGE, MOVED]


@pytest.mark.parametrize("exited", [-signal.SIGINT, 128 + signal.SIGINT])
@pytest.mark.parametrize("stopped", ["wget", "curl"])
def test_a_downloader_stopped_by_ctrl_c_ends_the_download(
        downloader, stopped, exited, monkeypatch, tmp_path):
    """invoke swallows a Ctrl-C and returns the command's death by SIGINT.

    Read as a failure, it sent the download on to the next downloader, so a
    Ctrl-C started it over (#227); a shell reports the same death as 130.
    """
    module, download = downloader
    dst = tmp_path / "distro.qcow2"
    ran = []

    def shell_run(command, **_kwargs):
        program = command.split()[0]
        ran.append(program)
        dst.write_bytes(PAYLOAD[:CUT])  # each leaves part of the image
        return SimpleNamespace(ok=False, exited=exited if program == stopped else 4,
                               stdout="", stderr="")

    def urllib_fallback():
        ran.append("urllib")
        raise OSError("the urllib fallback began")

    monkeypatch.setattr(module, "_shell_run", shell_run)
    monkeypatch.setattr(module, "build_opener", urllib_fallback)

    with pytest.raises(KeyboardInterrupt):
        download("http://127.0.0.1:9/distro.qcow2", str(dst))

    # nothing after the one that was stopped
    assert ran == (["wget"] if stopped == "wget" else ["wget", "curl"])
    assert _left_at(dst) is None


#: run in a process of its own, so that it can be sent a Ctrl-C
_DOWNLOAD_IN_A_CHILD = """
import sys
from boxman.utils.http_download import download_url
try:
    print("RETURNED", download_url(sys.argv[1], sys.argv[2]), flush=True)
except KeyboardInterrupt:
    print("STOPPED", flush=True)
"""


@pytest.mark.parametrize("to", ["the-process-group", "boxman-alone"])
def test_ctrl_c_stops_a_download_wget_is_doing(real_wget, to, tmp_path):
    """The whole of it, for real.

    A terminal's Ctrl-C reaches wget and boxman alike. A SIGINT to boxman
    alone, from ``kill -INT`` or a supervisor, invoke used to answer by
    writing ``\\x03`` to wget's stdin, which wget does not read: the
    download ran on (#227 review).
    """
    dst = tmp_path / "distro.qcow2"
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    with _serving(200, PAYLOAD, stall=True) as server:
        child = subprocess.Popen(
            [sys.executable, "-c", _DOWNLOAD_IN_A_CHILD, _url(server), str(dst)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            start_new_session=True)
        try:
            # wget is part-way through once the server has sent it part of the image
            assert server.stalled.wait(timeout=60)
            if to == "the-process-group":
                os.killpg(child.pid, signal.SIGINT)
            else:
                os.kill(child.pid, signal.SIGINT)
            out, _ = child.communicate(timeout=30)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()

    # its last word, after boxman's own log lines
    assert [line for line in out.splitlines()
            if line.startswith(("STOPPED", "RETURNED"))] == ["STOPPED"]
    # nothing went on to ask again: not curl, not the urllib fallback
    assert _asked_by(server) == ["Wget"]
    assert _left_at(dst) is None


#: wget 1.x's ``-S`` output: each response's status line and headers,
#: indented two spaces, among wget's own messages
WGET_302_THEN_200 = """\
--2026-10-05 21:16:00--  http://127.0.0.1:41915/moved
Connecting to 127.0.0.1:41915... connected.
HTTP request sent, awaiting response...
  HTTP/1.0 302 Found
  Location: /ok
  Content-Length: 0
Location: /ok [following]
--2026-10-05 21:16:00--  http://127.0.0.1:41915/ok
HTTP request sent, awaiting response...
  HTTP/1.0 200 OK
  Content-Length: 102400
Length: 102400 (100K)
"""
WGET_300 = """\
HTTP request sent, awaiting response...
  HTTP/1.0 300 Multiple Choices
  Content-Length: 47
Length: 47
"""


#: a 206 for the rest of the image, from byte 4096 on, as wget asks for when
#: it resumes a download that was cut off
RESUMED = "  HTTP/1.1 206 Partial Content\n  Content-Range: bytes 4096-102399/102400\n"
HTTP = "http://127.0.0.1:41915/images/distro.qcow2"
WHOLE = 102400


@pytest.mark.parametrize(("output", "url", "size", "counts"), [
    pytest.param(WGET_302_THEN_200, HTTP, WHOLE, True, id="302-then-200"),  # the last decides
    pytest.param(WGET_300, HTTP, 47, False, id="300"),
    pytest.param("  HTTP/2 200\n", HTTP, WHOLE, True, id="http2-200"),
    pytest.param("  HTTP/1.1 200 OK\n  HTTP/1.1 300 Multiple Choices\n", HTTP, WHOLE, False,
                 id="200-then-300"),
    # a status line is one wget printed, not one inside a header value
    pytest.param("  HTTP/1.1 200 OK\n  X-Note: HTTP/1.1 300\n", HTTP, WHOLE, True,
                 id="status-in-a-header"),
    # a 206 counts when what wget has is the whole length it gives
    pytest.param(RESUMED, HTTP, WHOLE, True, id="206-resumed-whole"),
    pytest.param(RESUMED, HTTP, WHOLE - 1, False, id="206-resumed-short"),
    pytest.param("  HTTP/1.1 206 Partial Content\n  Content-Range: bytes 0-4095/102400\n",
                 HTTP, 4096, False, id="206-a-part"),
    pytest.param("  HTTP/1.1 206 Partial Content\n  Content-Length: 4096\n", HTTP, 4096,
                 False, id="206-no-range"),
    pytest.param("  HTTP/1.1 206 Partial Content\n  Content-Range: bytes */102400\n",
                 HTTP, WHOLE, False, id="206-unsatisfied-range"),
    # the Content-Range of an earlier response is not the final one's
    pytest.param("  HTTP/1.1 206 Partial Content\n  Content-Range: bytes 0-102399/102400\n"
                 "Retrying.\n  HTTP/1.1 206 Partial Content\n  Content-Length: 4096\n",
                 HTTP, WHOLE, False, id="206-range-of-an-earlier-response"),
    # no status printed at all: wget's exit status decides for ftp://, which
    # prints none, as before; an http(s) one whose status went unseen does
    # not count (#227 review: a wgetrc can send it to a log file)
    pytest.param("==> RETR distro.qcow2 ... done.\n", "ftp://127.0.0.1/distro.qcow2", WHOLE,
                 True, id="ftp"),
    pytest.param("", "ftp://127.0.0.1/distro.qcow2", WHOLE, True, id="ftp-nothing"),
    pytest.param("", HTTP, WHOLE, False, id="http-nothing"),
    pytest.param("", "https://127.0.0.1/distro.qcow2", WHOLE, False, id="https-nothing"),
    pytest.param("", "127.0.0.1:41915/distro.qcow2", WHOLE, False, id="no-scheme-nothing"),
])
def test_only_a_whole_2xx_wget_printed_last_counts(output, url, size, counts):
    assert http_download.wget_download_ok(output, url, size) is counts


#: an answer that says it is the first 4096 bytes of the image
A_PART = {"Content-Range": f"bytes 0-{CUT - 1}/{len(PAYLOAD)}"}


def test_a_part_the_server_calls_a_part_is_refused(downloader, real_wget, monkeypatch, tmp_path):
    """A 206 to a request for the whole is a part of it (#227 review).

    It came complete, with the length it promised, so no length check sees
    it; only its Content-Range says what it is a part of. curl and urllib
    never ask for a range, so neither takes a 206 at all.
    """
    module, download = downloader
    ran = _run_for_real(monkeypatch, module)
    dst = tmp_path / "distro.qcow2"

    with _serving(206, PAYLOAD[:CUT], headers=A_PART) as server:
        ok = download(_url(server), str(dst))

    assert (ok, _left_at(dst)) == (False, None)
    assert ran == [("wget", 0), ("curl", 0)]
    assert _asked_by(server) == ["Wget", "curl", "boxman"]


def test_wget_resumes_a_download_that_was_cut_off(downloader, real_wget, monkeypatch, tmp_path):
    """A 206 that completes what wget has counts: wget asks for it itself."""
    module, download = downloader
    ran = _run_for_real(monkeypatch, module)
    dst = tmp_path / "distro.qcow2"

    with _serving(200, PAYLOAD, resume=True) as server:
        ok = download(_url(server), str(dst))

    assert (ok, _left_at(dst)) == (True, PAYLOAD)
    assert ran == [("wget", 0)]
    # the first answer was cut off; wget asked for the rest of it
    assert [request.range for request in server.requests] == [None, f"bytes={CUT}-"]


@pytest.mark.parametrize(("status", "counts"), [
    ("200", True),
    ("206", False),  # a part: curl is never asked for a range (#227 review)
    ("226", True),  # FTP's transfer complete
    ("000", True),  # no response code at all, as for a file:// copy
    ("200\n", True),  # whitespace around the status is not part of it
    (" \t226 ", True),
    ("302", False),  # a 3xx that went nowhere
    ("404", False),
    ("", False),  # no status printed
    ("x200", False),  # nor is anything before or after it
    ("200x", False),
    ("2000", False),
    ("0000", False),
    ("200 302", False),
])
def test_only_a_2xx_or_no_status_counts(status, counts):
    assert http_download.curl_final_status_ok(status) is counts
