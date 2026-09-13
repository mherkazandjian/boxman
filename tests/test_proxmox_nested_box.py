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


#: A stand-in for scripts/lib.sh. The real pve-ceph.sh sources whatever
#: lib.sh sits beside it, so copying the script next to this one exercises
#: its actual control flow -- no reimplementation of the loop under test.
#: `pssh` answers as a cluster would: `ceph mon dump` reports only the
#: monitors created so far, and `ceph quorum_status` fails while there are
#: none, which is the state a from-scratch run starts in.
_CEPH_STUB_LIB = r"""
NODES=(pve1 pve2 pve3 pve4)
LOGS="$T/logs"; mkdir -p "$LOGS"
LAB_NET=10.77.0.0/24
CEPH_POOL=vmpool
MONSTATE="$T/mons"; : > "$MONSTATE"
CALLS="$T/calls";  : > "$CALLS"

log() { :; }
die() { echo "die $*" >> "$CALLS"; exit 9; }
sleep() { :; }

pssh() {
    local node=$1; shift; local cmd="$*"
    case "$cmd" in
        *"ceph mon dump"*)
            if [ -s "$MONSTATE" ]; then
                echo "epoch 1"; local i=0
                while read -r m; do
                    echo "$i: [v2:10.77.0.1$i:3300/0] mon.$m"; i=$((i+1))
                done < "$MONSTATE"
            fi
            return 0 ;;
        *"pveceph mon create"*)
            echo "mon_create $node" >> "$CALLS"
            echo "$node" >> "$MONSTATE"; return 0 ;;
        *"ceph quorum_status"*)
            echo "quorum_probe $node" >> "$CALLS"
            [ -s "$MONSTATE" ] && return 0 || return 1 ;;
        *"ceph --version"*) echo "ceph version 19.2.0"; return 0 ;;
        *"ceph health"*)    echo "HEALTH_OK"; return 0 ;;
        *) return 0 ;;
    esac
}
"""


def _run_ceph_bootstrap(tmp_path) -> tuple[int, list[str]]:
    """Run the real pve-ceph.sh against the stub; return (exit code, trace).

    The trace keeps only the two events the monitor order depends on:
    ``mon_create <node>`` and ``quorum_probe <node>``.
    """
    work = tmp_path / "cephrun"
    work.mkdir()
    shutil.copy(os.path.join(BOX, "scripts", "pve-ceph.sh"), work / "pve-ceph.sh")
    (work / "lib.sh").write_text(_CEPH_STUB_LIB)
    proc = subprocess.run(
        ["bash", "./pve-ceph.sh"], cwd=work, timeout=120,
        env=dict(os.environ, T=str(work)), capture_output=True, text=True)
    calls = work / "calls"
    trace = [ln for ln in (calls.read_text().splitlines() if calls.exists() else [])
             if ln.startswith(("mon_create", "quorum_probe", "die"))]
    return proc.returncode, trace


def test_the_first_ceph_monitor_is_created_without_waiting_for_quorum(tmp_path):
    """
    #171 B1. ``pveceph init`` writes /etc/pve/ceph.conf -- configuration, not a
    monitor -- so on four fresh nodes nothing can be quorate until the first
    ``pveceph mon create`` has run. Waiting first could only time out: the
    from-scratch run died after 60 probes having created no monitor at all.
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
    one takes a few seconds and ``mon create`` fails with "Could not connect to
    ceph cluster" inside that window. Only the bootstrap monitor is exempt."""
    _rc, trace = _run_ceph_bootstrap(tmp_path)

    for node in ("pve2", "pve3"):
        assert f"mon_create {node}" in trace, f"{node} was never created; trace={trace}"
        idx = trace.index(f"mon_create {node}")
        assert trace[idx - 1] == "quorum_probe pve1", (
            f"{node} was created without first waiting for quorum; trace={trace}")
