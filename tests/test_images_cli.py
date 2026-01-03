import unittest

from boxman.images.cli import format_image_inspect
from boxman.images.metadata import VmImageMetadata
from boxman.images.resolver import ResolvedBaseImage


class TestImageInspectFormatting(unittest.TestCase):
    def test_format_legacy_libvirt_vm(self):
        resolved = ResolvedBaseImage(kind="libvirt-vm", src_vm_name="ubuntu-base")
        out = format_image_inspect(resolved=resolved, cache_dir=None)
        self.assertIn("kind: libvirt-vm", out)
        self.assertIn("src_vm_name: ubuntu-base", out)

    def test_format_local_qcow2_with_metadata(self):
        resolved = ResolvedBaseImage(
            kind="local-qcow2",
            qcow2_path="/tmp/cache/disk.qcow2",
            metadata_path="/tmp/cache/vmimage.json",
            image_ref="example.com/repo:tag",
            metadata=VmImageMetadata(firmware="bios", machine="pc", disk_bus="virtio", net_model="e1000"),
        )
        out = format_image_inspect(resolved=resolved, cache_dir="/tmp/cache")
        self.assertIn("kind: local-qcow2", out)
        self.assertIn("image_ref: example.com/repo:tag", out)
        self.assertIn("cache_dir: /tmp/cache", out)
        self.assertIn("qcow2_path: /tmp/cache/disk.qcow2", out)
        self.assertIn("metadata_path: /tmp/cache/vmimage.json", out)
        self.assertIn("firmware: bios", out)
        self.assertIn("machine: pc", out)
        self.assertIn("disk_bus: virtio", out)
        self.assertIn("net_model: e1000", out)


if __name__ == "__main__":
    unittest.main()
