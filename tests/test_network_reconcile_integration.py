"""
Network reconciliation against a real libvirt.

The reconcile logic is unit-tested with virsh mocked out, which is enough for
the diff but not for the part that has actually broken twice: what
``define_network`` does with the real projects cache, and whether a guest whose
network was destroyed underneath it comes back. Both bugs were invisible to a
mocked provider. These tests drive the real thing.

**These tests create, destroy and redefine libvirt networks, and power-cycle a
guest they created.** They are therefore opt-in twice over: the ``integration``
marker keeps them out of the default run, and they skip unless
``BOXMAN_INTEGRATION_LIBVIRT=1`` says the host is disposable. Point them at a
throwaway machine -- a nested VM is ideal -- never at a host carrying work.

Environment:

``BOXMAN_INTEGRATION_LIBVIRT=1``
    Required. Confirms this host may have networks destroyed on it.
``BOXMAN_IT_URI``
    libvirt URI, default ``qemu:///system``.
``BOXMAN_IT_SUBNET_A`` / ``BOXMAN_IT_SUBNET_B``
    The two /24 prefixes to move the network between, default ``10.77.1`` and
    ``10.77.2``. Both must be unused on the host or the test skips.
``BOXMAN_IT_BASE_IMAGE``
    Optional qcow2 for the guest, readable **by the qemu user** (a file under a
    0750 home is not). With it the guest runs an OS and answers the shutdown
    request, so the reconnect is quick. Without it the guest is a disk-less
    domain that boots nothing: still enough to hold a tap on the bridge, but
    the power cycle has to wait out the graceful-shutdown timeout, which adds
    about two minutes.

The host also needs passwordless sudo for ``iptables``: boxman's NAT setup
shells out to it when a network is defined.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

import pytest
import yaml

from boxman.manager import BoxmanManager
from boxman.providers.libvirt.session import LibVirtSession


pytestmark = pytest.mark.integration

URI = os.environ.get("BOXMAN_IT_URI", "qemu:///system")
SUBNET_A = os.environ.get("BOXMAN_IT_SUBNET_A", "10.77.1")
SUBNET_B = os.environ.get("BOXMAN_IT_SUBNET_B", "10.77.2")
BASE_IMAGE = os.environ.get("BOXMAN_IT_BASE_IMAGE")

PROJECT = "boxman_it_reconcile"
CLUSTER = "c1"
NETWORK = "lab"
FULL_NET = f"bprj__{PROJECT}__bprj__clstr__{CLUSTER}__clstr__{NETWORK}"
GUEST = "boxman-it-guest"
GUEST_MAC = "52:54:00:17:17:01"


def virsh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["virsh", "--connect", URI, *args],
                          capture_output=True, text=True)


def _ip_bin() -> str:
    return shutil.which("ip") or "/usr/sbin/ip"


def taps_on(bridge: str) -> list[str]:
    """The vnet interfaces enslaved to *bridge* right now."""
    out = subprocess.run([_ip_bin(), "link", "show", "master", bridge],
                         capture_output=True, text=True)
    return [line.split(":")[1].strip()
            for line in out.stdout.splitlines() if ": vnet" in line]


def reservations(network: str) -> dict[str, str]:
    """mac -> ip for the network's dhcp reservations, parsed rather than grepped.

    Substring checks are not safe here: an address like ``10.77.1.10`` is a
    prefix of the range end ``10.77.1.100``, which makes a naive ``in`` assert
    pass and a ``not in`` assert fail for the wrong reason.
    """
    root = ET.fromstring(virsh("net-dumpxml", network).stdout)
    return {host.get("mac"): host.get("ip")
            for host in root.findall("./ip/dhcp/host")}


def network_address(network: str) -> str | None:
    root = ET.fromstring(virsh("net-dumpxml", network).stdout)
    element = root.find("ip")
    return element.get("address") if element is not None else None


def bridge_of(network: str) -> str | None:
    for line in virsh("net-dumpxml", network).stdout.splitlines():
        if "<bridge" in line and "name='" in line:
            return line.split("name='")[1].split("'")[0]
    return None


def network_config(subnet: str, reservation_host: str = "10") -> dict:
    return {
        "mode": "nat",
        "ip": {
            "address": f"{subnet}.1",
            "netmask": "255.255.255.0",
            "dhcp": {
                "range": {"start": f"{subnet}.50", "end": f"{subnet}.100"},
                "hosts": [{"mac": GUEST_MAC, "name": "pinned",
                           "ip": f"{subnet}.{reservation_host}"}],
            },
        },
    }


def _preflight() -> str | None:
    """Return the reason this host must be left alone, or None if it is fine."""
    if os.environ.get("BOXMAN_INTEGRATION_LIBVIRT") != "1":
        return ("set BOXMAN_INTEGRATION_LIBVIRT=1 to run this on a disposable "
                "host: it destroys and redefines libvirt networks")

    listed = virsh("net-list", "--all", "--name")
    if listed.returncode != 0:
        return f"libvirt is not reachable at {URI}"

    if FULL_NET in listed.stdout:
        return f"{FULL_NET} is left over from an earlier run, remove it first"

    # refuse to touch a host already using either subnet
    for name in [n.strip() for n in listed.stdout.splitlines() if n.strip()]:
        xml = virsh("net-dumpxml", name).stdout
        for subnet in (SUBNET_A, SUBNET_B):
            if f"address='{subnet}.1'" in xml:
                return (f"{subnet}.0/24 is already used by network {name}; "
                        f"set BOXMAN_IT_SUBNET_A/B to free prefixes")

    if BASE_IMAGE and not os.path.exists(BASE_IMAGE):
        return f"BOXMAN_IT_BASE_IMAGE does not exist: {BASE_IMAGE}"

    return None


@pytest.fixture(scope="module", autouse=True)
def disposable_host():
    reason = _preflight()
    if reason:
        pytest.skip(reason)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """
    A boxman project on a private HOME, so the real projects cache is untouched.

    Yields a factory that builds a manager for a given subnet. Everything it
    created is torn down afterwards even if the test fails.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    workdir = tmp_path / "work"
    workdir.mkdir()

    def build(subnet: str, reservation_host: str = "10") -> BoxmanManager:
        conf = {
            "project": PROJECT,
            "provider": {"libvirt": {"uri": URI, "virsh_cmd": "/usr/bin/virsh",
                                     "use_sudo": True}},
            "clusters": {CLUSTER: {
                "workdir": str(workdir / CLUSTER),
                "networks": {NETWORK: network_config(subnet, reservation_host)},
                "vms": {},
            }},
        }
        path = tmp_path / "conf.yml"
        path.write_text(yaml.safe_dump(conf))

        manager = BoxmanManager(config=str(path))
        manager.load_app_config({})
        manager.runtime = "local"
        session = LibVirtSession(manager.config)
        session.manager = manager
        manager.provider = session
        try:
            manager.register_project_in_cache()
        except RuntimeError:
            pass                      # a previous build in the same test
        return manager

    try:
        yield build
    finally:
        virsh("destroy", GUEST)
        virsh("undefine", GUEST)
        virsh("net-destroy", FULL_NET)
        virsh("net-undefine", FULL_NET)


