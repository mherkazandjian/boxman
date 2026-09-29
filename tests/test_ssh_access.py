"""
#223: ``boxman update`` on a project whose only VM was shut off rewrote the
ssh_config without that VM's block, logged "ERROR: failed to add ssh keys to
some vms", and exited 0.

The rule pinned here:

- a VM that is not running is expected: one WARNING naming it, no ERROR, no
  failure, and it keeps its ssh_config block, with the address from the file
  being rewritten, marked as coming from an earlier run;
- a VM boxman expects to reach -- running, not excluded from waiting -- that
  did not get the admin key fails the verb: update and provision exit 2,
  naming it, once the rest of the verb has run;
- the other ways the key step used to report "not all successful" are
  classified: no admin_pass is a warning; an admin_pass that cannot be
  resolved, a missing public key or key pair, and an ssh config that cannot
  be written fail the verb.

Only the session is a mock: the ssh flow, and the verbs around it up to
their stubbed provisioning steps, run for real. Exit codes are taken from
``app.main()``, where the CLI turns a BoxmanError into exit 2.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import shlex
import shutil
import subprocess
import types
from unittest.mock import MagicMock

import pytest
from tests.conftest import make_bare_manager

from boxman.exceptions import BoxmanError, ProvisionError
from boxman.manager import BoxmanManager
from boxman.runtime.docker_compose import DockerComposeRuntime
from boxman.runtime.local import LocalRuntime

pytestmark = pytest.mark.unit


def full(vm: str) -> str:
    """The libvirt name of *vm* in the test project."""
    return f"bprj__demo__bprj_cluster_1_{vm}"


class Lab:
    """
    A bare manager with one cluster and a mocked session.

    ``states`` is what the state query reports and ``addresses`` what the
    session gives a *running* VM -- one address, or a list of them, one per
    interface; a VM that is not running has no address, as with the real
    session.
    """

    def __init__(self, tmp_path, vms=("vm01", "vm02"), admin_pass="secret",
                 key_pair=True, app_config=None):
        cluster = {
            "workdir": str(tmp_path / "c1"),
            "admin_user": "admin",
            "admin_key_name": "id_ed25519_boxman",
            "ssh_config": "ssh_config",
            "vms": {vm: {"hostname": vm} for vm in vms},
        }
        if admin_pass is not None:
            cluster["admin_pass"] = admin_pass
        self.mgr = make_bare_manager({
            "project": "demo",
            "workspace": {"path": str(tmp_path)},
            "clusters": {"cluster_1": cluster},
        })
        # boxman.yml; its ssh.authorized_keys are the global keys
        self.mgr.app_config = app_config
        self.states = {full(vm): "running" for vm in vms}
        self.addresses: dict[str, str | list[str]] = {}
        self.session = MagicMock()
        self.session.get_vm_ip_addresses.side_effect = self._addresses_of
        self.mgr.provider = self.session
        self.mgr._docker_ssh_jump_stanza = lambda: None
        self.mgr._get_vm_states = lambda: dict(self.states)
        # sshpass, ssh-copy-id and the backoff between attempts stay out
        self.mgr._try_add_ssh_key = MagicMock(return_value=True)
        self.ssh_config = tmp_path / "ssh_config"
        if key_pair:
            (tmp_path / "id_ed25519_boxman").write_text("private\n")
            (tmp_path / "id_ed25519_boxman.pub").write_text(
                "ssh-ed25519 AAAA test\n")

    def _addresses_of(self, name):
        if self.states.get(name) != "running" or name not in self.addresses:
            return {}
        found = self.addresses[name]
        if isinstance(found, str):
            found = [found]
        return {f"vnet{i}": address for i, address in enumerate(found)}

    def asked_for_address(self, vm) -> int:
        return sum(1 for c in self.session.get_vm_ip_addresses.call_args_list
                   if c.args[0] == full(vm))

    def logged(self, level) -> list[str]:
        calls = getattr(self.mgr.logger, level).call_args_list
        return [c.args[0] for c in calls if c.args]

    def pushed(self) -> list[str]:
        return [c.kwargs["hostname"]
                for c in self.mgr._try_add_ssh_key.call_args_list]

    def block(self, alias: str) -> str | None:
        """The ssh_config block whose first Host alias is *alias*."""
        return _block_in(self.ssh_config, alias)


def _block_in(path, alias: str) -> str | None:
    """The block of the ssh_config at *path* whose first alias is *alias*."""
    if not path.is_file():
        return None
    for chunk in path.read_text().split("\n\n\n"):
        lines = chunk.strip("\n").splitlines()
        if lines and lines[0].split()[:2] == ["Host", alias]:
            return "\n".join(lines)
    return None


def _sync_run_parallel(self, tasks, op_label="parallel task",
                       max_workers=None, on_result=None):
    """_run_parallel's contract, in-process."""
    results, failures = {}, {}
    for label, target, args in tasks:
        try:
            results[label] = target(*args)
        except Exception as exc:
            failures[label] = f"{type(exc).__name__}: {exc}"
    return results, failures


def _nothing_to_change(self, cluster_name, cluster_cfg, vm_name, vm_info,
                       result_queue, dry_run=False, allow_restart=False):
    """Stand-in for ``_update_single_vm``: the VM is as configured."""


@pytest.fixture
def update_flow(monkeypatch):
    """Everything ``update`` does before its ssh step, stubbed: no network
    changes, every configured VM exists and has nothing to change."""
    for name, stub in (
            ("_update_sessions_with_runtime", lambda self: None),
            ("ensure_shared_bridges", lambda self: None),
            ("reconcile_networks", lambda self, **kw: {}),
            ("_find_all_existing_project_vms",
             lambda self: self._get_project_vm_names()),
            ("_normalize_cdroms_for_update",
             lambda self, names, allow_fetch=True: {}),
            ("_update_single_vm", _nothing_to_change),
            ("_run_parallel", _sync_run_parallel)):
        monkeypatch.setattr(BoxmanManager, name, stub)


