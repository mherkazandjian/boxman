"""Unit tests for scripts/check_box_images.py, the dead base-image URL checker.

The checker is a dev script, not part of the importable ``boxman`` package,
so it is loaded by path. No test touches the network: a fake transport sits
in front of urllib's real HTTP(S) handlers and answers from a routing table,
so redirects, error statuses and the HEAD -> ranged GET fallback all run
through urllib's own processing -- only the socket is fake.
"""

import http.client
import importlib.util
import io
import os
import socket
import urllib.error
import urllib.request
import urllib.response

import pytest

pytestmark = pytest.mark.unit

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO, "scripts", "check_box_images.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("boxman_box_image_checker", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_module()

URL = "https://mirror.example.org/images/distro-9.7.qcow2"
MOVED = "https://cdn.example.org/images/distro-9.7.qcow2"


class FakeTransport(urllib.request.BaseHandler):
    """Answer http(s) requests from *routes* instead of the network.

    *routes* maps ``(method, url)`` to a status, a ``(status, headers)``
    pair, or an exception to raise. A request with no route fails the test,
    which is how "no request was made" is asserted.
    """

    # ahead of urllib's ProxyHandler (100) and HTTP(S)Handler (500)
    handler_order = 50

    def __init__(self, routes):
        self.routes = routes
        self.seen = []

    def _answer(self, req):
        method = req.get_method()
        self.seen.append((method, req.full_url, req.get_header("Range")))
        answer = self.routes.get((method, req.full_url))
        if answer is None:
            raise AssertionError(f"unexpected request: {method} {req.full_url}")
        if isinstance(answer, BaseException):
            raise answer
        status, headers = answer if isinstance(answer, tuple) else (answer, {})
        msg = http.client.HTTPMessage()
        for key, value in headers.items():
            msg[key] = value
        response = urllib.response.addinfourl(io.BytesIO(b""), msg, req.full_url, status)
        response.msg = http.client.responses.get(status, "")
        return response

    http_open = _answer
    https_open = _answer


def _check(routes, url=URL):
    transport = FakeTransport(routes)
    status = checker.check_url(url, timeout=5, opener=checker.build_opener(transport))
    return status, transport.seen


# --------------------------------------------------------------------------- #
# check_url                                                                   #
# --------------------------------------------------------------------------- #
def test_live_url_is_ok_from_a_head_alone():
    status, seen = _check({("HEAD", URL): 200})
    assert status.state == checker.OK
    assert seen == [("HEAD", URL, None)]


@pytest.mark.parametrize("code", [404, 410])
def test_missing_image_is_gone(code):
    status, seen = _check({("HEAD", URL): code, ("GET", URL): code})
    assert status.state == checker.GONE
    assert status.code == code
    # a "gone" verdict fails a CI tier, so HEAD alone never decides it
    assert [s[0] for s in seen] == ["HEAD", "GET"]


def test_redirect_to_404_is_gone_and_names_the_final_url():
    status, seen = _check({
        ("HEAD", URL): (302, {"Location": MOVED}),
        ("HEAD", MOVED): 404,
        ("GET", URL): (302, {"Location": MOVED}),
        ("GET", MOVED): 404,
    })
    assert status.state == checker.GONE
    assert status.final_url == MOVED


def test_redirect_keeps_a_head_a_head():
    # urllib re-issues a redirected request as a GET, which against a real
    # mirror starts streaming the whole image
    status, seen = _check({
        ("HEAD", URL): (301, {"Location": MOVED}),
        ("HEAD", MOVED): 200,
    })
    assert status.state == checker.OK
    assert status.final_url == MOVED
    assert seen == [("HEAD", URL, None), ("HEAD", MOVED, None)]


@pytest.mark.parametrize("head_code", [405, 501, 403])
def test_head_rejected_then_ranged_get_decides(head_code):
    status, seen = _check({("HEAD", URL): head_code, ("GET", URL): 206})
    assert status.state == checker.OK
    assert status.code == 206
    assert seen[1] == ("GET", URL, "bytes=0-0")


