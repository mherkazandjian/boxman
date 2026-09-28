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

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import ConfigError
from boxman.providers.libvirt.net import Network
from boxman.providers.libvirt.session import _ABSENT, LibVirtSession
from conftest import domain_listing, domain_uuid
from fake_libvirt_host import FakeHost
from fake_libvirt_host import Result as FakeResult

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


class TestForceStopVM:
    """#164 X3 — the stop that recovers a crashed guest.

    Distinct from destroy_vm, which undefines the domain. libvirt refuses
    `start` while a domain is still active, and a crashed one is active,
    so this has to run first — and it must leave the definition alone.
    """

    def test_already_shut_off_is_a_noop(self):
        s = _session({})
        mock_destroyer = MagicMock()
        mock_destroyer.is_vm_shut_off.return_value = True
        with patch("boxman.providers.libvirt.session.DestroyVM",
                   return_value=mock_destroyer):
            assert s.force_stop_vm("vm01") is True
        mock_destroyer.virsh.execute.assert_not_called()

    def test_active_domain_is_destroyed_then_confirmed(self):
        s = _session({})
        mock_destroyer = MagicMock()
        # active first, shut off after the virsh destroy
        mock_destroyer.is_vm_shut_off.side_effect = [False, True]
        mock_destroyer.virsh.execute.return_value = _result()
        with patch("boxman.providers.libvirt.session.DestroyVM",
                   return_value=mock_destroyer):
            assert s.force_stop_vm("vm01") is True
        mock_destroyer.virsh.execute.assert_called_once_with(
            "destroy", "vm01", warn=True)
        # the definition must survive
        mock_destroyer.undefine_vm.assert_not_called()
        mock_destroyer.force_undefine_vm.assert_not_called()

    def test_failed_virsh_destroy_is_reported(self):
        s = _session({})
        mock_destroyer = MagicMock()
        mock_destroyer.is_vm_shut_off.return_value = False
        mock_destroyer.virsh.execute.return_value = _result(
            ok=False, stderr="error: Domain not found")
        with patch("boxman.providers.libvirt.session.DestroyVM",
                   return_value=mock_destroyer):
            assert s.force_stop_vm("vm01") is False

    def test_domain_still_active_after_destroy_is_reported(self):
        s = _session({})
        mock_destroyer = MagicMock()
        mock_destroyer.is_vm_shut_off.side_effect = [False, False]
        mock_destroyer.virsh.execute.return_value = _result()
        with patch("boxman.providers.libvirt.session.DestroyVM",
                   return_value=mock_destroyer):
            assert s.force_stop_vm("vm01") is False

    def test_unreadable_state_reads_as_stopped(self):
        """is_vm_shut_off() answers True when the `domstate` query itself
        fails, so an unreachable libvirt reads as "stopped" here.

        That is deliberate and safe *only* because this is never the
        authority: `up` checks the start_vm() that follows, and libvirt
        refuses to start a domain that is actually still active. Pinned so
        the fail-open is a decision rather than an accident — if this helper
        ever gains a caller that does not check the next step, it needs a
        fail-closed variant instead.
        """
        s = _session({})
        mock_destroyer = MagicMock()
        mock_destroyer.is_vm_shut_off.return_value = True   # query failed
        with patch("boxman.providers.libvirt.session.DestroyVM",
                   return_value=mock_destroyer):
            assert s.force_stop_vm("vm01") is True


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


class TestDiskPathsInUse:
    """What every defined domain uses, backing chains included -- the check
    that keeps a removed VM's cleanup off another domain's disk (#207
    review, finding 1). It fails closed: any unanswered query is None."""

    BLK_A = (" Type   Device   Target   Source\n"
             "------------------------------------\n"
             " file   disk     vda      /ws/a.qcow2\n"
             " file   cdrom    sda      -\n")
    BLK_B = (" Type   Device   Target   Source\n"
             "------------------------------------\n"
             " file   disk     vda      /ws/b.top\n"
             " file   cdrom    sdb      /iso/install.iso\n")
    CHAIN = {
        "/ws/a.qcow2": '{"filename": "/ws/a.qcow2"}',
        "/ws/b.top": ('[{"filename": "/ws/b.top", '
                      '"full-backing-filename": "/tpl/base.qcow2"}, '
                      '{"filename": "/tpl/base.qcow2"}]'),
        "/iso/install.iso": '{"filename": "/iso/install.iso"}',
    }

    HEADER = (" Type   Device   Target   Source\n"
              "------------------------------------\n")

    def _run(self, inventories=None, failures=(), chains=None,
             list_ok=True):
        """*inventories* maps ``(domain, inactive)`` to domblklist output;
        *failures* lists the ``(domain, inactive)`` queries that fail."""
        inventories = inventories or {}
        chains = self.CHAIN if chains is None else chains

        def virsh_execute(*args, **kwargs):
            if args[0] == "list":
                # every domain; none of them running
                return _result(stdout=domain_listing(args, "vm-a", "vm-b")
                               if "--all" in args else "", ok=list_ok)
            key = (args[1], "--inactive" in args)
            if key in failures:
                return _result(ok=False, stderr="error: failed")
            default = {"vm-a": self.BLK_A, "vm-b": self.BLK_B}[args[1]]
            return _result(stdout=inventories.get(key, default))

        def shell(command, **kwargs):
            source = command.rsplit(" ", 1)[1].strip("'")
            if source not in chains:
                return _result(ok=False, stderr="Could not open")
            return _result(stdout=chains[source])

        s = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh, \
             patch("boxman.providers.libvirt.session.LibVirtCommandBase") as cmd:
            virsh.return_value.execute.side_effect = virsh_execute
            cmd.return_value.execute_shell.side_effect = shell
            return s.disk_paths_in_use(), cmd

    def test_maps_sources_and_backing_files_to_their_domain(self):
        in_use, cmd = self._run()
        assert in_use == {
            "/ws/a.qcow2": "vm-a",
            "/ws/b.top": "vm-b",
            "/tpl/base.qcow2": "vm-b",
            "/iso/install.iso": "vm-b",
        }
        # a running guest holds its image locked: -U reads it anyway
        assert all(" -U " in c.args[0]
                   for c in cmd.return_value.execute_shell.call_args_list)

    def test_none_when_the_domain_list_fails(self):
        in_use, _ = self._run(list_ok=False)
        assert in_use is None

    def test_none_when_a_domain_cannot_be_inspected(self):
        in_use, _ = self._run(failures=[("vm-b", False)])
        assert in_use is None

    def test_none_when_a_chain_cannot_be_read(self):
        chains = dict(self.CHAIN)
        del chains["/ws/b.top"]
        in_use, _ = self._run(chains=chains)
        assert in_use is None

    def test_none_when_qemu_img_output_is_not_json(self):
        chains = dict(self.CHAIN, **{"/ws/a.qcow2": "garbage"})
        in_use, _ = self._run(chains=chains)
        assert in_use is None

    # -- #212 review round 2, R2-2: a running domain's persistent disks ----

    def test_a_running_domains_persistent_disks_count_too(self):
        """Live XML says b.top, the persistent XML (next start) says an
        overlay backed by another file -- both chains are in use. Observed
        on libvirt 10.0: plain domblklist of a running domain reports only
        the live source."""
        persistent = self.HEADER + " file   disk     vda      /ws/b.next\n"
        chains = dict(self.CHAIN, **{
            "/ws/b.next": ('[{"filename": "/ws/b.next", '
                           '"full-backing-filename": "/ws/removed.qcow2"}, '
                           '{"filename": "/ws/removed.qcow2"}]')})
        in_use, _ = self._run(inventories={("vm-b", True): persistent},
                              chains=chains)
        assert in_use["/ws/b.top"] == "vm-b"
        assert in_use["/ws/b.next"] == "vm-b"
        assert in_use["/ws/removed.qcow2"] == "vm-b"

    def test_none_when_a_persistent_inventory_cannot_be_read(self):
        in_use, _ = self._run(failures=[("vm-b", True)])
        assert in_use is None

    def test_a_source_in_both_inventories_is_read_once(self):
        _in_use, cmd = self._run()
        sources = [c.args[0].rsplit(" ", 1)[1].strip("'")
                   for c in cmd.return_value.execute_shell.call_args_list]
        assert sorted(sources) == sorted(set(sources))

    # -- R2-3: an incomplete answer is "cannot tell" -----------------------

    def test_none_when_a_row_has_no_source(self):
        in_use, _ = self._run(inventories={
            ("vm-b", False): self.HEADER + " file   disk     vda\n"})
        assert in_use is None

    def test_none_when_a_row_cannot_be_parsed(self):
        in_use, _ = self._run(inventories={
            ("vm-b", False): self.BLK_B + " garbled\n"})
        assert in_use is None

    def test_none_when_the_chain_is_empty(self):
        chains = dict(self.CHAIN, **{"/ws/a.qcow2": "[]"})
        in_use, _ = self._run(chains=chains)
        assert in_use is None

    def test_none_when_a_chain_entry_has_no_filename(self):
        chains = dict(self.CHAIN, **{"/ws/a.qcow2": '[{"format": "qcow2"}]'})
        in_use, _ = self._run(chains=chains)
        assert in_use is None

    def test_none_when_the_chain_is_not_a_list_of_images(self):
        chains = dict(self.CHAIN, **{"/ws/a.qcow2": '"a.qcow2"'})
        in_use, _ = self._run(chains=chains)
        assert in_use is None

    # -- #208: pool volumes are local files; only network is remote --------

    VOLUME_XML = (
        "<domain><devices>"
        "<disk type='volume' device='disk'>"
        "<source pool='p1' volume='top.qcow2'/><target dev='vdb'/></disk>"
        "<disk type='volume' device='cdrom'>"
        "<source pool='p1' volume='install.iso'/><target dev='sdc'/></disk>"
        "</devices></domain>")
    VOLUME_ROWS = (" volume    disk    vdb   top.qcow2\n"
                   " volume    cdrom   sdc   install.iso\n"
                   " network   disk    vdd   rbd-pool/image\n")
    VOLUME_PATHS = {"top.qcow2": "/pool/top.qcow2",
                    "install.iso": "/pool/install.iso"}

    def _run_volumes(self, dumpxml=None, vol_paths=None, extra_rows=""):
        rows = self.HEADER + self.VOLUME_ROWS + extra_rows
        dumpxml = self.VOLUME_XML if dumpxml is None else dumpxml
        vol_paths = self.VOLUME_PATHS if vol_paths is None else vol_paths
        chains = dict(self.CHAIN, **{
            "/pool/top.qcow2": ('[{"filename": "/pool/top.qcow2", '
                                '"full-backing-filename": "/ws/removed.qcow2"}, '
                                '{"filename": "/ws/removed.qcow2"}]'),
            "/pool/install.iso": '{"filename": "/pool/install.iso"}',
            # readable, so only the type check can refuse the dir row below
            "/srv/share": '{"filename": "/srv/share"}'})

        def virsh_execute(*args, **kwargs):
            if args[0] == "list":
                return _result(stdout=domain_listing(args, "vm-a", "vm-b")
                               if "--all" in args else "")
            if args[0] == "dumpxml":
                return _result(stdout=dumpxml)
            if args[0] == "vol-path":
                path = vol_paths.get(args[1])
                return (_result(stdout=path + "\n") if path
                        else _result(ok=False, stderr="no storage vol"))
            if args[1] == "vm-b":
                return _result(stdout=rows)
            return _result(stdout=self.BLK_A)

        def shell(command, **kwargs):
            source = command.rsplit(" ", 1)[1].strip("'")
            if source not in chains:
                return _result(ok=False, stderr="Could not open")
            return _result(stdout=chains[source])

        s = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh, \
             patch("boxman.providers.libvirt.session.LibVirtCommandBase") as cmd:
            virsh.return_value.execute.side_effect = virsh_execute
            cmd.return_value.execute_shell.side_effect = shell
            return s.disk_paths_in_use(), cmd, virsh.return_value.execute

    def test_local_pool_volumes_are_resolved_and_mapped(self):
        """A volume-type disk and CD-ROM are local files of a directory
        pool: mapped by their resolved paths, the disk's backing file too.
        Observed on libvirt 10.0: domblklist shows only the volume name."""
        in_use, _cmd, execute = self._run_volumes()
        assert in_use["/pool/top.qcow2"] == "vm-b"
        assert in_use["/ws/removed.qcow2"] == "vm-b"
        assert in_use["/pool/install.iso"] == "vm-b"
        paths = [c.args for c in execute.call_args_list
                 if c.args[0] == "vol-path"]
        assert ("vol-path", "top.qcow2") in paths
        assert all(c.kwargs.get("pool") == "p1"
                   for c in execute.call_args_list if c.args[0] == "vol-path")

    def test_a_network_source_is_skipped_not_fatal(self):
        in_use, cmd, _ = self._run_volumes()
        assert in_use is not None
        asked = " ".join(c.args[0]
                         for c in cmd.return_value.execute_shell.call_args_list)
        assert "rbd-pool/image" not in asked

    def test_none_when_a_volume_cannot_be_resolved(self):
        in_use, _, _ = self._run_volumes(
            vol_paths={"install.iso": "/pool/install.iso"})
        assert in_use is None

    def test_none_when_the_volume_xml_names_no_pool(self):
        in_use, _, _ = self._run_volumes(dumpxml="<domain><devices/></domain>")
        assert in_use is None

    def test_none_for_a_source_type_neither_local_nor_remote(self):
        in_use, _, _ = self._run_volumes(
            extra_rows=" dir   disk   vde   /srv/share\n")
        assert in_use is None


