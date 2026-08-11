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
import shlex
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
        with patch.object(t.virsh, "execute", return_value=_result(ok=False)), \
                patch(self.SLEEP) as sleep:
            assert t._poll_done_marker("/var/log/done") is False
        # back-off 1 + 2 + 4 == the 7s cap
        assert sum(c.args[0] for c in sleep.call_args_list) == 7

    def test_explicit_max_wait_still_wins(self, tmp_path: Path):
        t = _make_template(tmp_path, cloudinit_done_timeout=900)
        with patch.object(t.virsh, "execute", return_value=_result(ok=False)), \
                patch(self.SLEEP) as sleep:
            assert t._poll_done_marker("/var/log/done", max_wait=3) is False
        assert sum(c.args[0] for c in sleep.call_args_list) == 3

    def test_fallback_wait_honours_configured_timeout(self, tmp_path: Path):
        t = _make_template(tmp_path, cloudinit_fallback_timeout=30)
        with patch.object(t.virsh, "execute", return_value=_result(ok=True)), \
                patch(self.SLEEP) as sleep:
            t._wait_cloudinit_fallback()
        assert sleep.call_count == 3          # range(0, 30, 10)

    def test_agent_wait_loop_scales_with_timeout(self, tmp_path: Path):
        # agent never answers: 10s cap / 2s per probe == 5 guest-ping attempts,
        # then a zero-length blind wait, then the shutdown path
        t = _make_template(
            tmp_path, cloudinit_agent_timeout=10, cloudinit_fallback_timeout=0)
        pings = 0

        def fake_execute(*args, **kwargs):
            nonlocal pings
            if args and args[0] == "qemu-agent-command":
                pings += 1
                return _result(ok=False)
            return _result(stdout="shut off")

        with patch.object(t.virsh, "execute", side_effect=fake_execute), \
                patch(self.SLEEP):
            assert t.verify_and_shutdown() is True
        assert pings == 5

    def test_guest_exec_loop_scales_with_timeout(self, tmp_path: Path):
        # agent answers guest-ping but guest-exec stays blacklisted:
        # 1 ping + (20s / 5s) == 4 guest-exec probes
        t = _make_template(
            tmp_path, cloudinit_guest_exec_timeout=20,
            cloudinit_fallback_timeout=0)
        exec_probes = 0

        def fake_execute(*args, **kwargs):
            nonlocal exec_probes
            if args and args[0] == "qemu-agent-command":
                payload = args[2]
                if "guest-exec" in payload:
                    exec_probes += 1
                    return _result(ok=False)
                return _result(ok=True)  # guest-ping
            return _result(stdout="shut off")

        with patch.object(t.virsh, "execute", side_effect=fake_execute), \
                patch(self.SLEEP):
            assert t.verify_and_shutdown() is True
        assert exec_probes == 4


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
            if args and args[0] == "qemu-agent-command":
                payload = args[2]
                # the agent answers, guest-exec works, the marker never appears
                return _result(ok=("guest-ping" in payload or "guest-exec" in payload),
                               stdout='{"return": {"pid": 1}}')
            return _result()

        t.virsh.execute = fake_execute
        return t, calls

    def test_a_missing_marker_still_shuts_the_vm_down(self, tmp_path: Path):
        t, calls = self._template(tmp_path, cloudinit_done_timeout=1)
        with patch(self.SLEEP):
            assert t.verify_and_shutdown() is False
        assert any(args[0] == "shutdown" for args in calls), \
            "the template VM was left running"

    def test_no_marker_configured_is_not_a_hard_failure(self, tmp_path: Path):
        # nothing to check against is not the same as a failed check: falling
        # back to a blind wait keeps templates that predate the marker working
        t = _make_template(tmp_path, cloudinit_userdata="#cloud-config\n",
                           cloudinit_fallback_timeout=0)
        assert t.cloudinit_done_marker is None

        def fake_execute(*args, **kwargs):
            if args and args[0] == "qemu-agent-command":
                return _result(ok=True)
            return _result(stdout="shut off")

        t.virsh.execute = fake_execute
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


