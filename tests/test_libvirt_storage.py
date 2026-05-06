"""
Unit tests for boxman.providers.libvirt.storage.StorageManager.

Covers:
- ``vm_disk_paths`` filename convention
- qemu-img JSON parsing in ``disk_info`` / ``disk_chain`` / ``disk_measure``
- guest-side fstrim via ``virsh domfstrim``
- ``compact_disk`` method resolution and ``--drop-snapshots`` gate
- ``has_discard_unmap`` detection
- ``shutdown_and_wait`` happy path and timeout fallback
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.storage import StorageManager, vm_disk_paths


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
def sm() -> StorageManager:
    return StorageManager(provider_config={"use_sudo": False, "uri": "qemu:///system"})


class TestVmDiskPaths:

    def test_boot_only_when_no_extras(self, tmp_path: Path):
        paths = vm_disk_paths(str(tmp_path), "vm01")
        assert paths == [str(tmp_path / "vm01.qcow2")]

    def test_includes_extras_by_name(self, tmp_path: Path):
        info = {"disks": [{"name": "data"}, {"name": "logs"}]}
        paths = vm_disk_paths(str(tmp_path), "vm01", info)
        assert paths == [
            str(tmp_path / "vm01.qcow2"),
            str(tmp_path / "vm01_data.qcow2"),
            str(tmp_path / "vm01_logs.qcow2"),
        ]

    def test_skips_disks_without_name(self, tmp_path: Path):
        info = {"disks": [{"size": 10}, {"name": "data"}]}
        paths = vm_disk_paths(str(tmp_path), "vm01", info)
        assert paths == [
            str(tmp_path / "vm01.qcow2"),
            str(tmp_path / "vm01_data.qcow2"),
        ]

    def test_expands_user_home(self):
        paths = vm_disk_paths("~/workdir", "vm01")
        assert "~" not in paths[0]


class TestInit:

    def test_defaults_with_no_config(self):
        sm = StorageManager()
        assert sm.uri == "qemu:///system"
        assert sm.use_sudo is False

    def test_reads_from_config(self):
        sm = StorageManager({"uri": "qemu+ssh://x", "use_sudo": True})
        assert sm.uri == "qemu+ssh://x"
        assert sm.use_sudo is True


class TestDiskInfo:

    def test_parses_qemu_img_json(self, sm: StorageManager):
        payload = {"virtual-size": 21474836480, "actual-size": 1234567890,
                   "format": "qcow2"}
        with patch.object(sm.cmd, "execute_shell",
                          return_value=_result(stdout=json.dumps(payload))):
            assert sm.disk_info("/p/d.qcow2") == payload

    def test_returns_empty_on_failure(self, sm: StorageManager):
        with patch.object(sm.cmd, "execute_shell",
                          return_value=_result(ok=False, stderr="no such file")):
            assert sm.disk_info("/p/d.qcow2") == {}

    def test_returns_empty_on_bad_json(self, sm: StorageManager):
        with patch.object(sm.cmd, "execute_shell",
                          return_value=_result(stdout="not json")):
            assert sm.disk_info("/p/d.qcow2") == {}


class TestDiskChain:

    def test_parses_list(self, sm: StorageManager):
        payload = [{"filename": "head"}, {"filename": "base"}]
        with patch.object(sm.cmd, "execute_shell",
                          return_value=_result(stdout=json.dumps(payload))):
            assert sm.disk_chain("/p/d.qcow2") == payload

    def test_wraps_single_dict_in_list(self, sm: StorageManager):
        payload = {"filename": "head"}
        with patch.object(sm.cmd, "execute_shell",
                          return_value=_result(stdout=json.dumps(payload))):
            assert sm.disk_chain("/p/d.qcow2") == [payload]


class TestCountSnapshots:

    def test_counts_non_empty_lines(self, sm: StorageManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout="snap1\nsnap2\n\n")):
            assert sm.count_snapshots("vm01") == 2

    def test_zero_on_failure(self, sm: StorageManager):
        with patch.object(sm.virsh, "execute", return_value=_result(ok=False)):
            assert sm.count_snapshots("vm01") == 0


class TestHasDiscardUnmap:

    XML_WITH_DISCARD = """\
<domain>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' discard='unmap'/>
      <source file='/p/d.qcow2'/>
    </disk>
  </devices>
