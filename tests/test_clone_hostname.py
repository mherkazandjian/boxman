"""
The guest hostname a clone is given (#200).

Covers the three places a name is decided or used: config validation, which
refuses a bad value before any clone exists; the manager's resolver, which
falls back to the VM key the same way the ssh alias does; and the clone pass,
whose invocation and guest-side rename script are exercised here -- the
script by actually running it against a directory standing in for the guest.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from tests import test_provision_boxes as boxes
from tests.conftest import make_bare_manager

from boxman.exceptions import CloneSanitizerError, ConfigError
from boxman.providers.libvirt.clone_vm import (
    CLONE_DEGRADATIONS_KEY,
    CLONE_GUEST_HOSTNAME_KEY,
    CLOUD_INIT_HOSTNAME_DROPIN,
    HOSTNAME_SCRIPT,
    CloneVM,
    render_hostname_script,
)

pytestmark = pytest.mark.unit


def _clone(tmp_path: Path, **info) -> CloneVM:
    return CloneVM(
        src_vm_name="template-base", new_vm_name="bprj__p__bprj_c1_vm1",
        info=info, workdir=str(tmp_path), provider_config={"use_sudo": False})


def _only_hostname(tmp_path: Path, name: str = "node01", **info) -> CloneVM:
    """A clone whose pass covers the hostname alone: no key generation."""
    return _clone(tmp_path, clone_machine_id="off", clone_ssh_host_keys="off",
                  **{CLONE_GUEST_HOSTNAME_KEY: name}, **info)


def _config(vms: dict) -> dict:
    return {"project": "p", "clusters": {"cluster_1": {"vms": vms}}}


# ---------------------------------------------------------------------------
# config validation -- before any clone exists
# ---------------------------------------------------------------------------

class TestValidateCloneIdentityConfig:

    def test_a_valid_config_passes(self):
        manager = make_bare_manager(_config({
            "node01": {"hostname": "node01"},
            "node02": {"hostname": "ctrl.example.com"},
            "node03": {},
        }))
        manager.validate_clone_identity_config()

    @pytest.mark.parametrize("hostname", [
        "-bad", "a" * 64, "a." * 126 + "ab", True, 42])
    def test_refuses_the_issues_invalid_values(self, hostname):
        manager = make_bare_manager(_config({"vm1": {"hostname": hostname}}))
        with pytest.raises(ConfigError, match=r"cluster_1\.vms\.vm1\.hostname"):
            manager.validate_clone_identity_config()

    def test_refuses_a_block_scalar_newline(self):
        """R2: `hostname: |` leaves a trailing newline in the value."""
        manager = make_bare_manager(_config({"vm1": {"hostname": "node01\n"}}))
        with pytest.raises(ConfigError, match=r"vm1\.hostname"):
            manager.validate_clone_identity_config()

    def test_names_every_offending_vm_at_once(self):
        manager = make_bare_manager(_config({
            "vm1": {"hostname": "-bad"},
            "vm2": {"hostname": True},
            "vm3": {"clone_hostname": "sometimes"},
        }))
        with pytest.raises(ConfigError) as caught:
            manager.validate_clone_identity_config()
        message = str(caught.value)
        assert "vm1.hostname" in message
        assert "vm2.hostname" in message
        assert "vm3.clone_hostname" in message

    @pytest.mark.parametrize("key", [
        "clone_machine_id", "clone_ssh_host_keys", "clone_hostname"])
    def test_refuses_an_invalid_policy_before_cloning(self, key):
        manager = make_bare_manager(_config({"vm1": {key: "strict"}}))
        with pytest.raises(ConfigError, match=key):
            manager.validate_clone_identity_config()

    def test_an_invalid_key_only_warns_under_auto(self):
        """A key like my_vm is a fine alias; a config working today must not break."""
        manager = make_bare_manager(_config({"my_vm": {}}))
        manager.validate_clone_identity_config()
        warnings = [c.args[0] for c in manager.logger.warning.call_args_list]
        assert any("my_vm" in w and "hostname:" in w for w in warnings)

    def test_an_invalid_key_is_an_error_under_required(self):
        manager = make_bare_manager(
            _config({"my_vm": {"clone_hostname": "required"}}))
        with pytest.raises(ConfigError, match="my_vm"):
            manager.validate_clone_identity_config()

    def test_an_invalid_key_is_ignored_under_off(self):
        manager = make_bare_manager(_config({"my_vm": {"clone_hostname": "off"}}))
        manager.validate_clone_identity_config()
        manager.logger.warning.assert_not_called()

    def test_a_declared_name_covers_an_invalid_key(self):
        manager = make_bare_manager(_config({"my_vm": {"hostname": "my-vm"}}))
        manager.validate_clone_identity_config()
        manager.logger.warning.assert_not_called()

    @pytest.mark.parametrize("boot_order", [["cdrom", "hd"], ["network", "hd"]])
    def test_direct_boot_vms_are_not_cloned_so_not_checked(self, boot_order):
        manager = make_bare_manager(_config({
            "vm1": {"boot_order": boot_order, "hostname": "not_valid"}}))
        manager.validate_clone_identity_config()


# ---------------------------------------------------------------------------
# the manager's resolver -- same fallback as the ssh alias
# ---------------------------------------------------------------------------

class TestGuestHostname:

    def test_a_declared_hostname_wins(self):
        manager = make_bare_manager({})
        assert manager.guest_hostname("vm1", {"hostname": "node01"}) == "node01"

    def test_falls_back_to_the_vm_key(self):
        manager = make_bare_manager({})
        assert manager.guest_hostname("vm1", {}) == "vm1"

    def test_an_explicit_null_reads_as_absent(self):
        manager = make_bare_manager({})
        assert manager.guest_hostname("vm1", {"hostname": None}) == "vm1"

    def test_a_key_that_is_not_a_hostname_yields_none(self):
        manager = make_bare_manager({})
        assert manager.guest_hostname("my_vm", {}) is None


class TestSshAliasOfANullHostname:
    """An explicit ``hostname: null`` names the ssh alias after the VM key.

    ``vm_info.get('hostname', vm_name)`` returns None for a present-but-null
    key, so two such VMs in one cluster both got the alias ``cluster_1_None``
    -- one alias for two hosts, while their guests were named vm1 and vm2.
    Each site that builds an alias is driven here, not just the shared rule.
    """

    @staticmethod
    def _manager(tmp_path: Path):
        manager = make_bare_manager({
            "project": "p",
            "workspace": {"path": str(tmp_path)},
            "clusters": {"cluster_1": {
                "vms": {"vm1": {"hostname": None}, "vm2": {"hostname": None}},
                "admin_pass": "secret",
                "ssh_config": "ssh_config",
            }},
        })
        session = MagicMock()
        session.get_vm_ip_addresses.side_effect = (
            lambda name: {"eth0": f"192.168.10.{name[-1]}"})
        manager.session_for_cluster = lambda cluster_name: session
        manager._docker_ssh_jump_stanza = lambda: None
        return manager

    def test_the_written_ssh_config(self, tmp_path):
        self._manager(tmp_path).write_ssh_config()

        hosts = [line.split()[1]
                 for line in (tmp_path / "ssh_config").read_text().splitlines()
                 if line.startswith("Host ")]
        assert hosts == ["cluster_1_vm1", "cluster_1_vm2"]

    def test_the_key_push(self, tmp_path):
        (tmp_path / "id_ed25519_boxman.pub").write_text("ssh-ed25519 AAAA\n")
        manager = self._manager(tmp_path)
        manager._try_add_ssh_key = MagicMock(return_value=True)

        assert manager.add_ssh_keys_to_vms() is True

        pushed = [c.kwargs["hostname"]
                  for c in manager._try_add_ssh_key.call_args_list]
        assert pushed == ["cluster_1_vm1", "cluster_1_vm2"]

    def test_the_connection_info(self, tmp_path):
        manager = self._manager(tmp_path)
        manager.connect_info()

        lines = [c.args[0] for c in manager.logger.status.call_args_list]
        assert "vm: vm1 (hostname: vm1)" in lines
        assert "vm: vm2 (hostname: vm2)" in lines


class TestIntegrationNameCheck:
    """The integration tier's stale-name check must not pass vacuously.

    It asks the guest's resolver whether the template's name still points
    at the guest. A guest without getent, or a lookup that fails for any
    reason other than "not found", used to read as "not found" -- a green
    result claiming a check that never ran.
    """

    CONFIG = {
        "workspace": {"path": "/workspace"},
        "clusters": {"c1": {"base_image": "tmpl", "vms": {"vm1": {}}}},
    }

    def _check(self, resolver_output: str) -> int:
        def ssh(ssh_config, host, command):
            if command == boxes.GUEST_NAME_PROBE:
                return SimpleNamespace(stdout="vm1\n")
            assert "getent hosts tmpl" in command
            return SimpleNamespace(stdout=resolver_output)

        with patch.object(boxes, "ssh_cmd", side_effect=ssh):
            return boxes.assert_guest_names(self.CONFIG)

    def test_a_name_that_no_longer_resolves_passes(self):
        assert self._check("getent-status=2\n") == 1

    def test_a_name_resolving_elsewhere_passes(self):
        assert self._check("192.168.10.9    tmpl\ngetent-status=0\n") == 1

    def test_a_name_resolving_to_the_guest_itself_fails(self):
        with pytest.raises(AssertionError, match="still resolves"):
            self._check("127.0.1.1       tmpl\ngetent-status=0\n")

    def test_a_guest_without_getent_fails(self):
        with pytest.raises(AssertionError, match="could not check"):
            self._check("getent-status=missing\n")

    def test_a_failed_lookup_fails(self):
        with pytest.raises(AssertionError, match="could not check"):
            self._check("getent-status=1\n")

    def test_no_status_at_all_fails(self):
        with pytest.raises(AssertionError, match="could not check"):
            self._check("")


# ---------------------------------------------------------------------------
# the clone pass
# ---------------------------------------------------------------------------

class TestHostnameInThePlan:

    def test_a_named_clone_plans_the_hostname_property(self, tmp_path):
        plan = _clone(tmp_path, **{CLONE_GUEST_HOSTNAME_KEY: "node01"}
                      ).build_identity_plan()
        assert [p.config_key for p in plan.properties][-1] == "clone_hostname"
        assert plan.hostname == "node01"
        assert plan.needs_customize is True

    def test_off_leaves_it_out(self, tmp_path):
        plan = _clone(tmp_path, clone_hostname="off",
                      **{CLONE_GUEST_HOSTNAME_KEY: "node01"}).build_identity_plan()
        assert plan.hostname is None
        assert "clone_hostname" not in [p.config_key for p in plan.properties]

    def test_no_usable_name_leaves_it_out_under_auto(self, tmp_path):
        plan = _clone(tmp_path, **{CLONE_GUEST_HOSTNAME_KEY: None}
                      ).build_identity_plan()
        assert plan.hostname is None

    def test_no_usable_name_is_a_config_error_under_required(self, tmp_path):
        with pytest.raises(ConfigError, match="clone_hostname=required"):
            _clone(tmp_path, clone_hostname="required",
                   **{CLONE_GUEST_HOSTNAME_KEY: None})

    def test_a_direct_caller_without_the_key_uses_hostname(self, tmp_path):
        """A provider caller outside the manager has no VM key to fall back on."""
        assert _clone(tmp_path, hostname="node01").guest_hostname == "node01"
        assert _clone(tmp_path).guest_hostname is None

    def test_an_invalid_value_never_reaches_the_guest(self, tmp_path):
        with pytest.raises(ConfigError, match="hostname"):
            _clone(tmp_path, **{CLONE_GUEST_HOSTNAME_KEY: "-bad"})


class TestHostnameInvocation:

    def _args(self, tmp_path, name):
        clone = _only_hostname(tmp_path, name)
        plan = clone.build_identity_plan()
        clone.stage_identity_plan(plan)
        try:
            args, kwargs = clone.build_sysprep_invocation(plan)
            script = Path(args[args.index("--run") + 1])
            return args, kwargs, script.read_text()
        finally:
            for staging in plan.staging_dirs:
                shutil.rmtree(staging, ignore_errors=True)

    def test_carries_exactly_the_resolved_name(self, tmp_path):
        args, kwargs, _script = self._args(tmp_path, "ctrl.example.com")
        assert args[args.index("--hostname") + 1] == "ctrl.example.com"
        assert kwargs["operations"] == "customize"

    def test_the_script_runs_before_hostname_rewrites_etc_hostname(self, tmp_path):
        """The script reads the template's name, which --hostname replaces."""
        args, _kwargs, _script = self._args(tmp_path, "node01")
        assert args.index("--run") < args.index("--hostname")

    def test_the_staged_script_is_the_rendered_one(self, tmp_path):
        args, _kwargs, script = self._args(tmp_path, "node01")
        assert script == render_hostname_script("node01")
        assert args[args.index("--run") + 1].endswith(f"/{HOSTNAME_SCRIPT}")

    def test_hostname_is_never_passed_without_a_value(self, tmp_path):
        plan = _clone(tmp_path, clone_hostname="off",
                      **{CLONE_GUEST_HOSTNAME_KEY: "node01"}).build_identity_plan()
        args, _kwargs = _clone(tmp_path).build_sysprep_invocation(plan)
        assert "--hostname" not in args

    def test_staging_is_removed_after_the_pass(self, tmp_path):
        clone = _only_hostname(tmp_path)
        with patch.object(clone.virt_sysprep, "execute") as execute:
            execute.return_value.ok = True
            clone.run_identity_pass(clone.build_identity_plan())
        staged = execute.call_args.args[execute.call_args.args.index("--run") + 1]
        assert not Path(staged).exists()


