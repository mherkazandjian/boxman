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
TALOS_VERSION = "v1.99.0"


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
    _refused(host.run("preflight.sh"), "a run never adopts what it did not create")


def test_preflight_never_adopts_an_existing_network(host):
    host.edit(lambda w: w["networks"].update({NETWORK: {"address": "192.168.53.1"}}))
    _refused(host.run("preflight.sh"), f"these already exist: {NETWORK}")


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
