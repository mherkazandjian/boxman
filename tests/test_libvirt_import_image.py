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


class TestValidateManifest:
    """Direct tests on the static manifest validator (raises ValueError)."""

    VALID = {"xml_path": "vm.xml", "image_path": "disk.qcow2", "provider": "libvirt"}

    def test_happy_path(self):
        ImageImporter._validate_manifest(self.VALID, "test://m")

    @pytest.mark.parametrize("key", ["xml_path", "image_path", "provider"])
    def test_missing_required_key_raises(self, key):
        bad = {k: v for k, v in self.VALID.items() if k != key}
        with pytest.raises(ValueError, match=f"missing required field {key!r}"):
            ImageImporter._validate_manifest(bad, "test://m")

    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="must be a JSON object"):
            ImageImporter._validate_manifest(["not", "a", "dict"], "test://m")

    @pytest.mark.parametrize("bad_value", ["", "   ", 42, None, []])
    def test_xml_path_must_be_non_empty_string(self, bad_value):
        bad = {**self.VALID, "xml_path": bad_value}
        with pytest.raises(ValueError, match="'xml_path'"):
            ImageImporter._validate_manifest(bad, "test://m")

    @pytest.mark.parametrize("bad_value", ["", 42, None, []])
    def test_image_path_must_be_non_empty_string(self, bad_value):
        bad = {**self.VALID, "image_path": bad_value}
        with pytest.raises(ValueError, match="'image_path'"):
            ImageImporter._validate_manifest(bad, "test://m")

    def test_unsupported_provider_raises(self):
        bad = {**self.VALID, "provider": "aws-ec2"}
        with pytest.raises(ValueError, match="not supported"):
            ImageImporter._validate_manifest(bad, "test://m")

    def test_provider_case_insensitive(self):
        ImageImporter._validate_manifest(
            {**self.VALID, "provider": "LIBVIRT"}, "test://m"
        )

    def test_source_appears_in_error(self):
        bad = {k: v for k, v in self.VALID.items() if k != "xml_path"}
        with pytest.raises(ValueError, match=r"https://example\.com/m\.json"):
            ImageImporter._validate_manifest(bad, "https://example.com/m.json")


class TestLoadManifestFromUri:
    """The strict, URI-aware loader (file://, http(s)://)."""

    VALID = {"xml_path": "vm.xml", "image_path": "disk.qcow2", "provider": "libvirt"}

    def test_file_uri_returns_dict_and_local_path(self, tmp_path: Path):
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps(self.VALID))
        manifest, local = ImageImporter.load_manifest_from_uri(f"file://{p}")
        assert manifest == self.VALID
        assert local == str(p)

    def test_file_uri_with_tilde_expands(self, tmp_path: Path, monkeypatch):
        # Pretend HOME is tmp_path so `~/manifest.json` resolves into it.
        monkeypatch.setenv("HOME", str(tmp_path))
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps(self.VALID))
        manifest, local = ImageImporter.load_manifest_from_uri("file://~/manifest.json")
        assert manifest == self.VALID
        assert local == str(p)

    def test_file_missing_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="manifest file not found"):
            ImageImporter.load_manifest_from_uri(f"file://{tmp_path / 'nope.json'}")

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError, match="unsupported manifest URI scheme"):
            ImageImporter.load_manifest_from_uri("ftp://example.com/m.json")

    def test_bad_json_raises(self, tmp_path: Path):
        p = tmp_path / "manifest.json"
        p.write_text("{ not valid json")
        with pytest.raises(ValueError, match="failed to parse manifest JSON"):
            ImageImporter.load_manifest_from_uri(f"file://{p}")

    def test_validation_error_propagates(self, tmp_path: Path):
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps({"xml_path": "x", "image_path": "y"}))  # no provider
        with pytest.raises(ValueError, match="missing required field 'provider'"):
            ImageImporter.load_manifest_from_uri(f"file://{p}")

    def test_http_uri_downloads_and_parses(self, tmp_path: Path):
        captured = {}

        def fake_download(url, dst):
            captured["url"] = url
            captured["dst"] = dst
            Path(dst).write_text(json.dumps(self.VALID))
            return True

        with patch(
            "boxman.providers.libvirt.import_image.download_url",
            side_effect=fake_download,
        ):
            manifest, local = ImageImporter.load_manifest_from_uri(
                "https://example.com/m.json"
            )
        assert manifest == self.VALID
        assert captured["url"] == "https://example.com/m.json"
        assert local.endswith("manifest.json")
        assert Path(local).exists()

    def test_http_download_failure_raises(self):
        with patch(
            "boxman.providers.libvirt.import_image.download_url",
            return_value=False,
        ):
            with pytest.raises(ValueError, match="failed to download manifest"):
                ImageImporter.load_manifest_from_uri("http://example.com/m.json")


