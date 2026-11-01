"""
Deterministic checks for boxes/talos-unattended.

Two halves, neither touching libvirt or the network:

- conf.yml's shape: the seven-VM topology, sizing, the shared ISO, pinned MACs
  that the fabric network reserves, and no ssh credentials on the API-only
  Talos group.
- scripts/gen-config.sh, executed for real with a fake ``talosctl`` on PATH
  that records its argv and writes recognisable secret material. The tests
  assert the commands it ran, that a rerun changes nothing and mints no new
  identity, that the guards stop before side effects, and that no secret
  reaches its output or a git work tree.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import subprocess
import sys

import pytest
import yaml

from boxman.manager import BoxmanManager
from boxman.utils.jinja_env import create_jinja_env

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOX = os.path.join(REPO, "boxes", "talos-unattended")
GEN_CONFIG = os.path.join(BOX, "scripts", "gen-config.sh")
NODES_FROM_CONF = os.path.join(BOX, "scripts", "nodes-from-conf.py")

TALOS_VERSION = "v1.99.0"   # synthetic: these tests pin no real release
SECRET_MARK = "FAKESECRET"

CP = [f"cp-0{i}" for i in (1, 2, 3)]
WORKERS = [f"worker-0{i}" for i in (1, 2, 3)]


# --------------------------------------------------------------------------- #
# conf.yml                                                                    #
# --------------------------------------------------------------------------- #
def _render(monkeypatch) -> dict:
    for var in ("TALOS_LAB_DIR", "TALOS_LAB_PREFIX", "TALOS_ISO_URI",
                "TALOS_ISO_CHECKSUM", "TALOS_OPERATOR_KEY", "TALOS_OPERATOR_PUBKEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BOXMAN_CONF_DIR", BOX)
    return yaml.safe_load(create_jinja_env(BOX).get_template("conf.yml").render())


def test_seven_vms_in_two_groups_with_the_design_sizing(monkeypatch):
    cfg = _render(monkeypatch)
    assert cfg["version"] == "1.0"
    assert set(cfg["clusters"]) == {"management", "talos"}
    assert set(cfg["clusters"]["management"]["vms"]) == {"control"}
    talos = cfg["clusters"]["talos"]["vms"]
    assert list(talos) == CP + WORKERS
    for name in CP:
        assert (talos[name]["vcpus"], talos[name]["memory"], talos[name]["disk_size"]) \
            == (2, 4096, "50G"), name
    for name in WORKERS:
        assert (talos[name]["vcpus"], talos[name]["memory"], talos[name]["disk_size"]) \
            == (4, 6144, "150G"), name
    template = cfg["templates"]["control-base"]
    assert (template["vcpus"], template["memory"], template["disk_size"]) == (2, 2048, "40G")
    control = cfg["clusters"]["management"]["vms"]["control"]
    assert control["memory"] == 2048
    assert control["cpus"]["sockets"] * control["cpus"]["cores"] * control["cpus"]["threads"] == 2
    assert cfg["clusters"]["management"]["base_image"] == template["name"]


def test_talos_nodes_boot_the_shared_iso_with_a_reserved_mac(monkeypatch):
    cfg = _render(monkeypatch)
    hosts = cfg["clusters"]["management"]["networks"]["fabric"]["ip"]["dhcp"]["hosts"]
    reserved = {h["name"]: h for h in hosts}
    assert set(reserved) == set(CP + WORKERS)
    assert len({h["mac"] for h in hosts}) == len({h["ip"] for h in hosts}) == 6
    for name, vm in cfg["clusters"]["talos"]["vms"].items():
        assert vm["boot_order"] == ["cdrom", "hd"], name
        assert vm["cdroms"] == [{"name": "talos-install"}], name
        assert vm["networks"] == [{"name": "management::fabric",
                                   "mac": reserved[name]["mac"]}], name
    assert set(cfg["isos"]) == {"talos-install"}


def test_api_only_group_has_no_ssh_credentials(monkeypatch):
    cfg = _render(monkeypatch)
    talos = cfg["clusters"]["talos"]
    for key in ("admin_user", "admin_pass", "admin_key_name", "ssh_config", "base_image"):
        assert key not in talos, key
    # the control VM is reached with the operator key the template authorizes;
    # no password exists for boxman to inject a key with
    assert "admin_pass" not in cfg["clusters"]["management"]


def test_config_passes_boot_validation(monkeypatch):
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = _render(monkeypatch)
    mgr.app_config = {}
    mgr.validate_base_images()   # ISO declared, MACs well formed and unique


def test_unsupplied_iso_pin_is_the_marked_placeholder(monkeypatch):
    # make check-box-images skips exactly this marker instead of probing it
    iso = _render(monkeypatch)["isos"]["talos-install"]
    assert "placeholder" in iso["uri"]
    assert iso["checksum"] == "sha256:" + "0" * 64


def _template_userdata(monkeypatch) -> dict:
    return yaml.safe_load(_render(monkeypatch)["templates"]["control-base"]["cloudinit"])


def _prepare_script(userdata: dict) -> str:
    return next(f for f in userdata["write_files"]
                if f["path"] == "/usr/local/sbin/prepare-control-template")["content"]


def test_control_template_is_a_non_secret_recipe(monkeypatch):
    cloudinit = _render(monkeypatch)["templates"]["control-base"]["cloudinit"]
    data = yaml.safe_load(cloudinit)
    admin = next(u for u in data["users"] if u["name"] == "admin")
    assert admin["lock_passwd"] is True and admin["ssh_authorized_keys"]
    assert data["ssh_pwauth"] is False
    assert "chpasswd" not in data
    assert "qemu-guest-agent" in data["packages"]
    script = _prepare_script(data)
    assert script.count("\nfetch ") == 3            # talosctl, kubectl, flux
    assert "sha256sum -c" in script
    for word in ("secrets.yaml", "talosconfig", "TOKEN", "machineconfig"):
        assert word not in cloudinit, word


def test_only_the_fail_fast_script_can_mark_the_template_ready(monkeypatch):
    # review B1: runcmd runs under /bin/sh without errexit, so a marker
    # written by its own entry published a template whose tools had failed
    template = _render(monkeypatch)["templates"]["control-base"]
    data = yaml.safe_load(template["cloudinit"])
    marker = template["cloudinit_done_marker"]
    script = _prepare_script(data)
    assert "set -euo pipefail" in script.splitlines()[:5]
    assert f"marker=${{READY_MARKER:-{marker}}}" in script
    assert data["runcmd"][-1] == "/usr/local/sbin/prepare-control-template"
    for entry in data["runcmd"]:
        assert "template-ready" not in str(entry) and "/var/lib/talos-unattended" not in str(entry)


#: Stands in for curl: copies $FAKE_DOWNLOADS/<last url segment> to -o.
FAKE_CURL = r"""#!/usr/bin/env bash
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out=$2; shift ;; --retry) shift ;; -*) ;; *) url=$1 ;; esac
  shift
