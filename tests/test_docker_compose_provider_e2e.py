"""
End-to-end integration tests for the **docker-compose provider**.

Not to be confused with ``test_docker_compose.py``, which covers the
docker-compose *runtime* (libvirt-in-a-container). The two axes share a name
but are unrelated: this file drives real ``boxman`` verbs against real
containers on the ``local`` runtime.

Two tiers:

* **docker-only** — the whole provider lifecycle against
  ``boxes/docker-compose-standalone``: provision, ps, exec, volume
  persistence across down/up, snapshot take/restore/delete, destroy. Needs
  Docker + compose v2 only, so it runs anywhere Docker does.
* **hybrid (KVM-gated)** — ``boxes/hybrid-libvirt-docker-compose``: a libvirt
  VM and a container sharing an L2 domain, plus the mixed-project ps and
  inventory. Skipped without ``/dev/kvm``.

These are local-only, like ``make test-provision`` — they create and destroy
real infrastructure::

    make test-dc-e2e                                  # docker-only tier
    make test-dc-e2e pytest_args="-k hybrid"          # hybrid tier too
"""

import json
import os
import shutil

import invoke
import pytest

BOXMAN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXES_DIR = os.path.join(BOXMAN_DIR, "boxes")
STANDALONE = os.path.join(BOXES_DIR, "docker-compose-standalone")
HYBRID = os.path.join(BOXES_DIR, "hybrid-libvirt-docker-compose")

#: compose project boxman derives for the standalone box's ``web`` cluster
#: (``<provider.project_name>_<cluster>``)
STANDALONE_PROJECT = "dc_standalone_web"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd, cwd=None, warn=False):
    """Run a shell command (optionally in *cwd*) and return the invoke Result."""
    ctx = invoke.context.Context()
    if cwd:
        with ctx.cd(cwd):
            return ctx.run(cmd, hide=True, warn=warn, in_stream=False)
    return ctx.run(cmd, hide=True, warn=warn, in_stream=False)


def _boxman(args, cwd, warn=False):
    """Invoke the boxman CLI out of the working tree (no install required)."""
    src = os.path.join(BOXMAN_DIR, "src")
    app = os.path.join(src, "boxman", "scripts", "app.py")
    return _run(
        f"PYTHONPATH={src}:$PYTHONPATH python3 {app} {args}", cwd=cwd, warn=warn)


def _workspace_path(box_dir):
    """The box's ``workspace.path``, expanded — where the workspace-level
    inventory / ssh_config / env.sh are written (a cluster ``workdir`` only
    holds that cluster's own files)."""
    import yaml
    from boxman.utils.jinja_env import create_jinja_env

    raw = open(os.path.join(box_dir, "conf.yml")).read()
    rendered = create_jinja_env(box_dir).from_string(raw).render()
    conf = yaml.safe_load(rendered)
    path = (conf.get("workspace") or {}).get("path")
    assert path, f"{box_dir}/conf.yml declares no workspace.path"
    return os.path.expanduser(path)


def _docker_available():
    return (
        shutil.which("docker") is not None
        and _run("docker compose version", warn=True).ok
    )


def _kvm_available():
    return os.path.exists("/dev/kvm")


requires_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="needs Docker with the compose v2 plugin")

requires_kvm = pytest.mark.skipif(
    not _kvm_available(), reason="needs /dev/kvm for the libvirt half")


# ---------------------------------------------------------------------------
# Tier 1 — docker-only: the full provider lifecycle
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def standalone_up():
    """Provision the standalone box for the module, destroy it after.

    ``--force`` so a stale cache entry from an interrupted run cannot wedge
    the whole module.
    """
    _boxman("destroy --auto-accept", cwd=STANDALONE, warn=True)
    result = _boxman("provision --force", cwd=STANDALONE, warn=True)
    assert result.ok, f"provision failed:\n{result.stdout}\n{result.stderr}"
    yield STANDALONE
    _boxman("destroy --auto-accept", cwd=STANDALONE, warn=True)


