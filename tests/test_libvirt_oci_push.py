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

    def __call__(self, cmd, **kwargs):
        # Stash the most recent invocation so tests can inspect it.
        self.last_cmd = cmd
        self.last_kwargs = kwargs
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
        # pushed by basename from the file's dir (oras rejects absolute paths)
        assert fake.last_cmd[3] == "disk.qcow2"
        assert fake.last_kwargs.get("cwd") == str(qcow2.resolve().parent)
        assert str(qcow2) not in fake.last_cmd  # no absolute path leaks in

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
        assert "disk.qcow2" in fake.last_cmd
        assert "vmimage.json" in fake.last_cmd
        assert fake.last_kwargs.get("cwd") == str(qcow2.resolve().parent)
        # only basenames, never absolute paths (oras rejects those)
        assert str(qcow2) not in fake.last_cmd
        assert str(metadata) not in fake.last_cmd

    def test_metadata_in_different_dir_raises(self, tmp_path: Path):
        qcow2 = tmp_path / "disk.qcow2"
        qcow2.write_bytes(b"qcow2 data")
        sub = tmp_path / "sub"
        sub.mkdir()
        metadata = sub / "vmimage.json"
        metadata.write_text("{}")
        with pytest.raises(RuntimeError, match="same directory"):
            push_oci_image(
                image_ref="registry.com/repo:tag",
                qcow2_path=str(qcow2),
                metadata_path=str(metadata),
            )

    def test_relative_metadata_resolved_against_qcow2_dir(self, tmp_path: Path, monkeypatch):
        """A bare relative --metadata is co-located with the qcow2, not the CWD.

        Invoking from an unrelated directory must still find vmimage.json sitting
        next to the qcow2 (the co-location contract), not raise 'not found'.
        """
        qcow2 = tmp_path / "disk.qcow2"
        metadata = tmp_path / "vmimage.json"
        qcow2.write_bytes(b"qcow2 data")
        metadata.write_text("{}")
        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.chdir(other)
        fake = _FakeRun(returncode=0)
        with patch(
            "boxman.providers.libvirt.oci_push.subprocess.run", side_effect=fake
        ):
            push_oci_image(
                image_ref="registry.com/repo:tag",
                qcow2_path=str(qcow2),
                metadata_path="vmimage.json",  # bare, NOT relative to the CWD
            )
        assert fake.last_kwargs.get("cwd") == os.path.dirname(
            os.path.abspath(str(qcow2))
        )
        assert "disk.qcow2" in fake.last_cmd
        assert "vmimage.json" in fake.last_cmd

    def test_symlink_qcow2_basename_resolvable_from_cwd(self, tmp_path: Path):
        """cwd and basename must come from the same (unresolved) path.

        A symlink whose name/dir differ from its target previously paired a
        resolved cwd with the symlink's basename, naming a file that does not
        exist there.
        """
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        target = real_dir / "disk.qcow2"
        target.write_bytes(b"qcow2 data")
        link_dir = tmp_path / "links"
        link_dir.mkdir()
        link = link_dir / "mydisk.qcow2"
        link.symlink_to(target)
        fake = _FakeRun(returncode=0)
        with patch(
            "boxman.providers.libvirt.oci_push.subprocess.run", side_effect=fake
        ):
            push_oci_image(image_ref="registry.com/repo:tag", qcow2_path=str(link))
        cwd = fake.last_kwargs.get("cwd")
        pushed = fake.last_cmd[3]
        assert pushed == "mydisk.qcow2"
        assert cwd == str(link_dir)
        # basename must actually open relative to cwd (oras follows the symlink)
        assert (Path(cwd) / pushed).is_file()

    def test_relative_qcow2_from_other_cwd(self, tmp_path: Path, monkeypatch):
        """A relative qcow2 path anchors to the process CWD; cwd+basename then
        resolve to the real file regardless of where oras is launched from."""
        data = tmp_path / "data"
        data.mkdir()
        (data / "disk.qcow2").write_bytes(b"qcow2 data")
        monkeypatch.chdir(data)
        fake = _FakeRun(returncode=0)
        with patch(
            "boxman.providers.libvirt.oci_push.subprocess.run", side_effect=fake
        ):
            push_oci_image(image_ref="registry.com/repo:tag", qcow2_path="disk.qcow2")
        cwd = fake.last_kwargs.get("cwd")
        assert cwd == str(data)
        assert fake.last_cmd[3] == "disk.qcow2"
        assert (Path(cwd) / fake.last_cmd[3]).is_file()

    def test_relative_metadata_with_subdir_rejected(self, tmp_path: Path, monkeypatch):
        """A relative --metadata with directory components keeps its full path
        and is rejected by the co-location check — it is NOT flattened onto a
        same-named decoy sitting next to the qcow2."""
        qcow2 = tmp_path / "disk.qcow2"
        qcow2.write_bytes(b"qcow2 data")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "vmimage.json").write_text("{}")
        # a decoy with the same basename right next to the qcow2
        (tmp_path / "vmimage.json").write_text('{"decoy": true}')
        monkeypatch.chdir(tmp_path)
        with pytest.raises(RuntimeError, match="same directory"):
            push_oci_image(
                image_ref="registry.com/repo:tag",
                qcow2_path=str(qcow2),
                metadata_path="sub/vmimage.json",
            )

    def test_tilde_metadata_expanded(self, tmp_path: Path, monkeypatch):
        """A `~`-prefixed metadata path is expanded before the absoluteness
        check (so it is not treated as a literal `~` directory)."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        qcow2 = home / "disk.qcow2"
        qcow2.write_bytes(b"qcow2 data")
        (home / "vmimage.json").write_text("{}")
        fake = _FakeRun(returncode=0)
        with patch(
            "boxman.providers.libvirt.oci_push.subprocess.run", side_effect=fake
        ):
            push_oci_image(
                image_ref="registry.com/repo:tag",
                qcow2_path=str(qcow2),
                metadata_path="~/vmimage.json",
            )
        assert "vmimage.json" in fake.last_cmd
        assert fake.last_kwargs.get("cwd") == str(home)

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
