"""
The boxes/talos-unattended install driver, run for real against fakes.

The host scripts (preflight.sh, control-exec.sh, install.sh, status.sh) and
the control-VM scripts (apply-config.sh, bootstrap.sh, flux.sh) execute
unchanged. virsh, ip, df, boxman, ssh, talosctl, kubectl, flux and curl on
PATH are tests/talos_unattended_fakes.py, sharing one JSON world that each
test arranges and then inspects. The fake ssh runs the remote command
locally in a directory standing in for the control VM, after checking the
pinned host key and the operator key. Nothing touches libvirt, the network
or a real cluster.

Every stage has a failure test and a rerun test: what the driver refuses,
that the refusal happens before the side effect it guards, and that a
second run changes nothing it should not (identity, applied configs, etcd
bootstrap).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOX = os.path.join(REPO, "boxes", "talos-unattended")
SCRIPTS = os.path.join(BOX, "scripts")
FAKES = os.path.join(REPO, "tests", "talos_unattended_fakes.py")

PROJECT = "lab353"
PREFIX = f"bprj__{PROJECT}__bprj"
NETWORK = f"{PREFIX}__clstr__management__clstr__fabric"
CONTROL = f"{PREFIX}_management_control"
CP1 = f"{PREFIX}_talos_cp-01"
W1 = f"{PREFIX}_talos_worker-01"
GIB = 1 << 30


def _pins() -> dict:
    with open(os.path.join(BOX, "versions.env")) as fobj:
        lines = [line.strip() for line in fobj]
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


PINS = _pins()
TALOS_VERSION = PINS["TALOS_VERSION"]


def _gen_fake() -> str:
    """The config-generation fake talosctl of the v0-a tests."""
    spec = importlib.util.spec_from_file_location(
        "talos_box_tests", os.path.join(REPO, "tests", "test_talos_unattended_box.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FAKE_TALOSCTL


def _host_world(tmp_path) -> dict:
    """A shared host with unrelated networks, before the smoke project exists."""
    return {
        "domains": {},
        "other_domains": ["someone-elses-vm", "talos-unattended-control-base"],
        "networks": {
            "default": {"address": "192.168.122.1", "bridge": "virbr0"},
            "other-lab": {"address": "198.51.100.1", "bridge": "virbr9"},
        },
        "host_addrs": [
            "1: lo    inet 127.0.0.1/8 scope host lo",
            "2: eth0    inet 203.0.113.7/32 scope global eth0",
            "3: virbr0    inet 192.168.122.1/24 brd 192.168.122.255 scope global virbr0",
        ],
        "host_routes": [
            "default via 203.0.113.1 dev eth0 proto static",
            "192.168.122.0/24 dev virbr0 proto kernel scope link src 192.168.122.1",
            "blackhole 10.99.0.0/16 proto static",
        ],
        "df_avail_kib": 500 * (1 << 20),
    }


@pytest.fixture
def host(tmp_path):
    """Fakes on PATH, a world, a private inputs file and an operator key."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("virsh", "ip", "df", "boxman", "ssh", "talosctl", "kubectl", "flux", "curl"):
        wrapper = bin_dir / tool
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKES}" {tool} "$@"\n')
        wrapper.chmod(0o755)
    gen = tmp_path / "talosctl-gen"
    gen.write_text(_gen_fake().replace("@PYTHON@", sys.executable))
    gen.chmod(0o755)
    python3 = bin_dir / "python3"
    python3.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python3.chmod(0o755)

    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    key = keys / "operator"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "op@example", "-f", str(key)],
                   check=True)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal: {128 << 20} kB\nMemAvailable: {64 << 20} kB\n")
    world_path = tmp_path / "world.json"
    world_path.write_text(json.dumps(_host_world(tmp_path)))
    log = tmp_path / "calls.log"
    lab = tmp_path / "lab"
    inputs = tmp_path / "inputs.env"
    inputs.write_text("\n".join([
        f"TALOS_PROJECT={PROJECT}",
        "TALOS_PROFILE=smoke",
        "TALOS_LIBVIRT_USE_SUDO=false",
        f"TALOS_LAB_DIR={lab}",
        "TALOS_LAB_PREFIX=192.168.53",
        f"TALOS_OPERATOR_KEY={key}",
        f'TALOS_OPERATOR_PUBKEY="{(keys / "operator.pub").read_text().strip()}"',
        f"TALOS_BACKUP_DIR={tmp_path / 'backup'}",
        f"TALOS_STATE_DIR={tmp_path / 'control-home' / 'state'}",
        "TALOS_CLUSTER_NAME=lab",
        "TALOS_INSTALL_DISK=/dev/vda",
        "TALOS_VIP=192.168.53.5",
        'TALOS_NTP_SERVERS="ntp1.example ntp2.example"',
        "TALOS_POD_CIDR=10.244.0.0/16",
        "TALOS_SERVICE_CIDR=10.96.0.0/12",
        "",
    ]))
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONPATH": os.path.join(REPO, "src"),
        "TALOS_PYTHON": sys.executable,
        "TALOS_MEMINFO": str(meminfo),
        "BOXMAN_APP_CONF": str(tmp_path / "no-boxman.yml"),
        "FAKE_WORLD": str(world_path),
        "FAKE_LOG": str(log),
        "FAKE_TALOSCTL_GEN": str(gen),
        "FAKE_TALOS_VERSION": TALOS_VERSION,
        "TALOS_POLL_SECONDS": "0",
        "TMPDIR": str(tmp_path),
    }

    class Host:
        root = tmp_path
        bin = bin_dir
        key_path = str(key)
        inputs_file = inputs
        lab_dir = lab
        backup = tmp_path / "backup"
        driver = lab / "driver"

        def world(self) -> dict:
            return json.loads(world_path.read_text())

        def set_world(self, **changes) -> dict:
            world = self.world()
            world.update(changes)
            world_path.write_text(json.dumps(world))
            return world

        def edit(self, fn) -> None:
            world = self.world()
            fn(world)
            world_path.write_text(json.dumps(world))

        def run(self, script, *args, stdin=None, **overrides):
            e = dict(env, **{k: v for k, v in overrides.items() if v is not None})
            for k in [k for k, v in overrides.items() if v is None]:
                e.pop(k, None)
            return subprocess.run(["bash", os.path.join(SCRIPTS, script), "--inputs", str(inputs), *args],
                                  capture_output=True, text=True, env=e, input=stdin, timeout=120,
                                  cwd=BOX)

        def run_boxman(self, *args, extra=None):
            """boxman itself (the fake), as an operator runs it."""
            e = dict(env, **(extra or {}))
            return subprocess.run([str(bin_dir / "boxman"), *args], capture_output=True, text=True,
                                  env=e, timeout=180, cwd=BOX)

        def calls(self, tool=None) -> list[str]:
            lines = log.read_text().splitlines() if log.exists() else []
            return [line for line in lines if tool is None or line.split(" ", 1)[0] == tool]

        def meminfo(self, avail_kib: int) -> None:
            meminfo.write_text(f"MemTotal: {128 << 20} kB\nMemAvailable: {avail_kib} kB\n")

    return Host()


def _ok(proc):
    assert proc.returncode == 0, proc.stderr
    return proc


def _refused(proc, message, status=1):
    assert proc.returncode == status, (proc.returncode, proc.stderr)
    assert message in proc.stderr, proc.stderr
    return proc


# --------------------------------------------------------------------------- #
# preflight                                                                   #
# --------------------------------------------------------------------------- #
def test_preflight_passes_on_a_free_host_and_changes_nothing(host):
    before = host.world()
    out = _ok(host.run("preflight.sh")).stderr
    assert "ok: topology: smoke profile, 1 control VM + 1 control plane(s) + 1 worker(s)" in out
    assert "ok: ownership: new project lab353" in out
    assert "ok: network: fabric 192.168.53.1/24 overlaps none of 2 other libvirt networks" in out
    assert "ready: project lab353, smoke profile" in out
    assert host.world() == before
    assert not host.calls("boxman") and not host.calls("ssh")
    assert not host.lab_dir.exists() and not host.backup.exists()
    # read-only libvirt queries only
    verbs = {line.split()[3] for line in host.calls("virsh")}
    assert verbs <= {"list", "net-list", "net-dumpxml", "domstate", "domuuid"}


@pytest.mark.parametrize("failing,message", [
    ("virsh list", "cannot list the libvirt domains"),
    ("virsh net-list", "cannot list the libvirt networks"),
    ("virsh net-dumpxml other-lab", "cannot read the libvirt network other-lab"),
    ("ip addr", "cannot list the host's IPv4 addresses"),
    ("ip route", "cannot list the host's IPv4 routes"),
])
def test_a_failed_observation_stops_preflight(host, failing, message):
    """Review B1: a listing that failed must never read as "no overlap"."""
    host.set_world(fail=[failing])
    proc = _refused(host.run("preflight.sh"), message)
    assert "ready:" not in proc.stderr


