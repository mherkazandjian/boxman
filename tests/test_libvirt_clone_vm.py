"""
Unit tests for boxman.providers.libvirt.clone_vm.CloneVM.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import invoke
import pytest

from boxman.exceptions import (
    CloneCleanupError,
    CloneSanitizerError,
    CloneSanitizerUnavailableError,
    ConfigError,
    ProvisionError,
)
from boxman.providers.libvirt.clone_vm import (
    CLONE_DEGRADATION_NOTICES_KEY,
    SSH_HOST_KEY_ARCHIVE,
    SSH_HOST_KEY_TYPES,
    CloneVM,
)
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


def _machine_id_only(clone: CloneVM):
    """A plan covering only the machine id.

    Deliberately narrow: it contributes no customizations, so it needs no
    staging and the error-classification tests below stay about
    virt-sysprep's exit status rather than about key generation.
    """
    clone.ssh_host_keys_policy = "off"
    return clone.build_identity_plan()


def _staging_leftovers(vm_name: str) -> list[Path]:
    """Staging directories this vm left behind in the temp dir."""
    return list(
        Path(tempfile.gettempdir()).glob(f"boxman-hostkeys-{vm_name}-*"))


def _vm(tmp_path: Path, **info) -> CloneVM:
    return CloneVM(
        src_vm_name="base", new_vm_name="vm-new", info=info,
        workdir=str(tmp_path))


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
             patch.object(clone, "run_identity_pass", return_value=True), \
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
             patch.object(c, "run_identity_pass", return_value=True), \
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
             patch.object(clone, "run_identity_pass", return_value=True), \
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
            clone, "run_identity_pass",
            side_effect=lambda plan: (order.append("sysprep"), True)[1],
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
             patch.object(clone, "run_identity_pass", side_effect=error), \
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
                 "run_identity_pass",
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
                 clone, "run_identity_pass", side_effect=sanitizer
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

    def test_every_policy_off_skips_the_pass(self, clone: CloneVM):
        clone.machine_id_policy = "off"
        clone.ssh_host_keys_policy = "off"
        with patch.object(clone.virt_clone, "execute", return_value=_result()), \
             patch.object(clone, "run_identity_pass") as reset, \
             patch.object(clone, "remove_network_interfaces", return_value=True):
            assert clone.create_clone() is True
        reset.assert_not_called()

    def test_machine_id_off_alone_still_runs_the_pass(self, clone: CloneVM):
        """``off`` on one property must not disable the others."""
        clone.machine_id_policy = "off"
        with patch.object(clone.virt_clone, "execute", return_value=_result()), \
             patch.object(clone, "run_identity_pass") as reset, \
             patch.object(clone, "remove_network_interfaces", return_value=True):
            assert clone.create_clone() is True
        reset.assert_called_once()


class TestRunIdentityPass:

    def test_machine_id_only_plan_runs_exactly_that_operation(
        self, clone: CloneVM
    ):
        with patch.object(
            clone.virt_sysprep, "execute", return_value=_result()
        ) as execute:
            assert clone.run_identity_pass(_machine_id_only(clone)) is None
        execute.assert_called_once_with(
            domain="vm01", operations="machine-id", keys_from_stdin=True,
            warn=True, execution_timeout=300, timeout=315)

    def test_default_plan_carries_host_keys_and_customize(
        self, clone: CloneVM
    ):
        with patch.object(
            clone.virt_sysprep, "execute", return_value=_result()
        ) as execute:
            clone.run_identity_pass(clone.build_identity_plan())

        kwargs = execute.call_args.kwargs
        assert kwargs["operations"] == "machine-id,ssh-hostkeys,customize"
        # the staged keys travel as one archive, passed positionally
        args = execute.call_args.args
        assert args[0] == "--tar-in"
        assert args[1].endswith(f"/{SSH_HOST_KEY_ARCHIVE}:/etc/ssh")
        assert len(args) == 2

    def test_nonzero_exit_is_a_typed_failure(self, clone: CloneVM):
        with patch.object(
            clone.virt_sysprep,
            "execute",
            return_value=_result(
                ok=False, stderr="inspection failed", return_code=1),
        ):
            with pytest.raises(CloneSanitizerError, match="inspection failed"):
                clone.run_identity_pass(_machine_id_only(clone))

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
            with pytest.raises(CloneSanitizerUnavailableError) as caught:
                clone.run_identity_pass(_machine_id_only(clone))
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
                clone.run_identity_pass(_machine_id_only(clone))
        assert not isinstance(caught.value, CloneSanitizerUnavailableError)
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
            with pytest.raises(
                CloneSanitizerUnavailableError,
                match="not installed",
            ):
                clone.run_identity_pass(_machine_id_only(clone))

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
            with pytest.raises(CloneSanitizerUnavailableError) as caught:
                clone.run_identity_pass(_machine_id_only(clone))
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
                clone.run_identity_pass(_machine_id_only(clone))
        assert execute.call_args.kwargs["execution_timeout"] == 300
        assert execute.call_args.kwargs["timeout"] == 315

    @pytest.mark.parametrize("return_code", [124, 137])
    def test_inner_timeout_exit_is_typed(
        self, clone: CloneVM, return_code: int
    ):
        with patch.object(
            clone.virt_sysprep,
            "execute",
            return_value=_result(ok=False, return_code=return_code),
        ):
            with pytest.raises(CloneSanitizerError, match="timed out after 300s"):
                clone.run_identity_pass(_machine_id_only(clone))


class TestIdentityPlan:
    """The pure planning half: what the pass will ask virt-sysprep to do."""

    def test_default_policies_cover_both_properties(self, clone: CloneVM):
        plan = clone.build_identity_plan()
        assert [prop.config_key for prop in plan.properties] == [
            "clone_machine_id", "clone_ssh_host_keys"]
        assert plan.operations == ["machine-id", "ssh-hostkeys"]
        assert plan.needs_customize is True
        assert plan.fresh_ssh_host_keys is True
        assert plan.strictest_policy == "auto"

    def test_host_keys_off_leaves_a_machine_id_only_plan(self, tmp_path: Path):
        plan = _vm(tmp_path, clone_ssh_host_keys="off").build_identity_plan()
        assert plan.operations == ["machine-id"]
        assert plan.needs_customize is False
        assert plan.fresh_ssh_host_keys is False

    def test_machine_id_off_leaves_a_host_keys_only_plan(self, tmp_path: Path):
        plan = _vm(tmp_path, clone_machine_id="off").build_identity_plan()
        assert plan.operations == ["ssh-hostkeys"]
        assert plan.needs_customize is True

    def test_every_policy_off_yields_an_empty_plan(self, tmp_path: Path):
        plan = _vm(
            tmp_path, clone_machine_id="off", clone_ssh_host_keys="off",
        ).build_identity_plan()
        assert plan.properties == []
        assert plan.operations == []

    def test_a_single_required_property_makes_the_pass_fail_closed(
        self, tmp_path: Path
    ):
        """The strictest policy among the enabled properties wins."""
        plan = _vm(
            tmp_path, clone_machine_id="auto", clone_ssh_host_keys="required",
        ).build_identity_plan()
        assert plan.strictest_policy == "required"

    def test_machine_id_off_with_another_property_on_warns(
        self, tmp_path: Path, captured_logs
    ):
        """``customize`` always rewrites /etc/machine-id; say so."""
        _vm(tmp_path, clone_machine_id="off").build_identity_plan()
        assert any(
            "clone_machine_id=off cannot be honoured" in rec.message
            and "customize" in rec.message
            for rec in captured_logs.records)

    def test_machine_id_off_alone_does_not_warn(
        self, tmp_path: Path, captured_logs
    ):
        _vm(
            tmp_path, clone_machine_id="off", clone_ssh_host_keys="off",
        ).build_identity_plan()
        assert not any(
            "cannot be honoured" in rec.message
            for rec in captured_logs.records)

    def test_describe_reads_as_a_list_of_properties(self, clone: CloneVM):
        assert clone.build_identity_plan().describe() == (
            "machine id and ssh host keys")
        assert _machine_id_only(clone).describe() == "machine id"

    @pytest.mark.parametrize("policy", ["auto", "required", "off"])
    def test_accepts_host_key_policies(self, tmp_path: Path, policy: str):
        assert _vm(
            tmp_path, clone_ssh_host_keys=policy).ssh_host_keys_policy == policy

    @pytest.mark.parametrize("policy", ["strict", True, None, ["auto"]])
    def test_rejects_invalid_host_key_policy(self, tmp_path: Path, policy):
        with pytest.raises(ConfigError, match="clone_ssh_host_keys"):
            _vm(tmp_path, clone_ssh_host_keys=policy)


class TestSysprepInvocation:

    def test_customize_is_enabled_whenever_customizations_exist(
        self, clone: CloneVM
    ):
        plan = _machine_id_only(clone)
        plan.customizations = ["--upload", "/tmp/k:/etc/ssh/k"]
        args, kwargs = clone.build_sysprep_invocation(plan)
        assert kwargs["operations"].split(",") == ["machine-id", "customize"]
        assert args == ["--upload", "/tmp/k:/etc/ssh/k"]

    def test_customize_is_absent_without_customizations(self, clone: CloneVM):
        _args, kwargs = clone.build_sysprep_invocation(
            _machine_id_only(clone))
        assert "customize" not in kwargs["operations"]

    def test_invariant_rejects_customizations_without_customize(self):
        """The silent-no-op guard: virt-sysprep would exit 0 doing nothing."""
        with pytest.raises(ProvisionError, match="silently ignore"):
            CloneVM.assert_customize_invariant(
                ["machine-id"], ["--hostname", "node01"])

    def test_invariant_accepts_customizations_with_customize(self):
        CloneVM.assert_customize_invariant(
            ["machine-id", "customize"], ["--hostname", "node01"])

    def test_invariant_accepts_operations_without_customizations(self):
        CloneVM.assert_customize_invariant(["machine-id"], [])

    def test_selinux_relabelling_is_never_suppressed(self, clone: CloneVM):
        """An unlabelled uploaded host key is one sshd refuses to read."""
        args, kwargs = clone.build_sysprep_invocation(
            clone.build_identity_plan())
        assert "--no-selinux-relabel" not in args
        assert "no_selinux_relabel" not in kwargs

    def test_an_invariant_violation_is_not_degraded_to_a_notice(
        self, clone: CloneVM
    ):
        """A boxman bug must not be reported as an uninspectable guest."""
        notices: list[str] = []
        clone.info[CLONE_DEGRADATION_NOTICES_KEY] = notices
        with patch.object(
            clone, "build_sysprep_invocation",
            side_effect=ProvisionError("bad plan"),
        ), patch.object(clone, "discard_unsafe_clone") as discard:
            with pytest.raises(ProvisionError, match="bad plan"):
                clone.apply_identity_policies()
        assert notices == []
        discard.assert_not_called()


class TestSshHostKeys:

    def test_generates_a_real_key_of_every_type(self, clone: CloneVM):
        args, staging = clone.generate_ssh_host_keys()
        try:
            assert args == [
                "--tar-in",
                f"{os.path.join(staging, SSH_HOST_KEY_ARCHIVE)}:/etc/ssh",
            ]
            for key_type in SSH_HOST_KEY_TYPES:
                private = os.path.join(staging, f"ssh_host_{key_type}_key")
                assert os.path.isfile(private)
                assert os.path.isfile(f"{private}.pub")
                assert "PRIVATE KEY" in Path(private).read_text()
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def test_archive_entries_are_root_owned_with_sshd_modes(
        self, clone: CloneVM
    ):
        """Why an archive at all, rather than --upload.

        libguestfs preserves the *source* file's ownership on upload, so the
        guest would get its private host keys owned by whatever uid boxman
        runs as, and ``--chown`` cannot fix that portably -- guestfs-tools
        1.52.0 rejects every form of its own documented syntax. A tar entry
        carries uid, gid and mode itself.
        """
        _args, staging = clone.generate_ssh_host_keys()
        try:
            archive = os.path.join(staging, SSH_HOST_KEY_ARCHIVE)
            with tarfile.open(archive) as tar:
                members = {member.name: member for member in tar.getmembers()}

            expected = dict(CloneVM.ssh_host_key_files())
            assert set(members) == set(expected)
            for name, member in members.items():
                assert member.uid == 0 and member.gid == 0, (
                    f"{name} is owned by {member.uid}:{member.gid} in the "
                    f"archive; sshd needs its host keys owned by root")
                assert member.uname == "root" and member.gname == "root"
                assert member.mode == expected[name], (
                    f"{name} has mode {member.mode:o}, expected "
                    f"{expected[name]:o}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def test_the_archive_carries_nothing_but_the_keys(self, clone: CloneVM):
        """It unpacks into /etc/ssh, so a stray member would land there."""
        _args, staging = clone.generate_ssh_host_keys()
        try:
            with tarfile.open(os.path.join(staging, SSH_HOST_KEY_ARCHIVE)) as tar:
                names = tar.getnames()
            assert SSH_HOST_KEY_ARCHIVE not in names
            assert len(names) == 2 * len(SSH_HOST_KEY_TYPES)
            assert all("/" not in name for name in names)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def test_two_clones_never_share_a_host_key(self, tmp_path: Path):
        """#201: clones of one template must not present the same keys."""
        first_args, first = _vm(tmp_path).generate_ssh_host_keys()
        second_args, second = _vm(tmp_path).generate_ssh_host_keys()
        try:
            assert first != second
            for key_type in SSH_HOST_KEY_TYPES:
                name = f"ssh_host_{key_type}_key.pub"
                assert (Path(first) / name).read_text() != (
                    Path(second) / name).read_text()
            assert first_args != second_args  # distinct staging paths
        finally:
            shutil.rmtree(first, ignore_errors=True)
            shutil.rmtree(second, ignore_errors=True)

    def test_keygen_failure_is_typed_and_leaves_nothing_behind(
        self, clone: CloneVM
    ):
        with patch(
            "boxman.providers.libvirt.clone_vm._shell_run",
            return_value=_result(ok=False, stderr="disk full"),
        ):
            with pytest.raises(CloneSanitizerError, match="ssh host key"):
                clone.generate_ssh_host_keys()
        assert _staging_leftovers(clone.new_vm_name) == []

    def test_staging_is_removed_after_a_successful_pass(self, clone: CloneVM):
        with patch.object(
            clone.virt_sysprep, "execute", return_value=_result()
        ):
            clone.run_identity_pass(clone.build_identity_plan())
        assert _staging_leftovers(clone.new_vm_name) == []

    def test_staging_is_removed_after_a_failed_pass(self, clone: CloneVM):
        with patch.object(
            clone.virt_sysprep, "execute",
            return_value=_result(ok=False, stderr="inspection failed",
                                 return_code=1),
        ):
            with pytest.raises(CloneSanitizerError):
                clone.run_identity_pass(clone.build_identity_plan())
        assert _staging_leftovers(clone.new_vm_name) == []


