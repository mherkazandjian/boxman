"""
Deterministic checks for boxes/talos-unattended.

Neither half touching libvirt or the network:

- conf.yml's shape: the seven-VM topology, sizing, the shared ISO, pinned MACs
  that the fabric network reserves, and no ssh credentials on the API-only
  Talos group.
- the control template's tool install, run for real against a fake curl.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest
import yaml

from boxman.manager import BoxmanManager
from boxman.utils.jinja_env import create_jinja_env

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOX = os.path.join(REPO, "boxes", "talos-unattended")
NODES_FROM_CONF = os.path.join(BOX, "scripts", "nodes-from-conf.py")

TALOS_VERSION = "v1.99.0"   # synthetic: these tests pin no real release

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


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