@pytest.mark.parametrize("entry,text,message", [
    ("virsh net-dumpxml other-lab", "<network><ip address='198.51.100.1' netmask='255.0.255.0'/></network>",
     "cannot parse the libvirt network other-lab"),
    ("virsh net-dumpxml other-lab", "not xml at all", "cannot parse the libvirt network other-lab"),
    ("virsh net-dumpxml other-lab", "<domain/>", "cannot parse the libvirt network other-lab"),
    ("virsh net-dumpxml other-lab", "<network><ip address='198.51.100.999' prefix='24'/></network>",
     "cannot parse the address 198.51.100.999/24"),
    ("ip addr", "2: eth0    inet6 fe80::1/64 scope link\n", "cannot parse the host address line"),
    ("ip addr", "", "cannot find any host IPv4 address"),
    ("ip route", "10.0.0.0/33 dev eth0\n", "cannot parse the host route"),
    ("ip route", "garbage-route dev eth0\n", "cannot parse the host route"),
])
def test_an_unreadable_observation_stops_preflight(host, entry, text, message):
    """Review B1: a partial or unparseable inventory is a refusal too."""
    host.set_world(garbage={entry: text})
    proc = _refused(host.run("preflight.sh"), message)
    assert "ready:" not in proc.stderr


@pytest.mark.parametrize("change,message", [
    (lambda w: w["networks"].update(clash={"address": "192.168.53.1", "bridge": "virbr7"}),
     "overlaps libvirt network clash (192.168.53.1/24)"),
    (lambda w: w["networks"].update(wide={"xml": "<network><ip address='192.168.0.1' prefix='16'/></network>"}),
     "overlaps libvirt network wide (192.168.0.1/16)"),
    (lambda w: w["host_addrs"].append("9: docker0    inet 192.168.48.1/20 scope global docker0"),
     "overlaps 192.168.48.1/20 on host interface docker0"),
    (lambda w: w["host_routes"].append("192.168.53.0/24 via 203.0.113.9 dev eth0"),
     "overlaps the host route 192.168.53.0/24 (dev eth0)"),
    (lambda w: w["host_routes"].append("unreachable 192.168.53.64/26"),
     "overlaps the host route 192.168.53.64/26"),
])
def test_preflight_finds_every_kind_of_network_conflict(host, change, message):
    host.edit(change)
    _refused(host.run("preflight.sh"), message)


def test_a_failed_memory_reading_stops_preflight(host):
    host.root.joinpath("meminfo").write_text("MemTotal: 1 kB\n")
    _refused(host.run("preflight.sh"), "cannot read MemAvailable")


def test_a_failed_domain_state_reading_is_not_absence(host):
    host.edit(lambda w: w["domains"].update({CP1: {"uuid": "u1", "state": "running"}}))
    host.set_world(fail=["virsh domstate"])
    _refused(host.run("preflight.sh"), f"cannot read the state of {CP1}")


def test_preflight_never_adopts_existing_domains(host):
    host.edit(lambda w: w["domains"].update({CP1: {"uuid": "u1", "state": "shut off"}}))
    _refused(host.run("preflight.sh"), "A run never starts or adopts what it cannot prove it created")


def test_preflight_never_adopts_an_existing_network(host):
    host.edit(lambda w: w["networks"].update({NETWORK: {"address": "192.168.53.1"}}))
    _refused(host.run("preflight.sh"), f"these exist: {NETWORK}")


def test_a_new_cluster_refuses_a_backup_of_another_identity(host):
    (host.backup / "state").mkdir(parents=True)
    (host.backup / "state" / "secrets.yaml").write_text("x")
    _refused(host.run("preflight.sh"), "already backs up a cluster identity")


def test_preflight_refuses_too_little_memory(host):
    host.meminfo(14 << 20)    # 12 GiB of guests + 4 GiB headroom do not fit
    _refused(host.run("preflight.sh"), "memory: 12288 MiB for guests not running + 4096 MiB headroom")


def test_preflight_refuses_too_little_disk(host):
    host.set_world(df_avail_kib=100 * (1 << 20))
    _refused(host.run("preflight.sh"), "disk: 240 GiB of virtual disks to create > 100 GiB free")


def test_the_full_profile_passes_the_same_checks(host):
    with open(host.inputs_file, "a") as fobj:
        fobj.write("TALOS_PROFILE=full\n")
    _refused(host.run("preflight.sh"), "disk: 640 GiB of virtual disks to create > 500 GiB free")
    host.set_world(df_avail_kib=1 << 30)
    out = _ok(host.run("preflight.sh")).stderr
    assert "1 control VM + 3 control plane(s) + 3 worker(s)" in out
    assert "memory: 32768 MiB for guests not running" in out


def test_an_unknown_profile_is_refused(host):
    with open(host.inputs_file, "a") as fobj:
        fobj.write("TALOS_PROFILE=tiny\n")
    _refused(host.run("preflight.sh"), "TALOS_PROFILE must be full or smoke")


@pytest.mark.parametrize("var,value,message", [
    ("TALOS_BACKUP_DIR", "", "TALOS_BACKUP_DIR is not set"),
    ("TALOS_STATE_DIR", "PLACEHOLDER-x", "TALOS_STATE_DIR is still a placeholder"),
    ("TALOS_OPERATOR_PUBKEY", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKey op@example",
     "TALOS_OPERATOR_PUBKEY is not the public half"),
    ("TALOS_VIP", "10.0.0.5", "TALOS_VIP 10.0.0.5 is not inside the fabric"),
    ("TALOS_LIBVIRT_USE_SUDO", "maybe", "TALOS_LIBVIRT_USE_SUDO must be true or false"),
])
def test_bad_inputs_are_refused(host, var, value, message):
    with open(host.inputs_file, "a") as fobj:
        fobj.write(f"{var}='{value}'\n")
    _refused(host.run("preflight.sh"), message)


def test_a_backup_inside_the_workspace_or_a_checkout_is_refused(host):
    with open(host.inputs_file, "a") as fobj:
        fobj.write(f"TALOS_BACKUP_DIR={host.lab_dir}/backup\n")
    _refused(host.run("preflight.sh"), "inside the workspace")
    repo = host.root / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    with open(host.inputs_file, "a") as fobj:
        fobj.write(f"TALOS_BACKUP_DIR={repo}/backup\n")
    _refused(host.run("preflight.sh"), "is inside a git checkout")


def test_an_operator_key_others_can_read_is_refused(host):
    os.chmod(host.key_path, 0o644)
    _refused(host.run("preflight.sh"), "is readable by others")


def test_a_pin_that_breaks_the_tested_set_is_refused(host):
    _refused(host.run("preflight.sh", TALOS_VERSION="v1.14.6"), "TALOS_ISO_URI must be an https URL")
    _refused(host.run("preflight.sh", TALOSCTL_SHA256="abc"), "TALOSCTL_SHA256 must be a sha256")


def test_a_non_local_runtime_is_refused(host):
    conf = host.root / "boxman.yml"
    conf.write_text("runtime: docker-compose\n")
    _refused(host.run("preflight.sh", BOXMAN_APP_CONF=str(conf)), "v0 supports the local runtime only")


FLUX_INPUTS = {"GITLAB_HOST": "gitlab.example", "GITLAB_OWNER": "lab-group",
               "GITOPS_REPOSITORY": "fleet", "GITOPS_BRANCH": "main",
               "GITOPS_CLUSTER_PATH": "./clusters/lab", "GITLAB_TOKEN": "glpat-FAKESECRET"}


def test_flux_is_off_by_default(host):
    assert "ok: flux: off" in _ok(host.run("preflight.sh")).stderr


@pytest.mark.parametrize("missing", sorted(FLUX_INPUTS))
def test_flux_on_without_its_target_or_credential_fails_before_anything(host, missing):
    env = dict(FLUX_INPUTS, TALOS_FLUX="on")
    env.pop(missing)
    _refused(host.run("preflight.sh", **env), f"TALOS_FLUX=on needs {missing}")
    assert not host.calls("boxman") and not host.lab_dir.exists()


def test_flux_on_with_everything_passes_and_never_prints_the_token(host):
    proc = _ok(host.run("preflight.sh", TALOS_FLUX="on", **FLUX_INPUTS))
    assert "flux: on, gitlab.example/lab-group/fleet main ./clusters/lab" in proc.stderr
    assert "FAKESECRET" not in proc.stderr + proc.stdout


