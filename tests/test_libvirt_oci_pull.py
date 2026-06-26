"""Unit tests for OCI image pull/inspect (oci_pull + boxman image inspect CLI).

Counterpart to ``test_libvirt_oci_push.py``. The ``oras`` CLI is stubbed via
``subprocess.run`` so these tests never touch a real registry.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from boxman.manager import BoxmanManager
from boxman.providers.libvirt import oci_pull
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


def _gztar(members: dict) -> bytes:
    """Build a gzip-compressed tar (``{member_name: bytes}``) as a byte string."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _artifact_manifest(titles=("disk.qcow2",)) -> str:
    """A boxman/oras artifact manifest with titled ``*.qcow2`` layer(s)."""
    return json.dumps({
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "layers": [
            {
                "mediaType": "application/octet-stream",
                "size": 1,
                "digest": f"sha256:{t}",
                "annotations": {"org.opencontainers.image.title": t},
            }
            for t in titles
        ],
    })


def _image_manifest(layer_digests) -> str:
    """A container-image manifest (no titles) carrying *layer_digests*."""
    return json.dumps({
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json"},
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "size": 1,
                "digest": d,
            }
            for d in layer_digests
        ],
    })


def _index_manifest(entries) -> str:
    """A multi-arch image index. *entries* = list of ``(digest, os, arch)``."""
    return json.dumps({
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "size": 1,
                "digest": d,
                "platform": {"os": o, "architecture": a},
            }
            for d, o, a in entries
        ],
    })


def _oras_handler(manifests=None, blobs=None, pull_files=None):
    """Build a ``_FakeRun`` handler dispatching on the oras subcommand.

    - ``oras manifest fetch <ref>`` -> ``manifests[ref]`` (JSON; default ``{}``)
    - ``oras blob fetch <ref> --output <p>`` -> writes ``blobs[ref]`` to ``<p>``
    - ``oras pull <ref> -o <dir>`` -> writes each of ``pull_files`` into ``<dir>``
    """
    manifests = manifests or {}
    blobs = blobs or {}
    pull_files = pull_files or {}

    def handler(cmd):
        if cmd[:3] == ["oras", "manifest", "fetch"]:
            return manifests.get(cmd[3], "{}")
        if cmd[:3] == ["oras", "blob", "fetch"]:
            data = blobs.get(cmd[3])
            if data is not None:
                Path(cmd[cmd.index("--output") + 1]).write_bytes(data)
            return ""
        if cmd[:2] == ["oras", "pull"]:
            d = Path(cmd[cmd.index("-o") + 1])
            d.mkdir(parents=True, exist_ok=True)
            for name, content in pull_files.items():
                (d / name).write_bytes(content)
            return "pulled"
        return ""

    return handler


# ── pull_oci_image ────────────────────────────────────────────────────────────


def _patch_run(fake):
    return patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake)


