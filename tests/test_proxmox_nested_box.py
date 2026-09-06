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
