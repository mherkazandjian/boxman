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
                return _result(stdout="vm-a\nvm-b\n", ok=list_ok)
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
                return _result(stdout="vm-a\nvm-b\n")
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

    def _run(self, state, state_ok=True, absent=("/ws/gone.qcow2",)):
        calls = []

        def virsh_execute(*args, **kwargs):
            calls.append(args[0])
            if args[0] == "list":
                return _result(stdout="vm-b\n")
            if args[0] == "domstate":
                assert args[1] == "vm-b"
                return _result(stdout=f"{state}\n", ok=state_ok)
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
    def test_an_active_domain_fails_the_scan_and_says_why(
            self, state, captured_logs):
        in_use, _ = self._run(state)
        assert in_use is None
        assert "vm-b" in captured_logs.text
        assert "/ws/gone.qcow2" in captured_logs.text

    def test_a_shut_off_domain_is_scanned_without_it(self):
        in_use, _ = self._run("shut off")
        assert in_use == {"/ws/b.qcow2": "vm-b"}

    def test_a_state_that_cannot_be_read_fails_the_scan(self, captured_logs):
        in_use, _ = self._run("shut off", state_ok=False)
        assert in_use is None
        assert "vm-b" in captured_logs.text

    def test_the_state_is_asked_only_once_the_absence_is_confirmed(self):
        _, calls = self._run("shut off")
        assert calls.count("domstate") == 1
        assert calls.index("domstate") > calls.index("probe /ws/gone.qcow2")

    def test_no_state_is_asked_while_absence_is_not_confirmed(self):
        in_use, calls = self._run("shut off", absent=())
        assert in_use is None
        assert "domstate" not in calls


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
                return _result(stdout="vm-b\n")
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
        chains, (qemu_img, probe) = self._commands({
            "runtime": "docker-compose", "runtime_container": "rt"})
        assert chains == {}
        for command in (qemu_img, probe):
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
        self._unless_root()
        disk = self._image(tmp_path / "a.qcow2")
        os.chmod(disk, 0)
        assert _session({}).backing_chains([disk]) is None

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

    @pytest.mark.parametrize("state, scanned", [("shut off", True),
                                                ("running", False)])
    def test_a_domain_whose_cdrom_is_gone(self, tmp_path, state, scanned):
        """... is scanned without it once it is shut off (#208 review
        round 4, 2: a running one may still hold the deleted file open)."""
        disk = self._image(tmp_path / "b.qcow2")
        gone = str(tmp_path / "seed.iso")
        blk = (TestDiskPathsInUse.HEADER
               + f" file   disk     vda      {disk}\n"
               + f" file   cdrom    sda      {gone}\n")

        def virsh_execute(*args, **kwargs):
            if args[0] == "list":
                return _result(stdout="vm-b\n")
            if args[0] == "domstate":
                return _result(stdout=f"{state}\n")
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
                return _result(stdout="vm-b\n")
            return _result(stdout=blk)

        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh:
            virsh.return_value.execute.side_effect = virsh_execute
            assert _session({}).disk_paths_in_use() is None
