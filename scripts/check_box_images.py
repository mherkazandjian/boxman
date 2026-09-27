#!/usr/bin/env python3
"""
Check that every box's downloadable base image still exists upstream.

The example boxes pin point-release cloud images and ISOs by URL and
checksum. Distribution mirrors delete a point release's images once it is
superseded (Rocky does it on every minor release), and a deleted image only
surfaces much later, as a download failure deep inside ``create-templates``.
This finds it in seconds instead: it renders every ``boxes/**/conf.yml`` with
boxman's Jinja2 helpers, collects each template ``image.uri`` and each
``isos:`` ``uri``, and probes every http(s) one without downloading it.

A probe is a HEAD request that follows redirects. When the server answers
HEAD with an error status, or advertises an empty image (204/205 or
``Content-Length: 0``), a one-byte ranged GET decides instead: some servers
reject HEAD outright or answer it inaccurately, and a wrong "dead" verdict
costs someone a red CI run for nothing.

Verdicts:
  ok           the image is there
  gone         404/410 -- the mirror removed it; update the uri and checksum
  unreachable  DNS failure, timeout, refused connection, TLS failure
  error        any other error status (403, 5xx, ...), a malformed url, or
               an image the ranged GET confirmed empty
  skipped      not http(s) (``oci://``, local paths), or an operator-supplied
               placeholder (an all-zero checksum, or "placeholder" in the uri)

Usage:
    make check-box-images [boxes="boxes/<box> ..."]
    python scripts/check_box_images.py [--timeout SECONDS] [BOX_DIR_OR_CONF ...]

Exit status: 0 when every probed URL is ok; 1 when any is gone, unreachable
or answers with an error, or when a box config cannot be rendered.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import glob
import http.client
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

import jinja2
import yaml

try:
    from boxman.exceptions import BoxmanError
    from boxman.utils.jinja_env import create_jinja_env
except ImportError:  # run straight from a checkout, without PYTHONPATH=src
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
    from boxman.exceptions import BoxmanError
    from boxman.utils.jinja_env import create_jinja_env

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXES_DIR = os.path.join(REPO_DIR, "boxes")

DEFAULT_TIMEOUT = 20.0
# the agent boxman's own image downloader sends
USER_AGENT = "boxman/1.0"
_WORKERS = 8

OK = "ok"
GONE = "gone"
UNREACHABLE = "unreachable"
ERROR = "error"
SKIPPED = "skipped"
DEAD = (GONE, UNREACHABLE, ERROR)
# the detail prefix of an ERROR for an image that exists but has no bytes
EMPTY_IMAGE = "empty image"

# what a probe can raise short of an HTTP status: URLError (DNS, refused,
# TLS) and a bare TimeoutError are OSErrors; a mangled response is an
# HTTPException. HTTPError is an OSError too, so it must be caught first.
_NETWORK_ERRORS = (OSError, http.client.HTTPException)


class ImageRef(NamedTuple):
    """One downloadable image a box config declares."""

    box: str          # the box directory, relative to boxes/ when inside it
    conf_path: str    # the config, relative to the repo root when inside it
    kind: str         # "template" or "iso"
    name: str         # its key under templates: / isos:
    uri: str
    placeholder: bool = False


class UrlStatus(NamedTuple):
    """The verdict on one URL."""

    state: str
    detail: str
    code: int | None = None
    final_url: str | None = None


# ---------------------------------------------------------------------------
# collecting the URLs
# ---------------------------------------------------------------------------


def discover_box_configs(boxes_dir: str = BOXES_DIR) -> list[str]:
    """
    Every ``conf.yml`` under *boxes_dir*, nested projects included.

    Multi-project boxes keep theirs a level down
    (``libvirt-multi-project-cloudinit-ansible/projects/rocky9``), which a
    ``*/conf.yml`` glob would miss. ``.boxman/`` runtime directories are
    left out.
    """
    pattern = os.path.join(boxes_dir, "**", "conf.yml")
    return sorted(
        path for path in glob.glob(pattern, recursive=True)
        if ".boxman" not in path.split(os.sep)
    )


@contextlib.contextmanager
def _config_location_env(conf_path: str):
    """Point ``BOXMAN_CONF_FILE``/``BOXMAN_CONF_DIR`` at *conf_path* for a while."""
    values = {
        "BOXMAN_CONF_FILE": conf_path,
        "BOXMAN_CONF_DIR": os.path.dirname(conf_path),
    }
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def render_box_config(conf_path: str) -> dict:
    """
    Render *conf_path* as a Jinja2 template and parse the YAML.

    The rendering tests/test_provision_boxes.py's ``parse_box_config`` does,
    with boxman's helpers (``env()``, ``env_required()``, ...). The
    ``BOXMAN_CONF_FILE``/``BOXMAN_CONF_DIR`` variables boxman exposes to a
    config are set to *this* config for the duration of the render: one
    process renders every box here, and a first-writer-wins ``setdefault``
    would hand every later box the first one's directory.
    """
    conf_path = os.path.abspath(conf_path)
    with _config_location_env(conf_path):
        jinja_env = create_jinja_env(os.path.dirname(conf_path))
        rendered = jinja_env.get_template(os.path.basename(conf_path)).render()
    return yaml.safe_load(rendered) or {}


def display_path(path: str) -> str:
    """*path* relative to the repo root when it is inside it, else absolute."""
    path = os.path.abspath(path)
    rel = os.path.relpath(path, REPO_DIR)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return path
    return rel


def _box_label(conf_path: str) -> str:
    box_dir = os.path.dirname(os.path.abspath(conf_path))
    rel = os.path.relpath(box_dir, BOXES_DIR)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return display_path(box_dir)
    return rel


def is_placeholder(uri, checksum) -> bool:
    """
    Whether an image entry is a stand-in the operator has to replace.

    Not every image can be pinned to a public URL: ``talos-iso-boot`` boots
    an ISO generated per instance by Omni, so it ships a marker and says to
    substitute one. Such an entry has nothing to reach, and calling it dead
    would flag a box that behaves exactly as documented. Recognised markers:
    an all-zero checksum (no real artifact is pinned) and ``placeholder`` in
    the uri.
    """
    digest = str(checksum or "").split(":", 1)[-1]
    if digest and set(digest) == {"0"}:
        return True
    return "placeholder" in str(uri).lower()


def collect_image_refs(config: dict, conf_path: str) -> list[ImageRef]:
    """
    Every downloadable image *config* declares: template images and isos.

    Takes an already-rendered config so a caller that holds one (the
    provisioning tier) does not render it twice. A cluster or VM
    ``base_image: oci://...`` is not collected: boxman only expands OCI
    references there, and those are skipped anyway.
    """
    if not isinstance(config, dict):
        return []
    box = _box_label(conf_path)
    shown = display_path(conf_path)
    refs: list[ImageRef] = []

    templates = config.get("templates") or {}
    if isinstance(templates, dict):
        for key, tpl in templates.items():
            if not isinstance(tpl, dict):
                continue
            image = tpl.get("image")
            # boxman takes both `image: {uri:, checksum:}` and a bare string
            if isinstance(image, dict):
                uri, checksum = image.get("uri"), image.get("checksum")
            else:
                uri, checksum = image, None
            if uri:
                refs.append(ImageRef(box, shown, "template", str(key), str(uri),
                                     is_placeholder(uri, checksum)))

    isos = config.get("isos") or {}
    if isinstance(isos, dict):
        for name, spec in isos.items():
            if isinstance(spec, dict) and spec.get("uri"):
                refs.append(ImageRef(box, shown, "iso", str(name), str(spec["uri"]),
                                     is_placeholder(spec["uri"], spec.get("checksum"))))
    return refs


# ---------------------------------------------------------------------------
# probing them
# ---------------------------------------------------------------------------


class _ProbeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """
    Follow redirects without turning a HEAD into a GET or leaking proxy credentials.

    urllib re-issues a redirected request as a plain GET, so a HEAD that hits
    a redirecting mirror would start streaming a multi-gigabyte image. Keep
    the method; the other headers (``Range`` included) are carried over by
    urllib already.

    That carry-over includes ``Proxy-Authorization``, which urllib's
    ProxyHandler adds as an ordinary header and keeps off the wire only when
    tunnelling. A redirect from a proxied http mirror to a host reached
    directly (an https CDN with no https proxy set, or a ``no_proxy`` host)
    would hand that host the proxy password. Drop it from every redirected
    request; the proxy handler adds it back when the destination is proxied.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if req.get_method() == "HEAD":
            new.method = "HEAD"
        for hdrs in (new.headers, new.unredirected_hdrs):
            for key in [k for k in hdrs if k.lower() == "proxy-authorization"]:
                del hdrs[key]
        return new


