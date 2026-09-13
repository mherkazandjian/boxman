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

    make test-dc-e2e                              # both tiers; hybrid
                                                  # auto-skips without /dev/kvm
    make test-dc-e2e pytest_args="-k Hybrid"      # restrict to the hybrid tier
    make test-dc-e2e pytest_args="-k Lifecycle"   # restrict to the docker-only tier
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
        """D3: a restore rolls back the container filesystem but NOT volumes.

        Both values are changed *after* the snapshot, which is what makes the
        two assertions discriminating: the filesystem one fails if the commit
        is not restored, and the volume one fails if volume data *were* rolled
        back with it. A value written before the take would read the same
        either way and prove nothing.
        """
        _boxman("exec web.cache -- redis-cli set divergence before", cwd=standalone_up)
        _boxman("exec web.cache -- redis-cli save", cwd=standalone_up)
        assert _boxman("snapshot take --name e2e1 -m e2e", cwd=standalone_up, warn=True).ok

        # after the snapshot: dirty the container filesystem *and* the volume
        _boxman("exec web.cache -- sh -c 'echo dirt > /tmp/dirt'", cwd=standalone_up)
        _boxman("exec web.cache -- redis-cli set divergence after", cwd=standalone_up)
        _boxman("exec web.cache -- redis-cli save", cwd=standalone_up)

        assert _boxman("snapshot restore --name e2e1", cwd=standalone_up, warn=True).ok

        # the container filesystem went back to the snapshot. Assert the
        # specific "no such file" rather than merely a non-zero exit, which a
        # broken `exec` (or a container left down by the restore) would also
        # produce — that would turn a failed restore into a green test.
        gone = _boxman("exec web.cache -- ls /tmp/dirt", cwd=standalone_up, warn=True)
        assert not gone.ok, "container filesystem was not rolled back"
        assert "No such file" in (gone.stdout + gone.stderr), (
            f"expected /tmp/dirt to be absent after restore, but the command "
            f"failed for a different reason:\n{gone.stdout}{gone.stderr}")

        # ...but the volume kept the *post-snapshot* write (docker commit never
        # captured it), which is the divergence the docs warn about
        kept = _boxman("exec web.cache -- redis-cli get divergence", cwd=standalone_up)
        assert "after" in kept.stdout, (
            "named volume was rolled back with the snapshot — D3 says commit "
            f"captures the writable layer only. got: {kept.stdout!r}")

        assert _boxman("snapshot delete --name e2e1", cwd=standalone_up, warn=True).ok

    def test_snapshot_take_rejects_duplicate_name(self, standalone_up):
        assert _boxman("snapshot take --name dup", cwd=standalone_up, warn=True).ok
        again = _boxman("snapshot take --name dup", cwd=standalone_up, warn=True)
        assert "already exists" in (again.stdout + again.stderr)
        _boxman("snapshot delete --name dup", cwd=standalone_up, warn=True)

    def test_destroy_removes_containers_and_named_volumes(self):
        """AC15: destroy tears down containers *and* named volumes.

        **Must stay the last test in this class.** It deliberately does not
        take ``standalone_up`` yet tears the module deployment down, so any
        test added below it — or any run order shuffled by a plugin such as
        pytest-randomly — would execute against a destroyed project. (The
        second destroy in the fixture teardown is harmless: idempotent and
        ``warn=True``.)

        The re-provision is asserted so a transient failure cannot leave both
        emptiness checks passing vacuously against a project that was never
        brought up.
        """
        assert _boxman("provision --force", cwd=STANDALONE, warn=True).ok, \
            "re-provision failed — the emptiness assertions below would be vacuous"
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

#: the VM's address on the shared bridge, and the NIC it belongs to. That L2
#: domain has no DHCP on purpose (addressing it at boot would make boxman
#: mistake the host-unreachable address for node01's management IP), so the
#: fixture assigns it — see the box README, step 1.
VM_BRIDGE_IP = "10.10.0.20"
VM_BRIDGE_NIC = "enp7s0"
CONTAINER_BRIDGE_IP = "10.10.0.10"


def _vm_ssh(box_dir, command, warn=False):
    """Run *command* on the hybrid box's VM.

    ``boxman ssh`` opens an interactive session and takes no trailing command,
    so this goes through the ssh_config boxman generates in the workspace.
    """
    ssh_config = os.path.join(_workspace_path(box_dir), "ssh_config")
    return _run(
        f"ssh -F {ssh_config} -o BatchMode=yes -o ConnectTimeout=10 "
        f"compute_node01 {command}", warn=warn)