class TestVmStorageDevices:
    """The torn-down domain's own inventory: both definitions, so a CD-ROM
    or disk only in the persistent XML is known before undefining (#208)."""

    LIVE = (TestDiskPathsInUse.HEADER
            + " file   disk    vda   /ws/vm.qcow2\n"
            + " file   cdrom   sda   /iso/live.iso\n")
    PERSISTENT = (TestDiskPathsInUse.HEADER
                  + " file   disk    vda   /ws/vm.qcow2\n"
                  + " file   cdrom   sda   /iso/next-boot.iso\n")

    def _run(self, live, persistent):
        def execute(*args, **kwargs):
            out = persistent if "--inactive" in args else live
            return out if not isinstance(out, str) else _result(stdout=out)

        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh:
            virsh.return_value.execute.side_effect = execute
            return _session({}).vm_storage_devices("vm")

    def test_both_definitions_de_duplicated(self):
        rows = self._run(self.LIVE, self.PERSISTENT)
        assert [(r.device, r.source) for r in rows] == [
            ("disk", "/ws/vm.qcow2"),
            ("cdrom", "/iso/live.iso"),
            ("cdrom", "/iso/next-boot.iso"),
        ]

    def test_none_when_either_definition_cannot_be_read(self):
        assert self._run(self.LIVE, _result(ok=False)) is None
        assert self._run(_result(ok=False), self.PERSISTENT) is None

    def test_none_when_a_definition_reads_as_incomplete(self):
        assert self._run(self.LIVE, self.PERSISTENT + " garbled\n") is None

    def _run_volume(self, vol_path):
        rows = (TestDiskPathsInUse.HEADER
                + " file     disk    vda   /ws/vm.qcow2\n"
                + " volume   cdrom   sda   install.iso\n"
                + " network  disk    vdb   rbd-pool/image\n")
        xml = ("<domain><devices><disk type='volume' device='cdrom'>"
               "<source pool='isos' volume='install.iso'/>"
               "<target dev='sda'/></disk></devices></domain>")

        def execute(*args, **kwargs):
            if args[0] == "dumpxml":
                return _result(stdout=xml)
            if args[0] == "vol-path":
                return (_result(stdout=vol_path + "\n") if vol_path
                        else _result(ok=False, stderr="no storage vol"))
            return _result(stdout=rows)

        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh:
            virsh.return_value.execute.side_effect = execute
            return _session({}).vm_storage_devices("vm")

    def test_a_volume_cdrom_is_resolved_to_its_path(self):
        """So the teardown's media exclusion sees the file it names
        (#212+#208 review, 3)."""
        rows = self._run_volume("/pool/isos/install.iso")
        assert ("volume", "cdrom", "/pool/isos/install.iso") in [
            (r.type, r.device, r.source) for r in rows]
        # a remote source is kept as reported; consumers skip it
        assert ("network", "rbd-pool/image") in [(r.type, r.source)
                                                  for r in rows]

    def test_none_when_a_volume_cannot_be_resolved(self):
        assert self._run_volume(None) is None

    def test_none_for_a_source_type_neither_local_nor_remote(self):
        rows = (TestDiskPathsInUse.HEADER
                + " file   disk   vda   /ws/vm.qcow2\n"
                + " dir    disk   vdb   /srv/share\n")
        assert self._run(rows, rows) is None


class TestBackingChains:
    """The chain of each extra disk of a VM being torn down, read before
    undefining: head first, the bottom image last (#212 review round 3,
    R3-2; #208)."""

    def _run(self, sources, absent=()):
        """*absent* lists the paths the absence probe confirms absent."""
        chains = TestDiskPathsInUse.CHAIN

        def shell(command, **kwargs):
            if _ABSENT in command:
                probed = shlex.split(command)[3]
                return _result(stdout=_ABSENT if probed in absent else "")
            source = command.rsplit(" ", 1)[1].strip("'")
            if source not in chains:
                return _result(ok=False, stderr="Could not open")
            return _result(stdout=chains[source])

        with patch("boxman.providers.libvirt.session.LibVirtCommandBase") as cmd:
            cmd.return_value.execute_shell.side_effect = shell
            return _session({}).backing_chains(sources)

    def test_each_source_maps_to_its_chain_head_first(self):
        assert self._run(["/ws/a.qcow2", "/ws/b.top"]) == {
            "/ws/a.qcow2": ["/ws/a.qcow2"],
            "/ws/b.top": ["/ws/b.top", "/tpl/base.qcow2"],
        }

    def test_none_when_any_chain_cannot_be_read(self):
        """... and its source is not confirmed absent."""
        assert self._run(["/ws/a.qcow2", "/ws/missing.qcow2"]) is None

    def test_a_source_confirmed_absent_is_left_out(self):
        assert self._run(["/ws/a.qcow2", "/ws/missing.qcow2"],
                         absent={"/ws/missing.qcow2"}) == {
            "/ws/a.qcow2": ["/ws/a.qcow2"]}

    def test_no_sources_is_an_empty_answer(self):
        assert self._run([]) == {}


