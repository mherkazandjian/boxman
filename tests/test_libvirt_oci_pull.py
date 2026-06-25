"""Unit tests for OCI image pull/inspect (oci_pull + boxman image inspect CLI).

Counterpart to ``test_libvirt_oci_push.py``. The ``oras`` CLI is stubbed via
``subprocess.run`` so these tests never touch a real registry.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from boxman.manager import BoxmanManager
from boxman.providers.libvirt.oci_pull import (
    VmImageMetadata,
    _repo_without_tag,
    format_inspect,
    inspect_oci_image,
    pull_oci_image,
)


pytestmark = pytest.mark.unit


class _FakeRun:
    """A fake ``subprocess.run`` side-effect for the oras CLI.

    *handler* (optional) receives the command and may create files (to emulate
    ``oras pull -o <dir>``); it returns the stdout string for that call.
    """

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", handler=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.handler = handler
        self.calls: list = []

    def __call__(self, cmd, **_kwargs):
        self.calls.append(cmd)
        out = self.stdout
        if self.handler is not None:
            produced = self.handler(cmd)
            if produced is not None:
                out = produced
        return SimpleNamespace(returncode=self.returncode, stdout=out, stderr=self.stderr)


# ── pull_oci_image ────────────────────────────────────────────────────────────


class TestPullOciImage:
    def test_pull_finds_disk_qcow2(self, tmp_path: Path):
        out_dir = tmp_path / "out"

        def handler(cmd):
            d = Path(cmd[cmd.index("-o") + 1])
            d.mkdir(parents=True, exist_ok=True)
            (d / "disk.qcow2").write_bytes(b"qcow2")
            (d / "vmimage.json").write_text("{}")
            return "pulled"

        fake = _FakeRun(returncode=0, handler=handler)
        with patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake):
            qcow2 = pull_oci_image("oci://reg/repo:tag", str(out_dir))
        assert qcow2.endswith("disk.qcow2")
        # scheme is stripped before being handed to oras
        assert fake.calls[0][:3] == ["oras", "pull", "reg/repo:tag"]

    def test_pull_falls_back_to_any_qcow2(self, tmp_path: Path):
        out_dir = tmp_path / "out"

        def handler(cmd):
            d = Path(cmd[cmd.index("-o") + 1])
            d.mkdir(parents=True, exist_ok=True)
            (d / "ubuntu-24.04.qcow2").write_bytes(b"qcow2")
            return "pulled"

        fake = _FakeRun(returncode=0, handler=handler)
        with patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake):
            qcow2 = pull_oci_image("reg/repo:tag", str(out_dir))
        assert qcow2.endswith("ubuntu-24.04.qcow2")

    def test_pull_without_qcow2_raises(self, tmp_path: Path):
        out_dir = tmp_path / "out"

        def handler(cmd):
            d = Path(cmd[cmd.index("-o") + 1])
            d.mkdir(parents=True, exist_ok=True)
            (d / "readme.txt").write_text("no disk here")
            return "pulled"

        fake = _FakeRun(returncode=0, handler=handler)
        with patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake):
            with pytest.raises(RuntimeError, match="no qcow2 was found"):
                pull_oci_image("reg/repo:tag", str(out_dir))

    def test_pull_empty_ref_raises(self):
        with pytest.raises(ValueError, match="image_ref must be a non-empty string"):
            pull_oci_image("", "/tmp/whatever")

    def test_pull_oras_not_found(self, tmp_path: Path):
        with patch(
            "boxman.providers.libvirt.oci_pull.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            with pytest.raises(RuntimeError, match="oras CLI not found"):
                pull_oci_image("reg/repo:tag", str(tmp_path / "out"))

    def test_pull_oras_failure_includes_stderr(self, tmp_path: Path):
        fake = _FakeRun(returncode=1, stdout="", stderr="manifest unknown")
        with patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake):
            with pytest.raises(RuntimeError) as excinfo:
                pull_oci_image("reg/repo:tag", str(tmp_path / "out"))
        msg = str(excinfo.value)
        assert "oras pull failed" in msg
        assert "manifest unknown" in msg


# ── inspect_oci_image ─────────────────────────────────────────────────────────


_MANIFEST = json.dumps({
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "annotations": {"org.opencontainers.image.created": "2026-06-25"},
    "layers": [
        {
            "mediaType": "application/octet-stream",
            "size": 123456,
            "digest": "sha256:aaa",
            "annotations": {"org.opencontainers.image.title": "disk.qcow2"},
        },
        {
            "mediaType": "application/json",
            "size": 42,
            "digest": "sha256:bbb",
            "annotations": {"org.opencontainers.image.title": "vmimage.json"},
        },
    ],
})


class TestInspectOciImage:
    def test_inspect_summarizes_manifest_and_metadata(self):
        def handler(cmd):
            if cmd[:3] == ["oras", "manifest", "fetch"]:
                return _MANIFEST
            if cmd[:3] == ["oras", "blob", "fetch"]:
                return json.dumps({"firmware": "bios", "net_model": "e1000"})
            return ""

        fake = _FakeRun(returncode=0, handler=handler)
        with patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake):
            summary = inspect_oci_image("oci://reg/repo:tag")

        assert summary["image_ref"] == "reg/repo:tag"
        assert summary["media_type"].endswith("manifest.v1+json")
        titles = [layer["title"] for layer in summary["layers"]]
        assert "disk.qcow2" in titles and "vmimage.json" in titles
        md = summary["metadata"]
        assert isinstance(md, VmImageMetadata)
        assert md.firmware == "bios"
        assert md.net_model == "e1000"
        # the blob-fetch ref must use repo@digest (tag dropped)
        blob_calls = [c for c in fake.calls if c[:3] == ["oras", "blob", "fetch"]]
        assert blob_calls and blob_calls[0][3] == "reg/repo@sha256:bbb"

    def test_inspect_without_metadata_layer(self):
        manifest = json.dumps({
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "layers": [
                {
                    "mediaType": "application/octet-stream",
                    "size": 1,
                    "digest": "sha256:aaa",
                    "annotations": {"org.opencontainers.image.title": "disk.qcow2"},
                }
            ],
        })

        fake = _FakeRun(returncode=0, stdout=manifest)
        with patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake):
            summary = inspect_oci_image("reg/repo:tag")
        assert summary["metadata"] is None
        # no blob fetch should have been attempted
        assert all(c[:3] != ["oras", "blob", "fetch"] for c in fake.calls)

    def test_inspect_empty_ref_raises(self):
        with pytest.raises(ValueError, match="image_ref must be a non-empty string"):
            inspect_oci_image("")

    def test_inspect_bad_manifest_json_raises(self):
        fake = _FakeRun(returncode=0, stdout="not json")
        with patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake):
            with pytest.raises(ValueError, match="could not parse manifest"):
                inspect_oci_image("reg/repo:tag")


# ── _repo_without_tag (tag/digest/port parsing) ───────────────────────────────


class TestRepoWithoutTag:
    def test_plain_tag(self):
        assert _repo_without_tag("reg.com/team/ubuntu:latest") == "reg.com/team/ubuntu"

    def test_registry_with_port(self):
        assert _repo_without_tag("localhost:5000/repo:tag") == "localhost:5000/repo"

    def test_no_tag(self):
        assert _repo_without_tag("reg.com/team/ubuntu") == "reg.com/team/ubuntu"

    def test_digest_pinned_ref(self):
        # the ':' inside the digest must NOT be treated as a tag separator
        assert _repo_without_tag(
            "reg.com/team/ubuntu@sha256:abcdef0123456789") == "reg.com/team/ubuntu"

    def test_digest_with_port(self):
        assert _repo_without_tag(
            "localhost:5000/repo@sha256:abc") == "localhost:5000/repo"


# ── metadata mapping ──────────────────────────────────────────────────────────


class TestMetadataFromDict:
    def test_defaults(self):
        from boxman.providers.libvirt.oci_pull import _metadata_from_dict
        md = _metadata_from_dict({})
        assert md == VmImageMetadata()
        assert md.firmware == "uefi"
        assert md.disk_bus == "virtio"

    def test_known_fields_and_unknown_ignored(self):
        from boxman.providers.libvirt.oci_pull import _metadata_from_dict
        md = _metadata_from_dict(
            {"firmware": "bios", "machine": "q35", "arch": "x86_64", "extra": "ignored"})
        assert md.firmware == "bios"
        assert md.machine == "q35"
        assert md.arch == "x86_64"


# ── inspect: image index + digest refs ────────────────────────────────────────


class TestInspectImageIndex:
    def test_multi_arch_index_surfaced(self):
        index = json.dumps({
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"mediaType": "application/vnd.oci.image.manifest.v1+json",
                 "size": 100, "digest": "sha256:amd",
                 "platform": {"os": "linux", "architecture": "amd64"}},
                {"mediaType": "application/vnd.oci.image.manifest.v1+json",
                 "size": 101, "digest": "sha256:arm",
                 "platform": {"os": "linux", "architecture": "arm64"}},
            ],
        })
        fake = _FakeRun(returncode=0, stdout=index)
        with patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake):
            summary = inspect_oci_image("reg/repo:multi")
        assert summary["layers"] == []
        assert len(summary["manifests"]) == 2
        plats = [m["platform"] for m in summary["manifests"]]
        assert "linux/amd64" in plats and "linux/arm64" in plats
        # no vmimage.json layer -> no blob fetch attempted
        assert all(c[:3] != ["oras", "blob", "fetch"] for c in fake.calls)
        out = format_inspect(summary)
        assert "manifests (image index): 2" in out
        assert "linux/amd64" in out


class TestInspectDigestRef:
    def test_blob_fetch_drops_ref_digest(self):
        def handler(cmd):
            if cmd[:3] == ["oras", "manifest", "fetch"]:
                return _MANIFEST
            if cmd[:3] == ["oras", "blob", "fetch"]:
                return json.dumps({"firmware": "uefi"})
            return ""

        fake = _FakeRun(returncode=0, handler=handler)
        with patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake):
            summary = inspect_oci_image("oci://reg/boxman/ubuntu@sha256:deadbeef")
        blob_calls = [c for c in fake.calls if c[:3] == ["oras", "blob", "fetch"]]
        assert blob_calls
        # the repo must drop the ref's @digest; the blob ref uses the LAYER digest
        assert blob_calls[0][3] == "reg/boxman/ubuntu@sha256:bbb"
        assert summary["metadata"] is not None


# ── format_inspect ────────────────────────────────────────────────────────────


class TestFormatInspect:
    def test_format_without_metadata(self):
        out = format_inspect({
            "image_ref": "reg/repo:tag",
            "media_type": "application/vnd.oci.image.manifest.v1+json",
            "layers": [{"title": "disk.qcow2", "media_type": "x", "size": 9, "digest": "sha256:a"}],
            "annotations": {},
            "metadata": None,
        })
        assert "image_ref: reg/repo:tag" in out
        assert "disk.qcow2" in out
        assert "metadata: <none>" in out

    def test_format_with_metadata(self):
        out = format_inspect({
            "image_ref": "reg/repo:tag",
            "media_type": "",
            "layers": [],
            "annotations": {},
            "metadata": VmImageMetadata(firmware="bios", net_model="e1000"),
        })
        assert "firmware: bios" in out
        assert "net_model: e1000" in out


# ── BoxmanManager.inspect_image (CLI dispatcher) ──────────────────────────────


class TestInspectImageCli:
    def test_success_prints_summary(self, capsys):
        cli_args = SimpleNamespace(image_ref="oci://reg/repo:tag")
        fake = _FakeRun(returncode=0, stdout=_MANIFEST)

        def handler(cmd):
            if cmd[:3] == ["oras", "manifest", "fetch"]:
                return _MANIFEST
            return json.dumps({"firmware": "uefi"})

        fake.handler = handler
        with patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake):
            BoxmanManager.inspect_image(None, cli_args)
        out = capsys.readouterr().out
        assert "image_ref: reg/repo:tag" in out
        assert "disk.qcow2" in out

    def test_failure_exits_with_code_1(self, capsys):
        cli_args = SimpleNamespace(image_ref="reg/repo:tag")
        fake = _FakeRun(returncode=1, stdout="", stderr="manifest unknown")
        with patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake):
            with pytest.raises(SystemExit) as excinfo:
                BoxmanManager.inspect_image(None, cli_args)
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "error inspecting image" in out


# ── CloudInitTemplate._oci_cache_url (collision-free cache key) ────────────────


class TestOciCacheUrl:
    """The OCI cache key must not collide for distinct refs sharing a repo:tag."""

    def test_distinct_registries_same_tail_do_not_collide(self):
        import os
        from urllib.parse import urlparse
        from boxman.providers.libvirt.cloudinit import CloudInitTemplate

        a = CloudInitTemplate._oci_cache_url("oci://regA/team1/ubuntu:latest")
        b = CloudInitTemplate._oci_cache_url("oci://regB/team2/ubuntu:latest")
        assert a != b
        # ImageCache keys by basename — those must differ too
        base_a = os.path.basename(urlparse(a).path)
        base_b = os.path.basename(urlparse(b).path)
        assert base_a != base_b
        assert base_a.endswith(".qcow2") and ":" not in base_a

    def test_same_ref_is_stable(self):
        from boxman.providers.libvirt.cloudinit import CloudInitTemplate

        ref = "oci://reg/repo:tag"
        assert (CloudInitTemplate._oci_cache_url(ref)
                == CloudInitTemplate._oci_cache_url(ref))

    def test_scheme_optional(self):
        from boxman.providers.libvirt.cloudinit import CloudInitTemplate

        assert (CloudInitTemplate._oci_cache_url("oci://reg/repo:tag")
                == CloudInitTemplate._oci_cache_url("reg/repo:tag"))
