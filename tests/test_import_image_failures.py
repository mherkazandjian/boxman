"""
Failure-path tests for ``boxman import-image`` (#164 F1).

Two layers, deliberately:

* :class:`TestImporterRaises` drives ``ImageImporter.import_image()``
  directly and pins that each failure raises ``ImageImportError``.
* :class:`TestImportImageExitsTwo` drives ``app.main()`` end to end and
  pins that the raise survives to **exit 2**.

The second layer is the one that matters. Before this change the importer
already signalled every one of these failures -- it returned ``False`` --
and :meth:`LibVirtSession.import_image` discarded it, so a failed import
printed an error and exited **0**. A unit test alone would not have caught
that, because the unit was never the broken part.
"""

import json
import shlex
import shutil as _shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from boxman.exceptions import ImageImportError
from boxman.providers.libvirt.import_image import ImageImporter
from boxman.scripts.app import main

SAMPLE_VM_XML = """<?xml version='1.0' encoding='utf-8'?>
<domain type='kvm'>
  <name>packaged-vm</name>
  <uuid>11111111-2222-3333-4444-555555555555</uuid>
  <devices>
    <disk type='file' device='disk'>
      <source file='/original/location/disk.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
  </devices>
</domain>
"""


class _Result:
    """Minimal stand-in for an invoke Result."""

    def __init__(self, stdout: str = "", stderr: str = "", ok: bool = True):
        self.stdout = stdout
        self.stderr = stderr
        self.ok = ok
        self.failed = not ok
        self.return_code = 0 if ok else 1


