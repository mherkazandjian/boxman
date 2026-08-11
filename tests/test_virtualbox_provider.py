"""
Unit tests for the Phase 1 VirtualBox provider skeleton.

Phase 1 is skeleton / consolidation: the provider is wired, satisfies the
:class:`~boxman.abstract.providers.ProviderSession` protocol, and has a working
``VBoxManage`` command runner — but every per-VM/network/snapshot operation is
still a ``NotImplementedError`` stub. These tests pin that contract:

* the session satisfies the protocol (structural isinstance);
* construction is side-effect free (no shell-out — the core legacy bug);
* the command runner builds the expected string and *captures* output (the
  other core legacy bug — output capture was commented out);
* the runtime guard rejects any non-``local`` runtime;
* the stubbed methods raise ``NotImplementedError`` (red-to-green target).

All external calls are mocked — no VBoxManage is invoked on any host.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from boxman.abstract.providers import ProviderSession
from boxman.exceptions import ConfigError
from boxman.providers.virtualbox import VirtualBoxSession as VirtualBoxSessionExport
from boxman.providers.virtualbox.commands import VBoxManageCommand
from boxman.providers.virtualbox.session import VirtualBoxSession


pytestmark = pytest.mark.unit


# canned VBoxManage fixtures -------------------------------------------------

LIST_VMS_FIXTURE = (
    '"ubuntu-20.04-vanilla" {8234b7cf-fc60-48ea-96e7-67aed359cca8}\n'
    '"centos8-minimal-base" {64e766ca-6630-4bfb-9aa9-c6b4c4c6c899}\n'
)

SHOWVMINFO_FIXTURE = (
    'name="myvm"\n'
    'UUID="79992a4d-7f9f-4557-9054-f5c9ac44538a"\n'
    'VMState="running"\n'
    'memory=2048\n'
)


def _completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["VBoxManage"], returncode=returncode, stdout=stdout, stderr="")


# --- protocol ---------------------------------------------------------------

class TestProtocol:

    def test_session_satisfies_provider_session_protocol(self):
        session = VirtualBoxSession(config={"provider": {"virtualbox": {}}})
        assert isinstance(session, ProviderSession)

    def test_package_export_is_the_real_class(self):
        # __init__.py must export the real (non-empty) class
        assert VirtualBoxSessionExport is VirtualBoxSession
        assert isinstance(
            VirtualBoxSessionExport(config={}), ProviderSession)


# --- side-effect-free construction ------------------------------------------

class TestConstructionSideEffectFree:

    def test_init_does_not_shell_out(self):
        """The legacy __init__ eagerly ran ``vboxmanage natnetwork list``.
        The rewritten one must touch nothing external."""
        with patch("subprocess.run") as mock_run, \
                patch("subprocess.Popen") as mock_popen:
            VirtualBoxSession(config={"provider": {"virtualbox": {}}})
            mock_run.assert_not_called()
            mock_popen.assert_not_called()

    def test_command_construction_does_not_shell_out(self):
        with patch(
                "boxman.providers.virtualbox.commands.subprocess.run") as mock_run:
            VBoxManageCommand(provider_config={})
            mock_run.assert_not_called()

    def test_init_accepts_none_config(self):
        session = VirtualBoxSession()
        assert session.provider_config == {}
        assert isinstance(session, ProviderSession)


# --- config surface ---------------------------------------------------------

class TestConfigSurface:

    def test_project_provider_config_wins(self):
        session = VirtualBoxSession(
            config={"provider": {"virtualbox": {"use_sudo": True}}})
        # app-level default should not override the project-level value
        session.update_provider_config({"use_sudo": False, "vboxmanage_cmd": "vbm"})
        assert session.use_sudo is True
        assert session.provider_config["vboxmanage_cmd"] == "vbm"

    def test_uri_defaults_to_empty_string(self):
        session = VirtualBoxSession(config={})
        assert session.uri == ""
        session.uri = "something"
        assert session.uri == "something"

    def test_use_sudo_setter(self):
        session = VirtualBoxSession(config={})
        assert session.use_sudo is False
        session.use_sudo = True
        assert session.use_sudo is True

    def test_update_provider_config_with_runtime_is_noop(self):
        session = VirtualBoxSession(
            config={"provider": {"virtualbox": {"vboxmanage_cmd": "vbm"}}})
        before = session.provider_config
        assert session.update_provider_config_with_runtime() is None
        assert session.provider_config == before


# --- command runner ---------------------------------------------------------

class TestVBoxManageCommand:

    def test_build_command_default_binary(self):
        cmd = VBoxManageCommand(provider_config={})
        assert cmd.build_command("list", "vms") == "VBoxManage list vms"

    def test_build_command_with_sudo(self):
        cmd = VBoxManageCommand(provider_config={"use_sudo": True})
        assert cmd.build_command("list", "vms") == "sudo VBoxManage list vms"

    def test_build_command_custom_binary(self):
        cmd = VBoxManageCommand(provider_config={"vboxmanage_cmd": "vboxmanage"})
        assert cmd.build_command("list", "vms") == "vboxmanage list vms"

    def test_build_command_kwargs_rendering(self):
        cmd = VBoxManageCommand(provider_config={})
        built = cmd.build_command(
            "clonevm", "src", name="dst", register=True, uuid=None, live=False)
        # --name dst present, --register bare, uuid/live skipped
        assert built == "VBoxManage clonevm src --name dst --register"

    def test_override_use_sudo(self):
        cmd = VBoxManageCommand(provider_config={"use_sudo": True}, override_use_sudo=False)
        assert cmd.build_command("list", "vms") == "VBoxManage list vms"

    def test_run_captures_output(self):
        """The single most important fix vs the legacy runner: stdout is
        actually populated (legacy left it None)."""
        cmd = VBoxManageCommand(provider_config={})
        with patch(
                "boxman.providers.virtualbox.commands.subprocess.run",
                return_value=_completed(stdout=LIST_VMS_FIXTURE)) as mock_run:
            result = cmd.run("list", "vms")

        assert result.stdout == LIST_VMS_FIXTURE
        assert result.stdout is not None
        # capture must be requested explicitly
        _, kwargs = mock_run.call_args
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        # command was split into argv
        args, _ = mock_run.call_args
        assert args[0] == ["VBoxManage", "list", "vms"]

    def test_run_check_raises_on_nonzero(self):
        cmd = VBoxManageCommand(provider_config={})
        with patch(
                "boxman.providers.virtualbox.commands.subprocess.run",
                return_value=_completed(stdout="", returncode=1)):
            with pytest.raises(RuntimeError):
                cmd.run("startvm", "nope", check=True)

    def test_list_vms_parses_canned_fixture(self):
        cmd = VBoxManageCommand(provider_config={})
        with patch(
                "boxman.providers.virtualbox.commands.subprocess.run",
                return_value=_completed(stdout=LIST_VMS_FIXTURE)):
            vms = cmd.list_vms()
        assert vms == {
            "ubuntu-20.04-vanilla": "8234b7cf-fc60-48ea-96e7-67aed359cca8",
            "centos8-minimal-base": "64e766ca-6630-4bfb-9aa9-c6b4c4c6c899",
        }

    def test_parse_vms_handles_none(self):
        # the legacy failure mode (stdout None) must degrade gracefully
        assert VBoxManageCommand.parse_vms(None) == {}

    def test_parse_machinereadable(self):
        parsed = VBoxManageCommand.parse_machinereadable(SHOWVMINFO_FIXTURE)
        assert parsed["name"] == "myvm"
        assert parsed["VMState"] == "running"
        assert parsed["memory"] == "2048"

    def test_wrap_for_runtime_rejects_non_local(self):
        cmd = VBoxManageCommand(provider_config={"runtime": "docker-compose"})
        with pytest.raises(ValueError):
            cmd.run("list", "vms")


# --- runtime guard ----------------------------------------------------------

class TestRuntimeGuard:

    def test_local_runtime_is_allowed(self):
        from boxman.scripts.app import ensure_virtualbox_runtime_is_local
        assert ensure_virtualbox_runtime_is_local("local") is None

    @pytest.mark.parametrize("runtime", ["docker", "docker-compose", "remote"])
    def test_non_local_runtime_raises_config_error(self, runtime):
        from boxman.scripts.app import ensure_virtualbox_runtime_is_local
        with pytest.raises(ConfigError):
            ensure_virtualbox_runtime_is_local(runtime)


# --- stubbed operations -----------------------------------------------------

class TestStubbedOperations:

    def _session(self):
        return VirtualBoxSession(config={"provider": {"virtualbox": {}}})

    @pytest.mark.parametrize(
        "call",
        [
            lambda s: s.start_vm("vm"),
            lambda s: s.destroy_vm("vm"),
            lambda s: s.clone_vm("new", "src", {}, "/tmp"),
            lambda s: s.vm_exists("vm"),
            lambda s: s.define_network(name="net"),
            lambda s: s.destroy_network(name="net"),
            lambda s: s.remove_network(name="net"),
            lambda s: s.configure_vm_cpu_memory("vm"),
            lambda s: s.configure_vm_network_interfaces("vm", []),
            lambda s: s.configure_vm_disks("vm", [], "/tmp"),
            lambda s: s.configure_vm_cdroms("vm", []),
            lambda s: s.configure_vm_shared_folders("vm", []),
            lambda s: s.get_vm_ip_addresses("vm"),
            lambda s: s.suspend_vm("vm"),
            lambda s: s.resume_vm("vm"),
            lambda s: s.save_vm("vm", "/tmp"),
            lambda s: s.restore_vm("vm", "/tmp"),
            lambda s: s.destroy_disks("/tmp", "vm", []),
            lambda s: s.set_boot_order("vm", ["hd"]),
            lambda s: s.import_image("file://m", "vm", "/tmp"),
            lambda s: s.snapshot_take(vm_name="vm"),
            lambda s: s.snapshot_restore("vm"),
            lambda s: s.snapshot_delete("vm", "snap"),
            lambda s: s.snapshot_list("vm"),
        ],
    )
    def test_operations_raise_not_implemented(self, call):
        with pytest.raises(NotImplementedError) as exc:
            call(self._session())
        assert "VirtualBox provider" in str(exc.value)

    def test_storage_property_raises(self):
        with pytest.raises(NotImplementedError):
            _ = self._session().storage


# --- CLI dispatch -------------------------------------------------------------

class TestCliDispatch:
    """#85 item 28: a virtualbox stub dying mid-flow must surface as a clean
    exit 2 with a Phase 1 message, not a raw traceback."""

    def test_virtualbox_stub_exits_2_with_message(self):
        from boxman.scripts import app
        with patch("boxman.scripts.app._main",
                   side_effect=NotImplementedError(
                       "VirtualBox provider: start_vm lands in Phase 2")), \
                patch("boxman.scripts.app.log") as mock_log:
            with pytest.raises(SystemExit) as exc:
                app.main()
        assert exc.value.code == 2
        msg = mock_log.error.call_args.args[0]
        assert "start_vm lands in Phase 2" in msg
        assert "Phase 1" in msg
        assert "non-functional" in msg

    def test_unrelated_not_implemented_still_raises(self):
        """Only the virtualbox stubs are translated; an unrelated
        NotImplementedError keeps its traceback."""
        from boxman.scripts import app
        with patch("boxman.scripts.app._main",
                   side_effect=NotImplementedError("something else broke")):
            with pytest.raises(NotImplementedError, match="something else"):
                app.main()


# --- salvaged command builders ----------------------------------------------

class TestCommandBuilders:

    def test_clone_vm_builder(self):
        from boxman.providers.virtualbox.clone_vm import CloneVM
        built = CloneVM(provider_config={}).build_clone_command(
            src_vm_name="base", new_vm_name="node01")
        assert built == "VBoxManage clonevm base --mode all --name node01 --register"

    def test_natnetwork_add_builder_uses_dhcp_not_dchp(self):
        """The legacy builder had a dchp/dhcp NameError; assert the fixed one
        emits a valid --dhcp on argument."""
        from boxman.providers.virtualbox.net import NatNetwork
        built = NatNetwork(provider_config={}).build_add_command(
            network_name="labnet", network="10.0.1.0/24", enable=True)
        assert "natnetwork add" in built
        assert "--netname labnet" in built
        assert "--dhcp on" in built
        assert "--enable" in built

    def test_natnetwork_parse_list(self):
        from boxman.providers.virtualbox.net import NatNetwork
        fixture = (
            "NAT Networks:\n\n"
            "Name:        labnet\n"
            "Network:     10.0.1.0/24\n"
            "Gateway:     10.0.1.1\n"
            "IPv6:        No\n"
            "Enabled:     Yes\n\n"
            "1 network found\n"
        )
        parsed = NatNetwork.parse_list(fixture)
        assert parsed["labnet"]["network"] == "10.0.1.0/24"

    def test_snapshot_take_builder(self):
        from boxman.providers.virtualbox.snapshot import Snapshot
        built = Snapshot(provider_config={}).build_take_command(
            "node01", "state1", description="first", live=True)
        assert built == (
            "VBoxManage snapshot node01 take state1 "
            "--description first --live")

    def test_storage_createmedium_defaults_to_config_format(self):
        from boxman.providers.virtualbox.storage import Storage
        storage = Storage(provider_config={"default_medium_format": "VMDK"})
        built = storage.build_createmedium_command(filename="/tmp/d.vmdk", size_mb=1024)
        assert "--format VMDK" in built
        assert "--size 1024" in built

    def test_destroy_vm_unregister_builder(self):
        from boxman.providers.virtualbox.destroy_vm import DestroyVM
        built = DestroyVM(provider_config={}).build_unregister_command("node01")
        assert built == "VBoxManage unregistervm node01 --delete"

    def test_modifyvm_natpf_builder(self):
        from boxman.providers.virtualbox.modifyvm import ModifyVm
        built = ModifyVm(provider_config={}).build_natpf_command(
            "node01", host_port=2222, guest_port=22)
        assert '--natpf1' in built
        assert '"guestssh,tcp,,2222,,22"' in built