@pytest.mark.parametrize("var,value", [
    ("GITOPS_CLUSTER_PATH", "/abs/path"), ("GITOPS_CLUSTER_PATH", "./clusters/../other"),
    ("GITOPS_BRANCH", "main..evil"), ("GITLAB_HOST", "gitlab.example/x"),
])
def test_flux_targets_are_checked(host, var, value):
    env = dict(FLUX_INPUTS, TALOS_FLUX="on", **{var: value})
    _refused(host.run("preflight.sh", **env), "flux:")


def _set_inputs(host, **values) -> None:
    with open(host.inputs_file, "a") as fobj:
        for var, value in values.items():
            fobj.write(f"{var}='{value}'\n")


def test_a_backup_reached_through_a_symlink_into_the_workspace_is_refused(host):
    """Review B2: the comparison is physical, not lexical."""
    host.lab_dir.mkdir()
    outside = host.root / "outside"
    outside.mkdir()
    (outside / "link").symlink_to(host.lab_dir)
    _set_inputs(host, TALOS_BACKUP_DIR=f"{outside}/link/backup")
    _refused(host.run("preflight.sh"), f"is {host.lab_dir}/backup, inside the workspace")


def test_a_backup_path_with_dot_components_is_refused(host):
    (host.root / "outside").mkdir()
    _set_inputs(host, TALOS_BACKUP_DIR=f"{host.root}/outside/../lab/backup")
    _refused(host.run("preflight.sh"), "has . or .. components")


def test_a_workspace_reached_through_a_symlink_still_contains_its_backup(host):
    real = host.root / "real-lab"
    real.mkdir()
    link = host.root / "lab-link"
    link.symlink_to(real)
    _set_inputs(host, TALOS_LAB_DIR=str(link), TALOS_BACKUP_DIR=f"{real}/backup")
    _refused(host.run("preflight.sh"), f"is {real}/backup, inside the workspace ({real})")


def test_a_workspace_inside_the_backup_is_refused(host):
    _set_inputs(host, TALOS_BACKUP_DIR=str(host.root))
    _refused(host.run("preflight.sh"), "lies inside TALOS_BACKUP_DIR")


def test_a_backup_symlinked_to_a_place_outside_the_workspace_passes(host):
    elsewhere = host.root / "vault"
    elsewhere.mkdir()
    (host.root / "backup-link").symlink_to(elsewhere)
    _set_inputs(host, TALOS_BACKUP_DIR=f"{host.root}/backup-link/lab")
    _ok(host.run("preflight.sh"))


# --------------------------------------------------------------------------- #
# the whole driver: install.sh against a fake lab                             #
# --------------------------------------------------------------------------- #
CONTROL_MAC = "52:54:00:aa:bb:cc"
CONTROL_IP = "192.168.53.150"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyOfTheControlVm"
NODES = {
    "cp-01": ("controlplane", "192.168.53.11", "52:54:00:53:00:11", 50 * GIB),
    "worker-01": ("worker", "192.168.53.21", "52:54:00:53:00:21", 150 * GIB),
}


def _arrange_lab(host) -> None:
    """What `boxman up` creates: the control VM and two maintenance-mode nodes."""
    lab = str(host.lab_dir)
    creates = {CONTROL: {
        "uuid": "uuid-control", "nics": [{"source": NETWORK, "mac": CONTROL_MAC}],
        "disks": [{"device": "disk", "target": "vda", "source": f"{lab}/management/{CONTROL}.qcow2",
                   "capacity": 40 * GIB}],
        "agent": {"files": {"/etc/ssh/ssh_host_ed25519_key.pub": HOST_KEY + " root@control\n"}},
    }}
    leases = [{"mac": CONTROL_MAC, "ip": CONTROL_IP}]
    talos = {"vip": "192.168.53.5", "nodes": {}}
    for name, (role, addr, mac, size) in NODES.items():
        dom = f"{PREFIX}_talos_{name}"
        creates[dom] = {
            "uuid": f"uuid-{name}", "nics": [{"source": NETWORK, "mac": mac}],
            "disks": [{"device": "disk", "target": "vda", "source": f"{lab}/talos/{dom}.qcow2", "capacity": size},
                      {"device": "cdrom", "target": "sda", "source": "/cache/talos.iso", "capacity": 366292992}],
        }
        leases.append({"mac": mac, "ip": addr})
        talos["nodes"][addr] = {
            "name": name, "role": role, "mode": "maintenance",
            "links": [{"id": "ens3", "permanentAddr": mac, "hardwareAddr": mac}],
            "disks": [{"id": "vda", "dev_path": "/dev/vda", "size": size, "readonly": False, "cdrom": False},
                      {"id": "sr0", "dev_path": "/dev/sr0", "size": 366292992, "readonly": True, "cdrom": True}],
            "volumes": [{"id": "sr0", "dev_path": "/dev/sr0", "parent": "", "name": "iso9660"},
                        {"id": "vda", "dev_path": "/dev/vda", "parent": "", "name": ""}],
        }
    host.set_world(
        boxman={"creates": creates, "networks": {NETWORK: {"address": "192.168.53.1", "bridge": "virbr53"}},
                "leases": {NETWORK: leases}},
        control={"user": "admin", "ip": CONTROL_IP, "host_key": HOST_KEY, "key_path": host.key_path,
                 "home": str(host.root / "control-home")},
        talos=talos, kubectl_version=PINS["KUBECTL_VERSION"], flux_version=PINS["FLUX_VERSION"],
    )


@pytest.fixture
def lab(host):
    _arrange_lab(host)
    return host


STATE = "control-home/state"


def _talos_calls(host, *words) -> list[str]:
    return [c for c in host.calls("talosctl") if all(w in c.split() for w in words)]


HOOK_FAILED = re.compile(r"hook '[^']+' failed \(exit (\d+)\)")


def _install(host, **env):
    """make install: boxman up, whose pre_up/post_up hooks run the driver.

    boxman reports a failed hook as its own exit status 2, naming the hook's
    status in the message; the returned returncode is the hook's, so that
    tests can tell a failure (1) from drift (3) as before.
    """
    proc = host.run("install.sh", **env)
    match = HOOK_FAILED.search(proc.stderr)
    if proc.returncode == 2 and match:
        proc.returncode = int(match.group(1))
    return proc


def test_install_converges_from_nothing(lab):
    proc = _ok(_install(lab))
    err = proc.stderr
    assert "install: converged: project lab353, smoke profile" in err
    # the order of the stages
    stages = [line.split("== ", 1)[1].split(":")[0] for line in err.splitlines() if "install: == " in line]
    assert stages == ["preflight", "ownership", "control", "nodes", "ship", "restore", "generate",
                      "apply", "bootstrap", "status"]
    assert "ownership: boxman created the 3 VMs and the network in this up" in err
    world = lab.world()
    assert world["boxman"]["calls"] == 1
    # each node got its config once, through the maintenance API, then authenticated
    assert len(_talos_calls(lab, "apply-config")) == 2
    assert all("--insecure" in c for c in _talos_calls(lab, "apply-config"))
    assert [n["mode"] for n in world["talos"]["nodes"].values()] == ["installed", "installed"]
    # etcd bootstrapped exactly once, on the control plane's own address
    assert world["talos"]["bootstrap_calls"] == 1
    assert _talos_calls(lab, "bootstrap") == [
        f"talosctl --talosconfig {lab.root}/{STATE}/talosconfig -e 192.168.53.11 -n 192.168.53.11 bootstrap"]
    # no Talos API call ever went through the VIP
    assert not [c for c in lab.calls("talosctl") if "192.168.53.5" in c.split()]
    # the permanent_addr check ran and was recorded
    assert "link ens3 has permanent_addr 52:54:00:53:00:11 (the reserved MAC)" in err
    # status from the control VM
    assert "kube       2/2 nodes Ready" in proc.stdout and "test pod: smoke-1 Running worker-01" in proc.stdout
    # the private state: backup, record, kubeconfig
    backup = lab.backup / "state"
    assert (backup / "secrets.yaml").exists() and (backup / "journal" / "cp-01.applied").exists()
    assert oct(os.stat(lab.backup).st_mode & 0o777) == "0o700"
    kubeconfig = lab.driver / "kubeconfig"
    assert oct(os.stat(kubeconfig).st_mode & 0o777) == "0o600"
    record = (lab.driver / "run.env").read_text()
    assert "uuid.control=uuid-control" in record and "uuid.cp-01=uuid-cp-01" in record
    assert "identity=" in record


def test_install_prints_no_secret(lab):
    proc = _ok(_install(lab))
    assert "FAKESECRET" not in proc.stdout + proc.stderr