class TestPullOciImage:
    # ── boxman / oras artifact path (titled qcow2 layer) ──────────────────────

    def test_pull_artifact_finds_disk_qcow2(self, tmp_path: Path):
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _artifact_manifest(("disk.qcow2",))},
            pull_files={"disk.qcow2": b"qcow2", "vmimage.json": b"{}"}))
        with _patch_run(fake):
            qcow2 = pull_oci_image("oci://reg/repo:tag", str(tmp_path / "out"))
        assert qcow2.endswith("disk.qcow2")
        pulls = [c for c in fake.calls if c[:2] == ["oras", "pull"]]
        assert pulls and pulls[0][2] == "reg/repo:tag"  # oci:// scheme stripped

    def test_pull_artifact_falls_back_to_any_qcow2(self, tmp_path: Path):
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _artifact_manifest(("ubuntu-24.04.qcow2",))},
            pull_files={"ubuntu-24.04.qcow2": b"qcow2"}))
        with _patch_run(fake):
            qcow2 = pull_oci_image("reg/repo:tag", str(tmp_path / "out"))
        assert qcow2.endswith("ubuntu-24.04.qcow2")

    def test_pull_artifact_without_qcow2_raises(self, tmp_path: Path):
        # manifest advertises a qcow2 layer, but oras pull yields no qcow2
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _artifact_manifest(("disk.qcow2",))},
            pull_files={"readme.txt": b"no disk here"}))
        with _patch_run(fake):
            with pytest.raises(RuntimeError, match="no qcow2 was found"):
                pull_oci_image("reg/repo:tag", str(tmp_path / "out"))

    # ── container image / KubeVirt containerDisk path (embedded qcow2) ────────

    def test_pull_containerdisk_extracts_embedded_qcow2(self, tmp_path: Path):
        layer = _gztar({"disk/ubuntu-24.04.qcow2": b"QCOWDATA", "etc/hostname": b"h"})
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _image_manifest(["sha256:layer1"])},
            blobs={"reg/repo@sha256:layer1": layer}))
        with _patch_run(fake):
            qcow2 = pull_oci_image("reg/repo:tag", str(tmp_path / "out"))
        assert Path(qcow2).name == "ubuntu-24.04.qcow2"
        assert Path(qcow2).read_bytes() == b"QCOWDATA"
        blobs = [c for c in fake.calls if c[:3] == ["oras", "blob", "fetch"]]
        assert blobs and blobs[0][3] == "reg/repo@sha256:layer1"

    def test_pull_containerdisk_extracts_img_under_disk(self, tmp_path: Path):
        # KubeVirt containerDisks commonly name the disk *.img under /disk/
        layer = _gztar({"disk/ubuntu.img": b"IMGDISK", "etc/hostname": b"h"})
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _image_manifest(["sha256:l"])},
            blobs={"reg/repo@sha256:l": layer}))
        with _patch_run(fake):
            disk = pull_oci_image("reg/repo:tag", str(tmp_path / "out"))
        assert Path(disk).name == "ubuntu.img"
        assert Path(disk).read_bytes() == b"IMGDISK"

    def test_pull_containerdisk_disk_file_without_extension(self, tmp_path: Path):
        # the disk under /disk/ may have no extension at all
        layer = _gztar({"disk/downloaded": b"NOEXTDISK"})
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _image_manifest(["sha256:l"])},
            blobs={"reg/repo@sha256:l": layer}))
        with _patch_run(fake):
            disk = pull_oci_image("reg/repo:tag", str(tmp_path / "out"))
        assert Path(disk).name == "downloaded"
        assert Path(disk).read_bytes() == b"NOEXTDISK"

    def test_pull_containerdisk_skips_whiteout_marker(self, tmp_path: Path):
        # an overlay .wh. whiteout next to the real disk must be ignored
        layer = _gztar({"disk/.wh.old-cloudimg.img": b"", "disk/ubuntu.img": b"REAL"})
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _image_manifest(["sha256:l"])},
            blobs={"reg/repo@sha256:l": layer}))
        with _patch_run(fake):
            disk = pull_oci_image("reg/repo:tag", str(tmp_path / "out"))
        assert Path(disk).name == "ubuntu.img"
        assert Path(disk).read_bytes() == b"REAL"
        assert not (tmp_path / "out" / ".wh.old-cloudimg.img").exists()

    def test_pull_containerdisk_prefers_disk_dir_over_extension(self, tmp_path: Path):
        # a disk-extension file outside disk/ loses to a file under disk/
        layer = _gztar({"data/cloud.img": b"ELSEWHERE", "disk/ubuntu.img": b"REAL"})
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _image_manifest(["sha256:l"])},
            blobs={"reg/repo@sha256:l": layer}))
        with _patch_run(fake):
            disk = pull_oci_image("reg/repo:tag", str(tmp_path / "out"))
        assert Path(disk).read_bytes() == b"REAL"

    def test_pull_containerdisk_from_multiarch_index(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(oci_pull, "_host_arch", lambda: "amd64")
        layer = _gztar({"disk/disk.qcow2": b"AMD64DISK"})
        fake = _FakeRun(handler=_oras_handler(
            manifests={
                "reg/repo:tag": _index_manifest([
                    ("sha256:armmanifest", "linux", "arm64"),
                    ("sha256:amdmanifest", "linux", "amd64"),
                ]),
                "reg/repo@sha256:amdmanifest": _image_manifest(["sha256:amdlayer"]),
            },
            blobs={"reg/repo@sha256:amdlayer": layer}))
        with _patch_run(fake):
            qcow2 = pull_oci_image("reg/repo:tag", str(tmp_path / "out"))
        assert Path(qcow2).read_bytes() == b"AMD64DISK"
        # the amd64 platform manifest (not arm64) was resolved
        assert ["oras", "manifest", "fetch", "reg/repo@sha256:amdmanifest"] in fake.calls
        assert ["oras", "manifest", "fetch", "reg/repo@sha256:armmanifest"] not in fake.calls

    def test_pull_containerdisk_prefers_disk_dir(self, tmp_path: Path):
        # a root-level qcow2 AND a disk/*.qcow2 — the disk/ one must win
        layer = _gztar({"root.qcow2": b"ROOT", "disk/real.qcow2": b"DISK"})
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _image_manifest(["sha256:l"])},
            blobs={"reg/repo@sha256:l": layer}))
        with _patch_run(fake):
            qcow2 = pull_oci_image("reg/repo:tag", str(tmp_path / "out"))
        assert Path(qcow2).name == "real.qcow2"
        assert Path(qcow2).read_bytes() == b"DISK"
        # the non-preferred extraction is cleaned up
        assert not (tmp_path / "out" / "root.qcow2").exists()

    def test_pull_containerdisk_upper_layer_wins(self, tmp_path: Path):
        lower = _gztar({"disk/foo.qcow2": b"OLD"})
        upper = _gztar({"disk/foo.qcow2": b"NEW"})
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _image_manifest(["sha256:lower", "sha256:upper"])},
            blobs={"reg/repo@sha256:lower": lower, "reg/repo@sha256:upper": upper}))
        with _patch_run(fake):
            qcow2 = pull_oci_image("reg/repo:tag", str(tmp_path / "out"))
        assert Path(qcow2).read_bytes() == b"NEW"
        # layers are scanned top-down; the upper hit short-circuits the lower
        fetched = [c[3] for c in fake.calls if c[:3] == ["oras", "blob", "fetch"]]
        assert fetched == ["reg/repo@sha256:upper"]

    def test_pull_containerdisk_no_qcow2_raises(self, tmp_path: Path):
        layer = _gztar({"etc/hostname": b"host", "readme": b"x"})
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _image_manifest(["sha256:l"])},
            blobs={"reg/repo@sha256:l": layer}))
        with _patch_run(fake):
            with pytest.raises(RuntimeError, match="no VM disk"):
                pull_oci_image("reg/repo:tag", str(tmp_path / "out"))

    def test_pull_unreadable_layer_raises(self, tmp_path: Path):
        # a zstd-compressed (or otherwise non-tar) blob can't be read by tarfile
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _image_manifest(["sha256:l"])},
            blobs={"reg/repo@sha256:l": b"\x28\xb5\x2f\xfd not a tar"}))
        with _patch_run(fake):
            with pytest.raises(RuntimeError, match="zstd|could not read OCI layer"):
                pull_oci_image("reg/repo:tag", str(tmp_path / "out"))

    def test_pull_containerdisk_skips_symlink_member(self, tmp_path: Path):
        # a symlink named like a qcow2 must be ignored, not followed/recreated
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("disk/evil.qcow2")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        out_dir = tmp_path / "out"
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _image_manifest(["sha256:l"])},
            blobs={"reg/repo@sha256:l": buf.getvalue()}))
        with _patch_run(fake):
            with pytest.raises(RuntimeError, match="no VM disk"):
                pull_oci_image("reg/repo:tag", str(out_dir))
        assert not (out_dir / "evil.qcow2").exists()

    def test_pull_containerdisk_neutralizes_traversal_name(self, tmp_path: Path):
        # a regular member with a traversing name is written by basename only
        layer = _gztar({"../../../../etc/evil.qcow2": b"DISKDATA"})
        out_dir = tmp_path / "out"
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _image_manifest(["sha256:l"])},
            blobs={"reg/repo@sha256:l": layer}))
        with _patch_run(fake):
            qcow2 = pull_oci_image("reg/repo:tag", str(out_dir))
        assert Path(qcow2) == out_dir / "evil.qcow2"  # no traversal out of out_dir
        assert Path(qcow2).read_bytes() == b"DISKDATA"

    def test_pull_containerdisk_nested_index(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(oci_pull, "_host_arch", lambda: "amd64")
        layer = _gztar({"disk/d.qcow2": b"NESTED"})
        fake = _FakeRun(handler=_oras_handler(
            manifests={
                "reg/repo:tag": _index_manifest([("sha256:inner", "linux", "amd64")]),
                "reg/repo@sha256:inner": _index_manifest([("sha256:plat", "linux", "amd64")]),
                "reg/repo@sha256:plat": _image_manifest(["sha256:layer"]),
            },
            blobs={"reg/repo@sha256:layer": layer}))
        with _patch_run(fake):
            qcow2 = pull_oci_image("reg/repo:tag", str(tmp_path / "out"))
        assert Path(qcow2).read_bytes() == b"NESTED"

    def test_pull_index_without_host_arch_raises(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(oci_pull, "_host_arch", lambda: "amd64")
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": _index_manifest([("sha256:arm", "linux", "arm64")])}))
        with _patch_run(fake):
            with pytest.raises(RuntimeError, match="no manifest for linux/amd64"):
                pull_oci_image("reg/repo:tag", str(tmp_path / "out"))

    # ── error handling ────────────────────────────────────────────────────────

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
        # the first registry call is the manifest fetch, so a bad ref surfaces there
        fake = _FakeRun(returncode=1, stdout="", stderr="manifest unknown")
        with patch("boxman.providers.libvirt.oci_pull.subprocess.run", side_effect=fake):
            with pytest.raises(RuntimeError) as excinfo:
                pull_oci_image("reg/repo:tag", str(tmp_path / "out"))
        msg = str(excinfo.value)
        assert "oras manifest fetch failed" in msg
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

    def test_inspect_reports_kind_artifact(self):
        fake = _FakeRun(stdout=_artifact_manifest(("disk.qcow2",)))
        with _patch_run(fake):
            summary = inspect_oci_image("reg/repo:tag")
        assert summary["kind"] == "artifact"

    def test_inspect_reports_kind_image(self):
        fake = _FakeRun(stdout=_image_manifest(["sha256:l"]))
        with _patch_run(fake):
            summary = inspect_oci_image("reg/repo:tag")
        assert summary["kind"] == "image"

    def test_inspect_reports_kind_image_index(self):
        fake = _FakeRun(stdout=_index_manifest([("sha256:m", "linux", "amd64")]))
        with _patch_run(fake):
            summary = inspect_oci_image("reg/repo:tag")
        assert summary["kind"] == "image-index"
        assert summary["manifests"]  # index entries are surfaced

    def test_format_inspect_shows_kind_and_containerdisk_hint(self):
        text = format_inspect({
            "image_ref": "reg/repo:tag",
            "kind": "image-index",
            "layers": [],
            "manifests": [
                {"platform": "linux/amd64", "media_type": "", "size": 0, "digest": "d"},
            ],
        })
        assert "kind: image-index" in text
        assert "containerDisk" in text


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


# ── _select_index_digest / _host_arch / _resolve_image_manifest ───────────────


class TestSelectIndexDigest:
    def test_picks_host_arch(self, monkeypatch):
        monkeypatch.setattr(oci_pull, "_host_arch", lambda: "amd64")
        manifest = json.loads(_index_manifest([
            ("sha256:arm", "linux", "arm64"),
            ("sha256:amd", "linux", "amd64"),
        ]))
        assert oci_pull._select_index_digest(manifest) == "sha256:amd"

    def test_skips_attestation_unknown(self, monkeypatch):
        monkeypatch.setattr(oci_pull, "_host_arch", lambda: "amd64")
        manifest = {"manifests": [
            {"digest": "sha256:att", "platform": {"architecture": "unknown"}},
            {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
        ]}
        assert oci_pull._select_index_digest(manifest) == "sha256:amd"

    def test_no_host_arch_returns_none(self, monkeypatch):
        monkeypatch.setattr(oci_pull, "_host_arch", lambda: "amd64")
        manifest = json.loads(_index_manifest([("sha256:arm", "linux", "arm64")]))
        assert oci_pull._select_index_digest(manifest) is None

    def test_empty_index_returns_none(self):
        assert oci_pull._select_index_digest({"manifests": []}) is None


class TestHostArch:
    def test_known_arch_mapped(self, monkeypatch):
        monkeypatch.setattr(oci_pull.platform, "machine", lambda: "x86_64")
        assert oci_pull._host_arch() == "amd64"

    def test_unknown_arch_falls_back_to_raw(self, monkeypatch):
        # an unmapped machine must NOT masquerade as amd64
        monkeypatch.setattr(oci_pull.platform, "machine", lambda: "riscv64")
        assert oci_pull._host_arch() == "riscv64"


class TestResolveImageManifest:
    def test_nested_index_too_deep_raises(self, monkeypatch):
        monkeypatch.setattr(oci_pull, "_host_arch", lambda: "amd64")
        idx = _index_manifest([("sha256:x", "linux", "amd64")])
        # every resolved manifest is itself an index -> exceeds the depth cap
        fake = _FakeRun(handler=_oras_handler(
            manifests={"reg/repo:tag": idx, "reg/repo@sha256:x": idx}))
        with _patch_run(fake):
            with pytest.raises(RuntimeError, match="nests deeper"):
                oci_pull._resolve_image_manifest("reg/repo:tag", json.loads(idx))