class TestInUseWithAMissingSource:
    """A source confirmed absent uses nothing only in a domain positively
    shut off: an active one -- running, paused, suspended, crashed -- can
    hold the unlinked image open together with the images below it, whose
    files still exist (#208 review round 4, 2)."""

    BLK = (TestDiskPathsInUse.HEADER
           + " file   disk     vda      /ws/b.qcow2\n"
           + " file   disk     vdb      /ws/gone.qcow2\n")

    def _run(self, state, state_ok=True, absent=("/ws/gone.qcow2",),
             live="", dump_ok=True):
        """*live* is what ``virsh dumpxml`` (the live definition) says."""
        calls = []

        def virsh_execute(*args, **kwargs):
            calls.append(args[0])
            if args[0] == "list":
                # the running domains are the ones not shut off
                return _result(stdout=domain_listing(args, "vm-b")
                               if "--all" in args or state != "shut off"
                               else "")
            if args[0] == "domstate":
                assert args[1] == "vm-b"
                return _result(stdout=f"{state}\n", ok=state_ok)
            if args[0] == "dumpxml":
                assert "--inactive" not in args
                return _result(stdout=live, ok=dump_ok)
            return _result(stdout=self.BLK)

        def shell(command, **kwargs):
            if _ABSENT in command:
                probed = shlex.split(command)[3]
                calls.append(f"probe {probed}")
                return _result(stdout=_ABSENT if probed in absent else "")
            source = command.rsplit(" ", 1)[1].strip("'")
            calls.append(f"qemu-img {source}")
            if source == "/ws/b.qcow2":
                return _result(stdout='{"filename": "/ws/b.qcow2"}')
            return _result(ok=False, stderr="Could not open")

        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh, \
             patch("boxman.providers.libvirt.session.LibVirtCommandBase") as cmd:
            virsh.return_value.execute.side_effect = virsh_execute
            cmd.return_value.execute_shell.side_effect = shell
            return _session({}).disk_paths_in_use(), calls

    @pytest.mark.parametrize("state", [
        "running", "paused", "pmsuspended", "in shutdown", "crashed", "idle"])
    def test_an_active_domain_whose_live_definition_cannot_be_read(
            self, state, captured_logs):
        """... fails the scan, and says why."""
        in_use, _ = self._run(state, live="not xml")
        assert in_use is None
        assert "vm-b" in captured_logs.text
        assert "/ws/gone.qcow2" in captured_logs.text
        assert "live definition could not be read" in captured_logs.text

    def test_a_shut_off_domain_is_scanned_without_it(self):
        in_use, _ = self._run("shut off")
        assert in_use == {"/ws/b.qcow2": "vm-b"}

    def test_a_state_that_cannot_be_read_fails_the_scan(self, captured_logs):
        """... even when the live definition would say it holds nothing."""
        live = ("<domain><devices><disk type='file' device='disk'>"
                "<source file='/ws/gone.qcow2' index='1'/><backingStore/>"
                "<target dev='vdb'/></disk></devices></domain>")
        in_use, calls = self._run("shut off", state_ok=False, live=live)
        assert in_use is None
        assert "vm-b" in captured_logs.text
        assert "state could not be read" in captured_logs.text
        assert "dumpxml" not in calls

    def test_the_state_is_asked_only_once_the_absence_is_confirmed(self):
        _, calls = self._run("shut off")
        assert calls.count("domstate") == 1
        assert calls.index("domstate") > calls.index("probe /ws/gone.qcow2")

    def test_no_state_is_asked_while_absence_is_not_confirmed(self):
        in_use, calls = self._run("shut off", absent=())
        assert in_use is None
        assert "domstate" not in calls

    # -- the live backing chain of a running domain's missing source --------

    @staticmethod
    def _live(disk_inner, device="disk", target="vdb"):
        return (f"<domain><devices><disk type='file' device='{device}'>"
                f"{disk_inner}<target dev='{target}'/></disk>"
                f"</devices></domain>")

    @staticmethod
    def _level(path, inner="<backingStore/>", kind="file", attr="file"):
        return (f"<backingStore type='{kind}' index='2'>"
                f"<format type='qcow2'/><source {attr}='{path}'/>{inner}"
                f"</backingStore>")

    def test_a_running_domain_maps_what_its_live_chain_holds(self, tmp_path):
        base = tmp_path / "base.qcow2"
        base.write_bytes(b"base")
        live = self._live("<source file='/ws/gone.qcow2' index='1'/>"
                          + self._level(base))

        in_use, calls = self._run("running", live=live)

        assert in_use == {"/ws/b.qcow2": "vm-b",
                          os.path.realpath(base): "vm-b"}
        st = os.stat(base)
        assert in_use.identities[(st.st_dev, st.st_ino)] == "vm-b"
        assert calls.count("dumpxml") == 1

    def test_a_running_domain_whose_whole_chain_is_gone_holds_nothing(
            self, tmp_path):
        """The b2p2-lab case: head and base both deleted -- nothing on
        disk to protect, and the scan completes."""
        live = self._live("<source file='/ws/gone.qcow2' index='1'/>"
                          + self._level(tmp_path / "gone-base.qcow2"))

        in_use, _ = self._run("running", live=live)

        assert in_use == {"/ws/b.qcow2": "vm-b"}

    def test_a_deleted_seed_with_an_empty_chain_holds_nothing(self):
        live = self._live("<source file='/ws/gone.qcow2' index='3'/>"
                          "<backingStore/>", device="cdrom")

        in_use, _ = self._run("running", live=live)

        assert in_use == {"/ws/b.qcow2": "vm-b"}

    def test_a_source_only_in_the_persistent_definition_is_skipped(self):
        """The live definition does not hold it, so QEMU does not."""
        live = self._live("<source file='/ws/b.qcow2' index='1'/>"
                          "<backingStore/>", target="vda")

        in_use, _ = self._run("running", live=live)

        assert in_use == {"/ws/b.qcow2": "vm-b"}

    def test_a_network_level_names_nothing_local(self, tmp_path):
        base = tmp_path / "base.qcow2"
        base.write_bytes(b"base")
        network = ("<backingStore type='network' index='2'>"
                   "<source protocol='rbd' name='pool/img'/>"
                   + self._level(base) + "</backingStore>")
        live = self._live("<source file='/ws/gone.qcow2' index='1'/>"
                          + network)

        in_use, _ = self._run("running", live=live)

        assert in_use == {"/ws/b.qcow2": "vm-b",
                          os.path.realpath(base): "vm-b"}

    @pytest.mark.parametrize("disk_inner, why", [
        ("<source file='/ws/gone.qcow2' index='1'/>",
         "records no backing chain"),
        ("<source file='/ws/gone.qcow2' index='1'/><backingStore/>"
         "<mirror type='file' job='copy'><source file='/ws/m.qcow2'/>"
         "</mirror>", "block job"),
        ("<source file='/ws/gone.qcow2' index='1'/>"
         "<backingStore type='volume' index='2'>"
         "<source pool='p' volume='v'/><backingStore/></backingStore>",
         "volume level"),
        ("<source file='/ws/gone.qcow2' index='1'/>"
         "<backingStore type='file' index='2'>"
         "<source file='/ws/base.qcow2'/></backingStore>",
         "only in part"),
    ], ids=["no backingStore", "mirror", "volume level", "open-ended"])
    def test_a_chain_that_cannot_be_told_fails_the_scan(
            self, disk_inner, why, captured_logs):
        in_use, _ = self._run("running", live=self._live(disk_inner))

        assert in_use is None
        assert why in captured_logs.text
        assert "/ws/gone.qcow2" in captured_logs.text

    def test_every_live_disk_with_the_source_is_examined(
            self, captured_logs):
        """#208 review round 6, 2: the same missing source on two live
        disks, a block copy on the second."""
        disks = ("<devices><disk type='file' device='disk'>"
                 "<source file='/ws/gone.qcow2' index='1'/><backingStore/>"
                 "<target dev='vdb'/></disk>"
                 "<disk type='file' device='disk'>"
                 "<source file='/ws/gone.qcow2' index='3'/><backingStore/>"
                 "<mirror type='file' job='copy'><source file='/ws/c.qcow2'/>"
                 "</mirror><target dev='vdc'/></disk></devices>")

        in_use, _ = self._run("running", live=f"<domain>{disks}</domain>")

        assert in_use is None
        assert "block job" in captured_logs.text

    def test_the_files_every_such_disk_holds_are_all_mapped(self, tmp_path):
        one, two = tmp_path / "one.qcow2", tmp_path / "two.qcow2"
        one.write_bytes(b"one")
        two.write_bytes(b"two")
        live = ("<domain><devices><disk type='file' device='disk'>"
                "<source file='/ws/gone.qcow2' index='1'/>"
                + self._level(one) + "<target dev='vdb'/></disk>"
                "<disk type='file' device='disk'>"
                "<source file='/ws/gone.qcow2' index='3'/>"
                + self._level(two) + "<target dev='vdc'/></disk>"
                "</devices></domain>")

        in_use, _ = self._run("running", live=live)

        assert in_use == {"/ws/b.qcow2": "vm-b",
                          os.path.realpath(one): "vm-b",
                          os.path.realpath(two): "vm-b"}

    def test_a_live_definition_that_cannot_be_dumped_fails_the_scan(
            self, captured_logs):
        in_use, _ = self._run("running", dump_ok=False)

        assert in_use is None
        assert "live definition could not be read" in captured_logs.text

    @pytest.mark.skipif(os.geteuid() == 0, reason="root searches mode 000")
    def test_a_held_file_whose_identity_cannot_be_read_fails_the_scan(
            self, tmp_path, captured_logs):
        hidden = tmp_path / "hidden"
        hidden.mkdir()
        base = hidden / "base.qcow2"
        base.write_bytes(b"base")
        live = self._live("<source file='/ws/gone.qcow2' index='1'/>"
                          + self._level(base))
        hidden.chmod(0)
        try:
            in_use, _ = self._run("running", live=live)
        finally:
            hidden.chmod(0o700)

        assert in_use is None
        assert str(base) in captured_logs.text

    def test_a_missing_volume_source_fails_the_scan(self, captured_logs):
        """A pool volume's chain is not read from the live definition."""
        virsh = MagicMock()
        virsh.execute.side_effect = lambda *args, **kwargs: _result(
            stdout=domain_listing(args, "vm-b") if args[0] == "list"
            else "running\n")

        held = _session({})._held_below(virsh, "vm-b", domain_uuid("vm-b"),
                                        "/pool/gone.qcow2", True, {})

        assert held is None
        assert "volume" in captured_logs.text