def arm_provision(lab: Lab, monkeypatch) -> None:
    """Stub provision's building steps; the ssh setup stays real."""
    monkeypatch.setattr("time.sleep", lambda _s: None)
    mgr = lab.mgr
    mgr.cache = MagicMock()
    mgr.cache.projects = {}
    for name in ("_update_sessions_with_runtime", "register_project_in_cache",
                 "_expand_oci_base_images", "validate_base_images",
                 "provision_files", "ensure_shared_bridges",
                 "define_networks", "clone_vms", "configure_and_start_vms",
                 "wait_for_vm_ips", "connect_info",
                 "provision_compose_clusters", "deploy_netlab"):
        setattr(mgr, name, MagicMock())
    mgr.ensure_templates_exist = MagicMock(return_value=True)
    mgr._find_existing_project_vms = MagicMock(return_value=[])


def run_verb(monkeypatch, verb) -> tuple[int, str]:
    """Run *verb* the way the CLI does: ``(exit code, error message)``."""
    from boxman.scripts import app

    seen = {"message": ""}

    def _main():
        try:
            verb()
        except BoxmanError as exc:
            seen["message"] = str(exc)
            raise

    monkeypatch.setattr(app, "_main", _main)
    boxman_logger = logging.getLogger("boxman")
    level = boxman_logger.level
    try:
        app.main()
    except SystemExit as exc:
        return exc.code, seen["message"]
    finally:
        # main() lowers the logger to ERROR on its way out
        boxman_logger.setLevel(level)
    return 0, seen["message"]


def update(lab: Lab, monkeypatch) -> tuple[int, str]:
    return run_verb(monkeypatch, lambda: lab.mgr.update(types.SimpleNamespace(
        dry_run=False, yes=True, restart=False, recreate_networks=False)))


def provision(lab: Lab, monkeypatch) -> tuple[int, str]:
    return run_verb(monkeypatch, lambda: lab.mgr.provision(
        types.SimpleNamespace(force=False, rebuild_templates=False)))


def _both_up_once(lab: Lab) -> None:
    """An earlier run, with both VMs running, wrote the ssh config."""
    lab.addresses = {full("vm01"): "192.168.10.5",
                     full("vm02"): "192.168.10.6"}
    lab.mgr.write_ssh_config()


def _written_block(host="Host cluster_1_vm01 node0", hostname="192.168.10.5",
                   drop=(), extra=()) -> bytes:
    """A VM block as write_ssh_config writes it, but for the lines named in
    *drop* (by keyword) and the ones in *extra*: one change at a time."""
    lines = [host, f"    Hostname {hostname}", "    User admin",
             '    IdentityFile "/ws/id_ed25519_boxman"',
             "    StrictHostKeyChecking no",
             "    UserKnownHostsFile /dev/null"]
    lines = [line for line in lines if line.split()[0] not in drop]
    return ("\n".join(lines + list(extra)) + "\n\n\n").encode()


#: The stanza write_ssh_config puts first under the docker runtime.
JUMP_STANZA = ("Host boxman-libvirt-jump\n"
               "    HostName     127.0.0.1\n"
               "    Port         2678\n"
               "    User         qemu_user\n"
               '    IdentityFile "/ws/.boxman/runtime/docker/data/ssh/id_ed25519"\n'
               "    StrictHostKeyChecking no\n"
               "    UserKnownHostsFile /dev/null\n"
               "\n\n")


def _unreadable(path) -> None:
    """Make the file at *path* unreadable (mode 000), or skip the test where
    that does not stop this user reading it -- as root."""
    path.chmod(0)
    if os.access(path, os.R_OK):
        path.chmod(0o600)
        pytest.skip("mode 000 does not stop this user reading a file")


def _effective(config_path, host) -> dict[str, str]:
    """What OpenSSH itself applies to *host* -- not what the file says."""
    out = subprocess.run(
        ["ssh", "-F", str(config_path), "-G", host],
        capture_output=True, text=True, check=True).stdout
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


needs_ssh = pytest.mark.skipif(shutil.which("ssh") is None,
                               reason="the oracle here is OpenSSH itself")