@pytest.mark.parametrize("failure, expected", [
    (urllib.error.URLError(TimeoutError("timed out")), "timed out after 5s"),
    # a read timeout escapes urllib unwrapped
    (TimeoutError("timed out"), "timed out after 5s"),
    (urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
     "Name or service not known"),
    (urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
     "Connection refused"),
    (http.client.RemoteDisconnected("Remote end closed connection without response"),
     "Remote end closed connection"),
])
def test_network_failure_is_unreachable_not_gone(failure, expected):
    status, seen = _check({("HEAD", URL): failure})
    assert status.state == checker.UNREACHABLE
    assert expected in status.detail
    # no second request to wait out another timeout on
    assert len(seen) == 1


def test_other_error_status_is_an_error_not_gone():
    status, _ = _check({("HEAD", URL): 503, ("GET", URL): 503})
    assert status.state == checker.ERROR
    assert status.code == 503


@pytest.mark.parametrize("source", [
    "oci://docker.io/tedezed/ubuntu-container-disk:24.04",
    "/var/lib/libvirt/images/base.qcow2",
    "file:///var/lib/libvirt/images/base.qcow2",
])
def test_non_http_source_is_skipped_without_a_request(source):
    # an empty routing table fails the test on any request at all
    status, seen = _check({}, url=source)
    assert status.state == checker.SKIPPED
    assert seen == []


# --------------------------------------------------------------------------- #
# collect_image_refs / check_refs                                             #
# --------------------------------------------------------------------------- #
CONFIG = {
    "templates": {
        "rocky": {"image": {"uri": URL, "checksum": "sha256:" + "ab" * 32}},
        "bare": {"image": "https://mirror.example.org/bare.img"},
        "oci": {"image": {"uri": "oci://registry.example.org/img:1"}},
        "no-image": {"name": "x"},
    },
    "isos": {
        "live": {"uri": "https://mirror.example.org/live.iso", "checksum": "sha256:" + "cd" * 32},
        "omni": {"uri": "https://factory.example.org/omni.iso", "checksum": "sha256:" + "0" * 64},
    },
}


def test_collect_image_refs_finds_template_images_and_isos():
    conf = os.path.join(_REPO, "boxes", "some-box", "conf.yml")
    refs = {(r.kind, r.name): r for r in checker.collect_image_refs(CONFIG, conf)}
    assert set(refs) == {("template", "rocky"), ("template", "bare"), ("template", "oci"),
                         ("iso", "live"), ("iso", "omni")}
    assert refs[("template", "bare")].uri == "https://mirror.example.org/bare.img"
    # an all-zero checksum marks a stand-in the operator replaces
    assert refs[("iso", "omni")].placeholder
    assert not refs[("iso", "live")].placeholder
    assert refs[("template", "rocky")].box == "some-box"
    assert refs[("template", "rocky")].conf_path == os.path.join("boxes", "some-box", "conf.yml")


def test_check_refs_probes_each_url_once_and_never_a_placeholder():
    conf = os.path.join(_REPO, "boxes", "some-box", "conf.yml")
    refs = checker.collect_image_refs(CONFIG, conf)
    refs += checker.collect_image_refs(CONFIG, conf)          # every url twice
    transport = FakeTransport({
        ("HEAD", URL): 200,
        ("HEAD", "https://mirror.example.org/bare.img"): 200,
        ("HEAD", "https://mirror.example.org/live.iso"): 404,
        ("GET", "https://mirror.example.org/live.iso"): 404,
    })
    results = checker.check_refs(refs, opener=checker.build_opener(transport))

    assert [ref for ref, _ in results] == refs
    states = {(ref.kind, ref.name): status.state for ref, status in results}
    assert states == {
        ("template", "rocky"): checker.OK, ("template", "bare"): checker.OK,
        ("template", "oci"): checker.SKIPPED, ("iso", "live"): checker.GONE,
        ("iso", "omni"): checker.SKIPPED,
    }
    heads = [url for method, url, _ in transport.seen if method == "HEAD"]
    assert sorted(heads) == sorted(set(heads))


