"""Unit tests for OCI image push (push_oci_image + boxman image push CLI).

Originally authored by Orski174 (omareljamal17@gmail.com) on the
``oci-feat`` branch (PR #30, commit ac8310fe). Ported here to the
current architecture: ``boxman.images.resolver`` → ``boxman.providers.libvirt.oci_push``,
``boxman.images.cli.image_push`` → ``BoxmanManager.push_image``,
unittest.TestCase → pytest style.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from boxman.manager import BoxmanManager
from boxman.providers.libvirt.oci_push import push_oci_image


pytestmark = pytest.mark.unit


class _FakeRun:
    """Build a fake subprocess.run side-effect for the oras CLI."""

    def __init__(self, returncode: int = 0, stdout: str = "pushed", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, cmd, **_kwargs):
        # Stash the most recent invocation so tests can inspect it.
        self.last_cmd = cmd
        return SimpleNamespace(
            returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


class TestPushOciImage:
    """Direct tests on ``push_oci_image`` (via ``_oras_push``)."""

    def test_qcow2_only(self, tmp_path: Path):
        qcow2 = tmp_path / "disk.qcow2"
        qcow2.write_bytes(b"qcow2 data")
        fake = _FakeRun(returncode=0)
        with patch(
            "boxman.providers.libvirt.oci_push.subprocess.run", side_effect=fake
        ):
            push_oci_image(image_ref="registry.com/repo:tag", qcow2_path=str(qcow2))
        assert fake.last_cmd[:3] == ["oras", "push", "registry.com/repo:tag"]
        assert fake.last_cmd[3] == str(qcow2)

    def test_qcow2_and_metadata(self, tmp_path: Path):
        qcow2 = tmp_path / "disk.qcow2"
        metadata = tmp_path / "vmimage.json"
        qcow2.write_bytes(b"qcow2 data")
        metadata.write_text('{"firmware": "uefi"}')
        fake = _FakeRun(returncode=0)
        with patch(
            "boxman.providers.libvirt.oci_push.subprocess.run", side_effect=fake
        ):
            push_oci_image(
                image_ref="registry.com/repo:v1.0",
                qcow2_path=str(qcow2),
                metadata_path=str(metadata),
            )
        assert fake.last_cmd[:3] == ["oras", "push", "registry.com/repo:v1.0"]
        assert str(qcow2) in fake.last_cmd
        assert str(metadata) in fake.last_cmd

    def test_missing_qcow2_raises(self):
        with pytest.raises(RuntimeError, match="qcow2 file not found"):
            push_oci_image(
                image_ref="registry.com/repo:tag",
                qcow2_path="/nonexistent/disk.qcow2",
            )

    def test_missing_metadata_raises(self, tmp_path: Path):
        qcow2 = tmp_path / "disk.qcow2"
        qcow2.write_bytes(b"qcow2 data")
        with pytest.raises(RuntimeError, match="metadata file not found"):
            push_oci_image(
                image_ref="registry.com/repo:tag",
                qcow2_path=str(qcow2),
                metadata_path="/nonexistent/vmimage.json",
            )

    def test_empty_image_ref_raises(self, tmp_path: Path):
        qcow2 = tmp_path / "disk.qcow2"
        qcow2.write_bytes(b"qcow2 data")
        with pytest.raises(ValueError, match="image_ref must be a non-empty string"):
            push_oci_image(image_ref="", qcow2_path=str(qcow2))

    def test_oras_not_found(self, tmp_path: Path):
        qcow2 = tmp_path / "disk.qcow2"
        qcow2.write_bytes(b"qcow2 data")
        with patch(
            "boxman.providers.libvirt.oci_push.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            with pytest.raises(RuntimeError, match="oras CLI not found"):
                push_oci_image(
                    image_ref="registry.com/repo:tag", qcow2_path=str(qcow2)
                )

    def test_oras_command_failure_includes_stderr(self, tmp_path: Path):
        qcow2 = tmp_path / "disk.qcow2"
        qcow2.write_bytes(b"qcow2 data")
        fake = _FakeRun(returncode=1, stdout="", stderr="authentication failed")
        with patch(
            "boxman.providers.libvirt.oci_push.subprocess.run", side_effect=fake
        ):
            with pytest.raises(RuntimeError) as excinfo:
                push_oci_image(
                    image_ref="registry.com/repo:tag", qcow2_path=str(qcow2)
                )
        msg = str(excinfo.value)
        assert "oras push failed" in msg
        assert "authentication failed" in msg


class TestPushImageCli:
    """Tests for the CLI dispatcher ``BoxmanManager.push_image``."""

    def test_success_prints_message(self, tmp_path: Path, capsys):
        qcow2 = tmp_path / "disk.qcow2"
        qcow2.write_bytes(b"qcow2 data")
        cli_args = SimpleNamespace(
            image_ref="registry.com/repo:tag",
            qcow2=str(qcow2),
            metadata=None,
        )
        fake = _FakeRun(returncode=0)
        with patch(
            "boxman.providers.libvirt.oci_push.subprocess.run", side_effect=fake
        ):
            BoxmanManager.push_image(None, cli_args)
        out = capsys.readouterr().out
        assert "successfully pushed" in out
        assert "registry.com/repo:tag" in out

    def test_failure_exits_with_code_1(self, capsys):
        cli_args = SimpleNamespace(
            image_ref="registry.com/repo:tag",
            qcow2="/nonexistent/disk.qcow2",
            metadata=None,
        )
        with pytest.raises(SystemExit) as excinfo:
            BoxmanManager.push_image(None, cli_args)
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "error pushing image" in out