class TestAVmThatIsNotRunningIsExpected:

    @pytest.mark.parametrize("state", ["shut off", "paused", "managedsave"])
    def test_update_keeps_its_entry_warns_once_and_exits_0(
            self, tmp_path, monkeypatch, update_flow, state):
        lab = Lab(tmp_path)
        _both_up_once(lab)
        lab.states[full("vm01")] = state

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        block = lab.block("cluster_1_vm01")
        assert block is not None, "the stopped VM lost its ssh_config entry"
        lines = block.splitlines()
        assert lines[0] == "Host cluster_1_vm01 node0"
        assert "    Hostname 192.168.10.5" in lines
        assert f"vm01 was not running ({state})" in block
        assert "from an earlier run" in block
        assert "    Hostname 192.168.10.6" in lab.block("cluster_1_vm02")
        assert lab.logged("error") == []
        about_it = [w for w in lab.logged("warning") if "vm01" in w]
        assert len(about_it) == 1, about_it
        assert "cluster_1/vm01" in about_it[0]
        assert state in about_it[0]
        assert "192.168.10.5" in about_it[0]
        assert "`boxman update` adds the key" in about_it[0]
        assert "`boxman up`" in about_it[0]
        assert lab.pushed() == ["cluster_1_vm02"]

    def test_the_session_is_asked_for_its_address_once_per_run(
            self, tmp_path, monkeypatch, update_flow):
        """The session logs "is not running, cannot get the ip addresses"
        each time it is asked. The ssh steps asked twice and connect_info
        a third time; only connect_info's lookup is left."""
        lab = Lab(tmp_path, vms=("vm01",))
        lab.states[full("vm01")] = "shut off"

        update(lab, monkeypatch)

        assert lab.asked_for_address("vm01") == 1

    @needs_ssh
    def test_openssh_resolves_the_kept_entry(
            self, tmp_path, monkeypatch, update_flow):
        lab = Lab(tmp_path)
        _both_up_once(lab)
        lab.states[full("vm01")] = "shut off"

        update(lab, monkeypatch)

        effective = _effective(lab.ssh_config, "cluster_1_vm01")
        assert effective["hostname"] == "192.168.10.5"
        assert effective["user"] == "admin"
        assert _effective(lab.ssh_config, "node0")["hostname"] == "192.168.10.5"

    def test_the_entry_survives_later_runs(
            self, tmp_path, monkeypatch, update_flow):
        """The kept block, comment and all, is read back the next time."""
        lab = Lab(tmp_path)
        _both_up_once(lab)
        lab.states[full("vm01")] = "shut off"

        update(lab, monkeypatch)
        code, message = update(lab, monkeypatch)

        assert code == 0, message
        block = lab.block("cluster_1_vm01")
        assert "    Hostname 192.168.10.5" in block.splitlines()
        assert block.count("# boxman:") == 1

    def test_no_earlier_entry_means_none_is_written(
            self, tmp_path, monkeypatch, update_flow):
        lab = Lab(tmp_path)
        lab.addresses = {full("vm02"): "192.168.10.6"}
        lab.states[full("vm01")] = "shut off"

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        assert lab.block("cluster_1_vm01") is None
        assert lab.block("cluster_1_vm02") is not None
        assert lab.logged("error") == []
        [warning] = [w for w in lab.logged("warning") if "vm01" in w]
        assert "no earlier entry" in warning

    #: Each is a block boxman writes with one thing changed, so each case
    #: shows the rule that rejects it -- not some other deviation.
    UNPARSEABLE = {
        "not utf-8": b"\xff\xfe" + _written_block(),
        "not an ssh config at all": b"\x00\x01{\"json\": true}\x7f\n",
        "a name, not an address":
            _written_block(hostname="vm01.example.org"),
        "an IPv6 address": _written_block(hostname="fe80::1"),
        "a keyword boxman never writes":
            _written_block(extra=["    Port 2222"]),
        "an option with arguments":
            _written_block(extra=["    ProxyCommand nc %h %p"]),
        "two addresses":
            _written_block(extra=["    Hostname 192.168.10.7"]),
        "a directive boxman writes, missing": _written_block(drop=("User",)),
        "host-key checking left on":
            _written_block(drop=("StrictHostKeyChecking",),
                           extra=["    StrictHostKeyChecking yes"]),
        "a jump host boxman never writes":
            _written_block(extra=["    ProxyJump bastion.example.org"]),
        "key=value syntax":
            _written_block(drop=("Hostname",),
                           extra=["    Hostname=192.168.10.5"]),
        "a trailing comment": _written_block(hostname="192.168.10.5 # old"),
        "a Host line without the node alias":
            _written_block(host="Host cluster_1_vm01"),
        "a second alias that is not node<N>":
            _written_block(host="Host cluster_1_vm01 production"),
        "the alias written twice":
            _written_block()
            + _written_block(host="Host cluster_1_vm01 node1",
                             hostname="192.168.10.7"),
        "the alias under another, one-alias heading":
            _written_block()
            + b"Host cluster_1_vm01\n    Hostname 203.0.113.9\n",
        "its node alias under another heading":
            _written_block() + b"Host node0\n    Hostname 203.0.113.9\n",
        "a Match block": _written_block(host="Match host cluster_1_vm01"),
    }

    @pytest.mark.parametrize("content", list(UNPARSEABLE.values()),
                             ids=list(UNPARSEABLE))
    def test_an_unparseable_earlier_file_is_no_earlier_entry(
            self, tmp_path, monkeypatch, update_flow, content):
        lab = Lab(tmp_path)
        lab.ssh_config.write_bytes(content)
        lab.addresses = {full("vm02"): "192.168.10.6"}
        lab.states[full("vm01")] = "shut off"

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        assert lab.block("cluster_1_vm01") is None
        assert lab.block("cluster_1_vm02") is not None
        assert lab.logged("error") == []
        [warning] = [w for w in lab.logged("warning") if "vm01" in w]
        assert "no earlier entry" in warning

    @pytest.mark.parametrize("interface", ["its only one", "a second one"])
    def test_an_address_a_running_vm_now_reports_is_not_kept(
            self, tmp_path, monkeypatch, update_flow, interface):
        """The stale-address case that matters: another VM of the project,
        same user and key, would take the login silently -- on whichever of
        its interfaces holds the address."""
        lab = Lab(tmp_path)
        _both_up_once(lab)
        lab.states[full("vm01")] = "shut off"
        if interface == "its only one":
            lab.addresses[full("vm02")] = "192.168.10.5"
            own = "192.168.10.5"
        else:
            lab.addresses[full("vm02")] = ["192.168.20.6", "192.168.10.5"]
            own = "192.168.20.6"

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        assert lab.block("cluster_1_vm01") is None
        # vm02's own block still uses its first address
        assert f"    Hostname {own}" in lab.block(
            "cluster_1_vm02").splitlines()
        [warning] = [w for w in lab.logged("warning") if "vm01" in w]
        assert "is now cluster_1/vm02's" in warning

    def test_an_address_a_vm_of_another_file_reports_is_not_kept(
            self, tmp_path):
        """Clusters without a shared workspace get a file each, but share the
        project's networks: the check spans every file written."""
        lab = Lab(tmp_path)
        config = lab.mgr.config
        config["workspace"] = {}
        other = copy.deepcopy(config["clusters"]["cluster_1"])
        other.update(workdir=str(tmp_path / "c2"),
                     vms={"vm03": {"hostname": "vm03"}})
        config["clusters"]["cluster_2"] = other
        for cluster in config["clusters"].values():
            os.makedirs(cluster["workdir"])
        vm03 = "bprj__demo__bprj_cluster_2_vm03"
        lab.states[vm03] = "running"
        lab.addresses = {full("vm01"): "192.168.10.5",
                         full("vm02"): "192.168.10.6",
                         vm03: "192.168.20.7"}
        lab.mgr.write_ssh_config()
        lab.states[full("vm01")] = "shut off"
        # vm01's old address is now on vm03's second interface
        lab.addresses[vm03] = ["192.168.20.7", "192.168.10.5"]

        lab.mgr.write_ssh_config(vm_states=dict(lab.states))

        assert "cluster_1_vm01" not in (tmp_path / "c1" / "ssh_config").read_text()
        [warning] = [w for w in lab.logged("warning") if "cluster_1/vm01" in w]
        assert "is now cluster_2/vm03's" in warning

    def test_a_kept_entry_keeps_its_jump_host_under_the_docker_runtime(
            self, tmp_path, monkeypatch, update_flow):
        """The strict reading still takes the block the docker runtime
        writes, ProxyJump included."""
        lab = Lab(tmp_path)
        lab.mgr._docker_ssh_jump_stanza = lambda: JUMP_STANZA
        _both_up_once(lab)
        lab.states[full("vm01")] = "shut off"

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        lines = lab.block("cluster_1_vm01").splitlines()
        assert "    Hostname 192.168.10.5" in lines
        assert "    ProxyJump boxman-libvirt-jump" in lines

    def test_an_address_two_earlier_entries_claim_is_kept_by_neither(
            self, tmp_path, monkeypatch, update_flow):
        lab = Lab(tmp_path)
        # both reported it when the file was written (a lease handed on)
        lab.addresses = {full("vm01"): "192.168.10.5",
                         full("vm02"): "192.168.10.5"}
        lab.mgr.write_ssh_config()
        lab.states = {full("vm01"): "shut off", full("vm02"): "shut off"}

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        assert lab.block("cluster_1_vm01") is None
        assert lab.block("cluster_1_vm02") is None
        for vm in ("vm01", "vm02"):
            [warning] = [w for w in lab.logged("warning") if vm in w]
            assert "claims its earlier address 192.168.10.5 too" in warning

    def test_a_vm_update_creates_never_reuses_an_earlier_entry(
            self, tmp_path, monkeypatch, update_flow):
        """A domain undefined outside boxman is new to update, which clones
        it again; the block under its alias belongs to the old domain."""
        lab = Lab(tmp_path)
        _both_up_once(lab)
        monkeypatch.setattr(BoxmanManager, "_find_all_existing_project_vms",
                            lambda self: [full("vm02")])
        for name in ("_expand_oci_base_images", "validate_base_images",
                     "_clone_and_configure_new_vms", "wait_for_vm_ips"):
            setattr(lab.mgr, name, MagicMock())
        lab.mgr.ensure_templates_exist = MagicMock(return_value=True)
        # the new vm01 powered itself off again (an installer that finished)
        lab.states[full("vm01")] = "shut off"

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        lab.mgr._clone_and_configure_new_vms.assert_called_once_with(
            {full("vm01")})
        assert lab.block("cluster_1_vm01") is None
        [warning] = [w for w in lab.logged("warning") if "cluster_1/vm01" in w]
        assert "created in this run" in warning

    @pytest.mark.parametrize("networks", [
        "unchanged", "recreated without reconnecting vm02"])
    def test_up_keeps_the_entry_of_a_vm_that_did_not_come_up(
            self, tmp_path, monkeypatch, networks):
        lab = Lab(tmp_path)
        _both_up_once(lab)
        lab.states[full("vm01")] = "shut off"
        lab.session.start_vm.return_value = False
        for name, stub in (
                ("_update_sessions_with_runtime", lambda self: None),
                ("ensure_shared_bridges", lambda self: None),
                ("reconcile_networks", lambda self, **kw: {}),
                ("_control_vm_targets", lambda self, cli_args: [
                    (full("vm01"), str(tmp_path / "c1"))]),
                ("_refuse_stale_external_save", lambda self, vm, wd: None),
                ("_run_parallel", _sync_run_parallel)):
            monkeypatch.setattr(BoxmanManager, name, stub)
        for name in ("wait_for_vm_ips", "ensure_netlab_up",
                     "provision_compose_clusters", "connect_info"):
            setattr(lab.mgr, name, MagicMock())
        if networks != "unchanged":
            # vm02 runs, but the recreate left it without its network
            lab.mgr.reconcile_networks = MagicMock(
                return_value={"net": "partial"})
            lab.mgr._reattach_failed_vms = {full("vm02")}
            del lab.addresses[full("vm02")]

        with pytest.raises(ProvisionError, match="could not bring up"):
            lab.mgr.up(types.SimpleNamespace(
                force=False, yes=False, recreate_networks=False,
                vms="all", cluster=None))

        assert "    Hostname 192.168.10.5" in lab.block(
            "cluster_1_vm01").splitlines()
        [warning] = [w for w in lab.logged("warning")
                     if "cluster_1/vm01" in w]
        # up adds no keys, so it does not say one was skipped
        assert "ssh key" not in warning
        assert "`boxman up`" in warning
        # vm01 never came up; a vm02 the recreate cut off gets no lease
        # either -- waiting for either one only burns the timeout
        if networks == "unchanged":
            lab.mgr.wait_for_vm_ips.assert_called_once_with(
                [full("vm02")], max_wait=300)
        else:
            lab.mgr.wait_for_vm_ips.assert_not_called()


