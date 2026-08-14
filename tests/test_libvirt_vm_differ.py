"""
Unit tests for boxman.providers.libvirt.vm_differ.VMStateDiffer.

Currently pins the disk-diff regression from issue #85 item 23: a
desired disk whose image file already exists (leftover from a failed
earlier run) but which is not attached must be reported as an
attach-only new disk — not silently skipped.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from boxman.exceptions import ConfigError
from boxman.providers.libvirt.vm_differ import VMStateDiffer

pytestmark = pytest.mark.unit


@pytest.fixture
def _default_memballoon_state():
    """diff_vm probes the balloon device via virsh; default it to the
    libvirt defaults (no freePageReporting, no stats) so the disk-diff
    helper doesn't need another patch in every test."""
    with patch.object(VMStateDiffer, 'get_actual_memballoon',
                      return_value={'free_page_reporting': False,
                                    'autodeflate': False,
                                    'stats_period': None}):
        yield


@pytest.fixture
def differ() -> VMStateDiffer:
    return VMStateDiffer(provider_config={"use_sudo": False,
                                          "uri": "qemu:///system"})


def _diff_disks(differ: VMStateDiffer,
                desired_disks: list[dict],
                actual_disks: list[dict],
                workdir: str) -> dict:
    """Run diff_vm with every non-disk probe mocked out."""
    with patch.object(differ, "get_vm_state", return_value="running"), \
         patch.object(differ, "get_actual_cpu",
                      return_value={"sockets": 1, "cores": 1, "threads": 1,
                                    "total_vcpus": 1, "current_vcpus": 1}), \
         patch.object(differ, "get_max_vcpus", return_value=1), \
         patch.object(differ, "get_actual_memory_mb", return_value=1024), \
         patch.object(differ, "get_max_memory_mb", return_value=1024), \
         patch.object(differ, "get_actual_disks", return_value=actual_disks), \
         patch.object(differ, "get_actual_cdroms", return_value=[]), \
         patch.object(differ, "get_actual_shared_folders", return_value=[]):
        return differ.diff_vm(
            domain_name="vm01",
            desired_cpus=None,
            desired_memory_mb=None,
            desired_disks=desired_disks,
            workdir=workdir,
            disk_prefix="vm01",
        )


@pytest.mark.usefixtures("_default_memballoon_state")
class TestDiskDiff:

    def test_new_disk_when_file_missing(self, differ: VMStateDiffer,
                                        tmp_path: Path):
        desired = [{"name": "data", "target": "vdb", "size": 1024}]
        diff = _diff_disks(differ, desired, [], str(tmp_path))
        assert diff["new_disks"] == desired
        assert "attach_only" not in diff["new_disks"][0]

    def test_stale_file_becomes_attach_only(self, differ: VMStateDiffer,
                                            tmp_path: Path):
        """Regression for issue #85 item 23: image exists but is not
        attached — must surface as attach-only, not vanish silently."""
        (tmp_path / "vm01_data.qcow2").write_bytes(b"x")
        desired = [{"name": "data", "target": "vdb", "size": 1024}]
        diff = _diff_disks(differ, desired, [], str(tmp_path))
        assert len(diff["new_disks"]) == 1
        assert diff["new_disks"][0]["attach_only"] is True
        assert diff["new_disks"][0]["name"] == "data"

    def test_stale_file_logs_warning(self, differ: VMStateDiffer,
                                     tmp_path: Path, caplog):
        (tmp_path / "vm01_data.qcow2").write_bytes(b"x")
        desired = [{"name": "data", "target": "vdb", "size": 1024}]
        with caplog.at_level("WARNING"):
            _diff_disks(differ, desired, [], str(tmp_path))
        assert any("attach" in rec.message and "vdb" in rec.message
                   for rec in caplog.records)

    def test_attached_disk_not_in_new_disks(self, differ: VMStateDiffer,
                                            tmp_path: Path):
        desired = [{"name": "data", "target": "vdb", "size": 1024}]
        actual = [{"target": "vdb",
                   "source": str(tmp_path / "vm01_data.qcow2"),
                   "size_mb": 1024}]
        diff = _diff_disks(differ, desired, actual, str(tmp_path))
        assert diff["new_disks"] == []
        assert diff["resize_disks"] == []


_BALLOON_XML = """<domain type='kvm'>
  <name>vm01</name>
  <devices>
    {memballoon}
  </devices>
</domain>"""


def _balloon_xml(memballoon: str = "") -> str:
    return _BALLOON_XML.format(memballoon=memballoon)


class TestMemballoonState:
    """get_actual_memballoon / normalize_memballoon_config."""

    def test_absent_element_is_defaults(self, differ: VMStateDiffer):
        with patch.object(differ.virsh_edit, "get_domain_xml",
                          return_value=_balloon_xml()):
            assert differ.get_actual_memballoon("vm01") == {
                "free_page_reporting": False, "autodeflate": False,
                "stats_period": None}

    def test_fpr_autodeflate_on_and_stats(self, differ: VMStateDiffer):
        xml = _balloon_xml(
            "<memballoon model='virtio' freePageReporting='on' "
            "autodeflate='on'>"
            "<stats period='5'/></memballoon>")
        with patch.object(differ.virsh_edit, "get_domain_xml",
                          return_value=xml):
            assert differ.get_actual_memballoon("vm01") == {
                "free_page_reporting": True, "autodeflate": True,
                "stats_period": 5}

    @pytest.mark.parametrize(
        "memballoon",
        ["<memballoon model='virtio'/>",
         "<memballoon model='virtio' autodeflate='off'/>"])
    def test_autodeflate_missing_or_off_is_false(
            self, differ: VMStateDiffer, memballoon: str):
        with patch.object(differ.virsh_edit, "get_domain_xml",
                          return_value=_balloon_xml(memballoon)):
            assert differ.get_actual_memballoon("vm01")["autodeflate"] is False

    def test_normalize_none_is_defaults(self):
        assert VMStateDiffer.normalize_memballoon_config(None) == {
            "free_page_reporting": False, "autodeflate": False,
            "stats_period": None}

    def test_normalize_partial_block(self):
        assert VMStateDiffer.normalize_memballoon_config(
            {"autodeflate": True}) == {
            "free_page_reporting": False, "autodeflate": True,
            "stats_period": None}

    def test_normalize_rejects_non_bool_autodeflate(self):
        with pytest.raises(ConfigError, match="memballoon.autodeflate"):
            VMStateDiffer.normalize_memballoon_config({"autodeflate": "false"})