def build_opener(*handlers) -> urllib.request.OpenerDirector:
    """
    An opener that follows redirects, keeps a HEAD a HEAD, and does not
    carry proxy credentials across a redirect.

    Extra *handlers* join the chain; the unit tests put a fake transport in
    front of the network that way.
    """
    return urllib.request.build_opener(_ProbeRedirectHandler, *handlers)


def is_http(uri: str) -> bool:
    return urllib.parse.urlsplit(str(uri)).scheme.lower() in ("http", "https")


def _content_length(headers) -> int | None:
    try:
        return int(headers.get("Content-Length", ""))
    except (TypeError, ValueError):
        return None


def _probe(opener, url: str, method: str, timeout: float, headers=None):
    """
    Send one request. Return its status, its url after redirects, and why the
    response shows the image to be empty, or None when it does not.

    A missing ``Content-Length`` proves nothing either way: valid responses
    may omit it. A HEAD can only advertise emptiness; a GET settles it, and
    one byte of body is enough to show the image is not empty.
    """
    request = urllib.request.Request(
        url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with opener.open(request, timeout=timeout) as response:
        status = response.status
        if status in (204, 205):
            empty = f"{status} {response.msg}".strip()
        elif _content_length(response.headers) == 0:
            empty = f"{status}, Content-Length: 0"
        elif method == "GET" and not response.read(1):
            empty = f"{status} with no body"
        else:
            empty = None
        return status, response.geturl(), empty


def _from_http_error(exc: urllib.error.HTTPError) -> UrlStatus:
    final_url = getattr(exc, "url", None) or exc.filename
    if exc.code == 416:
        # an unsatisfiable range is answered with `bytes */<length>`: the
        # first byte being out of range means the image has no bytes at all
        content_range = (exc.headers.get("Content-Range") or "") if exc.headers else ""
        if content_range.rsplit("/", 1)[-1].strip() == "0":
            return UrlStatus(ERROR, f"{EMPTY_IMAGE} (416, Content-Range: {content_range})",
                             exc.code, final_url)
    detail = f"{exc.code} {exc.reason}".strip()
    state = GONE if exc.code in (404, 410) else ERROR
    return UrlStatus(state, detail, exc.code, final_url)


def _unreachable(exc: BaseException, timeout: float) -> UrlStatus:
    # URLError wraps the socket-level cause; a read timeout arrives bare
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, TimeoutError):
        detail = f"timed out after {timeout:g}s"
    else:
        detail = str(reason) or type(reason).__name__
    return UrlStatus(UNREACHABLE, detail)


