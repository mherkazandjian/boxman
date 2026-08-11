"""
Test that --boxman-conf overrides the default ~/.config/boxman/boxman.yml.
"""

import logging
import os
import tempfile
import yaml
import pytest
import shutil as _shutil

from boxman.scripts.app import load_boxman_config
from boxman.manager import BoxmanManager
from boxman.exceptions import ConfigError


class TestBoxmanConfOverride:

    def test_custom_boxman_conf_is_loaded(self, tmp_path):
        """Verify that load_boxman_config reads from the specified path,
        not from ~/.config/boxman/boxman.yml."""
        custom_config = {
            "ssh": {
                "authorized_keys": ["ssh-ed25519 AAAA_TEST_KEY test@boxman"]
            },
            "providers": {
                "libvirt": {
                    "uri": "qemu+tcp://custom-host/system",
                    "use_sudo": False,
                    "verbose": False,
                }
            },
        }

        conf_path = tmp_path / "custom_boxman.yml"
        conf_path.write_text(yaml.dump(custom_config))

        loaded = load_boxman_config(str(conf_path))

        assert loaded == custom_config
        assert loaded["providers"]["libvirt"]["uri"] == "qemu+tcp://custom-host/system"
        assert loaded["ssh"]["authorized_keys"] == [
            "ssh-ed25519 AAAA_TEST_KEY test@boxman"
        ]

    def test_custom_conf_does_not_read_default(self, tmp_path):
        """Ensure values come from the custom file, not the default location."""
        sentinel = "BOXMAN_TEST_SENTINEL_VALUE"
        custom_config = {
            "providers": {
                "libvirt": {
                    "uri": sentinel,
                }
            },
        }

        conf_path = tmp_path / "boxman_sentinel.yml"
        conf_path.write_text(yaml.dump(custom_config))

        loaded = load_boxman_config(str(conf_path))

        assert loaded["providers"]["libvirt"]["uri"] == sentinel

        # cross-check: the default config (if it exists) should not contain
        # our sentinel
        default_path = os.path.expanduser("~/.config/boxman/boxman.yml")
        if os.path.isfile(default_path):
            default_loaded = load_boxman_config(default_path)
            default_uri = (
                default_loaded
                .get("providers", {})
                .get("libvirt", {})
                .get("uri", "")
            )
            assert default_uri != sentinel, (
                "default config unexpectedly contains the sentinel value"
            )

    def test_missing_conf_raises(self):
        """Verify that a non-existent *custom* config path raises an error."""
        with pytest.raises(FileNotFoundError):
            load_boxman_config("/nonexistent/path/boxman.yml")

    def test_default_conf_created_when_missing(self, tmp_path, monkeypatch):
        """When the default ~/.config/boxman/boxman.yml is missing it is
        created automatically with sensible defaults."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        default_path = fake_home / ".config" / "boxman" / "boxman.yml"
        assert not default_path.exists()

        loaded = load_boxman_config(str(default_path))

        # file should now exist on disk
        assert default_path.exists()

        # verify defaults
        assert loaded["providers"]["libvirt"]["use_sudo"] is False
        assert loaded["providers"]["libvirt"]["verbose"] is False
        assert loaded["runtime"] == "local"

        # paths should be the system paths (or bare command names)
        virsh_cmd = loaded["providers"]["libvirt"]["virsh_cmd"]
        assert virsh_cmd == (_shutil.which("virsh") or "virsh")

    def test_global_authorized_keys_resolves_literal(self):
        """Literal SSH keys are returned as-is."""
        mgr = BoxmanManager()
        mgr.app_config = {
            "ssh": {
                "authorized_keys": [
                    "ssh-ed25519 AAAA_LITERAL literal@host",
                ]
            }
        }
        keys = mgr.get_global_authorized_keys()
        assert keys == ["ssh-ed25519 AAAA_LITERAL literal@host"]

    def test_global_authorized_keys_resolves_env(self, monkeypatch):
        """${env:BOXMAN_SSH_PUBKEY} is resolved from the environment."""
        monkeypatch.setenv("BOXMAN_SSH_PUBKEY", "ssh-ed25519 AAAA_FROM_ENV env@host")
        mgr = BoxmanManager()
        mgr.app_config = {
            "ssh": {
                "authorized_keys": ["${env:BOXMAN_SSH_PUBKEY}"]
            }
        }
        keys = mgr.get_global_authorized_keys()
        assert keys == ["ssh-ed25519 AAAA_FROM_ENV env@host"]

    def test_global_authorized_keys_resolves_file(self, tmp_path):
        """file:// references are resolved from the filesystem."""
        pub_key_file = tmp_path / "test_key.pub"
        pub_key_file.write_text("ssh-ed25519 AAAA_FROM_FILE file@host\n")
        mgr = BoxmanManager()
        mgr.app_config = {
            "ssh": {
                "authorized_keys": [f"file://{pub_key_file}"]
            }
        }
        keys = mgr.get_global_authorized_keys()
        assert keys == ["ssh-ed25519 AAAA_FROM_FILE file@host"]

    def test_global_authorized_keys_skips_unresolvable(self):
        """Unresolvable entries are skipped with a warning."""
        mgr = BoxmanManager()
        mgr.app_config = {
            "ssh": {
                "authorized_keys": [
                    "${env:BOXMAN_NONEXISTENT_VAR_12345}",
                    "ssh-ed25519 AAAA_GOOD good@host",
                ]
            }
        }
        keys = mgr.get_global_authorized_keys()
        assert keys == ["ssh-ed25519 AAAA_GOOD good@host"]

    def test_global_authorized_keys_mixed_formats(self, tmp_path, monkeypatch):
        """Literal strings, file refs, and env vars are all resolved together."""
        monkeypatch.setenv("BOXMAN_TEST_KEY", "ssh-ed25519 AAAA_ENV env@host")

        pub_file = tmp_path / "extra.pub"
        pub_file.write_text("ssh-rsa BBBB_FILE file@host\n")

        mgr = BoxmanManager()
        mgr.app_config = {
            "ssh": {
                "authorized_keys": [
                    "ssh-ed25519 AAAA_LITERAL literal@host",
                    "${env:BOXMAN_TEST_KEY}",
                    f"file://{pub_file}",
                ]
            }
        }
        keys = mgr.get_global_authorized_keys()
        assert keys == [
            "ssh-ed25519 AAAA_LITERAL literal@host",
            "ssh-ed25519 AAAA_ENV env@host",
            "ssh-rsa BBBB_FILE file@host",
        ]

    def test_runtime_defaults_to_local_when_absent(self, tmp_path):
        """When boxman.yml has no 'runtime' key, it defaults to 'local'."""
        custom_config = {"providers": {"libvirt": {"uri": "qemu:///system"}}}
        conf_path = tmp_path / "boxman_no_runtime.yml"
        conf_path.write_text(yaml.dump(custom_config))

        loaded = load_boxman_config(str(conf_path))
        assert loaded.get("runtime", "local") == "local"

    def test_runtime_docker_compose_is_loaded(self, tmp_path):
        """When boxman.yml sets runtime: docker, it is read correctly."""
        custom_config = {
            "runtime": "docker",
            "runtime_config": {
                "runtime_container": "my-test-container",
            },
            "providers": {"libvirt": {"uri": "qemu:///system"}},
        }
        conf_path = tmp_path / "boxman_docker_rt.yml"
        conf_path.write_text(yaml.dump(custom_config))

        loaded = load_boxman_config(str(conf_path))
        assert loaded["runtime"] == "docker"
        assert loaded["runtime_config"]["runtime_container"] == "my-test-container"

    def test_project_use_sudo_takes_precedence(self):
        """Project-level use_sudo: False must not be overridden by app-level use_sudo: True."""
        from boxman.providers.libvirt.session import LibVirtSession

        # Simulate project config with use_sudo: False
        project_config = {
            "project": "test_proj",
            "provider": {
                "libvirt": {
                    "uri": "qemu:///system",
                    "use_sudo": False,
                    "verbose": True,
                }
            },
        }

        session = LibVirtSession(config=project_config)
        assert session.provider_config["use_sudo"] is False
        assert session.use_sudo is False

        # Simulate manager with app config that has use_sudo: True
        mgr = BoxmanManager()
        mgr._runtime_name = "docker-compose"
        mgr.config = project_config
        mgr.app_config = {
            "runtime": "docker-compose",
            "runtime_config": {"runtime_container": "test-ctr"},
            "providers": {"libvirt": {"use_sudo": True}},
        }

        session.manager = mgr
        session.update_provider_config_with_runtime()

        # Project-level use_sudo: False must win
        assert session.provider_config["use_sudo"] is False
        # But runtime keys should be injected
        assert session.provider_config["runtime"] == "docker-compose"
        assert session.provider_config["runtime_container"] == "test-ctr"


