"""
Test that --boxman-conf overrides the default ~/.config/boxman/boxman.yml.
"""

import os
import tempfile
import yaml
import pytest

from boxman.scripts.app import load_boxman_config
from boxman.manager import BoxmanManager


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
        """Verify that a non-existent config path raises an error."""
        with pytest.raises(FileNotFoundError):
            load_boxman_config("/nonexistent/path/boxman.yml")

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
