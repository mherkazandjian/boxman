"""
#164 F1 — paths and URIs interpolated into shell commands must stay inert.

These commands run through a shell. ``$(…)`` inside *double* quotes is still
evaluated by it, and single-quote concatenation (``'{path}'``) breaks the
moment a path contains an apostrophe. That is survivable while every value
comes from the project's own config; it stops being survivable when a value
comes from a remote manifest, which is exactly what F1a introduces.

The assertion throughout is the same: parse the command the way a shell would
and require the dangerous value to come back as one argument, unchanged.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.import_image import ImageImporter
from boxman.utils.http_download import download_url

pytestmark = pytest.mark.unit

#: A command substitution, and an apostrophe. The apostrophe matters on its
#: own: it is what defeats ``'{path}'`` concatenation, which ``$(…)`` alone
#: would not expose.
#: no slash, so it is also a legal filename component
SUBSTITUTION = "$(id)"
APOSTROPHE = "it's"


def _result(ok=True, stdout="", stderr=""):
    r = MagicMock(name="invoke.Result")
    r.ok = ok
    r.failed = not ok
    r.stdout = stdout
    r.stderr = stderr
    r.return_code = 0 if ok else 1
    return r


def _args_of(command: str) -> list[str]:
    """The argv a shell would build from *command*."""
    return shlex.split(command)


class TestDownloadUrl:

    @pytest.mark.parametrize("payload", [SUBSTITUTION, APOSTROPHE])
    def test_a_hostile_url_is_passed_as_one_literal_argument(self, payload):
        url = f"https://example.invalid/{payload}.iso"
        with patch("boxman.utils.http_download._shell_run",
                   return_value=_result(ok=False)) as run_fn:
            with patch("boxman.utils.http_download.urllib.request.urlopen",
                       side_effect=OSError("no network")):
                download_url(url, "/tmp/dst.iso")

        for call in run_fn.call_args_list:
            assert url in _args_of(call.args[0]), (
                f"URL not passed literally: {call.args[0]}")

    @pytest.mark.parametrize("payload", [SUBSTITUTION, APOSTROPHE])
    def test_a_hostile_destination_is_passed_as_one_literal_argument(
            self, payload):
        dst = f"/tmp/{payload}.iso"
        with patch("boxman.utils.http_download._shell_run",
                   return_value=_result(ok=False)) as run_fn:
            with patch("boxman.utils.http_download.urllib.request.urlopen",
                       side_effect=OSError("no network")):
                download_url("https://example.invalid/x.iso", dst)

        for call in run_fn.call_args_list:
            assert dst in _args_of(call.args[0]), (
                f"destination not passed literally: {call.args[0]}")


class TestImporterShellCalls:

    def _importer(self, uri="qemu:///system"):
        return ImageImporter(uri=uri)

    @pytest.mark.parametrize("payload", [SUBSTITUTION, APOSTROPHE])
    def test_copy_passes_both_paths_literally(self, payload):
        src = f"/src/{payload}.qcow2"
        dst = f"/dst/{payload}.qcow2"
        with patch("boxman.providers.libvirt.import_image.run",
                   return_value=_result()) as run_fn:
            self._importer().copy_disk_image_sparse(src, dst)

        args = _args_of(run_fn.call_args.args[0])
        assert src in args
        assert dst in args

    @pytest.mark.parametrize("payload", [SUBSTITUTION, APOSTROPHE])
    def test_define_passes_an_xml_path_literally(self, payload, tmp_path: Path):
        """
        The XML path is derived from the **VM name**, which comes from the
        manifest's XML rather than from boxman — so it is its own untrusted
        input, not a variant of the disk basename cases.
        """
        xml_path = str(tmp_path / f"{payload}.xml")
        with patch("boxman.providers.libvirt.import_image.run",
                   return_value=_result()) as run_fn:
            self._importer().define_vm(xml_path)

        assert xml_path in _args_of(run_fn.call_args.args[0])

    @pytest.mark.parametrize("payload", [SUBSTITUTION, APOSTROPHE])
    def test_the_connection_uri_is_passed_literally(self, payload):
        uri = f"qemu+ssh://{payload}/system"
        with patch("boxman.providers.libvirt.import_image.run",
                   return_value=_result(stdout="")) as run_fn:
            self._importer(uri=uri).check_vm_exists("anything")

        assert uri in _args_of(run_fn.call_args.args[0])


class TestChecksumCommands:
    """
    ``sha256sum '{path}'`` was concatenation, not quoting: an apostrophe in
    the path closes the quote and the remainder is parsed as shell.
    """

    @pytest.mark.parametrize("payload", [SUBSTITUTION, APOSTROPHE])
    def test_checksum_paths_are_passed_literally(self, payload, tmp_path: Path):
        import boxman.providers.libvirt.import_image as mod

        nasty = tmp_path / f"{payload}.qcow2"
        nasty.write_bytes(b"x")
        seen = []

        def _fake_run(cmd, *args, **kwargs):
            seen.append(cmd)
            return _result(stdout="deadbeef  -\n")

        with patch.object(mod, "run", side_effect=_fake_run):
            mod.run(f"sha256sum {shlex.quote(str(nasty))}", hide=True)

        assert str(nasty) in _args_of(seen[0])


# ---------------------------------------------------------------------------
# The assertions above parse the command with shlex.split, which does NOT
# execute command substitution -- so a badly quoted command can keep a
# literal $(...) and pass them (#164 F1 review, finding 9). What follows
# runs the production code through a real shell against stub executables,
# where an unsafe command actually fires.
# ---------------------------------------------------------------------------

import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

#: slash-free, so it is a legal filename component, and it has a side
#: effect a shell would leave behind: a file named `pwned` in the cwd.
LIVE_PAYLOAD = "a$(touch pwned)b"


def _stub_bin(directory: Path, names: list[str]) -> Path:
    """Put argv-recording stubs for *names* on a fresh PATH directory."""
    bindir = directory / "bin"
    bindir.mkdir()
    log = directory / "argv.log"
    for name in names:
        stub = bindir / name
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"open({str(log)!r}, 'a').write(chr(31).join(sys.argv) + chr(10))\n"
            # sha256sum's caller parses stdout; give it a plausible line
            "if sys.argv[0].endswith('sha256sum'):\n"
            "    print('0' * 64 + '  ' + (sys.argv[-1] if len(sys.argv) > 1 else ''))\n"
            # download_url only accepts a downloader's success if the
            # destination exists and is non-empty -- a stub that merely
            # exits 0 lets it fall through wget, curl and on to real
            # urllib (#164 F1 review round 3, finding 7)
            "if sys.argv[0].rstrip('0123456789.').endswith(('wget', 'curl')):\n"
            "    dst = None\n"
            "    for flag in ('-O', '-o'):\n"
            "        if flag in sys.argv:\n"
            "            dst = sys.argv[sys.argv.index(flag) + 1]\n"
            "    if dst:\n"
            "        open(dst, 'wb').write(b'stub-payload')\n"
            "sys.exit(0)\n"
        )
        stub.chmod(0o755)
    return bindir


def _recorded(directory: Path) -> list[list[str]]:
    log = directory / "argv.log"
    if not log.exists():
        return []
    return [line.split(chr(31))
            for line in log.read_text().splitlines() if line]


class TestTheShellActuallyRunsThem:
    """Production commands, a real shell, and stubs that record argv."""

    def _run_in(self, workdir: Path, body: str) -> subprocess.CompletedProcess:
        """Run *body* in a subprocess whose cwd and PATH we control."""
        bindir = _stub_bin(workdir, ["wget", "curl", "sha256sum", "rsync"])
        env = dict(os.environ)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        # urllib is blocked in every downloader subprocess, not only in
        # the dedicated guard test -- otherwise a stub that fails lets the
        # real fallback run behind a passing assertion (#164 F1 review 4).
        preamble = (
            "import urllib.request\n"
            "urllib.request.urlopen = lambda *a, **k: ("
            "_ for _ in ()).throw(RuntimeError('urllib blocked in test'))\n"
        )
        return subprocess.run(
            [sys.executable, "-c", preamble + body],
            cwd=workdir, env=env, capture_output=True, text=True, timeout=120)

    def test_download_url_does_not_execute_a_hostile_url(self, tmp_path: Path):
        url = f"https://example.invalid/{LIVE_PAYLOAD}.iso"
        proc = self._run_in(tmp_path, (
            "from boxman.utils.http_download import download_url\n"
            f"download_url({url!r}, 'dst.iso')\n"
        ))

        assert proc.returncode == 0, proc.stderr
        assert not (tmp_path / "pwned").exists(), (
            "the shell executed $(touch pwned) from the url")
        calls = _recorded(tmp_path)
        assert calls, "no downloader ran"
        assert any(url in call for call in calls), (
            f"the url did not survive as one literal argument: {calls}")

    def test_checksums_do_not_execute_a_hostile_path(self, tmp_path: Path):
        """The apostrophe case: `'{path}'` was concatenation, not quoting."""
        target = tmp_path / f"{LIVE_PAYLOAD}-it's.qcow2"
        target.write_bytes(b"x")
        proc = self._run_in(tmp_path, (
            "from boxman.providers.libvirt.import_image import ImageImporter\n"
            f"print(ImageImporter._sha256({str(target)!r}))\n"
        ))

        assert proc.returncode == 0, proc.stderr
        assert not (tmp_path / "pwned").exists(), (
            "the shell executed $(touch pwned) from the path")
        calls = _recorded(tmp_path)
        assert any(str(target) in call for call in calls), (
            f"the path did not survive as one literal argument: {calls}")

    def test_sparse_copy_does_not_execute_hostile_paths(self, tmp_path: Path):
        src = tmp_path / f"src-{LIVE_PAYLOAD}.qcow2"
        src.write_bytes(b"x")
        dst = tmp_path / "dst-it's.qcow2"
        proc = self._run_in(tmp_path, (
            "from boxman.providers.libvirt.import_image import ImageImporter\n"
            "imp = ImageImporter(uri='qemu:///system')\n"
            f"imp.copy_disk_image_sparse({str(src)!r}, {str(dst)!r})\n"
        ))

        assert proc.returncode == 0, proc.stderr
        assert not (tmp_path / "pwned").exists(), (
            "the shell executed $(touch pwned) from a disk path")
        calls = _recorded(tmp_path)
        assert any(str(src) in call and str(dst) in call for call in calls), (
            f"the paths did not survive as literal arguments: {calls}")


class TestVirshCommandsThroughARealShell:
    """The agreed additions: a virsh stub, and a blocked urllib fallback.

    The URI and definition-path cases were still ``shlex.split`` assertions,
    which cannot execute substitution, and the downloader test fell through
    into real urllib (#164 F1 review round 2, finding 11).
    """

    def _run_in(self, workdir: Path, body: str) -> subprocess.CompletedProcess:
        bindir = _stub_bin(workdir, ["virsh", "wget", "curl", "sha256sum"])
        env = dict(os.environ)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        # urllib is blocked in every downloader subprocess, not only in
        # the dedicated guard test -- otherwise a stub that fails lets the
        # real fallback run behind a passing assertion (#164 F1 review 4).
        preamble = (
            "import urllib.request\n"
            "urllib.request.urlopen = lambda *a, **k: ("
            "_ for _ in ()).throw(RuntimeError('urllib blocked in test'))\n"
        )
        return subprocess.run(
            [sys.executable, "-c", preamble + body],
            cwd=workdir, env=env, capture_output=True, text=True, timeout=120)

    def test_a_hostile_connection_uri_is_one_literal_argument(self, tmp_path):
        uri = f"qemu+ssh://{LIVE_PAYLOAD}/system"
        proc = self._run_in(tmp_path, (
            "from boxman.providers.libvirt.import_image import ImageImporter\n"
            f"ImageImporter(uri={uri!r}).check_vm_exists('node01')\n"
        ))

        assert proc.returncode == 0, proc.stderr
        assert not (tmp_path / "pwned").exists(), (
            "the shell executed $(touch pwned) from the connection uri")
        assert any(uri in call for call in _recorded(tmp_path)), _recorded(tmp_path)

    def test_a_hostile_definition_path_is_one_literal_argument(self, tmp_path):
        xml = tmp_path / f"{LIVE_PAYLOAD}-it's.xml"
        xml.write_text("<domain/>")
        proc = self._run_in(tmp_path, (
            "from boxman.providers.libvirt.import_image import ImageImporter\n"
            "imp = ImageImporter(uri='qemu:///system')\n"
            f"imp.define_vm({str(xml)!r})\n"
        ))

        assert proc.returncode == 0, proc.stderr
        assert not (tmp_path / "pwned").exists(), (
            "the shell executed $(touch pwned) from the xml path")
        assert any(str(xml) in call for call in _recorded(tmp_path))

    def test_a_hostile_download_destination_is_one_literal_argument(self, tmp_path):
        dst = f"dst-{LIVE_PAYLOAD}-it's.iso"
        proc = self._run_in(tmp_path, (
            "from boxman.utils.http_download import download_url\n"
            f"download_url('https://example.invalid/x.iso', {dst!r})\n"
        ))

        assert proc.returncode == 0, proc.stderr
        assert not (tmp_path / "pwned").exists(), (
            "the shell executed $(touch pwned) from the destination path")
        assert any(dst in call for call in _recorded(tmp_path))

    def test_the_urllib_fallback_is_never_reached(self, tmp_path):
        """The stubs write a real destination, so wget succeeds.

        Asserted on a marker the subprocess prints, not on stderr:
        ``download_url`` catches every exception and logs it, so an
        AssertionError raised from a patched ``urlopen`` was swallowed and
        the test passed even when the forbidden fallback ran (#164 F1
        review round 3, finding 7).
        """
        proc = self._run_in(tmp_path, (
            "import urllib.request\n"
            "reached = []\n"
            "urllib.request.urlopen = lambda *a, **k: reached.append(1)\n"
            "from boxman.utils.http_download import download_url\n"
            "ok = download_url('https://example.invalid/x.iso', 'dst.iso')\n"
            "print('RESULT', ok, 'URLLIB', bool(reached))\n"
        ))

        assert proc.returncode == 0, proc.stderr
        assert 'RESULT True URLLIB False' in proc.stdout, proc.stdout


class TestTheCurlFallbackIsExercised:
    """wget succeeding would otherwise hide curl's shell handling.

    The stub downloaders now write a destination, so wget always wins and
    curl is never reached — its quoting would go untested (#164 F1 review
    round 4, non-blocking follow-up).
    """

    def _run_with_failing_wget(self, workdir: Path, body: str):
        bindir = _stub_bin(workdir, ["curl", "sha256sum"])
        # a wget that records its argv and then fails
        wget = bindir / "wget"
        log = workdir / "argv.log"
        wget.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"open({str(log)!r}, 'a').write(chr(31).join(sys.argv) + chr(10))\n"
            "sys.exit(1)\n"
        )
        wget.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        preamble = (
            "import urllib.request\n"
            "urllib.request.urlopen = lambda *a, **k: ("
            "_ for _ in ()).throw(RuntimeError('urllib blocked in test'))\n"
        )
        return subprocess.run(
            [sys.executable, "-c", preamble + body],
            cwd=workdir, env=env, capture_output=True, text=True, timeout=120)

    def test_curl_receives_a_hostile_url_literally(self, tmp_path: Path):
        url = f"https://example.invalid/{LIVE_PAYLOAD}.iso"
        proc = self._run_with_failing_wget(tmp_path, (
            "from boxman.utils.http_download import download_url\n"
            f"ok = download_url({url!r}, 'dst.iso')\n"
            "print('RESULT', ok)\n"
        ))

        assert proc.returncode == 0, proc.stderr
        assert not (tmp_path / "pwned").exists(), (
            "the shell executed $(touch pwned) from the url via curl")
        calls = _recorded(tmp_path)
        # reachability: both downloaders ran, so curl really was exercised
        assert any(c[0].endswith('wget') for c in calls), calls
        assert any(c[0].endswith('curl') for c in calls), calls
        assert any(url in c for c in calls if c[0].endswith('curl')), calls
        assert 'RESULT True' in proc.stdout, proc.stdout
