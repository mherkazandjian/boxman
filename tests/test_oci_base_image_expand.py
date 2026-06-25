"""Unit tests for ``BoxmanManager._expand_oci_base_images``.

A ``base_image: oci://…`` reference cannot be cloned directly (the clone path
needs a libvirt VM name), so it is expanded into an implicit ``templates`` entry
and the ``base_image`` is rewritten to that template's name.
"""

from __future__ import annotations

import logging

import pytest

from boxman.manager import BoxmanManager


pytestmark = pytest.mark.unit


def _manager(config: dict) -> BoxmanManager:
    """Build a BoxmanManager without running __init__ (no cache side effects)."""
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = config
    mgr.logger = logging.getLogger("test.oci_expand")
    return mgr


class TestExpandOciBaseImages:
    def test_cluster_level_oci_ref_is_expanded(self):
        ref = "oci://registry.example.com/boxman/ubuntu-24.04:latest"
        cfg = {"clusters": {"c1": {"base_image": ref, "vms": {"vm1": {}}}}}
        mgr = _manager(cfg)

        mgr._expand_oci_base_images()

        tpl_name = BoxmanManager._oci_template_name(ref)
        assert cfg["clusters"]["c1"]["base_image"] == tpl_name
        assert tpl_name in cfg["templates"]
        assert cfg["templates"][tpl_name] == {"name": tpl_name, "image": {"uri": ref}}

    def test_vm_level_oci_ref_is_expanded(self):
        ref = "oci://reg/repo:tag"
        cfg = {"clusters": {"c1": {"base_image": "rocky9", "vms": {"vm1": {"base_image": ref}}}}}
        mgr = _manager(cfg)

        mgr._expand_oci_base_images()

        tpl_name = BoxmanManager._oci_template_name(ref)
        # cluster base_image (non-oci) untouched
        assert cfg["clusters"]["c1"]["base_image"] == "rocky9"
        # vm base_image rewritten
        assert cfg["clusters"]["c1"]["vms"]["vm1"]["base_image"] == tpl_name
        assert tpl_name in cfg["templates"]

    def test_non_oci_base_image_untouched(self):
        cfg = {
            "templates": {"t1": {"name": "rocky9-base"}},
            "clusters": {"c1": {"base_image": "rocky9-base", "vms": {"vm1": {}}}},
        }
        mgr = _manager(cfg)

        mgr._expand_oci_base_images()

        assert cfg["clusters"]["c1"]["base_image"] == "rocky9-base"
        # existing templates preserved, nothing new injected
        assert cfg["templates"] == {"t1": {"name": "rocky9-base"}}

    def test_same_ref_dedupes_to_one_template(self):
        ref = "oci://reg/repo:tag"
        cfg = {
            "clusters": {
                "c1": {"base_image": ref, "vms": {"vm1": {"base_image": ref}}},
                "c2": {"base_image": ref, "vms": {}},
            }
        }
        mgr = _manager(cfg)

        mgr._expand_oci_base_images()

        tpl_name = BoxmanManager._oci_template_name(ref)
        assert list(cfg["templates"].keys()) == [tpl_name]
        assert cfg["clusters"]["c1"]["base_image"] == tpl_name
        assert cfg["clusters"]["c1"]["vms"]["vm1"]["base_image"] == tpl_name
        assert cfg["clusters"]["c2"]["base_image"] == tpl_name

    def test_distinct_refs_get_distinct_templates(self):
        ref_a = "oci://reg/repo:a"
        ref_b = "oci://reg/repo:b"
        cfg = {
            "clusters": {
                "c1": {"base_image": ref_a, "vms": {}},
                "c2": {"base_image": ref_b, "vms": {}},
            }
        }
        mgr = _manager(cfg)

        mgr._expand_oci_base_images()

        assert len(cfg["templates"]) == 2
        assert BoxmanManager._oci_template_name(ref_a) in cfg["templates"]
        assert BoxmanManager._oci_template_name(ref_b) in cfg["templates"]

    def test_no_clusters_is_noop(self):
        cfg = {}
        mgr = _manager(cfg)
        mgr._expand_oci_base_images()  # must not raise
        assert "templates" not in cfg or cfg.get("templates") == {}


class TestOciTemplateName:
    def test_deterministic_and_safe(self):
        ref = "oci://registry.example.com/boxman/ubuntu-24.04:latest"
        name = BoxmanManager._oci_template_name(ref)
        assert name == BoxmanManager._oci_template_name(ref)  # deterministic
        assert name.startswith("boxman-oci-ubuntu-24.04-latest-")
        # colon stripped, only libvirt-safe chars remain
        assert ":" not in name
        assert all(c.isalnum() or c in "._-" for c in name)

    def test_scheme_optional(self):
        with_scheme = BoxmanManager._oci_template_name("oci://reg/repo:tag")
        without_scheme = BoxmanManager._oci_template_name("reg/repo:tag")
        assert with_scheme == without_scheme