def check_url(url: str, timeout: float = DEFAULT_TIMEOUT, opener=None) -> UrlStatus:
    """Probe *url* without downloading it and return the verdict."""
    if not is_http(url):
        return UrlStatus(SKIPPED, "not an http(s) source")
    opener = opener or build_opener()
    try:
        status, final_url, empty = _probe(opener, url, "HEAD", timeout)
        if not empty:
            return UrlStatus(OK, str(status), status, final_url)
        head_said = f"HEAD answered {empty}"
    except urllib.error.HTTPError as exc:
        head_said = f"HEAD answered {exc.code}"
        exc.close()
    except _NETWORK_ERRORS as exc:
        # unreachable is unreachable; a GET would only wait out a second timeout
        return _unreachable(exc, timeout)
    except ValueError as exc:
        return UrlStatus(ERROR, f"invalid url: {exc}")

    # HEAD answered with an error status or advertised an empty image. Let a
    # one-byte ranged GET decide: some servers reject HEAD (405, 501, 403)
    # while serving GET fine, and some answer HEAD inaccurately.
    try:
        status, final_url, empty = _probe(opener, url, "GET", timeout, {"Range": "bytes=0-0"})
    except urllib.error.HTTPError as exc:
        exc.close()
        return _from_http_error(exc)
    except _NETWORK_ERRORS as exc:
        return _unreachable(exc, timeout)
    if empty:
        return UrlStatus(ERROR, f"{EMPTY_IMAGE} ({empty})", status, final_url)
    return UrlStatus(OK, f"{status} ({head_said})", status, final_url)


