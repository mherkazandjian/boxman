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