</domain>
"""

    XML_WITHOUT_DISCARD = """\
<domain>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/p/d.qcow2'/>
    </disk>
  </devices>
</domain>
"""

    XML_DISCARD_ON_CDROM_ONLY = """\
<domain>
  <devices>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw' discard='unmap'/>
      <source file='/p/seed.iso'/>
    </disk>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/p/d.qcow2'/>
    </disk>
  </devices>
</domain>
"""

    def test_true_when_disk_has_discard(self, sm: StorageManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout=self.XML_WITH_DISCARD)):
            assert sm.has_discard_unmap("vm01") is True

    def test_false_when_disk_missing_discard(self, sm: StorageManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout=self.XML_WITHOUT_DISCARD)):
            assert sm.has_discard_unmap("vm01") is False

    def test_only_data_disks_count(self, sm: StorageManager):
        # cdrom has discard but no data disk does — must still report False
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout=self.XML_DISCARD_ON_CDROM_ONLY)):
            assert sm.has_discard_unmap("vm01") is False


class TestIsRunning:

    def test_true_when_running(self, sm: StorageManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout="running")):
            assert sm.is_running("vm01") is True

    def test_false_when_shut_off(self, sm: StorageManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout="shut off")):
            assert sm.is_running("vm01") is False


class TestIsLibguestfsAvailable:

    def test_true_when_command_succeeds(self, sm: StorageManager):
        with patch.object(sm.cmd, "execute_shell", return_value=_result()):
            assert sm.is_libguestfs_available() is True

    def test_false_when_command_fails(self, sm: StorageManager):
        with patch.object(sm.cmd, "execute_shell", return_value=_result(ok=False)):
            assert sm.is_libguestfs_available() is False


class TestFstrimGuest:

    def test_success(self, sm: StorageManager):
        with patch.object(sm.virsh, "execute", return_value=_result()) as exe:
            assert sm.fstrim_guest("vm01") is True
        assert exe.call_args.args[0] == "domfstrim"
        assert exe.call_args.args[1] == "vm01"

    def test_agent_not_responsive_gives_helpful_error(self, sm: StorageManager):
        with patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False,
                                               stderr="Guest agent is not responding")), \
             patch.object(sm.logger, "error") as err:
            assert sm.fstrim_guest("vm01") is False
        msgs = [c.args[0] for c in err.call_args_list]
        assert any("qemu-guest-agent" in m for m in msgs)


class TestSnapshotMemoryFiles:

    def test_lists_matching_files(self, sm: StorageManager, tmp_path: Path):
        m1 = tmp_path / "vm01_snapshot_a.raw"
        m2 = tmp_path / "vm01_snapshot_b.raw"
        m1.write_bytes(b"x")
        m2.write_bytes(b"x")
        # unrelated file should not appear
        (tmp_path / "vm02_snapshot_a.raw").write_bytes(b"x")
        files = sm.snapshot_memory_files(str(tmp_path), "vm01")
        assert sorted(files) == sorted([str(m1), str(m2)])

    def test_empty_when_nothing_present(self, sm: StorageManager, tmp_path: Path):
        assert sm.snapshot_memory_files(str(tmp_path), "vm01") == []


class TestCompactDisk:

    def test_auto_picks_sparsify_when_snapshots_present(self, sm: StorageManager):
        with patch.object(sm, "is_libguestfs_available", return_value=True), \
             patch.object(sm, "sparsify_in_place", return_value=True) as sparsify, \
             patch.object(sm, "convert") as convert:
            ok = sm.compact_disk("/p/d.qcow2", method="auto", has_snapshots=True)
        assert ok is True
        sparsify.assert_called_once_with("/p/d.qcow2")
        convert.assert_not_called()

    def test_auto_picks_convert_when_no_snapshots(self, sm: StorageManager):
        with patch.object(sm, "convert", return_value=True) as convert:
            ok = sm.compact_disk("/p/d.qcow2", method="auto", has_snapshots=False)
        assert ok is True
        convert.assert_called_once_with("/p/d.qcow2", compress=False)

    def test_convert_refused_with_snapshots_without_drop(self, sm: StorageManager):
        with patch.object(sm, "convert") as convert, \
             patch.object(sm.logger, "error") as err:
            ok = sm.compact_disk("/p/d.qcow2", method="convert",
                                 has_snapshots=True, drop_snapshots=False)
        assert ok is False
        convert.assert_not_called()
        msgs = [c.args[0] for c in err.call_args_list]
        assert any("--drop-snapshots" in m for m in msgs)

    def test_convert_allowed_with_drop_snapshots(self, sm: StorageManager):
        with patch.object(sm, "convert", return_value=True) as convert:
            ok = sm.compact_disk("/p/d.qcow2", method="convert",
                                 has_snapshots=True, drop_snapshots=True)
        assert ok is True
        convert.assert_called_once_with("/p/d.qcow2", compress=False)

    def test_convert_compressed_passes_compress_flag(self, sm: StorageManager):
        with patch.object(sm, "convert", return_value=True) as convert:
            sm.compact_disk("/p/d.qcow2", method="convert-compressed",
                            has_snapshots=False)
        convert.assert_called_once_with("/p/d.qcow2", compress=True)

    def test_sparsify_refused_when_libguestfs_missing(self, sm: StorageManager):
        with patch.object(sm, "is_libguestfs_available", return_value=False), \
             patch.object(sm, "sparsify_in_place") as sparsify, \
             patch.object(sm.logger, "error") as err:
            ok = sm.compact_disk("/p/d.qcow2", method="sparsify")
        assert ok is False
        sparsify.assert_not_called()
        msgs = [c.args[0] for c in err.call_args_list]
        assert any("guestfs-tools" in m or "libguestfs" in m for m in msgs)

    def test_unknown_method_returns_false(self, sm: StorageManager):
        assert sm.compact_disk("/p/d.qcow2", method="zap") is False


class TestConvert:

    def test_uses_compress_flag(self, sm: StorageManager):
        with patch.object(sm.cmd, "execute_shell", return_value=_result()) as shell:
            assert sm.convert("/p/d.qcow2", compress=True) is True
        cmd = shell.call_args.args[0]
        assert "qemu-img convert -c -O qcow2" in cmd
        assert "/p/d.qcow2.compact-tmp" in cmd
        assert "mv /p/d.qcow2.compact-tmp /p/d.qcow2" in cmd

    def test_no_compress_by_default(self, sm: StorageManager):
        with patch.object(sm.cmd, "execute_shell", return_value=_result()) as shell:
            sm.convert("/p/d.qcow2", compress=False)
        cmd = shell.call_args.args[0]
        assert " -c " not in cmd
        assert "qemu-img convert -O qcow2" in cmd

    def test_cleans_up_tmp_on_failure(self, sm: StorageManager):
        with patch.object(sm.cmd, "execute_shell",
                          side_effect=[_result(ok=False, stderr="boom"), _result()]) as shell:
            assert sm.convert("/p/d.qcow2") is False
        # Second call must be the rm cleanup
        assert "rm -f" in shell.call_args_list[1].args[0]


class TestShutdownAndWait:

    def test_noop_when_not_running(self, sm: StorageManager):
        with patch.object(sm, "is_running", return_value=False), \
             patch.object(sm.virsh, "execute") as exe:
            assert sm.shutdown_and_wait("vm01") is True
        exe.assert_not_called()

    def test_success_when_vm_stops_in_time(self, sm: StorageManager):
        # First is_running True (initial check), then False after shutdown
        running_states = iter([True, False])
        with patch.object(sm, "is_running",
                          side_effect=lambda _vm: next(running_states)), \
             patch.object(sm.virsh, "execute", return_value=_result()), \
             patch("boxman.providers.libvirt.storage.time.sleep"):
            assert sm.shutdown_and_wait("vm01", timeout_s=10) is True

    def test_destroys_after_timeout(self, sm: StorageManager):
        # is_running stays True forever — must fall through to destroy
        time_values = [0]  # always before deadline=5 on entry, after on second check

        def fake_time():
            time_values[0] += 100
            return time_values[0]

        with patch.object(sm, "is_running", return_value=True), \
             patch.object(sm.virsh, "execute", return_value=_result()) as exe, \
             patch("boxman.providers.libvirt.storage.time.time",
                   side_effect=fake_time), \
             patch("boxman.providers.libvirt.storage.time.sleep"):
            sm.shutdown_and_wait("vm01", timeout_s=5)
        verbs = [c.args[0] for c in exe.call_args_list]
        assert "shutdown" in verbs
        assert "destroy" in verbs