class TestInUseByIdentity:
    """Every file another domain uses is recorded by its identity too, read
    host-side, so a teardown recognises it under any other name; a file
    whose identity cannot be read fails the scan (#208, the alias
    review)."""

    @staticmethod
    def _scan(tmp_path, source, chain_json):
        blk = (TestDiskPathsInUse.HEADER
               + f" file   disk     vda      {source}\n")

        def virsh_execute(*args, **kwargs):
            if args[0] == "list":
                return _result(stdout=domain_listing(args, "vm-b")
                               if "--all" in args else "")
            return _result(stdout=blk)

        def shell(command, **kwargs):
            if _ABSENT in command:
                return _result(stdout="")
            asked = command.rsplit(" ", 1)[1].strip("'")
            if asked != str(source):
                return _result(ok=False, stderr="Could not open")
            return _result(stdout=chain_json)

        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh, \
             patch("boxman.providers.libvirt.session.LibVirtCommandBase") as cmd:
            virsh.return_value.execute.side_effect = virsh_execute
            cmd.return_value.execute_shell.side_effect = shell
            return _session({}).disk_paths_in_use()

    @staticmethod
    def _chain(top, base):
        return (f'[{{"filename": "{top}", "full-backing-filename": '
                f'"{base}"}}, {{"filename": "{base}"}}]')

    def test_every_file_is_recorded_by_identity_too(self, tmp_path):
        top, base = tmp_path / "b.top", tmp_path / "base.qcow2"
        top.write_bytes(b"top")
        base.write_bytes(b"base")

        in_use = self._scan(tmp_path, top, self._chain(top, base))

        assert in_use == {os.path.realpath(top): "vm-b",
                          os.path.realpath(base): "vm-b"}
        for path in (top, base):
            st = os.stat(path)
            assert in_use.identities[(st.st_dev, st.st_ino)] == "vm-b"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root searches mode 000")
    def test_a_file_whose_identity_cannot_be_read_fails_the_scan(
            self, tmp_path, captured_logs):
        hidden = tmp_path / "hidden"
        hidden.mkdir()
        top, base = hidden / "b.top", hidden / "base.qcow2"
        top.write_bytes(b"top")
        base.write_bytes(b"base")
        hidden.chmod(0)
        try:
            in_use = self._scan(tmp_path, top, self._chain(top, base))
        finally:
            hidden.chmod(0o700)

        assert in_use is None
        assert str(top) in captured_logs.text
        assert "make it accessible" in captured_logs.text

    @pytest.mark.skipif(os.geteuid() == 0, reason="root searches mode 000")
    def test_a_directory_that_can_be_searched_but_not_listed_is_read(
            self, tmp_path):
        """Ordinary use: /var/lib/libvirt/images is root's, 0711 -- its
        files still stat fine for a user, as 0111 is to its owner."""
        images = tmp_path / "images"
        images.mkdir()
        top, base = images / "b.top", images / "base.qcow2"
        top.write_bytes(b"top")
        base.write_bytes(b"base")
        images.chmod(0o111)
        try:
            in_use = self._scan(tmp_path, top, self._chain(top, base))
        finally:
            images.chmod(0o700)

        assert in_use is not None
        assert len(in_use.identities) == 2


