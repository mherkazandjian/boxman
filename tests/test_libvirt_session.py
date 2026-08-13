"""
Unit tests for boxman.providers.libvirt.session.LibVirtSession.

Focus on the high-value surface:
  - Effective provider config (plain attribute; precedence is resolved
    upstream by boxman.providers.merge_provider_configs)
  - uri / use_sudo property delegation + setters
  - update_provider_config_with_runtime
  - destroy_disks filesystem cleanup including snapshot leftovers
  - Simple delegators (destroy_vm, start_vm)

The huge orchestration methods (configure_vm_*, update_vm_*, verify_*,
save/restore, snapshot wiring) are covered by integration tests
(test_provision_boxes.py + Phase 1.5 E2E) not by unit tests.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import ConfigError
from boxman.providers.libvirt.net import Network
from boxman.providers.libvirt.session import LibVirtSession

pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True, stderr: str = "", return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


def _session(provider: dict | None = None) -> LibVirtSession:
    cfg = {"provider": {"libvirt": provider or {}}}
    return LibVirtSession(config=cfg)


class TestConfigPrecedence:
    """The session holds an already-merged provider config (precedence is
    resolved upstream by boxman.providers.merge_provider_configs); updates
    here are plain last-write-wins."""

    def test_defaults_on_empty_provider(self):
        s = _session({})
        assert s.provider_config == {}

    def test_reads_project_provider(self):
        s = _session({"uri": "qemu:///system", "use_sudo": True})
        assert s.provider_config["uri"] == "qemu:///system"
        assert s.provider_config["use_sudo"] is True

    def test_update_is_last_write_wins(self):
        s = _session({"use_sudo": True})
        s.update_provider_config({"use_sudo": False})
        assert s.provider_config["use_sudo"] is False

    def test_update_fills_in_missing_keys(self):
        s = _session({"use_sudo": True})
        s.update_provider_config({"uri": "qemu:///custom"})
        assert s.provider_config["uri"] == "qemu:///custom"
        assert s.provider_config["use_sudo"] is True


class TestUriAndUseSudoProperties:

    def test_uri_default(self):
        assert _session({}).uri == "qemu:///system"

    def test_uri_getter_and_setter(self):
        s = _session({})
        s.uri = "qemu+ssh://host"
        assert s.uri == "qemu+ssh://host"
        assert s.provider_config["uri"] == "qemu+ssh://host"

    def test_use_sudo_default_false(self):
        assert _session({}).use_sudo is False

    def test_use_sudo_setter(self):
        s = _session({})
        s.use_sudo = True
        assert s.use_sudo is True

    def test_use_sudo_setter_overrides_project_value(self):
        """The setter writes straight into the effective config."""
        s = _session({"use_sudo": False})
        s.use_sudo = True
        assert s.use_sudo is True


class TestUpdateProviderConfigWithRuntime:

    def test_noop_when_manager_is_none(self):
        s = _session({"uri": "qemu:///system"})
        s.update_provider_config_with_runtime()
        assert s.provider_config["uri"] == "qemu:///system"

    def test_delegates_to_manager_and_applies_runtime_keys(self):
        s = _session({"use_sudo": True})
        manager = MagicMock()
        # get_provider_config_with_runtime derives from the session's own
        # config and adds runtime metadata on top
        manager.get_provider_config_with_runtime.return_value = {
            "use_sudo": True, "runtime": "docker-compose",
        }
        s.manager = manager

        s.update_provider_config_with_runtime()
        # Runtime was applied
        assert s.provider_config["runtime"] == "docker-compose"
        assert s.provider_config["use_sudo"] is True


class TestBridgeTransitionPlanning:

    NAT_XML = (
        "<network><name>demo</name><forward mode='nat'/>"
        "<bridge name='virbr0' stp='on' delay='0'/>"
        "<mac address='52:54:00:0a:0b:0c'/>"
        "<ip address='10.5.3.1' netmask='255.255.255.0'/></network>"
    )
    BRIDGE_XML = (
        "<network><name>demo</name><forward mode='bridge'/>"
        "<bridge name='virbr0'/></network>"
    )

    @staticmethod
    def _session() -> LibVirtSession:
        session = _session({
            "uri": "qemu+ssh://hypervisor.example/system",
            "use_sudo": False,
        })
        session.manager = MagicMock()
        return session

    @staticmethod
    def _network_state(xml: str):
        return (
            patch.object(Network, "exists", return_value=True),
            patch.object(Network, "dump_xml", return_value=xml),
            patch.object(Network, "attached_domains", return_value=[]),
            patch.object(Network, "is_active", return_value=True),
        )

    def test_nat_to_bridge_same_name_is_rejected_during_planning(self):
        session = self._session()
        patches = self._network_state(self.NAT_XML)
        with patches[0], patches[1], patches[2], patches[3]:
            with pytest.raises(ConfigError, match="would delete.*managed bridge"):
                session.plan_network(
                    name="demo",
                    info={"mode": "bridge", "bridge": {"name": "virbr0"}},
                )

    def test_bridge_to_nat_same_pinned_name_is_rejected_during_planning(self):
        session = self._session()
        patches = self._network_state(self.BRIDGE_XML)
        with (
            patches[0], patches[1], patches[2], patches[3],
            patch.object(Network, "get_bridge_from_network",
                         return_value="virbr0"),
        ):
            with pytest.raises(ConfigError, match="cannot claim its name"):
                session.plan_network(
                    name="demo",
                    info={"mode": "nat", "bridge": {"name": "virbr0"}},
                )

    def test_bridge_to_auto_nat_reserves_name_before_removal(self):
        session = self._session()
        patches = self._network_state(self.BRIDGE_XML)
        with (
            patches[0], patches[1], patches[2], patches[3],
            patch.object(Network, "get_bridge_from_network",
                         return_value="virbr0"),
            patch.object(Network, "find_available_bridge_name",
                         return_value="virbr1"),
        ):
            plan = session.plan_network(name="demo", info={"mode": "nat"})

        assert plan["action"] == "recreate"
        assert plan["replacement_bridge_name"] == "virbr1"


class TestDestroyDisks:

    def test_removes_boot_disk_and_named_extras_and_snapshot_leftovers(
        self, tmp_path: Path
    ):
        # set up fake workdir
        (tmp_path / "vm01.qcow2").write_bytes(b"x")
        (tmp_path / "vm01_data.qcow2").write_bytes(b"x")
        (tmp_path / "vm01.2026-04-21T08:00:00").write_bytes(b"x")
        (tmp_path / "vm01_snapshot_s1.raw").write_bytes(b"x")
        (tmp_path / "other-vm.qcow2").write_bytes(b"x")   # untouched

        s = _session({"use_sudo": False})
        assert s.destroy_disks(
            str(tmp_path), "vm01", [{"name": "data"}],
        ) is True

        # vm01-prefixed files are gone
        assert not (tmp_path / "vm01.qcow2").exists()
        assert not (tmp_path / "vm01_data.qcow2").exists()
        assert not (tmp_path / "vm01.2026-04-21T08:00:00").exists()
        assert not (tmp_path / "vm01_snapshot_s1.raw").exists()
        # other-vm left alone
        assert (tmp_path / "other-vm.qcow2").exists()

    def test_missing_files_are_silently_ignored(self, tmp_path: Path):
        s = _session({})
        # nothing in tmp_path — should not raise
        assert s.destroy_disks(str(tmp_path), "no-vm", []) is True


class TestDestroyVMDelegation:

    def test_force_false_uses_remove(self):
        s = _session({})
        mock_destroyer = MagicMock()
        mock_destroyer.remove.return_value = True
        with patch("boxman.providers.libvirt.session.DestroyVM",
                   return_value=mock_destroyer):
            assert s.destroy_vm("vm01", force=False) is True
        mock_destroyer.remove.assert_called_once()
        mock_destroyer.force_undefine_vm.assert_not_called()

    def test_force_true_uses_force_undefine(self):
        s = _session({})
        mock_destroyer = MagicMock()
        mock_destroyer.force_undefine_vm.return_value = True
        with patch("boxman.providers.libvirt.session.DestroyVM",
                   return_value=mock_destroyer):
            assert s.destroy_vm("vm01", force=True) is True
        mock_destroyer.force_undefine_vm.assert_called_once()
        mock_destroyer.remove.assert_not_called()


class TestStartVM:

    def test_noop_when_already_running(self):
        s = _session({})
        mock_virsh = MagicMock()
        mock_virsh.execute.return_value = _result(stdout="running\n")
        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=mock_virsh):
            assert s.start_vm("vm01") is True
        # only the state probe was called, not the start
        first_call = mock_virsh.execute.call_args_list[0]
        assert first_call.args[0] == "domstate"

    def test_starts_when_shut_off_and_verifies(self):
        s = _session({})
        mock_virsh = MagicMock()
        mock_virsh.execute.side_effect = [
            _result(stdout="shut off\n"),   # first domstate
            _result(ok=True),               # start
            _result(stdout="running\n"),    # verify
        ]
        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=mock_virsh):
            assert s.start_vm("vm01") is True

    def test_start_failure_returns_false(self):
        s = _session({})
        mock_virsh = MagicMock()
        mock_virsh.execute.side_effect = [
            _result(stdout="shut off\n"),
            _result(ok=False, stderr="nope"),
        ]
        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=mock_virsh):
            assert s.start_vm("vm01") is False

    def test_still_not_running_after_start_returns_false(self):
        s = _session({})
        mock_virsh = MagicMock()
        mock_virsh.execute.side_effect = [
            _result(stdout="shut off\n"),
            _result(ok=True),
            _result(stdout="shut off\n"),  # verify still shut off
        ]
        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=mock_virsh):
            assert s.start_vm("vm01") is False

    def test_exception_returns_false(self):
        s = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand",
                   side_effect=RuntimeError("x")):
            assert s.start_vm("vm01") is False


class TestCloneVMDelegation:

    def test_calls_cloneVM_with_expected_args(self, tmp_path: Path):
        s = _session({"use_sudo": False})
        mock_cloner = MagicMock()
        mock_cloner.clone.return_value = True
        with patch("boxman.providers.libvirt.session.CloneVM",
                   return_value=mock_cloner) as clone_cls:
            s.clone_vm("new-vm", "src-vm", {"info": "x"}, str(tmp_path))

        _args, kwargs = clone_cls.call_args
        assert kwargs["new_vm_name"] == "new-vm"
        assert kwargs["src_vm_name"] == "src-vm"
        assert kwargs["workdir"] == str(tmp_path)
        mock_cloner.clone.assert_called_once()

    def test_raises_when_clone_fails(self, tmp_path: Path):
        s = _session({})
        mock_cloner = MagicMock()
        mock_cloner.clone.return_value = False
        with patch("boxman.providers.libvirt.session.CloneVM",
                   return_value=mock_cloner):
            with pytest.raises(RuntimeError, match="Failed to clone"):
                s.clone_vm("new-vm", "src", {}, str(tmp_path))


class TestVmExists:

    def test_true_when_name_in_list(self):
        s = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(
                stdout="other-vm\nrocky-template\n")
            assert s.vm_exists("rocky-template") is True

    def test_false_when_name_absent(self):
        s = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(stdout="other\n")
            assert s.vm_exists("missing") is False

    def test_false_on_virsh_failure(self):
        s = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(ok=False)
            assert s.vm_exists("x") is False


class TestTemplateDisksPresent:
    """Guards the broken-template detection that prevents virt-clone from
    failing inscrutably when the libvirt domain still exists but its
    backing qcow2 has been deleted."""

    XML_HEALTHY_TEMPLATE = """\
