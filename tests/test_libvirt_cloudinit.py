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
    CloudInitTemplate, DEFAULT_USER_DATA, DEFAULT_META_DATA, DEFAULT_DONE_MARKER,
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


class TestCloudinitTimeouts:
    """The cloud-init verification caps are per-template knobs (conf.yml)."""

    SLEEP = "boxman.providers.libvirt.cloudinit.time.sleep"

    def test_defaults_match_previous_hardcoded_values(self, tmp_path: Path):
        t = _make_template(tmp_path)
        assert t.cloudinit_agent_timeout == 300
        assert t.cloudinit_guest_exec_timeout == 120
        assert t.cloudinit_done_timeout == 120
        assert t.cloudinit_fallback_timeout == 180

    def test_overrides_are_stored(self, tmp_path: Path):
        t = _make_template(
            tmp_path,
            cloudinit_done_timeout=900,
            cloudinit_guest_exec_timeout=180,
        )
        assert t.cloudinit_done_timeout == 900
        assert t.cloudinit_guest_exec_timeout == 180

    def test_a_quoted_timeout_is_coerced(self, tmp_path: Path):
        # yaml hands back "900" for a quoted value; left alone it reaches the
        # // in the wait loops and raises TypeError, long after virt-install
        # has already booted the VM
        t = _make_template(tmp_path, cloudinit_done_timeout="900")
        assert t.cloudinit_done_timeout == 900

    @pytest.mark.parametrize("value", [None, "abc", "", -5, [300]])
    def test_a_nonsense_timeout_is_rejected_up_front(self, tmp_path: Path, value):
        with pytest.raises(ValueError, match="cloudinit_done_timeout"):
            _make_template(tmp_path, cloudinit_done_timeout=value)

    def test_zero_means_skip_the_wait(self, tmp_path: Path):
        t = _make_template(tmp_path, cloudinit_fallback_timeout=0)
        assert t.cloudinit_fallback_timeout == 0

    def test_poll_done_marker_honours_configured_timeout(self, tmp_path: Path):
        t = _make_template(tmp_path, cloudinit_done_timeout=7)
        with patch.object(t.virsh, "execute_shell", return_value=_result(ok=False)), \
                patch(self.SLEEP) as sleep:
            assert t._poll_done_marker("/var/log/done") is False
        # back-off 1 + 2 + 4 == the 7s cap
        assert sum(c.args[0] for c in sleep.call_args_list) == 7

    def test_explicit_max_wait_still_wins(self, tmp_path: Path):
        t = _make_template(tmp_path, cloudinit_done_timeout=900)
        with patch.object(t.virsh, "execute_shell", return_value=_result(ok=False)), \
                patch(self.SLEEP) as sleep:
            assert t._poll_done_marker("/var/log/done", max_wait=3) is False
        assert sum(c.args[0] for c in sleep.call_args_list) == 3

    def test_fallback_wait_honours_configured_timeout(self, tmp_path: Path):
        t = _make_template(tmp_path, cloudinit_fallback_timeout=30)
        with patch.object(t.virsh, "execute_shell", return_value=_result(ok=True)), \
                patch(self.SLEEP) as sleep:
            t._wait_cloudinit_fallback()
        assert sleep.call_count == 3          # range(0, 30, 10)

    def test_agent_wait_loop_scales_with_timeout(self, tmp_path: Path):
        # agent never answers: 10s cap / 2s per probe == 5 guest-ping attempts,
        # then a zero-length blind wait, then the shutdown path
        t = _make_template(
            tmp_path, cloudinit_agent_timeout=10, cloudinit_fallback_timeout=0)
        with patch.object(t.virsh, "execute_shell",
                          return_value=_result(ok=False)) as shell, \
                patch.object(t.virsh, "execute",
                             return_value=_result(stdout="shut off")), \
                patch(self.SLEEP):
            assert t.verify_and_shutdown() is True
        assert shell.call_count == 5

    def test_guest_exec_loop_scales_with_timeout(self, tmp_path: Path):
        # agent answers guest-ping but guest-exec stays blacklisted:
        # 1 ping + (20s / 5s) == 4 guest-exec probes
        t = _make_template(
            tmp_path, cloudinit_guest_exec_timeout=20,
            cloudinit_fallback_timeout=0)
        calls: list[str] = []

        def fake_shell(cmd, **kwargs):
            calls.append(cmd)
            return _result(ok="guest-ping" in cmd)

        with patch.object(t.virsh, "execute_shell", side_effect=fake_shell), \
                patch.object(t.virsh, "execute",
                             return_value=_result(stdout="shut off")), \
                patch(self.SLEEP):
            assert t.verify_and_shutdown() is True
        assert sum("guest-exec" in c for c in calls) == 4


