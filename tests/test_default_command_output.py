"""Requested reports reach the CLI at default verbosity, without duplication."""

import logging
import sys
from unittest.mock import MagicMock

import pytest

from boxman import log
from boxman.manager import BoxmanManager
from boxman.providers.libvirt.session import LibVirtSession
from boxman.providers.libvirt.snapshot import SnapshotManager
from boxman.providers.libvirt.storage import StorageManager
from boxman.scripts import app
from test_libvirt_snapshot import _restoring, _RevertingLibvirt

pytestmark = pytest.mark.unit

VM = "bprj__demo__bprj_c1_node01"


@pytest.fixture(params=[[], ["-v"], ["-vv"], ["-q"]],
                ids=["default", "verbose", "debug", "quiet"])
def cli(request, tmp_path, monkeypatch, capsys):
    """Real CLI setup/dispatch and logging; only provider I/O is substituted."""
    conf = tmp_path / "conf.yml"
    conf.write_text(
        "project: demo\nprovider:\n  libvirt:\n    use_sudo: false\n"
        f"clusters:\n  c1:\n    workdir: {tmp_path}\n"
        "    vms:\n      node01: {}\n")
    app_conf = tmp_path / "boxman.yml"
    app_conf.write_text("runtime: local\n")
    monkeypatch.delenv("BOXMAN_VERBOSITY", raising=False)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(log, "handlers", [handler])
    previous_level = log.level

    def run(*args, expected_exit=0):
        handler.stream = sys.stdout
        monkeypatch.setattr(sys, "argv", [
            "boxman", "--conf", str(conf), "--boxman-conf", str(app_conf),
            *args, *request.param])
        if expected_exit:
            with pytest.raises(SystemExit) as exc:
                app.main()
            assert exc.value.code == expected_exit
        else:
            app.main()
        out, err = capsys.readouterr()
        assert not err
        return out

    try:
        yield run, request.param == ["-q"]
    finally:
        log.setLevel(previous_level)


def _visible(out, quiet, *lines):
    if quiet:
        assert out == ""
    else:
        for line in lines:
            assert out.count(line) == 1, out


def _run_parallel(self, tasks, **kwargs):
    return {name: target(*args) for name, target, args in tasks}, {}


@pytest.mark.parametrize("state,notice,args", [
    ("shut off", "already shut off", ()),
    ("managedsave", "already saved", ()),
    ("paused", "already suspended", ("--suspend",)),
])
def test_down_reports_an_already_down_vm(cli, monkeypatch, state, notice, args):
    run, quiet = cli
    monkeypatch.setattr(BoxmanManager, "_get_vm_states", lambda self: {VM: state})
    monkeypatch.setattr(BoxmanManager, "_run_parallel", _run_parallel)
    out = run("down", *args)
    _visible(out, quiet, notice, "infrastructure is down")


@pytest.mark.parametrize("args,method,line", [
    ((), "save_vm", "state saved"),
    (("--suspend",), "suspend_vm", "suspended"),
], ids=["save", "suspend"])
def test_down_reports_each_running_vm_it_brings_down(cli, monkeypatch, args, method, line):
    run, quiet = cli
    monkeypatch.setattr(BoxmanManager, "_get_vm_states", lambda self: {VM: "running"})
    monkeypatch.setattr(BoxmanManager, "_run_parallel", _run_parallel)
    monkeypatch.setattr(LibVirtSession, method, lambda *a: True)
    out = run("down", *args)
    _visible(out, quiet, f"VM '{VM}' {line}", "infrastructure is down")


def _run_parallel_collecting(self, tasks, **kwargs):
    results, failures = {}, {}
    for name, target, args in tasks:
        try:
            results[name] = target(*args)
        except Exception as exc:  # what the real runner does
            failures[name] = exc
    return results, failures


def test_down_announces_nothing_for_a_vm_it_could_not_save(cli, monkeypatch):
    run, _quiet = cli
    monkeypatch.setattr(BoxmanManager, "_get_vm_states", lambda self: {VM: "running"})
    monkeypatch.setattr(BoxmanManager, "_run_parallel", _run_parallel_collecting)
    monkeypatch.setattr(LibVirtSession, "save_vm", lambda *a: False)
    out = run("down", expected_exit=2)
    assert "state saved" not in out
    assert "infrastructure is down" not in out
    assert f"could not bring down 1 VM(s) ({VM})" in out


@pytest.mark.parametrize("empty", [False, True], ids=["rows", "empty"])
def test_storage_df_prints_its_report(cli, tmp_path, monkeypatch, empty):
    run, quiet = cli
    if not empty:
        (tmp_path / f"{VM}.qcow2").write_bytes(b"disk")
    monkeypatch.setattr(StorageManager, "count_snapshots", lambda *a: 1)
    monkeypatch.setattr(StorageManager, "snapshot_memory_files", lambda *a: [])
    monkeypatch.setattr(LibVirtSession, "snapshot_memory_images", lambda *a: {})
    monkeypatch.setattr(StorageManager, "disk_info", lambda *a: {
        "actual-size": 1024, "virtual-size": 2048})
    monkeypatch.setattr(StorageManager, "disk_chain", lambda *a: ["disk"])
    monkeypatch.setattr(StorageManager, "disk_measure", lambda *a: {"required": 512})
    out = run("storage", "df")
    _visible(out, quiet, "SNAPMEM", "RECLAIM~",
             "(no qcow2 disks found on host)" if empty else f"{VM}.qcow2")


