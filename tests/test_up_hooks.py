"""
``hooks:`` for ``boxman up``: pre_up/post_up tasks and the up record.

The hooks run for real (TaskRunner, shell commands writing to a log); the
infrastructure side of ``up`` and libvirt are stubbed, so nothing here
touches a hypervisor.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from types import SimpleNamespace

import pytest

from boxman.exceptions import ConfigError, HookError, ProvisionError
from boxman.manager import BoxmanManager
from boxman.manager_parts.hooks import UP_RECORD_NAME

pytestmark = pytest.mark.unit

PROJECT = "hooked"
D1 = f"bprj__{PROJECT}__bprj_c1_vm1"
D2 = f"bprj__{PROJECT}__bprj_c1_vm2"
N1 = f"bprj__{PROJECT}__bprj__clstr__c1__clstr__net1"
EMPTY = {"domains": {}, "networks": {}}
CREATED = {"domains": {D1: "uuid-1", D2: "uuid-2"}, "networks": {N1: "net-uuid-1"}}

# every hook appends "<task> <BOXMAN_HOOK> <run id> <force> <record or ->" to the log
LOG_LINE = 'echo "{name} $BOXMAN_HOOK $BOXMAN_UP_RUN_ID $BOXMAN_UP_FORCE ${{BOXMAN_UP_RECORD:--}}" >> "$HOOK_LOG"'


def _config(tmp_path, hooks=None, tasks=None):
    tasks = tasks if tasks is not None else {
        name: {"command": LOG_LINE.format(name=name)} for name in ("a", "b", "c", "d")}
    config = {
        "version": "1.0",
        "project": PROJECT,
        "provider": {"libvirt": {"uri": "qemu:///system"}},
        "workspace": {"path": str(tmp_path / "workspace")},
        "clusters": {"c1": {"workdir": str(tmp_path / "workspace" / "c1"),
                            "networks": {"net1": {"mode": "nat"}},
                            "vms": {"vm1": {}, "vm2": {}}}},
        "tasks": tasks,
    }
    if hooks is not None:
        config["hooks"] = hooks
    return config


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    """A manager on a synthetic config whose up body and libvirt are stubs."""
    conf_dir = tmp_path / "project"
    conf_dir.mkdir()
    log = tmp_path / "hooks.log"
    monkeypatch.setenv("HOOK_LOG", str(log))

    def make(hooks=None, tasks=None, inventories=(EMPTY, CREATED), up_error=None, provisions=True):
        m = BoxmanManager.__new__(BoxmanManager)
        m.config = _config(tmp_path, hooks, tasks)
        m.config_path = str(conf_dir / "conf.yml")
        m.logger = logging.getLogger("test-up-hooks")
        m.cache = SimpleNamespace(cache_dir=str(tmp_path / "cache"))
        m.events = []
        # the real provision() (which marks that this up provisioned), with
        # the provider work stubbed
        m._provision = lambda cli_args: m.events.append("provision")
        m.report_clone_degradations = lambda: None
        reads = list(inventories)

        def inventory():
            m.events.append("inventory")
            return reads.pop(0) if reads else CREATED
        m.project_inventory = inventory

        def body(cli_args):
            m.events.append("up")
            (tmp_path / "workspace").mkdir(exist_ok=True)
            if provisions:
                m.provision(cli_args)
            if up_error:
                raise up_error
        m._up = body
        m.log = log
        m.record = tmp_path / "workspace" / UP_RECORD_NAME
        return m
    return make


def _args(force=False):
    return SimpleNamespace(force=force)


def _hook_lines(m):
    return m.log.read_text().splitlines() if m.log.exists() else []


# ---- no hooks: up is unchanged ------------------------------------------------
def test_without_hooks_up_is_exactly_the_old_up(mgr):
    m = mgr()
    m.project_inventory = lambda: pytest.fail("no hooks: libvirt must not be read")
    m.up(_args())
    assert m.events == ["up", "provision"]
    assert not m.record.exists()
    assert not m.log.exists()


def test_empty_hook_lists_are_no_hooks(mgr):
    m = mgr(hooks={"pre_up": [], "post_up": None})
    m.up(_args())
    assert m.events == ["up", "provision"] and not m.record.exists()


# ---- fresh: order, environment and the record ----------------------------------
def test_a_fresh_up_runs_the_hooks_in_order_around_up_and_records_what_it_created(mgr):
    m = mgr(hooks={"pre_up": ["a", "b"], "post_up": ["c", "d"]})
    m.up(_args())
    lines = [line.split() for line in _hook_lines(m)]
    assert [(line[0], line[1]) for line in lines] == [
        ("a", "pre_up"), ("b", "pre_up"), ("c", "post_up"), ("d", "post_up")]
    # pre_up before the inventory and up, post_up after
    assert m.events == ["inventory", "up", "provision", "inventory"]
    run_ids = {line[2] for line in lines}
    assert len(run_ids) == 1 and len(run_ids.pop()) == 32
    assert {line[3] for line in lines} == {"0"}
    # only post_up gets the record
    assert [line[4] for line in lines[:2]] == ["-", "-"]
    assert {line[4] for line in lines[2:]} == {str(m.record)}

    record = json.loads(m.record.read_text())
    assert record["boxman_up_record"] == 1
    assert record["project"] == PROJECT
    assert record["run_id"] == lines[0][2]
    assert record["status"] == "succeeded"
    assert record["force"] is False
    assert record["created"] == CREATED
    assert record["present"] == CREATED
    assert record["finished_at"].endswith("Z")
    assert stat.S_IMODE(os.stat(m.record).st_mode) == 0o600


def test_only_pre_up_reads_nothing_and_writes_no_record(mgr):
    m = mgr(hooks={"pre_up": ["a"]})
    m.up(_args())
    assert m.events == ["up", "provision"] and not m.record.exists()
    assert [line.split()[:2] for line in _hook_lines(m)] == [["a", "pre_up"]]


# ---- rerun: nothing created ---------------------------------------------------------
def test_a_rerun_records_nothing_created_and_everything_present(mgr):
    m = mgr(hooks={"post_up": ["c"]}, inventories=(CREATED, CREATED), provisions=False)
    m.up(_args())
    record = json.loads(m.record.read_text())
    assert record["created"] == EMPTY
    assert record["present"] == CREATED
    assert [line.split()[:2] for line in _hook_lines(m)] == [["c", "post_up"]]


def test_each_up_has_its_own_run_id(mgr):
    m = mgr(hooks={"post_up": ["c"]}, inventories=(EMPTY, CREATED, CREATED, CREATED))
    m.up(_args())
    first = json.loads(m.record.read_text())["run_id"]
    m.up(_args())
    second = json.loads(m.record.read_text())["run_id"]
    assert first != second
    assert [line.split()[2] for line in _hook_lines(m)] == [first, second]


# ---- --force: recreated resources are created --------------------------------------
def test_force_reaches_the_hooks_and_recreated_resources_count_as_created(mgr):
    old = {"domains": {D1: "old-1", D2: "uuid-2"}, "networks": {N1: "net-uuid-1"}}
    new = {"domains": {D1: "new-1", D2: "uuid-2"}, "networks": {N1: "net-uuid-1"}}
    m = mgr(hooks={"pre_up": ["a"], "post_up": ["c"]}, inventories=(old, new))
    m.up(_args(force=True))
    assert {line.split()[3] for line in _hook_lines(m)} == {"1"}
    record = json.loads(m.record.read_text())
    assert record["force"] is True
    assert record["created"] == {"domains": {D1: "new-1"}, "networks": {}}
    assert record["present"] == new


# ---- failures -------------------------------------------------------------------------
def test_a_failing_pre_up_hook_stops_before_anything_changes(mgr):
    tasks = {"a": {"command": 'echo a >> "$HOOK_LOG"; exit 3'},
             "b": {"command": 'echo b >> "$HOOK_LOG"'},
             "c": {"command": 'echo c >> "$HOOK_LOG"'}}
    m = mgr(hooks={"pre_up": ["a", "b"], "post_up": ["c"]}, tasks=tasks)
    with pytest.raises(HookError, match="pre_up hook 'a' failed .exit 3.*before boxman up changed anything"):
        m.up(_args())
    assert m.events == []
    assert _hook_lines(m) == ["a"]
    assert not m.record.exists()


def test_a_failing_post_up_hook_fails_up_and_stops_the_rest(mgr):
    tasks = {"c": {"command": 'echo c >> "$HOOK_LOG"; exit 1'},
             "d": {"command": 'echo d >> "$HOOK_LOG"'}}
    m = mgr(hooks={"post_up": ["c", "d"]}, tasks=tasks)
    with pytest.raises(HookError, match="post_up hook 'c' failed .exit 1.*next boxman up runs the post_up hooks again"):
        m.up(_args())
    assert _hook_lines(m) == ["c"]
    assert json.loads(m.record.read_text())["status"] == "succeeded"


def test_a_failed_up_runs_no_post_up_hook_and_records_the_failure(mgr):
    partial = {"domains": {D1: "uuid-1"}, "networks": {N1: "net-uuid-1"}}
    m = mgr(hooks={"pre_up": ["a"], "post_up": ["c"]}, inventories=(EMPTY, partial),
            up_error=ProvisionError("provision finished, but 1 VM(s) never started"))
    with pytest.raises(ProvisionError, match="never started"):
        m.up(_args())
    assert [line.split()[0] for line in _hook_lines(m)] == ["a"]
    record = json.loads(m.record.read_text())
    assert record["status"] == "failed"
    assert record["created"] == partial and record["present"] == partial


def test_an_unreadable_inventory_never_hides_why_up_failed(mgr):
    m = mgr(hooks={"post_up": ["c"]}, up_error=ProvisionError("the real failure"))
    calls = []

    def inventory():
        calls.append(1)
        if len(calls) > 1:
            raise ProvisionError("libvirt unreadable")
        return EMPTY
    m.project_inventory = inventory
    with pytest.raises(ProvisionError, match="the real failure"):
        m.up(_args())
    assert not m.record.exists()


def test_a_failed_up_before_the_workspace_exists_writes_no_record(mgr, tmp_path):
    m = mgr(hooks={"post_up": ["c"]}, up_error=ProvisionError("early"))

    def body(cli_args):
        raise ProvisionError("early")
    m._up = body
    with pytest.raises(ProvisionError, match="early"):
        m.up(_args())
    assert not (tmp_path / "workspace").exists()


def test_the_partial_state_branch_of_the_real_up(mgr):
    """Some VMs missing, no --force: the real _up refuses; no post_up."""
    m = mgr(hooks={"pre_up": ["a"], "post_up": ["c"]})
    del m._up      # the real one, with its provider calls stubbed
    m.validate_snapshot_dirs = lambda: None
    m._get_vm_states = lambda: {D1: "running"}
    (m.record.parent).mkdir(parents=True)
    with pytest.raises(ProvisionError, match="partial infrastructure state"):
        m.up(_args())
    assert [line.split()[0] for line in _hook_lines(m)] == ["a"]
    assert json.loads(m.record.read_text())["status"] == "failed"


def test_the_partial_state_branch_with_force_reprovisions_and_records_it(mgr):
    before = {"domains": {D1: "old-1"}, "networks": {}}
    m = mgr(hooks={"post_up": ["c"]}, inventories=(before, CREATED))
    del m._up
    m.validate_snapshot_dirs = lambda: None
    m._get_vm_states = lambda: {D1: "running"}
    (m.record.parent).mkdir(parents=True)
    m.up(_args(force=True))
    assert "provision" in m.events
    record = json.loads(m.record.read_text())
    assert record["force"] is True and record["created"] == CREATED


# ---- where and with what hooks run --------------------------------------------------
def test_hooks_run_in_the_config_dir_unless_the_task_sets_a_workdir(mgr, tmp_path):
    own = tmp_path / "own"
    own.mkdir()
    tasks = {"a": {"command": 'pwd >> "$HOOK_LOG"'},
             "b": {"command": 'pwd >> "$HOOK_LOG"', "workdir": str(own)}}
    m = mgr(hooks={"pre_up": ["a", "b"]}, tasks=tasks)
    m.up(_args())
    assert _hook_lines(m) == [os.path.realpath(tmp_path / "project"), os.path.realpath(own)]


def test_post_up_gets_the_workspace_env_and_pre_up_does_not(mgr, tmp_path):
    env_file = tmp_path / "ws.env"
    env_file.write_text("WS_ONLY=from-the-workspace\n")
    tasks = {name: {"command": f'echo "{name} ${{WS_ONLY:-unset}}" >> "$HOOK_LOG"'} for name in ("a", "c")}
    m = mgr(hooks={"pre_up": ["a"], "post_up": ["c"]}, tasks=tasks)
    m.config["workspace"]["env_file"] = str(env_file)
    m.up(_args())
    assert _hook_lines(m) == ["a unset", "c from-the-workspace"]


# ---- validation -----------------------------------------------------------------------
@pytest.mark.parametrize("hooks,message", [
    (["a"], "'hooks' must be a mapping"),
    ({"post_down": ["a"]}, "unknown phase.*post_down"),
    ({"pre_up": "a"}, "hooks.pre_up must be a list of task names"),
    ({"pre_up": [""]}, "hooks.pre_up must be a list of task names"),
    ({"post_up": ["nope"]}, "names task 'nope', which is not defined"),
])
def test_a_bad_hooks_key_is_refused_before_anything(mgr, hooks, message):
    m = mgr(hooks=hooks)
    with pytest.raises(ConfigError, match=message):
        m.up(_args())
    assert m.events == [] and not m.log.exists()


@pytest.mark.parametrize("task", [
    {"description": "nothing to run"}, {"command": ""}, {"command": 123}, {"command": ["echo", "hi"]},
    "echo not a mapping",
])
def test_a_hook_task_without_a_string_command_is_refused_before_anything(mgr, task):
    """Caught before hooks, inventory or up, never after provisioning."""
    m = mgr(hooks={"post_up": ["a"]}, tasks={"a": task})
    with pytest.raises(ConfigError, match="task 'a' needs a command, a non-empty string"):
        m.up(_args())
    assert m.events == [] and not m.record.exists()


def test_tasks_that_are_not_a_mapping_are_refused(mgr):
    m = mgr(hooks={"pre_up": ["a"]})
    m.config["tasks"] = ["a"]
    with pytest.raises(ConfigError, match="'tasks' must be a mapping"):
        m.up(_args())


@pytest.mark.parametrize("hooks", [{}, {"pre_up": [], "post_up": None}])
def test_empty_hooks_never_validate_unrelated_tasks(mgr, hooks):
    """Empty hooks are no hooks: up behaves as before, even with a tasks:
    section boxman run would reject."""
    m = mgr(hooks=hooks)
    m.config["tasks"] = ["not", "a", "mapping"]
    m.up(_args())
    assert m.events == ["up", "provision"] and not m.record.exists()


# ---- the inventory behind the record ------------------------------------------------
class _FakeVirsh:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def execute(self, *args, warn=False):
        self.calls.append(args)
        ok, out = self.answers.get(args[0], (True, ""))
        if callable(out):
            ok, out = out(args)
        return SimpleNamespace(ok=ok, stdout=out, stderr="" if ok else "virsh failed")


def _inventory_mgr(tmp_path, answers):
    m = BoxmanManager.__new__(BoxmanManager)
    m.config = _config(tmp_path)
    m.logger = logging.getLogger("test-up-hooks")
    fake = _FakeVirsh(answers)
    m._virsh = lambda: fake
    return m, fake


def test_the_inventory_holds_only_the_projects_resources(tmp_path):
    u1, ux = "0d381ade-3d0e-4868-92f9-acc00ca7595a", "8bcbdcb4-0679-4267-96ea-c08bcea48b06"
    listing = f"{u1} {D1}\n{ux} bprj__other__bprj_c1_vm1\n"
    m, fake = _inventory_mgr(tmp_path, {
        "list": (True, listing),
        "net-list": (True, f"default\n{N1}\nbprj__other__bprj__clstr__c1__clstr__net1\n"),
        "net-uuid": (True, "net-uuid-1\n"),
    })
    assert m.project_inventory() == {"domains": {D1: u1}, "networks": {N1: "net-uuid-1"}}
    assert ("net-uuid", N1) in fake.calls
    assert not [c for c in fake.calls if c[0] == "net-uuid" and c[1] != N1]


@pytest.mark.parametrize("answers,message", [
    ({"list": (False, "")}, "domain list could not be read"),
    ({"list": (True, "garbage line without a uuid\n")}, "domain list could not be read"),
    ({"list": (True, ""), "net-list": (False, "")}, "network list could not be read"),
    ({"list": (True, ""), "net-list": (True, f"{N1}\n"), "net-uuid": (False, "")}, f"uuid of network {N1}"),
])
def test_an_unreadable_inventory_is_an_error_not_an_empty_one(tmp_path, answers, message):
    m, _ = _inventory_mgr(tmp_path, answers)
    with pytest.raises(ProvisionError, match=message):
        m.project_inventory()



# ---- one hooked up at a time, and only a provision creates ----------------------------
def test_a_second_hooked_up_of_the_project_changes_nothing(mgr):
    import fcntl
    m = mgr(hooks={"pre_up": ["a"], "post_up": ["c"]})
    path = m.up_lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(HookError, match="another boxman up of project 'hooked' is running"):
            m.up(_args())
    assert m.events == [] and not m.log.exists() and not m.record.exists()
    m.up(_args())      # released: the next one runs
    assert "up" in m.events


def test_the_lock_is_held_while_the_hooks_run(mgr, monkeypatch):
    probe = ('python3 -c "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR); '
             'fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)" "$UP_LOCK" '
             '&& echo "$BOXMAN_HOOK free" >> "$HOOK_LOG" || echo "$BOXMAN_HOOK held" >> "$HOOK_LOG"')
    m = mgr(hooks={"pre_up": ["a"], "post_up": ["c"]}, tasks={"a": {"command": probe}, "c": {"command": probe}})
    monkeypatch.setenv("UP_LOCK", m.up_lock_path())
    m.up(_args())
    assert _hook_lines(m) == ["pre_up held", "post_up held"]


def test_resources_that_appear_while_an_up_finds_the_project_in_place_are_not_its_own(mgr):
    """Another run creates them between the first inventory and this up's
    decision; this up provisioned nothing, so it claims nothing."""
    m = mgr(hooks={"post_up": ["c"]}, inventories=(EMPTY, CREATED), provisions=False)
    m.up(_args())
    record = json.loads(m.record.read_text())
    assert record["created"] == EMPTY
    assert record["present"] == CREATED


# ---- --force through the real up on a complete project -----------------------------
def test_force_on_a_complete_running_project_reconciles_and_creates_nothing(mgr):
    """The real _up: --force deprovisions only a partial project; a complete,
    running one takes the all-running path, and the record says so."""
    m = mgr(hooks={"post_up": ["c"]}, inventories=(CREATED, CREATED))
    del m._up
    m.validate_snapshot_dirs = lambda: None
    m._get_vm_states = lambda: {D1: "running", D2: "running"}
    for name in ("ensure_shared_bridges", "report_network_results", "raise_on_network_failures",
                 "ensure_netlab_up", "connect_info"):
        setattr(m, name, lambda *a, **k: None)
    m.reconcile_networks = lambda **k: {}
    m._reconcile_compose_clusters = lambda: None
    m.eject_install_media = lambda *a, **k: []
    m._rewrite_ssh_config = lambda: []
    (m.record.parent).mkdir(parents=True)
    m.up(_args(force=True))
    assert "provision" not in m.events
    record = json.loads(m.record.read_text())
    assert record["force"] is True and record["created"] == EMPTY and record["present"] == CREATED
    assert {line.split()[3] for line in _hook_lines(m)} == {"1"}


# ---- an absolute record path, whatever the hook's working directory ---------------
def test_a_relative_workspace_gives_the_hooks_an_absolute_record_path(mgr, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    operator = tmp_path / "operator"
    (operator / "lab").mkdir(parents=True)
    check = 'case "$BOXMAN_UP_RECORD" in /*) ;; *) exit 9 ;; esac; test -f "$BOXMAN_UP_RECORD" && pwd >> "$HOOK_LOG"'
    m = mgr(hooks={"post_up": ["c", "d"]},
            tasks={"c": {"command": check}, "d": {"command": check, "workdir": str(elsewhere)}})
    m.config["workspace"]["path"] = "lab"
    monkeypatch.chdir(operator)
    m.up(_args())
    assert _hook_lines(m) == [os.path.realpath(tmp_path / "project"), os.path.realpath(elsewhere)]
    assert (operator / "lab" / UP_RECORD_NAME).exists()


def _running_project(m, plan_action, define_outcome="created"):
    """The real _up all-running path and the real reconcile_networks, with
    the provider stubbed: plan_network answers *plan_action* for net1."""
    del m._up
    m.validate_snapshot_dirs = lambda: None
    m._get_vm_states = lambda: {D1: "running", D2: "running"}
    for name in ("ensure_shared_bridges", "report_network_results", "raise_on_network_failures",
                 "ensure_netlab_up", "connect_info"):
        setattr(m, name, lambda *a, **k: None)
    m._reconcile_compose_clusters = lambda: None
    m.eject_install_media = lambda *a, **k: []
    m._rewrite_ssh_config = lambda: []
    m.provider = SimpleNamespace(plan_network=lambda name, info: {
        "action": plan_action, "structural": [], "range_ops": [], "host_ops": []})
    m._define_network = lambda *a, **k: define_outcome
    m.wait_for_vm_ips = lambda *a, **k: None
    (m.record.parent).mkdir(parents=True)


@pytest.mark.parametrize("outcome", ["created"])
def test_a_network_this_up_defined_on_the_running_path_is_recorded_as_created(mgr, outcome):
    """reconcile_networks created it, so this up made it."""
    before = {"domains": {D1: "uuid-1", D2: "uuid-2"}, "networks": {}}
    after = {"domains": {D1: "uuid-1", D2: "uuid-2"}, "networks": {N1: "net-uuid-new"}}
    m = mgr(hooks={"post_up": ["c"]}, inventories=(before, after), provisions=False)
    _running_project(m, "create", outcome)
    m.up(_args())
    record = json.loads(m.record.read_text())
    assert record["created"] == {"domains": {}, "networks": {N1: "net-uuid-new"}}
    assert m._networks_created is None      # tracked only inside a hooked up


def test_a_network_that_appeared_without_this_up_defining_it_is_not_claimed(mgr):
    before = {"domains": {D1: "uuid-1", D2: "uuid-2"}, "networks": {}}
    after = {"domains": {D1: "uuid-1", D2: "uuid-2"}, "networks": {N1: "net-uuid-other"}}
    m = mgr(hooks={"post_up": ["c"]}, inventories=(before, after), provisions=False)
    _running_project(m, "none")
    m.up(_args())
    assert json.loads(m.record.read_text())["created"] == EMPTY


def test_network_tracking_is_off_without_hooks(mgr):
    m = mgr(provisions=False)
    _running_project(m, "create")
    m.up(_args())
    assert getattr(m, "_networks_created", None) is None
    assert not m.record.exists()


# ---- a Ctrl-C during a hook -----------------------------------------------------------
#: a boxman up with the hook *stopped* in the phase argv[2], followed by
#: *later* in the same phase; the up body and libvirt are stubs that note
#: what they did in the file "events", and the up runs under the handlers
#: boxman's main() sets up, as the real boxman up does
_HOOKED_UP_IN_A_CHILD = """
import logging, shlex, sys
from pathlib import Path
from types import SimpleNamespace
from boxman.manager import BoxmanManager
from boxman.utils import shell
from tests.test_up_hooks import _config
tmp, phase, hook = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
def note(what):
    with open(tmp / "events", "a") as events:
        events.write(what + "\\n")