class TestInUseWithABlockJob:
    """A block job's destination -- a block copy's target, written while
    the job runs -- is in use by the domain running it, together with
    everything below it. Only an active domain runs one, and its live
    definition names it (#208, the mirror analysis)."""

    BLK = TestDiskPathsInUse.HEADER + " file   disk     vda      /ws/b.qcow2\n"
    PLAIN = {"/ws/a.qcow2": "vm-a", "/ws/b.qcow2": "vm-b"}
    COPY = ("<mirror type='file' file='/w/web.copy.qcow2' format='qcow2' "
            "job='copy'><format type='qcow2'/>"
            "<source file='/w/web.copy.qcow2' index='2'/><backingStore/>"
            "</mirror>")

    def _run(self, live="", active="vm-b\n", active_ok=True, dump_ok=True,
             chains=None):
        """vm-a is shut off; vm-b is active unless *active* says otherwise,
        and *live* is its live definition."""
        calls = []
        chains = dict(TestDiskPathsInUse.CHAIN,
                      **{"/ws/b.qcow2": '{"filename": "/ws/b.qcow2"}'},
                      **(chains or {}))

        def virsh_execute(*args, **kwargs):
            calls.append(args)
            if args[0] == "list":
                if "--all" in args:
                    return _result(stdout=domain_listing(args, "vm-a", "vm-b"))
                if not active_ok:
                    return _result(ok=False, stderr="error: failed")
                return _result(stdout=active)
            if args[0] == "dumpxml":
                return _result(stdout=live, ok=dump_ok)
            if args[1] == "vm-a":
                return _result(stdout=TestDiskPathsInUse.BLK_A)
            return _result(stdout=self.BLK)

        def shell(command, **kwargs):
            if _ABSENT in command:
                return _result(stdout="")
            source = command.rsplit(" ", 1)[1].strip("'")
            if source not in chains:
                return _result(ok=False, stderr="Could not open")
            return _result(stdout=chains[source])

        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh, \
             patch("boxman.providers.libvirt.session.LibVirtCommandBase") as cmd:
            virsh.return_value.execute.side_effect = virsh_execute
            cmd.return_value.execute_shell.side_effect = shell
            return _session({}).disk_paths_in_use(), calls

    @staticmethod
    def _live(mirror):
        return ("<domain><devices><disk type='file' device='disk'>"
                "<source file='/ws/b.qcow2' index='1'/><backingStore/>"
                f"{mirror}<target dev='vda'/></disk></devices></domain>")

    def test_a_block_copy_destination_is_in_use(self):
        in_use, _ = self._run(live=self._live(self.COPY), chains={
            "/w/web.copy.qcow2": '{"filename": "/w/web.copy.qcow2"}'})

        assert in_use == dict(self.PLAIN, **{"/w/web.copy.qcow2": "vm-b"})

    def test_everything_below_the_destination_is_in_use_too(self):
        in_use, _ = self._run(live=self._live(self.COPY), chains={
            "/w/web.copy.qcow2": (
                '[{"filename": "/w/web.copy.qcow2", '
                '"full-backing-filename": "/w/base.qcow2"}, '
                '{"filename": "/w/base.qcow2"}]')})

        assert in_use["/w/web.copy.qcow2"] == "vm-b"
        assert in_use["/w/base.qcow2"] == "vm-b"

    @pytest.mark.parametrize("mirror, destination", [
        ("<mirror type='file' file='/w/copy.qcow2' format='qcow2' "
         "job='copy'/>", "/w/copy.qcow2"),
        ("<mirror type='file' job='copy'><format type='qcow2'/>"
         "<source file='/w/copy.qcow2'/></mirror>", "/w/copy.qcow2"),
        ("<mirror type='block' job='copy'><format type='raw'/>"
         "<source dev='/dev/vg/copy'/></mirror>", "/dev/vg/copy"),
    ], ids=["file attribute", "source file", "source dev"])
    def test_the_destination_is_named_either_way(self, mirror, destination):
        in_use, _ = self._run(live=self._live(mirror), chains={
            destination: f'{{"filename": "{destination}"}}'})

        assert in_use[destination] == "vm-b"

    def test_the_destination_and_what_backs_it_are_known_by_identity(
            self, tmp_path):
        copy, base = tmp_path / "web.copy.qcow2", tmp_path / "base.qcow2"
        copy.write_bytes(b"")
        base.write_bytes(b"")
        mirror = (f"<mirror type='file' file='{copy}' job='copy'>"
                  f"<source file='{copy}'/></mirror>")
        in_use, _ = self._run(live=self._live(mirror), chains={
            str(copy): (f'[{{"filename": "{copy}", "full-backing-filename": '
                        f'"{base}"}}, {{"filename": "{base}"}}]')})

        for path in (copy, base):
            st = os.stat(path)
            assert in_use.identities[(st.st_dev, st.st_ino)] == "vm-b"

    def test_a_destination_whose_identity_cannot_be_read_fails_the_scan(
            self, tmp_path, captured_logs):
        if os.geteuid() == 0:
            pytest.skip("root reads past mode 000")
        locked = tmp_path / "locked"
        locked.mkdir()
        copy = locked / "web.copy.qcow2"
        copy.write_bytes(b"")
        mirror = f"<mirror type='file' file='{copy}' job='copy'/>"
        locked.chmod(0o600)
        try:
            in_use, _ = self._run(live=self._live(mirror), chains={
                str(copy): f'{{"filename": "{copy}"}}'})
        finally:
            locked.chmod(0o700)

        assert in_use is None
        assert f"could not read the identity of {copy}" in captured_logs.text

    def test_a_destination_whose_chain_cannot_be_read_fails_the_scan(
            self, captured_logs):
        in_use, _ = self._run(live=self._live(self.COPY))

        assert in_use is None
        assert ("a block job on domain vm-b holds /w/web.copy.qcow2"
                in captured_logs.text)
        assert "vda" in captured_logs.text

    @pytest.mark.parametrize("live, dump_ok", [
        ("", False), ("not xml", True)], ids=["not dumped", "not xml"])
    def test_a_live_definition_that_cannot_be_read_fails_the_scan(
            self, captured_logs, live, dump_ok):
        in_use, _ = self._run(live=live, dump_ok=dump_ok)

        assert in_use is None
        assert "live definition of domain vm-b" in captured_logs.text

    @pytest.mark.parametrize("mirror", [
        "<mirror type='file' job='copy'><format type='qcow2'/></mirror>",
        "<mirror type='volume' job='copy'><source pool='p' volume='v'/>"
        "</mirror>",
    ], ids=["no source", "a volume"])
    def test_a_mirror_that_names_no_file_fails_the_scan(
            self, captured_logs, mirror):
        in_use, _ = self._run(live=self._live(mirror))

        assert in_use is None
        assert "a block job on domain vm-b" in captured_logs.text

    def test_every_disks_block_job_is_examined(self):
        live = ("<domain><devices>"
                "<disk type='file' device='disk'>"
                "<source file='/ws/b.qcow2'/><target dev='vda'/></disk>"
                "<disk type='file' device='disk'>"
                "<source file='/ws/c.qcow2'/><mirror type='file' "
                "file='/w/c.copy.qcow2' job='copy'/><target dev='vdb'/></disk>"
                "<disk type='file' device='disk'>"
                "<source file='/ws/d.qcow2'/><mirror type='file' "
                "file='/w/d.copy.qcow2' job='copy'/><target dev='vdc'/></disk>"
                "</devices></domain>")
        in_use, _ = self._run(live=live, chains={
            "/w/c.copy.qcow2": '{"filename": "/w/c.copy.qcow2"}',
            "/w/d.copy.qcow2": '{"filename": "/w/d.copy.qcow2"}'})

        assert in_use["/w/c.copy.qcow2"] == "vm-b"
        assert in_use["/w/d.copy.qcow2"] == "vm-b"

    def test_the_running_domains_that_cannot_be_listed_fail_the_scan(
            self, captured_logs):
        in_use, _ = self._run(live=self._live(""), active_ok=False)

        assert in_use is None
        assert "could not list the running domains" in captured_logs.text

    def test_a_network_mirror_names_nothing_local(self):
        in_use, _ = self._run(live=self._live(
            "<mirror type='network' job='copy'><format type='raw'/>"
            "<source protocol='nbd' name='copy'>"
            "<host name='example' port='10809'/></source></mirror>"))

        assert in_use == self.PLAIN

    def test_a_shut_off_domain_is_not_read(self):
        in_use, calls = self._run(active="")

        assert in_use == self.PLAIN
        assert not [c for c in calls if c[0] == "dumpxml"]

    def test_an_active_domain_without_a_block_job_changes_nothing(self):
        in_use, calls = self._run(live=self._live(""))

        assert in_use == self.PLAIN
        assert [c for c in calls if c[0] == "dumpxml"] == [("dumpxml", "vm-b")]


class TestInUseWhileDomainsGoAway:
    """deprovision tears VMs down in parallel, so a domain listed when the
    scan starts can be undefined by its own teardown before the scan reads
    it. Once what it uses cannot be told and a fresh, successful listing
    names neither it nor its UUID, it holds nothing and is skipped, with
    whatever was read of it; still listed, renamed (its UUID under another
    name), or a listing that fails or cannot be read, fails the scan
    (#213)."""

    CHAINS = {"/ws/a.qcow2": '{"filename": "/ws/a.qcow2"}',
              "/ws/b.qcow2": '{"filename": "/ws/b.qcow2"}'}
    A_ONLY = {"/ws/a.qcow2": "vm-a"}

    # how vm-b fails: the query of it that fails, and what reaches it
    FAILURES = {
        # its inventory
        "inventory": dict(failing={("domblklist", "vm-b")}),
        # the chain of a source spelled so that its absence is not probed
        "unprobed source": dict(unprobed=True),
        # its state, asked for a source of it that no longer exists
        "state": dict(failing={("domstate", "vm-b")}, missing=True),
        # its live definition, read for its block jobs while it runs
        "live definition": dict(failing={("dumpxml", "vm-b")},
                                active={"vm-b"}),
        # what that definition names: a block job's destination...
        "mirror": dict(active={"vm-b"},
                       mirror="<mirror type='file' job='copy'/>"),
        # ... or its chain
        "destination": dict(active={"vm-b"}, mirror=(
            "<mirror type='file' file='/w/copy.qcow2' job='copy'/>")),
    }
    #: vm-b under another name, virsh domrename's doing: the same UUID
    RENAMED = (domain_uuid("vm-b"), "vm-renamed")
    #: a new domain defined under vm-b's name: another UUID
    REPLACED = (domain_uuid("vm-b, again"), "vm-b")

    def _run(self, relists, failing=frozenset(), active=frozenset(),
             missing=False, unprobed=False, mirror="", first=None):
        """vm-a, then vm-b, each read as listed first (or as *first* says,
        raw); the *failing* ``(query, domain)`` pairs fail. Each listing
        after the first answers the next of *relists*: the domains it
        lists, its raw output, or ``None`` for one that fails. vm-b's disk
        carries *mirror*; it has a CD-ROM that no longer exists when
        *missing*, and a second disk, spelled with ``..``, whose chain
        cannot be read when *unprobed*."""
        calls = []
        relists = iter(relists)
        blk_b = (TestDiskPathsInUse.HEADER
                 + " file   disk     vda      /ws/b.qcow2\n"
                 + (" file   disk     vdb      /ws/heads/../b2.qcow2\n"
                    if unprobed else "")
                 + (" file   cdrom    sda      /ws/gone.iso\n"
                    if missing else ""))
        live = ("<domain><devices><disk type='file' device='disk'>"
                f"<source file='/ws/b.qcow2'/>{mirror}<target dev='vda'/>"
                "</disk></devices></domain>")

        def virsh_execute(*args, **kwargs):
            calls.append(args)
            if args[0] == "list":
                if "--all" not in args:
                    return _result(stdout="".join(f"{d}\n" for d in active))
                if sum(c[:2] == ("list", "--all") for c in calls) == 1:
                    return _result(stdout=first if first is not None
                                   else domain_listing(args, "vm-a", "vm-b"))
                answer = next(relists)
                if answer is None:
                    return _result(ok=False, stderr="error: failed to "
                                   "connect to the hypervisor")
                return _result(stdout=answer if isinstance(answer, str)
                               else domain_listing(args, *answer))
            if (args[0], args[1]) in failing:
                return _result(ok=False, stderr=f"error: failed to get "
                               f"domain '{args[1]}'")
            if args[0] == "domblklist":
                return _result(stdout=TestDiskPathsInUse.BLK_A
                               if args[1] == "vm-a" else blk_b)
            if args[0] == "domstate":
                return _result(stdout="running\n")
            if args[0] == "dumpxml":
                return _result(stdout=live)
            raise AssertionError(f"unexpected virsh {args}")

        def shell(command, **kwargs):
            if _ABSENT in command:
                probed = shlex.split(command)[3]
                return _result(stdout=_ABSENT if probed == "/ws/gone.iso"
                               else "")
            source = command.rsplit(" ", 1)[1].strip("'")
            if source not in self.CHAINS:
                return _result(ok=False, stderr="Could not open")
            return _result(stdout=self.CHAINS[source])

        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh, \
             patch("boxman.providers.libvirt.session.LibVirtCommandBase") as cmd:
            virsh.return_value.execute.side_effect = virsh_execute
            cmd.return_value.execute_shell.side_effect = shell
            return _session({}).disk_paths_in_use(), calls

    @pytest.mark.parametrize("failure", FAILURES)
    def test_a_domain_gone_since_is_skipped(self, captured_logs, tmp_path,
                                            failure):
        """... with what was read of it before -- its disk, and the image
        below it, by identity too -- and no warning: a teardown that removes
        nothing of it has nothing to report."""
        base = tmp_path / "b-base.qcow2"
        base.write_bytes(b"")
        self.CHAINS = dict(self.CHAINS, **{"/ws/b.qcow2": (
            f'[{{"filename": "/ws/b.qcow2", "full-backing-filename": '
            f'"{base}"}}, {{"filename": "{base}"}}]')})

        in_use, _ = self._run([("vm-a",)], **self.FAILURES[failure])

        assert in_use == self.A_ONLY
        assert in_use.identities == {}
        assert not [r for r in captured_logs.records if r.levelno >= 30]

    @pytest.mark.parametrize("failure", FAILURES)
    @pytest.mark.parametrize("listed", [("vm-a", "vm-b"), ("vm-a", REPLACED)],
                             ids=["still there", "replaced"])
    def test_a_domain_still_listed_fails_the_scan(self, failure, listed):
        """... whether it never went -- a running domain undefined
        meanwhile stays listed, transient -- or a new one was defined under
        its name: whatever the failed query's error says."""
        in_use, _ = self._run([listed], **self.FAILURES[failure])

        assert in_use is None

    @pytest.mark.parametrize("failure", FAILURES)
    def test_a_domain_renamed_since_fails_the_scan(self, captured_logs,
                                                   failure):
        """virsh domrename keeps a domain and its disks under a name the
        scan never read: its UUID tells it from a deleted one, and the scan
        fails, naming both names (review round 8, 1)."""
        in_use, _ = self._run([("vm-a", self.RENAMED)],
                              **self.FAILURES[failure])

        assert in_use is None
        assert "domain vm-b was renamed vm-renamed" in captured_logs.text

    @pytest.mark.parametrize("failure", FAILURES)
    @pytest.mark.parametrize("relist", [None, "vm-a\n"],
                             ids=["fails", "names without UUIDs"])
    def test_a_listing_that_cannot_tell_fails_the_scan(self, failure, relist):
        in_use, _ = self._run([relist], **self.FAILURES[failure])

        assert in_use is None

    def test_a_domain_list_without_uuids_fails_the_scan(self, captured_logs):
        """The UUIDs are what tell a renamed domain from a deleted one."""
        in_use, _ = self._run([], first="vm-a\nvm-b\n")

        assert in_use is None
        assert "could not read the domain list" in captured_logs.text

    def test_each_failure_lists_anew(self):
        """vm-b is still listed when vm-a is found gone, and goes away
        before its own query: a listing older than a failure cannot tell."""
        in_use, calls = self._run(
            [("vm-b",), ()], failing={("domblklist", "vm-a"),
                                      ("domblklist", "vm-b")})

        assert in_use == {}
        assert sum(c[:2] == ("list", "--all") for c in calls) == 3

    def test_a_domain_read_whole_is_kept_as_it_was(self):
        """No listing is taken when nothing fails."""
        in_use, calls = self._run([])

        assert in_use == dict(self.A_ONLY, **{"/ws/b.qcow2": "vm-b"})
        assert sum(c[:2] == ("list", "--all") for c in calls) == 1

    def test_a_file_two_domains_use_is_named_for_the_first(self, tmp_path):
        """What each domain uses is gathered on its own, then added to what
        the domains before it use: a file both use keeps the first one's
        name, by path and by identity alike."""
        base = tmp_path / "base.qcow2"
        base.write_bytes(b"")
        self.CHAINS = {source: (f'[{{"filename": "{source}", '
                                f'"full-backing-filename": "{base}"}}, '
                                f'{{"filename": "{base}"}}]')
                       for source in ("/ws/a.qcow2", "/ws/b.qcow2")}

        in_use, _ = self._run([])

        st = os.stat(base)
        assert in_use[os.path.realpath(base)] == "vm-a"
        assert in_use.identities[(st.st_dev, st.st_ino)] == "vm-a"