@pytest.fixture(scope="module")
def hybrid_up():
    """Provision the hybrid box for the module, destroy it after.

    Also establishes the L2 precondition: the VM's address on the shared
    bridge. Doing it here (rather than skipping later when it is absent) keeps
    the AC10/AC11 assertions able to *fail* — a broken macvlan parent, a
    missing bridge attachment or a firewall regression must not look like an
    unconfigured operator.

    Module-scoped and defined at module level: a class-scoped fixture written
    as an instance method is deprecated by pytest.
    """
    _boxman("destroy --auto-accept", cwd=HYBRID, warn=True)
    # The template name is shared with other boxes (e.g.
    # tiny-libvirt-ubuntu-24.04-cloudinit) via the default
    # ~/boxman-templates workdir, and a full-suite run may have last built
    # it with a different box's cloud-init (different admin password, no
    # key injection path). Rebuild it from THIS box's config so the clone
    # gets the right users — same pattern as test_provision_boxes.
    result = _boxman("create-templates --force", cwd=HYBRID, warn=True)
    assert result.ok, (
        f"hybrid create-templates failed:\n{result.stdout}\n{result.stderr}")
    result = _boxman("provision --force", cwd=HYBRID, warn=True)
    assert result.ok, f"hybrid provision failed:\n{result.stdout}\n{result.stderr}"

    # idempotent: a re-run would report "File exists", so verify rather than
    # trust the exit status
    _vm_ssh(HYBRID, f"sudo ip addr add {VM_BRIDGE_IP}/24 dev {VM_BRIDGE_NIC}",
            warn=True)
    shown = _vm_ssh(HYBRID, f"ip -4 -br addr show {VM_BRIDGE_NIC}", warn=True)
    assert VM_BRIDGE_IP in shown.stdout, (
        f"could not put {VM_BRIDGE_IP} on {VM_BRIDGE_NIC}; the L2 assertions "
        f"would be meaningless. got: {shown.stdout!r} {shown.stderr!r}")

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
        internal = [line for line in nets.splitlines() if "backend" in line]
        assert internal, f"cluster-internal network not found in:\n{nets}"
        assert all(line.split()[1] == "bridge" for line in internal)

        shared = [line for line in nets.splitlines() if "app_bridge" in line]
        assert shared, f"shared macvlan network not found in:\n{nets}"
        for line in shared:
            name = line.split()[0]
            info = _run(f"docker network inspect {name}", warn=True).stdout
            assert '"driver": "macvlan"' in info or '"Driver": "macvlan"' in info
            assert "bx_app" in info, "macvlan is not parented on the host bridge"

    def test_container_and_vm_reach_each_other_over_shared_bridge(self, hybrid_up):
        """AC10: VM and container ping each other across the shared bridge.

        The address precondition is established by the fixture, so a failure
        here is a real regression (macvlan parent, bridge attachment,
        netfilter) rather than an unconfigured operator — no skipping.
        """
        out = _boxman(
            f"exec services.web -- ping -c2 -W2 {VM_BRIDGE_IP}",
            cwd=hybrid_up, warn=True)
        assert out.ok, f"container could not reach the VM:\n{out.stdout}{out.stderr}"

        back = _vm_ssh(hybrid_up, f"ping -c2 -W2 {CONTAINER_BRIDGE_IP}", warn=True)
        assert back.ok, f"VM could not reach the container:\n{back.stdout}{back.stderr}"

    def test_arp_resolves_across_the_shared_bridge(self, hybrid_up):
        """AC11: each side learns the other's real MAC — proof this is L2 and
        not routed. The VM's MAC is pinned in the box conf, so assert that
        exact value rather than merely 'some lladdr'."""
        arp = _boxman(
            f"exec services.web -- ip neigh show {VM_BRIDGE_IP}",
            cwd=hybrid_up, warn=True)
        assert "lladdr" in arp.stdout, f"no ARP entry for the VM: {arp.stdout!r}"
        # adapter_2's mac in boxes/hybrid-libvirt-docker-compose/conf.yml
        assert "52:54:00:aa:00:02" in arp.stdout.lower(), (
            f"container resolved the VM to an unexpected MAC: {arp.stdout!r}")

        back = _vm_ssh(hybrid_up, f"ip neigh show {CONTAINER_BRIDGE_IP}", warn=True)
        assert "lladdr" in back.stdout, (
            f"VM learned no ARP entry for the container: {back.stdout!r}")

    def test_cluster_internal_address_unreachable_from_vm(self, hybrid_up):
        """AC12, the traffic-level half: the container's cluster-internal
        address is not reachable from the VM — only the shared bridge is
        L2-adjacent.

        Asserts ping actually ran and reported total loss. A bare
        ``not out.ok`` would also be satisfied by ssh failing outright, which
        would pass this test without ever demonstrating isolation.
        """
        out = _vm_ssh(hybrid_up, "ping -c2 -W2 172.31.0.2", warn=True)
        assert "100% packet loss" in out.stdout, (
            "expected total loss to the cluster-internal address; either it is "
            f"reachable (isolation regression) or ping never ran:\n"
            f"{out.stdout}{out.stderr}")
        assert not out.ok


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")
class TestCandidateResolutionMatchesDeployment:
    """A candidate must resolve exactly as the deployment command does.

    `validate()` and `resolved_model()` both aim at a *staged* file rather
    than the published one, so they carry the project directory explicitly.
    If that drifted, a candidate could resolve against a different `.env` or
    resolve a relative path differently from the `up` that follows — and pass
    a check the deployment then fails (#164 NET-C3).

    No containers are started: this compares configuration resolution only.
    """

    def _project(self, tmp_path):
        """A workdir whose .env disagrees with the caller's directory."""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / ".env").write_text("TAG=alpine\nEXTRA=./included.yml\n")
        (workdir / "included.yml").write_text(
            "services:\n  extra: {image: busybox}\n")
        (tmp_path / ".env").write_text("TAG=latest\nEXTRA=./wrong.yml\n")
        body = (
            'include: ["${EXTRA}"]\n'
            'services:\n'
            '  web:\n'
            '    image: "nginx:${TAG}"\n'
            '    volumes: ["./data:/d"]\n'
        )
        (workdir / "docker-compose.yml").write_text(body)
        (workdir / ".candidate.yml").write_text(body)
        (workdir / "data").mkdir()
        return workdir

    def _deployment_config(self, runner):
        """Resolve the **published** file through the production command.

        Must go through `_base()` — the prefix `up()` uses. A handwritten
        equivalent compares two things built the same way and cannot detect
        the drift this test exists to catch (#164 NET-C3).
        """
        out = invoke.run(f"{runner._base()} config --format json",
                         hide=True, warn=True, in_stream=False)
        assert out.ok, out.stderr
        return json.loads(out.stdout)

    def test_candidate_and_published_resolve_identically(self, tmp_path):
        from boxman.providers.docker_compose.compose_runner import ComposeRunner

        workdir = self._project(tmp_path)
        # run from a directory that is NOT the workdir, with its own .env
        runner = ComposeRunner(project="dirctx", workdir=str(workdir),
                               compose_file=str(workdir / "docker-compose.yml"))
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            candidate = runner.resolved_model(str(workdir / ".candidate.yml"))
            deployed = self._deployment_config(runner)
        finally:
            os.chdir(cwd)

        # the workdir's .env wins over the caller's, for both
        assert candidate["services"]["web"]["image"] == "nginx:alpine"
        assert deployed["services"]["web"]["image"] == "nginx:alpine"
        # the relative include resolved against the workdir, for both
        assert "extra" in candidate["services"]
        assert "extra" in deployed["services"]
        # and the relative bind path resolved to the workdir's, not the
        # caller's — asserting only that the two agree would pass even if
        # both resolved against the wrong directory
        expected = str(workdir / "data")
        assert candidate["services"]["web"]["volumes"][0]["source"] == expected
        assert deployed["services"]["web"]["volumes"][0]["source"] == expected

    def test_validate_accepts_the_candidate_from_another_directory(self,
                                                                   tmp_path):
        from boxman.providers.docker_compose.compose_runner import ComposeRunner

        workdir = self._project(tmp_path)
        runner = ComposeRunner(project="dirctx", workdir=str(workdir),
                               compose_file=str(workdir / "docker-compose.yml"))
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            assert runner.validate(str(workdir / ".candidate.yml")).ok
        finally:
            os.chdir(cwd)

    def test_the_project_directory_is_carried_not_inferred(self, tmp_path):
        """`--project-directory` must actually be on the command.

        When the compose file sits *in* the workdir, Compose infers the same
        directory from the file's parent and the flag is redundant — so a
        regression built only on that arrangement passes even if `_base()`
        stops sending it. Here the file is elsewhere, so the flag is the only
        thing selecting the workdir's `.env` (#164 NET-C3).
        """
        from boxman.providers.docker_compose.compose_runner import ComposeRunner

        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / ".env").write_text("TAG=alpine\n")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / ".env").write_text("TAG=latest\n")
        (elsewhere / "docker-compose.yml").write_text(
            'services:\n  web: {image: "nginx:${TAG}"}\n')

        runner = ComposeRunner(
            project="dirctx2", workdir=str(workdir),
            compose_file=str(elsewhere / "docker-compose.yml"))
        assert "--project-directory" in runner._base()

        out = invoke.run(f"{runner._base()} config --format json",
                         hide=True, warn=True, in_stream=False)
        assert out.ok, out.stderr
        model = json.loads(out.stdout)

        # the workdir's .env, not the compose file's neighbour
        assert model["services"]["web"]["image"] == "nginx:alpine"
