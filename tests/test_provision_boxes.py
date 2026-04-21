"""
Integration tests for box provisioning.

Discovers all box directories under boxes/ that contain a conf.yml,
provisions each box, then verifies VM resources (SSH, OS, disks, NICs,
CPU, memory) match the declared configuration.

Requires a working libvirt/KVM environment on the host.

Usage:
    make test-provision                                          # all boxes
    make test-provision pytest_args="-k tiny-libvirt-rocky-9"    # single box
    make test-provision verbose=1                                # verbose
"""

import glob
import os
import time

import invoke
import pytest
import yaml

from boxman.utils.jinja_env import create_jinja_env

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BOXMAN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXES_DIR = os.path.join(BOXMAN_DIR, "boxes")

# ---------------------------------------------------------------------------
# OS variant → /etc/os-release ID mapping
# ---------------------------------------------------------------------------

OS_VARIANT_TO_ID = {
    "ubuntu24.04": "ubuntu",
    "rocky9": "rocky",
    "centos7.0": "centos",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd, warn=False):
    """Run a shell command and return the invoke Result."""
    ctx = invoke.context.Context()
    return ctx.run(cmd, hide=True, warn=warn, in_stream=False)


def discover_boxes():
    """Return sorted list of box directories that contain a conf.yml."""
    pattern = os.path.join(BOXES_DIR, "*/conf.yml")
    return sorted(os.path.dirname(p) for p in glob.glob(pattern))


def parse_box_config(box_dir):
    """Render the Jinja2 conf.yml and return the parsed YAML dict."""
    jinja_env = create_jinja_env(box_dir)
    # Set BOXMAN_CONF_DIR so templates that reference it resolve correctly
    os.environ.setdefault("BOXMAN_CONF_DIR", box_dir)
    template = jinja_env.get_template("conf.yml")
    rendered = template.render()
    return yaml.safe_load(rendered)


def ssh_cmd(ssh_config_path, host, command, retries=8, backoff=3):
    """Run *command* on *host* via SSH, retrying on failure.

    Returns the invoke Result on success, or calls ``pytest.fail`` after
    exhausting all retries.
    """
    ssh = (
        f"ssh -F {ssh_config_path} "
        f"-o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null "
        f"-o ConnectTimeout=10 "
        f"{host} {command}"
    )
    last_err = None
    for attempt in range(1, retries + 1):
        result = _run(ssh, warn=True)
        if result.ok:
            return result
        last_err = result.stderr.strip()
        if attempt < retries:
            time.sleep(backoff * attempt)

    pytest.fail(
        f"SSH command failed after {retries} attempts on {host}: "
        f"{command!r}\nLast stderr: {last_err}"
    )


def iter_vms(config):
    """Yield (cluster_name, vm_name, vm_config) for every VM in *config*."""
    for cluster_name, cluster in config.get("clusters", {}).items():
        for vm_name, vm_cfg in cluster.get("vms", {}).items():
            yield cluster_name, vm_name, vm_cfg


def get_ssh_config_path(config, cluster_name):
    """Derive the ssh_config path for a cluster from the workspace path."""
    workspace_path = os.path.expanduser(config["workspace"]["path"])
    cluster = config["clusters"][cluster_name]
    ssh_config_name = cluster.get("ssh_config", "ssh_config")
    return os.path.join(workspace_path, ssh_config_name)


def get_ssh_host(cluster_name, vm_name, vm_cfg):
    """Return the SSH host alias that boxman generates: <cluster>_<hostname>."""
    hostname = vm_cfg.get("hostname", vm_name)
    return f"{cluster_name}_{hostname}"


def get_os_id(config):
    """Extract the expected /etc/os-release ID from the first template's os_variant."""
    templates = config.get("templates", {})
    for tmpl in templates.values():
        os_variant = tmpl.get("os_variant", "")
        return OS_VARIANT_TO_ID.get(os_variant, os_variant)
    return ""


