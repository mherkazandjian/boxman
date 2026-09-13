"""
Unit tests for boxman.providers.libvirt.virsh_edit.VirshEdit.

Currently pins the issue #85 item 38 fixes: the ``if not result.ok``
failure branches in the hot-update helpers must be reachable — the
virsh calls are made with ``warn=True`` and a failed command returns
False instead of raising RuntimeError.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
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


class TestConfigureCpuMemoryEditsPersistentConfig:
    """configure_cpu_memory must read the *inactive* (persistent) XML.

    A direct-boot (ISO/PXE) VM is still running virt-install's transient
    install XML (cdrom-first, on_reboot=destroy, install media inserted) when
    the post-create configure step runs. Redefining from the live XML would
    persist that and re-run the installer on every boot.

    The two fixtures below deliberately differ. An earlier version of this
    test returned one XML for both dumps and carried no boot order, reboot
    policy or CD-ROM, so it could assert that ``--inactive`` was passed but not
    that the definition came from it -- an implementation that redefined live
    installer XML would have passed it unchanged (#171 C1).
    """

    #: what virt-install's transient install domain looks like
    _LIVE = ("<domain type='kvm'><name>d</name>"
             "<memory unit='KiB'>1048576</memory>"
             "<currentMemory unit='KiB'>1048576</currentMemory>"
             "<vcpu placement='static'>1</vcpu>"
             "<os><type>hvm</type><boot dev='cdrom'/><boot dev='hd'/></os>"
             "<on_reboot>destroy</on_reboot>"
             "<devices><disk type='file' device='cdrom'>"
             "<source file='/iso/install.iso'/><target dev='sda'/>"
             "</disk></devices></domain>")

    #: the real, persistent definition
    _INACTIVE = ("<domain type='kvm'><name>d</name>"
                 "<memory unit='KiB'>1048576</memory>"
                 "<currentMemory unit='KiB'>1048576</currentMemory>"
                 "<vcpu placement='static'>1</vcpu>"
                 "<os><type>hvm</type><boot dev='hd'/></os>"
                 "<on_reboot>restart</on_reboot>"
                 "<devices/></domain>")

    def _run(self, ve: VirshEdit):
        """Drive configure_cpu_memory; return (calls, xml handed to define)."""
        defined = {}

        def _exe(*args, **kwargs):
            if args and args[0] == "dumpxml":
                inactive = "--inactive" in args
                return _result(stdout=self._INACTIVE if inactive else self._LIVE)
            if args and args[0] == "define":
                defined["xml"] = open(args[1], encoding="utf-8").read()
            return _result()

        with patch.object(ve.virsh, "execute", side_effect=_exe) as exe:
            ok = ve.configure_cpu_memory("d", None, 2048)
        return ok, exe.call_args_list, defined.get("xml", "")

    def test_reads_inactive_xml_then_defines(self, ve: VirshEdit):
        ok, calls, _xml = self._run(ve)
        assert ok is True
        first = calls[0].args
        assert first[0] == "dumpxml"
        assert "d" in first and "--inactive" in first, first
        assert any(c.args and c.args[0] == "define" for c in calls)

    def test_the_definition_comes_from_the_persistent_xml(self, ve: VirshEdit):
        """The assertions that matter: what was *defined*, not which flag was
        passed to the dump.

        Parsed, not string-matched: lxml re-serialises with double quotes, so
        `"<boot dev='cdrom'/>" not in xml` is true however the domain is
        configured -- an absence assertion that cannot match is no assertion.
        """
        _ok, _calls, xml = self._run(ve)
        assert xml, "nothing was handed to `virsh define`"
        root = ET.fromstring(xml)

        boots = [b.get("dev") for b in root.findall("os/boot")]
        assert boots == ["hd"], f"the installer's boot order was persisted: {boots}"
        reboot = root.findtext("on_reboot")
        assert reboot != "destroy", (
            "on_reboot=destroy was persisted; the domain would vanish on reboot")
        cdroms = [d for d in root.findall("devices/disk")
                  if d.get("device") == "cdrom"]
        assert not cdroms, "the install media was persisted"

    def test_the_new_memory_actually_reaches_the_definition(self, ve: VirshEdit):
        """Otherwise the test above could pass on an empty document."""
        _ok, _calls, xml = self._run(ve)
        root = ET.fromstring(xml)
        assert root.findtext("memory") == "2097152", xml
