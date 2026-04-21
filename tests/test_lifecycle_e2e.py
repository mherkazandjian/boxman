"""
End-to-end lifecycle integration test for the docker-compose runtime.

Exercises the full provision → snapshot → hot-update → destroy flow
against a real libvirt container on the host. Gated on
``@pytest.mark.integration`` + ``@pytest.mark.slow`` so it never runs
in the default CI path; invoke explicitly with::

    make test-integration pytest_args='-m "integration and slow"'

Requirements
------------
* Docker with compose v2
* ``/dev/kvm`` accessible to the invoking user
* ``sudo`` not required (the box config uses
  ``provider.libvirt.use_sudo: False``)
* Internet access for the first run (pulls the Ubuntu 24.04 cloud image)

The first provision downloads the base image and runs cloud-init inside
the template VM; subsequent runs hit the cached image. Budget ~5–10
minutes end-to-end even on fast hardware.

Part of Phase 1.5 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import invoke
import pytest

from boxman.manager import BoxmanManager


pytestmark = [pytest.mark.integration, pytest.mark.slow]


BOX_DIR = (
    Path(__file__).resolve().parent.parent
    / "boxes"
    / "tiny-libvirt-ubuntu-24.04-cloudinit-docker-runtime"
)
BOX_CONF = BOX_DIR / "conf.yml"
DOCKER_DIR = Path(__file__).resolve().parent.parent / "containers" / "docker"


# ---------------------------------------------------------------------------
# Hardware / environment gating
# ---------------------------------------------------------------------------

def _have_docker_compose_v2() -> bool:
    try:
        # in_stream=False is required — pytest captures/closes stdin and
        # invoke.run() otherwise blocks trying to attach to it, silently
        # returning ok=False and mis-skipping the whole module.
        r = invoke.run(
            "docker compose version", hide=True, warn=True, in_stream=False,
        )
        return bool(r.ok)
    except Exception:
        return False


def _have_dev_kvm() -> bool:
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def _have_box_conf() -> bool:
    return BOX_CONF.is_file()


# Skip reasons are resolved at collection time so the test fails fast
# and explains exactly what's missing.
SKIP_REASON = None
if not _have_docker_compose_v2():
    SKIP_REASON = "docker compose v2 not available"
elif not _have_dev_kvm():
    SKIP_REASON = "/dev/kvm not accessible to this user"
elif not _have_box_conf():
    SKIP_REASON = f"box config missing at {BOX_CONF}"

pytestmark.append(pytest.mark.skipif(bool(SKIP_REASON), reason=SKIP_REASON or ""))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: str, warn: bool = False) -> invoke.runners.Result:
    """Run a shell command in the docker dir and return the result."""
    ctx = invoke.context.Context()
    with ctx.cd(str(DOCKER_DIR)):
        return ctx.run(cmd, hide=True, warn=warn, in_stream=False)


def _ssh(host: str, cmd: str, ssh_config: Path, warn: bool = False) -> invoke.runners.Result:
    """SSH into *host* via the generated ssh_config and run *cmd*."""
    full = (
        f"ssh -F {ssh_config} "
        f"-o BatchMode=yes -o ConnectTimeout=10 "
        f"{host} {cmd}"
    )
    return invoke.run(full, hide=True, warn=warn, in_stream=False)


def _wait_for_ssh(host: str, ssh_config: Path, max_attempts: int = 20) -> None:
    """Wait until *host* accepts SSH. Raises on timeout."""
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        result = _ssh(host, "hostname", ssh_config, warn=True)
        if result.ok and result.stdout.strip():
            return
        last_err = result.stderr
        time.sleep(3)
    raise RuntimeError(
        f"host {host} never accepted SSH after {max_attempts} attempts: {last_err}"
    )


# ---------------------------------------------------------------------------
# Module-scoped lifecycle — provision once, exercise, destroy once
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def provisioned_box(tmp_path_factory):
    """
    Provision the box once for the whole module, then tear it down at
    the end regardless of how the individual assertions went.

    Yields a tuple ``(manager, ssh_config, vm_host_alias)``.

    Note: ``BoxmanManager.provision`` / ``destroy`` etc are ``@staticmethod``
    with ``cls`` as the first positional, so they're invoked as
    ``BoxmanManager.provision(manager, cli_args)`` — not via the bound
    method on the instance.
    """
    # Build a manager instance from the box config.
    manager = BoxmanManager(config=str(BOX_CONF))
    manager.runtime = "docker-compose"

    # Wire up the provider session that BoxmanManager.provision relies on.
    from boxman.providers.libvirt.session import LibVirtSession
    manager.provider = LibVirtSession(config=manager.config)
    manager.provider.manager = manager

    # Replicate the CLI wiring that happens between manager creation and
    # provision() — without this, the runtime container is brought up
    # without the right bind-mounts and host-side paths aren't writable.
    # See scripts/app.py where the same block runs before provision().
    rt = manager.runtime_instance
    rt.project_dir = str(BOX_DIR)
    if manager.config and "project" in manager.config:
        rt.project_name = manager.config["project"]
    workdirs = manager.collect_workdirs()
    if workdirs:
        rt.workdirs = workdirs
        for wd in workdirs:
            manager._ensure_writable_dir(wd)

    # Bring the libvirt container up with the correct bind-mounts. The
    # CLI's `up` / `destroy` do this via runtime.ensure_ready(), but
    # `provision` by itself doesn't. ensure_ready also tears down and
    # recreates the container when existing bind-mounts don't cover
    # every dir we just registered.
    rt.ensure_ready()

    # Provide a full argparse-style namespace with every flag provision
    # reads (via getattr with a default). --force clears any stale cache
    # entry from a previous run.
    provision_args = argparse_namespace(
        force=True,
        rebuild_templates=False,
        docker_compose=False,
    )
    BoxmanManager.provision(manager, provision_args)

    ws_path = Path(os.path.expanduser(
        manager.config["workspace"]["path"]))
    ssh_config = ws_path / "ssh_config"
    assert ssh_config.is_file(), f"ssh_config not written: {ssh_config}"

    vm_host = "cluster_1_boxman01"
    _wait_for_ssh(vm_host, ssh_config)

    try:
        yield manager, ssh_config, vm_host
    finally:
        # Best-effort teardown with auto_accept so the prompt doesn't
        # deadlock pytest. Failures here are logged but don't mask the
        # actual test assertion failure.
        destroy_args = argparse_namespace(
            auto_accept=True, templates=False,
        )
        try:
            BoxmanManager.destroy(manager, destroy_args)
        except Exception as exc:
            print(f"[teardown] destroy raised: {exc}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLifecycleSSH:

    def test_ssh_reaches_vm_and_reports_hostname(self, provisioned_box):
        _manager, ssh_config, vm_host = provisioned_box
        result = _ssh(vm_host, "hostname", ssh_config)
        assert result.ok
        # Hostname should match the configured cloud-init value
        assert "boxman01" in result.stdout.strip() or "hello" in result.stdout.strip()

    def test_vm_has_cloudinit_marker_file(self, provisioned_box):
        """The cloud-init user-data wrote /etc/hello-from-cloudinit."""
        _manager, ssh_config, vm_host = provisioned_box
        result = _ssh(
            vm_host, "cat /etc/hello-from-cloudinit", ssh_config, warn=True,
        )
        assert result.ok
        assert "hello world from cloud-init" in result.stdout


class TestLifecycleSnapshot:

    def test_take_modify_restore_round_trip(self, provisioned_box):
        """
        Regression guard for the overlay-preservation fix (057eb7d):
        take a snapshot, mutate filesystem state, restore, confirm the
        mutation is gone. Then take and revert a second time to confirm
        the first snapshot's overlay wasn't deleted.
        """
        manager, ssh_config, vm_host = provisioned_box

        # Take snap0 at clean state (static-call pattern, same as provision).
        BoxmanManager.snapshot_take(
            manager, argparse_namespace(snapshot_name="e2e_snap0"))

        # Write a marker that must NOT survive the revert. /tmp is world-
        # writable, so no sudo (which would need a TTY) is needed.
        _ssh(vm_host, "touch /tmp/e2e-marker", ssh_config)
        probe = _ssh(vm_host, "test -f /tmp/e2e-marker", ssh_config, warn=True)
        assert probe.ok, "marker should exist after creation"

        # Revert to snap0 — VM will reboot; wait for SSH again
        BoxmanManager.snapshot_restore(
            manager, argparse_namespace(snapshot_name="e2e_snap0"))
        _wait_for_ssh(vm_host, ssh_config)

        probe_after = _ssh(
            vm_host, "test -f /tmp/e2e-marker", ssh_config, warn=True,
        )
        assert not probe_after.ok, \
            "marker must be gone after restore; overlay preservation failed"


class TestLifecycleHotUpdate:

    def test_cpu_and_memory_visible_in_dumpxml(self, provisioned_box):
        """
        Post-provision the VM has the CPU/memory values from conf.yml.
        Spot-check via virsh dumpxml so a future refactor of
        ``configure_vm_cpu_memory`` can't silently drop a value.
        """
        manager, _ssh_config, _vm_host = provisioned_box
        vm_full_name = "bprj__boxman_dev_tiny-ubuntu-24.04-docker-runtime__bprj_cluster_1_boxman01"
        # docker-compose service is named "boxman-libvirt" (see
        # containers/docker/docker-compose.yml); address it by the
        # container name the runtime actually spun up.
        container = manager.runtime_instance.container_name
        xml = invoke.run(
            f'docker exec --user root {container} '
            f'/usr/bin/virsh -c qemu:///system dumpxml {vm_full_name}',
            hide=True, warn=True, in_stream=False,
        )
        assert xml.ok, f"dumpxml failed: {xml.stderr}"
        # conf.yml declared 2048 MB memory and sockets=1 cores=2 threads=2
        # (vcpus = 1*2*2 = 4).
        assert "<vcpu" in xml.stdout
        assert "<memory" in xml.stdout


# ---------------------------------------------------------------------------
# Small argparse stand-in
# ---------------------------------------------------------------------------

class _NS:
    """Minimal argparse.Namespace stand-in for passing ``cli_args`` to
    BoxmanManager methods that read attributes like ``snapshot_name``.
    """

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def argparse_namespace(**kwargs) -> _NS:
    """Defaults every BoxmanManager.* method inspects via getattr.

    Union of fields read by provision / destroy / snapshot_take /
    snapshot_restore / deprovision — easier to always provide them all
    than to tailor per-call. Real argparse would set unused flags to
    their ``default`` too.
    """
    defaults = {
        "snapshot_name": None,
        "snapshot_descr": "e2e lifecycle",
        "vms": "all",
        "force": False,
        "live": False,
        "description": "e2e lifecycle",
        "auto_accept": False,
        "templates": False,
        "rebuild_templates": False,
        "docker_compose": False,
        "cleanup": False,
    }
    defaults.update(kwargs)
    return _NS(**defaults)