def define_guest(network: str) -> None:
    """Define and start a guest holding a nic on *network*."""
    disk = ""
    if BASE_IMAGE:
        disk = (f"<disk type='file' device='disk'>"
                f"<driver name='qemu' type='qcow2'/>"
                f"<source file='{BASE_IMAGE}'/>"
                f"<target dev='vda' bus='virtio'/>"
                f"<readonly/></disk>")

    domain_type = "kvm" if os.path.exists("/dev/kvm") else "qemu"
    xml = (
        f"<domain type='{domain_type}'>"
        f"<name>{GUEST}</name>"
        f"<memory unit='MiB'>512</memory><vcpu>1</vcpu>"
        f"<os><type arch='x86_64'>hvm</type></os>"
        f"<devices>{disk}"
        f"<interface type='network'>"
        f"<source network='{network}'/>"
        f"<mac address='{GUEST_MAC}'/>"
        f"<model type='virtio'/>"
        f"</interface></devices></domain>"
    )

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as handle:
        handle.write(xml)
        path = handle.name
    try:
        assert virsh("define", path).returncode == 0, "could not define the guest"
        assert virsh("start", GUEST).returncode == 0, "could not start the guest"
    finally:
        os.unlink(path)

    for _ in range(30):
        if "running" in virsh("domstate", GUEST).stdout:
            return
        time.sleep(2)
    pytest.fail("the guest never reached the running state")