class TestListedDomains:
    """virsh list --all --uuid --name as the in-use scan reads it: a
    '<uuid> <name>' line each, and a blank line to end, as libvirt 10
    prints it; blanks around a name, as a padded column would add, are not
    part of it (#213 review round 8, 1)."""

    U1 = "f31f8d03-3f19-4853-ab40-477fd927d42f"
    U2 = "54c8095d-b626-4cbf-a728-157b24f16c94"

    @staticmethod
    def _read(output):
        from boxman.providers.libvirt.session import _listed_domains
        return _listed_domains(output)

    def test_each_name_and_uuid_in_the_order_listed(self):
        output = (f"{self.U1} vm-a{' ' * 26}\n"
                  f"{self.U2} a-name-longer-than-thirty-columns\n"
                  "\n")

        assert self._read(output) == [
            ("vm-a", self.U1), ("a-name-longer-than-thirty-columns", self.U2)]

    def test_a_name_with_a_space_is_one_name(self):
        assert self._read(f"{self.U1} my vm{' ' * 25}\n") == [
            ("my vm", self.U1)]

    def test_a_wider_gap_before_the_name_is_not_part_of_it(self):
        assert self._read(f"{self.U1}   vm-a\n") == [("vm-a", self.U1)]

    def test_a_uuid_is_read_in_lower_case(self):
        assert self._read(f"{self.U1.upper()} vm-a\n") == [("vm-a", self.U1)]

    def test_no_domains(self):
        assert self._read("\n") == []

    @pytest.mark.parametrize("line", [
        "vm-a", "vm a", U1, f"{U1[:-1]} vm-a", f"{U1}x vm-a"],
        ids=["a name", "a name with a space", "a uuid", "a short uuid",
             "a long uuid"])
    def test_a_line_that_is_not_a_uuid_and_a_name(self, line):
        assert self._read(f"{self.U2} vm-b\n{line}\n") is None


class TestAbsenceProbe:
    """How a source is confirmed absent (#208 review round 3, 4): through
    the command wrapper qemu-img runs through, never with sudo, and only on
    the probe's own positive answer."""

    def _commands(self, provider, probe_says=_ABSENT, probe_ok=True):
        seen = []

        def run(command, **kwargs):
            seen.append(command)
            if _ABSENT in command:
                return _result(stdout=probe_says, ok=probe_ok)
            return _result(ok=False, stderr="Could not open")

        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=run):
            chains = _session(provider).backing_chains(["/ws/seed.iso"])
        return chains, seen

    def test_it_never_adds_sudo_even_where_qemu_img_gets_it(self):
        chains, (qemu_img, probe) = self._commands({"use_sudo": True})
        assert chains == {}
        assert qemu_img.startswith("sudo qemu-img ")
        assert not probe.startswith("sudo")

    def test_it_runs_where_qemu_img_runs(self):
        chains, commands = self._commands({
            "runtime": "docker-compose", "runtime_container": "rt"})
        assert chains == {}
        # qemu-img, the probe asking whether it may not be read (#221), and
        # the absence probe
        assert len(commands) == 3
        for command in commands:
            assert command.startswith("docker exec --user root rt bash -c ")

    @pytest.mark.parametrize("says, ok", [("", True), (_ABSENT, False),
                                          ("something else", True)])
    def test_anything_but_its_answer_is_not_absence(self, says, ok):
        chains, _ = self._commands({}, probe_says=says, probe_ok=ok)
        assert chains is None