done
echo "$url" >> "$FAKE_DOWNLOADS/requested"
cp "$FAKE_DOWNLOADS/${url##*/}" "$out" 2>/dev/null || { echo "curl: (22) 404" >&2; exit 22; }
"""


@pytest.fixture
def template_build(tmp_path, monkeypatch):
    """The rendered prepare-control-template, runnable against fake downloads."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    tool = "#!/bin/sh\nexit 0\n"
    for name in ("talosctl-linux-amd64", "kubectl"):
        (downloads / name).write_text(tool)
    staged = tmp_path / "flux-src"
    staged.mkdir()
    (staged / "flux").write_text(tool)
    (staged / "flux").chmod(0o755)
    subprocess.run(["tar", "-czf", str(downloads / "flux_9.9.9_linux_amd64.tar.gz"),
                    "-C", str(staged), "flux"], check=True)
    pins = {
        "TALOS_VERSION": TALOS_VERSION,
        "TALOSCTL_SHA256": _sha(downloads / "talosctl-linux-amd64"),
        "KUBECTL_VERSION": "v9.9.9", "KUBECTL_SHA256": _sha(downloads / "kubectl"),
        "FLUX_VERSION": "v9.9.9",
        "FLUX_SHA256": _sha(downloads / "flux_9.9.9_linux_amd64.tar.gz"),
    }
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    marker = tmp_path / "var" / "template-ready"

    class Build:
        def __init__(self):
            self.pins = dict(pins)
            self.downloads = downloads
            self.tools = tools
            self.marker = marker

        def run(self):
            for key in pins:
                monkeypatch.delenv(key, raising=False)
            for key, value in self.pins.items():
                monkeypatch.setenv(key, value)
            script = tmp_path / "prepare-control-template"
            script.write_text(_prepare_script(_template_userdata(monkeypatch)))
            env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_DOWNLOADS": str(downloads),
                   "TOOLS_BIN_DIR": str(tools), "READY_MARKER": str(marker)}
            return subprocess.run(["bash", str(script)], capture_output=True,
                                  text=True, env=env, timeout=60)

    return Build()


def test_template_ready_after_every_tool_installs(template_build):
    proc = template_build.run()
    assert proc.returncode == 0, proc.stderr
    assert template_build.marker.exists()
    assert sorted(p.name for p in template_build.tools.iterdir()) == ["flux", "kubectl", "talosctl"]
    requested = (template_build.downloads / "requested").read_text()
    assert f"/talos/releases/download/{TALOS_VERSION}/talosctl-linux-amd64" in requested
    assert "/release/v9.9.9/bin/linux/amd64/kubectl" in requested
    assert "/flux2/releases/download/v9.9.9/flux_9.9.9_linux_amd64.tar.gz" in requested


