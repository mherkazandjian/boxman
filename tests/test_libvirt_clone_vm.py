"""
Unit tests for boxman.providers.libvirt.clone_vm.CloneVM.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import invoke
import pytest

from boxman.exceptions import (
    CloneCleanupError,
    CloneSanitizerError,
    CloneSanitizerUnavailable,
    ConfigError,
)
from boxman.providers.libvirt.clone_vm import CloneVM
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


@pytest.fixture
def clone(tmp_path: Path) -> CloneVM:
    return CloneVM(
        src_vm_name="template-base",
        new_vm_name="vm01",
        info={"network_adapters": [{"network": "default"}]},
        workdir=str(tmp_path),
        provider_config={"use_sudo": False},
    )


class TestConstruction:

    def test_image_path_derived_from_workdir(self, tmp_path: Path):
        c = CloneVM(
            src_vm_name="base",
            new_vm_name="vm-new",
            info={},
            workdir=str(tmp_path),
            provider_config=None,
        )
        assert c.new_image_path == str(tmp_path / "vm-new.qcow2")

    def test_workdir_tilde_is_expanded(self):
        c = CloneVM(
            src_vm_name="base",
            new_vm_name="vm-new",
            info={},
            workdir="~/fake-boxman-workdir",
            provider_config=None,
        )
        assert "~" not in c.new_image_path
        assert c.new_image_path.endswith("/vm-new.qcow2")

    def test_bundled_docker_runtime_contains_virt_sysprep_package(self):
        dockerfile = (
            Path(__file__).resolve().parents[1] / "containers/docker/Dockerfile"
        ).read_text()
        assert "guestfs-tools" in dockerfile

    @pytest.mark.parametrize("policy", ["auto", "required", "off"])
    def test_accepts_machine_id_policies(self, tmp_path: Path, policy: str):
        c = CloneVM(
            src_vm_name="base", new_vm_name="vm-new",
            info={"clone_machine_id": policy}, workdir=str(tmp_path))
        assert c.machine_id_policy == policy

    @pytest.mark.parametrize("policy", ["strict", True, None, ["auto"]])
    def test_rejects_invalid_machine_id_policy(self, tmp_path: Path, policy):
        with pytest.raises(ConfigError, match="clone_machine_id"):
            CloneVM(
                src_vm_name="base", new_vm_name="vm-new",
                info={"clone_machine_id": policy}, workdir=str(tmp_path))

    @pytest.mark.parametrize("timeout", [0, -1, True, "300"])
    def test_rejects_invalid_sysprep_timeout(self, tmp_path: Path, timeout):
        with pytest.raises(ConfigError, match="virt_sysprep_timeout"):
            CloneVM(
                src_vm_name="base", new_vm_name="vm-new", info={},
                workdir=str(tmp_path),
                provider_config={"virt_sysprep_timeout": timeout})


class TestCreateClone:

    def test_success_path_calls_virt_clone_with_correct_args(self, clone: CloneVM):
        with patch.object(clone.virt_clone, "execute") as virt_clone_exec, \
             patch.object(clone, "reset_machine_identity", return_value=True), \
             patch.object(clone, "remove_network_interfaces", return_value=True):
            virt_clone_exec.return_value = _result()
            assert clone.create_clone() is True

        (_args, kwargs) = virt_clone_exec.call_args
        assert kwargs["original"] == "template-base"
        assert kwargs["name"] == "vm01"
        assert kwargs["file"].endswith("vm01.qcow2")
        assert kwargs["auto_clone"] is True

    def test_skips_iface_removal_when_no_network_adapters(self, tmp_path: Path):
        c = CloneVM(
            src_vm_name="base",
            new_vm_name="vmx",
            info={},  # no network_adapters
            workdir=str(tmp_path),
            provider_config=None,
        )
        with patch.object(c.virt_clone, "execute", return_value=_result()) as virt_clone, \
             patch.object(c, "reset_machine_identity", return_value=True), \
             patch.object(c, "remove_network_interfaces") as remove_ifaces:
            assert c.create_clone() is True
            virt_clone.assert_called_once()
            remove_ifaces.assert_not_called()

    def test_runtime_error_returns_false(self, clone: CloneVM):
        with patch.object(clone.virt_clone, "execute", side_effect=RuntimeError("boom")):
            assert clone.create_clone() is False

    def test_remove_ifaces_failure_logs_warning_but_still_succeeds(
        self, clone: CloneVM, captured_logs
    ):
        with patch.object(clone.virt_clone, "execute", return_value=_result()), \
             patch.object(clone, "reset_machine_identity", return_value=True), \
             patch.object(clone, "remove_network_interfaces", return_value=False):
            assert clone.create_clone() is True
        assert any(
            "failed to remove network interfaces" in rec.message
            for rec in captured_logs.records
        )

    def test_machine_identity_is_reset_before_interface_changes(self, clone: CloneVM):
        order = []
        with patch.object(
            clone.virt_clone, "execute",
            side_effect=lambda *args, **kwargs: order.append("clone"),
        ), patch.object(
            clone, "reset_machine_identity",
            side_effect=lambda: (order.append("sysprep"), True)[1],
        ), patch.object(
            clone, "remove_network_interfaces",
            side_effect=lambda: (order.append("interfaces"), True)[1],
        ):
            assert clone.create_clone() is True
        assert order == ["clone", "sysprep", "interfaces"]

    def test_auto_sanitizer_failure_warns_and_continues(
        self, clone: CloneVM, captured_logs
    ):
        error = CloneSanitizerError("unsupported guest")
        with patch.object(clone.virt_clone, "execute", return_value=_result()), \
             patch.object(clone, "reset_machine_identity", side_effect=error), \
             patch.object(clone, "discard_unsafe_clone") as discard, \
             patch.object(
                 clone, "remove_network_interfaces", return_value=True
             ) as remove_ifaces:
            assert clone.create_clone() is True
        discard.assert_not_called()
        remove_ifaces.assert_called_once_with()
        assert any(
            "clone_machine_id=auto" in rec.message
            and "unsupported guest" in rec.message
            for rec in captured_logs.records)

    def test_required_sanitizer_failure_discards_and_propagates(
        self, clone: CloneVM
    ):
        clone.machine_id_policy = "required"
        error = CloneSanitizerError("inspection failed")
        with patch.object(clone.virt_clone, "execute", return_value=_result()), \
             patch.object(
                 clone,
                 "reset_machine_identity",
                 side_effect=error,
             ), \
             patch.object(clone, "discard_unsafe_clone") as discard, \
             patch.object(clone, "remove_network_interfaces") as remove_ifaces:
            with pytest.raises(CloneSanitizerError, match="inspection failed"):
                clone.create_clone()
        discard.assert_called_once_with(error)
        remove_ifaces.assert_not_called()

    def test_required_cleanup_failure_is_terminal_with_both_causes(
        self, clone: CloneVM
    ):
        clone.machine_id_policy = "required"
        sanitizer = CloneSanitizerError("unsupported encrypted guest")
        with patch.object(clone.virt_clone, "execute", return_value=_result()), \
             patch.object(
                 clone, "reset_machine_identity", side_effect=sanitizer
             ), \
             patch.object(
                 clone.virsh, "execute",
                 return_value=_result(
                     ok=False, stderr="storage pool is busy", return_code=1)
             ), \
             patch.object(clone, "remove_network_interfaces") as remove_ifaces:
            with pytest.raises(CloneCleanupError) as caught:
                clone.create_clone()
        assert "unsupported encrypted guest" in str(caught.value)
        assert "storage pool is busy" in str(caught.value)
        assert caught.value.__cause__ is sanitizer
        remove_ifaces.assert_not_called()

    def test_off_skips_sanitizer(self, clone: CloneVM):
        clone.machine_id_policy = "off"
        with patch.object(clone.virt_clone, "execute", return_value=_result()), \
             patch.object(clone, "reset_machine_identity") as reset, \
             patch.object(clone, "remove_network_interfaces", return_value=True):
            assert clone.create_clone() is True
        reset.assert_not_called()


class TestResetMachineIdentity:

    def test_runs_only_machine_id_operation_on_the_clone(self, clone: CloneVM):
        with patch.object(
            clone.virt_sysprep, "execute", return_value=_result()
        ) as execute:
            assert clone.reset_machine_identity() is None
        execute.assert_called_once_with(
            domain="vm01", operations="machine-id", keys_from_stdin=True,
            warn=True, execution_timeout=300, timeout=315)

    def test_nonzero_exit_is_a_typed_failure(self, clone: CloneVM):
        with patch.object(
            clone.virt_sysprep,
            "execute",
            return_value=_result(
                ok=False, stderr="inspection failed", return_code=1),
        ):
            with pytest.raises(CloneSanitizerError, match="inspection failed"):
                clone.reset_machine_identity()

    def test_missing_tool_has_actionable_package_guidance(self, clone: CloneVM):
        with patch.object(
            clone.virt_sysprep,
            "execute",
            return_value=_result(
                ok=False,
                stderr="virt-sysprep: not found",
                return_code=127,
            ),
        ):
            with pytest.raises(CloneSanitizerUnavailable) as caught:
                clone.reset_machine_identity()
        assert "virt-sysprep" in str(caught.value)
        assert "guestfs-tools" in str(caught.value)

    def test_domain_not_found_is_not_misclassified_as_missing_tool(
        self, clone: CloneVM
    ):
        with patch.object(
            clone.virt_sysprep,
            "execute",
            return_value=_result(
                ok=False,
                stderr="virt-sysprep: domain 'vm01' not found",
                return_code=127,
            ),
        ):
            with pytest.raises(CloneSanitizerError) as caught:
                clone.reset_machine_identity()
        assert not isinstance(caught.value, CloneSanitizerUnavailable)
        assert "domain 'vm01' not found" in str(caught.value)

    def test_sudo_missing_tool_signature_is_classified_exactly(
        self, clone: CloneVM
    ):
        with patch.object(
            clone.virt_sysprep,
            "execute",
            return_value=_result(
                ok=False,
                stderr="sudo: virt-sysprep: command not found",
                return_code=1,
            ),
        ):
            with pytest.raises(CloneSanitizerUnavailable, match="not installed"):
                clone.reset_machine_identity()

    def test_noninteractive_sudo_denial_is_a_permanent_prerequisite_failure(
        self, clone: CloneVM
    ):
        with patch.object(
            clone.virt_sysprep,
            "execute",
            return_value=_result(
                ok=False,
                stderr=(
                    "sudo: a terminal is required to read the password\n"
                    "sudo: a password is required"
                ),
                return_code=1,
            ),
        ):
            with pytest.raises(CloneSanitizerUnavailable) as caught:
                clone.reset_machine_identity()
        assert "passwordless sudo" in str(caught.value)
        assert "use_sudo" in str(caught.value)

    def test_timeout_is_typed_and_bounded(self, clone: CloneVM):
        timed_out = invoke.exceptions.CommandTimedOut(
            invoke.runners.Result(command="virt-sysprep", exited=-1),
            timeout=300,
        )
        with patch.object(
            clone.virt_sysprep, "execute", side_effect=timed_out
        ) as execute:
            with pytest.raises(CloneSanitizerError, match="timed out after 300s"):
                clone.reset_machine_identity()
        assert execute.call_args.kwargs["execution_timeout"] == 300
        assert execute.call_args.kwargs["timeout"] == 315

    def test_inner_timeout_exit_is_typed(self, clone: CloneVM):
        with patch.object(
            clone.virt_sysprep,
            "execute",
            return_value=_result(ok=False, return_code=124),
        ):
            with pytest.raises(CloneSanitizerError, match="timed out after 300s"):
                clone.reset_machine_identity()


class TestDiscardUnsafeClone:

    def test_undefines_only_the_failed_clone_and_its_storage(self, clone: CloneVM):
        with patch.object(clone.virsh, "execute", return_value=_result()) as execute:
            clone.discard_unsafe_clone()
        execute.assert_called_once_with(
            "undefine", "vm01", "--remove-all-storage", warn=True)

    def test_cleanup_failure_is_terminal_and_preserves_sanitizer_cause(
        self, clone: CloneVM
    ):
        sanitizer = CloneSanitizerError("inspection failed")
        with patch.object(
            clone.virsh,
            "execute",
            return_value=_result(ok=False, stderr="domain is busy"),
        ):
            with pytest.raises(CloneCleanupError) as caught:
                clone.discard_unsafe_clone(sanitizer)
        assert "inspection failed" in str(caught.value)
        assert "domain is busy" in str(caught.value)
        assert "manual cleanup" in str(caught.value)
        assert caught.value.__cause__ is sanitizer
        assert caught.value.sanitizer_error is sanitizer
        assert caught.value.cleanup_error == "domain is busy"

    def test_cleanup_executor_exception_preserves_both_failures(
        self, clone: CloneVM
    ):
        sanitizer = CloneSanitizerError("inspection failed")
        cleanup = ConfigError("runtime container missing")
        with patch.object(clone.virsh, "execute", side_effect=cleanup):
            with pytest.raises(CloneCleanupError) as caught:
                clone.discard_unsafe_clone(sanitizer)
        assert "inspection failed" in str(caught.value)
        assert "runtime container missing" in str(caught.value)
        assert caught.value.sanitizer_error is sanitizer
        assert caught.value.cleanup_error is cleanup
        assert caught.value.__cause__ is cleanup


class TestRemoveNetworkInterfaces:

    DOMIFLIST_SAMPLE = (
        "Interface   Type       Source     Model       MAC\n"
        "------------------------------------------------------\n"
        "vnet0       network    default    virtio      52:54:00:aa:bb:cc\n"
        "vnet1       network    extra      virtio      52:54:00:aa:bb:dd\n"
    )

    def test_parses_and_detaches_each_interface(self, clone: CloneVM):
        calls: list[tuple[tuple, dict]] = []

        def fake_execute(*args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == "domiflist":
                return _result(stdout=self.DOMIFLIST_SAMPLE)
            # detach-interface returns ok
            return _result()

        with patch.object(clone.virsh, "execute", side_effect=fake_execute):
            assert clone.remove_network_interfaces() is True

        detaches = [
            args for args, _kwargs in calls if args and args[0] == "detach-interface"
        ]
        assert len(detaches) == 2
        macs = [a for args in detaches for a in args if str(a).startswith("--mac=")]
        assert "--mac=52:54:00:aa:bb:cc" in macs
        assert "--mac=52:54:00:aa:bb:dd" in macs

    def test_empty_interface_list_returns_true(self, clone: CloneVM):
        with patch.object(
            clone.virsh, "execute",
            return_value=_result(stdout="Interface   Type\n-----------------\n"),
        ):
            assert clone.remove_network_interfaces() is True

    def test_domiflist_failure_returns_false(self, clone: CloneVM, captured_logs):
        with patch.object(clone.virsh, "execute", return_value=_result(ok=False)):
            assert clone.remove_network_interfaces() is False

    def test_domiflist_called_with_warn_true(self, clone: CloneVM):
        """Issue #85 item 38: without warn=True a failed domiflist raises
        and the graceful 'return False' branch is dead code."""
        with patch.object(clone.virsh, "execute",
                          return_value=_result(ok=False)) as execute:
            assert clone.remove_network_interfaces() is False
        assert execute.call_args.kwargs.get("warn") is True

    def test_detach_failure_is_logged_but_loop_continues(
        self, clone: CloneVM, captured_logs
    ):
        """Individual failures warn, overall returns True (best-effort)."""
        def fake_execute(*args, **_kwargs):
            if args[0] == "domiflist":
                return _result(stdout=self.DOMIFLIST_SAMPLE)
            return _result(ok=False, stderr="detach failed")

        with patch.object(clone.virsh, "execute", side_effect=fake_execute):
            assert clone.remove_network_interfaces() is True

        assert any(
            "failed to remove interface" in rec.message for rec in captured_logs.records
        )

    def test_unexpected_exception_returns_false(self, clone: CloneVM, captured_logs):
        with patch.object(clone.virsh, "execute", side_effect=ValueError("weird")):
            assert clone.remove_network_interfaces() is False


class TestCloneWrapper:

    def test_returns_false_when_create_clone_fails(self, clone: CloneVM):
        with patch.object(clone, "create_clone", return_value=False):
            assert clone.clone() is False

    def test_returns_true_on_successful_create(self, clone: CloneVM):
        with patch.object(clone, "create_clone", return_value=True):
            assert clone.clone() is True


class TestCloneVmIsoBootDispatch:
    """clone_vm delegates to IsoBootVM when boot_order starts with 'cdrom'."""

    def _make_session(self):
        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {"uri": "qemu:///system", "use_sudo": False}
        return session

    @patch("boxman.providers.libvirt.session.IsoBootVM")
    def test_cdrom_boot_order_dispatches_to_iso_boot_vm(self, mock_iso_cls, tmp_path):
        mock_iso_cls.return_value.create.return_value = True
        session = self._make_session()
        info = {
            "boot_order": ["cdrom", "hd"],
            "_resolved_iso_path": "/cache/talos.iso",
        }
        result = session.clone_vm(
            new_vm_name="cp-01",
            src_vm_name=None,
            info=info,
            workdir=str(tmp_path),
        )
        assert result is True
        mock_iso_cls.assert_called_once_with(
            vm_name="cp-01",
            info=info,
            provider_config=session.provider_config,
            workdir=str(tmp_path),
            iso_path="/cache/talos.iso",
        )
        mock_iso_cls.return_value.create.assert_called_once()

    @patch("boxman.providers.libvirt.session.IsoBootVM")
    def test_raises_when_resolved_iso_path_missing(self, mock_iso_cls, tmp_path):
        session = self._make_session()
        info = {"boot_order": ["cdrom", "hd"]}
        with pytest.raises(RuntimeError, match="_resolved_iso_path"):
            session.clone_vm(
                new_vm_name="cp-01",
                src_vm_name=None,
                info=info,
                workdir=str(tmp_path),
            )

    @patch("boxman.providers.libvirt.session.IsoBootVM")
    def test_raises_when_iso_boot_vm_create_fails(self, mock_iso_cls, tmp_path):
        mock_iso_cls.return_value.create.return_value = False
        session = self._make_session()
        info = {"boot_order": ["cdrom", "hd"], "_resolved_iso_path": "/cache/talos.iso"}
        with pytest.raises(RuntimeError, match="Failed to create ISO-boot VM"):
            session.clone_vm(
                new_vm_name="cp-01",
                src_vm_name=None,
                info=info,
                workdir=str(tmp_path),
            )


class TestCloneVmMachineIdentityFailureChain:

    def test_session_propagates_real_clone_sanitizer_failure(self, tmp_path):
        session = LibVirtSession.__new__(LibVirtSession)
        session.provider_config = {
            "uri": "qemu:///system",
            "use_sudo": False,
        }

        with patch(
            "boxman.providers.libvirt.clone_vm.VirtCloneCommand.execute",
            return_value=_result(),
        ) as virt_clone, patch(
            "boxman.providers.libvirt.clone_vm.VirtSysprepCommand.execute",
            return_value=_result(
                ok=False,
                stderr="inspection failed",
                return_code=1,
            ),
        ) as virt_sysprep, patch(
            "boxman.providers.libvirt.clone_vm.VirshCommand.execute",
            return_value=_result(),
        ) as virsh:
            with pytest.raises(CloneSanitizerError, match="inspection failed"):
                session.clone_vm(
                    new_vm_name="vm01",
                    src_vm_name="template-base",
                    info={"clone_machine_id": "required"},
                    workdir=str(tmp_path),
                )

        virt_clone.assert_called_once()
        virt_sysprep.assert_called_once_with(
            domain="vm01", operations="machine-id", keys_from_stdin=True,
            warn=True, execution_timeout=300, timeout=315)
        virsh.assert_called_once_with(
            "undefine", "vm01", "--remove-all-storage", warn=True)