@requires_docker
@pytest.mark.integration
class TestDockerComposeProviderLifecycle:
    """provision → ps → exec → volumes → snapshots → destroy, on real docker."""

    def test_containers_are_running(self, standalone_up):
        out = _run(
            f"docker compose -p {STANDALONE_PROJECT} ps --format json --all",
            warn=True).stdout
        states = {
            obj["Service"]: obj["State"]
            for line in out.splitlines() if line.strip()
            for obj in ([json.loads(line)] if line.strip().startswith("{")
                        else json.loads(line))
        }
        assert states.get("cache") == "running"
        assert states.get("frontend") == "running"

    def test_published_port_serves_the_bind_mount(self, standalone_up):
        """The frontend publishes :8080 and serves ./site (a read-only bind)."""
        body = _run("curl -s localhost:8080", warn=True).stdout
        assert "boxman" in body

    def test_bind_mount_is_readonly(self, standalone_up):
        """`readonly: true` must reach the container as :ro.

        Asserts the *specific* refusal rather than merely a non-zero exit, so
        this cannot pass because `exec` itself broke.
        """
        result = _boxman(
            "exec web.frontend -- sh -c 'echo x > /usr/share/nginx/html/x'",
            cwd=standalone_up, warn=True)
        assert "Read-only file system" in (result.stdout + result.stderr), (
            f"expected a read-only refusal, got:\n{result.stdout}\n{result.stderr}")
        # and the write really did not land
        listing = _boxman("exec web.frontend -- ls /usr/share/nginx/html",
                          cwd=standalone_up, warn=True)
        assert " x" not in listing.stdout.replace("\n", " ") + " "

    def test_exec_one_shot(self, standalone_up):
        result = _boxman("exec web.cache -- redis-cli ping", cwd=standalone_up)
        assert "PONG" in result.stdout

    def test_ps_reports_containers_with_provider(self, standalone_up):
        result = _boxman("ps --json", cwd=standalone_up, warn=True)
        rows = json.loads(result.stdout[result.stdout.index("["):])
        dc_rows = [r for r in rows if r.get("provider") == "docker-compose"]
        assert {r["vm"] for r in dc_rows} >= {"cache", "frontend"}

    def test_inventory_lists_containers_with_docker_connection(self, standalone_up):
        inv = os.path.join(
            standalone_up, ".boxman", "web", "inventory", "01-hosts.yml")
        assert os.path.isfile(inv), f"no per-cluster inventory at {inv}"
        text = open(inv).read()
        assert "community.docker.docker" in text
        # ansible_host must be the real container name, not the box name
        assert f"{STANDALONE_PROJECT}-cache-1" in text

    def test_named_volume_survives_down_up(self, standalone_up):
        """AC13: a named volume outlives a stop/start cycle."""
        _boxman("exec web.cache -- redis-cli set greeting hello", cwd=standalone_up)
        _boxman("exec web.cache -- redis-cli save", cwd=standalone_up)
        assert _boxman("down", cwd=standalone_up, warn=True).ok
        assert _boxman("up", cwd=standalone_up, warn=True).ok
        result = _boxman("exec web.cache -- redis-cli get greeting", cwd=standalone_up)
        assert "hello" in result.stdout

    def test_snapshot_take_restore_and_volume_divergence(self, standalone_up):
        """D3: a restore rolls back the container filesystem but NOT volumes."""
        _boxman("exec web.cache -- redis-cli set persisted yes", cwd=standalone_up)
        _boxman("exec web.cache -- redis-cli save", cwd=standalone_up)
        assert _boxman("snapshot take --name e2e1 -m e2e", cwd=standalone_up, warn=True).ok

        # dirty the container filesystem after the snapshot
        _boxman("exec web.cache -- sh -c 'echo dirt > /tmp/dirt'", cwd=standalone_up)
        assert _boxman("snapshot restore --name e2e1", cwd=standalone_up, warn=True).ok

        gone = _boxman("exec web.cache -- ls /tmp/dirt", cwd=standalone_up, warn=True)
        assert not gone.ok, "container filesystem was not rolled back"

        # ...while the named volume is untouched by the snapshot
        kept = _boxman("exec web.cache -- redis-cli get persisted", cwd=standalone_up)
        assert "yes" in kept.stdout

        assert _boxman("snapshot delete --name e2e1", cwd=standalone_up, warn=True).ok

    def test_snapshot_take_rejects_duplicate_name(self, standalone_up):
        assert _boxman("snapshot take --name dup", cwd=standalone_up, warn=True).ok
        again = _boxman("snapshot take --name dup", cwd=standalone_up, warn=True)
        assert "already exists" in (again.stdout + again.stderr)
        _boxman("snapshot delete --name dup", cwd=standalone_up, warn=True)

    def test_destroy_removes_containers_and_named_volumes(self):
        """AC15: destroy tears down containers *and* named volumes."""
        _boxman("provision --force", cwd=STANDALONE, warn=True)
        assert _boxman("destroy --auto-accept", cwd=STANDALONE, warn=True).ok
        ps = _run(f"docker compose -p {STANDALONE_PROJECT} ps -q", warn=True)
        assert not ps.stdout.strip()
        vols = _run(
            f"docker volume ls -q --filter label=com.docker.compose.project="
            f"{STANDALONE_PROJECT}", warn=True)
        assert not vols.stdout.strip()


