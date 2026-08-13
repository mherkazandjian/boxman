"""Real-libvirt coverage for per-cluster ``mode: bridge`` networks.

The test creates one temporary Linux bridge and one libvirt network inside a
disposable host. It is opt-in because both are host networking resources::

    BOXMAN_INTEGRATION_LIBVIRT=1 pytest \
        tests/test_libvirt_bridge_network_integration.py
"""

from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET

import pytest
import yaml

from boxman.manager import BoxmanManager
from boxman.providers.libvirt.session import LibVirtSession


pytestmark = pytest.mark.integration

URI = os.environ.get("BOXMAN_IT_URI", "qemu:///system")
PROJECT = "boxman_it_bridge_mode"
CLUSTER = "c1"
NETWORK = "migration"
FULL_NETWORK = (
    f"bprj__{PROJECT}__bprj__clstr__{CLUSTER}__clstr__{NETWORK}"
)
HOST_BRIDGE = "bxit76br"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def _virsh(*args: str) -> subprocess.CompletedProcess:
    return _run("virsh", "--connect", URI, *args)


@pytest.fixture
def host_bridge():
    if os.environ.get("BOXMAN_INTEGRATION_LIBVIRT") != "1":
        pytest.skip(
            "set BOXMAN_INTEGRATION_LIBVIRT=1 on a disposable host"
        )
    if _virsh("net-list", "--all", "--name").returncode != 0:
        pytest.skip(f"libvirt is not reachable at {URI}")
    if FULL_NETWORK in _virsh("net-list", "--all", "--name").stdout:
        pytest.skip(f"remove stale network {FULL_NETWORK} before running")
    if _run("ip", "link", "show", "dev", HOST_BRIDGE).returncode == 0:
        pytest.skip(f"host bridge {HOST_BRIDGE} already exists")

    created = _run(
        "sudo", "-n", "ip", "link", "add", "dev", HOST_BRIDGE,
        "type", "bridge",
    )
    assert created.returncode == 0, created.stderr
    try:
        raised = _run(
            "sudo", "-n", "ip", "link", "set", "dev", HOST_BRIDGE, "up"
        )
        assert raised.returncode == 0, raised.stderr
        yield
    finally:
        _virsh("net-destroy", FULL_NETWORK)
        _virsh("net-undefine", FULL_NETWORK)
        _run("sudo", "-n", "ip", "link", "delete", "dev", HOST_BRIDGE)


def test_bridge_network_round_trip_preserves_host_bridge(
    host_bridge, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    workdir = tmp_path / "work" / CLUSTER
    conf = {
        "version": "1.0",
        "project": PROJECT,
        "provider": {
            "libvirt": {
                "uri": URI,
                "virsh_cmd": "/usr/bin/virsh",
                "use_sudo": True,
            }
        },
        "clusters": {
            CLUSTER: {
                "workdir": str(workdir),
                "networks": {
                    NETWORK: {
                        "mode": "bridge",
                        "bridge": {"name": HOST_BRIDGE},
                        "enable": True,
                        "autostart": True,
                    }
                },
                "vms": {},
            }
        },
    }
    conf_path = tmp_path / "conf.yml"
    conf_path.write_text(yaml.safe_dump(conf))

    manager = BoxmanManager(config=str(conf_path))
    manager.load_app_config({})
    manager.runtime = "local"
    session = LibVirtSession(manager.config)
    session.manager = manager
    manager.provider = session
    manager.register_project_in_cache()

    manager.define_networks()

    dumped = _virsh("net-dumpxml", FULL_NETWORK)
    assert dumped.returncode == 0, dumped.stderr
    root = ET.fromstring(dumped.stdout)
    assert root.find("forward").attrib == {"mode": "bridge"}
    assert root.find("bridge").attrib == {"name": HOST_BRIDGE}
    assert root.find("mac") is None
    assert root.find("ip") is None
    assert manager.reconcile_networks() == {}

    assert manager.provider.remove_network(
        name=FULL_NETWORK,
        info=conf["clusters"][CLUSTER]["networks"][NETWORK],
    ) is True
    assert FULL_NETWORK not in _virsh("net-list", "--all", "--name").stdout
    assert _run("ip", "link", "show", "dev", HOST_BRIDGE).returncode == 0