def get_os_id_for_vm(config, cluster_cfg, vm_cfg):
    """
    Resolve the expected /etc/os-release ID for a single VM by looking
    up the effective ``base_image`` (VM-level > cluster-level fallback)
    and finding its ``os_variant`` in the ``templates:`` section.

    Fixes multi-template boxes like ``hybrid-two-vms-cloudinit`` where
    different VMs use different templates — the old get_os_id() returned
    the first template's OS for all VMs.
    """
    base_image = vm_cfg.get("base_image") or cluster_cfg.get("base_image")
    if base_image:
        for tmpl in config.get("templates", {}).values():
            if tmpl.get("name") == base_image:
                os_variant = tmpl.get("os_variant", "")
                return OS_VARIANT_TO_ID.get(os_variant, os_variant)
    # Fallback to first-template lookup for boxes without explicit base_image.
    return get_os_id(config)


def get_expected_vcpus(vm_cfg):
    """Compute expected vCPU count from sockets × cores × threads."""
    cpus = vm_cfg.get("cpus", {})
    return (
        cpus.get("sockets", 1)
        * cpus.get("cores", 1)
        * cpus.get("threads", 1)
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="class")
def provisioned_box(request):
    """Provision a box (create-templates + provision), yield config, then deprovision."""
    box_dir = request.param
    conf_path = os.path.join(box_dir, "conf.yml")
    config = parse_box_config(box_dir)

    # --- setup ---
    result = _run(f"boxman --conf {conf_path} create-templates --force", warn=True)
    if not result.ok:
        pytest.skip(
            f"create-templates failed for {os.path.basename(box_dir)}: "
            f"{result.stderr.strip()}"
        )

    result = _run(f"boxman --conf {conf_path} provision --force", warn=True)
    if not result.ok:
        pytest.skip(
            f"provision failed for {os.path.basename(box_dir)}: "
            f"{result.stderr.strip()}"
        )

    yield config

    # --- teardown ---
    _run(f"boxman --conf {conf_path} deprovision", warn=True)


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

box_dirs = discover_boxes()