class TestAgentCommand:
    """
    Guest-agent calls must go through VirshCommand (#85 item 5).

    A raw ``virsh qemu-agent-command …`` shell string ignores the configured
    ``uri`` (e.g. qemu+ssh://…) and the ``virsh_cmd`` override, so template
    creation with a non-default URI polled the wrong libvirt daemon.
    """

    SHELL_RUN = "boxman.providers.libvirt.commands._shell_run"

    def _run_capture(self, t: CloudInitTemplate, payload: str) -> str:
        with patch(self.SHELL_RUN, return_value=_result()) as run:
            t._agent_command(payload)
        return run.call_args.args[0]

    def test_custom_uri_is_used(self, tmp_path: Path):
        t = _make_template(tmp_path, provider_config={
            "use_sudo": False,
            "uri": "qemu+ssh://root@example.com/system",
        })
        cmd = self._run_capture(t, '{"execute":"guest-ping"}')
        assert "-c qemu+ssh://root@example.com/system" in cmd

    def test_virsh_cmd_override_is_used(self, tmp_path: Path):
        t = _make_template(tmp_path, provider_config={
            "use_sudo": False,
            "virsh_cmd": "/usr/local/bin/virsh",
        })
        cmd = self._run_capture(t, '{"execute":"guest-ping"}')
        assert cmd.startswith("/usr/local/bin/virsh ")

    def test_sudo_is_applied(self, tmp_path: Path):
        t = _make_template(tmp_path, provider_config={"use_sudo": True})
        cmd = self._run_capture(t, '{"execute":"guest-ping"}')
        assert cmd.startswith("sudo virsh -c qemu:///system ")

    def test_payload_is_a_single_quoted_token(self, tmp_path: Path):
        t = _make_template(tmp_path)
        cmd = self._run_capture(t, '{"execute":"guest-ping"}')
        assert "qemu-agent-command ubuntu-template " in cmd
        assert cmd.endswith('\'{"execute":"guest-ping"}\'')

    def test_verify_and_shutdown_routes_pings_through_virsh(self, tmp_path: Path):
        # the polling loop must call virsh.execute("qemu-agent-command", …),
        # not a raw shell string
        t = _make_template(
            tmp_path, cloudinit_agent_timeout=2, cloudinit_fallback_timeout=0,
            cloudinit_userdata="#cloud-config\n",  # no implicit done marker
        )
        with patch.object(t.virsh, "execute",
                          return_value=_result(stdout="shut off")) as ex, \
                patch("boxman.providers.libvirt.cloudinit.time.sleep"):
            assert t.verify_and_shutdown() is True
        assert ex.call_args_list[0].args[0] == "qemu-agent-command"