@pytest.mark.skipif(shutil.which("qemu-img") is None, reason="needs qemu-img")
class TestSourcesGoneFromTheHost:
    """A source that does not exist has no backing chain -- nothing below
    it to protect -- while an existing image that cannot be read still
    fails closed (#208 review round 3, 4). Real qemu-img, real files, the
    local runtime."""

    @staticmethod
    def _image(path):
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(path),
                        "1M"], check=True)
        return str(path)

    @staticmethod
    def _unless_root():
        if os.geteuid() == 0:
            pytest.skip("root reads past mode 000")

    def test_a_missing_source_is_left_out(self, tmp_path):
        disk = self._image(tmp_path / "a.qcow2")
        gone = str(tmp_path / "seed.iso")
        assert _session({}).backing_chains([disk, gone]) == {disk: [disk]}

    def test_a_source_whose_directory_is_gone_too_is_left_out(self, tmp_path):
        gone = str(tmp_path / "gone" / "deeper" / "seed.iso")
        assert _session({}).backing_chains([gone]) == {}

    def test_an_existing_image_that_cannot_be_read_fails_closed(self, tmp_path):
        """The real probe, in a real shell, finds that the user may not
        read it, so libvirt is asked (#221) -- which lists no volume at
        that path, as for any file in no storage pool."""
        self._unless_root()
        disk = self._image(tmp_path / "a.qcow2")
        os.chmod(disk, 0)
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh:
            virsh.return_value.execute.return_value = _result(
                ok=False, stderr="error: Storage volume not found")
            assert _session({}).backing_chains([disk]) is None
        assert [c.args for c in virsh.return_value.execute.call_args_list] == [
            ("vol-pool", disk)]

    def test_a_readable_image_qemu_img_cannot_open_is_not_taken_to_libvirt(
            self, tmp_path):
        """The real probe finds it readable: whatever stopped qemu-img, it
        was not the user's permission to read it (#221)."""
        disk = tmp_path / "a.qcow2"
        disk.write_bytes(b"QFI\xfb" + b"\xff" * 508)
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh:
            assert _session({}).backing_chains([str(disk)]) is None
        virsh.return_value.execute.assert_not_called()

    def test_a_missing_source_is_not_taken_to_libvirt(self, tmp_path):
        """The real probe: what is not there cannot be unreadable."""
        gone = str(tmp_path / "seed.iso")
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh:
            assert _session({}).backing_chains([gone]) == {}
        virsh.return_value.execute.assert_not_called()

    def test_a_dangling_symlink_is_not_taken_for_absent(self, tmp_path):
        link = tmp_path / "seed.iso"
        link.symlink_to(tmp_path / "nowhere.iso")
        assert _session({}).backing_chains([str(link)]) is None

    def test_a_directory_that_cannot_be_searched_proves_nothing(self, tmp_path):
        self._unless_root()
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            assert _session({}).backing_chains([str(locked / "seed.iso")]) is None
        finally:
            locked.chmod(0o700)

    @pytest.mark.parametrize("path", ["seed.iso", "{tmp}/x/../seed.iso"])
    def test_a_path_not_absolute_and_normal_proves_nothing(self, tmp_path, path):
        assert _session({}).backing_chains(
            [path.format(tmp=tmp_path)]) is None

    @pytest.mark.parametrize("state, live, scanned", [
        ("shut off", "", True),
        ("running", "<domain><devices><disk type='file' device='cdrom'>"
                    "<source file='{gone}' index='3'/><backingStore/>"
                    "<target dev='sda'/></disk></devices></domain>", True),
        ("running", "<domain><devices><disk type='file' device='cdrom'>"
                    "<source file='{gone}' index='3'/>"
                    "<target dev='sda'/></disk></devices></domain>", False),
    ], ids=["shut off", "running, empty chain", "running, no chain"])
    def test_a_domain_whose_cdrom_is_gone(self, tmp_path, state, live,
                                          scanned):
        """... is scanned without it once it is shut off, or while it runs
        with a live chain that holds nothing (#208 review round 4, 2)."""
        disk = self._image(tmp_path / "b.qcow2")
        gone = str(tmp_path / "seed.iso")
        blk = (TestDiskPathsInUse.HEADER
               + f" file   disk     vda      {disk}\n"
               + f" file   cdrom    sda      {gone}\n")

        def virsh_execute(*args, **kwargs):
            if args[0] == "list":
                return _result(stdout=domain_listing(args, "vm-b")
                               if "--all" in args or state != "shut off"
                               else "")
            if args[0] == "domstate":
                return _result(stdout=f"{state}\n")
            if args[0] == "dumpxml":
                return _result(stdout=live.format(gone=gone))
            return _result(stdout=blk)

        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh:
            virsh.return_value.execute.side_effect = virsh_execute
            in_use = _session({}).disk_paths_in_use()

        assert in_use == ({os.path.realpath(disk): "vm-b"} if scanned
                          else None)

    def test_a_domain_whose_disk_cannot_be_read_fails_the_scan(self, tmp_path):
        self._unless_root()
        disk = self._image(tmp_path / "b.qcow2")
        os.chmod(disk, 0)
        blk = TestDiskPathsInUse.HEADER + f" file   disk     vda      {disk}\n"

        def virsh_execute(*args, **kwargs):
            if args[0] == "list":
                return _result(stdout=domain_listing(args, "vm-b")
                               if "--all" in args else "")
            if args[0] == "vol-pool":
                # what libvirt answers for a file in no storage pool (#221)
                return _result(ok=False, stderr="error: Storage volume not "
                                                "found")
            return _result(stdout=blk)

        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh:
            virsh.return_value.execute.side_effect = virsh_execute
            assert _session({}).disk_paths_in_use() is None


# ---------------------------------------------------------------------------
# #221 — a chain the user may not read is read through libvirt
# ---------------------------------------------------------------------------

#: a cluster workdir, which virt-clone makes libvirt pool ``cluster_1``
POOL = "/ws/cluster_1"
TOP = f"{POOL}/vm01.qcow2"
BASE = f"{POOL}/base.qcow2"
#: what reads a chain through libvirt
VIRSH_READS = ("vol-pool", "pool-refresh", "vol-dumpxml")


def _pool_host(*images: tuple[str, dict]) -> FakeHost:
    """A host whose pool ``cluster_1`` lists *images* -- ``(path, fields)``
    -- as provision leaves clone disks: 0600, which the user cannot read."""
    host = FakeHost()
    for path, fields in images:
        host.add(path, **fields)
    host.define_pool("cluster_1", POOL)
    return host


def _overlay_host() -> FakeHost:
    """TOP, an overlay on BASE, both unreadable pool volumes."""
    return _pool_host((TOP, {"backing": BASE}), (BASE, {}))


def _chains(host: FakeHost, sources: list[str],
            provider: dict | None = None):
    """``backing_chains(sources)`` with every command answered by *host*."""
    with patch("boxman.providers.libvirt.commands._shell_run",
               side_effect=host.run):
        return _session(provider).backing_chains(sources)


def _libvirt_asked(host: FakeHost) -> list[tuple[str, tuple]]:
    """What libvirt was asked to read a chain, in order."""
    return [(what, args) for _, what, args in host.log if what in VIRSH_READS]