def test_a_second_run_converges_without_touching_identity_configs_or_etcd(lab):
    _ok(_install(lab))
    secrets = (lab.root / STATE / "secrets.yaml").read_bytes()
    record = (lab.driver / "run.env").read_text()
    calls = len(lab.calls("talosctl"))
    proc = _ok(_install(lab))
    new = lab.calls("talosctl")[calls:]
    assert not [c for c in new if "apply-config" in c or c.endswith(" bootstrap") or "gen secrets" in c]
    assert not [c for c in new if "--insecure" in c]
    assert (lab.root / STATE / "secrets.yaml").read_bytes() == secrets
    assert (lab.driver / "run.env").read_text() == record
    assert lab.world()["talos"]["bootstrap_calls"] == 1
    assert "cp-01: recorded" in proc.stdout and "worker-01: recorded" in proc.stdout
    assert "etcd: bootstrapped earlier" in proc.stderr
    assert "kubeconfig: keeping" in proc.stderr
    assert "cp-01: unchanged" in proc.stdout


def test_a_concurrent_install_is_refused_before_it_changes_anything(lab):
    """A second boxman up of the project while one runs (it holds boxman's
    up lock): no hook, no infrastructure change, nothing reaches a VM."""
    import fcntl
    lock = lab.root / ".config" / "boxman" / "cache" / "locks" / f"up-{PROJECT}.lock"
    lock.parent.mkdir(parents=True)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        proc = lab.run("install.sh")
    assert proc.returncode == 2
    assert f"another boxman up of project '{PROJECT}' is running" in proc.stderr
    assert "preflight:" not in proc.stderr
    assert "calls" not in lab.world()["boxman"]
    assert not lab.calls("ssh") and not lab.calls("talosctl")


def test_the_driver_lock_refuses_a_second_post_up(lab):
    import fcntl
    lab.driver.mkdir(parents=True)
    with open(lab.driver / ".install.lock", "w") as fobj:
        fcntl.flock(fobj, fcntl.LOCK_EX)
        _refused(_install(lab), "another install.sh holds")
    assert not lab.calls("ssh") and not (lab.driver / "run.env").exists()


# ---- preflight and up --------------------------------------------------------
def test_a_failed_preflight_stops_before_boxman(lab):
    lab.meminfo(1 << 20)
    _refused(_install(lab), "pre_up hook 'talos-preflight' failed")
    assert "calls" not in lab.world()["boxman"]
    assert not lab.backup.exists()


def test_a_failed_boxman_up_stops_the_run(lab):
    lab.edit(lambda w: w["boxman"].update(rc=1))
    _refused(_install(lab), "fake boxman: up failed")
    assert not lab.calls("ssh")


def test_a_recreated_vm_is_drift(lab):
    _ok(_install(lab))
    lab.edit(lambda w: w["domains"][W1].update(uuid="someone-else"))
    _refused(_install(lab), "is not the domain this driver created", status=1)
    assert len(_talos_calls(lab, "apply-config")) == 2


