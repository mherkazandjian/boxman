"""
Unit tests for boxman.providers.libvirt.virsh_edit.VirshEdit.

Currently pins the issue #85 item 38 fixes: the ``if not result.ok``
failure branches in the hot-update helpers must be reachable — the
virsh calls are made with ``warn=True`` and a failed command returns
False instead of raising RuntimeError.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from lxml import etree

from boxman.exceptions import ConfigError
from boxman.providers.libvirt.virsh_edit import VirshEdit

pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True, stderr: str = "",
            return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


@pytest.fixture
def ve() -> VirshEdit:
    return VirshEdit(provider_config={"use_sudo": False,
                                      "uri": "qemu:///system"})


class TestDeadErrorBranches:
    """Issue #85 item 38: each helper must pass warn=True so its
    not-ok branch is live, and must return False on failure."""

    def test_hot_set_vcpus_failure_returns_false(self, ve: VirshEdit):
        with patch.object(ve.virsh, "execute",
                          return_value=_result(ok=False, stderr="x")) as exe:
            assert ve.hot_set_vcpus("vm01", 4) is False
        assert exe.call_args.kwargs.get("warn") is True

    def test_hot_set_memory_failure_returns_false(self, ve: VirshEdit):
        with patch.object(ve.virsh, "execute",
                          return_value=_result(ok=False, stderr="x")) as exe:
            assert ve.hot_set_memory("vm01", 1024) is False
        assert exe.call_args.kwargs.get("warn") is True

    def test_hot_set_vcpus_success_returns_true(self, ve: VirshEdit):
        with patch.object(ve.virsh, "execute", return_value=_result()):
            assert ve.hot_set_vcpus("vm01", 4) is True

    def test_hot_set_memory_success_returns_true(self, ve: VirshEdit):
        with patch.object(ve.virsh, "execute", return_value=_result()):
            assert ve.hot_set_memory("vm01", 1024) is True


_DOMAIN_XML = """<domain type='kvm'>
  <name>vm01</name>
  <devices>
    {memballoon}
  </devices>