def _build_package(root: Path, xml_body: str = SAMPLE_VM_XML,
                   xml_name: str = "vm-definition.xml",
                   image_name: str = "disk.qcow2",
                   provider: str = "libvirt",
                   write_xml: bool = True,
                   write_image: bool = True) -> Path:
    """Lay out a manifest + xml + disk package; return the manifest path."""
    (root / "vm").mkdir(parents=True, exist_ok=True)
    if write_xml:
        (root / "vm" / xml_name).write_text(xml_body)
    if write_image:
        (root / "vm" / image_name).write_bytes(b"x" * 1024)
    manifest = {
        "xml_path": f"vm/{xml_name}",
        "image_path": f"vm/{image_name}",
        "provider": provider,
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root / "manifest.json"


def _fake_run(existing_vms: str = "other-vm\n", define_ok: bool = True,
              src_hash: str = "dead", dst_hash: str = "dead",
              truncate_copy: bool = False):
    """Build a ``run`` double that dispatches on the command shape."""

    def run(cmd, *args, **kwargs):
        if cmd.startswith("virsh") and "list --all --name" in cmd:
            return _Result(stdout=existing_vms, ok=True)
        if cmd.startswith("rsync"):
            parts = shlex.split(cmd)
            _shutil.copyfile(parts[3], parts[4])
            if truncate_copy:
                with open(parts[4], "r+b") as fobj:
                    fobj.truncate(16)
            return _Result(ok=True)
        if cmd.startswith("sha256sum"):
            parts = shlex.split(cmd)
            # the source is read first; key off the path so a mismatch can
            # be simulated without depending on call order
            which = src_hash if "/vm/" in parts[1] else dst_hash
            return _Result(stdout=f"{which}  {parts[1]}\n", ok=True)
        if cmd.startswith("virsh") and "define" in cmd:
            return _Result(stderr="" if define_ok else "error: bad xml",
                           ok=define_ok)
        raise AssertionError(f"unexpected command: {cmd}")

    return run


@pytest.mark.unit
class TestImporterRaises:
    """Every failure path raises rather than returning ``False``."""

    def _importer(self, manifest_path: Path, dst: Path, **kwargs):
        return ImageImporter(
            manifest_path=str(manifest_path),
            uri="qemu:///system",
            disk_dir=str(dst),
            **kwargs,
        )

    def test_unreadable_manifest_raises(self, tmp_path: Path):
        importer = self._importer(tmp_path / "nope.json", tmp_path / "dst",
                                  vm_name="vm1")
        with pytest.raises(ImageImportError, match="manifest"):
            importer.import_image()

    def test_unreadable_xml_raises(self, tmp_path: Path):
        manifest = _build_package(tmp_path / "pkg", write_xml=False)
        importer = self._importer(manifest, tmp_path / "dst", vm_name="vm1")
        with pytest.raises(ImageImportError, match="vm definition xml"):
            importer.import_image()

    def test_unreadable_xml_copies_nothing(self, tmp_path: Path):
        """The XML is validated before the destination is touched.

        This used to surface as a ``FileNotFoundError`` from ``shutil.copy2``
        at the very end -- after the vm directory had been created and the
        whole disk image copied into it.
        """
        manifest = _build_package(tmp_path / "pkg", write_xml=False)
        dst = tmp_path / "dst"
        importer = self._importer(manifest, dst, vm_name="vm1")
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run()):
            with pytest.raises(ImageImportError):
                importer.import_image()
        assert not (dst / "vm1").exists()

    def test_malformed_xml_raises(self, tmp_path: Path):
        manifest = _build_package(tmp_path / "pkg", xml_body="<domain><oops>")
        importer = self._importer(manifest, tmp_path / "dst", vm_name="vm1")
        with pytest.raises(ImageImportError, match="vm definition xml"):
            importer.import_image()

    def test_no_name_anywhere_raises(self, tmp_path: Path):
        """No ``--name`` and no ``/domain/name`` in the XML."""
        manifest = _build_package(
            tmp_path / "pkg",
            xml_body="<domain type='kvm'><uuid>u</uuid></domain>")
        importer = self._importer(manifest, tmp_path / "dst")
        with pytest.raises(ImageImportError, match="no vm name"):
            importer.import_image()

    def test_existing_vm_without_force_raises(self, tmp_path: Path):
        manifest = _build_package(tmp_path / "pkg")
        importer = self._importer(manifest, tmp_path / "dst", vm_name="dup")
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run(existing_vms="dup\nother\n")):
            with pytest.raises(ImageImportError, match="already exists"):
                importer.import_image()
        assert not (tmp_path / "dst" / "dup").exists()

    def test_existing_vm_directory_raises(self, tmp_path: Path):
        manifest = _build_package(tmp_path / "pkg")
        dst = tmp_path / "dst"
        (dst / "vm1").mkdir(parents=True)
        importer = self._importer(manifest, dst, vm_name="vm1")
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run()):
            with pytest.raises(ImageImportError, match="already exists"):
                importer.import_image()

    def test_missing_disk_image_raises(self, tmp_path: Path):
        manifest = _build_package(tmp_path / "pkg", write_image=False)
        importer = self._importer(manifest, tmp_path / "dst", vm_name="vm1")
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run()):
            with pytest.raises(ImageImportError, match="disk image file not found"):
                importer.import_image()

    def test_size_mismatch_raises(self, tmp_path: Path):
        manifest = _build_package(tmp_path / "pkg")
        importer = self._importer(manifest, tmp_path / "dst", vm_name="vm1")
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run(truncate_copy=True)):
            with pytest.raises(ImageImportError, match="size mismatch"):
                importer.import_image()

    def test_checksum_mismatch_raises(self, tmp_path: Path):
        manifest = _build_package(tmp_path / "pkg")
        importer = self._importer(manifest, tmp_path / "dst", vm_name="vm1")
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run(src_hash="aaaa", dst_hash="bbbb")):
            with pytest.raises(ImageImportError, match="checksum mismatch"):
                importer.import_image()

    def test_failed_define_raises(self, tmp_path: Path):
        manifest = _build_package(tmp_path / "pkg")
        importer = self._importer(manifest, tmp_path / "dst", vm_name="vm1")
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run(define_ok=False)):
            with pytest.raises(ImageImportError, match="refused to define"):
                importer.import_image()

    def test_happy_path_returns_none(self, tmp_path: Path):
        manifest = _build_package(tmp_path / "pkg")
        dst = tmp_path / "dst"
        importer = self._importer(manifest, dst, vm_name="vm1")
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run()):
            assert importer.import_image() is None
        assert (dst / "vm1" / "disk.qcow2").exists()
        assert (dst / "vm1" / "vm1.xml").exists()


def _run_cli(argv: list[str]) -> int:
    """Invoke ``main()`` with *argv* and return the exit code."""
    with patch.object(sys, "argv", ["boxman"] + argv):
        try:
            main()
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
    return 0


def _boxman_conf(tmp_path: Path, body: str = "providers:\n  libvirt:\n    uri: qemu:///system\n") -> Path:
    path = tmp_path / "boxman.yml"
    path.write_text(body)
    return path


