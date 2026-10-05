"""
#216 -- the urllib download fallbacks hand proxy credentials to the proxy
they belong to, and to no one else.

When wget and curl both fail, ``download_url`` and the cloud-image
template's own copy of it, ``CloudInitTemplate._download_image``, download
with urllib. Since #227 the template's is ``download_url`` itself; both are
still asked. urllib's ProxyHandler adds ``Proxy-Authorization`` (from a
proxy URL such as ``http://user:secret@proxy:3128``) as an ordinary request
header, and the stock redirect handler copies every header onto the
redirected request. So when a proxied mirror redirected, the password went
along: to a ``no_proxy`` host or an https CDN reached directly, or in the
CONNECT to another proxy.

Nothing here leaves the host. A "proxy" and an "origin" server run on
127.0.0.1 ephemeral ports and record the headers of every request they
get, and looking up any name but 127.0.0.1 fails. The mirror and CDN are
``.invalid`` hosts: a proxied request carries its absolute URL to the
proxy, so the client never looks them up. wget and curl are stubbed to
fail, so the urllib fallback does every download.
"""

from __future__ import annotations

import http.server
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from boxman.providers.libvirt import cloudinit
from boxman.utils import http_download

pytestmark = pytest.mark.unit

#: the Proxy-Authorization urllib builds for user:secret
CREDENTIALS = "Basic dXNlcjpzZWNyZXQ="
MIRROR = "http://mirror.invalid/images/distro.qcow2"
CDN = "http://cdn.invalid/images/distro.qcow2"
IMAGE = "/images/distro.qcow2"
#: more than two of the fallback's 1 MiB reads, ending part-way into one
PAYLOAD = bytes(range(256)) * 10_000 + b"end"


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Record every request, then answer it from the server's routes.

    A route maps a request target -- an absolute URL at the proxy, a path
    at the origin -- to ``("redirect", location)`` or ``("serve", body)``.
    Anything else, a CONNECT included, is refused with a 403.
    """

    def _record(self):
        self.server.requests.append(SimpleNamespace(
            method=self.command, target=self.path, headers=self.headers.items()))

    def do_GET(self):
        self._record()
        action, value = self.server.routes.get(self.path, (None, None))
        if action == "redirect":
            self.send_response(302)
            self.send_header("Location", value)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif action == "serve":
            self.send_response(200)
            self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            self.wfile.write(value)
        else:
            self.send_error(403)

    def do_CONNECT(self):
        self._record()
        self.send_error(403)

    def log_message(self, *_args):
        pass  # keep the test output clean


class _Server(http.server.ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind would look the address up with getfqdn()
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


@contextmanager
def _serving(routes=None, tls=None):
    """Run a recording server on a 127.0.0.1 ephemeral port for the block.

    *tls* is a ``(cert, key)`` pair to serve https with.
    """
    server = _Server(("127.0.0.1", 0), _Recorder)
    server.routes, server.requests = routes or {}, []
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*tls)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _proxy_auth(request):
    """Every Proxy-Authorization value *request* arrived with."""
    return [value for key, value in request.headers if key.lower() == "proxy-authorization"]


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    """Fail a lookup of any name but 127.0.0.1: nothing here may leave the host."""
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host != "127.0.0.1":
            raise socket.gaierror(socket.EAI_NONAME, f"loopback-only test looked up {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def proxies(monkeypatch):
    """Clear every ``*_proxy`` variable; the test sets the ones it names."""
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    # the stock urlopen() builds its opener, proxies included, on first use
    # and keeps it; start from none, so a return to it meets these proxies
    monkeypatch.setattr(urllib.request, "_opener", None)

    def use(**values):
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    return use


@pytest.fixture(params=["download_url", "CloudInitTemplate._download_image"])
def download(request, monkeypatch, tmp_path):
    """A downloader whose wget and curl fail, so that urllib downloads."""
    if request.param == "download_url":
        fetch = http_download.download_url
    else:
        template = cloudinit.CloudInitTemplate(
            template_name="t", image_path=str(tmp_path / "base.qcow2"),
            workdir=str(tmp_path / "workdir"),
            provider_config={"use_sudo": False, "uri": "qemu:///system"})
        # download_url since #227, so the same shell
        fetch = template._download_image
    tried = []

    def downloader_fails(command, **_kwargs):
        tried.append(command.split()[0])
        return SimpleNamespace(ok=False)

    monkeypatch.setattr(http_download, "_shell_run", downloader_fails)

    def run(url, dst):
        ok = fetch(url, str(dst))
        assert tried == ["wget", "curl"]
        return ok

    return run


@pytest.fixture
def loopback_cert(tmp_path, monkeypatch):
    """A certificate for 127.0.0.1 that the default SSL context trusts."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("needs the openssl CLI to make a certificate")
    cert, key = str(tmp_path / "cert.pem"), str(tmp_path / "key.pem")
    # A config of our own and every extension spelled out: Python 3.13's
    # strict verification wants a CA's basic constraints, key usage and key
    # identifiers, and the basic constraints came from the host's openssl.cnf
    # (its v3_ca section), so without one the https cases failed. Not
    # /dev/null: OpenSSL 1.1.1's req wants a distinguished_name section even
    # with -subj.
    config = tmp_path / "openssl.cnf"
    config.write_text("[req]\ndistinguished_name = dn\n[dn]\n")
    subprocess.run(
        [openssl, "req", "-x509", "-config", str(config),
         "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-keyout", key, "-out", cert, "-days", "2", "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,digitalSignature,keyCertSign",
         "-addext", "subjectKeyIdentifier=hash",
         "-addext", "authorityKeyIdentifier=keyid:always"],
        check=True, capture_output=True, timeout=60)
    monkeypatch.setenv("SSL_CERT_FILE", cert)
    return cert, key


