"""
Deterministic shape checks for boxes/proxmox-nested-ceph-cluster.

The box spans two physical hosts joined by a VXLAN and boots a locally built
Proxmox auto-install ISO, so the provisioning integration job excludes it.
What makes it work is a contract between the two renders of one conf.yml:
hpe1's libvirt NAT network must reserve the MAC of every node — including the
two that live on hpe2 — and every node must pin exactly that MAC on its first
NIC. Those are the things pinned here, without touching libvirt.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
import yaml

from boxman.manager import BoxmanManager
from boxman.utils.jinja_env import create_jinja_env

pytestmark = pytest.mark.unit

BOX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "boxes", "proxmox-nested-ceph-cluster")


def _render(monkeypatch, site: str) -> dict:
    monkeypatch.setenv("BOXMAN_SITE", site)
    monkeypatch.setenv("BOXMAN_CONF_DIR", BOX)
    monkeypatch.delenv("PVE_LAB_DIR", raising=False)
    env = create_jinja_env(BOX)
    return yaml.safe_load(env.get_template("conf.yml").render())


def _vms(cfg: dict) -> dict:
    return cfg["clusters"]["pve"]["vms"]


def _reservations(cfg_hpe1: dict) -> dict:
    hosts = cfg_hpe1["clusters"]["pve"]["networks"]["pvenet"]["ip"]["dhcp"]["hosts"]
    return {h["name"]: h for h in hosts}


def test_hpe1_is_the_nat_side_and_reserves_every_node(monkeypatch):
    cfg = _render(monkeypatch, "hpe1")
    net = cfg["clusters"]["pve"]["networks"]["pvenet"]
    assert net["mode"] == "nat"
    assert net["bridge"]["name"] == "virbr-pve"
    assert net["ip"]["address"] == "10.77.0.1"
    assert "shared_networks" not in cfg
    assert set(_vms(cfg)) == {"pve1", "pve2"}
    res = _reservations(cfg)
    assert set(res) == {"pve1", "pve2", "pve3", "pve4", "demo01"}
    assert res["pve3"]["ip"] == "10.77.0.13"  # hpe2's node, reserved on hpe1


def test_hpe2_is_the_bridge_side(monkeypatch):
    cfg = _render(monkeypatch, "hpe2")
    assert cfg["shared_networks"] == {
        "pvelab": {"bridge": "br-pve", "stp": False, "mtu": 1450}}
    assert cfg["clusters"]["pve"]["networks"]["pvenet"] == {
        "mode": "bridge", "bridge": {"name": "br-pve"}}
    assert set(_vms(cfg)) == {"pve3", "pve4"}


def test_default_site_is_hpe1(monkeypatch):
    monkeypatch.delenv("BOXMAN_SITE", raising=False)
    monkeypatch.setenv("BOXMAN_CONF_DIR", BOX)
    env = create_jinja_env(BOX)
    cfg = yaml.safe_load(env.get_template("conf.yml").render())
    assert set(_vms(cfg)) == {"pve1", "pve2"}


@pytest.mark.parametrize("site", ["hpe1", "hpe2"])
def test_every_node_pins_the_mac_hpe1_reserves(monkeypatch, site):
    reservations = _reservations(_render(monkeypatch, "hpe1"))
    cfg = _render(monkeypatch, site)
    for name, vm in _vms(cfg).items():
        nic = vm["networks"][0]
        assert nic["name"] == "pvenet"
        assert nic["mac"] == reservations[name]["mac"], name
        assert vm["hostname"] == name == reservations[name]["name"]


@pytest.mark.parametrize("site", ["hpe1", "hpe2"])
def test_nodes_use_the_iso_boot_schema(monkeypatch, site):
    cfg = _render(monkeypatch, site)
    cluster = cfg["clusters"]["pve"]
    assert cluster["admin_user"] == "root"
    assert "admin_pass" not in cluster  # nothing to push keys into on the ISO path
    assert os.path.isabs(cluster["admin_key_name"])
    for name, vm in _vms(cfg).items():
        assert vm["boot_order"][0] == "cdrom", name
        assert isinstance(vm["vcpus"], int) and "cpus" not in vm, name
        assert isinstance(vm["memory"], int), name
        assert os.path.isabs(vm["cdroms"][0]["source"]), name
        assert [d["target"] for d in vm["disks"]] == ["vdb", "vdc"], name
        assert "--cpu host-passthrough" in vm["virt_install_extra_args"], name
        assert any("guest_agent" in a for a in vm["virt_install_extra_args"]), name


@pytest.mark.parametrize("site", ["hpe1", "hpe2"])
def test_config_passes_boot_validation(monkeypatch, site):
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = _render(monkeypatch, site)
    mgr.app_config = {}
    mgr.validate_base_images()  # cdroms present, MACs well-formed and unique


def test_tasks_wrap_the_scripts(monkeypatch):
    cfg = _render(monkeypatch, "hpe1")
    expected = {"vxlan-up", "wait-installed", "wait-first-boot", "cluster",
                "ceph", "demo-vm", "migrate", "mtu-check"}
    assert expected <= set(cfg["tasks"])
    for name in expected:
        script = cfg["tasks"][name]["command"].split()[-1]
        assert script.startswith(BOX + "/scripts/"), name
        assert os.path.isfile(script), script


#: A stand-in for scripts/lib.sh. The real pve-ceph.sh sources whatever lib.sh
#: sits beside it, so copying the script next to this one exercises its actual
#: control flow -- no reimplementation of the loop under test.
#:
#: Three facts are modelled *independently*, because ceph reports them
#: separately and conflating them is what the script is being tested for:
#:   MON_STORAGE     nodes with /var/lib/ceph/mon/ceph-<node>
#:   MON_REGISTERED  nodes in the monmap
#:   MON_QUORUM      nodes in quorum_names
#: An earlier fixture derived all three from one list, which encoded the
#: implementation's own mistake and so could not expose it.
#:
#: `set -euo pipefail` is here because the real lib.sh sets it; its absence is
#: what let a broken bootstrap fix pass. Ordinary `log` events are recorded
#: too: discarding them let a mutation that reports a monitor OK *before*
#: checking its quorum pass every test.
_CEPH_STUB_LIB = r"""
set -euo pipefail
NODES=(pve1 pve2 pve3 pve4)
LOGS="$T/logs"; mkdir -p "$LOGS"
LAB_NET=10.77.0.0/24
CEPH_POOL=vmpool
CALLS="$T/calls";  : > "$CALLS"
MODE="${STUB_MODE:-normal}"

