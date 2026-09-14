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
#: `set -euo pipefail` is here because the real lib.sh sets it, and its absence
#: is what let the first version of this fix pass: an assignment from a failing
#: command substitution ends the script outright under `set -e`, and a stub that
#: always answered successfully could never produce that. STUB_MODE selects the
#: failure being modelled.
_CEPH_STUB_LIB = r"""
set -euo pipefail
NODES=(pve1 pve2 pve3 pve4)
LOGS="$T/logs"; mkdir -p "$LOGS"
LAB_NET=10.77.0.0/24
CEPH_POOL=vmpool
MONSTATE="$T/mons"; : > "$MONSTATE"
CALLS="$T/calls";  : > "$CALLS"
MODE="${STUB_MODE:-normal}"

log() { :; }
die() { echo "die $*" >> "$CALLS"; exit 9; }
sleep() { :; }
wait_ssh() { return 0; }

pssh() {
    local node=$1; shift; local cmd="$*"
    case "$cmd" in
        *"/var/lib/ceph/mon/ceph-"*)
            echo "mon_probe $node" >> "$CALLS"
            [ "$MODE" = ssh_fail ]  && return 255
            [ "$MODE" = odd_reply ] && { echo "maybe"; return 0; }
            # registered in the monmap, but its storage is gone
            if [ "$MODE" = registered_no_storage ] && [ "$node" = pve3 ]; then
                echo no; return 0
            fi
            if grep -qx "$node" "$MONSTATE" 2>/dev/null; then echo yes; else echo no; fi
            return 0 ;;
        *"pveceph mon create"*)
            echo "mon_create $node" >> "$CALLS"
            echo "$node" >> "$MONSTATE"; return 0 ;;
        *"ceph quorum_status"*)
            echo "quorum_probe $node" >> "$CALLS"
            [ -s "$MONSTATE" ] || return 1
            # Report quorum_names, so "a monitor answered" cannot be mistaken
            # for "this node's monitor joined". MON_NOT_IN_QUORUM names a node
            # whose directory exists but which never joined.
            names=""
            while read -r m; do
                [ "$m" = "${MON_NOT_IN_QUORUM:-}" ] && continue
                names="$names${names:+,}\"$m\""
            done < "$MONSTATE"
            if [ "$MODE" = registered_no_storage ]; then
                names="$names${names:+,}\"pve3\""
            fi
            echo "{\"quorum_names\":[$names]}"
            return 0 ;;
        *"ceph --version"*) echo "ceph version 19.2.0"; return 0 ;;
        *"ceph health"*)    echo "HEALTH_OK"; return 0 ;;
        *) echo "cmd $cmd" >> "$CALLS"; return 0 ;;
    esac
}
"""


def _run_ceph_bootstrap(tmp_path, mode: str = "normal",
                        not_in_quorum: str = "", full: bool = False):
    """Run the real pve-ceph.sh against the stub; return (exit code, trace)."""
    work = tmp_path / ("cephrun-" + mode + "-" + (not_in_quorum or "all"))
    work.mkdir()
    shutil.copy(os.path.join(BOX, "scripts", "pve-ceph.sh"), work / "pve-ceph.sh")
    (work / "lib.sh").write_text(_CEPH_STUB_LIB)
    proc = subprocess.run(
        ["bash", "./pve-ceph.sh"], cwd=work, timeout=120, capture_output=True,
        text=True, env=dict(os.environ, T=str(work), STUB_MODE=mode,
                 MON_NOT_IN_QUORUM=not_in_quorum))
    calls = work / "calls"
    every = calls.read_text().splitlines() if calls.exists() else []
    trace = [ln for ln in every
             if ln.startswith(("mon_create", "quorum_probe", "die"))]
    if full:
        return proc.returncode, trace, every
    return proc.returncode, trace