later = {"command": 'echo "$BOXMAN_HOOK later" >> ' + shlex.quote(str(tmp / "hooks.log"))}
other = "post_up" if phase == "pre_up" else "pre_up"
m = BoxmanManager.__new__(BoxmanManager)
m.config = _config(tmp, {phase: ["stopped", "later"], other: ["later"]},
                   {"stopped": {"command": hook}, "later": later})
m.config_path = str(tmp / "conf.yml")
m.logger = logging.getLogger("test-up-hooks")
m.cache = SimpleNamespace(cache_dir=str(tmp / "cache"))
m.report_clone_degradations = lambda: None
def inventory():
    note("inventory")
    return {"domains": {}, "networks": {}}
m.project_inventory = inventory
def body(cli_args):
    note("up")
    (tmp / "workspace").mkdir(exist_ok=True)
m._up = body
print("lock", m.up_lock_path(), flush=True)
with shell.interrupt_on_signals():
    m.up(SimpleNamespace(force=False))
note("returned")
"""


@pytest.mark.parametrize("phase", ["pre_up", "post_up"])
def test_a_ctrl_c_during_a_hook_stops_the_hook_and_boxman_up(tmp_path, phase):
    """A Ctrl-C at the terminal while a hook runs (#302): the hook has the
    terminal's SIGINT and is stopped, boxman up ends by SIGINT there, the
    hooks after it and, during pre_up, up itself never run, and the
    project's up lock is free again, held by no hook left behind."""
    import fcntl
    import signal

    from tests.test_utils_shell import (
        _TRAPS_SIGINT,
        _gone_within,
        _interrupt,
        _program,
        _running,
    )

    up = tmp_path / "up"
    try:
        out, err, code = _interrupt(
            tmp_path, _HOOKED_UP_IN_A_CHILD, [tmp_path, phase, _program(_TRAPS_SIGINT, up)],
            [up], how="terminal")
        assert code == -signal.SIGINT, err
        assert (tmp_path / "up.stopping").exists()
        assert _gone_within(up, 5)
    finally:
        # a hook a failing run left behind would sleep on for a minute
        if _running(up):
            os.kill(int(up.read_text()), signal.SIGKILL)
    log = tmp_path / "hooks.log"
    events = tmp_path / "events"
    if phase == "pre_up":
        assert not log.exists()
        assert not events.exists()
        assert not (tmp_path / "workspace" / UP_RECORD_NAME).exists()
    else:
        assert log.read_text().splitlines() == ["pre_up later"]
        assert events.read_text().splitlines() == ["inventory", "up", "inventory"]
        record = json.loads((tmp_path / "workspace" / UP_RECORD_NAME).read_text())
        assert record["status"] == "succeeded"
    lock = out.split("lock ", 1)[1].split()[0]
    with open(lock, "rb+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