class TestCreateTemplateSafeguards:
    """
    Safeguards in create_template (#85 item 26):

    - ``--force`` must wait for the old domain to shut off before undefine,
      and must abort when undefine fails (otherwise the virt-install below
      dies with "domain already exists").
    - A libvirt network without DHCP must abort before virt-install instead
      of burning the full guest-agent timeout.
    """

    SLEEP = "boxman.providers.libvirt.cloudinit.time.sleep"
    SHELL_RUN = "boxman.providers.libvirt.cloudinit._shell_run"

    @staticmethod
    def _stub_success_path(t: CloudInitTemplate) -> None:
        """Stub out everything past the VM-exists / DHCP gates."""
        t._resolve_bridge = MagicMock(return_value="virbr0")
        t._verify_dhcp_on_network = MagicMock(return_value=True)
        t.copy_base_image = MagicMock(return_value=True)
        t.prepare_nocloud_dir = MagicMock(return_value="/nocloud")
        t.build_seed_iso = MagicMock(return_value=True)
        t.verify_and_shutdown = MagicMock(return_value=True)

    def test_force_recreate_waits_for_shut_off_before_undefine(self, tmp_path: Path):
        t = _make_template(tmp_path)
        self._stub_success_path(t)
        t._check_vm_exists = MagicMock(return_value=True)
        calls: list[str] = []
        states = iter(["running", "running", "shut off"])

        def fake_execute(cmd, *args, **kwargs):
            calls.append(cmd)
            if cmd == "domstate":
                return _result(stdout=next(states, "shut off"))
            return _result()

        with patch.object(t.virsh, "execute", side_effect=fake_execute), \
                patch(self.SLEEP), \
                patch(self.SHELL_RUN, return_value=_result()):
            assert t.create_template(force=True) is True
        assert calls.index("destroy") < calls.index("domstate") < calls.index("undefine")
        # two "running" polls + one "shut off" before undefine
        assert calls[:calls.index("undefine")].count("domstate") == 3

    def test_force_recreate_aborts_when_undefine_fails(self, tmp_path: Path):
        t = _make_template(tmp_path)
        self._stub_success_path(t)
        t._check_vm_exists = MagicMock(return_value=True)

        def fake_execute(cmd, *args, **kwargs):
            if cmd == "domstate":
                return _result(stdout="shut off")
            if cmd == "undefine":
                return _result(ok=False, stderr="domain is still active")
            return _result()

        with patch.object(t.virsh, "execute", side_effect=fake_execute), \
                patch(self.SLEEP):
            assert t.create_template(force=True) is False
        t.copy_base_image.assert_not_called()

    def test_force_recreate_aborts_when_vm_stays_running(self, tmp_path: Path):
        t = _make_template(tmp_path)
        self._stub_success_path(t)
        t._check_vm_exists = MagicMock(return_value=True)
        calls: list[str] = []

        def fake_execute(cmd, *args, **kwargs):
            calls.append(cmd)
            if cmd == "domstate":
                return _result(stdout="running")
            return _result()

        with patch.object(t.virsh, "execute", side_effect=fake_execute), \
                patch(self.SLEEP):
            assert t.create_template(force=True) is False
        assert calls.count("domstate") == 30  # polled until the cap, then gave up
        assert "undefine" not in calls

    def test_create_template_aborts_when_dhcp_missing(self, tmp_path: Path):
        t = _make_template(tmp_path)
        self._stub_success_path(t)
        t._check_vm_exists = MagicMock(return_value=False)
        t._verify_dhcp_on_network = MagicMock(return_value=False)
        with patch.object(t.virsh, "execute", return_value=_result()):
            assert t.create_template() is False
        t.copy_base_image.assert_not_called()

    def test_explicit_bridge_skips_the_dhcp_check(self, tmp_path: Path):
        t = _make_template(tmp_path, bridge="virbr0")
        self._stub_success_path(t)
        t._check_vm_exists = MagicMock(return_value=False)
        with patch.object(t.virsh, "execute", return_value=_result()), \
                patch(self.SHELL_RUN, return_value=_result()):
            assert t.create_template() is True
        t._verify_dhcp_on_network.assert_not_called()