class TestAVmBoxmanExpectsToReachFailsTheVerb:

    @pytest.mark.parametrize("why", ["no address", "the copy failed"])
    def test_update_exits_2_naming_it(
            self, tmp_path, monkeypatch, update_flow, why):
        lab = Lab(tmp_path)
        lab.addresses = {full("vm02"): "192.168.10.6"}
        if why == "the copy failed":
            lab.addresses[full("vm01")] = "192.168.10.5"
            lab.mgr._try_add_ssh_key.side_effect = (
                lambda **kw: kw["hostname"] != "cluster_1_vm01")

        code, message = update(lab, monkeypatch)

        assert code == 2
        assert message.startswith("update finished with 1 failure(s)")
        assert "ssh key not added to cluster_1/vm01" in message
        assert "cluster_1/vm02" not in message
        # everything else still happened
        assert lab.block("cluster_1_vm02") is not None
        assert "cluster_1_vm02" in lab.pushed()

    @pytest.mark.parametrize("why", ["no address", "the copy failed"])
    def test_provision_exits_2_naming_it_after_the_rest_has_run(
            self, tmp_path, monkeypatch, why):
        lab = Lab(tmp_path)
        lab.addresses = {full("vm02"): "192.168.10.6"}
        if why == "the copy failed":
            lab.addresses[full("vm01")] = "192.168.10.5"
            lab.mgr._try_add_ssh_key.side_effect = (
                lambda **kw: kw["hostname"] != "cluster_1_vm01")
        arm_provision(lab, monkeypatch)

        code, message = provision(lab, monkeypatch)

        assert code == 2
        assert message.startswith("provision finished, but ssh key not "
                                  "added to cluster_1/vm01")
        assert "cluster_1/vm02" not in message
        assert lab.block("cluster_1_vm02") is not None
        lab.mgr.connect_info.assert_called_once()
        lab.mgr.provision_compose_clusters.assert_called_once()
        lab.mgr.deploy_netlab.assert_called_once()

    def test_provision_reports_it_with_the_vms_that_never_started(
            self, tmp_path, monkeypatch):
        lab = Lab(tmp_path)
        lab.states[full("vm02")] = "shut off"      # never starts
        lab.addresses = {full("vm01"): "192.168.10.5"}
        lab.mgr._try_add_ssh_key.return_value = False
        arm_provision(lab, monkeypatch)

        code, message = provision(lab, monkeypatch)

        assert code == 2
        assert f"1 VM(s) never started: {full('vm02')}" in message
        assert "ssh key not added to cluster_1/vm01" in message

    def test_provision_names_the_vms_an_unreadable_admin_pass_kept_out(
            self, tmp_path, monkeypatch):
        """A file:// password that exists but cannot be read raised
        PermissionError out of provision before its connection info,
        compose clusters and lab."""
        secret = tmp_path / "admin_pass"
        secret.write_text("secret\n")
        _unreadable(secret)
        lab = Lab(tmp_path, admin_pass=f"file://{secret}")
        lab.addresses = {full("vm01"): "192.168.10.5",
                         full("vm02"): "192.168.10.6"}
        arm_provision(lab, monkeypatch)

        code, message = provision(lab, monkeypatch)

        assert code == 2
        assert message.startswith(
            "provision finished, but ssh key not added to cluster_1/vm01, "
            "cluster_1/vm02: the admin_pass of cluster cluster_1 cannot be "
            "resolved")
        assert str(secret) in message
        assert lab.pushed() == []
        assert lab.block("cluster_1_vm01") is not None
        lab.mgr.connect_info.assert_called_once()
        lab.mgr.provision_compose_clusters.assert_called_once()
        lab.mgr.deploy_netlab.assert_called_once()

    def test_a_state_boxman_does_not_know_counts_as_running(
            self, tmp_path, monkeypatch, update_flow):
        """virsh translates its state names under a non-English locale. One
        that is not known to mean "not running" must not excuse a VM --
        neither from the key, nor with a kept, possibly stale, entry."""
        lab = Lab(tmp_path)
        _both_up_once(lab)
        lab.states[full("vm01")] = "en cours d'exécution"

        code, message = update(lab, monkeypatch)

        assert code == 2
        assert "ssh key not added to cluster_1/vm01" in message
        assert lab.block("cluster_1_vm01") is None

    def test_unknown_states_count_every_vm_as_expected(
            self, tmp_path, monkeypatch, update_flow):
        """Failing closed: when libvirt cannot say which VMs run, a VM
        without an address is not excused as a stopped one."""
        lab = Lab(tmp_path)

        def _no_states():
            raise ProvisionError("could not query VM states via virsh")

        lab.mgr._get_vm_states = _no_states
        lab.states[full("vm01")] = "shut off"   # all the session can tell
        lab.addresses = {full("vm02"): "192.168.10.6"}

        code, message = update(lab, monkeypatch)

        assert code == 2
        assert "ssh key not added to cluster_1/vm01" in message
        assert any("could not read the VMs' states" in w
                   for w in lab.logged("warning"))


