"""
Unit tests for boxman.providers.libvirt.cloudinit_presets.

Part of Phase 2.7 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).

Pins the behavior that was lifted out of ``cloudinit.py`` and confirms
the re-exports from the original module still resolve so the public
surface stays intact.
"""

from __future__ import annotations

import pytest

from boxman.providers.libvirt import cloudinit as _cloudinit_mod
from boxman.providers.libvirt.cloudinit_presets import (
    DEFAULT_META_DATA,
    DEFAULT_NETWORK_CONFIG,
    DEFAULT_USER_DATA,
    hash_password,
)


pytestmark = pytest.mark.unit


class TestDefaultMetaData:

    def test_has_expected_placeholders(self):
        assert "{instance_id}" in DEFAULT_META_DATA
        assert "{hostname}" in DEFAULT_META_DATA

    def test_renders_via_format(self):
        rendered = DEFAULT_META_DATA.format(
            instance_id="demo-001", hostname="demo"
        )
        assert "instance-id: demo-001" in rendered
        assert "local-hostname: demo" in rendered


class TestDefaultUserData:

    def test_is_valid_cloud_config_header(self):
        assert DEFAULT_USER_DATA.startswith("#cloud-config")

    def test_has_hostname_placeholder(self):
        assert "{hostname}" in DEFAULT_USER_DATA

    def test_contains_ssh_config_sections(self):
        assert "ssh_pwauth" in DEFAULT_USER_DATA
        assert "chpasswd" in DEFAULT_USER_DATA

    def test_contains_network_bringup_runcmd(self):
        # Guards the lines that the fix for IP allocation relies on.
        assert "netplan apply" in DEFAULT_USER_DATA
        assert "dhclient" in DEFAULT_USER_DATA


class TestDefaultNetworkConfig:

    def test_valid_netplan_v2(self):
        assert "version: 2" in DEFAULT_NETWORK_CONFIG
        assert "ethernets:" in DEFAULT_NETWORK_CONFIG

    def test_matches_both_en_and_eth(self):
        # Ensures VMs on both "enp*" and "eth*" naming conventions get DHCP.
        assert 'name: "en*"' in DEFAULT_NETWORK_CONFIG
        assert 'name: "eth*"' in DEFAULT_NETWORK_CONFIG


class TestHashPassword:

    def test_returns_sha512_crypt_hash(self):
        h = hash_password("hunter2")
        # SHA-512 crypt hashes always start with $6$
        assert h.startswith("$6$")
        assert "hunter2" not in h

    def test_salt_is_unique_per_call(self):
        """Random salt → two hashes of the same password must differ."""
        assert hash_password("same") != hash_password("same")


class TestBackwardsCompatibilityReexports:
    """Ensure callers importing from the old path still work.

    Before the Phase 2.7 extraction, these names lived directly in
    ``cloudinit.py``. They are re-exported from the new presets module
    so existing external imports continue to resolve.
    """

    def test_module_exposes_default_user_data(self):
        assert _cloudinit_mod.DEFAULT_USER_DATA is DEFAULT_USER_DATA

    def test_module_exposes_default_meta_data(self):
        assert _cloudinit_mod.DEFAULT_META_DATA is DEFAULT_META_DATA

    def test_module_exposes_default_network_config(self):
        assert _cloudinit_mod.DEFAULT_NETWORK_CONFIG is DEFAULT_NETWORK_CONFIG

    def test_cloud_init_template_hash_password_delegates(self):
        """CloudInitTemplate.hash_password is now a thin wrapper."""
        # Same behavior as calling the preset helper directly
        out = _cloudinit_mod.CloudInitTemplate.hash_password("xyz")
        assert out.startswith("$6$")