class TestShellQuoting:
    """
    Paths and names interpolated into shell command strings must be
    shlex-quoted (#85 item 6): a space or quote in a workdir, ISO path or
    template name must not split or break the command.
    """

    SHELL_RUN = "boxman.providers.libvirt.cloudinit._shell_run"

    def test_seed_iso_paths_are_quoted(self, tmp_path: Path):
        t = _make_template(tmp_path)
        nocloud = tmp_path / "no cloud"
        nocloud.mkdir()
        seed = str(tmp_path / "se'ed.iso")
        with patch.object(t.virsh, "execute_shell",
                          return_value=_result(ok=True)) as shell:
            assert t.build_seed_iso(str(nocloud), seed) is True
        cmd = shell.call_args.args[0]
        assert shlex.quote(seed) in cmd
        assert shlex.quote(str(nocloud / "user-data")) in cmd
        assert shlex.quote(str(nocloud / "meta-data")) in cmd

    def test_seed_iso_fallback_paths_are_quoted(self, tmp_path: Path):
        t = _make_template(tmp_path)
        nocloud = tmp_path / "no cloud"
        nocloud.mkdir()
        calls: list[str] = []

        def fake(cmd, *_a, **_kw):
            calls.append(cmd)
            return _result(ok="genisoimage" in cmd)  # cloud-localds fails

        with patch.object(t.virsh, "execute_shell", side_effect=fake):
            assert t.build_seed_iso(str(nocloud), str(tmp_path / "s.iso")) is True
        genisoimage = next(c for c in calls if "genisoimage" in c)
        assert shlex.quote(str(nocloud / "user-data")) in genisoimage

    def test_rsync_paths_are_quoted(self, tmp_path: Path):
        t = _make_template(tmp_path)
        with patch.object(t.virsh, "execute_shell",
                          return_value=_result(ok=True)) as shell:
            assert t._copy_local("/tmp/a b.qcow2", "/tmp/c d.qcow2") is True
        cmd = shell.call_args.args[0]
        assert shlex.quote("/tmp/a b.qcow2") in cmd
        assert shlex.quote("/tmp/c d.qcow2") in cmd

    def test_wget_command_quotes_dst_and_url(self, tmp_path: Path):
        t = _make_template(tmp_path)
        with patch(self.SHELL_RUN, return_value=_result(ok=True)) as run, \
                patch("boxman.providers.libvirt.cloudinit.os.path.isfile",
                      return_value=True), \
                patch("boxman.providers.libvirt.cloudinit.os.path.getsize",
                      return_value=10):
            assert t._download_image(
                "http://x/y iso.qcow2", "/tmp/d st.img") is True
        cmd = run.call_args.args[0]
        assert cmd.startswith("wget ")
        assert f"-O {shlex.quote('/tmp/d st.img')}" in cmd
        assert shlex.quote("http://x/y iso.qcow2") in cmd

    def test_curl_command_quotes_dst_and_url(self, tmp_path: Path):
        t = _make_template(tmp_path)
        calls: list[str] = []

        def fake_run(cmd, **_kw):
            calls.append(cmd)
            return _result(ok="curl" in cmd)

        with patch(self.SHELL_RUN, side_effect=fake_run), \
                patch("boxman.providers.libvirt.cloudinit.os.path.isfile",
                      return_value=True), \
                patch("boxman.providers.libvirt.cloudinit.os.path.getsize",
                      return_value=10):
            assert t._download_image(
                "http://x/y iso.qcow2", "/tmp/d st.img") is True
        curl = next(c for c in calls if c.startswith("curl "))
        assert f"-o {shlex.quote('/tmp/d st.img')}" in curl
        assert shlex.quote("http://x/y iso.qcow2") in curl

    def test_virt_install_command_quotes_name_and_paths(self, tmp_path: Path):
        t = _make_template(tmp_path, template_name="my tmpl")
        t._check_vm_exists = MagicMock(return_value=False)
        t._resolve_bridge = MagicMock(return_value="virbr0")
        t._verify_dhcp_on_network = MagicMock(return_value=True)
        t.copy_base_image = MagicMock(return_value=True)
        t.prepare_nocloud_dir = MagicMock(return_value="/nocloud")
        t.build_seed_iso = MagicMock(return_value=True)
        t.verify_and_shutdown = MagicMock(return_value=True)
        with patch.object(t.virsh, "execute", return_value=_result()), \
                patch(self.SHELL_RUN, return_value=_result()) as run:
            assert t.create_template() is True
        cmd = run.call_args.args[0]
        assert "--name='my tmpl'" in cmd
        dst_image = os.path.join(t.workdir, "my tmpl", "my tmpl.qcow2")
        assert f"path={shlex.quote(dst_image)}" in cmd
        seed = os.path.join(t.workdir, "my tmpl", "seed.iso")
        assert f"path={shlex.quote(seed)}" in cmd