class TestAVmExcludedFromWaitingIsNotAFailure:

    def test_update_a_vm_a_recreate_could_not_reconnect(
            self, tmp_path, monkeypatch, update_flow):
        lab = Lab(tmp_path)
        lab.addresses = {full("vm02"): "192.168.10.6"}   # vm01: no address
        lab.mgr._reattach_failed_vms = {full("vm01")}

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        assert lab.logged("error") == []
        assert any("cluster_1/vm01" in w for w in lab.logged("warning"))
        assert lab.pushed() == ["cluster_1_vm02"]

    def test_setup_ssh_access_leaves_out_what_the_caller_did_not_wait_for(
            self, tmp_path):
        lab = Lab(tmp_path)
        lab.addresses = {full("vm02"): "192.168.10.6"}   # vm01: no address

        lab.mgr.setup_ssh_access(unreachable={full("vm01")})

        assert lab.pushed() == ["cluster_1_vm02"]

    def test_provision_hands_over_the_vms_that_never_started(
            self, tmp_path, monkeypatch):
        lab = Lab(tmp_path)
        lab.states[full("vm02")] = "shut off"      # never starts
        arm_provision(lab, monkeypatch)
        lab.mgr.setup_ssh_access = MagicMock()

        provision(lab, monkeypatch)

        kwargs = lab.mgr.setup_ssh_access.call_args.kwargs
        assert list(kwargs["unreachable"]) == [full("vm02")]
        # every VM was cloned in this run: none inherits an earlier entry
        assert sorted(kwargs["fresh"]) == [full("vm01"), full("vm02")]

    def test_provision_never_reuses_an_earlier_entry(
            self, tmp_path, monkeypatch):
        lab = Lab(tmp_path)
        _both_up_once(lab)     # left behind by a deprovision without --cleanup
        lab.states[full("vm01")] = "shut off"      # the new vm01 never starts
        arm_provision(lab, monkeypatch)

        code, message = provision(lab, monkeypatch)

        assert code == 2 and "never started" in message
        assert lab.block("cluster_1_vm01") is None
        [warning] = [w for w in lab.logged("warning") if "cluster_1/vm01" in w]
        assert "created in this run" in warning

    def test_a_stopped_vm_is_not_waited_for_after_a_recreate(self, tmp_path):
        """A recreate leaves a shut-off VM shut off: waiting for its
        address burned the whole timeout."""
        lab = Lab(tmp_path, vms=("vm01", "vm02", "vm03", "vm04"))
        lab.states[full("vm01")] = "shut off"
        lab.mgr._reattach_failed_vms = {full("vm03")}
        # a state it does not know is waited for, as a running one
        lab.states[full("vm04")] = "en cours d'exécution"

        assert lab.mgr._vms_worth_waiting_for() == [full("vm02"), full("vm04")]


