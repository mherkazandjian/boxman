"""
Unit tests for selected BoxmanManager core methods.

Targets the pure / easily isolated methods of the 4122-LOC manager.py:
  - ``_merge_provider_configs`` (static, sudo-lists merging)
  - ``_canonical_runtime_name`` (static)
  - ``collect_workdirs`` (config-only, no I/O beyond path resolution)
  - ``fetch_value`` (classmethod — env/file/literal resolution)
  - ``get_global_authorized_keys`` (uses fetch_value)
  - ``runtime`` / ``runtime_instance`` / ``get_provider_config_with_runtime``

The orchestration surface (provision, up, down, snapshot_*, etc.) is
left to integration tests.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from boxman.manager import BoxmanManager


pytestmark = pytest.mark.unit


@pytest.fixture
def mgr(tmp_path: Path) -> BoxmanManager:
    """A BoxmanManager constructed with no config file, so it doesn't trigger
    any config-loading side effects."""
    with patch("boxman.manager.BoxmanCache"):
        return BoxmanManager()


class TestMergeProviderConfigs:
    """
    Scalar keys: local wins over global (dict.update).
    sudo_skip_commands / force_sudo_commands: per-command local-wins merge.
    """

    def test_scalars_local_overrides_global(self):
        merged = BoxmanManager._merge_provider_configs(
            {"uri": "qemu:///system", "use_sudo": True},
            {"use_sudo": False},
        )
        assert merged["uri"] == "qemu:///system"
        assert merged["use_sudo"] is False

    def test_empty_configs_yield_empty_sudo_lists(self):
        merged = BoxmanManager._merge_provider_configs({}, {})
        assert merged["sudo_skip_commands"] == []
        assert merged["force_sudo_commands"] == []

    def test_sudo_lists_union(self):
        merged = BoxmanManager._merge_provider_configs(
            {"sudo_skip_commands": ["ls"], "force_sudo_commands": ["virsh"]},
            {"sudo_skip_commands": ["cat"], "force_sudo_commands": ["virt-install"]},
        )
        assert merged["sudo_skip_commands"] == ["cat", "ls"]
        assert merged["force_sudo_commands"] == ["virsh", "virt-install"]

    def test_local_skip_wins_over_global_force(self):
        merged = BoxmanManager._merge_provider_configs(
            {"force_sudo_commands": ["virsh"]},
            {"sudo_skip_commands": ["virsh"]},
        )
        assert "virsh" in merged["sudo_skip_commands"]
        assert "virsh" not in merged["force_sudo_commands"]

    def test_local_force_wins_over_global_skip(self):
        merged = BoxmanManager._merge_provider_configs(
            {"sudo_skip_commands": ["virsh"]},
            {"force_sudo_commands": ["virsh"]},
        )
        assert "virsh" in merged["force_sudo_commands"]
        assert "virsh" not in merged["sudo_skip_commands"]

    def test_does_not_mutate_inputs(self):
        g = {"sudo_skip_commands": ["ls"]}
        l = {"sudo_skip_commands": ["cat"]}
        BoxmanManager._merge_provider_configs(g, l)
        assert g == {"sudo_skip_commands": ["ls"]}
        assert l == {"sudo_skip_commands": ["cat"]}


class TestCanonicalRuntimeName:

    def test_none_returns_none(self):
        assert BoxmanManager._canonical_runtime_name(None) is None

    def test_empty_returns_empty(self):
        assert BoxmanManager._canonical_runtime_name("") == ""

    def test_docker_and_docker_compose_collapse(self):
        assert BoxmanManager._canonical_runtime_name("docker") == "docker-compose"
        assert BoxmanManager._canonical_runtime_name("docker-compose") == "docker-compose"

    def test_case_insensitive(self):
        assert BoxmanManager._canonical_runtime_name("  DOCKER  ") == "docker-compose"

    def test_local_untouched(self):
        assert BoxmanManager._canonical_runtime_name("local") == "local"

    def test_unknown_preserved(self):
        assert BoxmanManager._canonical_runtime_name("aws-magic") == "aws-magic"


class TestCollectWorkdirs:

    def test_empty_config_returns_empty(self, mgr: BoxmanManager):
        mgr.config = None
        assert mgr.collect_workdirs() == []

    def test_workspace_path_included(self, mgr: BoxmanManager, tmp_path: Path):
        mgr.config = {"workspace": {"path": str(tmp_path)}}
        assert str(tmp_path) in mgr.collect_workdirs()

    def test_cluster_workdirs_included(self, mgr: BoxmanManager, tmp_path: Path):
        c1 = tmp_path / "c1"
        c2 = tmp_path / "c2"
        mgr.config = {
            "clusters": {
                "a": {"workdir": str(c1)},
                "b": {"workdir": str(c2)},
            }
        }
        out = mgr.collect_workdirs()
        assert str(c1) in out
        assert str(c2) in out

    def test_template_workdir_or_default(self, mgr: BoxmanManager, tmp_path: Path):
        tpl_wd = tmp_path / "my-tpl"
        mgr.config = {
            "templates": {
                "a": {"workdir": str(tpl_wd)},
                "b": {},  # should fall back to default ~/boxman-templates
            }
        }
        out = mgr.collect_workdirs()
        assert str(tpl_wd) in out
        assert any("boxman-templates" in p for p in out)

    def test_deduplicates_and_sorts(self, mgr: BoxmanManager, tmp_path: Path):
        shared = str(tmp_path / "shared")
        mgr.config = {
            "workspace": {"path": shared},
            "clusters": {
                "a": {"workdir": shared},
                "b": {"workdir": shared},
            },
        }
        out = mgr.collect_workdirs()
        assert out == sorted(set(out))
        assert out.count(shared) == 1


class TestFetchValue:

    def test_literal_string_returned_unchanged(self):
        assert BoxmanManager.fetch_value("just-a-literal-string") == "just-a-literal-string"

    def test_env_placeholder_resolved(self, monkeypatch):
        monkeypatch.setenv("BOXMAN_TEST_KEY", "value-from-env")
        assert BoxmanManager.fetch_value("${env:BOXMAN_TEST_KEY}") == "value-from-env"

    def test_env_missing_raises_value_error(self, monkeypatch):
        monkeypatch.delenv("BOXMAN_TEST_MISSING", raising=False)
        with pytest.raises(ValueError, match="is not set"):
            BoxmanManager.fetch_value("${env:BOXMAN_TEST_MISSING}")

    def test_file_scheme_reads_contents(self, tmp_path: Path):
        f = tmp_path / "key.pub"
        f.write_text("ssh-ed25519 AAAA... user@host\n")
        out = BoxmanManager.fetch_value(f"file://{f}")
        assert out == "ssh-ed25519 AAAA... user@host"   # trailing \n stripped

    def test_file_scheme_missing_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            BoxmanManager.fetch_value(f"file://{tmp_path / 'nope'}")

    def test_non_string_returned_as_is(self):
        assert BoxmanManager.fetch_value(42) == 42
        assert BoxmanManager.fetch_value(None) is None


class TestGetGlobalAuthorizedKeys:

    def test_empty_when_no_app_config(self, mgr: BoxmanManager):
        mgr.app_config = None
        assert mgr.get_global_authorized_keys() == []

    def test_empty_when_section_missing(self, mgr: BoxmanManager):
        mgr.app_config = {}
        assert mgr.get_global_authorized_keys() == []

    def test_literal_key_passed_through(self, mgr: BoxmanManager):
        mgr.app_config = {"ssh": {"authorized_keys": ["ssh-ed25519 AAA user@host"]}}
        assert mgr.get_global_authorized_keys() == ["ssh-ed25519 AAA user@host"]

    def test_env_placeholder_resolved(self, mgr: BoxmanManager, monkeypatch):
        monkeypatch.setenv("BOXMAN_SSH_PUBKEY", "ssh-ed25519 env-key user@host")
        mgr.app_config = {"ssh": {"authorized_keys": ["${env:BOXMAN_SSH_PUBKEY}"]}}
        assert mgr.get_global_authorized_keys() == ["ssh-ed25519 env-key user@host"]

    def test_file_reference_resolved(self, mgr: BoxmanManager, tmp_path: Path):
        pub = tmp_path / "id.pub"
        pub.write_text("ssh-ed25519 file-key user@host\n")
        mgr.app_config = {"ssh": {"authorized_keys": [f"file://{pub}"]}}
        assert mgr.get_global_authorized_keys() == ["ssh-ed25519 file-key user@host"]

    def test_unresolvable_entries_are_skipped_with_warning(
        self, mgr: BoxmanManager, captured_logs, monkeypatch
    ):
        monkeypatch.delenv("BOXMAN_MISSING_X", raising=False)
        mgr.app_config = {"ssh": {"authorized_keys": [
            "ssh-ed25519 present user@host",
            "${env:BOXMAN_MISSING_X}",
        ]}}
        keys = mgr.get_global_authorized_keys()
        assert keys == ["ssh-ed25519 present user@host"]
        assert any(
            "skipping unresolvable SSH key entry" in rec.message
            for rec in captured_logs.records
        )


class TestWriteGlobalAuthorizedKeysFile:

    def test_writes_one_key_per_line(self, mgr: BoxmanManager, tmp_path: Path):
        mgr.app_config = {"ssh": {"authorized_keys": [
            "ssh-ed25519 AAAA a@host", "ssh-ed25519 BBBB b@host",
        ]}}
        target = tmp_path / "ssh" / "global_authorized_keys"
        mgr.write_global_authorized_keys_file(str(target))
        lines = target.read_text().splitlines()
        assert lines == ["ssh-ed25519 AAAA a@host", "ssh-ed25519 BBBB b@host"]

    def test_skips_write_when_no_keys(self, mgr: BoxmanManager, tmp_path: Path):
        mgr.app_config = None
        target = tmp_path / "ssh" / "out"
        mgr.write_global_authorized_keys_file(str(target))
        assert not target.exists()


class TestRuntimeProperty:

    def test_default_runtime_is_local(self, mgr: BoxmanManager):
        assert mgr.runtime == "local"

    def test_setting_runtime_resets_instance(self, mgr: BoxmanManager):
        # access instance once so it's cached
        rt1 = mgr.runtime_instance
        mgr.runtime = "docker-compose"
        rt2 = mgr.runtime_instance
        assert rt1 is not rt2
        assert rt2.name == "docker-compose"


class TestGetProviderConfigWithRuntime:

    def test_injects_runtime_key_via_factory(self, mgr: BoxmanManager):
        mgr.runtime = "local"
        enriched = mgr.get_provider_config_with_runtime({"uri": "qemu:///system"})
        assert enriched["runtime"] == "local"
        assert enriched["uri"] == "qemu:///system"

    def test_docker_compose_runtime_injects_correctly(self, mgr: BoxmanManager):
        mgr.runtime = "docker-compose"
        enriched = mgr.get_provider_config_with_runtime({"uri": "qemu:///system"})
        assert enriched["runtime"] == "docker-compose"