@pytest.mark.parametrize("breakage", [
    "download_fails", "checksum_mismatch", "archive_lacks_binary", "pin_not_supplied"])
def test_a_failed_tool_install_leaves_no_ready_marker(template_build, breakage):
    build = template_build
    build.marker.parent.mkdir()
    build.marker.write_text("stale marker from an earlier attempt\n")
    if breakage == "download_fails":
        (build.downloads / "kubectl").unlink()
    elif breakage == "checksum_mismatch":
        (build.downloads / "kubectl").write_text("#!/bin/sh\necho tampered\n")
    elif breakage == "archive_lacks_binary":
        empty = build.downloads / "empty"
        empty.mkdir()
        (empty / "README").write_text("no binary here\n")
        tarball = build.downloads / "flux_9.9.9_linux_amd64.tar.gz"
        subprocess.run(["tar", "-czf", str(tarball), "-C", str(empty), "README"], check=True)
        build.pins["FLUX_SHA256"] = _sha(tarball)
    elif breakage == "pin_not_supplied":
        del build.pins["KUBECTL_SHA256"]
    proc = build.run()
    assert proc.returncode != 0
    assert not build.marker.exists(), breakage


def test_nodes_from_conf_prints_the_reserved_table():
    out = subprocess.run([sys.executable, NODES_FROM_CONF], capture_output=True,
                         text=True, check=True,
                         env={k: v for k, v in os.environ.items()
                              if not k.startswith("TALOS_")}).stdout
    rows = [line.split() for line in out.splitlines()]
    assert [r[0] for r in rows] == CP + WORKERS
    assert [r[1] for r in rows] == ["controlplane"] * 3 + ["worker"] * 3
    assert rows[0][2:] == ["192.168.53.11", "52:54:00:53:00:11"]
    assert rows[5][2:] == ["192.168.53.23", "52:54:00:53:00:23"]


def test_provisioning_job_leaves_the_box_out():
    # it needs private pins and a dedicated driver; `boxman up` alone is not
    # the install, so the generic ssh-based provisioning checks do not apply
    sys.path.insert(0, os.path.dirname(__file__))
    try:
        from test_provision_boxes import discover_boxes
    finally:
        sys.path.pop(0)
    assert BOX not in discover_boxes()


