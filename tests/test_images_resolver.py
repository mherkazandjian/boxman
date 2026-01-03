import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boxman.config_cache import BoxmanCache
from boxman.images.resolver import resolve_base_image
from boxman.images.metadata import VmImageMetadata


class TestResolveBaseImageOCI(unittest.TestCase):
    def test_legacy_libvirt_vm(self):
        resolved = resolve_base_image("ubuntu-base", cache=None)
        self.assertEqual(resolved.kind, "libvirt-vm")
        self.assertEqual(resolved.src_vm_name, "ubuntu-base")

    def test_oci_pull_and_find_disk_qcow2(self):
        with tempfile.TemporaryDirectory() as td:
            cache = BoxmanCache()
            # Point cache root at temp dir to avoid touching user home.
            cache.cache_dir = td
            cache.images_cache_dir = os.path.join(td, "images")
            os.makedirs(cache.images_cache_dir, exist_ok=True)

            def fake_run(cmd, **kwargs):
                # simulate oras pull output into the -o directory
                out_dir = Path(cmd[cmd.index("-o") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "disk.qcow2").write_bytes(b"qcow2")
                (out_dir / "vmimage.json").write_text("{}")

                class R:
                    returncode = 0
                    stdout = "ok"
                    stderr = ""

                return R()

            with patch("boxman.images.resolver.subprocess.run", side_effect=fake_run) as p:
                resolved = resolve_base_image("oci://example.com/repo:tag", cache=cache)

                self.assertEqual(resolved.kind, "local-qcow2")
                qcow2_path = resolved.qcow2_path
                metadata_path = resolved.metadata_path
                self.assertIsNotNone(qcow2_path)
                self.assertTrue(str(qcow2_path).endswith("disk.qcow2"))
                self.assertIsNotNone(metadata_path)
                self.assertTrue(str(metadata_path).endswith("vmimage.json"))
                self.assertEqual(resolved.image_ref, "example.com/repo:tag")
                md = resolved.metadata
                self.assertIsNotNone(md)
                self.assertIsInstance(md, VmImageMetadata)
                self.assertEqual(md.firmware, "uefi")
                self.assertEqual(p.call_count, 1)

    def test_metadata_defaults_when_vmimage_missing(self):
        with tempfile.TemporaryDirectory() as td:
            cache = BoxmanCache()
            cache.cache_dir = td
            cache.images_cache_dir = os.path.join(td, "images")
            os.makedirs(cache.images_cache_dir, exist_ok=True)

            def fake_run(cmd, **kwargs):
                out_dir = Path(cmd[cmd.index("-o") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "disk.qcow2").write_bytes(b"qcow2")
                # Intentionally no vmimage.json

                class R:
                    returncode = 0
                    stdout = "ok"
                    stderr = ""

                return R()

            with patch("boxman.images.resolver.subprocess.run", side_effect=fake_run):
                resolved = resolve_base_image("oci://example.com/repo:tag", cache=cache)

                md = resolved.metadata
                self.assertIsNotNone(md)
                self.assertIsInstance(md, VmImageMetadata)
                self.assertEqual(md.firmware, "uefi")

    def test_metadata_firmware_bios_honored(self):
        with tempfile.TemporaryDirectory() as td:
            cache = BoxmanCache()
            cache.cache_dir = td
            cache.images_cache_dir = os.path.join(td, "images")
            os.makedirs(cache.images_cache_dir, exist_ok=True)

            def fake_run(cmd, **kwargs):
                out_dir = Path(cmd[cmd.index("-o") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "disk.qcow2").write_bytes(b"qcow2")
                (out_dir / "vmimage.json").write_text('{"firmware": "bios"}')

                class R:
                    returncode = 0
                    stdout = "ok"
                    stderr = ""

                return R()

            with patch("boxman.images.resolver.subprocess.run", side_effect=fake_run):
                resolved = resolve_base_image("oci://example.com/repo:tag", cache=cache)

                md = resolved.metadata
                self.assertIsNotNone(md)
                self.assertIsInstance(md, VmImageMetadata)
                self.assertEqual(md.firmware, "bios")

    def test_oci_idempotent_no_second_pull(self):
        with tempfile.TemporaryDirectory() as td:
            cache = BoxmanCache()
            cache.cache_dir = td
            cache.images_cache_dir = os.path.join(td, "images")
            os.makedirs(cache.images_cache_dir, exist_ok=True)

            # Pre-seed a cached qcow2 where the resolver will look.
            # We don't know the exact hashed directory name here, so we just
            # rely on the fake_run to create it on first call and then call
            # resolver again to ensure it doesn't call subprocess.run.

            def fake_run(cmd, **kwargs):
                out_dir = Path(cmd[cmd.index("-o") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "disk.qcow2").write_bytes(b"qcow2")

                class R:
                    returncode = 0
                    stdout = "ok"
                    stderr = ""

                return R()

            with patch("boxman.images.resolver.subprocess.run", side_effect=fake_run) as p:
                _ = resolve_base_image("oci://example.com/repo:tag", cache=cache)
                _ = resolve_base_image("oci://example.com/repo:tag", cache=cache)
                self.assertEqual(p.call_count, 1)


if __name__ == "__main__":
    unittest.main()