class TestMemballoonDiff:
    """diff_vm must flag balloon changes only when the actual inactive
    state differs from the desired one, in both directions."""

    def _diff(self, differ: VMStateDiffer, desired, actual, *, live=None,
              vm_state="running") -> dict:
        states = [actual]
        if vm_state in VMStateDiffer._LIVE_DOMAIN_STATES:
            states.append(actual if live is None else live)
        with patch.object(differ, "get_vm_state", return_value=vm_state), \
             patch.object(differ, "get_actual_cpu",
                          return_value={"sockets": 1, "cores": 1, "threads": 1,
                                        "total_vcpus": 1, "current_vcpus": 1}), \
             patch.object(differ, "get_max_vcpus", return_value=1), \
             patch.object(differ, "get_actual_memory_mb", return_value=1024), \
             patch.object(differ, "get_max_memory_mb", return_value=1024), \
             patch.object(differ, "get_actual_disks", return_value=[]), \
             patch.object(differ, "get_actual_cdroms", return_value=[]), \
             patch.object(differ, "get_actual_shared_folders", return_value=[]), \
             patch.object(differ, "get_actual_memballoon",
                          side_effect=states):
            return differ.diff_vm(
                domain_name="vm01",
                desired_cpus=None,
                desired_memory_mb=None,
                desired_disks=[],
                workdir="/tmp",
                disk_prefix="vm01",
                desired_memballoon=desired,
            )

    def test_no_block_and_default_actual_is_no_change(self, differ):
        diff = self._diff(differ, None,
                          {"free_page_reporting": False, "autodeflate": False,
                           "stats_period": None})
        assert diff["memballoon_changed"] is False
        assert diff["memballoon_restart_pending"] is False

    def test_persistent_match_but_live_mismatch_needs_restart(self, differ):
        desired = {"free_page_reporting": False, "autodeflate": True,
                   "stats_period": None}
        diff = self._diff(
            differ, desired, desired,
            live={"free_page_reporting": False, "autodeflate": False,
                  "stats_period": None})

        assert diff["memballoon_changed"] is False
        assert diff["memballoon_restart_pending"] is True
        assert diff["live_memballoon"]["autodeflate"] is False

    @pytest.mark.parametrize(
        "vm_state",
        ["paused", "blocked", "in shutdown", "pmsuspended"],
    )
    def test_other_active_states_with_live_mismatch_need_restart(
            self, differ, vm_state):
        desired = {"free_page_reporting": False, "autodeflate": True,
                   "stats_period": None}

        diff = self._diff(
            differ, desired, desired, vm_state=vm_state,
            live={"free_page_reporting": False, "autodeflate": False,
                  "stats_period": None})

        assert diff["memballoon_restart_pending"] is True
        assert diff["live_memballoon"]["autodeflate"] is False

    def test_new_block_is_a_change(self, differ):
        diff = self._diff(differ, {"free_page_reporting": True},
                          {"free_page_reporting": False, "autodeflate": False,
                           "stats_period": None})
        assert diff["memballoon_changed"] is True
        assert diff["memballoon_restart_pending"] is True
        assert diff["desired_memballoon"] == {
            "free_page_reporting": True, "autodeflate": False,
            "stats_period": None}

    def test_removed_block_reconciles_to_defaults(self, differ):
        """Removing the memballoon block after enabling it must show up as
        a change back to the libvirt defaults (review P2)."""
        diff = self._diff(differ, None,
                          {"free_page_reporting": False, "autodeflate": True,
                           "stats_period": None})
        assert diff["memballoon_changed"] is True
        assert diff["desired_memballoon"] == {
            "free_page_reporting": False, "autodeflate": False,
            "stats_period": None}

    def test_matching_state_is_no_change(self, differ):
        diff = self._diff(differ,
                          {"free_page_reporting": True, "autodeflate": True,
                           "stats_period": 5},
                          {"free_page_reporting": True, "autodeflate": True,
                           "stats_period": 5})
        assert diff["memballoon_changed"] is False

    def test_stats_period_mismatch_is_a_change(self, differ):
        diff = self._diff(differ,
                          {"free_page_reporting": True, "stats_period": 10},
                          {"free_page_reporting": True, "autodeflate": False,
                           "stats_period": 5})
        assert diff["memballoon_changed"] is True

    @pytest.mark.parametrize("desired,actual", [(True, False), (False, True)])
    def test_autodeflate_mismatch_is_a_change(self, differ, desired, actual):
        diff = self._diff(
            differ,
            {"autodeflate": desired},
            {"free_page_reporting": False, "autodeflate": actual,
             "stats_period": None})
        assert diff["memballoon_changed"] is True
        assert diff["desired_memballoon"]["autodeflate"] is desired