class TestConfigurableOutputPaths:
    """Test that workspace.ansible_config, workspace.env_file, and
    workspace.inventory control where generated files are written."""

    @staticmethod
    def _base_config(workspace_path, **ws_extras):
        """Return a minimal config with one cluster and one VM."""
        ws = {"path": workspace_path}
        ws.update(ws_extras)
        return {
            "project": "test_proj",
            "workspace": ws,
            "clusters": {
                "mycluster": {
                    "vms": {"vm1": {"memory": 1024}},
                }
            },
        }

    def test_default_paths_unchanged(self, tmp_path):
        """Without custom keys, files use the default relative keys."""
        ws_path = str(tmp_path / "ws")
        config = self._base_config(ws_path)

        mgr = BoxmanManager()
        mgr.config = config
        mgr.resolve_workspace_defaults()

        ws_files = config["workspace"]["files"]
        assert "env.sh" in ws_files
        assert "inventory/01-hosts.yml" in ws_files
        assert "ansible.cfg" in ws_files

    def test_custom_ansible_config_path(self, tmp_path):
        """workspace.ansible_config redirects the ansible.cfg output key."""
        ws_path = str(tmp_path / "ws")
        custom_cfg = str(tmp_path / "shared" / "ansible.cfg")
        config = self._base_config(ws_path, ansible_config=custom_cfg)

        mgr = BoxmanManager()
        mgr.config = config
        mgr.resolve_workspace_defaults()

        ws_files = config["workspace"]["files"]
        assert custom_cfg in ws_files
        assert "ansible.cfg" not in ws_files

    def test_custom_env_file_path(self, tmp_path):
        """workspace.env_file redirects the env.sh output key."""
        ws_path = str(tmp_path / "ws")
        custom_env = str(tmp_path / "shared" / "env.sh")
        config = self._base_config(ws_path, env_file=custom_env)

        mgr = BoxmanManager()
        mgr.config = config
        mgr.resolve_workspace_defaults()

        ws_files = config["workspace"]["files"]
        assert custom_env in ws_files
        assert "env.sh" not in ws_files

    def test_custom_inventory_path(self, tmp_path):
        """workspace.inventory redirects inventory output to dir/01-hosts.yml."""
        ws_path = str(tmp_path / "ws")
        custom_inv = str(tmp_path / "shared" / "inventory")
        config = self._base_config(ws_path, inventory=custom_inv)

        mgr = BoxmanManager()
        mgr.config = config
        mgr.resolve_workspace_defaults()

        ws_files = config["workspace"]["files"]
        expected_key = os.path.join(custom_inv, "01-hosts.yml")
        assert expected_key in ws_files
        assert "inventory/01-hosts.yml" not in ws_files

    def test_custom_env_file_content_uses_custom_paths(self, tmp_path):
        """When custom paths are set, env.sh content references them."""
        ws_path = str(tmp_path / "ws")
        custom_inv = "../shared/inventory"
        custom_cfg = "../shared/ansible.cfg"
        config = self._base_config(
            ws_path, inventory=custom_inv, ansible_config=custom_cfg
        )

        mgr = BoxmanManager()
        mgr.config = config
        mgr.resolve_workspace_defaults()

        ws_files = config["workspace"]["files"]
        # env.sh uses default key when env_file is not set
        env_content = ws_files["env.sh"]
        assert f"INVENTORY={custom_inv}" in env_content
        assert f"ANSIBLE_CONFIG={custom_cfg}" in env_content

    def test_relative_custom_path_resolved_against_workspace(self, tmp_path):
        """A relative workspace.ansible_config is resolved against workspace.path."""
        ws_path = str(tmp_path / "ws")
        config = self._base_config(ws_path, ansible_config="../shared/ansible.cfg")

        mgr = BoxmanManager()
        mgr.config = config
        mgr.resolve_workspace_defaults()

        ws_files = config["workspace"]["files"]
        expected = os.path.normpath(os.path.join(ws_path, "../shared/ansible.cfg"))
        assert expected in ws_files


