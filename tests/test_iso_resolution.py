"""Unit tests for BoxmanManager ISO resolution helpers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import ProvisionError
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

    def test_raises_when_isos_section_is_not_a_mapping(self):
        # `isos: some-string` / `isos: [..]` must give a clear ValueError, not an
        # AttributeError from calling .items() on a non-dict
        for bad in ("oops", [{"uri": "x"}]):
            mgr = _manager_with_config({"isos": bad})
            with pytest.raises(ValueError, match="'isos:' must be a mapping"):
                mgr._resolve_isos()

    def test_an_already_cached_iso_is_used_without_downloading(self, tmp_path):
        """
        Never re-fetch a file that is present: the downloader writes straight
        to its destination, so that would truncate an ISO a running guest may
        have attached (#164 FB-5).
        """
        present = tmp_path / "talos-omni-deadbeef.iso"
        present.write_bytes(b"installer")
        mgr = _manager_with_config({
            "isos": {"talos-omni": {"uri": "https://example.com/talos.iso"}}
        })
        with patch("boxman.manager_parts.images.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.cache_path_for.return_value = str(present)
            mock_cache_cls.from_config.return_value = mock_cache
            with patch.object(type(mgr), "_download_iso",
                              side_effect=AssertionError("must not download")):
                result = mgr._resolve_isos()

        assert result == {"talos-omni": str(present)}
        assert present.read_bytes() == b"installer"

    def test_iso_cache_filename_disambiguates_shared_basename(self):
        # two distinct ISOs sharing a basename must map to distinct cache files
        a = BoxmanManager._iso_cache_filename(
            "talos-a", "https://factory.example.com/a/metal-amd64.iso")
        b = BoxmanManager._iso_cache_filename(
            "talos-b", "https://factory.example.com/b/metal-amd64.iso")
        assert a != b
        assert a.endswith(".iso") and "/" not in a

    def test_a_bad_existing_file_is_reported_not_evicted(self, tmp_path):
        """
        Eviction used to delete the file so a later run would re-download it.
        But a guest may have that ISO attached, and deleting or replacing it
        is the same destruction the download itself would cause. Report and
        let the operator decide (#164 FB-5).
        """
        bad = tmp_path / "talos-omni-deadbeef.iso"
        bad.write_bytes(b"corrupt")
        mgr = _manager_with_config({
            "isos": {"talos-omni": {
                "uri": "https://example.com/talos.iso",
                "checksum": "sha256:abc123",
            }}
        })
        with patch("boxman.manager_parts.images.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.enabled = True
            mock_cache.cache_path_for.return_value = str(bad)
            mock_cache_cls.from_config.return_value = mock_cache
            mock_cache_cls.verify_checksum = MagicMock(return_value=False)
            with pytest.raises(ProvisionError,
                               match="does not match its declared checksum"):
                mgr._resolve_isos()

        assert bad.exists()
        assert bad.read_bytes() == b"corrupt"

    def test_raises_under_non_local_runtime(self):
        mgr = _manager_with_config({
            "isos": {"talos-omni": {"uri": "https://example.com/talos.iso"}}
        })
        mgr._runtime_name = "docker"
        with pytest.raises(RuntimeError, match="not yet supported under the 'docker' runtime"):
            mgr._resolve_isos()

    def test_raises_when_download_fails(self, tmp_path):
        dest = tmp_path / "talos-omni-deadbeef.iso"
        mgr = _manager_with_config({
            "isos": {"talos-omni": {"uri": "https://example.com/talos.iso"}}
        })
        with patch("boxman.manager_parts.images.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.cache_path_for.return_value = str(dest)
            mock_cache_cls.from_config.return_value = mock_cache
            with patch.object(type(mgr), "_download_iso", return_value=False):
                with pytest.raises(ProvisionError,
                                   match="failed to download iso"):
                    mgr._resolve_isos()

        assert not dest.exists()
        assert list(tmp_path.iterdir()) == []

    def test_a_download_is_staged_and_published_atomically(self, tmp_path):
        """
        Downloading straight to the destination lets a concurrent resolver
        see a growing file, accept it, attach it, and then lose it when the
        first download fails and deletes it (#164 FB-5).
        """
        dest = tmp_path / "talos-omni-deadbeef.iso"
        seen = {}

        def _fake_download(url, path):
            seen["path"] = path
            seen["dest_existed_during"] = dest.exists()
            with open(path, "wb") as handle:
                handle.write(b"installer")
            return True

        mgr = _manager_with_config({
            "isos": {"talos-omni": {"uri": "https://example.com/talos.iso"}}
        })
        with patch("boxman.manager_parts.images.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.cache_path_for.return_value = str(dest)
            mock_cache_cls.from_config.return_value = mock_cache
            with patch.object(type(mgr), "_download_iso",
                              side_effect=_fake_download):
                result = mgr._resolve_isos()

        assert result == {"talos-omni": str(dest)}
        assert seen["path"] != str(dest), "downloaded straight to the destination"
        assert seen["dest_existed_during"] is False
        assert dest.read_bytes() == b"installer"
        # staging is cleaned up
        assert [p.name for p in tmp_path.iterdir()] == [dest.name]

    def test_a_concurrently_published_iso_is_not_replaced(self, tmp_path):
        """Whoever published first wins; replacing could truncate it."""
        dest = tmp_path / "talos-omni-deadbeef.iso"

        def _fake_download(url, path):
            with open(path, "wb") as handle:
                handle.write(b"ours")
            # another run publishes while this one downloads
            dest.write_bytes(b"theirs")
            return True

        mgr = _manager_with_config({
            "isos": {"talos-omni": {"uri": "https://example.com/talos.iso"}}
        })
        with patch("boxman.manager_parts.images.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.cache_path_for.return_value = str(dest)
            mock_cache_cls.from_config.return_value = mock_cache
            with patch.object(type(mgr), "_download_iso",
                              side_effect=_fake_download):
                result = mgr._resolve_isos()

        assert result == {"talos-omni": str(dest)}
        assert dest.read_bytes() == b"theirs"

    def test_verifies_checksum_when_provided(self, tmp_path):
        present = tmp_path / "talos-omni-deadbeef.iso"
        present.write_bytes(b"installer")
        mgr = _manager_with_config({
            "isos": {"talos-omni": {
                "uri": "https://example.com/talos.iso",
                "checksum": "sha256:abc123",
            }}
        })
        with patch("boxman.manager_parts.images.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.cache_path_for.return_value = str(present)
            mock_cache_cls.from_config.return_value = mock_cache
            mock_cache_cls.verify_checksum = MagicMock(return_value=True)
            result = mgr._resolve_isos()
        assert result == {"talos-omni": str(present)}
        mock_cache_cls.verify_checksum.assert_called_once_with(
            str(present), "sha256:abc123")

    def test_a_freshly_downloaded_bad_iso_never_reaches_the_cache(
            self, tmp_path):
        """A download that fails verification must not be published."""
        dest = tmp_path / "talos-omni-deadbeef.iso"

        def _fake_download(url, path):
            with open(path, "wb") as handle:
                handle.write(b"corrupt")
            return True

        mgr = _manager_with_config({
            "isos": {"talos-omni": {
                "uri": "https://example.com/talos.iso",
                "checksum": "sha256:abc123",
            }}
        })
        with patch("boxman.manager_parts.images.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.cache_path_for.return_value = str(dest)
            mock_cache_cls.from_config.return_value = mock_cache
            mock_cache_cls.verify_checksum = MagicMock(return_value=False)
            with patch.object(type(mgr), "_download_iso",
                              side_effect=_fake_download):
                with pytest.raises(ProvisionError,
                                   match="does not match its declared checksum"):
                    mgr._resolve_isos()

        assert not dest.exists()
        assert list(tmp_path.iterdir()) == []


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

    def test_resolves_string_cdrom_shorthand(self):
        # `cdroms: [talos-omni]` (list of strings) is a natural YAML shorthand
        mgr = _manager_with_config({})
        vm_info = {"boot_order": ["cdrom", "hd"], "cdroms": ["talos-omni"]}
        result = mgr._inject_resolved_iso(vm_info, {"talos-omni": "/cache/talos.iso"})
        assert result["cdroms"][0] == {"name": "talos-omni", "source": "/cache/talos.iso"}
        assert result["_resolved_iso_path"] == "/cache/talos.iso"

    def test_raises_on_invalid_cdrom_entry(self):
        mgr = _manager_with_config({})
        vm_info = {"boot_order": ["cdrom", "hd"], "cdroms": [{"foo": "bar"}]}
        with pytest.raises(ValueError, match="invalid cdroms entry"):
            mgr._inject_resolved_iso(vm_info, {})


class TestResolvedNetworkNames:
    def test_namespaces_first_nic(self):
        mgr = _manager_with_config({"project": "myproj"})
        names = mgr._resolved_network_names(
            "talos", {"networks": [{"name": "talos-net"}]})
        assert names == ["bprj__myproj__bprj__clstr__talos__clstr__talos-net"]

    def test_empty_when_no_networks(self):
        mgr = _manager_with_config({"project": "myproj"})
        assert mgr._resolved_network_names("talos", {}) == []


class TestValidateBaseImages:
    def _mgr(self, clusters, isos=None):
        cfg = {"project": "p", "clusters": clusters}
        if isos is not None:
            cfg["isos"] = isos
        return _manager_with_config(cfg)

    def test_cdrom_boot_without_cdroms_is_invalid(self):
        mgr = self._mgr(
            {"talos": {"vms": {"cp-01": {"boot_order": ["cdrom", "hd"]}}}},
            isos={"talos-omni": {"uri": "x"}})
        with pytest.raises(ValueError, match="invalid 'cdroms:'"):
            mgr.validate_base_images()

    def test_cdrom_boot_unknown_iso_is_invalid(self):
        mgr = self._mgr(
            {"talos": {"vms": {"cp-01": {
                "boot_order": ["cdrom", "hd"],
                "cdroms": [{"name": "nope"}]}}}},
            isos={"talos-omni": {"uri": "x"}})
        with pytest.raises(ValueError, match="unknown iso 'nope'"):
            mgr.validate_base_images()

    def test_cdrom_boot_valid_passes(self):
        mgr = self._mgr(
            {"talos": {"vms": {"cp-01": {
                "boot_order": ["cdrom", "hd"],
                "cdroms": [{"name": "talos-omni"}]}}}},
            isos={"talos-omni": {"uri": "x"}})
        mgr.validate_base_images()  # no raise

    def test_hd_boot_still_requires_base_image(self):
        mgr = self._mgr({"c": {"vms": {"v": {}}}})
        with pytest.raises(ValueError, match="no base_image"):
            mgr.validate_base_images()

    def test_network_boot_needs_no_base_image(self):
        mgr = self._mgr({"c": {"vms": {"v": {"boot_order": ["network", "hd"]}}}})
        mgr.validate_base_images()  # no raise
