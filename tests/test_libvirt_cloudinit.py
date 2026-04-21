"""
Unit tests for boxman.providers.libvirt.cloudinit.CloudInitTemplate.

Focus: image-path resolution, password hashing, VM-exists probe,
nocloud directory layout, and the build_seed_iso tool fallback chain.

The larger create_template / verify_and_shutdown flows are not covered
at unit level — they are orchestration and belong to integration tests.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.cloudinit import (
    CloudInitTemplate, DEFAULT_USER_DATA, DEFAULT_META_DATA,
)


pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True, stderr: str = "", return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


def _make_template(tmp_path: Path, **overrides) -> CloudInitTemplate:
    defaults = dict(
        template_name="ubuntu-template",
        image_path=str(tmp_path / "base.qcow2"),
        workdir=str(tmp_path / "workdir"),
        provider_config={"use_sudo": False, "uri": "qemu:///system"},
    )
    defaults.update(overrides)
    return CloudInitTemplate(**defaults)


class TestResolveImagePath:

    def test_strips_file_scheme(self):
        assert CloudInitTemplate._resolve_image_path("file:///var/img.qcow2") == "/var/img.qcow2"

    def test_leaves_http_unchanged(self):
        url = "http://example.com/img.qcow2"
        assert CloudInitTemplate._resolve_image_path(url) == url

    def test_leaves_https_unchanged(self):
        url = "https://example.com/img.qcow2"
        assert CloudInitTemplate._resolve_image_path(url) == url

    def test_expands_tilde(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/testuser")
        out = CloudInitTemplate._resolve_image_path("~/images/base.qcow2")
        assert out == "/home/testuser/images/base.qcow2"


class TestHashPassword:

    def test_returns_sha512_crypt(self):
        h = CloudInitTemplate.hash_password("hunter2")
        # SHA-512 crypt hashes start with $6$
        assert h.startswith("$6$")
        assert "hunter2" not in h

    def test_same_password_different_salts(self):
        # salt is random → two hashes of the same password must differ
        h1 = CloudInitTemplate.hash_password("same")
        h2 = CloudInitTemplate.hash_password("same")
        assert h1 != h2


class TestCheckVmExists:

    def test_true_when_name_in_list(self, tmp_path: Path):
        t = _make_template(tmp_path)
        with patch.object(t.virsh, "execute",
                          return_value=_result(stdout="other\nubuntu-template\n")):
            assert t._check_vm_exists() is True

    def test_false_when_absent(self, tmp_path: Path):
        t = _make_template(tmp_path)
        with patch.object(t.virsh, "execute",
                          return_value=_result(stdout="other\n")):
            assert t._check_vm_exists() is False

    def test_false_on_exec_failure(self, tmp_path: Path):
        t = _make_template(tmp_path)
        with patch.object(t.virsh, "execute", return_value=_result(ok=False)):
            assert t._check_vm_exists() is False


class TestPrepareNocloudDir:

    def test_creates_nocloud_dir_with_default_user_data(self, tmp_path: Path):
        t = _make_template(tmp_path)
        nocloud = t.prepare_nocloud_dir(str(tmp_path))
        assert Path(nocloud).is_dir()
        user_data = Path(nocloud) / "user-data"
        meta_data = Path(nocloud) / "meta-data"
        assert user_data.exists()
        assert meta_data.exists()
        assert user_data.read_text().startswith("#cloud-config")

    def test_meta_data_contains_template_name(self, tmp_path: Path):
        t = _make_template(tmp_path, template_name="custom-vm")
        nocloud = t.prepare_nocloud_dir(str(tmp_path))
        text = (Path(nocloud) / "meta-data").read_text()
        assert "instance-id: custom-vm-001" in text
        assert "local-hostname: custom-vm" in text

    def test_disabled_network_config_skipped(self, tmp_path: Path):
        t = _make_template(
            tmp_path, cloudinit_network_config="disabled",
        )
        nocloud = t.prepare_nocloud_dir(str(tmp_path))
        assert not (Path(nocloud) / "network-config").exists()

    def test_custom_network_config_written(self, tmp_path: Path):
        custom = "version: 2\nethernets:\n  eno1:\n    dhcp4: true\n"
        t = _make_template(tmp_path, cloudinit_network_config=custom)
        nocloud = t.prepare_nocloud_dir(str(tmp_path))
        assert (Path(nocloud) / "network-config").read_text() == custom

    def test_env_var_placeholder_expanded(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CI_PASSWORD", "s3cret")
        ud = "#cloud-config\npassword: ${env:CI_PASSWORD}\n"
        t = _make_template(tmp_path, cloudinit_userdata=ud)
        nocloud = t.prepare_nocloud_dir(str(tmp_path))
        rendered = (Path(nocloud) / "user-data").read_text()
        assert "password: s3cret" in rendered
        assert "${env:" not in rendered

    def test_hash_placeholder_replaced_with_crypt_hash(self, tmp_path: Path):
        ud = "#cloud-config\npassword: ${hash:hunter2}\n"
        t = _make_template(tmp_path, cloudinit_userdata=ud)
        nocloud = t.prepare_nocloud_dir(str(tmp_path))
        rendered = (Path(nocloud) / "user-data").read_text()
        # Raw password must not appear in the output
        assert "hunter2" not in rendered
        # SHA-512 crypt hash quoted for YAML safety
        assert "password: '$6$" in rendered


class TestBuildSeedIso:

    def test_cloud_localds_success_returns_true(self, tmp_path: Path):
        t = _make_template(tmp_path)
        (tmp_path / "nocloud").mkdir()
        with patch.object(t.virsh, "execute_shell",
                          return_value=_result(ok=True)) as shell:
            ok = t.build_seed_iso(str(tmp_path / "nocloud"), str(tmp_path / "seed.iso"))
        assert ok is True
        shell.assert_called_once()
        assert "cloud-localds" in shell.call_args.args[0]

    def test_falls_back_to_genisoimage(self, tmp_path: Path):
        t = _make_template(tmp_path)
        (tmp_path / "nocloud").mkdir()
        calls = []

        def fake(cmd, *_a, **_kw):
            calls.append(cmd)
            # only genisoimage succeeds; cloud-localds fails
            return _result(ok=("genisoimage" in cmd))

        with patch.object(t.virsh, "execute_shell", side_effect=fake):
            ok = t.build_seed_iso(str(tmp_path / "nocloud"), str(tmp_path / "seed.iso"))
        assert ok is True
        assert any("cloud-localds" in c for c in calls)
        assert any("genisoimage" in c for c in calls)

    def test_all_tools_fail_returns_false(self, tmp_path: Path):
        t = _make_template(tmp_path)
        (tmp_path / "nocloud").mkdir()
        with patch.object(t.virsh, "execute_shell",
                          return_value=_result(ok=False, stderr="nope")):
            ok = t.build_seed_iso(str(tmp_path / "nocloud"), str(tmp_path / "seed.iso"))
        assert ok is False

    def test_includes_network_config_flag_when_present(self, tmp_path: Path):
        t = _make_template(tmp_path)
        nocloud = tmp_path / "nocloud"
        nocloud.mkdir()
        (nocloud / "network-config").write_text("version: 2\n")
        with patch.object(t.virsh, "execute_shell",
                          return_value=_result(ok=True)) as shell:
            t.build_seed_iso(str(nocloud), str(tmp_path / "seed.iso"))
        cmd = shell.call_args.args[0]
        assert "--network-config=" in cmd


class TestDefaultTemplates:

    def test_default_user_data_is_cloud_config(self):
        rendered = DEFAULT_USER_DATA.format(hostname="demo")
        assert rendered.startswith("#cloud-config")
        assert "hostname: demo" in rendered

    def test_default_meta_data_contains_placeholders(self):
        rendered = DEFAULT_META_DATA.format(instance_id="abc", hostname="demo")
        assert "instance-id: abc" in rendered
        assert "local-hostname: demo" in rendered
