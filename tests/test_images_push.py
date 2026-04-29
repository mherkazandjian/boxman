import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boxman.images.resolver import push_oci_image
from boxman.images.cli import image_push


class TestOciPush(unittest.TestCase):
    def test_oras_push_with_qcow2_only(self):
        """Test pushing a qcow2 file without metadata."""
        with tempfile.TemporaryDirectory() as td:
            # Create a fake qcow2 file
            qcow2_path = os.path.join(td, "disk.qcow2")
            Path(qcow2_path).write_bytes(b"qcow2 data")

            def fake_run(cmd, **kwargs):
                self.assertEqual(cmd[0], "oras")
                self.assertEqual(cmd[1], "push")
                self.assertEqual(cmd[2], "registry.com/repo:tag")
                self.assertEqual(cmd[3], qcow2_path)

                class R:
                    returncode = 0
                    stdout = "pushed"
                    stderr = ""

                return R()

            with patch("boxman.images.resolver.subprocess.run", side_effect=fake_run):
                push_oci_image(
                    image_ref="registry.com/repo:tag",
                    qcow2_path=qcow2_path,
                )

    def test_oras_push_with_qcow2_and_metadata(self):
        """Test pushing qcow2 with vmimage.json metadata."""
        with tempfile.TemporaryDirectory() as td:
            qcow2_path = os.path.join(td, "disk.qcow2")
            metadata_path = os.path.join(td, "vmimage.json")
            Path(qcow2_path).write_bytes(b"qcow2 data")
            Path(metadata_path).write_text('{"firmware": "uefi"}')

            def fake_run(cmd, **kwargs):
                self.assertEqual(cmd[0], "oras")
                self.assertEqual(cmd[1], "push")
                self.assertEqual(cmd[2], "registry.com/repo:v1.0")
                # Both files should be in the command
                self.assertIn(qcow2_path, cmd)
                self.assertIn(metadata_path, cmd)

                class R:
                    returncode = 0
                    stdout = "pushed"
                    stderr = ""

                return R()

            with patch("boxman.images.resolver.subprocess.run", side_effect=fake_run):
                push_oci_image(
                    image_ref="registry.com/repo:v1.0",
                    qcow2_path=qcow2_path,
                    metadata_path=metadata_path,
                )

    def test_push_fails_with_missing_qcow2(self):
        """Test that push fails if qcow2 file doesn't exist."""
        with self.assertRaises(RuntimeError) as ctx:
            push_oci_image(
                image_ref="registry.com/repo:tag",
                qcow2_path="/nonexistent/disk.qcow2",
            )
        self.assertIn("qcow2 file not found", str(ctx.exception))

    def test_push_fails_with_missing_metadata(self):
        """Test that push fails if metadata file doesn't exist."""
        with tempfile.TemporaryDirectory() as td:
            qcow2_path = os.path.join(td, "disk.qcow2")
            Path(qcow2_path).write_bytes(b"qcow2 data")

            with self.assertRaises(RuntimeError) as ctx:
                push_oci_image(
                    image_ref="registry.com/repo:tag",
                    qcow2_path=qcow2_path,
                    metadata_path="/nonexistent/vmimage.json",
                )
            self.assertIn("metadata file not found", str(ctx.exception))

    def test_push_fails_with_empty_image_ref(self):
        """Test that push fails if image_ref is empty."""
        with tempfile.TemporaryDirectory() as td:
            qcow2_path = os.path.join(td, "disk.qcow2")
            Path(qcow2_path).write_bytes(b"qcow2 data")

            with self.assertRaises(ValueError) as ctx:
                push_oci_image(
                    image_ref="",
                    qcow2_path=qcow2_path,
                )
            self.assertIn("image_ref must be a non-empty string", str(ctx.exception))

    def test_push_oras_not_found(self):
        """Test error handling when oras CLI is not found."""
        with tempfile.TemporaryDirectory() as td:
            qcow2_path = os.path.join(td, "disk.qcow2")
            Path(qcow2_path).write_bytes(b"qcow2 data")

            with patch("boxman.images.resolver.subprocess.run", side_effect=FileNotFoundError):
                with self.assertRaises(RuntimeError) as ctx:
                    push_oci_image(
                        image_ref="registry.com/repo:tag",
                        qcow2_path=qcow2_path,
                    )
                self.assertIn("oras CLI not found", str(ctx.exception))

    def test_push_oras_command_fails(self):
        """Test error handling when oras push command fails."""
        with tempfile.TemporaryDirectory() as td:
            qcow2_path = os.path.join(td, "disk.qcow2")
            Path(qcow2_path).write_bytes(b"qcow2 data")

            def fake_run(cmd, **kwargs):
                class R:
                    returncode = 1
                    stdout = ""
                    stderr = "authentication failed"

                return R()

            with patch("boxman.images.resolver.subprocess.run", side_effect=fake_run):
                with self.assertRaises(RuntimeError) as ctx:
                    push_oci_image(
                        image_ref="registry.com/repo:tag",
                        qcow2_path=qcow2_path,
                    )
                self.assertIn("oras push failed", str(ctx.exception))
                self.assertIn("authentication failed", str(ctx.exception))

    def test_image_push_cli_success(self):
        """Test the image_push CLI handler with successful push."""
        with tempfile.TemporaryDirectory() as td:
            qcow2_path = os.path.join(td, "disk.qcow2")
            Path(qcow2_path).write_bytes(b"qcow2 data")

            def fake_run(cmd, **kwargs):
                class R:
                    returncode = 0
                    stdout = "pushed"
                    stderr = ""

                return R()

            class FakeCLIArgs:
                qcow2 = qcow2_path
                image_ref = "registry.com/repo:tag"
                metadata = None

            with patch("boxman.images.resolver.subprocess.run", side_effect=fake_run):
                with patch("builtins.print") as mock_print:
                    image_push(None, FakeCLIArgs())
                    # Verify success message was printed
                    calls = [str(call) for call in mock_print.call_args_list]
                    self.assertTrue(any("Successfully pushed" in str(call) for call in calls))

    def test_image_push_cli_failure_exits_with_code_1(self):
        """Test that CLI handler exits with code 1 on failure."""
        with tempfile.TemporaryDirectory() as td:
            qcow2_path = os.path.join(td, "disk.qcow2")
            Path(qcow2_path).write_bytes(b"qcow2 data")

            class FakeCLIArgs:
                qcow2 = "/nonexistent/disk.qcow2"
                image_ref = "registry.com/repo:tag"
                metadata = None

            with patch("builtins.print"):
                with self.assertRaises(SystemExit) as ctx:
                    image_push(None, FakeCLIArgs())
                self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