class TestChainReadThroughLibvirt:
    """With ``use_sudo: false`` -- the default -- qemu-img runs as the user,
    who may not read the 0600 pool volumes libvirt makes every clone disk.
    Such a chain is read through libvirt instead, its pool refreshed first,
    and whatever libvirt cannot describe still fails closed (#221)."""

    def test_an_unreadable_disk_is_read_through_libvirt(self):
        host = _pool_host((TOP, {}))

        assert _chains(host, [TOP]) == {TOP: [TOP]}
        assert host.order() == ["qemu-img", "unreadable-probe",
                                *VIRSH_READS]
        assert host.asked("vol-pool") == [(TOP,)]
        assert host.asked("pool-refresh") == [("cluster_1",)]
        # the probe asks as whom qemu-img ran: the user, never sudo
        assert host.identities("unreadable-probe") == {"user"}

    def test_an_overlay_is_followed_to_the_bottom(self):
        host = _overlay_host()

        assert _chains(host, [TOP]) == {TOP: [TOP, BASE]}
        assert host.asked("vol-dumpxml") == [(TOP,), (BASE,)]

    def test_the_pool_is_refreshed_before_a_layer_is_read(self):
        """libvirt's answer is the pool's last refresh: rebased out of band,
        an image still shows its old backing file until the pool is
        refreshed (checked on libvirt 10.0)."""
        new = f"{POOL}/new-base.qcow2"
        host = _overlay_host()
        host.add(new)
        host.refresh("cluster_1")
        host.files[TOP].backing = new       # qemu-img rebase -u, as root

        assert _chains(host, [TOP]) == {TOP: [TOP, new]}
        order = host.order()
        assert order.index("pool-refresh") < order.index("vol-dumpxml")

    def test_each_pool_is_refreshed_once_per_read(self):
        other = f"{POOL}/vm02.qcow2"
        host = _overlay_host()
        host.add(other, backing=BASE)
        host.refresh("cluster_1")

        assert _chains(host, [TOP, other]) == {TOP: [TOP, BASE],
                                               other: [other, BASE]}
        assert host.asked("pool-refresh") == [("cluster_1",)]

    def test_a_teardown_scope_refreshes_each_pool_once_across_reads(self):
        """A teardown's retry reads what is left of a chain layer by layer,
        in several calls: one refresh serves them all -- while a read
        outside a scope refreshes for itself."""
        host = _overlay_host()
        session = _session({})
        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=host.run):
            with session.chain_read_scope():
                assert session.backing_chains([TOP]) == {TOP: [TOP, BASE]}
                assert session.backing_chains([BASE]) == {BASE: [BASE]}
            assert host.asked("pool-refresh") == [("cluster_1",)]
            assert session.backing_chains([BASE]) == {BASE: [BASE]}
        assert host.asked("pool-refresh") == [("cluster_1",)] * 2

    # -- no fallback -----------------------------------------------------------

    def test_a_readable_image_qemu_img_cannot_open_stays_unread(self):
        """Not a permission problem, so libvirt is not asked: its reading of
        a damaged header need not agree with qemu-img's."""
        host = _pool_host((TOP, {"readable": True, "corrupt": True}))

        assert _chains(host, [TOP]) is None
        assert host.order() == ["qemu-img", "unreadable-probe",
                                "absence-probe"]

    def test_a_readable_head_on_an_unreadable_base_stays_unread(self):
        """The probe asks about the source itself: it can be read, so
        whatever qemu-img could not open below it is not the user's reading
        of it."""
        host = _pool_host((TOP, {"backing": BASE, "readable": True}),
                          (BASE, {}))

        assert _chains(host, [TOP]) is None
        assert host.order() == ["qemu-img", "unreadable-probe",
                                "absence-probe"]

    def test_a_qemu_img_that_ran_with_sudo_is_never_second_guessed(self):
        """Run as root, qemu-img could read the file: its failure is not
        the user's, whatever the user's probe would say -- here sudo wants a
        password."""
        host = _pool_host((TOP, {}))
        host.sudo_refused = True

        assert _chains(host, [TOP], {"use_sudo": True}) is None
        assert "unreadable-probe" not in host.order()
        assert not set(VIRSH_READS) & set(host.order())

    def test_a_source_that_is_gone_is_left_out_without_libvirt(self):
        host = _pool_host()

        assert _chains(host, [TOP]) == {}
        assert host.order() == ["qemu-img", "unreadable-probe",
                                "absence-probe"]

    # -- what libvirt cannot describe fails closed ------------------------------

    @staticmethod
    def _xml(host: FakeHost, path: str) -> str:
        return host._vol_dumpxml((path,)).stdout

    #: what reading TOP -> BASE through libvirt asks, in order
    READ = [("vol-pool", (TOP,)), ("pool-refresh", ("cluster_1",)),
            ("vol-dumpxml", (TOP,)), ("vol-pool", (BASE,)),
            ("vol-dumpxml", (BASE,))]

    @pytest.mark.parametrize("breakage, asked", [
        ("the source is no pool volume", 1),
        ("vol-pool fails", 1),
        ("vol-pool answers, then fails", 1),
        ("vol-pool prints no one pool", 1),
        ("the refresh fails", 2),
        ("the refresh answers, then fails", 2),
        ("vol-dumpxml fails", 3),
        ("vol-dumpxml answers, then fails", 3),
        ("the XML does not parse", 3),
        ("the XML is not a volume", 3),
        ("the XML describes another path", 3),
        ("the XML names no format", 3),
        ("the backing store names no file", 3),
    ])
    def test_what_libvirt_cannot_describe_fails_closed(self, breakage,
                                                       asked):
        """... and nothing after the failure is asked."""
        host = _overlay_host()
        if breakage == "the source is no pool volume":
            del host.listed["cluster_1"][TOP]
        elif breakage == "vol-pool fails":
            host.fail.add("vol-pool")
        elif breakage == "vol-pool answers, then fails":
            host.fail_after_answering.add("vol-pool")
        elif breakage == "vol-pool prints no one pool":
            host.vol_pool_override[TOP] = "cluster_1\ncluster_2\n\n"
        elif breakage == "the refresh fails":
            host.fail.add("pool-refresh")
        elif breakage == "the refresh answers, then fails":
            host.fail_after_answering.add("pool-refresh")
        elif breakage == "vol-dumpxml fails":
            host.fail.add("vol-dumpxml")
        elif breakage == "vol-dumpxml answers, then fails":
            host.fail_after_answering.add("vol-dumpxml")
        elif breakage == "the XML does not parse":
            host.dumpxml_override[TOP] = "<volume type='file'><name>"
        elif breakage == "the XML is not a volume":
            host.dumpxml_override[TOP] = self._xml(host, TOP).replace(
                "volume", "pool")
        elif breakage == "the XML describes another path":
            host.dumpxml_override[TOP] = self._xml(host, TOP).replace(
                f"<path>{TOP}</path>", f"<path>{POOL}/other.qcow2</path>")
        elif breakage == "the XML names no format":
            host.dumpxml_override[TOP] = self._xml(host, TOP).replace(
                "<format type='qcow2'/>", "", 1)
        elif breakage == "the backing store names no file":
            host.dumpxml_override[TOP] = self._xml(host, TOP).replace(
                f"<path>{BASE}</path>", "")

        assert _chains(host, [TOP]) is None
        assert _libvirt_asked(host) == self.READ[:asked]

    def test_a_layer_below_that_is_no_pool_volume_fails_closed(self):
        host = _overlay_host()
        outside = "/tpl/base.qcow2"
        del host.files[BASE]
        host.add(outside)
        host.files[TOP].backing = outside
        host.refresh("cluster_1")

        assert _chains(host, [TOP]) is None
        assert _libvirt_asked(host) == [*self.READ[:3],
                                        ("vol-pool", (outside,))]

    def test_a_cycle_fails_closed_at_the_first_repeat(self):
        host = _overlay_host()
        host.files[BASE].backing = TOP
        host.refresh("cluster_1")

        assert _chains(host, [TOP]) is None
        assert _libvirt_asked(host) == self.READ

    @pytest.mark.parametrize("says, ok, fallback", [
        (None, True, True),
        ("", True, False),
        (None, False, False),
        ("something else", True, False),
    ], ids=["its answer", "silence", "its answer, failed",
            "another answer"])
    def test_only_the_probes_own_answer_takes_it_to_libvirt(self, says, ok,
                                                           fallback):
        from boxman.providers.libvirt.session import _UNREADABLE

        host = _overlay_host()
        run = host.run

        def shell(command, **kwargs):
            if command.startswith("if [ -e") and "-r" in command:
                run(command)
                return FakeResult(
                    stdout=(_UNREADABLE if says is None else says) + "\n",
                    code=0 if ok else 1)
            return run(command)

        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=shell):
            chains = _session({}).backing_chains([TOP])
        assert chains == ({TOP: [TOP, BASE]} if fallback else None)
        assert bool(_libvirt_asked(host)) is fallback

    def test_a_chain_deeper_than_the_limit_fails_closed(self):
        from boxman.providers.libvirt.session import _MAX_CHAIN_DEPTH

        layers = [f"{POOL}/layer{i}.qcow2" for i in range(_MAX_CHAIN_DEPTH + 1)]

        def host_of(chain):
            return _pool_host(*((path, {"backing": below})
                                for path, below in zip(
                                    chain, [*chain[1:], None], strict=True)))

        deepest = layers[:_MAX_CHAIN_DEPTH]
        assert _chains(host_of(deepest), [deepest[0]]) == {
            deepest[0]: deepest}
        assert _chains(host_of(layers), [layers[0]]) is None

    # -- through either runtime's command wrappers --------------------------------

    def test_under_the_docker_runtime_all_of_it_runs_in_the_container(self):
        """A root there that may not read a file (a user namespace) takes
        the same route, and every command -- qemu-img, the probe, virsh --
        runs inside the runtime container."""
        host = _overlay_host()
        host.root_reads = False

        assert _chains(host, [TOP], {"runtime": "docker-compose",
                                     "runtime_container": "rt"}) == {
            TOP: [TOP, BASE]}
        assert host.commands and all(
            command.startswith("docker exec --user root rt bash -c ")
            for command in host.commands)

    def test_qemu_img_exempt_from_sudo_falls_back_with_virsh_as_configured(
            self):
        """``use_sudo: true`` with qemu-img in ``sudo_skip_commands``: the
        user's qemu-img cannot read the disk, the probe runs as the user
        too, and virsh keeps its sudo."""
        host = _overlay_host()

        assert _chains(host, [TOP], {"use_sudo": True,
                                     "sudo_skip_commands": ["qemu-img"]}) == {
            TOP: [TOP, BASE]}
        assert host.identities("qemu-img") == {"user"}
        assert host.identities("unreadable-probe") == {"user"}
        for what in VIRSH_READS:
            assert host.identities(what) == {"root"}


class TestInUseScanThroughLibvirt:
    """The host-wide in-use scan every teardown asks reads each domain's
    chains the same way: a sibling's 0600 clone disk no longer fails it
    (#221), so a parallel deprovision can tell what the others use."""

    B = "/tpl/b.img"
    ISO = "/iso/tools.iso"

    def _host(self) -> FakeHost:
        host = _overlay_host()
        host.add(self.B, readable=True)
        host.add(self.ISO, format="raw", readable=True)
        host.define_domain("vm-a", ("file", "disk", "vda", TOP),
                           running=True)
        host.define_domain("vm-b", ("file", "disk", "vda", self.B),
                           ("file", "cdrom", "sda", self.ISO))
        return host

    @staticmethod
    def _scan(host: FakeHost):
        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=host.run):
            return _session({}).disk_paths_in_use()

    def test_an_unreadable_disk_and_its_base_are_mapped(self):
        in_use = self._scan(self._host())

        assert in_use == {TOP: "vm-a", BASE: "vm-a",
                          self.B: "vm-b", self.ISO: "vm-b"}

    def test_each_pool_is_refreshed_once_per_scan(self):
        host = self._host()
        other = f"{POOL}/vm03.qcow2"
        host.add(other, backing=BASE)
        host.refresh("cluster_1")
        host.define_domain("vm-c", ("file", "disk", "vda", other))

        assert self._scan(host)[other] == "vm-c"
        assert host.asked("pool-refresh") == [("cluster_1",)]
        self._scan(host)
        assert host.asked("pool-refresh") == [("cluster_1",)] * 2

    def test_a_disk_libvirt_cannot_describe_fails_the_scan(self):
        host = self._host()
        del host.listed["cluster_1"][TOP]

        assert self._scan(host) is None
        assert host.asked("vol-pool") == [(TOP,)]