class TestTheOtherKeyProblemsAreClassified:

    def test_no_admin_pass_is_a_warning_not_a_failure(
            self, tmp_path, monkeypatch, update_flow):
        """The ISO-boot boxes run without one on purpose (talos has no
        sshd; the proxmox answer file authorizes the key itself)."""
        lab = Lab(tmp_path, admin_pass=None)
        _both_up_once(lab)

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        assert lab.logged("error") == []
        assert any("no admin_pass" in w for w in lab.logged("warning"))
        assert lab.pushed() == []
        assert lab.block("cluster_1_vm01") is not None

    def test_without_admin_pass_a_stopped_vm_is_promised_no_key(
            self, tmp_path, monkeypatch, update_flow):
        """No verb adds a key there, so the warning must not say one will."""
        lab = Lab(tmp_path, admin_pass=None)
        _both_up_once(lab)
        lab.states[full("vm01")] = "shut off"

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        assert "    Hostname 192.168.10.5" in lab.block(
            "cluster_1_vm01").splitlines()
        [warning] = [w for w in lab.logged("warning") if "cluster_1/vm01" in w]
        assert "key" not in warning
        assert "`boxman up`" in warning

    @pytest.mark.parametrize("reference", [
        "an unset variable", "a file it may not read"])
    def test_an_admin_pass_that_cannot_be_resolved_fails_the_update(
            self, tmp_path, monkeypatch, update_flow, reference):
        """It used to escape from the end of the run as a traceback: a
        ValueError for the variable, PermissionError for the file."""
        monkeypatch.delenv("A223_UNSET_PASSWORD", raising=False)
        admin_pass = "${env:A223_UNSET_PASSWORD}"
        if reference == "a file it may not read":
            secret = tmp_path / "A223_UNSET_PASSWORD"
            secret.write_text("secret\n")
            _unreadable(secret)
            admin_pass = f"file://{secret}"
        lab = Lab(tmp_path, admin_pass=admin_pass)
        _both_up_once(lab)

        code, message = update(lab, monkeypatch)

        assert code == 2
        # one cause, one failure, naming each VM it stopped
        assert message.startswith("update finished with 1 failure(s): ssh "
                                  "key not added to cluster_1/vm01, "
                                  "cluster_1/vm02: the admin_pass of cluster "
                                  "cluster_1 cannot be resolved")
        assert "A223_UNSET_PASSWORD" in message
        assert lab.pushed() == []
        assert lab.block("cluster_1_vm01") is not None

    def test_an_unreadable_admin_pass_with_no_vm_running_is_a_warning(
            self, tmp_path, monkeypatch, update_flow):
        """No key was due, so there is nothing it kept from a VM."""
        secret = tmp_path / "admin_pass"
        secret.write_text("secret\n")
        _unreadable(secret)
        lab = Lab(tmp_path, admin_pass=f"file://{secret}")
        _both_up_once(lab)
        lab.states = dict.fromkeys(lab.states, "shut off")

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        assert lab.logged("error") == []
        [warning] = [w for w in lab.logged("warning") if "admin_pass" in w]
        assert str(secret) in warning
        assert "no VM of cluster cluster_1 is running" in warning

    def test_an_unreadable_global_key_is_skipped_with_a_warning(
            self, tmp_path, monkeypatch, update_flow):
        """As an unresolvable one always was, rather than a traceback."""
        key = tmp_path / "global.pub"
        key.write_text("ssh-ed25519 AAAA global\n")
        _unreadable(key)
        lab = Lab(tmp_path, app_config={
            "ssh": {"authorized_keys": [f"file://{key}"]}})
        lab.addresses = {full("vm01"): "192.168.10.5",
                         full("vm02"): "192.168.10.6"}

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        assert lab.logged("error") == []
        assert any(w.startswith("skipping unresolvable SSH key entry")
                   and str(key) in w for w in lab.logged("warning"))

    @pytest.mark.parametrize("verb", ["update", "provision"])
    def test_global_keys_that_cannot_be_written_fail_the_verb_last(
            self, tmp_path, monkeypatch, update_flow, verb):
        """A file where the cluster's workdir should be stopped the ssh
        setup with a traceback before any key or config was written."""
        lab = Lab(tmp_path, app_config={
            "ssh": {"authorized_keys": ["ssh-ed25519 AAAA global"]}})
        (tmp_path / "c1").write_text("a file where the workdir should be\n")
        lab.addresses = {full("vm01"): "192.168.10.5",
                         full("vm02"): "192.168.10.6"}
        lab.mgr.connect_info = MagicMock()
        if verb == "provision":
            arm_provision(lab, monkeypatch)

        code, message = (update if verb == "update" else provision)(
            lab, monkeypatch)

        assert code == 2
        target = tmp_path / "c1" / "global_authorized_keys"
        assert (f"the global authorized keys of cluster cluster_1 could not "
                f"be written to {target}") in message
        # the rest of the setup still ran
        assert lab.block("cluster_1_vm01") is not None
        assert lab.pushed() == ["cluster_1_vm01", "cluster_1_vm02"]
        lab.mgr.connect_info.assert_called_once()

    def test_a_missing_public_key_fails_the_update(
            self, tmp_path, monkeypatch, update_flow):
        lab = Lab(tmp_path)
        (tmp_path / "id_ed25519_boxman.pub").unlink()
        _both_up_once(lab)

        code, message = update(lab, monkeypatch)

        assert code == 2
        assert "id_ed25519_boxman.pub does not exist" in message
        assert "cluster_1/vm01" in message

    def test_a_key_pair_that_cannot_be_generated_fails_the_update(
            self, tmp_path, monkeypatch, update_flow):
        lab = Lab(tmp_path, key_pair=False)
        # ssh-keygen "runs" and makes nothing
        monkeypatch.setattr("boxman.manager_parts.ssh.run", MagicMock())
        lab.addresses = {full("vm01"): "192.168.10.5",
                         full("vm02"): "192.168.10.6"}

        code, message = update(lab, monkeypatch)

        assert code == 2
        assert "key pair could not be generated" in message
        # the ssh config is still written: it used to be skipped outright
        assert lab.block("cluster_1_vm01") is not None

    def test_an_ssh_config_that_cannot_be_written_fails_the_update(
            self, tmp_path, monkeypatch, update_flow):
        lab = Lab(tmp_path)
        lab.ssh_config.mkdir()
        lab.addresses = {full("vm01"): "192.168.10.5",
                         full("vm02"): "192.168.10.6"}

        code, message = update(lab, monkeypatch)

        assert code == 2
        assert f"could not write the ssh config {lab.ssh_config}" in message