def test_a_no_proxy_origin_is_not_handed_the_proxy_password(proxies, download, tmp_path):
    dst = tmp_path / "distro.qcow2"
    with _serving({IMAGE: ("serve", PAYLOAD)}) as origin:
        moved = f"http://127.0.0.1:{origin.server_port}{IMAGE}"
        with _serving({MIRROR: ("redirect", moved)}) as proxy:
            proxies(http_proxy=f"http://user:secret@127.0.0.1:{proxy.server_port}",
                    no_proxy="127.0.0.1")
            assert download(MIRROR, dst) is True

    # the mirror was asked through the authenticated proxy...
    assert [(r.target, _proxy_auth(r)) for r in proxy.requests] == [(MIRROR, [CREDENTIALS])]
    # ...and the origin it moved to, exempt from the proxy, without the password
    assert [(r.target, _proxy_auth(r)) for r in origin.requests] == [(IMAGE, [])]
    assert dict(origin.requests[0].headers)["User-Agent"] == "boxman/1.0"
    assert dst.read_bytes() == PAYLOAD


def test_an_https_origin_with_no_https_proxy_is_not_handed_it(
        proxies, download, loopback_cert, tmp_path):
    dst = tmp_path / "distro.qcow2"
    with _serving({IMAGE: ("serve", PAYLOAD)}, tls=loopback_cert) as origin:
        moved = f"https://127.0.0.1:{origin.server_port}{IMAGE}"
        with _serving({MIRROR: ("redirect", moved)}) as proxy:
            # no https_proxy: the https CDN is reached directly
            proxies(http_proxy=f"http://user:secret@127.0.0.1:{proxy.server_port}")
            assert download(MIRROR, dst) is True

    assert [(r.target, _proxy_auth(r)) for r in proxy.requests] == [(MIRROR, [CREDENTIALS])]
    assert [(r.target, _proxy_auth(r)) for r in origin.requests] == [(IMAGE, [])]
    assert dst.read_bytes() == PAYLOAD


def test_another_proxy_is_not_handed_it(proxies, download, tmp_path):
    # an https destination is reached through https_proxy, in a CONNECT that
    # the stock opener filled with http_proxy's password
    cdn = "https://cdn.invalid/images/distro.qcow2"
    dst = tmp_path / "distro.qcow2"
    with _serving() as other:  # refuses the CONNECT: only its headers matter
        with _serving({MIRROR: ("redirect", cdn)}) as proxy:
            proxies(http_proxy=f"http://user:secret@127.0.0.1:{proxy.server_port}",
                    https_proxy=f"http://127.0.0.1:{other.server_port}")
            assert download(MIRROR, dst) is False

    assert [(r.target, _proxy_auth(r)) for r in proxy.requests] == [(MIRROR, [CREDENTIALS])]
    assert [(r.method, r.target, _proxy_auth(r)) for r in other.requests] == [
        ("CONNECT", "cdn.invalid:443", [])]
    assert not dst.exists()


def test_a_redirect_through_the_same_proxy_is_authenticated_again(proxies, download, tmp_path):
    # dropping the header on a redirect must not break a proxied download:
    # the proxy handler adds it back for a destination it proxies
    dst = tmp_path / "distro.qcow2"
    with _serving({MIRROR: ("redirect", CDN), CDN: ("serve", PAYLOAD)}) as proxy:
        proxies(http_proxy=f"http://user:secret@127.0.0.1:{proxy.server_port}")
        assert download(MIRROR, dst) is True

    assert [(r.target, _proxy_auth(r)) for r in proxy.requests] == [
        (MIRROR, [CREDENTIALS]), (CDN, [CREDENTIALS])]
    assert dst.read_bytes() == PAYLOAD