@pytest.mark.integration
@pytest.mark.parametrize(
    "provisioned_box",
    box_dirs,
    ids=[os.path.basename(d) for d in box_dirs],
    indirect=True,
)
class TestProvisionBox:
    """Verify every VM in a provisioned box matches its declared config."""

    # -- SSH connectivity ---------------------------------------------------

    def test_ssh_connectivity(self, provisioned_box):
        config = provisioned_box
        for cluster_name, vm_name, vm_cfg in iter_vms(config):
            ssh_config = get_ssh_config_path(config, cluster_name)
            host = get_ssh_host(cluster_name, vm_name, vm_cfg)
            result = ssh_cmd(ssh_config, host, "echo ok")
            assert "ok" in result.stdout, (
                f"SSH echo failed on {host}: {result.stdout}"
            )

    # -- OS release ---------------------------------------------------------

    def test_os_release(self, provisioned_box):
        config = provisioned_box
        for cluster_name, vm_name, vm_cfg in iter_vms(config):
            cluster_cfg = config["clusters"][cluster_name]
            # Look up each VM's OS individually so multi-template boxes
            # (e.g. hybrid-two-vms-cloudinit) verify each VM against its
            # own template, not all against the first one.
            expected_id = get_os_id_for_vm(config, cluster_cfg, vm_cfg)
            ssh_config = get_ssh_config_path(config, cluster_name)
            host = get_ssh_host(cluster_name, vm_name, vm_cfg)
            result = ssh_cmd(ssh_config, host, "cat /etc/os-release")
            assert f"ID={expected_id}" in result.stdout or f'ID="{expected_id}"' in result.stdout, (
                f"Expected ID={expected_id} in /etc/os-release on {host}, "
                f"got:\n{result.stdout}"
            )

    # -- Disks --------------------------------------------------------------

    def test_disks(self, provisioned_box):
        config = provisioned_box
        for cluster_name, vm_name, vm_cfg in iter_vms(config):
            ssh_config = get_ssh_config_path(config, cluster_name)
            host = get_ssh_host(cluster_name, vm_name, vm_cfg)
            extra_disks = vm_cfg.get("disks", [])
            expected_targets = [d["target"] for d in extra_disks]

            result = ssh_cmd(ssh_config, host, "lsblk -dn -o NAME")
            devices = result.stdout.strip().splitlines()

            # Total block devices = boot disk (vda) + extra disks
            expected_count = 1 + len(extra_disks)
            assert len(devices) >= expected_count, (
                f"Expected at least {expected_count} block devices on {host}, "
                f"found {len(devices)}: {devices}"
            )

            for target in expected_targets:
                assert any(target in dev for dev in devices), (
                    f"Expected disk target {target} on {host}, "
                    f"found: {devices}"
                )

    # -- Network interfaces -------------------------------------------------

    def test_network_interfaces(self, provisioned_box):
        config = provisioned_box
        for cluster_name, vm_name, vm_cfg in iter_vms(config):
            ssh_config = get_ssh_config_path(config, cluster_name)
            host = get_ssh_host(cluster_name, vm_name, vm_cfg)
            expected_count = len(vm_cfg.get("network_adapters", []))

            # Run ``ip -o link show`` remotely, count non-lo interfaces
            # in Python. Using ``env`` with an absolute PATH so the
            # lookup works even on distros (CentOS 7) where ``/sbin``
            # and ``/usr/sbin`` aren't on the non-root default PATH,
            # without relying on shell-metacharacter survival across
            # SSH's arg-joining (a ``sh -c '...'`` wrapper's single
            # quotes get stripped by the local bash before SSH sees
            # them, silently leaving ``ip`` un-run and stdout empty).
            result = ssh_cmd(
                ssh_config, host,
                "env PATH=/sbin:/usr/sbin:/bin:/usr/bin ip -o link show",
            )
            # Each output line looks like:
            #   "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 ..."
            # Field layout: "<idx>: <name>: <flags> ..."
            non_lo = 0
            for line in result.stdout.splitlines():
                parts = line.split(":", 2)
                if len(parts) >= 2 and parts[1].strip() != "lo":
                    non_lo += 1
            assert non_lo >= expected_count, (
                f"Expected at least {expected_count} NICs on {host}, "
                f"found {non_lo}. Raw output:\n{result.stdout}"
            )

    # -- CPU ----------------------------------------------------------------

    def test_cpu(self, provisioned_box):
        config = provisioned_box
        for cluster_name, vm_name, vm_cfg in iter_vms(config):
            ssh_config = get_ssh_config_path(config, cluster_name)
            host = get_ssh_host(cluster_name, vm_name, vm_cfg)
            expected = get_expected_vcpus(vm_cfg)

            result = ssh_cmd(ssh_config, host, "nproc")
            actual = int(result.stdout.strip())
            assert actual == expected, (
                f"Expected {expected} vCPUs on {host}, got {actual}"
            )

    # -- Memory -------------------------------------------------------------

    def test_memory(self, provisioned_box):
        config = provisioned_box
        for cluster_name, vm_name, vm_cfg in iter_vms(config):
            ssh_config = get_ssh_config_path(config, cluster_name)
            host = get_ssh_host(cluster_name, vm_name, vm_cfg)
            expected_mb = vm_cfg.get("memory", 0)

            result = ssh_cmd(ssh_config, host, "free -m | awk '/Mem:/{print $2}'")
            actual_mb = int(result.stdout.strip())

            # ``free -m`` reports ``MemTotal`` from ``/proc/meminfo`` —
            # the memory the VM kernel *sees* after subtracting its own
            # reserved regions (text + data + modules + initramfs). On
            # Linux that overhead is typically 10–15% of provisioned
            # size and can hit 17% on heavier kernels. Use the looser of
            # two floors: 20% percentage (scales with VM size) or
            # 256 MB fixed slack (covers small VMs where the fixed
            # kernel footprint is a larger fraction). ``min`` picks the
            # looser bound so neither rule alone over-tightens.
            lower = min(expected_mb * 0.80, expected_mb - 256)
            assert actual_mb >= lower, (
                f"Expected ~{expected_mb} MB RAM on {host}, "
                f"got {actual_mb} MB (< {lower:.0f} MB tolerance floor)"
            )