# --------------------------------------------------------------------------- #
# gen-config.sh with a fake talosctl                                          #
# --------------------------------------------------------------------------- #
#: Records every invocation and stands in for talosctl with real YAML: secrets
#: carry recognisable FAKESECRET values, `gen config` builds role configs from
#: them (control planes get the CA keys, etcd and service-account material),
#: and `machineconfig patch` merges like Talos' strategic merge closely enough
#: for the identity checks -- maps merge, lists append, a patch document with
#: a `kind` is added beside the main one. Its output is a function of its
#: inputs, so an unchanged rerun is byte-identical; its talosconfig carries a
#: fresh random key each time, as the real one does. Shebang filled in by the
#: fixture: the test interpreter, which has PyYAML.
FAKE_TALOSCTL = r"""#!@PYTHON@
import os, secrets, sys
import yaml

argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join(argv) + "\n")
talosconfig = None
if argv[:1] == ["--talosconfig"]:
    talosconfig, argv = argv[1], argv[2:]


def opts(flag):
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == flag]


def opt(flag):
    found = opts(flag)
    return found[-1] if found else None


def merge(dst, src):
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            merge(dst[key], value)
        elif isinstance(value, list) and isinstance(dst.get(key), list):
            dst[key] = dst[key] + value
        else:
            dst[key] = value


def load(path):
    with open(path) as fobj:
        return [d for d in yaml.safe_load_all(fobj) if d is not None]


def patched(docs, patch_files):
    main = next(d for d in docs if d.get("version") == "v1alpha1")
    for ref in patch_files:
        for doc in load(ref.lstrip("@")):
            if "kind" in doc:
                docs.append(doc)
            else:
                merge(main, doc)
    return docs


def dump(docs, path):
    with open(path, "w") as fobj:
        yaml.safe_dump_all(docs, fobj, sort_keys=True)


def fresh():
    return "FAKESECRET-" + secrets.token_hex(8)


cmd = " ".join(argv[:2])
if cmd == "version --client":
    print("Client:\n\tTalos " + os.environ["FAKE_TALOS_VERSION"])
elif cmd == "gen secrets":
    out = opt("-o")
    if os.path.exists(out):
        sys.exit("file exists")
    with open(out, "w") as fobj:
        yaml.safe_dump({
            "cluster": {"id": fresh(), "secret": fresh()},
            "secrets": {"bootstraptoken": fresh(), "secretboxencryptionsecret": fresh()},
            "trustdinfo": {"token": fresh()},
            "certs": {name: {"crt": fresh(), "key": fresh()}
                      for name in ("os", "k8s", "k8saggregator", "etcd")},
            "serviceaccount": {"key": fresh()},
        }, fobj)
elif cmd == "gen config":
    name, endpoint = argv[2], argv[3]
    with open(opt("--with-secrets")) as fobj:
        sec = yaml.safe_load(fobj)
    certs = sec["certs"]
    out = opt("--output")
    os.makedirs(out)
    for role in ("controlplane", "worker"):
        machine = {"type": role, "token": sec["trustdinfo"]["token"],
                   "ca": {"crt": certs["os"]["crt"]},
                   "install": {"disk": opt("--install-disk"), "image": opt("--install-image")}}
        cluster = {"id": sec["cluster"]["id"], "secret": sec["cluster"]["secret"],
                   "clusterName": name, "controlPlane": {"endpoint": endpoint},
                   "token": sec["secrets"]["bootstraptoken"], "ca": {"crt": certs["k8s"]["crt"]}}
        if role == "controlplane":
            machine["ca"]["key"] = certs["os"]["key"]
            cluster["ca"]["key"] = certs["k8s"]["key"]
            cluster.update(aggregatorCA=certs["k8saggregator"],
                           serviceAccount={"key": sec["serviceaccount"]["key"]},
                           secretboxEncryptionSecret=sec["secrets"]["secretboxencryptionsecret"],
                           etcd={"ca": certs["etcd"]})
        docs = [{"version": "v1alpha1", "machine": machine, "cluster": cluster}]
        dump(patched(docs, opts("--config-patch")), os.path.join(out, role + ".yaml"))
    with open(os.path.join(out, "talosconfig"), "w") as fobj:
        fobj.write("client-key: " + fresh() + "\n")
elif cmd == "machineconfig patch":
    dump(patched(load(argv[2]), opts("--patch")), opt("--output"))
elif cmd == "validate --config":
    sys.exit(int(os.environ.get("FAKE_VALIDATE_RC", "0")))
elif argv[0] == "config" and argv[1] in ("endpoint", "node"):
    with open(talosconfig, "a") as fobj:
        fobj.write(" ".join(argv) + "\n")
else:
    sys.exit("fake talosctl: unexpected " + " ".join(argv))
"""