# ---------------------------------------------------------------------------
# Tier 2 — hybrid: VM ↔ container on a shared L2 domain (needs KVM)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def hybrid_up():
    """Provision the hybrid box for the module, destroy it after.

    Module-scoped and defined at module level: a class-scoped fixture written
    as an instance method is deprecated by pytest.
    """
    _boxman("destroy --auto-accept", cwd=HYBRID, warn=True)
    result = _boxman("provision --force", cwd=HYBRID, warn=True)
    assert result.ok, f"hybrid provision failed:\n{result.stdout}\n{result.stderr}"
    yield HYBRID
    _boxman("destroy --auto-accept", cwd=HYBRID, warn=True)


@requires_docker
@requires_kvm
@pytest.mark.integration
class TestHybridVmContainer:
    """AC10–AC12 + mixed-project ps/inventory. Boots a real VM — slow."""

    def test_mixed_ps_shows_both_providers(self, hybrid_up):
        result = _boxman("ps --json", cwd=hybrid_up, warn=True)
        rows = json.loads(result.stdout[result.stdout.index("["):])
        providers = {r.get("provider") for r in rows}
        assert "docker-compose" in providers
        assert "libvirt" in providers

    def test_inventory_spans_both_providers(self, hybrid_up):
        """AC18: the *workspace* inventory carries the VM and the container,
        each with its own connection style.

        Note this lives under ``workspace.path``, not the box directory — only
        the per-cluster inventories are written under a cluster ``workdir``.
        """
        inv = os.path.join(_workspace_path(hybrid_up), "inventory", "01-hosts.yml")
        assert os.path.isfile(inv), f"no workspace inventory at {inv}"
        text = open(inv).read()
        # the container: reached over the docker connection plugin
        assert "community.docker.docker" in text
        assert "hybrid_libvirt_dc_services-web-1" in text
        # the VM: an ordinary ssh host, no docker connection vars
        assert "compute_node01" in text

    def test_cluster_internal_network_is_isolated_from_the_bridge(self, hybrid_up):
        """AC12: the cluster-internal network is an ordinary compose-scoped
        bridge, while only the *shared* network is a macvlan onto the host
        bridge — so internal traffic never reaches the VM's L2 domain."""
        nets = _run("docker network ls --format '{{.Name}} {{.Driver}}'", warn=True).stdout
        internal = [l for l in nets.splitlines() if "backend" in l]
        assert internal, f"cluster-internal network not found in:\n{nets}"
        assert all(line.split()[1] == "bridge" for line in internal)

        shared = [l for l in nets.splitlines() if "app_bridge" in l]
        assert shared, f"shared macvlan network not found in:\n{nets}"
        for line in shared:
            name = line.split()[0]
            info = _run(f"docker network inspect {name}", warn=True).stdout
            assert '"driver": "macvlan"' in info or '"Driver": "macvlan"' in info
            assert "bx_app" in info, "macvlan is not parented on the host bridge"

    def test_container_reaches_vm_over_shared_bridge(self, hybrid_up):
        """AC10/AC11: the macvlan container pings the VM on the shared bridge
        and learns its MAC via ARP.

        The VM's shared-bridge address is assigned by the README walkthrough
        (that L2 domain has no DHCP), so skip rather than fail when it has not
        been set up — this test asserts boxman's plumbing, not the operator's.
        """
        ping = _boxman(
            "exec services.web -- ping -c2 -W2 10.10.0.20", cwd=hybrid_up, warn=True)
        if not ping.ok:
            pytest.skip(
                "VM shared-bridge address 10.10.0.20 not configured — follow "
                "the box README before running the L2 assertions")
        arp = _boxman(
            "exec services.web -- ip neigh show 10.10.0.20", cwd=hybrid_up, warn=True)
        assert "lladdr" in arp.stdout, "no ARP entry learned for the VM"
