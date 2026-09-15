"""Unit tests for BoxmanManager ISO resolution helpers."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import ConfigError, ProvisionError
from boxman.manager import BoxmanManager
from boxman.utils.mac import canonical_mac

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


class TestResolvedNetworkSpecs:
    def test_namespaces_and_lowercases_mac(self):
        mgr = _manager_with_config({"project": "myproj"})
        specs = mgr._resolved_network_specs(
            "pve", {"networks": [{"name": "pvenet", "mac": "52:54:00:77:00:AA"}]})
        assert specs == [{"name": "bprj__myproj__bprj__clstr__pve__clstr__pvenet",
                          "mac": "52:54:00:77:00:aa"}]

    def test_no_mac_is_none(self):
        mgr = _manager_with_config({"project": "myproj"})
        specs = mgr._resolved_network_specs("pve", {"networks": [{"name": "pvenet"}]})
        assert specs == [{"name": "bprj__myproj__bprj__clstr__pve__clstr__pvenet",
                          "mac": None}]

    def test_bare_string_entries_are_namespaced(self):
        mgr = _manager_with_config({"project": "myproj"})
        specs = mgr._resolved_network_specs("pve", {"networks": ["pvenet"]})
        assert specs == [{"name": "bprj__myproj__bprj__clstr__pve__clstr__pvenet",
                          "mac": None}]

    def test_unnamed_entries_are_skipped(self):
        mgr = _manager_with_config({"project": "myproj"})
        assert mgr._resolved_network_specs("pve", {"networks": [{"mac": "x"}, None, ""]}) == []

    def test_names_wrapper_still_returns_strings(self):
        mgr = _manager_with_config({"project": "myproj"})
        names = mgr._resolved_network_names(
            "pve", {"networks": [{"name": "pvenet", "mac": "52:54:00:77:00:11"}]})
        assert names == ["bprj__myproj__bprj__clstr__pve__clstr__pvenet"]

    def test_resolve_iso_config_stores_specs(self):
        cfg = {"project": "p", "clusters": {"c": {"vms": {"v": {
            "boot_order": ["cdrom", "hd"],
            "cdroms": [{"source": "/x.iso"}],
            "networks": [{"name": "n", "mac": "52:54:00:77:00:11"}]}}}}}
        mgr = _manager_with_config(cfg)
        mgr._resolve_iso_config()
        assert cfg["clusters"]["c"]["vms"]["v"]["_resolved_networks"] == [
            {"name": "bprj__p__bprj__clstr__c__clstr__n", "mac": "52:54:00:77:00:11"}]


class TestValidateDirectBootNetworks:
    def _mgr(self, vms):
        return _manager_with_config({"project": "p", "clusters": {"c": {"vms": vms}}})

    @staticmethod
    def _iso_vm(**extra):
        return {"boot_order": ["cdrom", "hd"], "cdroms": [{"source": "/x.iso"}], **extra}

    def test_bad_mac_is_invalid(self):
        mgr = self._mgr({"v": self._iso_vm(networks=[{"name": "n", "mac": "not-a-mac"}])})
        with pytest.raises(ValueError, match="invalid mac"):
            mgr.validate_base_images()

    def test_duplicate_mac_across_vms_is_invalid(self):
        mgr = self._mgr({
            "v1": self._iso_vm(networks=[{"name": "n", "mac": "52:54:00:77:00:11"}]),
            "v2": {"boot_order": ["network", "hd"],
                   "networks": [{"name": "n", "mac": "52:54:00:77:00:11"}]},
        })
        with pytest.raises(ValueError, match="already used by c.vms.v1"):
            mgr.validate_base_images()

    def test_unnamed_entry_is_invalid(self):
        mgr = self._mgr({"v": self._iso_vm(networks=[{"mac": "52:54:00:77:00:11"}])})
        with pytest.raises(ValueError, match="has no 'name'"):
            mgr.validate_base_images()

    def test_valid_mac_passes(self):
        mgr = self._mgr({
            "v1": self._iso_vm(networks=[{"name": "n", "mac": "52:54:00:77:00:11"}]),
            "v2": self._iso_vm(networks=["n", {"name": "m"}]),
        })
        mgr.validate_base_images()  # no raise

    def test_hd_boot_networks_are_not_validated(self):
        # `networks:` is inert on cloned VMs (NICs come from the template), so a
        # stray value there must not block a provision
        mgr = self._mgr({"v": {"base_image": "tpl",
                               "networks": [{"name": "n", "mac": "nope"}]}})
        mgr.validate_base_images()  # no raise


class TestIsoPublication:
    """
    Publishing a downloaded ISO must never replace one that is already there:
    a guest may have it attached, and its configured path would then name
    different media on reopening (#164 FB-5).
    """

    def _manager(self):
        return _manager_with_config({
            "isos": {"talos-omni": {
                "uri": "https://example.com/talos.iso",
                "checksum": "sha256:abc123",
            }}
        })

    @staticmethod
    def _writes(content):
        def _download(url, path):
            with open(path, "wb") as handle:
                handle.write(content)
            return True
        return _download

    def test_publication_that_cannot_be_atomic_is_refused(self, tmp_path):
        """
        The obvious fallback — check the destination is absent, then
        os.replace() — is a race: another run can publish and attach between
        the two, and this one then replaces media a guest is using.
        """
        dest = tmp_path / "talos-omni-deadbeef.iso"
        mgr = self._manager()

        with patch("boxman.manager_parts.images.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.cache_path_for.return_value = str(dest)
            mock_cache_cls.from_config.return_value = mock_cache
            mock_cache_cls.verify_checksum = MagicMock(return_value=True)
            with patch.object(type(mgr), "_download_iso",
                              side_effect=self._writes(b"installer")), \
                 patch("boxman.manager_parts.images.os.link",
                       side_effect=OSError("cross-device link")):
                with pytest.raises(ProvisionError, match="could not publish"):
                    mgr._resolve_isos()

        assert not dest.exists()
        assert list(tmp_path.iterdir()) == []

    def test_a_concurrent_winner_must_satisfy_this_declaration(self, tmp_path):
        """
        Only this run's staging file was verified. Two projects can share a
        name/URI cache key while declaring different checksums, so the file
        that won the race need not satisfy this caller's declaration.
        """
        dest = tmp_path / "talos-omni-deadbeef.iso"
        mgr = self._manager()

        def _download_and_lose_the_race(url, path):
            with open(path, "wb") as handle:
                handle.write(b"ours")
            dest.write_bytes(b"theirs")
            return True

        def _verify(path, _spec):
            # this run's staging bytes are fine; the winner's are not
            return path != str(dest)

        with patch("boxman.manager_parts.images.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.cache_path_for.return_value = str(dest)
            mock_cache_cls.from_config.return_value = mock_cache
            mock_cache_cls.verify_checksum = MagicMock(side_effect=_verify)
            with patch.object(type(mgr), "_download_iso",
                              side_effect=_download_and_lose_the_race):
                with pytest.raises(ProvisionError,
                                   match="published by another run"):
                    mgr._resolve_isos()

        # preserved, not replaced or removed — a guest may have it attached
        assert dest.read_bytes() == b"theirs"

    def test_a_concurrent_winner_that_matches_is_accepted(self, tmp_path):
        dest = tmp_path / "talos-omni-deadbeef.iso"
        mgr = self._manager()

        def _download_and_lose_the_race(url, path):
            with open(path, "wb") as handle:
                handle.write(b"ours")
            dest.write_bytes(b"theirs")
            return True

        with patch("boxman.manager_parts.images.ImageCache") as mock_cache_cls:
            mock_cache = MagicMock()
            mock_cache.cache_path_for.return_value = str(dest)
            mock_cache_cls.from_config.return_value = mock_cache
            mock_cache_cls.verify_checksum = MagicMock(return_value=True)
            with patch.object(type(mgr), "_download_iso",
                              side_effect=_download_and_lose_the_race):
                result = mgr._resolve_isos()

        assert result == {"talos-omni": str(dest)}
        assert dest.read_bytes() == b"theirs"


class TestMacCollisionsAcrossSpellingsAndAdapters:
    """#171 A1/A2. A mac is an address, not a string.

    libvirt parses `52:54:0:c:1:1` and stores `52:54:00:0c:01:01`, so a check
    that compares spellings lets one address through twice, and a `dhcp.hosts`
    reservation silently fails to match the NIC written for it.
    """

    def _mgr(self, vms):
        return _manager_with_config({"project": "p", "clusters": {"c": {"vms": vms}}})

    @staticmethod
    def _iso_vm(**extra):
        return {"boot_order": ["cdrom", "hd"], "cdroms": [{"source": "/x.iso"}], **extra}

    def test_two_spellings_of_one_address_collide(self):
        mgr = self._mgr({
            "v1": self._iso_vm(networks=[{"name": "n", "mac": "52:54:0:c:1:1"}]),
            "v2": self._iso_vm(networks=[{"name": "n", "mac": "52:54:00:0c:01:01"}]),
        })
        with pytest.raises(ValueError, match="already used"):
            mgr.validate_base_images()

    def test_the_stored_spec_is_canonical(self):
        """It is what reaches virt-install, so it must compare equal to a
        reservation written the padded way."""
        mgr = _manager_with_config({"project": "p"})
        specs = mgr._resolved_network_specs(
            "c", {"networks": [{"name": "n", "mac": "52:54:0:C:1:1"}]})
        assert specs[0]["mac"] == "52:54:00:0c:01:01"

    def test_a_pin_collides_with_an_adapter_on_another_vm(self):
        mgr = self._mgr({
            "v1": self._iso_vm(networks=[{"name": "n", "mac": "52:54:00:0c:01:01"}]),
            "v2": {"base_image": "t", "network_adapters": [{"mac": "52:54:00:0c:01:01"}]},
        })
        with pytest.raises(ValueError, match="network_adapters"):
            mgr.validate_base_images()

    def test_the_collision_is_found_whichever_side_is_declared_first(self):
        """The adapter index is built in one pass before the per-VM checks, so
        declaration order cannot hide the clash."""
        mgr = self._mgr({
            "aaa": {"base_image": "t", "network_adapters": [{"mac": "52:54:00:0c:01:02"}]},
            "zzz": self._iso_vm(networks=[{"name": "n", "mac": "52:54:0:c:1:2"}]),
        })
        with pytest.raises(ValueError, match="already used"):
            mgr.validate_base_images()

    def test_two_adapters_sharing_an_address_are_not_reported(self):
        """#171 A2(b). Adapters on separate isolated L2s may legitimately
        share one, and prohibiting it project-wide would refuse configurations
        outside what this validation is for."""
        mgr = self._mgr({
            "v1": {"base_image": "t", "network_adapters": [{"mac": "52:54:00:0c:01:03"}]},
            "v2": {"base_image": "t", "network_adapters": [{"mac": "52:54:00:0c:01:03"}]},
        })
        mgr.validate_base_images()

    def test_a_pin_and_its_own_reservation_resolve_to_one_address(self):
        """The positive case, with an actual reservation in it.

        The earlier version had none, so it asserted nothing: reservations do
        not take part in the duplicate index, and a config without one passes
        however the code behaves. Here the reservation is written padded and
        the pin unpadded -- the spelling difference #167's whole DHCP-hostname
        scheme depends on surviving.
        """
        reservation = "52:54:00:0c:01:09"
        pin = "52:54:0:C:1:9"
        cfg = {"project": "p", "clusters": {"c": {
            "networks": {"pvenet": {"mode": "nat", "ip": {
                "address": "10.77.0.1", "netmask": "255.255.255.0",
                "dhcp": {"hosts": [
                    {"mac": reservation, "ip": "10.77.0.19", "name": "node01"}]}}}},
            "vms": {"node01": {
                "boot_order": ["cdrom", "hd"],
                "cdroms": [{"source": "/x.iso"}],
                "networks": [{"name": "pvenet", "mac": pin}]}}}}}
        mgr = _manager_with_config(cfg)

        # not a duplicate: it is the same NIC as the reservation, not a clash
        mgr.validate_base_images()

        spec = mgr._resolved_network_specs(
            "c", cfg["clusters"]["c"]["vms"]["node01"])[0]
        assert spec["mac"] == canonical_mac(reservation), (
            "the pinned NIC and its DHCP reservation are different strings for "
            "one address; dnsmasq would never match them")


class TestDeclaredButUnresolvableNetworks:
    """#171 A4. The libvirt twin of #164 NET-C1: an explicit reference that
    cannot resolve must be refused, never dropped onto the default network."""

    def _mgr(self, vms):
        return _manager_with_config({"project": "p", "clusters": {"c": {"vms": vms}}})

    @staticmethod
    def _iso_vm(**extra):
        return {"boot_order": ["cdrom", "hd"], "cdroms": [{"source": "/x.iso"}], **extra}

    @pytest.mark.parametrize("entry", ["", "   "])
    def test_a_blank_entry_is_refused(self, entry):
        mgr = self._mgr({"v": self._iso_vm(networks=[entry])})
        with pytest.raises(ValueError, match="blank"):
            mgr.validate_base_images()

    def test_a_mapping_with_a_blank_name_is_refused(self):
        mgr = self._mgr({"v": self._iso_vm(networks=[{"name": "  "}])})
        with pytest.raises(ValueError, match="no 'name'"):
            mgr.validate_base_images()

    def test_one_bad_entry_among_good_ones_is_refused(self):
        mgr = self._mgr({"v": self._iso_vm(networks=["good", ""])})
        with pytest.raises(ValueError, match="blank"):
            mgr.validate_base_images()

    @pytest.mark.parametrize("networks", [None, []])
    def test_omitted_or_empty_still_means_the_default_network(self, networks):
        """`_resolve_iso_config` writes `_resolved_networks: []` even when no
        `networks:` was given, so an empty list must not be read as a failed
        resolution."""
        vm = self._iso_vm()
        if networks is not None:
            vm["networks"] = networks
        self._mgr({"v": vm}).validate_base_images()


class TestConfigIsRefusedBeforeAnythingIsBuilt:
    """#171 A3. These checks need no template, no libvirt and no filesystem,
    so paying for a template build or a forced deprovision before reporting a
    typo'd mac is avoidable -- and in `update` the networks had already been
    reconciled by the time the old call site was reached.
    """

    _BAD = {"project": "p", "clusters": {"c": {"vms": {
        "v": {"boot_order": ["cdrom", "hd"],
              "cdroms": [{"source": "/x.iso"}],
              "networks": [{"name": "n", "mac": "not-a-mac"}]}}}}}

    def test_provision_refuses_before_templates_or_clones(self):
        mgr = _manager_with_config(self._BAD)
        cls = type(mgr)
        with patch.object(cls, "_update_sessions_with_runtime"), \
             patch.object(cls, "ensure_templates_exist") as templates, \
             patch.object(cls, "_create_templates_impl") as build, \
             patch.object(cls, "deprovision") as deprovision, \
             patch.object(cls, "clone_vms") as clone, \
             patch.object(cls, "define_networks") as networks:
            with pytest.raises(ConfigError, match="invalid mac"):
                mgr.provision(SimpleNamespace(force=False, rebuild_templates=False))

        templates.assert_not_called()
        build.assert_not_called()
        deprovision.assert_not_called()
        clone.assert_not_called()
        networks.assert_not_called()

    def test_update_refuses_before_reconciling_anything(self):
        mgr = _manager_with_config(self._BAD)
        cls = type(mgr)
        with patch.object(cls, "_update_sessions_with_runtime"), \
             patch.object(cls, "ensure_templates_exist") as templates, \
             patch.object(cls, "reconcile_networks") as networks:
            with pytest.raises(ConfigError, match="invalid mac"):
                mgr.update(SimpleNamespace(
                    force=False, yes=False, dry_run=False, restart=False,
                    vms=None, cluster=None))

        templates.assert_not_called()
        networks.assert_not_called()

    def test_a_good_config_is_not_refused(self):
        """Without this the check could pass by rejecting everything."""
        good = {"project": "p", "clusters": {"c": {"vms": {
            "v": {"boot_order": ["cdrom", "hd"],
                  "cdroms": [{"source": "/x.iso"}],
                  "networks": [{"name": "n", "mac": "52:54:00:0c:01:09"}]}}}}}
        _manager_with_config(good).validate_direct_boot_config()
