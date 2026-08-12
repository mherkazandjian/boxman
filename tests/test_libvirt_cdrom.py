"""
Unit tests for boxman.providers.libvirt.cdrom.CDROMManager.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.cdrom import CDROMManager

pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True, stderr: str = "", return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


@pytest.fixture
def cd() -> CDROMManager:
    return CDROMManager("vm01", provider_config={"use_sudo": False})


class TestGenerateXml:

    def test_xml_contains_expected_parts(self, cd: CDROMManager):
        xml = cd._generate_cdrom_xml("/tmp/foo.iso", "hdc")
        assert "device='cdrom'" in xml
        assert "file='/tmp/foo.iso'" in xml
        assert "dev='hdc'" in xml
        assert "bus='ide'" in xml
        assert "<readonly/>" in xml


class TestBusForTarget:
    # _find_next_available_target falls back to sd* once the IDE slots are
    # taken; the device XML bus must match the target prefix (#85 item 26)

    def test_hd_targets_are_ide(self, cd: CDROMManager):
        assert cd._bus_for_target("hdc") == "ide"

    def test_sd_targets_are_sata(self, cd: CDROMManager):
        assert cd._bus_for_target("sda") == "sata"

    def test_generated_xml_matches_sata_target(self, cd: CDROMManager):
        xml = cd._generate_cdrom_xml("/tmp/foo.iso", "sda")
        assert "dev='sda' bus='sata'" in xml

    def test_detach_xml_matches_sata_target(self, cd: CDROMManager):
        written = {}

        def capture(*args, **kwargs):
            # the temp XML still exists while detach-device runs
            written["xml"] = Path(args[2]).read_text()
            return _result()

        with patch.object(cd.virsh, "execute", side_effect=capture):
            assert cd.detach_cdrom("sda") is True
        assert "dev='sda' bus='sata'" in written["xml"]


class TestFindNextAvailableTarget:
    # domblklist --details has 4 columns: Type Device Target Source
    DOMBLKLIST = (
        "Type  Device  Target  Source\n"
        "------------------------------------------------\n"
        "file  disk    vda     /var/lib/libvirt/vm01.qcow2\n"
    )

    def test_returns_hda_when_slots_free(self, cd: CDROMManager):
        with patch.object(cd.virsh, "execute", return_value=_result(stdout=self.DOMBLKLIST)):
            # vda is used, so hda is free
            assert cd._find_next_available_target() == "hda"

    def test_skips_used_targets(self, cd: CDROMManager):
        blk = (
            "Type  Device  Target  Source\n"
            "------------------------------------------------\n"
            "file  disk    hda     /tmp/x\n"
            "file  disk    hdb     /tmp/y\n"
        )
        with patch.object(cd.virsh, "execute", return_value=_result(stdout=blk)):
            assert cd._find_next_available_target() == "hdc"

    def test_falls_back_to_sd_when_all_ide_used(self, cd: CDROMManager):
        blk = (
            "Type  Device  Target  Source\n"
            "------------------------------------------------\n"
            "file  disk    hda     /tmp/a\n"
            "file  disk    hdb     /tmp/b\n"
            "file  disk    hdc     /tmp/c\n"
            "file  disk    hdd     /tmp/d\n"
        )
        with patch.object(cd.virsh, "execute", return_value=_result(stdout=blk)):
            assert cd._find_next_available_target() == "sda"


class TestAttachCDROM:

    def test_missing_iso_returns_false(self, cd: CDROMManager, captured_logs):
        assert cd.attach_cdrom("/nonexistent.iso") is False
        assert any("ISO file does not exist" in rec.message for rec in captured_logs.records)

    def test_attaches_with_persistent_flag_by_default(self, cd: CDROMManager, tmp_path: Path):
        iso = tmp_path / "ubuntu.iso"
        iso.write_bytes(b"fake iso")
        with patch.object(cd, "_find_next_available_target", return_value="hdc"), \
             patch.object(cd.virsh, "execute", return_value=_result()) as execute:
            assert cd.attach_cdrom(str(iso)) is True
        args, _kwargs = execute.call_args
        assert args[0] == "attach-device"
        assert args[1] == "vm01"
        assert "--persistent" in args

    def test_non_persistent_omits_flag(self, cd: CDROMManager, tmp_path: Path):
        iso = tmp_path / "ubuntu.iso"
        iso.write_bytes(b"fake iso")
        with patch.object(cd, "_find_next_available_target", return_value="hdc"), \
             patch.object(cd.virsh, "execute", return_value=_result()) as execute:
            cd.attach_cdrom(str(iso), persistent=False)
        args, _kwargs = execute.call_args
        assert "--persistent" not in args

    def test_no_available_target_returns_false(self, cd: CDROMManager, tmp_path: Path):
        iso = tmp_path / "u.iso"
        iso.write_bytes(b"x")
        with patch.object(cd, "_find_next_available_target", return_value=None):
            assert cd.attach_cdrom(str(iso)) is False

    def test_command_failure_returns_false(self, cd: CDROMManager, tmp_path: Path):
        iso = tmp_path / "u.iso"
        iso.write_bytes(b"x")
        with patch.object(cd, "_find_next_available_target", return_value="hdc"), \
             patch.object(cd.virsh, "execute", return_value=_result(ok=False, stderr="nope")) as execute:
            assert cd.attach_cdrom(str(iso)) is False
        # warn=True keeps the error branch live — without it execute raises
        # and the `if not result.ok` check is dead code (#85 item 38)
        assert execute.call_args.kwargs.get("warn") is True

    def test_cleans_up_temp_xml_on_exception(self, cd: CDROMManager, tmp_path: Path):
        """If execute raises, the temp XML file must still be removed."""
        iso = tmp_path / "u.iso"
        iso.write_bytes(b"x")
        recorded_path = {}

        orig_nt = __import__("tempfile").NamedTemporaryFile

        def tracker(*a, **kw):
            handle = orig_nt(*a, **kw)
            recorded_path["path"] = handle.name
            return handle

        with patch.object(cd, "_find_next_available_target", return_value="hdc"), \
             patch("boxman.providers.libvirt.cdrom.tempfile.NamedTemporaryFile", side_effect=tracker), \
             patch.object(cd.virsh, "execute", side_effect=ValueError("boom")):
            assert cd.attach_cdrom(str(iso)) is False
        # temp file should have been unlinked
        assert recorded_path["path"] is not None
        assert not Path(recorded_path["path"]).exists()


class TestDetachCDROM:

    def test_success_includes_readonly_xml(self, cd: CDROMManager):
        with patch.object(cd.virsh, "execute", return_value=_result()) as execute:
            assert cd.detach_cdrom("hdc") is True
        # first call is detach-device
        args, _kwargs = execute.call_args
        assert args[0] == "detach-device"

    def test_failure_returns_false(self, cd: CDROMManager):
        with patch.object(cd.virsh, "execute", return_value=_result(ok=False, stderr="x")) as execute:
            assert cd.detach_cdrom("hdc") is False
        # warn=True keeps the error branch live — without it execute raises
        # and the `if not result.ok` check is dead code (#85 item 38)
        assert execute.call_args.kwargs.get("warn") is True


class TestChangeMedia:

    def test_missing_file_returns_false(self, cd: CDROMManager):
        assert cd.change_media("hdc", "/missing.iso") is False

    def test_change_media_happy_path(self, cd: CDROMManager, tmp_path: Path):
        iso = tmp_path / "new.iso"
        iso.write_bytes(b"x")
        with patch.object(cd.virsh, "execute", return_value=_result()) as execute:
            assert cd.change_media("hdc", str(iso)) is True
        args, _kwargs = execute.call_args
        assert args[0] == "change-media"
        assert args[1] == "vm01"
        assert args[2] == "hdc"
        assert "--live" in args
        assert "--config" in args


class TestConfigureFromConfig:

    def test_missing_source_returns_false(self, cd: CDROMManager):
        assert cd.configure_from_config({"name": "iso1"}) is False

    def test_delegates_to_attach_cdrom(self, cd: CDROMManager):
        with patch.object(cd, "attach_cdrom", return_value=True) as attach:
            cd.configure_from_config({"source": "/x.iso", "target": "hdd"})
        attach.assert_called_once_with(source_path="/x.iso", target_dev="hdd")


class TestGetAttachedCDROMs:

    def test_parses_domblklist_output(self, cd: CDROMManager):
        out = (
            "Type  Device  Target  Source\n"
            "---------------------------------------------\n"
            "file  cdrom   hdc     /isos/ubuntu.iso\n"
            "file  disk    vda     /disks/vm01.qcow2\n"
            "file  cdrom   hdd     /isos/seed.iso\n"  # seed ISO filtered out
            "file  cdrom   hde     -\n"                # empty source filtered out
        )
        with patch.object(cd.virsh, "execute", return_value=_result(stdout=out)):
            found = cd.get_attached_cdroms()
        assert found == [{"target": "hdc", "source": "/isos/ubuntu.iso"}]

    def test_empty_on_execute_failure(self, cd: CDROMManager):
        with patch.object(cd.virsh, "execute", return_value=_result(ok=False)):
            assert cd.get_attached_cdroms() == []


class TestXmlEscaping:
    # device XML interpolates paths/names into attributes — a value holding
    # & or a quote must be escaped or libvirt cannot parse it (#85 item 6)

    def test_source_path_is_escaped(self, cd: CDROMManager):
        xml = cd._generate_cdrom_xml("/tmp/a&b's.iso", "hdc")
        assert "file='/tmp/a&amp;b&apos;s.iso'" in xml

    def test_target_is_escaped(self, cd: CDROMManager):
        xml = cd._generate_cdrom_xml("/tmp/foo.iso", 'hd"c')
        assert "dev='hd&quot;c'" in xml

    def test_detach_target_is_escaped(self, cd: CDROMManager):
        written = {}

        def capture(*args, **kwargs):
            written["xml"] = Path(args[2]).read_text()
            return _result()

        with patch.object(cd.virsh, "execute", side_effect=capture):
            assert cd.detach_cdrom("hd&c") is True
        assert "dev='hd&amp;c'" in written["xml"]

    def test_plain_values_are_left_untouched(self, cd: CDROMManager):
        xml = cd._generate_cdrom_xml("/tmp/foo.iso", "hdc")
        assert "file='/tmp/foo.iso'" in xml
        assert "dev='hdc'" in xml