def wait_for_tap(bridge: str, timeout: int = 60) -> list[str]:
    for _ in range(timeout // 2):
        taps = taps_on(bridge)
        if taps:
            return taps
        time.sleep(2)
    return []


class TestLiveTier:
    """Reservations change on a running network without disturbing it."""

    def test_a_reservation_change_leaves_the_bridge_alone(self, project):
        manager = project(SUBNET_A, reservation_host="10")
        manager.define_networks()
        bridge = bridge_of(FULL_NET)
        assert bridge, "the network was not defined"
        assert reservations(FULL_NET) == {GUEST_MAC: f"{SUBNET_A}.10"}

        # same network, the reservation moved to another address
        moved = project(SUBNET_A, reservation_host="20")
        results = moved.reconcile_networks()

        assert results.get(FULL_NET) == "updated", results
        assert reservations(FULL_NET) == {GUEST_MAC: f"{SUBNET_A}.20"}
        # the whole point of the live tier: the bridge never went away
        assert bridge_of(FULL_NET) == bridge

    def test_reconciling_twice_is_a_no_op(self, project):
        manager = project(SUBNET_A)
        manager.define_networks()

        again = project(SUBNET_A)
        assert again.reconcile_networks() == {}

    def test_a_stopped_network_is_started_again(self, project):
        manager = project(SUBNET_A)
        manager.define_networks()
        assert virsh("net-destroy", FULL_NET).returncode == 0

        restarted = project(SUBNET_A)
        results = restarted.reconcile_networks()

        assert results.get(FULL_NET) == "updated", results
        assert FULL_NET in virsh("net-list", "--name").stdout


class TestRecreateTier:
    """
    Structural drift: destroy, redefine, and put the guest back.

    Without BOXMAN_IT_BASE_IMAGE the guest cannot answer a shutdown request, so
    the reconnect falls through to a forced power cycle and this takes a couple
    of minutes.
    """

    def test_drift_is_reported_but_not_applied_without_the_flag(self, project):
        manager = project(SUBNET_A)
        manager.define_networks()
        before = virsh("net-dumpxml", FULL_NET).stdout

        moved = project(SUBNET_B)
        results = moved.reconcile_networks(allow_recreate=False)

        assert results.get(FULL_NET) == "skipped", results
        assert virsh("net-dumpxml", FULL_NET).stdout == before

    def test_recreate_moves_the_network_and_reconnects_the_guest(self, project):
        manager = project(SUBNET_A)
        manager.define_networks()
        original_bridge = bridge_of(FULL_NET)

        define_guest(FULL_NET)
        assert wait_for_tap(original_bridge), "the guest never got a tap"

        moved = project(SUBNET_B)
        plan = moved.provider.plan_network(
            name=FULL_NET, info=network_config(SUBNET_B))
        assert plan["action"] == "recreate", plan
        assert GUEST in plan["attached_vms"], plan["attached_vms"]

        results = moved.reconcile_networks(allow_recreate=True, auto_accept=True)
        assert results.get(FULL_NET) == "recreated", results

        assert FULL_NET in virsh("net-list", "--name").stdout
        assert network_address(FULL_NET) == f"{SUBNET_B}.1"
        assert reservations(FULL_NET) == {GUEST_MAC: f"{SUBNET_B}.10"}, \
            "the reservation was not carried over"

        # the cache has to agree, or the next run refuses the network as a
        # conflict with itself -- the failure this whole path regressed on
        moved.cache.read_projects_cache()
        cached = moved.cache.projects[PROJECT]["networks"][FULL_NET]
        assert cached["ip_address"] == f"{SUBNET_B}.1", cached

        assert "running" in virsh("domstate", GUEST).stdout
        assert wait_for_tap(bridge_of(FULL_NET)), "the guest was left disconnected"

        # and it converges: nothing left to do on the next pass
        settled = project(SUBNET_B)
        assert settled.reconcile_networks(allow_recreate=True,
                                          auto_accept=True) == {}