class TestDegradationMessage:

    def test_names_every_property_and_its_policy(self, clone: CloneVM):
        message = clone.degradation_message(
            clone.build_identity_plan(),
            CloneSanitizerError("no libguestfs"))
        assert "clone_machine_id=auto" in message
        assert "clone_ssh_host_keys=auto" in message
        assert "machine id and ssh host keys" in message
        assert "no libguestfs" in message

    def test_says_the_guest_may_have_kept_its_identity(self, clone: CloneVM):
        """The pass is not atomic, so the notice must not overclaim."""
        message = clone.degradation_message(
            clone.build_identity_plan(), CloneSanitizerError("boom"))
        assert "may have kept" in message

    def test_a_degraded_pass_records_one_notice_per_clone(
        self, clone: CloneVM
    ):
        notices: list[str] = []
        clone.info[CLONE_DEGRADATION_NOTICES_KEY] = notices
        with patch.object(
            clone, "run_identity_pass",
            side_effect=CloneSanitizerError("no libguestfs"),
        ):
            clone.apply_identity_policies()
        assert len(notices) == 1
        assert "clone_ssh_host_keys=auto" in notices[0]


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

    def test_cleanup_failure_without_a_sanitizer_cause_is_still_terminal(
        self, clone: CloneVM
    ):
        # discard_unsafe_clone() is reachable without a sanitizer error to
        # chain from. The cleanup failure must still raise rather than leave
        # an unsafe clone behind under a quiet return
        with patch.object(
            clone.virsh,
            "execute",
            return_value=_result(ok=False, stderr="domain is busy"),
        ):
            with pytest.raises(CloneCleanupError) as caught:
                clone.discard_unsafe_clone()
        assert "machine identity reset failed" in str(caught.value)
        assert "domain is busy" in str(caught.value)
        assert "manual cleanup" in str(caught.value)
        assert caught.value.sanitizer_error is None
        assert caught.value.__cause__ is None

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


class TestCloneVmIdentityFailureChain:

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
        virt_sysprep.assert_called_once()
        # the positional arguments are the staged uploads, whose paths carry
        # a random temp component; the operation list is the contract here
        assert virt_sysprep.call_args.kwargs == {
            "domain": "vm01",
            "operations": "machine-id,ssh-hostkeys,customize",
            "keys_from_stdin": True,
            "warn": True,
            "execution_timeout": 300,
            "timeout": 315,
        }
        virsh.assert_called_once_with(
            "undefine", "vm01", "--remove-all-storage", warn=True)