_storage="${MON_STORAGE:-}"
_registered="${MON_REGISTERED:-}"
_quorum="${MON_QUORUM:-}"
_has() { case " $1 " in *" $2 "*) return 0 ;; *) return 1 ;; esac; }

log() { echo "log $*" >> "$CALLS"; }
die() { printf 'die %s\n' "$(printf '%s' "$*" | tr '\n' ' ')" >> "$CALLS"; exit 9; }
sleep() { :; }
wait_ssh() { return 0; }

_json_list() {   # _json_list <space separated>
    local out="" n
    for n in $1; do out="$out${out:+,}\"$n\""; done
    echo "[$out]"
}

pssh() {
    local node=$1; shift; local cmd="$*"
    case "$cmd" in
        *"/var/lib/ceph/mon/ceph-"*)
            echo "mon_probe $node" >> "$CALLS"
            [ "$MODE" = ssh_fail ]  && return 255
            [ "$MODE" = odd_reply ] && { echo "maybe"; return 0; }
            _has "$_storage" "$node" && echo yes || echo no
            return 0 ;;
        *"ceph mon dump"*)
            echo "mon_dump" >> "$CALLS"
            [ "$MODE" = dump_fail ] && return 1
            echo "{\"mons\":[$(_json_list "$_registered" | sed 's/^\[//;s/\]$//' \
                 | sed 's/"\([^"]*\)"/{"name":"\1"}/g')]}"
            return 0 ;;
        *"pveceph mon create"*)
            echo "mon_create $node" >> "$CALLS"
            _storage="$_storage $node"; _registered="$_registered $node"
            _quorum="$_quorum $node"
            return 0 ;;
        *"ceph quorum_status"*)
            echo "quorum_probe $node" >> "$CALLS"
            [ -n "$_quorum" ] || return 1
            echo "{\"quorum_names\":$(_json_list "$_quorum")}"
            return 0 ;;
        *"ceph --version"*) echo "ceph version 19.2.0"; return 0 ;;
        *"ceph health"*)    echo "HEALTH_OK"; return 0 ;;
        *) echo "cmd $cmd" >> "$CALLS"; return 0 ;;
    esac
}
"""


def _run_ceph_bootstrap(tmp_path, mode: str = "normal", storage: str = "",
                        registered: str = "", quorum: str = "", tag: str = ""):
    """Run the real pve-ceph.sh against the stub; return (rc, trace, full)."""
    work = tmp_path / ("cephrun-" + (tag or mode))
    work.mkdir(exist_ok=True)
    shutil.copy(os.path.join(BOX, "scripts", "pve-ceph.sh"), work / "pve-ceph.sh")
    (work / "lib.sh").write_text(_CEPH_STUB_LIB)
    proc = subprocess.run(
        ["bash", "./pve-ceph.sh"], cwd=work, timeout=120, capture_output=True,
        text=True, env=dict(os.environ, T=str(work), STUB_MODE=mode,
                            MON_STORAGE=storage, MON_REGISTERED=registered,
                            MON_QUORUM=quorum))
    calls = work / "calls"
    full = calls.read_text().splitlines() if calls.exists() else []
    trace = [ln for ln in full
             if ln.startswith(("mon_create", "quorum_probe", "die"))]
    return proc.returncode, trace, full


def test_the_first_ceph_monitor_is_created_without_waiting_for_quorum(tmp_path):
    """#171 B1. ``pveceph init`` writes configuration, not a monitor, so on
    four fresh nodes nothing can be quorate until the first ``mon create``."""
    rc, trace, _full = _run_ceph_bootstrap(tmp_path, tag="cold")

    creates = [i for i, ln in enumerate(trace) if ln.startswith("mon_create")]
    assert creates, f"no monitor was ever created; trace={trace}"
    assert trace[creates[0]] == "mon_create pve1"
    assert not any(ln.startswith("quorum_probe") for ln in trace[:creates[0]]), trace
    assert rc == 0, f"bootstrap exited {rc}; trace={trace}"


def test_every_monitor_after_the_first_still_waits_for_quorum(tmp_path):
    _rc, trace, _full = _run_ceph_bootstrap(tmp_path, tag="order")
    for node in ("pve2", "pve3"):
        assert f"mon_create {node}" in trace, trace
        idx = trace.index(f"mon_create {node}")
        assert trace[idx - 1] == "quorum_probe pve1", trace


def test_a_node_that_cannot_be_asked_is_reported_not_assumed_monitorless(tmp_path):
    rc, trace, _full = _run_ceph_bootstrap(tmp_path, mode="ssh_fail", tag="sshfail")
    assert rc != 0, trace
    assert not [ln for ln in trace if ln.startswith("mon_create")], trace
    assert any(ln.startswith("die") and "cannot ask" in ln for ln in trace), trace


def test_an_unexpected_probe_reply_is_refused(tmp_path):
    rc, trace, _full = _run_ceph_bootstrap(tmp_path, mode="odd_reply", tag="odd")
    assert rc != 0, trace
    assert not [ln for ln in trace if ln.startswith("mon_create")], trace


def test_a_healthy_existing_cluster_is_left_alone(tmp_path):
    """The positive control. Without it, a consistency guard that refuses
    *any* registered monitor passes every other fixture, because they all
    start from a cold cluster and never meet one."""
    rc, _trace, full = _run_ceph_bootstrap(
        tmp_path, storage="pve1 pve2 pve3", registered="pve1 pve2 pve3",
        quorum="pve1 pve2 pve3", tag="healthy")

    assert rc == 0, f"a healthy cluster was refused: {full[-6:]}"
    assert not [ln for ln in full if ln.startswith("die")], full
    assert not [ln for ln in full if ln.startswith("mon_create")], \
        "an existing monitor was created again"


def test_a_monitor_that_never_joined_the_quorum_is_not_reported_ok(tmp_path):
    """"The cluster answered" is not "this monitor joined". pve3 has its store
    and is registered, but is not in quorum_names."""
    rc, trace, full = _run_ceph_bootstrap(
        tmp_path, storage="pve1 pve2 pve3", registered="pve1 pve2 pve3",
        quorum="pve1 pve2", tag="unjoined")

    assert rc != 0, f"a monitor outside the quorum was reported ok; trace={trace}"
    assert any(ln.startswith("die") and "pve3" in ln for ln in trace), trace
    # It must not be announced OK either -- reporting first and checking after
    # satisfies an exit-code assertion while still telling the operator the
    # opposite of the truth.
    assert not [ln for ln in full if ln == "log mon pve3 ok"], \
        "pve3 was reported ok before its quorum membership was established"
    # ...and nothing was done on the strength of it.
    osd = [i for i, ln in enumerate(full) if "osd create" in ln or "ceph-volume" in ln]
    died = [i for i, ln in enumerate(full) if ln.startswith("die")]
    assert died, full
    assert not [i for i in osd if i < died[0]], (
        f"{len(osd)} osd command(s) ran before the bad monitor was refused")


def test_a_registered_monitor_without_its_storage_is_diagnosed(tmp_path):
    """#171 B1, consistency half. The realistic shape: pve3 is in the monmap
    and *absent* from quorum_names, because its store is gone. A check that
    reads quorum_names instead of the monmap answers the wrong question and
    misses exactly this."""
    rc, _trace, full = _run_ceph_bootstrap(
        tmp_path, storage="pve1 pve2", registered="pve1 pve2 pve3",
        quorum="pve1 pve2", tag="regnostore")

    assert rc != 0, f"the mismatch was not reported: {full[-6:]}"
    assert not [ln for ln in full if "mon_create pve3" in ln], \
        "a monitor already in the monmap was created again"
    advice = " ".join(ln for ln in full if ln.startswith("die"))
    assert "ceph mon remove" in advice, f"no usable recovery was named: {advice}"
    assert "pveceph mon destroy" not in advice or "will refuse" in advice, (
        "advised a command whose own precondition is the missing directory")