def check_refs(refs: list[ImageRef], timeout: float = DEFAULT_TIMEOUT,
               opener=None) -> list[tuple[ImageRef, UrlStatus]]:
    """
    Probe every ref and pair it with its verdict, in the order given.

    Each distinct URL is probed once (most boxes share one Ubuntu image), a
    few at a time. Placeholders are never probed.
    """
    opener = opener or build_opener()
    urls = sorted({ref.uri for ref in refs if not ref.placeholder})
    verdicts: dict[str, UrlStatus] = {}
    if urls:
        probe = functools.partial(check_url, timeout=timeout, opener=opener)
        with ThreadPoolExecutor(max_workers=min(_WORKERS, len(urls))) as pool:
            verdicts = dict(zip(urls, pool.map(probe, urls), strict=True))
    placeholder = UrlStatus(SKIPPED, "operator-supplied placeholder")
    return [(ref, placeholder if ref.placeholder else verdicts[ref.uri]) for ref in refs]


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def describe_dead(ref: ImageRef, status: UrlStatus) -> str:
    """One line saying which image is dead, where it is pinned, and what to do."""
    what = "base image" if ref.kind == "template" else "ISO"
    where = f"{what} URL for {ref.kind} '{ref.name}' in {ref.conf_path}"
    moved = ""
    if status.final_url and status.final_url != ref.uri:
        moved = f" (redirected to {status.final_url})"
    if status.state == GONE:
        fields = ("image.uri and image.checksum" if ref.kind == "template"
                  else f"isos.{ref.name}.uri and its checksum")
        return (f"{where} returns {status.code}{moved}: the mirror removed it; "
                f"update {fields}: {ref.uri}")
    if status.state == UNREACHABLE:
        return f"{where} is unreachable ({status.detail}): {ref.uri}"
    if status.detail.startswith(EMPTY_IMAGE):
        return f"{where} serves an {status.detail}{moved}: {ref.uri}"
    return f"{where} answers {status.detail}{moved}: {ref.uri}"


def _state_label(status: UrlStatus) -> str:
    if status.state in (GONE, ERROR) and status.code:
        return f"{status.state} {status.code}"
    return status.state


def format_report(results: list[tuple[ImageRef, UrlStatus]],
                  config_errors: list[str], n_configs: int) -> list[str]:
    """The report as lines: one row per image, then what is dead, then a summary."""
    rows = [
        (_state_label(status), ref.box, f"{ref.kind} {ref.name}", ref.uri,
         f"({status.detail})" if status.state in (SKIPPED, UNREACHABLE) else "")
        for ref, status in results
    ]
    widths = [max((len(row[i]) for row in rows), default=0) for i in range(3)]
    lines = [
        f"{state:<{widths[0]}}  {box:<{widths[1]}}  {image:<{widths[2]}}  "
        f"{uri}  {note}".rstrip()
        for state, box, image, uri, note in rows
    ]

    dead = [describe_dead(ref, status) for ref, status in results if status.state in DEAD]
    if dead:
        lines += ["", f"dead image URLs ({len(dead)}):"] + [f"  {line}" for line in dead]
    if config_errors:
        lines += ["", f"configs that failed to render ({len(config_errors)}):"]
        lines += [f"  {line}" for line in config_errors]

    counts = {state: 0 for state in (OK, GONE, UNREACHABLE, ERROR, SKIPPED)}
    for _, status in results:
        counts[status.state] += 1
    n_urls = len({ref.uri for ref, status in results if status.state != SKIPPED})
    summary = ", ".join(f"{count} {state}" for state, count in counts.items())
    lines += ["", f"{len(results)} image reference(s) in {n_configs} config(s), "
                  f"{n_urls} distinct URL(s) probed: {summary}"]
    return lines


def main(argv=None, opener=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that every box's base-image and ISO URLs still exist upstream.")
    parser.add_argument(
        "paths", nargs="*", metavar="BOX_DIR_OR_CONF",
        help="box directories or conf.yml files (default: every box under boxes/)")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help="per-request timeout in seconds (default: %(default)g)")
    args = parser.parse_args(argv)

    if args.paths:
        conf_paths = [os.path.join(p, "conf.yml") if os.path.isdir(p) else p
                      for p in args.paths]
    else:
        conf_paths = discover_box_configs()

    refs: list[ImageRef] = []
    config_errors: list[str] = []
    for conf_path in conf_paths:
        try:
            config = render_box_config(conf_path)
        except (jinja2.TemplateError, yaml.YAMLError, BoxmanError, OSError) as exc:
            config_errors.append(f"{display_path(conf_path)}: {type(exc).__name__}: {exc}")
            continue
        refs.extend(collect_image_refs(config, conf_path))

    n_urls = len({ref.uri for ref in refs if not ref.placeholder and is_http(ref.uri)})
    print(f"probing {n_urls} distinct URL(s) from {len(conf_paths)} config(s)...", flush=True)
    results = check_refs(refs, timeout=args.timeout, opener=opener)
    print("\n".join(format_report(results, config_errors, len(conf_paths))))

    dead = any(status.state in DEAD for _, status in results)
    return 1 if dead or config_errors else 0


if __name__ == "__main__":
    sys.exit(main())