def test_describe_dead_says_where_and_what_to_update():
    ref = checker.ImageRef("some-box", "boxes/some-box/conf.yml", "template", "rocky", URL)
    line = checker.describe_dead(ref, checker.UrlStatus(checker.GONE, "404 Not Found", 404))
    assert "template 'rocky'" in line and "boxes/some-box/conf.yml" in line
    assert "404" in line and "image.uri and image.checksum" in line and URL in line

    unreachable = checker.describe_dead(ref, checker.UrlStatus(checker.UNREACHABLE, "timed out"))
    assert "unreachable (timed out)" in unreachable


# --------------------------------------------------------------------------- #
# discovery, rendering, main                                                  #
# --------------------------------------------------------------------------- #
def test_discovery_includes_nested_projects_and_skips_runtime_dirs(tmp_path):
    for rel in ("flat/conf.yml", "multi/projects/a/conf.yml", "flat/.boxman/conf.yml"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    found = [os.path.relpath(p, tmp_path) for p in checker.discover_box_configs(str(tmp_path))]
    assert found == [os.path.join("flat", "conf.yml"),
                     os.path.join("multi", "projects", "a", "conf.yml")]


def test_render_points_boxman_conf_dir_at_each_box_in_turn(tmp_path, monkeypatch):
    monkeypatch.delenv("BOXMAN_CONF_DIR", raising=False)
    confs = []
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        conf = tmp_path / name / "conf.yml"
        conf.write_text('dir: {{ env("BOXMAN_CONF_DIR") }}\n')
        confs.append(conf)
    assert [checker.render_box_config(str(c))["dir"] for c in confs] == [
        str(tmp_path / "one"), str(tmp_path / "two")]
    assert "BOXMAN_CONF_DIR" not in os.environ


def test_every_shipped_box_renders_and_declares_its_images():
    # the default sweep renders every box; a box that stops rendering would
    # otherwise only show up when someone runs the check
    confs = checker.discover_box_configs()
    refs = []
    for conf in confs:
        refs += checker.collect_image_refs(checker.render_box_config(conf), conf)
    assert any(r.box.endswith(os.path.join("projects", "rocky9")) for r in refs)
    assert {r.kind for r in refs} == {"template", "iso"}


def _write_box(root, name, uri):
    box = root / name
    box.mkdir()
    (box / "conf.yml").write_text(
        f"templates:\n  t1:\n    image:\n      uri: {uri}\n"
        "      checksum: sha256:{{ 'ab' * 32 }}\n")
    return str(box)


def test_main_exits_nonzero_and_reports_a_dead_image(tmp_path, capsys):
    live = _write_box(tmp_path, "live-box", URL)
    dead = _write_box(tmp_path, "dead-box", MOVED)
    transport = FakeTransport({("HEAD", URL): 200, ("HEAD", MOVED): 404, ("GET", MOVED): 404})
    code = checker.main([live, dead], opener=checker.build_opener(transport))
    out = capsys.readouterr().out
    assert code == 1
    assert f"template 't1' in {os.path.join(dead, 'conf.yml')} returns 404" in out
    assert "1 ok, 1 gone" in out


def test_main_exits_zero_when_every_image_is_live(tmp_path, capsys):
    live = _write_box(tmp_path, "live-box", URL)
    transport = FakeTransport({("HEAD", URL): 200})
    assert checker.main([live], opener=checker.build_opener(transport)) == 0
    assert "dead image URLs" not in capsys.readouterr().out


def test_main_exits_nonzero_on_a_config_that_does_not_render(tmp_path, capsys):
    box = tmp_path / "broken"
    box.mkdir()
    (box / "conf.yml").write_text("templates: {{ unclosed\n")
    assert checker.main([str(box)], opener=checker.build_opener(FakeTransport({}))) == 1
    assert "failed to render" in capsys.readouterr().out
