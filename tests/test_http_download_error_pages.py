"""
#224 -- an HTTP error page is never taken for the image.

``CloudInitTemplate._download_image`` downloads the base image of every
cloud-image template. When wget failed, it ran ``curl -L`` without
``--fail``: curl saved the error page the server answered with and exited
0, so the page was accepted as the image and, with the image cache on,
stored in the cache and reused from there, unless the template set an
``image_checksum``. ``download_url`` already runs ``curl -fL``; it is
covered too, as a guard. The ``-L`` is pinned as well: without it, curl
saves the page a server sends with a redirect and exits 0, and that page
is taken for the image.

Nothing here leaves the host. The server runs on a 127.0.0.1 ephemeral
port, as in test_http_download_proxy.py. Every ``*_proxy`` variable is
cleared and curl reads an empty ``.curlrc``, so curl goes to the server
directly, and looking up any name but 127.0.0.1 fails in this process,
where the urllib fallback runs. wget is stubbed to fail. curl is the real
one, and so is the urllib fallback after it unless a test says otherwise.
"""

from __future__ import annotations

import http.server
import os
import shutil
import socket
import socketserver
import threading
from contextlib import contextmanager
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
#: small error pages, like the 14-byte 401 page that #216's probe saw saved
ERROR_PAGES = {
    404: b"<html><body><h1>404 Not Found</h1></body></html>\n",
    401: b"Unauthorized!\n",
}
#: an image, and how much of it a server that hangs up part-way sends
PAYLOAD = bytes(range(256)) * 400
CUT = 4096
#: where a server that moved the image redirects to, and the page it sends with the 302
MOVED = "/mirror/distro.qcow2"
MOVED_PAGE = b"<html><body>302 Found</body></html>\n"


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Record who asked, then answer with the server's status and body.

    A server with a ``cut`` promises the whole body but sends only its
    first ``cut`` bytes, then hangs up. A path in its ``moved`` is answered
    with a 302 to where it moved, and a page saying so.
    """

    def do_GET(self):
        server = self.server
        server.requests.append(SimpleNamespace(
            target=self.path, agent=self.headers.get("User-Agent", "")))
        location = server.moved.get(self.path)
        status, body, cut = ((302, MOVED_PAGE, None) if location
                             else (server.status, server.body, server.cut))
        self.send_response(status)
        if location:
            self.send_header("Location", location)
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
def _serving(status, body, cut=None, moved=None):
    """Answer requests with *status* and *body* from a 127.0.0.1 ephemeral port.

    *moved* maps a path to where it moved; a request for it gets a 302.
    """
    server = _Server(("127.0.0.1", 0), _Recorder)
    server.status, server.body, server.cut, server.requests = status, body, cut, []
    server.moved = moved or {}
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield server
    finally:
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
    """``(module, download)``: a downloader, and the module whose shell it runs."""
    if request.param == "download_url":
        return http_download, http_download.download_url
    return cloudinit, _template(tmp_path)._download_image


@pytest.fixture(params=sorted(ERROR_PAGES), ids=lambda status: f"http{status}")
def error_page(request):
    """A server that answers every request with an error page."""
    with _serving(request.param, ERROR_PAGES[request.param]) as server:
        yield server


def test_an_error_page_is_not_taken_for_the_image(downloader, error_page, monkeypatch, tmp_path):
    module, download = downloader
    ran = _fail_wget(monkeypatch, module)
    dst = tmp_path / "distro.qcow2"

    ok = download(_url(error_page), str(dst))

    assert (ok, _left_at(dst)) == (False, None)
    # 22: curl --fail refused the page, and wrote none of it
    assert ran == [("wget", None), ("curl", 22)]
    # the urllib fallback then asked the same server, and failed cleanly too
    assert _asked_by(error_page) == ["curl", "boxman"]


def test_an_error_page_is_not_stored_in_the_image_cache(error_page, monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    template = _template(tmp_path, image_cache=ImageCache(cache_dir=str(cache_dir)))
    ran = _fail_wget(monkeypatch, cloudinit)
    dst = tmp_path / "distro.qcow2"

    ok = template._fetch_remote_image(_url(error_page), str(dst))

    # nothing in the cache for the next run to take as the image
    cached = {path.name: path.read_bytes() for path in cache_dir.iterdir()}
    assert (ok, cached, _left_at(dst)) == (False, {}, None)
    assert ran == [("wget", None), ("curl", 22)]
    assert _asked_by(error_page) == ["curl", "boxman"]


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