</domain>"""


def _domain_xml(memballoon: str = "") -> str:
    return _DOMAIN_XML.format(memballoon=memballoon)


class TestApplyMemballoonToXml:

    def _memballoon(self, xml: str):
        tree = etree.fromstring(xml.encode("utf-8"))
        matches = tree.xpath("//devices/memballoon")
        assert len(matches) == 1
        return matches[0]

    def test_creates_memballoon_with_free_page_reporting(self):
        xml = VirshEdit.apply_memballoon_to_xml(
            _domain_xml(), {"free_page_reporting": True})
        mb = self._memballoon(xml)
        assert mb.get("model") == "virtio"
        assert mb.get("freePageReporting") == "on"

    def test_upgrades_model_none_to_virtio(self):
        xml = VirshEdit.apply_memballoon_to_xml(
            _domain_xml("<memballoon model='none'/>"),
            {"free_page_reporting": True})
        mb = self._memballoon(xml)
        assert mb.get("model") == "virtio"
        assert mb.get("freePageReporting") == "on"

    def test_free_page_reporting_false(self):
        xml = VirshEdit.apply_memballoon_to_xml(
            _domain_xml("<memballoon model='virtio'/>"),
            {"free_page_reporting": False})
        assert self._memballoon(xml).get("freePageReporting") == "off"

    def test_autodeflate_true(self):
        xml = VirshEdit.apply_memballoon_to_xml(
            _domain_xml(), {"autodeflate": True})
        mb = self._memballoon(xml)
        assert mb.get("model") == "virtio"
        assert mb.get("autodeflate") == "on"

    def test_autodeflate_false(self):
        xml = VirshEdit.apply_memballoon_to_xml(
            _domain_xml("<memballoon model='virtio' autodeflate='on'/>"),
            {"autodeflate": False})
        assert self._memballoon(xml).get("autodeflate") == "off"

    def test_autodeflate_preserves_other_balloon_settings(self):
        xml = VirshEdit.apply_memballoon_to_xml(
            _domain_xml(
                "<memballoon model='virtio' freePageReporting='on'>"
                "<stats period='5'/></memballoon>"),
            {"autodeflate": True})
        mb = self._memballoon(xml)
        assert mb.get("freePageReporting") == "on"
        assert mb.get("autodeflate") == "on"
        assert mb.find("stats").get("period") == "5"

    def test_stats_period(self):
        xml = VirshEdit.apply_memballoon_to_xml(
            _domain_xml(), {"stats_period": 10})
        assert self._memballoon(xml).find("stats").get("period") == "10"

    def test_absent_keys_leave_memballoon_untouched(self):
        xml = VirshEdit.apply_memballoon_to_xml(
            _domain_xml("<memballoon model='virtio'/>"), {})
        mb = self._memballoon(xml)
        assert mb.get("model") == "virtio"
        assert mb.get("freePageReporting") is None
        assert mb.find("stats") is None

    def test_absent_autodeflate_key_preserves_existing_attribute(self):
        xml = VirshEdit.apply_memballoon_to_xml(
            _domain_xml(
                "<memballoon model='virtio' autodeflate='on'/>"),
            {"free_page_reporting": True})
        assert self._memballoon(xml).get("autodeflate") == "on"


class TestConfigureMemballoon:

    def test_none_config_is_noop(self, ve: VirshEdit):
        with patch.object(ve.virsh, "execute") as exe:
            assert ve.configure_memballoon("vm01", None) is True
        exe.assert_not_called()

    def test_empty_config_is_noop(self, ve: VirshEdit):
        with patch.object(ve.virsh, "execute") as exe:
            assert ve.configure_memballoon("vm01", {}) is True
        exe.assert_not_called()

    def test_success_returns_true(self, ve: VirshEdit):
        with patch.object(ve.virsh, "execute",
                          return_value=_result(stdout=_domain_xml())):
            assert ve.configure_memballoon(
                "vm01", {"free_page_reporting": True}) is True

    def test_define_failure_returns_false(self, ve: VirshEdit):
        def _execute(*args, **kwargs):
            if args[0] == "define":
                # redefine_domain calls execute without warn=True, so a
                # failed define raises instead of returning a non-ok result
                raise RuntimeError("invalid xml")
            return _result(stdout=_domain_xml())

        with patch.object(ve.virsh, "execute", side_effect=_execute):
            assert ve.configure_memballoon(
                "vm01", {"free_page_reporting": True}) is False


class TestMemballoonValidation:
    """Review P3: malformed config values must raise ConfigError instead
    of silently producing the opposite setting."""

    def test_free_page_reporting_string_rejected(self):
        with pytest.raises(ConfigError):
            VirshEdit.apply_memballoon_to_xml(
                _domain_xml(), {"free_page_reporting": "false"})

    @pytest.mark.parametrize("value", ["false", 0, 1, None])
    def test_autodeflate_non_bool_rejected(self, value):
        with pytest.raises(ConfigError, match="memballoon.autodeflate"):
            VirshEdit.apply_memballoon_to_xml(
                _domain_xml(), {"autodeflate": value})

    def test_stats_period_bool_rejected(self):
        with pytest.raises(ConfigError):
            VirshEdit.apply_memballoon_to_xml(
                _domain_xml(), {"stats_period": True})

    def test_stats_period_float_rejected(self):
        with pytest.raises(ConfigError):
            VirshEdit.apply_memballoon_to_xml(
                _domain_xml(), {"stats_period": 2.5})

    def test_stats_period_zero_rejected(self):
        with pytest.raises(ConfigError):
            VirshEdit.apply_memballoon_to_xml(
                _domain_xml(), {"stats_period": 0})

    def test_non_dict_config_rejected(self):
        with pytest.raises(ConfigError):
            VirshEdit.apply_memballoon_to_xml(_domain_xml(), "true")

    def test_stats_period_none_removes_stats(self):
        xml = VirshEdit.apply_memballoon_to_xml(
            _domain_xml("<memballoon model='virtio'>"
                        "<stats period='5'/></memballoon>"),
            {"stats_period": None})
        tree = etree.fromstring(xml.encode("utf-8"))
        assert tree.xpath("//devices/memballoon/stats") == []

    def test_configure_memballoon_reraises_config_error(self, ve: VirshEdit):
        with patch.object(ve.virsh, "execute",
                          return_value=_result(stdout=_domain_xml())):
            with pytest.raises(ConfigError):
                ve.configure_memballoon("vm01", {"stats_period": True})