def _two_clusters(tmp_path, layout: str, runtime: str, paths: str):
    """
    Two clusters and three VMs, running, to write an ssh config for.

    *layout* is "one shared file" (a workspace path) or "a file per
    cluster"; *runtime* "local" or "docker" (a DockerComposeRuntime mock:
    the real jump stanza and ProxyJump are written); *paths* is "custom
    names" -- a control: its own user, key name, ssh_config file name and
    hostnames per cluster -- or the same with "a blank in the workspace",
    which every path then holds, or "a blank in the key name".

    Returns the Lab and, per VM, what OpenSSH should read for it: ``(alias,
    node alias, address, user, config file, identity file)``.
    """
    root = tmp_path / ("work space" if paths == "a blank in the workspace"
                       else "work")
    root.mkdir()
    lab = Lab(root, key_pair=False)
    config = lab.mgr.config
    second = copy.deepcopy(config["clusters"]["cluster_1"])
    second.update(workdir=str(root / "c2"), vms={"vm03": {"hostname": "vm03"}})
    config["clusters"]["cluster_2"] = second
    key_name = ("access key" if paths == "a blank in the key name"
                else "custom_key")
    for number, cluster in enumerate(config["clusters"].values(), 1):
        cluster["admin_user"] = f"operator{number}"
        cluster["admin_key_name"] = key_name
        cluster["ssh_config"] = "custom ssh config"
        for vm, info in cluster["vms"].items():
            info["hostname"] = f"guest-{vm}"
    vm03 = "bprj__demo__bprj_cluster_2_vm03"
    lab.states[vm03] = "running"
    lab.addresses = {full("vm01"): "192.168.10.5",
                     full("vm02"): "192.168.10.6", vm03: "192.168.20.7"}
    shared = layout == "one shared file"
    if not shared:
        config["workspace"] = {}
        for cluster in config["clusters"].values():
            os.makedirs(cluster["workdir"])
    if runtime == "docker":
        docker = MagicMock(spec=DockerComposeRuntime)
        docker.ssh_port = 2678
        docker.ssh_identity_path = str(root / "jump_key")
        lab.mgr._runtime_instance = docker
        del lab.mgr._docker_ssh_jump_stanza     # the real stanza, not Lab's
    expected = []
    for alias, node, address, user, cluster in (
            ("cluster_1_guest-vm01", "node0", "192.168.10.5", "operator1", "c1"),
            ("cluster_1_guest-vm02", "node1", "192.168.10.6", "operator1", "c1"),
            ("cluster_2_guest-vm03", "node2", "192.168.20.7", "operator2", "c2")):
        folder = root if shared else root / cluster
        expected.append((alias, node, address, user,
                         folder / "custom ssh config", folder / key_name))
    return lab, expected


def _loaded_identity(config_path, host) -> str | None:
    """The identity file ssh loads for *host*: after ~, %-token and ${VAR}
    expansion, which ``ssh -G`` does not show. The transport is
    /bin/false, so no connection is made; ssh -v names what it loads."""
    out = subprocess.run(
        ["ssh", "-v", "-F", str(config_path), "-o",
         f"ProxyCommand={shutil.which('false')}", "-o", "BatchMode=yes",
         host, "true"],
        capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL)
    found = re.search(r"identity file (.+) type -?\d+", out.stderr)
    return found.group(1) if found else None


needs_false = pytest.mark.skipif(shutil.which("false") is None,
                                 reason="the transport here is /bin/false")


