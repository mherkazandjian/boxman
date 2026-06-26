"""Unit tests for BoxmanManager ISO resolution helpers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit


def _manager_with_config(config: dict) -> BoxmanManager:
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = config
    mgr.app_config = {}
    mgr.logger = MagicMock()
    return mgr


class TestResolveIsos:
    def test_returns_empty_dict_when_no_isos_section(self):
        mgr = _manager_with_config({})
        assert mgr._resolve_isos() == {}

    def test_returns_empty_dict_when_isos_is_empty(self):
        mgr = _manager_with_config({"isos": {}})
        assert mgr._resolve_isos() == {}

    def test_raises_when_uri_missing(self):
        mgr = _manager_with_config({"isos": {"talos-omni": {}}})
        with pytest.raises(ValueError, match="missing 'uri'"):
            mgr._resolve_isos()

    def test_raises_when_iso_conf_is_null(self):
        mgr = _manager_with_config({"isos": {"talos-omni": None}})
        with pytest.raises(ValueError, match="must be a mapping"):
            mgr._resolve_isos()

    def test_calls_image_cache_ensure(self):
        mgr = _manager_with_config({
            "isos": {"talos-omni": {"uri": "https://example.com/talos.iso"}}
        })
        with patch("boxman.manager.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.ensure.return_value = "/cache/talos.iso"
            mock_cache_cls.from_config.return_value = mock_cache
            result = mgr._resolve_isos()
        assert result == {"talos-omni": "/cache/talos.iso"}
        mock_cache.ensure.assert_called_once_with(
            "https://example.com/talos.iso", mgr._download_iso
        )

    def test_raises_when_download_fails(self):
        mgr = _manager_with_config({
            "isos": {"talos-omni": {"uri": "https://example.com/talos.iso"}}
        })
        with patch("boxman.manager.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.ensure.return_value = None
            mock_cache_cls.from_config.return_value = mock_cache
            with pytest.raises(RuntimeError, match="Failed to download ISO"):
                mgr._resolve_isos()

    def test_verifies_checksum_when_provided(self):
        mgr = _manager_with_config({
            "isos": {"talos-omni": {
                "uri": "https://example.com/talos.iso",
                "checksum": "sha256:abc123",
            }}
        })
        with patch("boxman.manager.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.ensure.return_value = "/cache/talos.iso"
            mock_cache.verify_checksum.return_value = True
            mock_cache_cls.from_config.return_value = mock_cache
            mock_cache_cls.verify_checksum = MagicMock(return_value=True)
            result = mgr._resolve_isos()
        assert result == {"talos-omni": "/cache/talos.iso"}
        mock_cache_cls.verify_checksum.assert_called_once_with("/cache/talos.iso", "sha256:abc123")

    def test_raises_on_checksum_mismatch(self):
        mgr = _manager_with_config({
            "isos": {"talos-omni": {
                "uri": "https://example.com/talos.iso",
                "checksum": "sha256:abc123",
            }}
        })
        with patch("boxman.manager.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.ensure.return_value = "/cache/talos.iso"
            mock_cache_cls.from_config.return_value = mock_cache
            mock_cache_cls.verify_checksum = MagicMock(return_value=False)
            with pytest.raises(RuntimeError, match="Checksum mismatch"):
                mgr._resolve_isos()


class TestInjectResolvedIso:
    def test_resolves_named_cdrom_reference(self):
        mgr = _manager_with_config({})
        vm_info = {
            "boot_order": ["cdrom", "hd"],
            "cdroms": [{"name": "talos-omni"}],
        }
        resolved_isos = {"talos-omni": "/cache/talos.iso"}
        result = mgr._inject_resolved_iso(vm_info, resolved_isos)
        assert result["cdroms"][0]["source"] == "/cache/talos.iso"
        assert result["_resolved_iso_path"] == "/cache/talos.iso"

    def test_does_not_mutate_input_vm_info(self):
        mgr = _manager_with_config({})
        vm_info = {"boot_order": ["cdrom", "hd"], "cdroms": [{"name": "talos-omni"}]}
        resolved_isos = {"talos-omni": "/cache/talos.iso"}
        mgr._inject_resolved_iso(vm_info, resolved_isos)
        assert "_resolved_iso_path" not in vm_info

    def test_raises_on_unknown_named_iso(self):
        mgr = _manager_with_config({})
        vm_info = {"boot_order": ["cdrom", "hd"], "cdroms": [{"name": "unknown-iso"}]}
        with pytest.raises(ValueError, match="unknown iso 'unknown-iso'"):
            mgr._inject_resolved_iso(vm_info, {})

    def test_no_injection_when_boot_order_is_not_cdrom(self):
        mgr = _manager_with_config({})
        vm_info = {
            "boot_order": ["hd"],
            "cdroms": [{"name": "talos-omni"}],
        }
        resolved_isos = {"talos-omni": "/cache/talos.iso"}
        result = mgr._inject_resolved_iso(vm_info, resolved_isos)
        assert "_resolved_iso_path" not in result

    def test_passthrough_when_no_cdroms(self):
        mgr = _manager_with_config({})
        vm_info = {"boot_order": ["hd"]}
        result = mgr._inject_resolved_iso(vm_info, {})
        assert result == vm_info

    def test_inline_source_string_passes_through_unchanged(self):
        mgr = _manager_with_config({})
        vm_info = {
            "boot_order": ["cdrom", "hd"],
            "cdroms": [{"source": "/local/talos.iso"}],
        }
        result = mgr._inject_resolved_iso(vm_info, {})
        assert result["cdroms"][0]["source"] == "/local/talos.iso"
        assert result["_resolved_iso_path"] == "/local/talos.iso"