def _node_table(path, cps=CP, workers=WORKERS):
    lines = [f"{n} controlplane 192.168.53.1{i + 1} 52:54:00:53:00:1{i + 1}"
             for i, n in enumerate(cps)]
    lines += [f"{n} worker 192.168.53.2{i + 1} 52:54:00:53:00:2{i + 1}"
              for i, n in enumerate(workers)]
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def lab(tmp_path):
    """A fake talosctl on PATH, a node table and complete inputs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "talosctl"
    fake.write_text(FAKE_TALOSCTL.replace("@PYTHON@", sys.executable))
    fake.chmod(0o755)
    # gen-config.sh runs check-node-config.py with `python3`; this one has PyYAML
    python3 = bin_dir / "python3"
    python3.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python3.chmod(0o755)
    log = tmp_path / "talosctl.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(log),
        "FAKE_TALOS_VERSION": TALOS_VERSION,
        "TALOS_STATE_DIR": str(tmp_path / "state"),
        "TALOS_CLUSTER_NAME": "lab",
        "TALOS_VERSION": TALOS_VERSION,
        "KUBERNETES_VERSION": "1.99.0",
        "TALOS_INSTALLER_IMAGE": f"registry.example/installer/abc:{TALOS_VERSION}",
        "TALOS_INSTALL_DISK": "/dev/vda",
        "TALOS_VIP": "192.168.53.5",
        "TALOS_NTP_SERVERS": "ntp1.example ntp2.example",
        "TALOS_POD_CIDR": "10.244.0.0/16",
        "TALOS_SERVICE_CIDR": "10.96.0.0/12",
    }

    class Lab:
        root = tmp_path
        bin = bin_dir
        state = tmp_path / "state"
        nodes = _node_table(tmp_path / "nodes.txt")

        def run(self, *args, **overrides):
            e = dict(env, **overrides)
            for key in [k for k, v in overrides.items() if v is None]:
                del e[key]
            return subprocess.run(["bash", GEN_CONFIG, "--nodes", str(self.nodes), *args],
                                  capture_output=True, text=True, env=e, timeout=60)

        def calls(self):
            return log.read_text().splitlines() if log.exists() else []

    return Lab()


def _ok(proc):
    assert proc.returncode == 0, proc.stderr
    return proc


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_dry_run_plans_without_state_or_talosctl(lab):
    out = _ok(lab.run("--dry-run")).stdout
    assert "talosctl gen secrets --talos-version v1.99.0" in out
    assert "--install-disk /dev/vda" in out and "https://192.168.53.5:6443" in out
    assert sum(line.startswith("plan: cp-") for line in out.splitlines()) == 3
    assert not lab.state.exists()
    assert lab.calls() == []


def test_first_run_generates_with_explicit_pins(lab):
    proc = _ok(lab.run())
    calls = lab.calls()
    assert calls[0] == "version --client --short"
    assert calls[1].startswith(f"gen secrets --talos-version {TALOS_VERSION} -o ")
    gen = calls[2]
    assert gen.startswith("gen config lab https://192.168.53.5:6443 --with-secrets ")
    for flag in (f"--talos-version {TALOS_VERSION}", "--kubernetes-version 1.99.0",
                 "--install-disk /dev/vda",
                 f"--install-image registry.example/installer/abc:{TALOS_VERSION}",
                 "--output-types controlplane,worker,talosconfig"):
        assert flag in gen, flag
    assert f"--with-secrets {lab.state}/secrets.yaml" in gen
    patches = [c for c in calls if c.startswith("machineconfig patch")]
    assert len(patches) == 6
    assert sum(c.startswith("validate --config") and "--mode metal --strict" in c
               for c in calls) == 6
    # talosctl talks to the control planes directly, never through the VIP
    assert calls[-2].startswith("--talosconfig ")
    assert calls[-2].endswith("config endpoint 192.168.53.11 192.168.53.12 192.168.53.13")
    assert calls[-1].endswith("config node 192.168.53.11")
    assert proc.stdout.splitlines() == [f"{n}: created" for n in CP + WORKERS]


def _config(path) -> dict:
    return next(d for d in yaml.safe_load_all(path.read_text()) if d.get("version") == "v1alpha1")


def test_node_configs_carry_role_identity_ntp_and_vip(lab):
    _ok(lab.run())
    for name in CP + WORKERS:
        cfg = _config(lab.state / "nodes" / f"{name}.yaml")
        role = "controlplane" if name.startswith("cp-") else "worker"
        assert cfg["machine"]["type"] == role
        net = cfg["machine"]["network"]
        assert net["hostname"] == name
        (nic,) = net["interfaces"]
        assert nic["deviceSelector"]["hardwareAddr"].startswith("52:54:00:53:00:")
        assert nic["dhcp"] is True
        assert ("vip" in nic) == (role == "controlplane")
        if role == "controlplane":
            assert nic["vip"] == {"ip": "192.168.53.5"}
        assert cfg["machine"]["time"]["servers"] == ["ntp1.example", "ntp2.example"]
        assert cfg["cluster"]["network"] == {"podSubnets": ["10.244.0.0/16"],
                                             "serviceSubnets": ["10.96.0.0/12"]}
        assert cfg["machine"]["install"] == {
            "disk": "/dev/vda", "image": f"registry.example/installer/abc:{TALOS_VERSION}"}


def test_state_is_private(lab):
    _ok(lab.run())
    assert _mode(lab.state) == 0o700
    for path in [lab.state / "secrets.yaml", lab.state / "talosconfig",
                 *(lab.state / "nodes").glob("*.yaml")]:
        assert _mode(path) == 0o600, path
    assert not list(lab.state.glob(".staging.*"))


def test_rerun_reuses_identity_and_changes_nothing(lab):
    _ok(lab.run())
    before = {p.name: _sha(p) for p in [lab.state / "secrets.yaml", lab.state / "talosconfig",
                                         *(lab.state / "nodes").glob("*.yaml")]}
    first_calls = len(lab.calls())
    proc = _ok(lab.run())
    rerun = lab.calls()[first_calls:]
    assert not any(c.startswith("gen secrets") for c in rerun)
    assert not any("config endpoint" in c for c in rerun)   # talosconfig kept
    assert proc.stdout.splitlines() == [f"{n}: unchanged" for n in CP + WORKERS]
    after = {p.name: _sha(p) for p in [lab.state / "secrets.yaml", lab.state / "talosconfig",
                                        *(lab.state / "nodes").glob("*.yaml")]}
    assert after == before


def test_a_patch_change_is_reported_and_published(lab, tmp_path):
    _ok(lab.run())
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "worker.yaml").write_text("machine:\n  kubelet: {}\n")
    proc = _ok(lab.run(TALOS_PATCH_DIR=str(patches)))
    status = dict(line.split(": ") for line in proc.stdout.splitlines())
    assert {status[n] for n in CP} == {"unchanged"}
    assert {status[n] for n in WORKERS} == {"changed"}
    worker_patch = next(c for c in lab.calls()[::-1] if "worker-01.yaml" in c
                        and c.startswith("machineconfig patch"))
    # user patches first, the generated identity patch last
    assert worker_patch.index(f"@{patches}/worker.yaml") < worker_patch.index("patches/worker-01.yaml")


def test_no_secret_reaches_the_output(lab):
    first = _ok(lab.run())
    second = _ok(lab.run())
    assert SECRET_MARK in (lab.state / "secrets.yaml").read_text()
    for proc in (first, second):
        assert SECRET_MARK not in proc.stdout + proc.stderr


# ---- B4: the repository guard fails closed -------------------------------- #
def _without_git(lab, tmp_path) -> str:
    """A PATH with the fake bin and every system tool except git."""
    sysbin = tmp_path / "sysbin-no-git"
    sysbin.mkdir()
    for d in ("/usr/bin", "/bin"):
        for entry in os.listdir(d):
            target = os.path.join(d, entry)
            if not entry.startswith("git") and not os.path.lexists(sysbin / entry):
                os.symlink(target, sysbin / entry)
    return f"{lab.bin}:{sysbin}"


def _assert_refused_before_state(lab, proc, state, message):
    assert proc.returncode != 0
    assert message in proc.stderr, proc.stderr
    assert not state.exists()
    assert lab.calls() == []


def test_state_inside_a_git_checkout_is_refused(lab, tmp_path):
    repo = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    state = repo / "deep" / "state"
    _assert_refused_before_state(lab, lab.run(TALOS_STATE_DIR=str(state)),
                                 state, "inside a git checkout")


@pytest.mark.parametrize("dot_git", ["directory", "worktree-file"])
def test_a_checkout_is_refused_even_without_git(lab, tmp_path, dot_git):
    repo = tmp_path / "checkout"
    if dot_git == "directory":
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
    else:   # what `git worktree add` and submodules leave behind
        repo.mkdir()
        (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/checkout\n")
    state = repo / "state"
    proc = lab.run(TALOS_STATE_DIR=str(state), PATH=_without_git(lab, tmp_path))
    _assert_refused_before_state(lab, proc, state, "inside a git checkout")


def test_a_failing_git_is_a_refusal_not_a_pass(lab, tmp_path):
    # e.g. "detected dubious ownership": git cannot say, so the answer is no
    broken = tmp_path / "broken-git"
    broken.mkdir()
    (broken / "git").write_text("#!/bin/sh\necho 'fatal: something unexpected' >&2\nexit 128\n")
    (broken / "git").chmod(0o755)
    state = tmp_path / "state"
    proc = lab.run(PATH=f"{broken}:{lab.bin}:/usr/bin:/bin")
    _assert_refused_before_state(lab, proc, state, "cannot tell whether")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can search any directory")
def test_an_uninspectable_ancestor_is_a_refusal(lab, tmp_path):
    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    state = locked / "inner" / "state"
    locked.chmod(0o600)   # readable listing, but not searchable
    try:
        proc = lab.run(TALOS_STATE_DIR=str(state))
    finally:
        locked.chmod(0o700)
    assert proc.returncode != 0
    assert "cannot inspect" in proc.stderr or "not searchable" in proc.stderr, proc.stderr
    assert lab.calls() == []


def test_an_empty_dot_git_directory_is_not_a_checkout(lab, tmp_path):
    # git's own test: no HEAD, no repository -- and git agrees
    (tmp_path / "plain" / ".git").mkdir(parents=True)
    _ok(lab.run(TALOS_STATE_DIR=str(tmp_path / "plain" / "state")))


# ---- B2: partial state never becomes a fresh identity ---------------------- #
def test_lost_secrets_stop_the_run_instead_of_a_new_identity(lab):
    _ok(lab.run())
    (lab.state / "secrets.yaml").unlink()
    n = len(lab.calls())
    proc = lab.run()
    assert proc.returncode != 0 and "restore it from backup" in proc.stderr
    assert not any(c.startswith("gen ") for c in lab.calls()[n:])
    assert not (lab.state / "secrets.yaml").exists()


@pytest.mark.parametrize("survivor", ["cluster.env", "talosconfig", "nodes", "patches"])
def test_bundle_and_fingerprint_lost_with_other_state_left(lab, survivor):
    _ok(lab.run())
    before = {p.name: _sha(p) for p in (lab.state / "nodes").glob("*.yaml")}
    (lab.state / "secrets.yaml").unlink()
    (lab.state / "secrets.sha256").unlink()
    for entry in ("cluster.env", "talosconfig", "nodes", "patches"):
        if entry != survivor:
            path = lab.state / entry
            subprocess.run(["rm", "-rf", str(path)], check=True)
    n = len(lab.calls())
    proc = lab.run()
    assert proc.returncode != 0
    assert "holds earlier state" in proc.stderr and survivor in proc.stderr, proc.stderr
    assert not any(c.startswith("gen ") for c in lab.calls()[n:])
    assert not (lab.state / "secrets.yaml").exists()
    if survivor == "nodes":
        assert {p.name: _sha(p) for p in (lab.state / "nodes").glob("*.yaml")} == before


def test_a_state_dir_with_only_run_leftovers_is_fresh(lab):
    # the lock file and an interrupted run's staging dir are not cluster state
    lab.state.mkdir(mode=0o700)
    (lab.state / ".lock").touch()
    (lab.state / ".staging.abc123").mkdir()
    _ok(lab.run())
    assert (lab.state / "secrets.yaml").exists()


# ---- B3: patches cannot replace identity, role or node identity ----------- #
OTHER_CLUSTER = "FOREIGN-VALUE"


@pytest.mark.parametrize("patch_file,body,violation", [
    ("worker-01.yaml", "machine:\n  type: controlplane\n", "machine.type"),
    ("all.yaml", "machine:\n  token: FOREIGN-VALUE\n", "machine.token"),
    ("controlplane.yaml", "machine:\n  ca:\n    crt: FOREIGN-VALUE\n    key: FOREIGN-VALUE\n",
     "machine.ca"),
    ("all.yaml", "machine:\n  acceptedCAs:\n    - crt: FOREIGN-VALUE\n", "machine.acceptedCAs"),
    ("all.yaml", "machine:\n  install:\n    image: registry.example/other:v1.99.0\n",
     "machine.install.image"),
    ("all.yaml", "machine:\n  install:\n    disk: /dev/vdb\n", "machine.install.disk"),
    ("cp-01.yaml", "cluster:\n  id: FOREIGN-VALUE\n", "cluster.id"),
    ("all.yaml", "cluster:\n  secret: FOREIGN-VALUE\n", "cluster.secret"),
    ("all.yaml", "cluster:\n  clusterName: other\n", "cluster.clusterName"),
    ("all.yaml", "cluster:\n  controlPlane:\n    endpoint: https://203.0.113.9:6443\n",
     "cluster.controlPlane.endpoint"),
    ("all.yaml", "cluster:\n  ca:\n    crt: FOREIGN-VALUE\n", "cluster.ca"),
    ("controlplane.yaml", "cluster:\n  etcd:\n    ca:\n      crt: FOREIGN-VALUE\n",
     "cluster.etcd.ca"),
    ("all.yaml", "cluster:\n  network:\n    podSubnets: [10.1.0.0/16]\n",
     "cluster.network.podSubnets"),
    ("worker.yaml",
     "machine:\n  network:\n    interfaces:\n      - interface: eth0\n        dhcp: true\n"
     "        vip:\n          ip: 192.168.53.5\n",
     "exactly the node's one interface"),
    ("all.yaml", "apiVersion: v1alpha1\nkind: HostnameConfig\nhostname: other\n",
     "added a HostnameConfig document"),
])
def test_a_patch_that_breaks_a_protected_setting_publishes_nothing(
        lab, tmp_path, patch_file, body, violation):
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / patch_file).write_text(body)
    proc = lab.run(TALOS_PATCH_DIR=str(patches))
    assert proc.returncode != 0
    assert violation in proc.stderr, proc.stderr
    assert "breaks a protected setting" in proc.stderr
    assert not (lab.state / "nodes").exists()
    assert not list(lab.state.glob(".staging.*"))
    # the checker names settings, never their values
    assert SECRET_MARK not in proc.stderr and OTHER_CLUSTER not in proc.stderr


def test_a_protected_change_on_rerun_keeps_the_published_configs(lab, tmp_path):
    _ok(lab.run())
    before = {p.name: _sha(p) for p in (lab.state / "nodes").glob("*.yaml")}
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "cp-02.yaml").write_text("machine:\n  type: worker\n")
    proc = lab.run(TALOS_PATCH_DIR=str(patches))
    assert proc.returncode != 0 and "cp-02: machine.type" in proc.stderr
    assert {p.name: _sha(p) for p in (lab.state / "nodes").glob("*.yaml")} == before


def test_the_checker_rejects_a_worker_vip_and_a_wrong_mac(tmp_path):
    check = os.path.join(BOX, "scripts", "check-node-config.py")
    base = {"version": "v1alpha1", "machine": {"type": "worker", "token": "t"},
            "cluster": {"id": "i"}}
    final = {"version": "v1alpha1", "machine": {
        "type": "worker", "token": "t",
        "network": {"hostname": "worker-01", "interfaces": [{
            "deviceSelector": {"hardwareAddr": "52:54:00:53:00:99"}, "dhcp": True,
            "vip": {"ip": "192.168.53.5"}}]}},
        "cluster": {"id": "i"}}
    (tmp_path / "base.yaml").write_text(yaml.safe_dump(base))
    (tmp_path / "final.yaml").write_text(yaml.safe_dump(final))
    proc = subprocess.run([sys.executable, check, str(tmp_path / "base.yaml"),
                           str(tmp_path / "final.yaml"), "worker-01", "worker",
                           "52:54:00:53:00:21", "192.168.53.5"],
                          capture_output=True, text=True)
    assert proc.returncode == 1
    assert "not selected by the node's mac" in proc.stderr
    assert "vip does not match the node's role" in proc.stderr


def test_tampered_secrets_are_refused(lab):
    _ok(lab.run())
    with open(lab.state / "secrets.yaml", "a") as fobj:
        fobj.write("# edited\n")
    proc = lab.run()
    assert proc.returncode != 0 and "fingerprint" in proc.stderr


@pytest.mark.parametrize("var,value", [
    ("TALOS_CLUSTER_NAME", "other"),
    ("TALOS_VIP", "192.168.53.6"),
    ("KUBERNETES_VERSION", "1.99.1"),
])
def test_identity_drift_is_refused(lab, var, value):
    _ok(lab.run())
    proc = lab.run(**{var: value})
    assert proc.returncode != 0 and "cluster.env" in proc.stderr


def test_talosctl_version_mismatch_stops_before_state(lab):
    proc = lab.run(FAKE_TALOS_VERSION="v1.98.0")
    assert proc.returncode != 0 and "inputs pin v1.99.0" in proc.stderr
    assert not lab.state.exists()


def test_a_concurrent_run_is_refused(lab):
    lab.state.mkdir(mode=0o700)
    with open(lab.state / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = lab.run()
    assert proc.returncode != 0 and ".lock" in proc.stderr
    assert not any(c.startswith("gen ") for c in lab.calls())


def test_a_failed_validation_publishes_nothing(lab):
    proc = lab.run(FAKE_VALIDATE_RC="1")
    assert proc.returncode != 0
    assert not (lab.state / "nodes").exists()
    assert not list(lab.state.glob(".staging.*"))


@pytest.mark.parametrize("override,message", [
    ({"TALOS_VERSION": None}, "TALOS_VERSION is not set"),
    ({"TALOS_NTP_SERVERS": "PLACEHOLDER-ntp-server"}, "placeholder"),
    ({"TALOS_VERSION": "v1.99"}, "exact release"),
    ({"TALOS_INSTALLER_IMAGE": "registry.example/installer/abc:v1.98.0"}, "must be tagged"),
    ({"TALOS_STATE_DIR": "relative/state"}, "absolute path"),
    ({"TALOS_POD_CIDR": "10.96.0.0/12"}, "must differ"),
    ({"TALOS_VIP": "192.168.53.11"}, "uses the VIP"),
])
def test_bad_inputs_fail_before_any_talosctl_call(lab, override, message):
    proc = lab.run(**override)
    assert proc.returncode != 0 and message in proc.stderr, proc.stderr
    assert lab.calls() == []


@pytest.mark.parametrize("table,message", [
    ("cp-01 controlplane 192.168.53.11 52:54:00:53:00:11\n"
     "worker-01 worker 192.168.53.21 52:54:00:53:00:21\n"
     "worker-02 worker 192.168.53.22 52:54:00:53:00:22\n", "got 1+2"),
    ("cp-01 controlplane 192.168.53.11 52:54:00:53:00:11\n"
     "worker-01 worker 192.168.53.21 52:54:00:53:00:11\n", "duplicate mac"),
    ("cp-01 etcd 192.168.53.11 52:54:00:53:00:11\n", "unknown role"),
    ("cp-01 controlplane 192.168.53.11\n", "expected"),
])
def test_bad_node_tables_fail_before_any_talosctl_call(lab, table, message):
    lab.nodes.write_text(table)
    proc = lab.run()
    assert proc.returncode != 0 and message in proc.stderr, proc.stderr
    assert lab.calls() == []


def test_one_plus_one_smoke_profile_is_accepted(lab):
    _node_table(lab.nodes, cps=["cp-01"], workers=["worker-01"])
    proc = _ok(lab.run())
    assert proc.stdout.splitlines() == ["cp-01: created", "worker-01: created"]


def test_shipped_inputs_example_is_refused_as_is(lab):
    env = {k: None for k in ("TALOS_STATE_DIR", "TALOS_VERSION", "TALOS_NTP_SERVERS",
                             "TALOS_INSTALLER_IMAGE", "KUBERNETES_VERSION")}
    proc = lab.run("--inputs", os.path.join(BOX, "inputs.env.example"), **env)
    assert proc.returncode != 0 and "placeholder" in proc.stderr
    assert lab.calls() == []
