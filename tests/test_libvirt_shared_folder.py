"""
Unit tests for boxman.providers.libvirt.shared_folder.SharedFolderManager.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt.shared_folder import SharedFolderManager


pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True, stderr: str = "", return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


MINIMAL_DOMAIN_XML = """\
<domain type='kvm'>
  <name>vm01</name>
  <memory unit='KiB'>2097152</memory>
  <devices>
  </devices>
</domain>
"""

DOMAIN_WITH_MEMFD = """\
<domain type='kvm'>
  <name>vm01</name>
  <memory unit='KiB'>2097152</memory>
  <memoryBacking>
    <source type='memfd'/>
  </memoryBacking>
  <devices>
  </devices>
</domain>
"""


@pytest.fixture
def sf() -> SharedFolderManager:
    return SharedFolderManager("vm01", provider_config={"use_sudo": False})


class TestGenerateFilesystemXml:

    def test_basic_rw_share(self, sf: SharedFolderManager):
        xml = sf._generate_filesystem_xml("mytag", "/host/share", readonly=False)
        assert "type='mount'" in xml
        assert "accessmode='passthrough'" in xml
        assert "<driver type='virtiofs'/>" in xml
        assert "dir='/host/share'" in xml
        assert "dir='mytag'" in xml
        assert "<readonly/>" not in xml

    def test_readonly_share_includes_readonly_tag(self, sf: SharedFolderManager):
        xml = sf._generate_filesystem_xml("tag", "/h", readonly=True)
        assert "<readonly/>" in xml


class TestAttachSharedFolder:

    def test_missing_host_path_returns_failure(self, sf: SharedFolderManager):
        out = sf.attach_shared_folder("tag", "/does-not-exist")
        assert out == {"success": False, "restart_needed": False}

    def test_live_attach_success(self, sf: SharedFolderManager, tmp_path: Path):
        with patch.object(sf, "execute", return_value=_result()) as execute:
            out = sf.attach_shared_folder("tag", str(tmp_path))
        assert out == {"success": True, "restart_needed": False}
        execute.assert_called_once()

    def test_falls_back_to_config_only_on_live_failure(
        self, sf: SharedFolderManager, tmp_path: Path
    ):
        calls = []

        def fake(*args, **_kwargs):
            calls.append(args)
            # first call (live) fails, second (config-only) succeeds
            return _result(ok=False, stderr="hotplug unsupported") if len(calls) == 1 else _result()

        with patch.object(sf, "execute", side_effect=fake):
            out = sf.attach_shared_folder("tag", str(tmp_path))
        assert out == {"success": True, "restart_needed": True}
        assert any("--config" in c for c in calls)

    def test_both_attempts_fail_returns_failure(
        self, sf: SharedFolderManager, tmp_path: Path
    ):
        with patch.object(sf, "execute", return_value=_result(ok=False, stderr="nope")):
            out = sf.attach_shared_folder("tag", str(tmp_path))
        assert out == {"success": False, "restart_needed": False}


class TestDetachSharedFolder:

    def test_live_detach_success(self, sf: SharedFolderManager):
        with patch.object(sf, "execute", return_value=_result()):
            out = sf.detach_shared_folder("tag", "/host")
        assert out == {"success": True, "restart_needed": False}

    def test_falls_back_to_config_only(self, sf: SharedFolderManager):
        calls = []

        def fake(*args, **_kwargs):
            calls.append(args)
            return _result(ok=False, stderr="x") if len(calls) == 1 else _result()

        with patch.object(sf, "execute", side_effect=fake):
            out = sf.detach_shared_folder("tag", "/host")
        assert out == {"success": True, "restart_needed": True}


class TestEnsureMemfdBacking:

    def test_skips_when_memfd_already_present(self, sf: SharedFolderManager):
        mock_editor = MagicMock()
        mock_editor.get_domain_xml.return_value = DOMAIN_WITH_MEMFD
        with patch("boxman.providers.libvirt.shared_folder.VirshEdit",
                   return_value=mock_editor):
            out = sf.ensure_memfd_backing()
        assert out == {"success": True, "restart_needed": False}
        mock_editor.redefine_domain.assert_not_called()

    def test_adds_memfd_when_missing_and_vm_stopped(self, sf: SharedFolderManager):
        mock_editor = MagicMock()
        mock_editor.get_domain_xml.return_value = MINIMAL_DOMAIN_XML
        mock_editor.redefine_domain.return_value = True
        with patch("boxman.providers.libvirt.shared_folder.VirshEdit",
                   return_value=mock_editor), \
             patch.object(sf, "execute", return_value=_result(stdout="shut off\n")):
            out = sf.ensure_memfd_backing()
        assert out == {"success": True, "restart_needed": False}
        mock_editor.redefine_domain.assert_called_once()
        # confirm injected XML contains memfd backing
        _name, injected = mock_editor.redefine_domain.call_args.args
        assert "memoryBacking" in injected
        assert "memfd" in injected

    def test_adds_memfd_when_missing_and_vm_running_signals_restart(
        self, sf: SharedFolderManager
    ):
        mock_editor = MagicMock()
        mock_editor.get_domain_xml.return_value = MINIMAL_DOMAIN_XML
        mock_editor.redefine_domain.return_value = True
        with patch("boxman.providers.libvirt.shared_folder.VirshEdit",
                   return_value=mock_editor), \
             patch.object(sf, "execute", return_value=_result(stdout="running\n")):
            out = sf.ensure_memfd_backing()
        assert out == {"success": True, "restart_needed": True}

    def test_redefine_failure_returns_failure(self, sf: SharedFolderManager):
        mock_editor = MagicMock()
        mock_editor.get_domain_xml.return_value = MINIMAL_DOMAIN_XML
        mock_editor.redefine_domain.return_value = False
        with patch("boxman.providers.libvirt.shared_folder.VirshEdit",
                   return_value=mock_editor):
            out = sf.ensure_memfd_backing()
        assert out == {"success": False, "restart_needed": False}

    def test_exception_in_xml_handling_returns_failure(self, sf: SharedFolderManager):
        mock_editor = MagicMock()
        mock_editor.get_domain_xml.side_effect = RuntimeError("virsh crashed")
        with patch("boxman.providers.libvirt.shared_folder.VirshEdit",
                   return_value=mock_editor):
            out = sf.ensure_memfd_backing()
        assert out == {"success": False, "restart_needed": False}


class TestConfigureFromConfig:

    def test_missing_name_returns_failure(self, sf: SharedFolderManager):
        out = sf.configure_from_config({"host_path": "/x"})
        assert out == {"success": False, "restart_needed": False}

    def test_missing_host_path_returns_failure(self, sf: SharedFolderManager):
        out = sf.configure_from_config({"name": "tag"})
        assert out == {"success": False, "restart_needed": False}

    def test_memfd_failure_short_circuits(self, sf: SharedFolderManager):
        with patch.object(sf, "ensure_memfd_backing",
                          return_value={"success": False, "restart_needed": False}), \
             patch.object(sf, "attach_shared_folder") as attach:
            out = sf.configure_from_config({"name": "t", "host_path": "/h"})
        assert out == {"success": False, "restart_needed": False}
        attach.assert_not_called()

    def test_restart_needed_propagates(self, sf: SharedFolderManager, tmp_path: Path):
        """If memfd or attach signals restart, combined result does too."""
        with patch.object(sf, "ensure_memfd_backing",
                          return_value={"success": True, "restart_needed": True}), \
             patch.object(sf, "attach_shared_folder",
                          return_value={"success": True, "restart_needed": False}):
            out = sf.configure_from_config({"name": "t", "host_path": str(tmp_path)})
        assert out == {"success": True, "restart_needed": True}


class TestGetAttachedSharedFolders:

    def test_parses_filesystems_from_xml(self, sf: SharedFolderManager):
        xml = """\
<domain>
  <devices>
    <filesystem type='mount' accessmode='passthrough'>
      <driver type='virtiofs'/>
      <source dir='/srv/one'/>
      <target dir='one'/>
    </filesystem>
    <filesystem type='mount' accessmode='passthrough'>
      <driver type='virtiofs'/>
      <source dir='/srv/two'/>
      <target dir='two'/>
      <readonly/>
    </filesystem>
  </devices>
</domain>
"""
        mock_editor = MagicMock()
        mock_editor.get_domain_xml.return_value = xml
        with patch("boxman.providers.libvirt.shared_folder.VirshEdit",
                   return_value=mock_editor):
            folders = sf.get_attached_shared_folders()
        assert folders == [
            {"name": "one", "host_path": "/srv/one", "readonly": False},
            {"name": "two", "host_path": "/srv/two", "readonly": True},
        ]

    def test_empty_when_no_filesystems(self, sf: SharedFolderManager):
        mock_editor = MagicMock()
        mock_editor.get_domain_xml.return_value = "<domain><devices/></domain>"
        with patch("boxman.providers.libvirt.shared_folder.VirshEdit",
                   return_value=mock_editor):
            assert sf.get_attached_shared_folders() == []