<domain>
  <devices>
    <disk type='file' device='disk'>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
  </devices>
</domain>"""

    XML_BROKEN_TEMPLATE = """\
<domain>
  <devices>
    <disk type='file' device='disk'>
      <source file='/this/path/will/not/exist.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
  </devices>
</domain>"""

    XML_CDROM_ONLY = """\
<domain>
  <devices>
    <disk type='file' device='cdrom'>
      <source file='/path/never-mind-i-am-missing.iso'/>
      <target dev='hdc' bus='ide'/>
    </disk>
  </devices>
</domain>"""

    def test_true_when_data_disk_present(self, tmp_path: Path):
        disk = tmp_path / "template.qcow2"
        disk.write_bytes(b"qcow2-stub")
        xml = self.XML_HEALTHY_TEMPLATE.format(disk_path=str(disk))
        s = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(stdout=xml)
            assert s.template_disks_present("rocky-template") is True

    def test_false_when_data_disk_missing(self):
        s = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(
                stdout=self.XML_BROKEN_TEMPLATE)
            assert s.template_disks_present("rocky-template") is False

    def test_ignores_missing_cdrom_iso(self):
        """A missing seed.iso is normal post-template-build; do not
        report the template as broken just because of an absent cdrom."""
        s = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(
                stdout=self.XML_CDROM_ONLY)
            assert s.template_disks_present("rocky-template") is True

    def test_true_when_dumpxml_fails(self):
        """Unknown domain → no claim about disks → True (do not block)."""
        s = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(
                ok=False, stderr="domain not found")
            assert s.template_disks_present("ghost") is True

    def test_true_when_xml_unparseable(self):
        s = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.return_value = _result(
                stdout="not xml at all")
            assert s.template_disks_present("x") is True
