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

With TALOS_UNATTENDED_TALOSCTL pointing at a talosctl of the pinned release,
the generation tests also run against it (no cluster or network needed).
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import runpy
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
VERSIONS_ENV = os.path.join(BOX, "versions.env")

TALOS_VERSION = "v1.99.0"   # synthetic: these tests pin no real release
SECRET_MARK = "FAKESECRET"

CP = [f"cp-0{i}" for i in (1, 2, 3)]
WORKERS = [f"worker-0{i}" for i in (1, 2, 3)]


# --------------------------------------------------------------------------- #
# conf.yml                                                                    #
# --------------------------------------------------------------------------- #
def _pins() -> dict:
    """versions.env as a dict, comments and blank lines skipped."""
    with open(VERSIONS_ENV) as fobj:
        lines = [line.rstrip("\n") for line in fobj]
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def _render(monkeypatch, **env) -> dict:
    """conf.yml rendered with the shipped pins and *env* only."""
    for var in ("TALOS_LAB_DIR", "TALOS_LAB_PREFIX", "TALOS_OPERATOR_KEY",
                "TALOS_OPERATOR_PUBKEY", *_pins()):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
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


# ---- the pinned version set ------------------------------------------------ #
def test_versions_env_is_plain_key_value_data():
    # read by Jinja (conf.yml) and by gen-config.sh as data, never sourced:
    # nothing in it may need quoting or expansion to mean the same to both
    with open(VERSIONS_ENV) as fobj:
        for line in fobj.read().splitlines():
            if line and not line.startswith("#"):
                assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[A-Za-z0-9._:/@+-]+", line), line


def test_the_pins_form_one_consistent_tested_set():
    pins = _pins()
    talos, schematic = pins["TALOS_VERSION"], pins["TALOS_SCHEMATIC_ID"]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", talos)
    assert re.fullmatch(r"[0-9a-f]{64}", schematic)
    # boot ISO and installer image from the same schematic and release
    assert pins["TALOS_ISO_URI"] == (
        f"https://factory.talos.dev/image/{schematic}/{talos}/metal-amd64.iso")
    image, digest = pins["TALOS_INSTALLER_IMAGE"].split("@")
    assert image == f"factory.talos.dev/metal-installer/{schematic}:{talos}"
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", pins["TALOS_ISO_CHECKSUM"])
    assert pins["TALOS_ISO_CHECKSUM"] != "sha256:" + "0" * 64
    # kubectl at the cluster's Kubernetes version
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins["KUBERNETES_VERSION"])
    assert pins["KUBECTL_VERSION"] == "v" + pins["KUBERNETES_VERSION"]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", pins["FLUX_VERSION"])
    for tool in ("TALOSCTL", "KUBECTL", "FLUX"):
        assert re.fullmatch(r"[0-9a-f]{64}", pins[f"{tool}_SHA256"]), tool


def test_conf_uses_the_pinned_iso_and_tools(monkeypatch):
    pins = _pins()
    cfg = _render(monkeypatch)
    assert cfg["isos"]["talos-install"] == {
        "uri": pins["TALOS_ISO_URI"], "checksum": pins["TALOS_ISO_CHECKSUM"]}
    script = _prepare_script(yaml.safe_load(cfg["templates"]["control-base"]["cloudinit"]))
    flux = pins["FLUX_VERSION"]
    for url, sha in (
            (f"/talos/releases/download/{pins['TALOS_VERSION']}/talosctl-linux-amd64",
             pins["TALOSCTL_SHA256"]),
            (f"dl.k8s.io/release/{pins['KUBECTL_VERSION']}/bin/linux/amd64/kubectl",
             pins["KUBECTL_SHA256"]),
            (f"/flux2/releases/download/{flux}/flux_{flux[1:]}_linux_amd64.tar.gz",
             pins["FLUX_SHA256"])):
        line = next(x for x in script.splitlines() if url in x)
        assert line.endswith(f'"{sha}"'), line


def test_an_environment_variable_overrides_a_pin(monkeypatch):
    cfg = _render(monkeypatch)
    over = _render(monkeypatch, TALOS_ISO_CHECKSUM="sha256:" + "ab" * 32, FLUX_VERSION="v9.9.9")
    assert over["isos"]["talos-install"]["checksum"] == "sha256:" + "ab" * 32
    assert over["isos"]["talos-install"]["uri"] == cfg["isos"]["talos-install"]["uri"]
    assert "flux_9.9.9_linux_amd64.tar.gz" in over["templates"]["control-base"]["cloudinit"]


def test_boxmans_own_loader_reads_the_pins(tmp_path, monkeypatch):
    # boxman pre-processes conf.yml before Jinja sees it; the include must
    # survive that, from a copy so the rendered dump stays out of the repo
    for var in _pins():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BOXMAN_CONF_FILE", str(tmp_path / "conf.yml"))
    monkeypatch.setenv("BOXMAN_CONF_DIR", str(tmp_path))
    for name in ("conf.yml", "versions.env"):
        (tmp_path / name).write_text(open(os.path.join(BOX, name)).read())
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.logger = logging.getLogger(__name__)
    cfg = mgr.load_config(str(tmp_path / "conf.yml"))
    assert cfg["isos"]["talos-install"]["checksum"] == _pins()["TALOS_ISO_CHECKSUM"]