@pytest.mark.parametrize("empty", [False, True], ids=["rows", "empty"])
def test_snapshot_list_prints_vm_and_snapshots(cli, monkeypatch, empty):
    run, quiet = cli
    monkeypatch.setattr(SnapshotManager, "list_snapshots", lambda *a, **k: [] if empty else [
        {"name": "s1", "description": "before update", "disk_only": True}])
    out = run("snapshot", "list")
    _visible(out, quiet, VM,
             "(no snapshots)" if empty else "Snapshot: s1 - Description: before update (disk only)")


def test_snapshot_list_never_reports_an_unread_list_as_empty(cli, monkeypatch):
    run, _quiet = cli

    def failed(self, vm_name, why=None):
        why.append("error: failed to get domain")
        return None

    monkeypatch.setattr(SnapshotManager, "_read_snapshot_graph", failed)
    out = run("snapshot", "list", expected_exit=2)
    assert "(no snapshots)" not in out
    assert out.count(f"could not list the snapshots of vm {VM}: "
                     "error: failed to get domain") == 1
    assert f"could not list the snapshots of {VM} (see the errors above)" in out


@pytest.mark.parametrize("readable", [True, False], ids=["some-read", "none-read"])
def test_snapshot_list_names_a_snapshot_it_could_not_read(cli, monkeypatch, readable):
    run, quiet = cli
    graph = {"s1": {"xml": None}}
    if readable:
        graph["s2"] = {"xml": "<domainsnapshot/>", "description": "kept"}
    monkeypatch.setattr(SnapshotManager, "_read_snapshot_graph", lambda *a, **k: graph)
    out = run("snapshot", "list", expected_exit=2)
    assert out.count(f"could not read snapshot(s) s1 of vm {VM}; "
                     "left out of the list") == 1
    assert "(no snapshots)" not in out
    assert "Snapshot: s1 " not in out
    if readable and not quiet:
        assert out.count("Snapshot: s2 - Description: kept") == 1


@pytest.mark.parametrize("empty", [False, True], ids=["rows", "empty"])
def test_compose_snapshot_list_prints_its_report(cli, monkeypatch, empty):
    run, quiet = cli
    session = MagicMock()
    session.snapshot_list_cluster.return_value = {} if empty else {
        "s1": {"created": "2026-01-01", "description": "before update",
               "boxes": {"web": "example:s1"}}}
    monkeypatch.setattr(BoxmanManager, "_select_vm_targets", lambda *a: [])
    monkeypatch.setattr(BoxmanManager, "_select_dc_clusters", lambda *a: [("web", {})])
    monkeypatch.setattr(BoxmanManager, "session_for_cluster", lambda *a: session)
    out = run("snapshot", "list")
    _visible(out, quiet, "cluster: web (docker-compose)",
             "(no snapshots)" if empty else "s1  created=2026-01-01  before update")
    if not empty and not quiet:
        assert out.count("web: example:s1") == 1


@pytest.mark.parametrize("noop", [False, True], ids=["plan", "nothing-to-collapse"])
def test_collapse_dry_run_prints_plan_without_mutating(cli, tmp_path, monkeypatch, noop):
    run, quiet = cli
    snapshot_files = tmp_path / "snapshot-files"
    snapshot_files.mkdir()
    vm = _RevertingLibvirt(snapshot_files)
    vm.take("s1")
    if not noop:
        vm.take("s2")
    sm = SnapshotManager({"use_sudo": False})
    execute, shell = _restoring(sm, vm)
    monkeypatch.setattr("boxman.providers.libvirt.snapshot.SnapshotManager", lambda *a: sm)
    monkeypatch.setattr(BoxmanManager, "_run_parallel", _run_parallel)
    monkeypatch.setattr(BoxmanManager, "_select_vm_targets", lambda *a: [
        (vm.VM, "c1", "node01", str(vm.workdir))])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with execute, shell:
        out = run("snapshot", "collapse", "--to", "s1", "--dry-run")
    if noop:
        _visible(out, quiet, "nothing to collapse")
    else:
        _visible(out, quiet, "would drop 1 snapshot(s)", "would rebase", "would keep ['s1']",
                 "would remove")
    assert all(p.read_bytes() == data for p, data in before.items())
    assert set(vm.snapshots) == ({"s1"} if noop else {"s1", "s2"})


def test_collapse_errors_remain_visible_even_when_quiet(cli, monkeypatch):
    run, _quiet = cli
    sm = SnapshotManager({"use_sudo": False})
    monkeypatch.setattr(sm.virsh, "execute", lambda *a, **kw: MagicMock(
        ok=False, stderr="snapshot does not exist"))
    monkeypatch.setattr("boxman.providers.libvirt.snapshot.SnapshotManager", lambda *a: sm)
    monkeypatch.setattr(BoxmanManager, "_run_parallel", _run_parallel)
    out = run("snapshot", "collapse", "--to", "missing", "--dry-run", expected_exit=2)
    assert "target snapshot 'missing' not found" in out
    assert "collapse dry-run failed" in out