class TestImportImageEndToEnd:
    """End-to-end happy path with all subprocess boundaries mocked."""

    def _build_package(self, root: Path) -> Path:
        manifest = {
            "xml_path": "vm/vm-definition.xml",
            "image_path": "vm/disk.qcow2",
            "provider": "libvirt",
        }
        (root / "vm").mkdir(parents=True)
        (root / "vm" / "vm-definition.xml").write_text(SAMPLE_VM_XML)
        (root / "vm" / "disk.qcow2").write_bytes(b"x" * 1024)
        (root / "manifest.json").write_text(json.dumps(manifest))
        return root / "manifest.json"

    def test_happy_path_calls_define_vm(self, tmp_path: Path):
        import re
        import shutil as _shutil

        manifest_path = self._build_package(tmp_path / "pkg")
        dst_dir = tmp_path / "dst"

        importer = ImageImporter(
            manifest_path=str(manifest_path),
            uri="qemu:///system",
            disk_dir=str(dst_dir),
            vm_name="end2end",
        )

        # Realistic mock: dispatch on the command shape so rsync actually
        # creates the destination, sha256sum returns matching hashes for
        # src/dst, and virsh succeeds.
        def fake_run(cmd, *args, **kwargs):
            if cmd.startswith("virsh") and "list --all --name" in cmd:
                return _result(stdout="other-vm\n", ok=True)
            if cmd.startswith("rsync"):
                m = re.match(r'rsync --sparse --progress "([^"]+)" "([^"]+)"', cmd)
                assert m, f"unexpected rsync cmd: {cmd}"
                _shutil.copyfile(m.group(1), m.group(2))
                return _result(ok=True)
            if cmd.startswith("sha256sum"):
                return _result(stdout="dead  beef\n", ok=True)
            if cmd.startswith("virsh") and "define" in cmd:
                return _result(ok=True)
            raise AssertionError(f"unexpected command: {cmd}")

        with patch(
            "boxman.providers.libvirt.import_image.run",
            side_effect=fake_run,
        ) as run_fn:
            assert importer.import_image() is True

        commands_run = [c.args[0] for c in run_fn.call_args_list]
        assert any("virsh -c qemu:///system list --all --name" in c for c in commands_run)
        assert any("rsync --sparse" in c for c in commands_run)
        assert any("virsh -c qemu:///system define" in c for c in commands_run)

        # Disk + edited XML in the destination layout.
        assert (dst_dir / "end2end" / "disk.qcow2").exists()
        assert (dst_dir / "end2end" / "end2end.xml").exists()

    def test_aborts_when_vm_already_exists(self, tmp_path: Path):
        manifest_path = self._build_package(tmp_path / "pkg")
        dst_dir = tmp_path / "dst"

        importer = ImageImporter(
            manifest_path=str(manifest_path),
            uri="qemu:///system",
            disk_dir=str(dst_dir),
            vm_name="dup",
        )

        with patch(
            "boxman.providers.libvirt.import_image.run",
            return_value=_result(stdout="dup\nother\n", ok=True),
        ):
            assert importer.import_image() is False
        # Nothing copied since we bailed early.
        assert not (dst_dir / "dup").exists()
