"""
Deterministic checks for the ISO-boot example boxes.

These boxes are excluded from the provisioning integration job -- they have no
`templates:` block and deliberately never create a login, so every assertion
in that suite would fail on them by design. That exclusion would otherwise
leave them untested entirely, and a pinned ISO whose URL or checksum has gone
stale would sit in the repository looking healthy. Everything verifiable
without downloading gigabytes is asserted here instead.

The URL reachability check is opt-in (``integration``): it is the one part
that needs the network.
"""

from __future__ import annotations

import glob
import os
import re

import pytest
import yaml

from boxman.utils.jinja_env import create_jinja_env

pytestmark = pytest.mark.unit

BOXES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "boxes")

ISO_BOXES = sorted(
    os.path.dirname(p) for p in glob.glob(os.path.join(BOXES_DIR, "*/conf.yml"))
    if os.path.basename(os.path.dirname(p)).endswith("-iso-boot")
)

# every box shipped as an ISO-boot example must be picked up here; a rename
# that silently drops one from this list is the failure mode being guarded
EXPECTED_AT_LEAST = {"nixos-25.05-iso-boot", "guix-1.5.0-iso-boot"}


def _config(box_dir: str) -> dict:
    env = create_jinja_env(box_dir)
    os.environ.setdefault("BOXMAN_CONF_DIR", box_dir)
    return yaml.safe_load(env.get_template("conf.yml").render())


def test_the_iso_boxes_are_discovered():
    names = {os.path.basename(d) for d in ISO_BOXES}
    assert EXPECTED_AT_LEAST <= names, f"missing ISO-boot boxes: {names}"


@pytest.mark.parametrize("box_dir", ISO_BOXES,
                         ids=[os.path.basename(d) for d in ISO_BOXES])
class TestIsoBoxConfig:

    def test_renders_and_parses(self, box_dir):
        assert _config(box_dir).get("project")

    def test_every_iso_is_pinned_with_a_sha256(self, box_dir):
        # an unpinned or unchecksummed ISO is how a box silently starts
        # booting something other than what it claims
        isos = _config(box_dir).get("isos") or {}
        assert isos, "an iso-boot box must declare isos:"
        for name, spec in isos.items():
            assert spec.get("uri", "").startswith("https://"), name
            checksum = spec.get("checksum", "")
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", checksum), (
                f"{name} needs a full sha256 checksum, got {checksum!r}")

    def test_uri_is_a_release_pinned_path(self, box_dir):
        # "latest" style aliases are republished in place, which silently
        # invalidates the checksum pinned next to them
        for name, spec in (_config(box_dir).get("isos") or {}).items():
            uri = spec["uri"]
            assert "latest" not in uri.lower(), (
                f"{name} points at a moving alias: {uri}")

    def test_vms_boot_from_a_declared_iso(self, box_dir):
        config = _config(box_dir)
        declared = set((config.get("isos") or {}).keys())
        vms = 0
        for cluster in config["clusters"].values():
            for vm_name, vm in (cluster.get("vms") or {}).items():
                vms += 1
                cdroms = [c["name"] for c in (vm.get("cdroms") or [])]
                assert cdroms, f"{vm_name} has no cdroms:"
                assert set(cdroms) <= declared, (
                    f"{vm_name} references an undeclared iso: {cdroms}")
                assert "cdrom" in (vm.get("boot_order") or []), vm_name
                assert vm.get("disk_size"), (
                    f"{vm_name} needs a disk_size to fall through to the ISO")
        assert vms, "no vms defined"

    def test_no_ssh_expectations_are_declared(self, box_dir):
        # these boxes cannot provision a login; declaring admin_user/ssh_config
        # would promise something the box does not deliver
        for cluster in _config(box_dir)["clusters"].values():
            assert "admin_user" not in cluster
            assert "base_image" not in cluster

    def test_readme_documents_the_limitation(self, box_dir):
        # scoped to the boxes added alongside this check: talos-iso-boot has
        # no README at all and ubuntu-24.04-live-iso-boot predates the
        # convention, and retrofitting those belongs in its own change
        if os.path.basename(box_dir) not in EXPECTED_AT_LEAST:
            pytest.skip("pre-existing box, README convention not retrofitted")
        readme = os.path.join(box_dir, "README.md")
        assert os.path.exists(readme), "an iso-boot box needs a README"
        text = open(readme, encoding="utf-8").read().lower()
        assert "boxman ssh" in text and "not" in text


@pytest.mark.integration
@pytest.mark.parametrize("box_dir", ISO_BOXES,
                         ids=[os.path.basename(d) for d in ISO_BOXES])
def test_pinned_iso_urls_are_still_reachable(box_dir):
    """The pinned URLs still resolve (a HEAD request, no download)."""
    import urllib.request
    for name, spec in (_config(box_dir).get("isos") or {}).items():
        request = urllib.request.Request(spec["uri"], method="HEAD")
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.status == 200, f"{name}: {spec['uri']}"
            assert int(response.headers.get("Content-Length", 0)) > 0
