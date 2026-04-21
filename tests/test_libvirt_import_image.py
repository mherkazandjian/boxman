"""
Unit tests for boxman.providers.libvirt.import_image.ImageImporter.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.import_image import ImageImporter


pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True, stderr: str = "", return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


SAMPLE_VM_XML = """\
<domain type='kvm'>
  <name>template-vm</name>
  <uuid>00000000-0000-0000-0000-000000000000</uuid>
  <memory unit='KiB'>2097152</memory>
  <devices>
    <disk type='file' device='disk'>
      <source file='/old/path/disk.qcow2'/>
      <target dev='vda'/>
    </disk>
  </devices>
</domain>
"""


@pytest.fixture
def importer() -> ImageImporter:
    return ImageImporter(uri="qemu:///system")


class TestLoadManifest:

    def test_missing_file_returns_none(self, importer: ImageImporter, tmp_path: Path):
        out = importer.load_manifest(str(tmp_path / "nope.json"))
        assert out is None

    def test_valid_manifest_returned(self, importer: ImageImporter, tmp_path: Path):
        m = {"xml_path": "vm.xml", "image_path": "disk.qcow2", "provider": "libvirt"}
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps(m))
        assert importer.load_manifest(str(p)) == m

    def test_missing_xml_path_rejected(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "m.json"
        p.write_text(json.dumps({"image_path": "disk.qcow2"}))
        assert importer.load_manifest(str(p)) is None

    def test_missing_image_path_rejected(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "m.json"
        p.write_text(json.dumps({"xml_path": "vm.xml"}))
        assert importer.load_manifest(str(p)) is None

    def test_unsupported_provider_rejected(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "m.json"
        p.write_text(json.dumps({
            "xml_path": "x", "image_path": "d", "provider": "aws-ec2",
        }))
        assert importer.load_manifest(str(p)) is None

    def test_bad_json_returns_none(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "m.json"
        p.write_text("this is not json {")
        assert importer.load_manifest(str(p)) is None


class TestLoadXML:

    def test_valid_xml_returns_tree(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "v.xml"
        p.write_text(SAMPLE_VM_XML)
        tree = importer.load_xml(str(p))
        assert tree is not None
        assert tree.getroot().xpath("/domain/name")[0].text == "template-vm"

    def test_malformed_xml_returns_none(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "v.xml"
        p.write_text("<domain><unclosed></domain>")
        assert importer.load_xml(str(p)) is None


class TestEditVmXML:

    def test_edits_name_uuid_and_disk(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "vm.xml"
        p.write_text(SAMPLE_VM_XML)
        assert importer.edit_vm_xml(
            str(p), new_vm_name="new-vm", disk_path="/new/path/disk.qcow2",
            change_uuid=True,
        ) is True

        edited = importer.load_xml(str(p)).getroot()
        assert edited.xpath("/domain/name")[0].text == "new-vm"
        new_uuid = edited.xpath("/domain/uuid")[0].text
        assert new_uuid and new_uuid != "00000000-0000-0000-0000-000000000000"
        disk_source = edited.xpath(
            "/domain/devices/disk[@type='file'][@device='disk']/source"
        )[0]
        assert disk_source.get("file") == "/new/path/disk.qcow2"

    def test_keeps_uuid_when_change_uuid_false(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "vm.xml"
        p.write_text(SAMPLE_VM_XML)
        importer.edit_vm_xml(str(p), "new-vm", "/x/disk.qcow2", change_uuid=False)
        edited = importer.load_xml(str(p)).getroot()
        assert edited.xpath("/domain/uuid")[0].text == "00000000-0000-0000-0000-000000000000"

    def test_returns_false_if_no_name_element(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "vm.xml"
        p.write_text("<domain><uuid>abc</uuid></domain>")
        assert importer.edit_vm_xml(str(p), "x", "/y.qcow2") is False

    def test_returns_false_if_no_disk_source(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "vm.xml"
        p.write_text(
            "<domain><name>t</name><uuid>u</uuid>"
            "<devices></devices></domain>"
        )
        assert importer.edit_vm_xml(str(p), "newname", "/y") is False


class TestCheckVmExists:

    def test_true_when_in_list(self, importer: ImageImporter):
        with patch(
            "boxman.providers.libvirt.import_image.run",
            return_value=_result(stdout="vm-a\nneedle\nvm-b\n"),
        ):
            assert importer.check_vm_exists("needle") is True

    def test_false_when_not_in_list(self, importer: ImageImporter):
        with patch(
            "boxman.providers.libvirt.import_image.run",
            return_value=_result(stdout="vm-a\nvm-b\n"),
        ):
            assert importer.check_vm_exists("needle") is False

    def test_false_on_run_failure(self, importer: ImageImporter):
        with patch(
            "boxman.providers.libvirt.import_image.run",
            return_value=_result(ok=False),
        ):
            assert importer.check_vm_exists("needle") is False

    def test_false_on_exception(self, importer: ImageImporter):
        with patch(
            "boxman.providers.libvirt.import_image.run",
            side_effect=RuntimeError("boom"),
        ):
            assert importer.check_vm_exists("needle") is False


class TestDefineVM:

    def test_success(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "vm.xml"
        p.write_text(SAMPLE_VM_XML)
        with patch(
            "boxman.providers.libvirt.import_image.run",
            return_value=_result(ok=True),
        ):
            assert importer.define_vm(str(p)) is True

    def test_failure(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "vm.xml"
        p.write_text(SAMPLE_VM_XML)
        with patch(
            "boxman.providers.libvirt.import_image.run",
            return_value=_result(ok=False, stderr="err"),
        ):
            assert importer.define_vm(str(p)) is False

    def test_exception(self, importer: ImageImporter, tmp_path: Path):
        p = tmp_path / "vm.xml"
        p.write_text(SAMPLE_VM_XML)
        with patch(
            "boxman.providers.libvirt.import_image.run",
            side_effect=RuntimeError("x"),
        ):
            assert importer.define_vm(str(p)) is False


class TestCopyDiskImageSparse:

    def test_success(self, importer: ImageImporter):
        with patch(
            "boxman.providers.libvirt.import_image.run",
            return_value=_result(ok=True),
        ) as run_fn:
            assert importer.copy_disk_image_sparse("/src.qcow2", "/dst.qcow2") is True
        cmd = run_fn.call_args.args[0]
        assert "rsync --sparse --progress" in cmd
        assert "/src.qcow2" in cmd
        assert "/dst.qcow2" in cmd

    def test_failure(self, importer: ImageImporter):
        with patch(
            "boxman.providers.libvirt.import_image.run",
            return_value=_result(ok=False, stderr="nope"),
        ):
            assert importer.copy_disk_image_sparse("/a", "/b") is False

    def test_exception(self, importer: ImageImporter):
        with patch(
            "boxman.providers.libvirt.import_image.run",
            side_effect=RuntimeError("x"),
        ):
            assert importer.copy_disk_image_sparse("/a", "/b") is False


class TestProgressCallback:

    def test_info_calls_callback(self):
        callback_msgs: list[str] = []
        imp = ImageImporter(progress_callback=callback_msgs.append)
        imp._log_info("hello")
        assert "hello" in callback_msgs

    def test_error_calls_callback(self):
        callback_msgs: list[str] = []
        imp = ImageImporter(progress_callback=callback_msgs.append)
        imp._log_error("bad")
        assert "bad" in callback_msgs