class TestPathsAreWrittenSoOpenSSHReadsThemBack:
    """
    The IdentityFile path went into ssh_config unquoted. A blank in it --
    from the workspace, a workdir, admin_key_name -- made the whole file
    invalid for OpenSSH ("keyword identityfile extra arguments at end of
    line"), and made the strict reading refuse boxman's own blocks, so a
    stopped VM lost its entry. Every path is now one double-quoted
    argument with its %s doubled; one that no spelling can carry fails
    the verb without touching the file (#223).
    """

    LAYOUTS = pytest.mark.parametrize(
        "layout", ["one shared file", "a file per cluster"])
    RUNTIMES = pytest.mark.parametrize("runtime", ["local", "docker"])
    PATHS = pytest.mark.parametrize("paths", [
        "custom names", "a blank in the workspace", "a blank in the key name"])

    @LAYOUTS
    @RUNTIMES
    @PATHS
    def test_what_boxman_writes_it_reads_back(
            self, tmp_path, layout, runtime, paths):
        lab, vms = _two_clusters(tmp_path, layout, runtime, paths)
        lab.mgr.write_ssh_config()
        lab.states = dict.fromkeys(lab.states, "shut off")

        lab.mgr.write_ssh_config(vm_states=dict(lab.states))

        for alias, node, address, _user, config_file, identity in vms:
            block = _block_in(config_file, alias)
            assert block is not None, f"{alias} lost its entry"
            lines = block.splitlines()
            assert lines[0] == f"Host {alias} {node}"
            assert f"    Hostname {address}" in lines
            assert f'    IdentityFile "{identity}"' in lines
            assert (("    ProxyJump boxman-libvirt-jump" in lines)
                    == (runtime == "docker"))
            if runtime == "docker":
                text = config_file.read_text()
                assert text.count("Host boxman-libvirt-jump\n") == 1

    @needs_ssh
    @LAYOUTS
    @RUNTIMES
    @PATHS
    def test_openssh_takes_every_file_boxman_writes(
            self, tmp_path, layout, runtime, paths):
        lab, vms = _two_clusters(tmp_path, layout, runtime, paths)
        lab.mgr.write_ssh_config()
        lab.states = dict.fromkeys(lab.states, "shut off")
        # the rewrite: kept entries, comments and all
        lab.mgr.write_ssh_config(vm_states=dict(lab.states))

        for alias, node, address, user, config_file, identity in vms:
            for name in (alias, node):
                effective = _effective(config_file, name)
                assert effective["hostname"] == address
                assert effective["user"] == user
                assert effective["identityfile"] == str(identity)
                if runtime == "docker":
                    assert effective["proxyjump"] == "boxman-libvirt-jump"

    @pytest.mark.parametrize("paths", [
        "a blank in the workspace", "a blank in the key name"])
    def test_update_keeps_the_entry_of_a_vm_whose_paths_hold_a_blank(
            self, tmp_path, monkeypatch, update_flow, paths):
        if paths == "a blank in the workspace":
            tmp_path = tmp_path / "work space"
            tmp_path.mkdir()
        lab = Lab(tmp_path)
        if paths == "a blank in the key name":
            lab.mgr.config["clusters"]["cluster_1"]["admin_key_name"] = "access key"
            (tmp_path / "id_ed25519_boxman").rename(tmp_path / "access key")
            (tmp_path / "id_ed25519_boxman.pub").rename(
                tmp_path / "access key.pub")
        _both_up_once(lab)
        lab.states[full("vm01")] = "shut off"

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        lines = lab.block("cluster_1_vm01").splitlines()
        assert "    Hostname 192.168.10.5" in lines
        assert lab.logged("error") == []
        [warning] = [w for w in lab.logged("warning") if "vm01" in w]
        assert "192.168.10.5" in warning

    @needs_ssh
    @pytest.mark.parametrize("key_name", [
        "a blank key", "it's key", "a\\b key"])
    def test_openssh_reads_the_identity_file_as_written(
            self, tmp_path, key_name):
        """A quote inside the other kind is literal, and so is a backslash
        that OpenSSH reads as no escape."""
        lab = Lab(tmp_path, key_pair=False)
        lab.mgr.config["clusters"]["cluster_1"]["admin_key_name"] = key_name
        lab.addresses = {full("vm01"): "192.168.10.5"}
        lab.mgr.write_ssh_config()

        effective = _effective(lab.ssh_config, "cluster_1_vm01")

        assert effective["identityfile"] == str(tmp_path / key_name)

    @needs_ssh
    @needs_false
    def test_a_percent_sign_in_the_identity_file_is_loaded_as_written(
            self, tmp_path):
        """OpenSSH expands %-tokens in IdentityFile: an undoubled %d in
        `100%done` loaded a file under the user's home instead."""
        tmp_path = tmp_path / "100%done"
        tmp_path.mkdir()
        lab = Lab(tmp_path, key_pair=False)
        lab.addresses = {full("vm01"): "192.168.10.5"}
        lab.mgr.write_ssh_config()
        identity = str(tmp_path / "id_ed25519_boxman")

        # -G shows the argument as read, before the %-tokens are expanded
        effective = _effective(lab.ssh_config, "cluster_1_vm01")
        assert effective["identityfile"] == identity.replace("%", "%%")
        assert _loaded_identity(lab.ssh_config, "cluster_1_vm01") == identity

    @pytest.mark.parametrize("key_name", ["access key", "100%done"])
    def test_a_file_from_before_the_quoting_keeps_its_entries(
            self, tmp_path, monkeypatch, update_flow, key_name):
        """boxman wrote the path unquoted before: blank, % and all. Such a
        file still keeps a stopped VM's entry, and the rewrite quotes it."""
        lab = Lab(tmp_path, key_pair=False)
        lab.mgr.config["clusters"]["cluster_1"]["admin_key_name"] = key_name
        (tmp_path / key_name).write_text("private\n")
        (tmp_path / f"{key_name}.pub").write_text("ssh-ed25519 AAAA test\n")
        identity = tmp_path / key_name
        lab.ssh_config.write_text("".join(
            f"Host cluster_1_{vm} node{index}\n"
            f"    Hostname {address}\n"
            f"    User admin\n"
            f"    IdentityFile {identity}\n"
            f"    StrictHostKeyChecking no\n"
            f"    UserKnownHostsFile /dev/null\n\n\n"
            for index, (vm, address) in enumerate(
                (("vm01", "192.168.10.5"), ("vm02", "192.168.10.6")))))
        lab.addresses = {full("vm02"): "192.168.10.6"}
        lab.states[full("vm01")] = "shut off"

        code, message = update(lab, monkeypatch)

        assert code == 0, message
        lines = lab.block("cluster_1_vm01").splitlines()
        assert "    Hostname 192.168.10.5" in lines
        escaped = str(identity).replace("%", "%%")
        assert f'    IdentityFile "{escaped}"' in lines

    UNWRITABLE = {
        "a double quote": 'id "boxman"',
        "a line break": "id\nboxman",
        "an environment variable": "id${HOME}",
        "a backslash at its end": "id\\",
        "a backslash before a quote": "id\\'s",
    }

    @pytest.mark.parametrize("key_name", list(UNWRITABLE.values()),
                             ids=list(UNWRITABLE))
    def test_a_path_no_ssh_config_can_hold_fails_the_verb_clearly(
            self, tmp_path, monkeypatch, update_flow, key_name):
        lab = Lab(tmp_path)
        _both_up_once(lab)
        before = lab.ssh_config.read_text()
        lab.mgr.config["clusters"]["cluster_1"]["admin_key_name"] = key_name
        (tmp_path / key_name).write_text("private\n")
        (tmp_path / f"{key_name}.pub").write_text("ssh-ed25519 AAAA test\n")

        code, message = update(lab, monkeypatch)

        assert code == 2
        assert message.startswith(
            f"update finished with 1 failure(s): could not write the ssh "
            f"config {lab.ssh_config}: the path "
            f"{str(tmp_path / key_name)!r} cannot be written into it")
        assert "\n" not in message
        # left as it was: never a file OpenSSH refuses
        assert lab.ssh_config.read_text() == before

    def test_a_jump_key_path_no_ssh_config_can_hold_fails_the_verb_clearly(
            self, tmp_path, monkeypatch, update_flow):
        lab = Lab(tmp_path)
        _both_up_once(lab)
        before = lab.ssh_config.read_text()
        docker = MagicMock(spec=DockerComposeRuntime)
        docker.ssh_port = 2678
        docker.ssh_identity_path = str(tmp_path / 'jump "key"')
        lab.mgr._runtime_instance = docker
        del lab.mgr._docker_ssh_jump_stanza

        code, message = update(lab, monkeypatch)

        assert code == 2
        assert (f"the path {docker.ssh_identity_path!r} cannot be written "
                f"into it") in message
        assert lab.ssh_config.read_text() == before

    @pytest.mark.parametrize("runtime", ["local", "docker-compose"])
    def test_the_key_check_hands_ssh_the_config_path_whole(
            self, tmp_path, monkeypatch, runtime):
        """A blank in the workspace split the -F path for the shell, so the
        check after each key copy failed, and so did the update."""
        lab = Lab(tmp_path)
        lab.mgr._runtime_name = runtime
        lab.mgr._runtime_instance = (
            LocalRuntime() if runtime == "local"
            else DockerComposeRuntime(config={"project_name": "demo"}))
        commands = []

        def _run(command, **_kwargs):
            commands.append(command)
            return types.SimpleNamespace(ok=True, stdout="vm01\n", stderr="")

        monkeypatch.setattr("boxman.manager_parts.ssh.run", _run)
        config_path = str(tmp_path / "work space" / "ssh config")

        assert lab.mgr._verify_ssh_connection("cluster_1_vm01", config_path)

        [command] = commands
        argv = shlex.split(command)
        if runtime != "local":
            argv = shlex.split(argv[-1])   # what bash -c runs in the container
        at = argv.index("-F")
        assert argv[at + 1:at + 3] == [config_path, "cluster_1_vm01"]