class TestRuntimeCollisionPrompt:
    """Tests for the .boxman-runtime sentinel + cross-runtime prompt."""

    def test_no_prompt_on_matching_sentinel(self, tmp_path, monkeypatch):
        wd = tmp_path / "workdir"
        wd.mkdir()
        (wd / BoxmanManager.RUNTIME_SENTINEL_FILENAME).write_text(
            "docker-compose\n")
        (wd / "env.sh").write_text("export X=1\n")

        mgr = BoxmanManager()

        def _fail_prompt(*_a, **_kw):
            raise AssertionError("no prompt expected when sentinel matches")

        monkeypatch.setattr("builtins.input", _fail_prompt)
        assert mgr._prompt_workdir_runtime_collision(
            str(wd), "docker-compose") == str(wd)

    def test_prompt_fires_on_mismatched_sentinel(self, tmp_path, monkeypatch):
        wd = tmp_path / "workdir"
        wd.mkdir()
        (wd / BoxmanManager.RUNTIME_SENTINEL_FILENAME).write_text("local\n")
        (wd / "env.sh").write_text("export X=1\n")

        mgr = BoxmanManager()
        monkeypatch.setattr("builtins.input", lambda *_: "y")

        result = mgr._prompt_workdir_runtime_collision(
            str(wd), "docker-compose")
        assert result == f"{wd}-docker-runtime"

    def test_prompt_no_keeps_original_path(self, tmp_path, monkeypatch):
        wd = tmp_path / "workdir"
        wd.mkdir()
        (wd / BoxmanManager.RUNTIME_SENTINEL_FILENAME).write_text("local\n")
        (wd / "env.sh").write_text("export X=1\n")

        mgr = BoxmanManager()
        monkeypatch.setattr("builtins.input", lambda *_: "n")

        result = mgr._prompt_workdir_runtime_collision(
            str(wd), "docker-compose")
        assert result == str(wd)

    def test_missing_sentinel_with_contents_prompts(self, tmp_path, monkeypatch):
        """Legacy workdirs (no sentinel) with provisioning artifacts should
        also trigger the prompt — they were created before sentinels existed."""
        wd = tmp_path / "workdir"
        wd.mkdir()
        (wd / "env.sh").write_text("legacy\n")

        mgr = BoxmanManager()
        monkeypatch.setattr("builtins.input", lambda *_: "y")

        assert mgr._prompt_workdir_runtime_collision(
            str(wd), "docker-compose") == f"{wd}-docker-runtime"

    def test_empty_dir_is_not_a_collision(self, tmp_path, monkeypatch):
        """An existing-but-empty directory has nothing to conflict with."""
        wd = tmp_path / "workdir"
        wd.mkdir()

        mgr = BoxmanManager()

        def _fail_prompt(*_a, **_kw):
            raise AssertionError("empty dir should not prompt")

        monkeypatch.setattr("builtins.input", _fail_prompt)
        assert mgr._prompt_workdir_runtime_collision(
            str(wd), "docker-compose") == str(wd)

    def test_nonexistent_dir_is_not_a_collision(self, tmp_path, monkeypatch):
        mgr = BoxmanManager()

        def _fail_prompt(*_a, **_kw):
            raise AssertionError("missing dir should not prompt")

        monkeypatch.setattr("builtins.input", _fail_prompt)
        missing = str(tmp_path / "not-there")
        assert mgr._prompt_workdir_runtime_collision(
            missing, "docker-compose") == missing

    def test_write_sentinel_records_runtime_name(self, tmp_path):
        wd = tmp_path / "workdir"
        wd.mkdir()
        mgr = BoxmanManager()

        mgr._write_runtime_sentinel(str(wd), "docker-compose")

        sentinel = wd / BoxmanManager.RUNTIME_SENTINEL_FILENAME
        assert sentinel.read_text().strip() == "docker-compose"

    def test_write_sentinel_noop_on_missing_dir(self, tmp_path):
        """Writing to a non-existent dir is a silent no-op."""
        mgr = BoxmanManager()
        mgr._write_runtime_sentinel(
            str(tmp_path / "not-there"), "docker-compose")  # must not raise

    def test_reconcile_rewrites_workspace_and_derived_clusters(
        self, tmp_path, monkeypatch
    ):
        """When the user accepts the suffix on workspace.path, derived
        cluster workdirs should be rewritten to track the new root."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / BoxmanManager.RUNTIME_SENTINEL_FILENAME).write_text("local\n")

        mgr = BoxmanManager()
        mgr.config = {
            "workspace": {"path": str(ws)},
            "clusters": {
                "cluster_1": {"workdir": str(ws / "cluster_1")},
                "cluster_2": {"workdir": "/elsewhere/independent"},
            },
        }
        monkeypatch.setattr("builtins.input", lambda *_: "y")

        mgr.reconcile_workdirs_with_runtime("docker-compose")

        new_ws = f"{ws}-docker-runtime"
        assert mgr.config["workspace"]["path"] == new_ws
        assert mgr.config["clusters"]["cluster_1"]["workdir"] == \
            f"{new_ws}/cluster_1"
        # cluster_2 lives outside workspace.path — untouched (and its
        # path doesn't exist on disk, so no further prompt fires).
        assert mgr.config["clusters"]["cluster_2"]["workdir"] == \
            "/elsewhere/independent"

    def test_reconcile_is_noop_for_local_runtime(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / BoxmanManager.RUNTIME_SENTINEL_FILENAME).write_text(
            "docker-compose\n")

        mgr = BoxmanManager()
        mgr.config = {"workspace": {"path": str(ws)}}

        def _fail_prompt(*_a, **_kw):
            raise AssertionError("local runtime must not prompt")

        monkeypatch.setattr("builtins.input", _fail_prompt)
        mgr.reconcile_workdirs_with_runtime("local")
        assert mgr.config["workspace"]["path"] == str(ws)

    def test_reconcile_rewrites_template_workdir(self, tmp_path, monkeypatch):
        tpl_wd = tmp_path / "tpl"
        tpl_wd.mkdir()
        (tpl_wd / BoxmanManager.RUNTIME_SENTINEL_FILENAME).write_text(
            "local\n")
        (tpl_wd / "some.img").write_bytes(b"")

        mgr = BoxmanManager()
        mgr.config = {
            "templates": {
                "t1": {"workdir": str(tpl_wd), "name": "t1"},
            },
        }
        monkeypatch.setattr("builtins.input", lambda *_: "y")

        mgr.reconcile_workdirs_with_runtime("docker-compose")

        assert mgr.config["templates"]["t1"]["workdir"] == \
            f"{tpl_wd}-docker-runtime"


class TestCollectWorkdirs:

    def test_collects_cluster_and_template_workdirs(self, tmp_path):
        mgr = BoxmanManager()
        mgr.config = {
            "clusters": {
                "c1": {"workdir": str(tmp_path / "c1")},
                "c2": {"workdir": str(tmp_path / "c2")},
            },
            "templates": {
                "t1": {"workdir": str(tmp_path / "tpl1")},
                "t2": {},  # falls back to default
            },
        }

        dirs = mgr.collect_workdirs()

        assert str(tmp_path / "c1") in dirs
        assert str(tmp_path / "c2") in dirs
        assert str(tmp_path / "tpl1") in dirs
        default_tpl = os.path.abspath(
            os.path.expanduser("~/boxman-templates"))
        assert default_tpl in dirs

    def test_returns_empty_when_no_clusters_or_templates(self):
        mgr = BoxmanManager()
        mgr.config = {}
        assert mgr.collect_workdirs() == []

    def test_deduplicates(self, tmp_path):
        mgr = BoxmanManager()
        shared = str(tmp_path / "shared")
        mgr.config = {
            "clusters": {
                "c1": {"workdir": shared},
                "c2": {"workdir": shared},
            },
        }
        assert mgr.collect_workdirs() == [shared]


class TestWriteSshConfig:
    """Tests for BoxmanManager.write_ssh_config covering both the
    local and docker (ProxyJump) runtime output formats."""

    @staticmethod
    def _make_manager(tmp_path, runtime_name="local"):
        from unittest.mock import MagicMock
        mgr = BoxmanManager()
        mgr.config = {
            "project": "test-proj",
            "workspace": {"path": str(tmp_path)},
            "clusters": {
                "cluster_1": {
                    "workdir": str(tmp_path / "cluster_1"),
                    "admin_user": "admin",
                    "admin_key_name": "id_ed25519_boxman",
                    "ssh_config": "ssh_config",
                    "vms": {
                        "vm1": {"hostname": "vm1"},
                        "vm2": {"hostname": "vm2"},
                    },
                },
            },
        }
        mgr._runtime_name = runtime_name
        # Stub provider.get_vm_ip_addresses so write_ssh_config gets IPs.
        mgr.provider = MagicMock()
        mgr.provider.get_vm_ip_addresses.side_effect = (
            lambda full_name: {"vnet0": "192.168.11.91"}
            if full_name.endswith("_vm1")
            else {"vnet0": "192.168.11.92"}
        )
        return mgr

    def test_local_runtime_emits_no_jump_stanza(self, tmp_path):
        mgr = self._make_manager(tmp_path, runtime_name="local")
        mgr.write_ssh_config()

        content = (tmp_path / "ssh_config").read_text()
        assert "Host boxman-libvirt-jump" not in content
        assert "ProxyJump" not in content
        # Regression: VM entries still look like they used to.
        assert "Host cluster_1_vm1" in content
        assert "Hostname 192.168.11.91" in content
        assert "IdentityFile" in content

    def test_docker_runtime_emits_jump_stanza_and_proxyjump(self, tmp_path):
        from unittest.mock import PropertyMock, patch
        from boxman.runtime.docker_compose import DockerComposeRuntime

        mgr = self._make_manager(tmp_path, runtime_name="docker-compose")
        # Wire a real DockerComposeRuntime instance whose ssh_port /
        # ssh_identity_path are stubbed — we don't want to touch the
        # per-project hash here.
        rt = DockerComposeRuntime(
            config={"project_name": "test-proj"}
        )
        mgr._runtime_instance = rt

        with patch.object(
            DockerComposeRuntime, "ssh_port",
            new_callable=PropertyMock, return_value=2678,
        ), patch.object(
            DockerComposeRuntime, "ssh_identity_path",
            new_callable=PropertyMock,
            return_value="/abs/path/.boxman/runtime/docker/data/ssh/id_ed25519",
        ):
            mgr.write_ssh_config()

        content = (tmp_path / "ssh_config").read_text()
        # Jump host appears exactly once, before the VM blocks.
        assert content.count("Host boxman-libvirt-jump") == 1
        assert "Port         2678" in content
        assert "User         qemu_user" in content
        assert (
            "IdentityFile /abs/path/.boxman/runtime/docker/data/ssh/id_ed25519"
            in content
        )
        # Every VM block has ProxyJump pointing at the jump alias.
        assert content.count("ProxyJump boxman-libvirt-jump") == 2
        # VM entries still contain the libvirt-network IP as HostName —
        # ProxyJump lets OpenSSH reach it.
        assert "Hostname 192.168.11.91" in content
        assert "Hostname 192.168.11.92" in content
        # Ordering: jump host stanza appears before either VM block.
        jump_idx = content.index("Host boxman-libvirt-jump")
        vm_idx = content.index("Host cluster_1_vm1")
        assert jump_idx < vm_idx


class TestDockerComposeSshPort:
    """Tests for the ssh_port / ssh_identity_path properties added so
    write_ssh_config can emit a ProxyJump stanza without re-deriving
    the per-project port offset."""

    def test_default_instance_uses_2222(self):
        from boxman.runtime.docker_compose import DockerComposeRuntime
        rt = DockerComposeRuntime()
        assert rt.ssh_port == 2222

    def test_per_project_port_is_deterministic_and_offset(self):
        from boxman.runtime.docker_compose import DockerComposeRuntime
        rt1 = DockerComposeRuntime(config={"project_name": "alpha"})
        rt2 = DockerComposeRuntime(config={"project_name": "alpha"})
        rt3 = DockerComposeRuntime(config={"project_name": "beta"})
        assert rt1.ssh_port == rt2.ssh_port
        assert rt1.ssh_port != 2222
        assert 2222 < rt1.ssh_port < 2222 + 1000
        # Different project names almost always hash to different
        # offsets (collision chance is ~1/1000).
        assert rt1.ssh_port != rt3.ssh_port

    def test_ssh_identity_path_lives_under_project_runtime_dir(self, tmp_path):
        from boxman.runtime.docker_compose import DockerComposeRuntime
        rt = DockerComposeRuntime(
            config={"project_name": "proj", "project_dir": str(tmp_path)},
        )
        rt.project_dir = str(tmp_path)
        expected_suffix = os.path.join(
            ".boxman", "runtime", "docker", "data", "ssh", "id_ed25519",
        )
        assert rt.ssh_identity_path.endswith(expected_suffix)
        assert str(tmp_path) in rt.ssh_identity_path


class TestNormalizeOwnership:
    """Tests for BoxmanManager._normalize_ownership — the helper that
    fixes stale root-owned entries inside otherwise-user-writable
    workdirs without escalating to sudo when not strictly needed."""

    def test_clean_user_owned_tree_is_a_noop(self, tmp_path, monkeypatch):
        """No foreign entries → no unlink, no sudo, nothing logged."""
        from unittest.mock import patch
        (tmp_path / "a.txt").write_text("hi")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("hi")

        mgr = BoxmanManager()

        run_calls: list = []

        def _capture_run(cmd, *_a, **_kw):
            run_calls.append(cmd)
            from unittest.mock import MagicMock
            return MagicMock(ok=True, stderr="", stdout="")

        monkeypatch.setattr("boxman.manager.run", _capture_run)
        mgr._normalize_ownership(str(tmp_path))

        # No sudo invoked.
        assert all("sudo" not in c for c in run_calls), run_calls
        # Entries untouched.
        assert (tmp_path / "a.txt").exists()
        assert (tmp_path / "sub" / "b.txt").exists()

    def test_foreign_owned_file_is_unlinked_no_sudo(
        self, tmp_path, monkeypatch
    ):
        """Stale root-owned file inside user-writable dir → unlinked
        directly, no sudo escalation."""
        from unittest.mock import patch
        stale = tmp_path / "seed.iso"
        stale.write_bytes(b"stale")

        mgr = BoxmanManager()

        run_calls: list = []

        def _capture_run(cmd, *_a, **_kw):
            run_calls.append(cmd)
            from unittest.mock import MagicMock
            return MagicMock(ok=True, stderr="", stdout="")

        monkeypatch.setattr("boxman.manager.run", _capture_run)

        # Pretend the file is owned by uid=0 (root) by patching scandir
        # to return an entry whose stat reports st_uid=0.
        real_scandir = os.scandir

        class _FakeEntry:
            def __init__(self, real, fake_uid):
                self._real = real
                self.path = real.path
                self.name = real.name
                self._fake_uid = fake_uid
            def stat(self, follow_symlinks=False):
                real_stat = self._real.stat(follow_symlinks=follow_symlinks)
                # st_uid is read-only; build a tuple-like proxy.
                class _Stat:
                    pass
                s = _Stat()
                s.st_uid = self._fake_uid
                return s
            def is_dir(self, follow_symlinks=False):
                return self._real.is_dir(follow_symlinks=follow_symlinks)

        def _fake_scandir(p):
            for e in real_scandir(p):
                yield _FakeEntry(e, fake_uid=0)

        monkeypatch.setattr("boxman.manager.os.scandir", _fake_scandir)
        mgr._normalize_ownership(str(tmp_path))

        # File got removed without sudo.
        assert not stale.exists()
        assert all("sudo" not in c for c in run_calls), run_calls

    def test_unwritable_dir_falls_back_to_sudo_chown(
        self, tmp_path, monkeypatch
    ):
        """When the parent dir itself is not writable, the cheap path
        is impossible — sudo chown -R must be invoked."""
        from unittest.mock import MagicMock
        mgr = BoxmanManager()

        run_calls: list = []

        def _capture_run(cmd, *_a, **_kw):
            run_calls.append(cmd)
            return MagicMock(ok=True, stderr="", stdout="")

        monkeypatch.setattr("boxman.manager.run", _capture_run)
        # Pretend the dir is not writable.
        monkeypatch.setattr(
            "boxman.manager.os.access",
            lambda p, mode: False if p == str(tmp_path) else True,
        )

        mgr._normalize_ownership(str(tmp_path))

        assert any(
            "sudo -n chown -R" in c and str(tmp_path) in c
            for c in run_calls
        ), run_calls

    def test_sudo_failure_raises_with_actionable_message(
        self, tmp_path, monkeypatch
    ):
        """When the cheap path can't run AND sudo fails, the user gets
        a copy-pasteable fix command in the exception."""
        from unittest.mock import MagicMock
        mgr = BoxmanManager()

        def _failing_run(cmd, *_a, **_kw):
            return MagicMock(
                ok=False, stderr="sudo: a password is required\n",
                stdout="",
            )

        monkeypatch.setattr("boxman.manager.run", _failing_run)
        monkeypatch.setattr(
            "boxman.manager.os.access",
            lambda p, mode: False,
        )

        with pytest.raises(PermissionError) as exc_info:
            mgr._normalize_ownership(str(tmp_path))

        msg = str(exc_info.value)
        assert "sudo chown -R" in msg
        assert "sudo rm -rf" in msg
        assert str(tmp_path) in msg
        assert "password is required" in msg


class TestConfigSchemaV2:
    """
    Config schema versioning — Phase 2 of the docker-compose provider epic
    (https://github.com/mherkazandjian/boxman/issues/50).

    ``BoxmanManager.load_config`` dispatches on the top-level ``version:``
    key and normalizes v2.0 configs into the internal (v1.0) shape:
    libvirt clusters get ``boxes:`` renamed to ``vms:``; docker-compose
    clusters keep ``boxes:``; unsupported versions / ambiguous clusters
    raise :class:`ConfigError`. v1.0 stays byte-identical.
    """

    @staticmethod
    def _mgr():
        return BoxmanManager()

    @staticmethod
    def _cfg(version=None, top_provider='libvirt', cluster=None):
        cfg = {'project': 'p', 'clusters': {'c': cluster or {}}}
        if version is not None:
            cfg['version'] = version
        if top_provider is not None:
            cfg['provider'] = {top_provider: {}}
        return cfg

    # --- v1.0: unchanged --------------------------------------------------

    def test_v1_no_version_key_returns_unchanged(self):
        cfg = self._cfg(cluster={'vms': {'v1': {'hostname': 'v1'}}})
        out = self._mgr()._apply_config_version(cfg)
        assert out is cfg
        assert out['clusters']['c'] == {'vms': {'v1': {'hostname': 'v1'}}}

    def test_v1_explicit_version_returns_unchanged(self):
        cfg = self._cfg(version='1.0', cluster={'vms': {'v1': {}}})
        out = self._mgr()._apply_config_version(cfg)
        assert 'vms' in out['clusters']['c']
        assert 'boxes' not in out['clusters']['c']

    def test_v1_boxes_key_warns(self, captured_logs):
        cfg = self._cfg(version='1.0', cluster={'boxes': {'b1': {}}})
        with captured_logs.at_level(logging.WARNING, logger='boxman'):
            self._mgr()._apply_config_version(cfg)
        assert any(
            "boxes" in r.message and "2.0" in r.message
            for r in captured_logs.records
        )

    def test_empty_config_is_tolerated(self):
        assert self._mgr()._apply_config_version(None) is None
        assert self._mgr()._apply_config_version({}) == {}

    # --- v1.0: docker-compose is rejected (needs v2.0) -------------------

    def test_v1_top_level_docker_compose_is_error(self):
        """A versionless config whose primary provider is docker-compose is
        rejected: the provider consumes 'boxes:', which v1.0 ignores, so
        provisioning would contradict the v1 'boxes ignored' nudge."""
        cfg = self._cfg(top_provider='docker-compose',
                        cluster={'boxes': {'web': {'image': 'nginx'}}})
        with pytest.raises(ConfigError, match=r"docker-compose.*requires version: '2.0'"):
            self._mgr()._apply_config_version(cfg)

    def test_v1_per_cluster_docker_compose_is_error(self):
        """A v1.0 config with a per-cluster docker-compose provider is also
        rejected (the predicate reads the same per-cluster override)."""
        cfg = self._cfg(version='1.0', top_provider='libvirt',
                        cluster={'provider': 'docker-compose',
                                 'boxes': {'web': {'image': 'nginx'}}})
        with pytest.raises(ConfigError, match=r"docker-compose.*requires version: '2.0'"):
            self._mgr()._apply_config_version(cfg)

    def test_v1_libvirt_with_boxes_still_only_warns(self):
        """The rejection is docker-compose-specific: a v1 libvirt cluster
        using 'boxes:' still merely warns (regression guard for the nudge)."""
        cfg = self._cfg(version='1.0', top_provider='libvirt',
                        cluster={'boxes': {'b1': {}}})
        out = self._mgr()._apply_config_version(cfg)  # no raise
        assert out is cfg

    # --- v2.0 libvirt: boxes -> vms --------------------------------------

    def test_v2_libvirt_boxes_renamed_to_vms(self):
        cfg = self._cfg(version='2.0', top_provider='libvirt',
                        cluster={'boxes': {'node01': {'hostname': 'node01'}}})
        out = self._mgr().normalize_v2_config(cfg)
        assert out['clusters']['c']['vms'] == {'node01': {'hostname': 'node01'}}
        assert 'boxes' not in out['clusters']['c']

    def test_v2_libvirt_only_equals_v1_shape(self):
        """AC: a v2.0 libvirt-only config normalizes to the exact internal
        shape of its v1.0 equivalent."""
        v2 = self._cfg(version='2.0', cluster={'boxes': {'n': {'memory': 2048}}})
        v1 = self._cfg(version='1.0', cluster={'vms': {'n': {'memory': 2048}}})
        out2 = self._mgr()._apply_config_version(v2)
        out1 = self._mgr()._apply_config_version(v1)
        assert out2['clusters'] == out1['clusters']

    def test_v2_libvirt_vms_accepted_with_warning(self, captured_logs):
        cfg = self._cfg(version='2.0', top_provider='libvirt',
                        cluster={'vms': {'n': {}}})
        with captured_logs.at_level(logging.WARNING, logger='boxman'):
            out = self._mgr()._apply_config_version(cfg)
        assert out['clusters']['c']['vms'] == {'n': {}}
        assert any(
            "legacy 'vms:'" in r.message
            for r in captured_logs.records if r.levelno >= logging.WARNING
        )

    def test_v2_libvirt_boxes_and_vms_is_error(self):
        cfg = self._cfg(version='2.0', top_provider='libvirt',
                        cluster={'boxes': {}, 'vms': {}})
        with pytest.raises(ConfigError, match=r"both 'boxes:' and 'vms:'"):
            self._mgr()._apply_config_version(cfg)

    # --- v2.0 docker-compose: boxes kept ---------------------------------

    def test_v2_docker_compose_keeps_boxes(self):
        cfg = self._cfg(version='2.0', top_provider='docker-compose',
                        cluster={'provider': 'docker-compose',
                                 'boxes': {'web': {'image': 'nginx'}}})
        out = self._mgr().normalize_v2_config(cfg)
        assert out['clusters']['c']['boxes'] == {'web': {'image': 'nginx'}}
        assert 'vms' not in out['clusters']['c']

    def test_v2_docker_compose_vms_is_error(self):
        cfg = self._cfg(version='2.0', top_provider='docker-compose',
                        cluster={'provider': 'docker-compose', 'vms': {}})
        with pytest.raises(ConfigError, match=r"docker-compose clusters only support 'boxes:'"):
            self._mgr()._apply_config_version(cfg)

    # --- per-cluster provider defaulting to the top-level primary --------

    def test_v2_cluster_without_provider_uses_libvirt_primary(self):
        """Top-level primary is libvirt → an un-annotated cluster is
        libvirt → boxes renamed to vms."""
        cfg = self._cfg(version='2.0', top_provider='libvirt',
                        cluster={'boxes': {'n': {}}})
        out = self._mgr().normalize_v2_config(cfg)
        assert 'vms' in out['clusters']['c']

    def test_v2_cluster_without_provider_uses_dc_primary(self):
        """Top-level primary is docker-compose → an un-annotated cluster is
        docker-compose → boxes kept as-is."""
        cfg = self._cfg(version='2.0', top_provider='docker-compose',
                        cluster={'boxes': {'web': {}}})
        out = self._mgr().normalize_v2_config(cfg)
        assert 'boxes' in out['clusters']['c']
        assert 'vms' not in out['clusters']['c']

    # --- unsupported version ---------------------------------------------

    def test_unsupported_version_raises(self):
        cfg = self._cfg(version='3.0', cluster={'vms': {}})
        with pytest.raises(ConfigError, match=r"unsupported config version: '3.0'"):
            self._mgr()._apply_config_version(cfg)

    def test_unsupported_version_lists_supported(self):
        cfg = self._cfg(version='banana', cluster={})
        with pytest.raises(ConfigError, match=r"'1.0', '2.0'"):
            self._mgr()._apply_config_version(cfg)

    # --- end-to-end through load_config ----------------------------------

    def test_load_config_normalizes_v2_and_writes_rendered(self, tmp_path):
        conf = {
            'version': '2.0',
            'project': 'e2e',
            'provider': {'libvirt': {'uri': 'qemu:///system'}},
            'clusters': {'compute': {'boxes': {'node01': {'hostname': 'node01'}}}},
        }
        path = tmp_path / 'conf.yml'
        path.write_text(yaml.dump(conf))

        mgr = BoxmanManager()
        loaded = mgr.load_config(str(path))

        # v2.0 boxes: normalized to internal vms:
        assert loaded['clusters']['compute']['vms'] == {'node01': {'hostname': 'node01'}}
        assert 'boxes' not in loaded['clusters']['compute']
        # AC: the .rendered.yml debug dump is still written
        assert (tmp_path / 'conf.rendered.yml').exists()

    def test_load_config_dollar_var_survives_preprocessing(self, tmp_path):
        """A docker-compose ``${VAR}`` interpolation must survive the bare
        ``{{ name }}`` -> ``{name}`` task-placeholder preprocessing."""
        conf = {
            'version': '2.0',
            'project': 'compose_env',
            'provider': {'docker-compose': {}},
            'clusters': {
                'services': {
                    'provider': 'docker-compose',
                    'boxes': {
                        'web': {
                            'image': 'nginx',
                            'environment': ['DB_HOST=${DB_HOST}', 'ESC=$${LITERAL}'],
                        }
                    },
                }
            },
        }
        path = tmp_path / 'conf.yml'
        path.write_text(yaml.dump(conf))

        loaded = BoxmanManager().load_config(str(path))
        env = loaded['clusters']['services']['boxes']['web']['environment']
        assert 'DB_HOST=${DB_HOST}' in env
        assert 'ESC=$${LITERAL}' in env

    # --- M1: unquoted numeric versions ------------------------------------

    def test_unquoted_int_version_1_is_v1(self):
        """`version: 1` (unquoted int) must not hard-fail — pre-PR it was
        ignored, so it stays on the v1.0 path."""
        cfg = self._cfg(version=1, cluster={'vms': {'v': {}}})
        out = self._mgr()._apply_config_version(cfg)
        assert 'vms' in out['clusters']['c']

    def test_unquoted_int_version_2_is_v2(self):
        cfg = self._cfg(version=2, top_provider='libvirt',
                        cluster={'boxes': {'n': {}}})
        out = self._mgr()._apply_config_version(cfg)
        assert 'vms' in out['clusters']['c']
        assert 'boxes' not in out['clusters']['c']

    def test_unquoted_float_version_2_0_is_v2(self):
        cfg = self._cfg(version=2.0, top_provider='libvirt',
                        cluster={'boxes': {'n': {}}})
        out = self._mgr()._apply_config_version(cfg)
        assert 'vms' in out['clusters']['c']

    def test_load_config_unquoted_version_2(self, tmp_path):
        """End-to-end: an unquoted `version: 2` file loads as v2.0."""
        path = tmp_path / 'conf.yml'
        path.write_text(
            "version: 2\n"
            "project: p\n"
            "provider:\n  libvirt: {}\n"
            "clusters:\n  c:\n    boxes:\n      n:\n        hostname: n\n"
        )
        loaded = BoxmanManager().load_config(str(path))
        assert loaded['clusters']['c']['vms'] == {'n': {'hostname': 'n'}}

    # --- C1: non-dict / malformed root ------------------------------------

    def test_non_dict_root_returned_unchanged(self):
        """A non-mapping YAML root must be returned as-is, not raise
        AttributeError past the clean handler."""
        assert self._mgr()._apply_config_version(['a', 'b']) == ['a', 'b']
        assert self._mgr()._apply_config_version("scalar") == "scalar"

    # --- M2/C2: provider validation ---------------------------------------

    def test_virtualbox_provider_rejected_in_v2(self):
        cfg = self._cfg(version='2.0', top_provider='virtualbox',
                        cluster={'boxes': {'n': {}}})
        with pytest.raises(ConfigError, match=r"'virtualbox' is not supported"):
            self._mgr()._apply_config_version(cfg)

    def test_unknown_provider_rejected(self):
        cfg = self._cfg(version='2.0', top_provider='libvirt',
                        cluster={'provider': 'libvrit', 'boxes': {}})
        with pytest.raises(ConfigError, match=r"unknown provider 'libvrit'"):
            self._mgr()._apply_config_version(cfg)

    # --- M3: per-box provider is a config error (ADR-001) -----------------

    def test_per_box_provider_in_boxes_rejected(self):
        cfg = self._cfg(version='2.0', top_provider='libvirt',
                        cluster={'boxes': {'web': {'provider': 'docker-compose'}}})
        with pytest.raises(ConfigError, match=r"box 'web'.*'provider:'"):
            self._mgr()._apply_config_version(cfg)

    def test_per_box_provider_in_legacy_vms_rejected(self):
        cfg = self._cfg(version='2.0', top_provider='libvirt',
                        cluster={'vms': {'db': {'provider': 'x'}}})
        with pytest.raises(ConfigError, match=r"box 'db'.*'provider:'"):
            self._mgr()._apply_config_version(cfg)

    # --- M5: partial-migration warning ------------------------------------

    def test_v1_partial_migration_boxes_and_vms_warns(self, captured_logs):
        """v1.0 cluster carrying both `vms:` and `boxes:` — the boxes are
        silently ignored, so the warning must still fire."""
        cfg = self._cfg(version='1.0',
                        cluster={'vms': {'old': {}}, 'boxes': {'new': {}}})
        with captured_logs.at_level(logging.WARNING, logger='boxman'):
            self._mgr()._apply_config_version(cfg)
        assert any(
            "boxes" in r.message and "ignored" in r.message
            for r in captured_logs.records
        )

    # --- N2: docker-compose both-keys message -----------------------------

    def test_dc_both_keys_message_names_docker_compose(self):
        cfg = self._cfg(version='2.0', top_provider='docker-compose',
                        cluster={'provider': 'docker-compose',
                                 'boxes': {}, 'vms': {}})
        with pytest.raises(ConfigError, match=r"docker-compose clusters only support 'boxes:'"):
            self._mgr()._apply_config_version(cfg)

    # --- mixed config parses (runnability is Phase 3) ---------------------

    def test_mixed_config_normalizes_without_error(self):
        """A libvirt-primary config with one dc cluster loads cleanly: the
        libvirt cluster gets `vms:`, the dc cluster keeps `boxes:`."""
        cfg = {
            'version': '2.0',
            'project': 'mixed',
            'provider': {'libvirt': {}},
            'clusters': {
                'compute': {'boxes': {'n': {}}},
                'services': {'provider': 'docker-compose', 'boxes': {'web': {}}},
            },
        }
        out = self._mgr().normalize_v2_config(cfg)
        assert 'vms' in out['clusters']['compute']
        assert out['clusters']['services']['boxes'] == {'web': {}}
        assert 'vms' not in out['clusters']['services']

    # --- N1: idempotency caveat -------------------------------------------

    def test_double_normalize_emits_spurious_legacy_warning(self, captured_logs):
        """Documents the in-place, run-once contract: re-normalizing an
        already-normalized dict treats the renamed `vms:` as legacy."""
        cfg = self._cfg(version='2.0', top_provider='libvirt',
                        cluster={'boxes': {'n': {}}})
        mgr = self._mgr()
        mgr.normalize_v2_config(cfg)  # boxes -> vms, no warning
        with captured_logs.at_level(logging.WARNING, logger='boxman'):
            mgr.normalize_v2_config(cfg)  # second pass: vms looks legacy
        assert any("legacy 'vms:'" in r.message for r in captured_logs.records)


class TestMainConfigErrorExit:
    """The ``main()`` wrapper (app.py) turns a ``ConfigError`` from config
    loading into a clean ``log.error`` + exit 2 — the only behavioural
    change in app.py for Phase 2 (finding L1), including the M4 fix that the
    message survives a viewer command's logger suppression."""

    def test_config_error_exits_2_and_logs(self, captured_logs, monkeypatch):
        from boxman.scripts import app

        def _raise():
            raise ConfigError('boom-config')

        monkeypatch.setattr(app, '_main', _raise)
        with captured_logs.at_level(logging.ERROR, logger='boxman'):
            with pytest.raises(SystemExit) as excinfo:
                app.main()
        assert excinfo.value.code == 2
        assert any('boom-config' in r.message for r in captured_logs.records)

    def test_error_survives_snapshot_log_suppression(self, captured_logs, monkeypatch):
        """M4: a viewer command raises the boxman logger to CRITICAL+1
        before load; the wrapper must restore it so the error is emitted."""
        from boxman.scripts import app

        boxman_logger = logging.getLogger('boxman')
        previous = boxman_logger.level
        boxman_logger.setLevel(logging.CRITICAL + 1)

        def _raise():
            raise ConfigError('shown-anyway')

        try:
            monkeypatch.setattr(app, '_main', _raise)
            with pytest.raises(SystemExit) as excinfo:
                app.main()
            assert excinfo.value.code == 2
            assert any('shown-anyway' in r.message for r in captured_logs.records)
        finally:
            boxman_logger.setLevel(previous)

    def test_provision_error_also_exits_2(self, captured_logs, monkeypatch):
        """Phase 3: the wrapper now catches the whole BoxmanError family, so an
        operational failure on the docker-compose path (an unhealthy service →
        ProvisionError) exits 2 cleanly instead of dumping a traceback."""
        from boxman.scripts import app
        from boxman.exceptions import ProvisionError

        def _raise():
            raise ProvisionError('service never became healthy')

        monkeypatch.setattr(app, '_main', _raise)
        with captured_logs.at_level(logging.ERROR, logger='boxman'):
            with pytest.raises(SystemExit) as excinfo:
                app.main()
        assert excinfo.value.code == 2
        assert any('never became healthy' in r.message for r in captured_logs.records)