def _template_userdata(monkeypatch, **env) -> dict:
    return yaml.safe_load(_render(monkeypatch, **env)["templates"]["control-base"]["cloudinit"])


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
            script = tmp_path / "prepare-control-template"
            script.write_text(_prepare_script(_template_userdata(monkeypatch, **self.pins)))
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
    "download_fails", "checksum_mismatch", "archive_lacks_binary", "pin_empty"])
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
    elif breakage == "pin_empty":
        build.pins["KUBECTL_SHA256"] = ""
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
#: Records every invocation and stands in for talosctl v1.14 with real YAML.
#: Secrets carry recognisable FAKESECRET values. `gen config` writes the
#: multi-document layout talosctl v1.14.5 writes (same documents, keys and
#: roles; test_the_fake_matches_the_real_layout compares them when a real
#: talosctl is supplied). `machineconfig patch` merges the way talosctl does:
#: a patch document without a kind merges into the v1alpha1 one, one with a
#: kind into the document of that kind and name, or is appended; maps merge,
#: lists append (KubeNetworkConfig's subnets are replaced) and `$patch: delete`
#: removes a document. `validate` applies the checks the real one was seen to
#: make on these configs. Output is a function of the inputs, so an unchanged
#: rerun is byte-identical; the talosconfig carries a fresh key each time, as
#: the real one does. Shebang filled in by the fixture: the test interpreter.
FAKE_TALOSCTL = r"""#!@PYTHON@
import ipaddress, os, secrets, sys
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


REPLACED = {("KubeNetworkConfig", "podSubnets"), ("KubeNetworkConfig", "serviceSubnets")}


def merge(dst, src, kind=None):
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            merge(dst[key], value)
        elif (isinstance(value, list) and isinstance(dst.get(key), list)
              and (kind, key) not in REPLACED):
            dst[key] = dst[key] + value
        else:
            dst[key] = value


def key(doc):
    return (doc["kind"], doc.get("name")) if "kind" in doc else ("v1alpha1", None)


def load(path):
    with open(path) as fobj:
        return [d for d in yaml.safe_load_all(fobj) if d is not None]


def patched(docs, patch_files):
    for ref in patch_files:
        for doc in load(ref.lstrip("@")):
            match = [d for d in docs if key(d) == key(doc)]
            if doc.get("$patch") == "delete":
                docs = [d for d in docs if key(d) != key(doc)]
            elif match:
                merge(match[0], doc, doc.get("kind"))
            else:
                docs.append(doc)
    return docs


def dump(docs, path):
    with open(path, "w") as fobj:
        yaml.safe_dump_all(docs, fobj, sort_keys=False)


def fresh():
    return "FAKESECRET-" + secrets.token_hex(8)


def doc(kind, **fields):
    return {"apiVersion": "v1alpha1", "kind": kind, **fields}


def role_config(role, name, endpoint, sec, k8s):
    certs, cp = sec["certs"], role == "controlplane"
    main = {"version": "v1alpha1", "debug": False, "persist": True,
            "machine": {"type": role, "token": sec["trustdinfo"]["token"],
                        "ca": {"crt": certs["os"]["crt"], "key": certs["os"]["key"] if cp else ""},
                        "certSANs": [], "features": {"diskQuotaSupport": True}},
            "cluster": {"token": sec["secrets"]["bootstraptoken"]}}
    if cp:
        main["cluster"]["etcd"] = {"ca": certs["etcd"]}
    node = {"nodeIP": {}}
    if cp:
        node["labels"] = {"node-role.kubernetes.io/control-plane": "",
                          "node.kubernetes.io/exclude-from-external-load-balancers": ""}
        node["taints"] = {"node-role.kubernetes.io/control-plane": "NoSchedule"}
    docs = [
        main,
        doc("DiscoveryServiceConfig", name="default", endpoint="https://discovery.talos.dev/"),
        doc("DiscoveryIdentityConfig", clusterID=sec["cluster"]["id"],
            clusterSecret=sec["cluster"]["secret"]),
        doc("UnattendedInstallConfig", installer={"image": opt("--install-image")},
            provisioning={"diskSelector": {
                "match": 'disk.dev_path == "%s"' % opt("--install-disk")}, "wipe": False}),
        doc("ResolverConfig", hostDNS={"enabled": True, "forwardKubeDNSToHost": True}),
        doc("VolumeConfig", name="EPHEMERAL", mount={"secure": True}),
        doc("FilesystemTrimConfig", interval="168h0m0s"),
        doc("SecurityProfileConfig", workloadIsolation=True),
        doc("HostnameConfig", auto="stable"),
        doc("KubeClusterConfig", clusterName=name, endpoint=endpoint),
        doc("KubeNodeConfig", **node),
        doc("KubeletConfig", image="ghcr.io/siderolabs/kubelet:v" + k8s, config={},
            defaultRuntimeSeccompProfileEnabled=True),
        doc("KubeNetworkConfig", dnsDomain="cluster.local", podSubnets=["10.244.0.0/16"],
            serviceSubnets=["10.96.0.0/12"]),
        doc("KubeAPIServerCAConfig", **({"issuingCA": {"cert": certs["k8s"]["crt"],
                                                       "key": certs["k8s"]["key"]}}
                                        if cp else {"acceptedCAs": [certs["k8s"]["crt"]]})),
        doc("KubePrismConfig", port=7445),
    ]
    if cp:
        image = "registry.k8s.io/kube-%s:v" + k8s
        docs += [
            doc("KubeAggregatorCAConfig", issuingCA={"cert": certs["k8saggregator"]["crt"],
                                                     "key": certs["k8saggregator"]["key"]}),
            doc("KubeAdmissionControlConfig", name="PodSecurity", configuration={
                "apiVersion": "pod-security.admission.config.k8s.io/v1alpha1",
                "defaults": {"audit": "restricted", "audit-version": "latest",
                             "enforce": "baseline", "enforce-version": "latest",
                             "warn": "restricted", "warn-version": "latest"},
                "exemptions": {"namespaces": ["kube-system"], "runtimeClasses": [],
                               "usernames": []},
                "kind": "PodSecurityConfiguration"}),
            doc("KubeAuditPolicyConfig", configuration={
                "apiVersion": "audit.k8s.io/v1", "kind": "Policy",
                "rules": [{"level": "Metadata"}]}),
            doc("KubeAuthenticationConfig", configuration={
                "anonymous": {"conditions": [{"path": "/livez"}, {"path": "/readyz"},
                                             {"path": "/healthz"}], "enabled": True},
                "jwt": []}),
            doc("KubeAuthorizerConfig", name="node", type="Node"),
            doc("KubeAuthorizerConfig", name="rbac", type="RBAC"),
            doc("KubeEtcdEncryptionConfig", config={"resources": [{
                "providers": [{"secretbox": {"keys": [{
                    "name": "key2", "secret": sec["secrets"]["secretboxencryptionsecret"]}]}}],
                "resources": ["secrets"]}]}),
            doc("KubeServiceAccountConfig", issuer={"privateKey": sec["serviceaccount"]["key"],
                                                    "issuerURL": endpoint}),
            doc("KubeAPIServerConfig", image=image % "apiserver"),
            doc("KubeControllerManagerConfig", image=image % "controller-manager"),
            doc("KubeSchedulerConfig", image=image % "scheduler", config={}),
            doc("KubeProxyConfig", image=image % "proxy", config={}),
            doc("KubeFlannelCNIConfig", backendType="vxlan", backendPort=4789),
            doc("KubeCoreDNSConfig", enabled=True),
        ]
    return docs


def validate(path):
    docs = load(path)
    main = next(d for d in docs if "kind" not in d)
    kinds = {key(d): d for d in docs}
    errors = []
    host = kinds.get(("HostnameConfig", None))
    if host is not None:
        if host.get("hostname") and host.get("auto") not in ("off", False):
            errors.append("'auto' and 'hostname' cannot be set at the same time")
        if (main.get("machine") or {}).get("network", {}).get("hostname"):
            errors.append("static hostname is already set in v1alpha1 config")
    if (main.get("cluster") or {}).get("network"):
        errors.append("cluster network config is already set in the v1alpha1 config")
    net = kinds.get(("KubeNetworkConfig", None)) or {}
    for cidr in net.get("podSubnets", []) + net.get("serviceSubnets", []):
        try:
            ipaddress.ip_network(cidr)
        except ValueError:
            errors.append("invalid subnet: %s is not a valid CIDR" % cidr)
    if errors or os.environ.get("FAKE_VALIDATE_RC", "0") != "0":
        sys.exit("%d errors occurred:\n" % len(errors) + "\n".join("\t* " + e for e in errors))
    print(path + " is valid for metal mode")


cmd = " ".join(argv[:2])
if cmd == "version --client":
    print("Client:\nTalos " + os.environ["FAKE_TALOS_VERSION"])
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
    out = opt("--output")
    os.makedirs(out)
    for role in ("controlplane", "worker"):
        docs = role_config(role, name, endpoint, sec, opt("--kubernetes-version"))
        dump(patched(docs, opts("--config-patch")), os.path.join(out, role + ".yaml"))
    with open(os.path.join(out, "talosconfig"), "w") as fobj:
        fobj.write("client-key: " + fresh() + "\n")
elif cmd == "machineconfig patch":
    dump(patched(load(argv[2]), opts("--patch")), opt("--output"))
elif cmd == "validate --config":
    validate(argv[2])
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
    # synthetic tests pin no real release: an empty version set, every value
    # from the environment below; tests of the shipped pins pass versions=None
    no_pins = tmp_path / "no-pins.env"
    no_pins.write_text("# no pins\n")
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

        def run(self, *args, versions=no_pins, **overrides):
            e = dict(env, **overrides)
            for key in [k for k, v in overrides.items() if v is None]:
                del e[key]
            pins = ["--versions", str(versions)] if versions else []
            return subprocess.run(["bash", GEN_CONFIG, "--nodes", str(self.nodes), *pins, *args],
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


def _docs(path) -> dict:
    """{(kind, name): document} of a multi-document machine config."""
    docs = {}
    for doc in yaml.safe_load_all(path.read_text()):
        if doc:
            docs[(doc["kind"], doc.get("name")) if "kind" in doc else ("v1alpha1", None)] = doc
    return docs


def _assert_node_configs(nodes_dir, names, image, ntp):
    """The patch forms gen-config writes, as they land in each final config."""
    for name in names:
        docs = _docs(nodes_dir / f"{name}.yaml")
        role = "controlplane" if name.startswith("cp-") else "worker"
        mac = f"52:54:00:53:00:{1 if role == 'controlplane' else 2}{name[-1]}"
        assert docs[("v1alpha1", None)]["machine"]["type"] == role
        assert docs[("HostnameConfig", None)] == {
            "apiVersion": "v1alpha1", "kind": "HostnameConfig", "auto": "off", "hostname": name}
        assert docs[("LinkAliasConfig", "fabric")]["selector"] == {
            "match": f'mac(link.permanent_addr) == "{mac}"'}
        assert ("DHCPv4Config", "fabric") in docs
        vips = [k for k in docs if k[0] == "Layer2VIPConfig"]
        if role == "controlplane":
            assert vips == [("Layer2VIPConfig", "192.168.53.5")]
            assert docs[vips[0]]["link"] == "fabric"
        else:
            assert vips == []
        assert docs[("TimeSyncConfig", None)]["ntp"] == {"servers": ntp}
        net = docs[("KubeNetworkConfig", None)]
        assert (net["podSubnets"], net["serviceSubnets"]) == (["10.244.0.0/16"], ["10.96.0.0/12"])
        install = docs[("UnattendedInstallConfig", None)]
        assert install["installer"] == {"image": image}
        assert install["provisioning"]["diskSelector"] == {"match": 'disk.dev_path == "/dev/vda"'}
        # no pre-v1.12 forms that would conflict with the documents
        assert "network" not in docs[("v1alpha1", None)]["machine"]
        assert "network" not in docs[("v1alpha1", None)]["cluster"]


def test_node_configs_carry_role_identity_ntp_and_vip(lab):
    _ok(lab.run())
    _assert_node_configs(lab.state / "nodes", CP + WORKERS,
                         f"registry.example/installer/abc:{TALOS_VERSION}",
                         ["ntp1.example", "ntp2.example"])


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
    assert "cannot inspect" in proc.stderr, proc.stderr
    assert lab.calls() == []


def _nested_checkout(tmp_path):
    """A checkout with a nested directory, and a link to it from outside."""
    repo = tmp_path / "checkout"
    (repo / "sub" / "dir").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "link").symlink_to(repo / "sub" / "dir")
    return repo, outside / "link"


@pytest.mark.parametrize("with_git", [False, True], ids=["no-git", "git"])
def test_a_symlink_into_a_nested_checkout_directory_is_refused(lab, tmp_path, with_git):
    # review B4b: the parent walk followed the path as written, so with no
    # git OUT/link -> REPO/sub/dir let OUT/link/state land inside REPO
    repo, link = _nested_checkout(tmp_path)
    path = None if with_git else _without_git(lab, tmp_path)
    proc = lab.run(TALOS_STATE_DIR=str(link / "state"), **({"PATH": path} if path else {}))
    assert proc.returncode != 0
    assert "inside a git" in proc.stderr, proc.stderr
    assert not (repo / "sub" / "dir" / "state").exists()
    assert lab.calls() == []


def test_a_dot_dot_below_a_missing_directory_is_refused(lab, tmp_path):
    # OUT/new does not exist, so OUT/new/../link resolves only when mkdir
    # creates it -- after any check of the path as it stood
    repo, link = _nested_checkout(tmp_path)
    state = link.parent / "new" / ".." / "link" / "state"
    proc = lab.run(TALOS_STATE_DIR=str(state), PATH=_without_git(lab, tmp_path))
    assert proc.returncode != 0 and ". or .. components" in proc.stderr, proc.stderr
    assert not (repo / "sub" / "dir" / "state").exists()
    assert not (link.parent / "new").exists()
    assert lab.calls() == []


def test_a_component_swapped_for_a_symlink_after_the_checks_is_caught(lab, tmp_path):
    # the checks pass on a real directory; it is replaced by a symlink into a
    # checkout just before mkdir -- the physical re-check must stop the run
    repo, link = _nested_checkout(tmp_path)
    real = link.parent / "real"
    real.mkdir()
    swap = tmp_path / "swap-bin"
    swap.mkdir()
    (swap / "mkdir").write_text(
        "#!/bin/sh\n"
        f"if [ ! -e '{tmp_path}/swapped' ]; then\n"
        f"  touch '{tmp_path}/swapped'; rmdir '{real}'; ln -s '{repo}/sub/dir' '{real}'\n"
        "fi\n"
        'exec /bin/mkdir "$@"\n')
    (swap / "mkdir").chmod(0o755)
    proc = lab.run(TALOS_STATE_DIR=str(real / "state"),
                   PATH=f"{swap}:{_without_git(lab, tmp_path)}")
    assert (tmp_path / "swapped").exists()
    assert proc.returncode != 0
    assert "not the checked" in proc.stderr, proc.stderr
    assert not (repo / "sub" / "dir" / "state" / "secrets.yaml").exists()
    assert not any(c.startswith("gen ") for c in lab.calls())


def test_a_symlinked_state_path_is_used_by_its_physical_location(lab, tmp_path):
    target = tmp_path / "private"
    target.mkdir()
    (tmp_path / "via").symlink_to(target)
    proc = _ok(lab.run(TALOS_STATE_DIR=str(tmp_path / "via" / "state")))
    assert (target / "state" / "secrets.yaml").exists()
    assert f"--with-secrets {os.path.realpath(target)}/state/secrets.yaml" in "\n".join(lab.calls())
    assert SECRET_MARK not in proc.stdout + proc.stderr


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
#: the same as certificate material: talosctl decodes v1alpha1 CA fields as
#: base64 while patching, before any check could see them
OTHER_CA = "Rk9SRUlHTi1WQUxVRQ=="


def _kind(kind, body=""):
    return f"apiVersion: v1alpha1\nkind: {kind}\n{body}"


#: (patch file, patch, the violation the checker must name)
PROTECTED_PATCHES = [
    # the v1alpha1 document's own identity, and legacy fields that would
    # duplicate (and might override) the v1.12+ documents
    ("worker-01.yaml", "machine:\n  type: controlplane\n", "machine.type"),
    ("all.yaml", "machine:\n  token: FOREIGN-VALUE\n", "machine.token"),
    ("controlplane.yaml", f"machine:\n  ca:\n    crt: {OTHER_CA}\n    key: {OTHER_CA}\n",
     "machine.ca"),
    ("all.yaml", f"machine:\n  acceptedCAs:\n    - crt: {OTHER_CA}\n", "machine.acceptedCAs"),
    ("all.yaml", "cluster:\n  token: FOREIGN-VALUE\n", "cluster.token"),
    ("controlplane.yaml", f"cluster:\n  etcd:\n    ca:\n      crt: {OTHER_CA}\n",
     "cluster.etcd.ca"),
    ("all.yaml", "machine:\n  install:\n    image: registry.example/other:v1.99.0\n",
     "machine.install"),
    ("cp-01.yaml", "cluster:\n  id: FOREIGN-VALUE\n", "cluster.id"),
    ("all.yaml", "cluster:\n  clusterName: other\n", "cluster.clusterName"),
    ("all.yaml", "cluster:\n  controlPlane:\n    endpoint: https://203.0.113.9:6443\n",
     "cluster.controlPlane"),
    ("all.yaml", "cluster:\n  network:\n    podSubnets: [10.1.0.0/16]\n", "cluster.network"),
    ("worker.yaml", "machine:\n  network:\n    interfaces:\n      - interface: eth0\n"
     "        dhcp: true\n        vip:\n          ip: 192.168.53.5\n", "machine.network"),
    # cluster identity, trust and install inputs, now in their own documents
    ("all.yaml", _kind("KubeClusterConfig", "clusterName: other\n"),
     "the KubeClusterConfig document was changed"),
    ("all.yaml", _kind("KubeClusterConfig", "endpoint: https://203.0.113.9:6443\n"),
     "the KubeClusterConfig document was changed"),
    ("all.yaml", _kind("DiscoveryIdentityConfig", "clusterID: FOREIGN-VALUE\n"),
     "the DiscoveryIdentityConfig document was changed"),
    ("controlplane.yaml", _kind("KubeAPIServerCAConfig", "issuingCA:\n  cert: FOREIGN-VALUE\n"),
     "the KubeAPIServerCAConfig document was changed"),
    ("worker.yaml", _kind("KubeAPIServerCAConfig", "acceptedCAs:\n  - FOREIGN-VALUE\n"),
     "the KubeAPIServerCAConfig document was changed"),
    ("controlplane.yaml", _kind("KubeAggregatorCAConfig", "issuingCA:\n  key: FOREIGN-VALUE\n"),
     "the KubeAggregatorCAConfig document was changed"),
    ("controlplane.yaml",
     _kind("KubeServiceAccountConfig", "issuer:\n  privateKey: FOREIGN-VALUE\n"),
     "the KubeServiceAccountConfig document was changed"),
    ("controlplane.yaml", _kind("KubeEtcdEncryptionConfig",
                                "config:\n  resources:\n    - providers: [{identity: {}}]\n"
                                "      resources: [secrets]\n"),
     "the KubeEtcdEncryptionConfig document was changed"),
    ("controlplane.yaml", _kind("KubeAuthenticationConfig",
                                "configuration:\n  jwt:\n    - issuer: {url: https://other}\n"),
     "the KubeAuthenticationConfig document was changed"),
    ("controlplane.yaml", _kind("KubeAuthorizerConfig", "name: rbac\ntype: AlwaysAllow\n"),
     "the KubeAuthorizerConfig document (rbac) was changed"),
    ("controlplane.yaml", _kind("KubeAuthorizerConfig", "name: rbac\n$patch: delete\n"),
     "a patch deleted the KubeAuthorizerConfig document (rbac)"),
    ("all.yaml", _kind("KubeNetworkConfig", "podSubnets: [10.1.0.0/16]\n"),
     "the KubeNetworkConfig document was changed"),
    ("all.yaml", _kind("UnattendedInstallConfig", "installer:\n  image: registry.example/other\n"),
     "the UnattendedInstallConfig document was changed"),
    ("all.yaml", _kind("UnattendedInstallConfig",
                       "provisioning:\n  diskSelector:\n    match: 'disk.dev_path == \"/dev/vdb\"'\n"),
     "the UnattendedInstallConfig document was changed"),
    ("all.yaml", _kind("TrustedRootsConfig", "name: other\ncertificates: FOREIGN-VALUE\n"),
     "a patch added the TrustedRootsConfig document (other)"),
    # the node's identity: one MAC-selected link on DHCP, the VIP by role
    ("cp-01.yaml", _kind("DHCPv4Config", "name: fabric\nignoreRoutes: true\n"),
     "the DHCPv4Config document (fabric) differs from the generated node patch"),
    ("worker.yaml", _kind("Layer2VIPConfig", "name: 192.168.53.5\nlink: fabric\n"),
     "the vip does not match the node's role"),
    ("all.yaml", _kind("LinkAliasConfig", "name: other\nselector:\n  match: 'true'\n"),
     "a patch added the LinkAliasConfig document (other)"),
    ("all.yaml", _kind("LinkConfig", "name: fabric\naddresses:\n  - address: 192.168.53.99/24\n"),
     "a patch added the LinkConfig document (fabric)"),
]


@pytest.mark.parametrize("patch_file,body,violation", PROTECTED_PATCHES)
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
    assert SECRET_MARK not in proc.stderr
    assert OTHER_CLUSTER not in proc.stderr and OTHER_CA not in proc.stderr


def test_a_protected_change_on_rerun_keeps_the_published_configs(lab, tmp_path):
    _ok(lab.run())
    before = {p.name: _sha(p) for p in (lab.state / "nodes").glob("*.yaml")}
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "cp-02.yaml").write_text("machine:\n  type: worker\n")
    proc = lab.run(TALOS_PATCH_DIR=str(patches))
    assert proc.returncode != 0 and "cp-02: machine.type" in proc.stderr
    assert {p.name: _sha(p) for p in (lab.state / "nodes").glob("*.yaml")} == before


CHECK = os.path.join(BOX, "scripts", "check-node-config.py")


def _check(tmp_path, base, patch, final, *args):
    for name, docs in (("base", base), ("patch", patch), ("final", final)):
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump_all(docs))
    return subprocess.run([sys.executable, CHECK, *(str(tmp_path / f"{n}.yaml")
                                                    for n in ("base", "patch", "final")), *args],
                          capture_output=True, text=True)


def test_the_checker_checks_the_node_identity_independently_of_the_patch(tmp_path):
    # even a generated patch that went wrong (another node's MAC, a VIP on a
    # worker, YAML's "off" read as false) is judged against the arguments
    main = {"version": "v1alpha1", "machine": {"type": "worker", "token": "t"}}
    patch = [{"apiVersion": "v1alpha1", "kind": "HostnameConfig", "auto": False,
              "hostname": "worker-01"},
             {"apiVersion": "v1alpha1", "kind": "LinkAliasConfig", "name": "fabric",
              "selector": {"match": 'mac(link.permanent_addr) == "52:54:00:53:00:99"'}},
             {"apiVersion": "v1alpha1", "kind": "DHCPv4Config", "name": "fabric"},
             {"apiVersion": "v1alpha1", "kind": "Layer2VIPConfig", "name": "192.168.53.5",
              "link": "fabric"}]
    proc = _check(tmp_path, [main], patch, [main, *patch],
                  "worker-01", "worker", "52:54:00:53:00:21", "192.168.53.5")
    assert proc.returncode == 1
    assert "HostnameConfig" not in proc.stderr          # auto: off parsed as false is fine
    assert "not selected by exactly its mac" in proc.stderr
    assert "dhcp is not configured on exactly the node's link" in proc.stderr
    assert "vip does not match the node's role" in proc.stderr


def test_the_checker_accepts_exactly_the_generated_node_identity(tmp_path):
    main = {"version": "v1alpha1", "machine": {"type": "controlplane", "token": "t"}}
    host = {"apiVersion": "v1alpha1", "kind": "HostnameConfig", "auto": "stable"}
    patch = [{"apiVersion": "v1alpha1", "kind": "HostnameConfig", "auto": "off",
              "hostname": "cp-01"},
             {"apiVersion": "v1alpha1", "kind": "LinkAliasConfig", "name": "fabric",
              "selector": {"match": 'mac(link.permanent_addr) == "52:54:00:53:00:11"'}},
             {"apiVersion": "v1alpha1", "kind": "DHCPv4Config", "name": "fabric"},
             {"apiVersion": "v1alpha1", "kind": "Layer2VIPConfig", "name": "192.168.53.5",
              "link": "fabric"}]
    proc = _check(tmp_path, [main, host], patch, [main, *patch],
                  "cp-01", "controlplane", "52:54:00:53:00:11", "192.168.53.5")
    assert proc.returncode == 0, proc.stderr


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
    ({"TALOS_POD_CIDR": "10.96.0.0/12"}, "must not overlap"),
    ({"TALOS_POD_CIDR": "10.0.0.0/8"}, "must not overlap"),
    ({"TALOS_SERVICE_CIDR": "10.100.0.0/12"}, "has host bits set"),
    ({"TALOS_POD_CIDR": "192.168.0.0/16"}, "lies inside 192.168.0.0/16"),
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


# ---- gen-config.sh reads the pinned version set ---------------------------- #
PINNED_VARS = {"TALOS_VERSION": None, "KUBERNETES_VERSION": None, "TALOS_INSTALLER_IMAGE": None}


def test_gen_config_defaults_to_the_shipped_version_set(lab):
    pins = _pins()
    out = _ok(lab.run("--dry-run", versions=None, **PINNED_VARS))
    plan = out.stdout
    assert f"--talos-version {pins['TALOS_VERSION']}" in plan
    assert f"--kubernetes-version {pins['KUBERNETES_VERSION']}" in plan
    assert f"--install-image {pins['TALOS_INSTALLER_IMAGE']}" in plan
    assert "overrides the pinned" not in out.stderr


def test_an_overridden_pin_is_used_and_logged(lab):
    pins = _pins()
    proc = _ok(lab.run("--dry-run", versions=None, **dict(PINNED_VARS, KUBERNETES_VERSION="1.36.0")))
    assert "--kubernetes-version 1.36.0" in proc.stdout
    assert f"KUBERNETES_VERSION=1.36.0 overrides the pinned {pins['KUBERNETES_VERSION']}" \
        in proc.stderr


@pytest.mark.parametrize("body,message", [
    ("TALOS_VERSION=$(touch pwned)\n", "expected KEY=value"),
    ('TALOS_VERSION="v1.2.3"\n', "expected KEY=value"),
    ("TALOS_VERSION=v1.2.3 # trailing comment\n", "expected KEY=value"),
    (None, "cannot read the version set"),
])
def test_a_malformed_or_missing_version_set_stops_the_run(lab, tmp_path, body, message):
    versions = tmp_path / "versions.env"
    if body is not None:
        versions.write_text(body)
    proc = lab.run("--dry-run", versions=versions)
    assert proc.returncode != 0 and message in proc.stderr, proc.stderr
    assert not (tmp_path / "pwned").exists()
    assert lab.calls() == []


# --------------------------------------------------------------------------- #
# the same, against a real talosctl at the pinned version (opt-in)            #
# --------------------------------------------------------------------------- #
#: Path to a talosctl binary at versions.env's TALOS_VERSION. These tests run
#: only when it is set: they check what the fake can only approximate -- that
#: the patch forms validate (`--strict`), that every protected change is
#: caught after Talos' own merge, that `gen config` is deterministic and that
#: the fake writes the same layout. No cluster or network is needed.
REAL_TALOSCTL = os.environ.get("TALOS_UNATTENDED_TALOSCTL")
real = pytest.mark.skipif(
    not REAL_TALOSCTL, reason="set TALOS_UNATTENDED_TALOSCTL to a talosctl at the pinned version")


@pytest.fixture
def real_lab(lab, tmp_path):
    """lab, with the real talosctl and the shipped version set."""
    have = subprocess.run([REAL_TALOSCTL, "version", "--client", "--short"],
                          capture_output=True, text=True).stdout
    pinned = _pins()["TALOS_VERSION"]
    assert f"Talos {pinned}" in have, f"TALOS_UNATTENDED_TALOSCTL is not {pinned}: {have}"
    real_bin = tmp_path / "real-bin"
    real_bin.mkdir()
    (real_bin / "talosctl").symlink_to(os.path.abspath(REAL_TALOSCTL))
    (real_bin / "python3").symlink_to(lab.bin / "python3")
    base_run = lab.run

    def run(*args, **overrides):
        env = dict({"PATH": f"{real_bin}:/usr/bin:/bin"}, **PINNED_VARS, **overrides)
        return base_run(*args, versions=None, **env)

    lab.run = run
    lab.talosctl = str(real_bin / "talosctl")
    return lab


@real
def test_real_talosctl_validates_the_configs_and_a_rerun_changes_nothing(real_lab):
    proc = _ok(real_lab.run())
    assert proc.stdout.splitlines() == [f"{n}: created" for n in CP + WORKERS]
    assert proc.stderr.count("is valid for metal mode") == 6      # validate --strict
    files = [real_lab.state / "talosconfig", *(real_lab.state / "nodes").glob("*.yaml")]
    before = {p.name: _sha(p) for p in files}
    proc = _ok(real_lab.run())
    assert proc.stdout.splitlines() == [f"{n}: unchanged" for n in CP + WORKERS]
    assert {p.name: _sha(p) for p in files} == before


@real
def test_real_talosctl_writes_the_expected_patch_forms(real_lab):
    _ok(real_lab.run())
    _assert_node_configs(real_lab.state / "nodes", CP + WORKERS,
                         _pins()["TALOS_INSTALLER_IMAGE"], ["ntp1.example", "ntp2.example"])


@real
@pytest.mark.parametrize("patch_file,body,violation", PROTECTED_PATCHES)
def test_real_talosctl_merges_cannot_slip_past_the_checker(
        real_lab, tmp_path, patch_file, body, violation):
    _node_table(real_lab.nodes, cps=["cp-01"], workers=["worker-01"])
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / patch_file).write_text(body)
    proc = real_lab.run(TALOS_PATCH_DIR=str(patches))
    assert proc.returncode != 0
    assert violation in proc.stderr, proc.stderr
    assert not (real_lab.state / "nodes").exists()
    assert OTHER_CLUSTER not in proc.stderr and OTHER_CA not in proc.stderr


def _layout(path) -> dict:
    """{(kind, name): sorted key paths} -- the structure, without values."""
    def paths(node, prefix):
        if isinstance(node, dict):
            if not node:
                yield prefix + "{}"
            for key, value in node.items():
                yield from paths(value, f"{prefix}.{key}")
        elif isinstance(node, list):
            if not node:
                yield prefix + "[]"
            for value in node:
                yield from paths(value, prefix + "[]")
        else:
            yield prefix
    return {k: sorted(set(paths(d, ""))) for k, d in _docs(path).items()}


@real
def test_the_fake_matches_the_real_layout(real_lab, tmp_path):
    fake = real_lab.bin / "talosctl"
    patch = tmp_path / "cluster.yaml"
    patch.write_text(_kind("TimeSyncConfig", "ntp:\n  servers: [ntp1.example]\n"))
    layouts = {}
    for label, talosctl in (("real", real_lab.talosctl), ("fake", str(fake))):
        out = tmp_path / label
        out.mkdir()
        env = dict(os.environ, FAKE_LOG=str(tmp_path / "fake.log"))
        for cmd in (["gen", "secrets", "-o", str(out / "secrets.yaml")],
                    ["gen", "config", "lab", "https://192.168.53.5:6443",
                     "--with-secrets", str(out / "secrets.yaml"),
                     "--kubernetes-version", "1.37.1", "--install-disk", "/dev/vda",
                     "--install-image", _pins()["TALOS_INSTALLER_IMAGE"],
                     "--config-patch", f"@{patch}", "--output", str(out / "gen")]):
            subprocess.run([talosctl, *cmd], check=True, capture_output=True, env=env)
        layouts[label] = {role: _layout(out / "gen" / f"{role}.yaml")
                          for role in ("controlplane", "worker")}
    assert layouts["fake"] == layouts["real"]
    # every document the checker protects exists in the real output
    checker = runpy.run_path(CHECK)
    kinds = {k for k, _ in layouts["real"]["controlplane"]}
    assert set(checker["PROTECTED_KINDS"]) <= kinds


# ---- the shipped example patches ------------------------------------------ #
PATCH_EXAMPLES = os.path.join(BOX, "patches.example")
#: documents the generated node patch owns
NODE_KINDS = {"HostnameConfig", "LinkAliasConfig", "DHCPv4Config", "Layer2VIPConfig"}


def test_the_example_patches_cover_each_level_and_avoid_protected_documents():
    files = sorted(os.listdir(PATCH_EXAMPLES))
    assert files == ["all.yaml", "controlplane.yaml", "worker-01.yaml", "worker.yaml"]
    protected = set(runpy.run_path(CHECK)["PROTECTED_KINDS"])
    for name in files:
        with open(os.path.join(PATCH_EXAMPLES, name)) as fobj:
            for doc in yaml.safe_load_all(fobj):
                # documents only: the legacy v1alpha1 forms conflict with them
                assert "kind" in doc, name
                assert doc["kind"] not in protected | NODE_KINDS, (name, doc["kind"])
                assert "$patch" not in doc, name


def _assert_examples_applied(nodes_dir, names):
    for name in names:
        docs = _docs(nodes_dir / f"{name}.yaml")
        assert docs[("TimeSyncConfig", None)]["bootTimeout"] == "2m0s"
        labels = docs[("KubeNodeConfig", None)].get("labels", {})
        if name.startswith("cp-"):
            assert docs[("KubeAPIServerConfig", None)]["certExtraSANs"] \
                == ["kubernetes.talos-lab.example"]
            assert "lab.example/pool" not in labels
        else:
            assert ("KubeAPIServerConfig", None) not in docs
            assert labels["lab.example/pool"] == "general"
            assert ("topology.kubernetes.io/zone" in labels) == (name == "worker-01")


def test_the_example_patches_apply_and_keep_every_node_identity(lab):
    proc = _ok(lab.run(TALOS_PATCH_DIR=PATCH_EXAMPLES))
    assert proc.stdout.splitlines() == [f"{n}: created" for n in CP + WORKERS]
    _assert_examples_applied(lab.state / "nodes", CP + WORKERS)
    _assert_node_configs(lab.state / "nodes", CP + WORKERS,
                         f"registry.example/installer/abc:{TALOS_VERSION}",
                         ["ntp1.example", "ntp2.example"])


@real
def test_real_talosctl_validates_the_example_patches(real_lab):
    proc = _ok(real_lab.run(TALOS_PATCH_DIR=PATCH_EXAMPLES))
    assert proc.stderr.count("is valid for metal mode") == 6
    _assert_examples_applied(real_lab.state / "nodes", CP + WORKERS)
    _assert_node_configs(real_lab.state / "nodes", CP + WORKERS,
                         _pins()["TALOS_INSTALLER_IMAGE"], ["ntp1.example", "ntp2.example"])