class TestHostnamePolicy:

    def test_auto_records_a_degradation_naming_the_hostname(self, tmp_path):
        clone = _clone(tmp_path, **{CLONE_GUEST_HOSTNAME_KEY: "node01"})
        collected: list = []
        clone.info[CLONE_DEGRADATIONS_KEY] = collected
        with patch.object(clone, "run_identity_pass",
                          side_effect=CloneSanitizerError("no libguestfs")):
            clone.apply_identity_policies()
        assert len(collected) == 1
        assert "hostname" in collected[0].properties
        assert "clone_hostname=auto" in collected[0].policies

    def test_required_fails_closed_and_discards_the_clone(self, tmp_path):
        clone = _clone(tmp_path, clone_hostname="required",
                       **{CLONE_GUEST_HOSTNAME_KEY: "node01"})
        with patch.object(clone, "run_identity_pass",
                          side_effect=CloneSanitizerError("no libguestfs")), \
             patch.object(clone, "discard_unsafe_clone") as discard:
            with pytest.raises(CloneSanitizerError):
                clone.apply_identity_policies()
        discard.assert_called_once()

    def test_off_never_invokes_the_pass_for_the_hostname_alone(self, tmp_path):
        clone = _clone(tmp_path, clone_machine_id="off", clone_ssh_host_keys="off",
                       clone_hostname="off", **{CLONE_GUEST_HOSTNAME_KEY: "node01"})
        with patch.object(clone, "run_identity_pass") as run:
            clone.apply_identity_policies()
        run.assert_not_called()