# ---- control -----------------------------------------------------------------
def test_ssh_is_refused_when_the_host_key_is_not_the_guests(lab):
    """The pinned key comes from the guest agent; a different server key fails."""
    lab.edit(lambda w: w["control"].update(host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAnImpostor"))
    _refused(_install(lab, TALOS_CONTROL_TIMEOUT="1"), "Host key verification failed")
    assert not (lab.root / STATE).exists()


def test_a_control_vm_without_its_agent_is_not_trusted(lab):
    lab.edit(lambda w: w["boxman"]["creates"][CONTROL].pop("agent"))
    _refused(_install(lab, TALOS_CONTROL_TIMEOUT="1"), "guest agent")
    assert not lab.calls("ssh")


def test_ssh_uses_only_the_operator_key(lab):
    other = lab.root / "keys" / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    lab.edit(lambda w: w["control"].update(key_path=str(other)))
    _refused(_install(lab, TALOS_CONTROL_TIMEOUT="1"), "Permission denied (publickey)")


def test_unpinned_control_tools_stop_the_run(lab):
    lab.set_world(flux_version="v2.9.0")
    _refused(_install(lab), f"flux is v2.9.0, the version set pins {PINS['FLUX_VERSION']}")
    assert not _talos_calls(lab, "gen", "secrets")


def test_control_exec_quotes_arguments_for_the_remote_shell(lab):
    _ok(_install(lab))
    proc = _ok(lab.run("control-exec.sh", "run", "--", "printf", "%s|", "a b", "$HOME", "'q'"))
    assert proc.stdout == "a b|$HOME|'q'|"


# ---- nodes -------------------------------------------------------------------
@pytest.mark.parametrize("change,message", [
    (lambda w: w["boxman"]["leases"][NETWORK][1].update(ip="192.168.53.111"),
     "cp-01: the lease for 52:54:00:53:00:11 is 192.168.53.111, not the reserved 192.168.53.11"),
    (lambda w: w["boxman"]["creates"][CP1]["nics"][0].update(mac="52:54:00:53:00:99"),
     "cp-01: expected exactly one NIC"),
    (lambda w: w["boxman"]["creates"][CP1]["nics"].append({"source": "other", "mac": "52:54:00:00:00:01"}),
     "cp-01: expected exactly one NIC"),
    (lambda w: w["boxman"]["creates"][CP1]["disks"][0].update(source="/var/lib/libvirt/images/ceph.qcow2"),
     "cp-01: its boot disk /var/lib/libvirt/images/ceph.qcow2 is not in this project's workspace"),
    (lambda w: w["boxman"]["creates"][W1]["disks"][0].update(capacity=100 * GIB),
     "worker-01: its boot disk is 107374182400 bytes"),
])
def test_node_identity_is_checked_before_anything_reaches_a_node(lab, change, message):
    lab.edit(change)
    _refused(_install(lab), message, status=3)
    assert not _talos_calls(lab, "apply-config")
    assert not (lab.root / STATE).exists()


def test_a_node_without_a_lease_times_out(lab):
    lab.edit(lambda w: w["boxman"]["leases"][NETWORK].pop())
    _refused(_install(lab, TALOS_LEASE_TIMEOUT="1"), "worker-01: no DHCP lease for 52:54:00:53:00:21")
    assert not _talos_calls(lab, "apply-config")


# ---- apply -------------------------------------------------------------------
def _node(addr):
    return lambda w: w["talos"]["nodes"][addr]


def _edit_node(host, addr, **changes):
    host.edit(lambda w: w["talos"]["nodes"][addr].update(changes))


@pytest.mark.parametrize("changes,message", [
    ({"links": [{"id": "ens3", "permanentAddr": "", "hardwareAddr": "52:54:00:53:00:21"}]},
     "carries 52:54:00:53:00:21 as its hardware address but not as permanent_addr"),
    ({"links": [{"id": "ens3", "permanentAddr": "52:54:00:99:99:99", "hardwareAddr": "52:54:00:99:99:99"}]},
     "no link has the reserved MAC 52:54:00:53:00:21"),
    ({"disks": [{"id": "vda", "dev_path": "/dev/vda", "size": 100 * GIB, "readonly": False, "cdrom": False}]},
     "/dev/vda is 107374182400 bytes, not the 161061273600-byte disk"),
    ({"disks": [{"id": "vdb", "dev_path": "/dev/vdb", "size": 150 * GIB, "readonly": False, "cdrom": False}]},
     "its one writable disk is /dev/vdb, not /dev/vda"),
    ({"disks": [{"id": "vda", "dev_path": "/dev/vda", "size": 150 * GIB, "readonly": False, "cdrom": False},
                {"id": "vdb", "dev_path": "/dev/vdb", "size": 150 * GIB, "readonly": False, "cdrom": False}]},
     "expected exactly one writable disk"),
    ({"volumes": [{"id": "vda", "dev_path": "/dev/vda", "parent": "", "name": ""},
                  {"id": "vda1", "dev_path": "/dev/vda1", "parent": "vda", "name": "xfs"}]},
     "/dev/vda is not empty (/dev/vda1)"),
    ({"volumes": [{"id": "vda", "dev_path": "/dev/vda", "parent": "", "name": "gpt"}]},
     "/dev/vda is not empty (/dev/vda)"),
])
def test_a_node_whose_link_or_disk_is_not_the_expected_one_gets_nothing(lab, changes, message):
    _edit_node(lab, "192.168.53.21", **changes)
    _refused(_install(lab), message, status=3)
    applied = _talos_calls(lab, "apply-config")
    assert all("192.168.53.21" not in c for c in applied)
    # the backup kept what was done, and a rerun after the fix converges
    assert (lab.backup / "state" / "journal").is_dir()


def test_without_a_volume_listing_the_hosts_creation_evidence_is_required(lab):
    """Review B4: no listing and no host evidence means no install."""
    _edit_node(lab, "192.168.53.11", volumes_forbidden=True)
    proc = _ok(_install(lab))
    assert "the host saw nothing written to it since boxman created it" in proc.stderr


def test_without_a_volume_listing_a_written_disk_gets_nothing(lab):
    _edit_node(lab, "192.168.53.11", volumes_forbidden=True)
    # a first run records the disks after up, then stops (unpinned flux)
    lab.set_world(flux_version="v0.0.0")
    _refused(_install(lab), "flux is v0.0.0")
    # something writes to cp-01's disk before the next run
    lab.edit(lambda w: w["domains"][CP1]["disks"][0].update(allocation=64 << 20))
    lab.set_world(flux_version=PINS["FLUX_VERSION"])
    _refused(_install(lab), "no evidence /dev/vda is unwritten")
    assert not [c for c in _talos_calls(lab, "apply-config") if "192.168.53.11" in c]


@pytest.mark.parametrize("entry,text,message", [
    ("talosctl get discoveredvolumes", "garbage\n", "cannot read the maintenance API's volume listing"),
    ("talosctl get discoveredvolumes", "", "volume listing does not name /dev/vda"),
    ("talosctl get disks", "- not a listing\n", "cannot list or read its disks"),
])
def test_an_unreadable_disk_or_volume_listing_gets_no_install(lab, entry, message, text):
    lab.set_world(garbage={entry: text})
    _refused(_install(lab), message)
    assert not _talos_calls(lab, "apply-config")


def test_a_failed_apply_is_retried_only_while_the_node_is_unconfigured(lab):
    lab.edit(lambda w: w["talos"].update(apply_fail={"192.168.53.21": "error"}))
    _refused(_install(lab), "worker-01: apply-config failed")
    assert (lab.root / STATE / "journal" / "worker-01.applying").exists()
    lab.edit(lambda w: w["talos"].pop("apply_fail"))
    proc = _ok(_install(lab))
    assert "worker-01: an earlier apply never took effect" in proc.stderr
    assert lab.world()["talos"]["nodes"]["192.168.53.21"]["applies"] == 2


def test_an_apply_whose_answer_was_lost_is_recovered_not_resent(lab):
    lab.edit(lambda w: w["talos"].update(apply_fail={"192.168.53.21": "lost"}))
    _refused(_install(lab), "worker-01: apply-config failed")
    lab.edit(lambda w: w["talos"].pop("apply_fail"))
    proc = _ok(_install(lab))
    assert "worker-01: recovered" in proc.stdout
    assert lab.world()["talos"]["nodes"]["192.168.53.21"]["applies"] == 1


def test_an_installed_node_that_stops_authenticating_is_never_retried_insecurely(lab):
    _ok(_install(lab))
    insecure = len([c for c in lab.calls("talosctl") if "--insecure" in c])
    _edit_node(lab, "192.168.53.21", auth="reject")
    _refused(_install(lab, TALOS_AUTH_TIMEOUT="1"), "not retrying in insecure mode")
    assert len([c for c in lab.calls("talosctl") if "--insecure" in c]) == insecure


def test_a_node_reset_to_maintenance_after_install_is_not_reinstalled(lab):
    _ok(_install(lab))
    _edit_node(lab, "192.168.53.21", mode="maintenance")
    _refused(_install(lab, TALOS_AUTH_TIMEOUT="1"), "worker-01 was configured by this cluster")
    assert lab.world()["talos"]["nodes"]["192.168.53.21"]["applies"] == 1


def test_a_changed_config_on_an_installed_node_is_drift_not_an_update(lab, tmp_path):
    _ok(_install(lab))
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "worker.yaml").write_text("apiVersion: v1alpha1\nkind: KubeletConfig\nextraArgs:\n  v: '2'\n")
    with open(lab.inputs_file, "a") as fobj:
        fobj.write(f"TALOS_PATCH_DIR={patches}\n")
    proc = _refused(_install(lab), "worker-01: its config changed since it was applied", status=3)
    assert "v0 does not update installed nodes" in proc.stderr
    assert lab.world()["talos"]["nodes"]["192.168.53.21"]["applies"] == 1


def test_an_unconfigured_looking_node_that_authenticates_is_left_alone(lab):
    """No journal, but the node already trusts this CA: state was lost."""
    _ok(_install(lab))
    (lab.root / STATE / "journal" / "worker-01.applied").unlink()
    (lab.backup / "state" / "journal" / "worker-01.applied").unlink()
    _refused(_install(lab, TALOS_MAINTENANCE_TIMEOUT="1"), "no record of configuring it", status=3)
    assert lab.world()["talos"]["nodes"]["192.168.53.21"]["applies"] == 1


def test_a_node_that_never_answers_times_out(lab):
    _edit_node(lab, "192.168.53.21", mode="off")
    _refused(_install(lab, TALOS_MAINTENANCE_TIMEOUT="1"), "worker-01: no maintenance API at 192.168.53.21")


# ---- bootstrap -----------------------------------------------------------------
def test_a_bootstrap_whose_answer_was_lost_is_recovered_not_repeated(lab):
    lab.edit(lambda w: w["talos"].update(bootstrap_fail="error-after"))
    _refused(_install(lab), "the bootstrap RPC to cp-01 failed")
    lab.edit(lambda w: w["talos"].pop("bootstrap_fail"))
    proc = _ok(_install(lab))
    assert "the earlier bootstrap took effect" in proc.stderr
    assert lab.world()["talos"]["bootstrap_calls"] == 1
    assert "recovered" in (lab.backup / "state" / "journal" / "bootstrap.done").read_text()


def test_an_unconfirmed_bootstrap_without_etcd_stops_instead_of_bootstrapping_again(lab):
    lab.edit(lambda w: w["talos"].update(bootstrap_fail="error-before"))
    _refused(_install(lab), "the bootstrap RPC to cp-01 failed")
    lab.edit(lambda w: w["talos"].pop("bootstrap_fail"))
    proc = _refused(_install(lab, TALOS_ETCD_TIMEOUT="1"), "Not bootstrapping again", status=3)
    assert "remove" in proc.stderr and "bootstrap.intent" in proc.stderr
    assert lab.world()["talos"]["bootstrap_calls"] == 1


def test_an_existing_etcd_is_never_bootstrapped(lab):
    _ok(_install(lab))
    for name in ("bootstrap.done", "bootstrap.intent"):
        for root in (lab.root / STATE, lab.backup / "state"):
            (root / "journal" / name).unlink(missing_ok=True)
    proc = _ok(_install(lab))
    assert "already reports members; the cluster exists, no bootstrap" in proc.stderr
    assert lab.world()["talos"]["bootstrap_calls"] == 1


def test_bootstrap_waits_for_time_sync_and_records_nothing_before(lab):
    _edit_node(lab, "192.168.53.11", synced=False)
    # one pre-bootstrap observation suffices here, so the 1 s bound is spent
    # on the time-sync wait this test is about
    _refused(_install(lab, TALOS_AUTH_TIMEOUT="1", TALOS_PREBOOT_CHECKS="1"), "cp-01's time is not synchronised")
    assert not (lab.root / STATE / "journal" / "bootstrap.intent").exists()
    assert "bootstrap_calls" not in lab.world()["talos"]


@pytest.mark.parametrize("kube,message", [
    ({"not_ready": ["worker-01"]}, "not every node is Ready"),
    ({"not_rolled_out": ["deployment/coredns"]}, "CoreDNS did not roll out"),
    ({"not_rolled_out": ["daemonset/kube-flannel"]}, "the CNI daemonset kube-flannel did not roll out"),
    ({"not_rolled_out": ["deployment/smoke"]}, "the test Deployment did not become available"),
    ({"smoke_pending": True}, "the test Deployment has no Running, scheduled pod"),
])
def test_an_unhealthy_cluster_fails_the_run_and_a_rerun_resumes(lab, kube, message):
    lab.set_world(kube=kube)
    _refused(_install(lab, TALOS_KUBE_TIMEOUT="1"), message)
    lab.set_world(kube={})
    _ok(_install(lab))
    assert lab.world()["talos"]["bootstrap_calls"] == 1


# ---- identity: restore, drift ------------------------------------------------
def test_a_control_vm_that_lost_its_state_gets_it_back_from_the_backup(lab):
    import shutil
    _ok(_install(lab))
    secrets = (lab.backup / "state" / "secrets.yaml").read_bytes()
    shutil.rmtree(lab.root / STATE)
    proc = _ok(_install(lab))
    assert "restored from" in proc.stderr
    assert (lab.root / STATE / "secrets.yaml").read_bytes() == secrets
    assert len(_talos_calls(lab, "gen", "secrets")) == 1
    assert lab.world()["talos"]["bootstrap_calls"] == 1


def test_an_identity_lost_everywhere_stops_the_run(lab):
    import shutil
    _ok(_install(lab))
    shutil.rmtree(lab.root / STATE)
    shutil.rmtree(lab.backup / "state")
    _refused(_install(lab), "neither the control VM nor", status=3)
    assert len(_talos_calls(lab, "gen", "secrets")) == 1


def test_a_changed_identity_on_the_control_vm_is_drift(lab):
    _ok(_install(lab))
    record = lab.driver / "run.env"
    record.write_text(record.read_text().replace("identity=", "identity=0000"))
    _refused(_install(lab), "the control VM's cluster identity changed", status=3)


# ---- status --------------------------------------------------------------------
def test_status_reports_a_healthy_lab_and_changes_nothing(lab):
    _ok(_install(lab))
    before = lab.world()
    proc = _ok(lab.run("status.sh"))
    assert "control    running, uuid as recorded" in proc.stdout
    assert "etcd       bootstrapped once" in proc.stdout
    assert lab.world()["talos"] == before["talos"]


def test_status_fails_when_a_node_is_down(lab):
    _ok(_install(lab))
    _edit_node(lab, "192.168.53.21", mode="off")
    proc = lab.run("status.sh")
    assert proc.returncode == 1
    assert "authenticated API DOES NOT answer" in proc.stdout


# ---- flux ----------------------------------------------------------------------
FLUX_ON = dict(FLUX_INPUTS, TALOS_FLUX="on")


def _gitlab(lab, projects=("lab-group/fleet",)):
    lab.edit(lambda w: (w.update(gitlab={"token": "glpat-FAKESECRET", "projects": list(projects)}),
                        w.setdefault("flux", {}).update(token="glpat-FAKESECRET")))


def test_flux_bootstraps_once_to_the_given_target_with_the_token_off_argv(lab):
    _gitlab(lab)
    proc = _ok(_install(lab, **FLUX_ON))
    flux = lab.world()["flux"]
    assert flux["bootstrap_calls"] == 1
    assert flux["installed"] == {"url": "https://gitlab.example/lab-group/fleet.git", "branch": "main",
                                 "path": "./clusters/lab"}
    assert "--deploy-token-auth" in flux["args"] and "--personal" not in flux["args"]
    assert "flux       root GitRepository and Kustomization Ready" in proc.stdout
    # the token never reaches an argv, the log or a shipped file
    assert not [c for c in lab.calls() if "FAKESECRET" in c]
    assert "FAKESECRET" not in proc.stdout + proc.stderr
    assert "glpat" not in (lab.root / "control-home" / "talos-unattended" / "control.env").read_text()
    # a rerun checks and leaves it alone
    _ok(_install(lab, **FLUX_ON))
    assert lab.world()["flux"]["bootstrap_calls"] == 1


def test_flux_never_creates_a_missing_repository(lab):
    _gitlab(lab, projects=())
    _refused(_install(lab, **FLUX_ON), "is not an existing project this credential can read")
    assert "bootstrap_calls" not in lab.world()["flux"]


def test_flux_bootstrapped_elsewhere_is_drift(lab):
    _gitlab(lab)
    _ok(_install(lab, **FLUX_ON))
    _refused(_install(lab, **dict(FLUX_ON, GITOPS_CLUSTER_PATH="./clusters/other")),
             "flux: already bootstrapped from", status=3)
    assert lab.world()["flux"]["bootstrap_calls"] == 1


def test_a_failed_flux_bootstrap_fails_the_run(lab):
    _gitlab(lab)
    lab.edit(lambda w: w["flux"].update(bootstrap_fail=True))
    _refused(_install(lab, **FLUX_ON), "flux: bootstrap failed")


def test_a_flux_that_does_not_become_ready_fails_the_run(lab):
    _gitlab(lab)
    lab.edit(lambda w: w["flux"].update(not_ready=True))
    _refused(_install(lab, **dict(FLUX_ON, TALOS_FLUX_TIMEOUT="1")), "flux: flux check failed")


def test_a_wrong_credential_fails_flux_and_is_not_printed(lab):
    _gitlab(lab)
    proc = _refused(_install(lab, **dict(FLUX_ON, GITLAB_TOKEN="glpat-WRONG")), "is not an existing project")
    assert "glpat-WRONG" not in proc.stdout + proc.stderr
    assert not [c for c in lab.calls() if "glpat" in c]


def test_flux_off_runs_no_flux_and_needs_no_credential(lab):
    _ok(_install(lab))
    assert not lab.calls("flux") or all("version" in c for c in lab.calls("flux"))
    assert not lab.calls("curl")


def test_flux_sh_refuses_to_run_unless_flux_is_on(lab):
    _ok(_install(lab))
    proc = lab.run("control-exec.sh", "run", "--", "talos-unattended/scripts/flux.sh",
                   "--inputs", "talos-unattended/control.env", stdin="glpat-FAKESECRET\n")
    _refused(proc, "Flux is opt-in")



# ---- review regressions: ownership (B6) ----------------------------------------
def test_a_failed_up_that_created_domains_blocks_every_later_run(lab):
    """B6: domains from an up that never finished are not provably ours."""
    lab.edit(lambda w: w["boxman"].update(fail_after_create=True))
    _refused(_install(lab), "up failed after creating the domains")
    assert not (lab.driver / "run.env").exists()     # no post_up hook ran
    assert json.loads((lab.lab_dir / "boxman-up-record.json").read_text())["status"] == "failed"
    lab.edit(lambda w: w["boxman"].update(fail_after_create=False))
    before = len(lab.calls())
    proc = _refused(_install(lab), "this driver has no record of project lab353")
    assert "pre_up hook 'talos-preflight' failed" in proc.stderr
    new = lab.calls()[before:]
    # boxman up is called, and its pre_up hook refuses before it changes
    # anything: no second up body, nothing reaches a VM
    assert not [c for c in new if c.split()[0] in ("ssh", "talosctl")]
    assert lab.world()["boxman"]["calls"] == 1


def test_an_incomplete_record_never_adopts_a_replaced_domain(lab):
    lab.edit(lambda w: w["boxman"].update(fail_after_create=True))
    _refused(_install(lab), "up failed after creating the domains")
    lab.edit(lambda w: (w["boxman"].update(fail_after_create=False),
                        w["domains"][W1].update(uuid="someone-elses")))
    _refused(_install(lab), "this driver has no record of project lab353")
    assert lab.world()["boxman"]["calls"] == 1


def test_an_up_that_created_nothing_may_be_retried(lab):
    lab.edit(lambda w: w["boxman"].update(rc=1))
    _refused(_install(lab), "fake boxman: up failed")
    lab.edit(lambda w: w["boxman"].pop("rc"))
    proc = _ok(_install(lab))
    assert "ok: ownership: new project lab353" in proc.stderr


def test_a_recreated_network_is_never_adopted(lab):
    _ok(_install(lab))
    lab.edit(lambda w: w["networks"][NETWORK].update(uuid="another-network"))
    _refused(_install(lab), f"{NETWORK} is not the network this driver created")
    assert lab.world()["boxman"]["calls"] == 1


def test_a_swapped_boot_disk_is_drift(lab):
    _ok(_install(lab))
    lab.edit(lambda w: w["domains"][W1]["disks"][0].update(source=f"{lab.lab_dir}/talos/other.qcow2"))
    _refused(_install(lab), "worker-01: its boot disk is", status=3)


def test_an_extra_disk_on_a_node_is_refused_on_the_host(lab):
    lab.edit(lambda w: w["boxman"]["creates"][W1]["disks"].insert(
        1, {"device": "disk", "target": "vdb", "source": "/data/ceph.qcow2", "capacity": 150 * GIB}))
    _refused(_install(lab), "expected exactly one disk, vda", status=3)
    assert not _talos_calls(lab, "apply-config")


def test_only_the_boot_disk_may_be_the_install_disk(host):
    with open(host.inputs_file, "a") as fobj:
        fobj.write("TALOS_INSTALL_DISK=/dev/vdb\n")
    _refused(host.run("preflight.sh"), "TALOS_INSTALL_DISK must be /dev/vda")


# ---- review regressions: credential destinations (B7) ------------------------
def _checkout(path):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def test_a_driver_directory_in_a_checkout_is_refused_before_any_write(lab):
    repo = _checkout(lab.root / "repo")
    _refused(_install(lab, TALOS_DRIVER_DIR=str(repo / "driver")), "is inside a git checkout")
    assert not (repo / "driver").exists()
    assert "calls" not in lab.world()["boxman"]


def test_a_workspace_reached_through_a_link_into_a_checkout_is_refused(lab):
    repo = _checkout(lab.root / "repo")
    (repo / "labs").mkdir()
    (lab.root / "lab-link").symlink_to(repo / "labs")
    with open(lab.inputs_file, "a") as fobj:
        fobj.write(f"TALOS_LAB_DIR={lab.root}/lab-link/lab\n")
    _refused(_install(lab), "is inside a git checkout")
    assert not list((repo / "labs").iterdir())


def test_a_restore_never_writes_into_a_checkout_on_the_control_vm(lab):
    import shutil
    _ok(_install(lab))
    shutil.rmtree(lab.root / STATE)
    repo = _checkout(lab.root / "control-home" / "repo")
    with open(lab.inputs_file, "a") as fobj:
        fobj.write(f"TALOS_STATE_DIR={repo}/state\n")
    _refused(_install(lab), "keep cluster secrets out of repositories")
    assert not (repo / "state").exists()


# ---- review regressions: bootstrap exactly once (B3) -------------------------
def _drop_bootstrap_journal(lab):
    for name in ("bootstrap.done", "bootstrap.intent"):
        for root in (lab.root / STATE, lab.backup / "state"):
            (root / "journal" / name).unlink(missing_ok=True)


def test_a_lost_journal_with_unhealthy_etcd_never_bootstraps_again(lab):
    _ok(_install(lab))
    _drop_bootstrap_journal(lab)
    lab.edit(lambda w: w["talos"].update(etcd_down=True))
    proc = _refused(_install(lab, TALOS_AUTH_TIMEOUT="1"), "cannot establish that etcd was never bootstrapped",
                    status=3)
    assert "cp-01=Failed" in proc.stderr
    assert lab.world()["talos"]["bootstrap_calls"] == 1


@pytest.mark.parametrize("state", ["Running", "Waiting", "Starting", ""])
def test_bootstrap_needs_etcd_positively_waiting_for_it(lab, state):
    _edit_node(lab, "192.168.53.11", etcd_state=state)
    _refused(_install(lab, TALOS_AUTH_TIMEOUT="1"), "cannot establish that etcd was never bootstrapped", status=3)
    assert "bootstrap_calls" not in lab.world()["talos"]


def test_an_unanswered_etcd_query_never_bootstraps(lab):
    lab.edit(lambda w: w.update(fail=["talosctl service etcd"]))
    _refused(_install(lab, TALOS_AUTH_TIMEOUT="1"), "cp-01=unknown", status=3)
    assert "bootstrap_calls" not in lab.world()["talos"]


def test_an_unreadable_journal_entry_stops_the_run(lab):
    _ok(_install(lab))
    entry = lab.root / STATE / "journal" / "bootstrap.done"
    entry.chmod(0)
    try:
        # the driver stops at the first stage that reads the state: the backup
        _refused(_install(lab), "backup: cannot read the control VM's state")
        # bootstrap.sh itself refuses it too
        proc = lab.run("control-exec.sh", "run", "--", "talos-unattended/scripts/bootstrap.sh",
                       "--inputs", "talos-unattended/control.env", "--nodes", "talos-unattended/nodes.txt")
        _refused(proc, "cannot read the journal entry")
    finally:
        entry.chmod(0o600)
    assert lab.world()["talos"]["bootstrap_calls"] == 1


def test_a_journal_lost_on_the_control_vm_only_is_drift_and_the_backup_is_kept(lab):
    """B3: the backup never rotates away a journal entry the control VM lost."""
    _ok(_install(lab))
    lab.edit(lambda w: w["talos"].update(etcd_down=True))
    (lab.root / STATE / "journal" / "bootstrap.done").unlink()
    (lab.root / STATE / "journal" / "cp-01.applied").unlink()
    proc = _refused(_install(lab), "the control VM's journal lost", status=3)
    assert "bootstrap.done" in proc.stderr and "cp-01.applied" in proc.stderr
    assert (lab.backup / "state" / "journal" / "bootstrap.done").exists()
    assert (lab.backup / "state" / "journal" / "cp-01.applied").exists()
    assert lab.world()["talos"]["bootstrap_calls"] == 1
    assert len(_talos_calls(lab, "apply-config")) == 2


def test_existing_etcd_data_is_not_mistaken_for_a_fresh_node(lab):
    """B3: Preparing and no members, but the data directory is not empty."""
    _ok(_install(lab))
    _drop_bootstrap_journal(lab)
    lab.edit(lambda w: (w["talos"].update(etcd_down=True),
                        w["talos"]["nodes"]["192.168.53.11"].update(etcd_state="Preparing")))
    proc = _refused(_install(lab, TALOS_AUTH_TIMEOUT="1"), "cannot establish that etcd was never bootstrapped",
                    status=3)
    assert "cp-01=Preparing/data-data" in proc.stderr
    assert lab.world()["talos"]["bootstrap_calls"] == 1


@pytest.mark.parametrize("etcd_dir,seen", [("error", "data-unknown"), ("garbage", "data-unknown")])
def test_an_unreadable_etcd_data_directory_never_bootstraps(lab, etcd_dir, seen):
    _edit_node(lab, "192.168.53.11", etcd_dir=etcd_dir)
    proc = _refused(_install(lab, TALOS_AUTH_TIMEOUT="1"), "cannot establish that etcd was never bootstrapped",
                    status=3)
    assert seen in proc.stderr
    assert "bootstrap_calls" not in lab.world()["talos"]


def test_an_absent_etcd_data_directory_counts_as_empty(lab):
    _edit_node(lab, "192.168.53.11", etcd_dir="missing")
    proc = _ok(_install(lab))
    assert "cp-01=Preparing/data-empty" in proc.stderr
    assert lab.world()["talos"]["bootstrap_calls"] == 1


def _stop_before_bootstrap(lab):
    """A first run that applies the configs, then stops before bootstrapping."""
    _edit_node(lab, "192.168.53.11", synced=False)
    _refused(_install(lab, TALOS_AUTH_TIMEOUT="1", TALOS_PREBOOT_CHECKS="1"), "time is not synchronised")
    _edit_node(lab, "192.168.53.11", synced=True)
    return lab.root / STATE / "journal"


@pytest.mark.parametrize("arrange,message", [
    (lambda j: (j / "bootstrap.intent").mkdir(), "cannot read the journal entry"),
    (lambda j: (j / "bootstrap.intent").write_text(""), "is empty"),
    (lambda j: (j / "bootstrap.intent").write_text("not a record at all\n"), "malformed entry"),
])
def test_an_unusable_bootstrap_journal_entry_stops_without_a_bootstrap(lab, arrange, message):
    arrange(_stop_before_bootstrap(lab))
    _refused(_install(lab), message)
    assert "bootstrap_calls" not in lab.world()["talos"]


def test_a_failed_intent_write_sends_no_bootstrap(lab):
    journal = _stop_before_bootstrap(lab)
    (journal / ".bootstrap.intent.tmp").mkdir()
    (journal / ".bootstrap.intent.tmp" / "blocker").write_text("x")
    _refused(_install(lab), "cannot write")
    assert "bootstrap_calls" not in lab.world()["talos"]


# ---- review regressions: bounded calls (B9) ------------------------------------
@pytest.mark.parametrize("world,message", [
    ({"kube": {"hang": ["get nodes"]}}, "nodes registered after"),
    ({"kube": {"hang": ["--raw=/readyz"]}}, "does not answer within"),
    ({"talos": {"hang": ["get timestatus"]}}, "time is not synchronised"),
])
def test_a_hung_request_cannot_outlive_its_stage(lab, world, message):
    import time
    def hang(w):
        for key, value in world.items():
            w.setdefault(key, {}).update(value)
    lab.edit(hang)
    start = time.monotonic()
    _refused(_install(lab, TALOS_CALL_TIMEOUT="2", TALOS_KUBE_TIMEOUT="3", TALOS_AUTH_TIMEOUT="3",
                      TALOS_PREBOOT_CHECKS="1"), message)
    assert time.monotonic() - start < 100
    # the bound reached the control VM in its shipped inputs, not through a
    # leaked host environment (the fake ssh passes none)
    shipped = (lab.root / "control-home" / "talos-unattended" / "control.env").read_text()
    assert "TALOS_CALL_TIMEOUT=2\n" in shipped


# ---- review regressions: Flux absence and destination (B5) -----------------------
@pytest.mark.parametrize("failing,message", [
    ("kubectl get crd", "cannot tell whether Flux is installed"),
    ("kubectl get gitrepository", "cannot read the flux-system GitRepository"),
])
def test_a_failed_flux_lookup_is_not_absence(lab, failing, message):
    _gitlab(lab)
    lab.edit(lambda w: w["flux"].update(crd=True))
    lab.set_world(fail=[failing])
    _refused(_install(lab, **FLUX_ON), message)
    assert "bootstrap_calls" not in lab.world()["flux"]
    assert not lab.calls("curl")


def test_flux_never_bootstraps_into_an_occupied_path(lab):
    _gitlab(lab)
    lab.edit(lambda w: w["gitlab"].update(paths=["clusters/lab"]))
    _refused(_install(lab, **FLUX_ON), "already holds files, and this cluster has no record", status=3)
    assert "bootstrap_calls" not in lab.world()["flux"]


def test_flux_resumes_its_own_interrupted_bootstrap(lab):
    _gitlab(lab)
    lab.edit(lambda w: w["flux"].update(bootstrap_fail="after-push"))
    _refused(_install(lab, **FLUX_ON), "flux: bootstrap failed")
    assert "clusters/lab" in lab.world()["gitlab"]["paths"]
    lab.edit(lambda w: w["flux"].pop("bootstrap_fail"))
    proc = _ok(_install(lab, **FLUX_ON))
    assert "this cluster's own interrupted bootstrap; resuming it" in proc.stderr
    assert lab.world()["flux"]["bootstrap_calls"] == 2


@pytest.mark.parametrize("gitlab,message", [
    ({"branches": ["other"]}, "the branch main does not exist"),
    ({"tree_status": 500}, "cannot tell whether clusters/lab exists"),
    ({"down": True}, "cannot reach the GitLab API"),
])
def test_an_unclear_flux_destination_stops_before_bootstrap(lab, gitlab, message):
    _gitlab(lab)
    lab.edit(lambda w: w["gitlab"].update(gitlab))
    _refused(_install(lab, **FLUX_ON), message)
    assert "bootstrap_calls" not in lab.world()["flux"]


@pytest.mark.parametrize("var", ["TALOS_CALL_TIMEOUT", "TALOS_REQUEST_TIMEOUT", "TALOS_KUBE_TIMEOUT",
                                 "TALOS_PREBOOT_CHECKS"])
def test_a_zero_bound_is_refused_before_anything_runs(lab, var):
    """B9: timeout(1) and kubectl read 0 as "no bound"."""
    _refused(_install(lab, **{var: "0"}), f"{var} must be a positive whole number")
    assert "calls" not in lab.world()["boxman"]


@pytest.mark.parametrize("var", ["TALOS_CALL_TIMEOUT", "TALOS_REQUEST_TIMEOUT"])
def test_a_zero_bound_in_the_shipped_inputs_is_refused_on_the_control_vm(lab, var):
    _ok(_install(lab))
    with open(lab.root / "control-home" / "talos-unattended" / "control.env", "a") as fobj:
        fobj.write(f"{var}=0\n")
    before = len(lab.calls("talosctl")) + len(lab.calls("kubectl"))
    proc = lab.run("control-exec.sh", "run", "--", "talos-unattended/scripts/bootstrap.sh",
                   "--inputs", "talos-unattended/control.env", "--nodes", "talos-unattended/nodes.txt")
    _refused(proc, f"{var} must be a positive whole number")
    assert len(lab.calls("talosctl")) + len(lab.calls("kubectl")) == before


@pytest.mark.parametrize("list_format", ["single", "multi"])
def test_both_talosctl_list_formats_are_read(lab, list_format):
    """Attempt 3: for one node talosctl prints names only, without a header."""
    _edit_node(lab, "192.168.53.11", list_format=list_format)
    proc = _ok(_install(lab))
    assert "cp-01=Preparing/data-empty" in proc.stderr
    assert lab.world()["talos"]["bootstrap_calls"] == 1


@pytest.mark.parametrize("text", ["NODE NAME\n192.168.53.11 . extra\n", "a b c\n", "\n"])
def test_an_unexpected_list_output_is_unknown_and_never_bootstraps(lab, text):
    lab.set_world(garbage={"talosctl list /var/lib/etcd": text})
    _refused(_install(lab, TALOS_AUTH_TIMEOUT="1"), "data-unknown", status=3)
    assert "bootstrap_calls" not in lab.world()["talos"]



# ---- the plain boxman up of option 2: hooks and the up record --------------------
def _sourced_inputs(host) -> dict:
    """The TALOS_* variables of an operator who ran: set -a; . inputs.env"""
    out = subprocess.run(["bash", "-c", f"set -a; . {host.inputs_file}; env -0"], env={},
                         capture_output=True, text=True, check=True).stdout
    pairs = (line.split("=", 1) for line in out.split("\0") if "=" in line)
    return {k: v for k, v in pairs if k.startswith("TALOS_")}


def test_a_plain_boxman_up_installs_the_cluster(lab):
    """No make install, no TALOS_INPUTS: boxman up and its hooks only."""
    inputs = _sourced_inputs(lab)
    proc = _ok(lab.run_boxman("--conf", os.path.join(BOX, "conf.yml"), "up", extra=inputs))
    assert "install: converged: project lab353" in proc.stderr
    assert lab.world()["talos"]["bootstrap_calls"] == 1
    again = _ok(lab.run_boxman("--conf", os.path.join(BOX, "conf.yml"), "up", extra=inputs))
    assert "cp-01: recorded" in again.stdout and "bootstrapped earlier" in again.stderr
    assert lab.world()["talos"]["bootstrap_calls"] == 1


def test_make_install_is_a_wrapper_around_boxman_up(lab):
    proc = _ok(_install(lab))
    assert "boxman up; its pre_up and post_up hooks run this script" in proc.stderr
    up = [c for c in lab.calls("boxman")]
    assert up == [f"boxman --conf {BOX}/conf.yml up"]


def test_a_failing_hook_is_boxman_ups_exit_status_2(lab):
    lab.meminfo(1 << 20)
    proc = lab.run("install.sh")
    assert proc.returncode == 2
    assert "pre_up hook 'talos-preflight' failed (exit 1)" in proc.stderr


def test_boxman_up_force_is_refused_by_the_pre_up_hook(lab):
    _refused(_install(lab, BOXMAN="boxman --force"), "boxman up --force is not supported for this box")
    assert "calls" not in lab.world()["boxman"]


def test_the_driver_takes_ownership_from_the_record_not_from_libvirt(lab):
    """A record that names another uuid than libvirt's: nothing is recorded."""
    lab.edit(lambda w: w["boxman"].update(record_tamper={W1: "uuid-not-in-libvirt"}))
    _refused(_install(lab), "is not the domain boxman's up record says this up created", status=3)
    assert not (lab.driver / "run.env").exists()
    assert not lab.calls("ssh")


@pytest.mark.parametrize("change,message", [
    ({"record_run_id": "an-earlier-up"}, "belongs to another boxman up"),
    ({"record_project": "another-project"}, "is for project another-project"),
])
def test_a_record_of_another_up_is_refused(lab, change, message):
    lab.edit(lambda w: w["boxman"].update(change))
    _refused(_install(lab), message)
    assert not (lab.driver / "run.env").exists()


def _post_up(host, record: dict, run_id="run-1"):
    """install.sh --post-up as boxman's hook, with a hand-made record."""
    path = host.root / "record.json"
    path.write_text(json.dumps(record))
    return host.run("install.sh", "--post-up", BOXMAN_HOOK="post_up", BOXMAN_UP_RUN_ID=run_id,
                    BOXMAN_UP_RECORD=str(path))


def _record(created, present, **extra):
    return dict({"boxman_up_record": 1, "project": PROJECT, "run_id": "run-1", "status": "succeeded",
                 "force": False, "created": created, "present": present}, **extra)


ALL = {"domains": {CONTROL: "uuid-control", CP1: "uuid-cp-01", W1: "uuid-worker-01"},
       "networks": {NETWORK: "net-uuid-1"}}


def test_the_post_up_hook_runs_only_inside_boxman_up(lab):
    _refused(lab.run("install.sh", "--post-up"), "runs as boxman up's post_up hook")
    _refused(lab.run("install.sh", "--pre-up"), "runs as boxman up's pre_up hook")


@pytest.mark.parametrize("record,message,status", [
    (_record(ALL, ALL, status="failed"), "did not succeed (failed)", 1),
    (_record(ALL, ALL, boxman_up_record=2), "is not a boxman up record", 1),
    (_record(ALL, {"domains": {}, "networks": {}}), "does not list", 1),
    (_record({"domains": {CONTROL: "uuid-control"}, "networks": {}}, ALL), "partly recreated", 3),
])
def test_an_unusable_record_gets_no_ownership(lab, record, message, status):
    _ok(_install(lab))
    _refused(_post_up(lab, record), message, status=status)


def test_a_project_created_anew_under_a_held_cluster_is_drift(lab):
    """e.g. a forced recreation: the old identity cannot be reused."""
    _ok(_install(lab))
    _refused(_post_up(lab, _record(ALL, ALL)), "created this project's VMs and network anew", status=3)


def test_an_existing_project_must_match_the_driver_record(lab):
    _ok(_install(lab))
    present = json.loads(json.dumps(ALL))
    present["domains"][CP1] = "uuid-swapped"
    lab.edit(lambda w: w["domains"][CP1].update(uuid="uuid-swapped"))
    _refused(_post_up(lab, _record({"domains": {}, "networks": {}}, present)),
             f"{CP1} is not the domain this driver created", status=3)