class TestVerificationFailureLeavesCleanState:
    """
    What is left behind when cloud-init cannot be verified.

    A template VM left *running* is worse than a failed one: the next
    ``create-templates`` reports "already exists", and ``up`` sees the domain,
    skips creation and virt-clones a running guest.
    """

    SLEEP = "boxman.providers.libvirt.cloudinit.time.sleep"

    @staticmethod
    def _template(tmp_path: Path, **overrides):
        t = _make_template(tmp_path, cloudinit_userdata="#cloud-config\n",
                           cloudinit_done_marker="/var/log/done", **overrides)
        calls: list = []

        def fake_execute(*args, **kwargs):
            calls.append(args)
            # domstate is asked whether the VM went down
            if args and args[0] == "domstate":
                return _result(stdout="shut off")
            return _result()

        def fake_shell(cmd, **kwargs):
            calls.append(("shell", cmd))
            # the agent answers, guest-exec works, the marker never appears
            return _result(ok=("guest-ping" in cmd or "guest-exec" in cmd),
                           stdout='{"return": {"pid": 1}}')

        t.virsh.execute = fake_execute
        t.virsh.execute_shell = fake_shell
        return t, calls

    def test_a_missing_marker_still_shuts_the_vm_down(self, tmp_path: Path):
        t, calls = self._template(tmp_path, cloudinit_done_timeout=1)
        with patch(self.SLEEP):
            assert t.verify_and_shutdown() is False
        assert any(args[0] == "shutdown" for args in calls if args and args[0] != "shell"), \
            "the template VM was left running"

    def test_no_marker_configured_is_not_a_hard_failure(self, tmp_path: Path):
        # nothing to check against is not the same as a failed check: falling
        # back to a blind wait keeps templates that predate the marker working
        t = _make_template(tmp_path, cloudinit_userdata="#cloud-config\n",
                           cloudinit_fallback_timeout=0)
        assert t.cloudinit_done_marker is None
        t.virsh.execute = lambda *a, **k: _result(stdout="shut off")
        t.virsh.execute_shell = lambda cmd, **k: _result(
            ok=("guest-ping" in cmd or "guest-exec" in cmd),
            stdout='{"return": {"pid": 1}}')
        with patch(self.SLEEP):
            assert t.verify_and_shutdown() is True

    def test_a_template_without_its_own_cloudinit_gets_the_default_marker(self, tmp_path: Path):
        # an implicit template synthesised from `base_image: oci://…` has
        # nowhere to declare one, but it runs DEFAULT_USER_DATA, whose last
        # runcmd entry appends to this file
        t = _make_template(tmp_path)
        assert t.cloudinit_done_marker == DEFAULT_DONE_MARKER

    def test_the_default_marker_is_written_last_not_early(self):
        # write_files runs in the config stage, before runcmd: a marker from
        # there would be found while the template was still being built
        from boxman.providers.libvirt.cloudinit_presets import DEFAULT_USER_DATA
        assert DEFAULT_DONE_MARKER not in DEFAULT_USER_DATA.split("runcmd:")[0]
        last_runcmd = DEFAULT_USER_DATA.rstrip().splitlines()[-1]
        assert DEFAULT_DONE_MARKER in last_runcmd, last_runcmd