@pytest.mark.smoke
class TestImportImageExitsTwo:
    """The CLI half: a failed import must never exit 0.

    ``LibVirtSession.import_image`` discarded the importer's ``False``, so
    every one of these ended the process with a success status.
    """

    def _argv(self, manifest: Path, conf: Path, dst: Path,
              name: str = "vm1", provider: str | None = None) -> list[str]:
        argv = [
            "--boxman-conf", str(conf),
            "import-image",
            "--uri", f"file://{manifest}",
            "--name", name,
            "--directory", str(dst),
        ]
        if provider:
            argv += ["--provider", provider]
        return argv

    def test_existing_vm_directory_exits_2(self, tmp_path: Path):
        manifest = _build_package(tmp_path / "pkg")
        dst = tmp_path / "dst"
        (dst / "vm1").mkdir(parents=True)
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run()):
            code = _run_cli(self._argv(manifest, _boxman_conf(tmp_path), dst))
        assert code == 2

    def test_failed_define_exits_2(self, tmp_path: Path):
        """The last step fails after everything else succeeded."""
        manifest = _build_package(tmp_path / "pkg")
        dst = tmp_path / "dst"
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run(define_ok=False)):
            code = _run_cli(self._argv(manifest, _boxman_conf(tmp_path), dst))
        assert code == 2

    def test_failed_define_exits_2_with_explicit_provider(self, tmp_path: Path):
        """``--provider libvirt`` skips the manifest pre-fetch: same result."""
        manifest = _build_package(tmp_path / "pkg")
        dst = tmp_path / "dst"
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run(define_ok=False)):
            code = _run_cli(self._argv(manifest, _boxman_conf(tmp_path), dst,
                                       provider="libvirt"))
        assert code == 2

    def test_checksum_mismatch_exits_2(self, tmp_path: Path):
        manifest = _build_package(tmp_path / "pkg")
        dst = tmp_path / "dst"
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run(src_hash="aaaa", dst_hash="bbbb")):
            code = _run_cli(self._argv(manifest, _boxman_conf(tmp_path), dst))
        assert code == 2

    def test_successful_import_exits_0(self, tmp_path: Path):
        """The other half of the contract: success is still success."""
        manifest = _build_package(tmp_path / "pkg")
        dst = tmp_path / "dst"
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run()):
            code = _run_cli(self._argv(manifest, _boxman_conf(tmp_path), dst))
        assert code == 0
        assert (dst / "vm1" / "disk.qcow2").exists()


@pytest.mark.smoke
class TestProviderLookupIsSafe:
    """The provider name reaches the registry and boxman.yml intact.

    Manifest validation compares ``provider.lower()`` against the supported
    set, so a manifest may legally spell it ``LibVirt``; the original
    spelling was then used as a dict key against both ``boxman.yml`` and
    the provider registry, and raised a bare ``KeyError`` (exit 1, with a
    traceback).
    """

    def _argv(self, manifest: Path, conf: Path, dst: Path) -> list[str]:
        return [
            "--boxman-conf", str(conf),
            "import-image",
            "--uri", f"file://{manifest}",
            "--name", "vm1",
            "--directory", str(dst),
        ]

    @pytest.mark.parametrize("spelling", ["libvirt", "LibVirt", "LIBVIRT", " libvirt "])
    def test_manifest_provider_case_is_normalised(self, tmp_path: Path, spelling: str):
        manifest = _build_package(tmp_path / "pkg", provider=spelling)
        dst = tmp_path / "dst"
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run()):
            code = _run_cli(self._argv(manifest, _boxman_conf(tmp_path), dst))
        assert code == 0
        assert (dst / "vm1" / "disk.qcow2").exists()

    def test_boxman_conf_without_providers_section(self, tmp_path: Path):
        """A boxman.yml with no ``providers:`` at all must not KeyError."""
        conf = _boxman_conf(tmp_path, body="runtime: local\n")
        manifest = _build_package(tmp_path / "pkg")
        dst = tmp_path / "dst"
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=_fake_run()):
            code = _run_cli(self._argv(manifest, conf, dst))
        assert code == 0

    def test_provider_block_without_uri_uses_default(self, tmp_path: Path):
        """A libvirt block that omits ``uri`` falls back to qemu:///system.

        ``manager.config['uri']`` raised ``KeyError`` here.
        """
        conf = _boxman_conf(tmp_path, body="providers:\n  libvirt:\n    use_sudo: false\n")
        manifest = _build_package(tmp_path / "pkg")
        dst = tmp_path / "dst"
        fake = _fake_run()
        with patch("boxman.providers.libvirt.import_image.run",
                   side_effect=fake) as run_fn:
            code = _run_cli(self._argv(manifest, conf, dst))
        assert code == 0
        issued = [c.args[0] for c in run_fn.call_args_list]
        assert any("virsh -c qemu:///system" in c for c in issued)