def test_the_first_ceph_monitor_is_created_without_waiting_for_quorum(tmp_path):
    """
    #171 B1. ``pveceph init`` writes /etc/pve/ceph.conf -- configuration, not a
    monitor -- so on four fresh nodes nothing can be quorate until the first
    ``pveceph mon create`` has run, and waiting first could only time out.
    """
    rc, trace = _run_ceph_bootstrap(tmp_path)

    creates = [i for i, ln in enumerate(trace) if ln.startswith("mon_create")]
    assert creates, f"no monitor was ever created; trace={trace}"
    assert trace[creates[0]] == "mon_create pve1"
    assert not any(ln.startswith("quorum_probe") for ln in trace[:creates[0]]), (
        f"a quorum probe ran before any monitor existed; trace={trace}")
    assert rc == 0, f"bootstrap exited {rc}; trace={trace}"


def test_every_monitor_after_the_first_still_waits_for_quorum(tmp_path):
    """The wait is correct for monitors 2..n: the election after the previous
    one takes a few seconds and ``mon create`` fails inside that window."""
    _rc, trace = _run_ceph_bootstrap(tmp_path)

    for node in ("pve2", "pve3"):
        assert f"mon_create {node}" in trace, f"{node} was never created; trace={trace}"
        idx = trace.index(f"mon_create {node}")
        assert trace[idx - 1] == "quorum_probe pve1", (
            f"{node} was created without first waiting for quorum; trace={trace}")


def test_a_node_that_cannot_be_asked_is_reported_not_assumed_monitorless(tmp_path):
    """A failed probe says nothing about whether a monitor exists.

    Reading ssh failure as absence would bootstrap a second monitor onto a
    cluster that already has one. The run must stop and say so instead.
    """
    rc, trace = _run_ceph_bootstrap(tmp_path, mode="ssh_fail")

    assert rc != 0, f"an unreachable node was tolerated; trace={trace}"
    assert not [ln for ln in trace if ln.startswith("mon_create")], (
        f"a monitor was created despite an unusable probe; trace={trace}")
    assert any(ln.startswith("die") and "cannot ask" in ln for ln in trace), trace


def test_an_unexpected_probe_reply_is_refused(tmp_path):
    """Only ``yes``/``no`` is an answer. Anything else -- empty output, a
    changed message -- must not be read as "no monitor here"."""
    rc, trace = _run_ceph_bootstrap(tmp_path, mode="odd_reply")

    assert rc != 0, f"an unexpected reply was tolerated; trace={trace}"
    assert not [ln for ln in trace if ln.startswith("mon_create")], trace


def test_a_monitor_that_never_joined_the_quorum_is_not_reported_ok(tmp_path):
    """#171 B1, amended. "The cluster answered" is not "this monitor joined":
    with pve3's directory present but pve3 absent from quorum_names, an answer
    from any surviving monitor read as success, so the loop logged `mon pve3
    ok` and carried on into OSD creation against a monitor that had never
    joined."""
    rc, trace, full = _run_ceph_bootstrap(tmp_path, not_in_quorum="pve3", full=True)

    assert rc != 0, f"a monitor outside the quorum was reported ok; trace={trace}"
    assert any(ln.startswith("die") and "pve3" in ln for ln in trace), trace
    # ...and it refused *before* doing any work on the strength of it. Moving
    # the check after OSD creation still fails the run, just eight commands too
    # late.
    osd = [i for i, ln in enumerate(full) if "osd create" in ln or "ceph-volume" in ln]
    died = [i for i, ln in enumerate(full) if ln.startswith("die")]
    assert died, full
    assert not [i for i in osd if i < died[0]], (
        f"{len(osd)} osd command(s) ran before the bad monitor was refused")


def test_a_monitor_in_the_monmap_without_its_storage_is_diagnosed(tmp_path):
    """#171 B1, the registration/storage consistency half.

    After a half-finished removal, or a node restored from an older image, the
    monmap can name a monitor whose /var/lib/ceph/mon directory is gone. The
    old code ran `pveceph mon create` straight into an "already exists" error
    with nothing said about how to recover.
    """
    rc, trace, full = _run_ceph_bootstrap(tmp_path, mode="registered_no_storage",
                                          full=True)

    assert rc != 0, f"the mismatch was not reported; trace={trace}"
    assert any("pveceph mon destroy" in ln for ln in full), (
        f"no recovery advice was given; trace={full[-6:]}")
    assert not [ln for ln in full if "mon_create pve3" in ln], (
        "a monitor already in the monmap was created again")