# ---------------------------------------------------------------------------
# the guest-side script, run for real against a directory
# ---------------------------------------------------------------------------

def _guest(tmp_path: Path, hostname: str, hosts: str | None,
           hosts_template: str | None = None, cloud: bool = False) -> Path:
    root = tmp_path / "guest"
    (root / "etc").mkdir(parents=True)
    (root / "etc/hostname").write_text(hostname + "\n")
    if hosts is not None:
        (root / "etc/hosts").write_text(hosts)
    if cloud or hosts_template is not None:
        (root / "etc/cloud").mkdir()
    if hosts_template is not None:
        (root / "etc/cloud/templates").mkdir()
        (root / "etc/cloud/templates/hosts.debian.tmpl").write_text(hosts_template)
    return root


def _run(root: Path, name: str) -> None:
    script = root.parent / HOSTNAME_SCRIPT
    script.write_text(render_hostname_script(name))
    script.chmod(0o755)
    result = subprocess.run(
        [str(script)], env={**os.environ, "BOXMAN_GUEST_ROOT": str(root)},
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def _run_raw(root: Path, name: str):
    script = root.parent / HOSTNAME_SCRIPT
    script.write_text(render_hostname_script(name))
    script.chmod(0o755)
    return subprocess.run(
        [str(script)], env={**os.environ, "BOXMAN_GUEST_ROOT": str(root)},
        capture_output=True, text=True, check=False)


def _hosts(root: Path) -> list[str]:
    return (root / "etc/hosts").read_text().splitlines()


class TestRenameScript:

    DEBIAN = ("127.0.1.1 hello-cloudinit.localdomain hello-cloudinit\n"
              "127.0.0.1 localhost\n\n"
              "# The following lines are desirable for IPv6 capable hosts\n"
              "::1 ip6-localhost ip6-loopback\n")
    REDHAT = ("127.0.0.1 hello-cloudinit.localdomain hello-cloudinit\n"
              "127.0.0.1 localhost.localdomain localhost\n"
              "::1 hello-cloudinit.localdomain hello-cloudinit\n"
              "::1 localhost6.localdomain6 localhost6\n"
              "10.0.0.5 repo.internal   # keep me\n")

    def test_debian_self_line_takes_the_clones_names(self, tmp_path):
        root = _guest(tmp_path, "hello-cloudinit", self.DEBIAN)
        _run(root, "node01.example.com")
        assert _hosts(root)[0] == "127.0.1.1 node01.example.com node01"
        # everything else is untouched, blank line and comment included
        assert _hosts(root)[1:] == self.DEBIAN.splitlines()[1:]

    def test_redhat_loopback_lines_are_renamed_on_both_families(self, tmp_path):
        root = _guest(tmp_path, "hello-cloudinit.localdomain", self.REDHAT)
        _run(root, "node01")
        assert _hosts(root) == [
            "127.0.0.1 node01",
            "127.0.0.1 localhost.localdomain localhost",
            "::1 node01",
            "::1 localhost6.localdomain6 localhost6",
            "10.0.0.5 repo.internal   # keep me",
        ]

    def test_no_line_resolves_the_template_name_afterwards(self, tmp_path):
        """The issue's point: a clone must not resolve its old name to itself."""
        for hosts, old in ((self.DEBIAN, "hello-cloudinit"),
                           (self.REDHAT, "hello-cloudinit.localdomain")):
            root = _guest(tmp_path / old, old, hosts)
            _run(root, "node01")
            assert "hello-cloudinit" not in (root / "etc/hosts").read_text()

    def test_the_real_ubuntu_templates_self_line(self, tmp_path):
        """Found on a real guest: cloud-init took the fqdn from the metadata's
        local-hostname (the template's name) and the short name from user-data,
        so the two are unrelated and only one matches /etc/hostname."""
        hosts = ("127.0.1.1 ubuntu-24.04-minimal-base-template-cloudinit "
                 "hello-cloudinit\n127.0.0.1 localhost\n"
                 "::1 localhost ip6-localhost ip6-loopback\n"
                 "ff02::1 ip6-allnodes\n")
        root = _guest(tmp_path, "hello-cloudinit", hosts)
        _run(root, "node01.example.com")
        assert _hosts(root) == [
            "127.0.1.1 node01.example.com node01",
            "127.0.0.1 localhost",
            "::1 localhost ip6-localhost ip6-loopback",
            "ff02::1 ip6-allnodes",
        ]

    def test_127_0_1_1_is_a_self_line_even_when_the_name_drifted(self, tmp_path):
        """/etc/hostname no longer matches, but 127.0.1.1 is still its own name."""
        root = _guest(tmp_path, "renamed-by-hand",
                      "127.0.1.1 template.example template\n127.0.0.1 localhost\n")
        _run(root, "node01")
        assert _hosts(root) == ["127.0.1.1 node01", "127.0.0.1 localhost"]

    def test_a_line_that_does_not_name_the_template_is_untouched(self, tmp_path):
        hosts = "127.0.0.1 localhost dev-proxy\n127.0.1.1 oldbox\n"
        root = _guest(tmp_path, "oldbox", hosts)
        _run(root, "node01")
        assert _hosts(root) == ["127.0.0.1 localhost dev-proxy", "127.0.1.1 node01"]

    def test_a_template_named_localhost_never_loses_localhost(self, tmp_path):
        hosts = "127.0.0.1 localhost localhost.localdomain\n::1 localhost\n"
        root = _guest(tmp_path, "localhost.localdomain", hosts)
        _run(root, "node01")
        assert _hosts(root) == [
            "127.0.0.1 localhost localhost.localdomain",
            "::1 localhost",
            "127.0.1.1 node01",
        ]

    def test_a_mixed_line_is_renamed_in_place(self, tmp_path):
        root = _guest(tmp_path, "oldbox", "127.0.0.1 localhost oldbox  # mixed\n")
        _run(root, "node01")
        assert _hosts(root) == ["127.0.0.1 localhost node01 # mixed"]

    def test_running_twice_changes_nothing_more(self, tmp_path):
        root = _guest(tmp_path, "hello-cloudinit", self.DEBIAN)
        _run(root, "node01")
        first = (root / "etc/hosts").read_text()
        (root / "etc/hostname").write_text("node01\n")
        _run(root, "node01")
        assert (root / "etc/hosts").read_text() == first

    def test_a_guest_without_etc_hosts_is_left_without_one(self, tmp_path):
        root = _guest(tmp_path, "oldbox", None)
        _run(root, "node01")
        assert not (root / "etc/hosts").exists()

    def test_no_cloud_init_means_no_drop_in(self, tmp_path):
        root = _guest(tmp_path, "oldbox", "127.0.0.1 localhost\n")
        _run(root, "node01")
        assert not (root / "etc/cloud").exists()

    def test_cloud_init_gets_a_drop_in_that_keeps_the_name(self, tmp_path):
        root = _guest(tmp_path, "oldbox", "127.0.0.1 localhost\n", cloud=True)
        _run(root, "ctrl.example.com")
        dropin = (root / CLOUD_INIT_HOSTNAME_DROPIN.lstrip("/")).read_text()
        assert "preserve_hostname: true" in dropin
        assert "hostname: 'ctrl'" in dropin
        assert "fqdn: 'ctrl.example.com'" in dropin

    def test_a_failed_hosts_rewrite_fails_the_script(self, tmp_path):
        """R1: a write failure must reach the clone policy, not report 0."""
        root = _guest(tmp_path, "oldbox", "127.0.1.1 oldbox\n")
        (root / "etc").chmod(0o555)          # no scratch file can be created
        try:
            result = _run_raw(root, "node01")
        finally:
            (root / "etc").chmod(0o755)
        assert result.returncode != 0
        assert (root / "etc/hosts").read_text() == "127.0.1.1 oldbox\n"

    def test_a_symlinked_hosts_file_is_rewritten_through_the_link(self, tmp_path):
        """R1: the link stays a link, its target gets the new names, and no
        fixed scratch name can alias the source."""
        root = _guest(tmp_path, "oldbox", None)
        (root / "etc/hosts.boxman").write_text("127.0.1.1 oldbox\n")
        (root / "etc/hosts").symlink_to("hosts.boxman")
        _run(root, "node01")
        assert (root / "etc/hosts").is_symlink()
        assert (root / "etc/hosts.boxman").read_text() == "127.0.1.1 node01\n"

    def test_hosts_keeps_its_mode(self, tmp_path):
        root = _guest(tmp_path, "oldbox", "127.0.1.1 oldbox\n")
        (root / "etc/hosts").chmod(0o640)
        _run(root, "node01")
        assert (root / "etc/hosts").stat().st_mode & 0o777 == 0o640

    def test_no_scratch_file_is_left_behind(self, tmp_path):
        root = _guest(tmp_path, "oldbox", "127.0.1.1 oldbox\n", cloud=True)
        (root / "etc/cloud/cloud.cfg").write_text(
            "cloud_init_modules:\n - update_etc_hosts\n")
        _run(root, "node01")
        leftovers = [p.name for p in (root / "etc").rglob("*")
                     if "boxman" in p.name and p.name != "99-boxman-hostname.cfg"]
        assert leftovers == []

    def test_a_failed_drop_in_write_fails_the_script(self, tmp_path):
        root = _guest(tmp_path, "oldbox", "127.0.0.1 localhost\n", cloud=True)
        (root / "etc/cloud/cloud.cfg.d").write_text("not a directory")
        assert _run_raw(root, "node01").returncode != 0

    @pytest.mark.parametrize("name", ["no", "123", "null", "1.2", "on"])
    def test_the_drop_in_values_are_strings(self, tmp_path, name):
        """R3: an unquoted `hostname: no` is YAML false, and cloud-init would
        call .split('.') on it."""
        import yaml
        root = _guest(tmp_path, "oldbox", "127.0.0.1 localhost\n", cloud=True)
        _run(root, name)
        data = yaml.safe_load(
            (root / CLOUD_INIT_HOSTNAME_DROPIN.lstrip("/")).read_text())
        assert data["hostname"] == name.split(".")[0]
        assert data["fqdn"] == name
        assert data["preserve_hostname"] is True

    def test_crlf_self_lines_are_renamed_and_keep_their_endings(self, tmp_path):
        """R4: a CR left on the last name hid the template's name."""
        root = _guest(tmp_path, "oldbox", None)
        (root / "etc/hosts").write_bytes(b"::1 oldbox\r\n127.0.0.1 localhost\r\n")
        _run(root, "node01")
        assert (root / "etc/hosts").read_bytes() == (
            b"::1 node01\r\n127.0.0.1 localhost\r\n")

    @pytest.mark.parametrize("address", ["0:0:0:0:0:0:0:1", "::0001", "0::1"])
    def test_expanded_ipv6_loopback_spellings_are_loopback(self, tmp_path, address):
        """R4: only the abbreviated ::1 was recognised."""
        root = _guest(tmp_path, "oldbox", f"{address} oldbox\n")
        _run(root, "node01")
        assert _hosts(root) == [f"{address} node01"]

    @pytest.mark.parametrize("entry", [
        " - update_etc_hosts",
        "  - update-etc-hosts",
        " - [update_etc_hosts, always]",
        " - [ update_etc_hosts ]",
        ' - "update_etc_hosts"',
    ])
    def test_cloud_init_stops_managing_etc_hosts(self, tmp_path, entry):
        """R7: every manage_etc_hosts mode goes through update_etc_hosts, and
        user-data outranks any drop-in -- so the clone's module list drops it.
        The file must stay valid YAML, with every other module untouched."""
        import yaml
        cfg = ("cloud_init_modules:\n - seed_random\n" + entry +
               "\n - ca_certs\ncloud_config_modules:\n - runcmd\n")
        root = _guest(tmp_path, "oldbox", "127.0.0.1 localhost\n", cloud=True)
        (root / "etc/cloud/cloud.cfg").write_text(cfg)
        _run(root, "node01")
        data = yaml.safe_load((root / "etc/cloud/cloud.cfg").read_text())
        flat = [m if isinstance(m, str) else m[0] for m in data["cloud_init_modules"]]
        assert flat == ["seed_random", "ca_certs"]
        assert data["cloud_config_modules"] == ["runcmd"]

    def test_hosts_templates_are_no_longer_edited(self, tmp_path):
        """R7 replaces the template edit: with the module off, it is moot."""
        template = "## template:jinja\n127.0.1.1 {{fqdn}} {{hostname}}\n"
        root = _guest(tmp_path, "oldbox", "127.0.0.1 localhost\n", template)
        _run(root, "node01")
        assert (root / "etc/cloud/templates/hosts.debian.tmpl").read_text() == template

    def test_a_trailing_newline_never_reaches_the_script(self):
        """R2, at the provider boundary."""
        with pytest.raises(ConfigError):
            render_hostname_script("node01\n")

    def test_refuses_to_render_an_unvalidated_name(self):
        with pytest.raises(ConfigError, match="refusing"):
            render_hostname_script("evil'; rm -rf /; '")
