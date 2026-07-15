"""
Unit tests for the docker-compose provider (Phase 3, epic #42 / issue #51).

Everything here is mocked — no live ``docker`` and no libvirt. Per the
host-vs-VM testing policy, live container behaviour is exercised only inside
the staging VM (nested docker-compose e2e), never on the host. These tests
cover four seams:

- :class:`ComposeGenerator` — ``boxes:`` → compose ``services:`` translation,
  cluster-internal bridge networks, absolute ``build.context`` (D4),
  ``compose_extra:`` deep-merge (D7), and the warn+skip of out-of-phase box
  features (``volumes:`` → Phase 5, shared/macvlan networks → Phase 4).
- :class:`ComposeRunner` — the exact ``docker compose`` command strings and
  the preflight / failure error paths (``boxman.utils.shell.run`` mocked).
- :class:`DockerComposeSession` — satisfies the ``ProviderSession`` protocol,
  the coarse per-cluster methods drive the runner correctly, the per-VM
  protocol methods raise, and the ``runtime: local`` guardrail fires.
- Manager dispatch — ``_vm_clusters`` / ``_compose_clusters`` partition a
  mixed config and the coarse ``*_compose_clusters`` helpers route each dc
  cluster to its session (libvirt clusters untouched).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml

from boxman.abstract.providers import ProviderSession
from boxman.exceptions import ConfigError, ProvisionError, RuntimeUnavailable
from boxman.manager import BoxmanManager
from boxman.providers import create_session
from boxman.providers.docker_compose.compose_generator import ComposeGenerator
from boxman.providers.docker_compose.compose_runner import (
    DEFAULT_READINESS_TIMEOUT,
    ComposeRunner,
)
from boxman.providers.docker_compose.session import (
    DockerComposeSession,
    _sanitize_project_name,
)


# --------------------------------------------------------------------------
# ComposeGenerator
# --------------------------------------------------------------------------
class TestComposeGenerator:

    def test_translates_boxes_to_services_passthrough(self):
        gen = ComposeGenerator()
        cluster = {
            "boxes": {
                "web": {
                    "image": "nginx:latest",
                    "command": "nginx -g 'daemon off;'",
                    "environment": ["FOO=bar"],
                    "ports": ["8080:80"],
                    "depends_on": ["api"],
                    "restart": "unless-stopped",
                    "healthcheck": {"test": ["CMD", "true"]},
                },
            },
        }
        compose = gen.generate("stack", cluster, conf_dir="/proj")
        web = compose["services"]["web"]
        assert web["image"] == "nginx:latest"
        assert web["command"] == "nginx -g 'daemon off;'"
        assert web["environment"] == ["FOO=bar"]
        assert web["ports"] == ["8080:80"]
        assert web["depends_on"] == ["api"]
        assert web["restart"] == "unless-stopped"
        assert web["healthcheck"] == {"test": ["CMD", "true"]}
        # the obsolete top-level `version:` key must not be emitted
        assert "version" not in compose

    def test_unknown_box_keys_are_dropped(self):
        """Only the passthrough keys and build/networks land in a service;
        boxman-specific keys (e.g. base_image) are not leaked into compose."""
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "postgres:16", "base_image": "x"}}}
        svc = gen.generate("s", cluster, conf_dir="/proj")["services"]["db"]
        assert svc == {"image": "postgres:16"}

    def test_build_context_dict_resolved_absolute(self):
        gen = ComposeGenerator()
        cluster = {
            "boxes": {
                "api": {"build": {"context": "./api", "dockerfile": "Dockerfile"}},
            },
        }
        svc = gen.generate("s", cluster, conf_dir="/proj/conf")["services"]["api"]
        assert svc["build"]["context"] == "/proj/conf/api"
        assert svc["build"]["dockerfile"] == "Dockerfile"

    def test_build_context_string_resolved_absolute(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"api": {"build": "api"}}}
        svc = gen.generate("s", cluster, conf_dir="/proj")["services"]["api"]
        assert svc["build"] == "/proj/api"

    def test_cluster_internal_networks_become_top_level(self):
        gen = ComposeGenerator()
        cluster = {
            "networks": {"backend": {"subnet": "172.30.0.0/24"}},
            "boxes": {
                "web": {"image": "nginx", "networks": ["backend"]},
            },
        }
        compose = gen.generate("s", cluster, conf_dir="/proj")
        assert compose["networks"]["backend"] == {
            "driver": "bridge",
            "ipam": {"config": [{"subnet": "172.30.0.0/24"}]},
        }
        assert compose["services"]["web"]["networks"] == ["backend"]

    def test_network_without_subnet_defaults_to_bridge(self):
        gen = ComposeGenerator()
        cluster = {"networks": {"backend": {}}, "boxes": {"w": {"image": "x"}}}
        compose = gen.generate("s", cluster, conf_dir="/proj")
        assert compose["networks"]["backend"] == {"driver": "bridge"}

    def test_compose_extra_deep_merge_cluster_and_box(self):
        gen = ComposeGenerator()
        cluster = {
            "compose_extra": {"services": {"web": {"labels": {"team": "infra"}}}},
            "boxes": {
                "web": {
                    "image": "nginx",
                    "compose_extra": {"labels": {"role": "frontend"}},
                },
            },
        }
        svc = gen.generate("s", cluster, conf_dir="/proj")["services"]["web"]
        # per-box escape hatch merges onto the service
        assert svc["labels"]["role"] == "frontend"
        # per-cluster escape hatch merges onto the whole compose dict
        assert svc["labels"]["team"] == "infra"

    def test_box_compose_extra_overrides_generated_key(self):
        gen = ComposeGenerator()
        cluster = {
            "boxes": {
                "web": {"image": "nginx", "compose_extra": {"image": "nginx:pinned"}},
            },
        }
        svc = gen.generate("s", cluster, conf_dir="/proj")["services"]["web"]
        assert svc["image"] == "nginx:pinned"

    def test_network_compose_extra_merges_onto_spec(self):
        gen = ComposeGenerator()
        cluster = {
            "networks": {
                "backend": {"subnet": "10.0.0.0/24", "compose_extra": {"internal": True}},
            },
            "boxes": {"w": {"image": "x", "networks": ["backend"]}},
        }
        net = gen.generate("s", cluster, conf_dir="/proj")["networks"]["backend"]
        assert net["internal"] is True
        assert net["driver"] == "bridge"

    def test_volumes_warn_and_skip(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "postgres", "volumes": ["data:/var/lib"]}}}
        with mock.patch.object(gen.logger, "warning") as warn:
            svc = gen.generate("s", cluster, conf_dir="/proj")["services"]["db"]
        assert "volumes" not in svc
        assert warn.called
        assert "Phase 5" in warn.call_args[0][0]

    def test_shared_network_ref_warned_and_skipped(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "networks": ["labnet"]}}}
        with mock.patch.object(gen.logger, "warning") as warn:
            svc = gen.generate(
                "s", cluster, conf_dir="/proj", shared_network_names=["labnet"]
            )["services"]["w"]
        assert "networks" not in svc
        assert warn.called
        assert "Phase 4" in warn.call_args[0][0]

    def test_unknown_network_ref_warned_and_skipped(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "networks": ["ghost"]}}}
        with mock.patch.object(gen.logger, "warning") as warn:
            svc = gen.generate("s", cluster, conf_dir="/proj")["services"]["w"]
        assert "networks" not in svc
        assert warn.called

    def test_box_without_image_or_build_raises(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"broken": {"environment": ["X=1"]}}}
        with pytest.raises(ConfigError, match=r"must define 'image:' or 'build:'"):
            gen.generate("s", cluster, conf_dir="/proj")

    def test_write_produces_valid_yaml_file(self, tmp_path):
        gen = ComposeGenerator()
        compose = gen.generate(
            "s", {"boxes": {"w": {"image": "nginx"}}}, conf_dir="/proj"
        )
        path = gen.write(compose, str(tmp_path))
        assert path == os.path.join(str(tmp_path), "docker-compose.yml")
        with open(path) as fobj:
            loaded = yaml.safe_load(fobj)
        assert loaded["services"]["w"]["image"] == "nginx"
        assert "version" not in loaded


# --------------------------------------------------------------------------
# ComposeRunner
# --------------------------------------------------------------------------
def _ok(stdout="", stderr=""):
    return SimpleNamespace(ok=True, stdout=stdout, stderr=stderr)


def _fail(stdout="", stderr="boom"):
    return SimpleNamespace(ok=False, stdout=stdout, stderr=stderr)


class TestComposeRunner:

    def _runner(self):
        return ComposeRunner(
            project="proj_stack",
            compose_file="/wd/docker-compose.yml",
            workdir="/wd",
        )

    def test_base_command_shape(self):
        base = self._runner()._base()
        assert base == (
            "docker compose -p proj_stack "
            "-f /wd/docker-compose.yml "
            "--project-directory /wd"
        )

    def test_up_command_and_wait_timeout(self):
        runner = self._runner()
        with mock.patch(
            "boxman.providers.docker_compose.compose_runner.run",
            return_value=_ok(),
        ) as run:
            runner.up(45)
        cmd = run.call_args[0][0]
        assert "up -d --wait --wait-timeout 45" in cmd
        assert cmd.startswith("docker compose -p proj_stack")

    def test_up_default_timeout(self):
        runner = self._runner()
        with mock.patch(
            "boxman.providers.docker_compose.compose_runner.run",
            return_value=_ok(),
        ) as run:
            runner.up()
        assert f"--wait-timeout {DEFAULT_READINESS_TIMEOUT}" in run.call_args[0][0]

    def test_up_raises_provision_error_on_failure(self):
        runner = self._runner()
        with mock.patch(
            "boxman.providers.docker_compose.compose_runner.run",
            return_value=_fail(stderr="service unhealthy"),
        ):
            with pytest.raises(ProvisionError, match=r"service unhealthy"):
                runner.up(10)

    def test_down_stop_start_commands(self):
        runner = self._runner()
        with mock.patch(
            "boxman.providers.docker_compose.compose_runner.run",
            return_value=_ok(),
        ) as run:
            runner.down()
            runner.down_volumes()
            runner.stop()
            runner.start()
        cmds = [c.args[0] for c in run.call_args_list]
        assert any(c.endswith("down --remove-orphans") for c in cmds)
        assert any("down --volumes --remove-orphans" in c for c in cmds)
        assert any(c.endswith(" stop") for c in cmds)
        assert any(c.endswith(" start") for c in cmds)

    def test_project_and_file_are_shell_quoted(self):
        runner = ComposeRunner(
            project="pr oj",
            compose_file="/w d/docker-compose.yml",
            workdir="/w d",
        )
        base = runner._base()
        assert "'pr oj'" in base
        assert "'/w d/docker-compose.yml'" in base

    def test_preflight_missing_docker_raises(self):
        runner = self._runner()
        with mock.patch(
            "boxman.providers.docker_compose.compose_runner.shutil.which",
            return_value=None,
        ):
            with pytest.raises(RuntimeUnavailable, match=r"'docker' is not on PATH"):
                runner.preflight()

    def test_preflight_missing_compose_plugin_raises(self):
        runner = self._runner()
        with mock.patch(
            "boxman.providers.docker_compose.compose_runner.shutil.which",
            return_value="/usr/bin/docker",
        ), mock.patch(
            "boxman.providers.docker_compose.compose_runner.run",
            return_value=_fail(),
        ):
            with pytest.raises(RuntimeUnavailable, match=r"Compose v2 plugin"):
                runner.preflight()

    def test_preflight_ok(self):
        runner = self._runner()
        with mock.patch(
            "boxman.providers.docker_compose.compose_runner.shutil.which",
            return_value="/usr/bin/docker",
        ), mock.patch(
            "boxman.providers.docker_compose.compose_runner.run",
            return_value=_ok(),
        ):
            runner.preflight()  # no raise


# --------------------------------------------------------------------------
# DockerComposeSession
# --------------------------------------------------------------------------
class _FakeRunner:
    """Records coarse-method calls without touching docker."""

    def __init__(self):
        self.project = "proj_stack"
        self.calls: list = []

    def preflight(self):
        self.calls.append(("preflight",))

    def up(self, timeout):
        self.calls.append(("up", timeout))

    def down(self):
        self.calls.append(("down",))

    def down_volumes(self):
        self.calls.append(("down_volumes",))

    def stop(self):
        self.calls.append(("stop",))

    def start(self):
        self.calls.append(("start",))


class TestDockerComposeSession:

    def _session(self, runtime="local"):
        session = DockerComposeSession({"provider": {"docker-compose": {}}})
        session.manager = SimpleNamespace(runtime=runtime, config_path="/proj/conf.yml")
        return session

    def _patch_context(self, session, runner, compose_file="/wd/docker-compose.yml"):
        return mock.patch.object(
            session, "_compose_context",
            return_value=(runner, "/wd", compose_file),
        )

    def test_satisfies_provider_protocol(self):
        assert isinstance(self._session(), ProviderSession)

    def test_registry_builds_the_session(self):
        session = create_session(
            "docker-compose", {"provider": {"docker-compose": {}}})
        assert isinstance(session, DockerComposeSession)

    def test_up_cluster_preflights_and_ups_with_timeout(self):
        session = self._session()
        runner = _FakeRunner()
        with self._patch_context(session, runner):
            session.up_cluster("stack", {"readiness_timeout": 30})
        assert ("preflight",) in runner.calls
        assert ("up", 30) in runner.calls

    def test_up_cluster_default_timeout(self):
        session = self._session()
        runner = _FakeRunner()
        with self._patch_context(session, runner):
            session.up_cluster("stack", {})
        assert ("up", DEFAULT_READINESS_TIMEOUT) in runner.calls

    def test_stop_cluster_calls_stop(self):
        session = self._session()
        runner = _FakeRunner()
        with self._patch_context(session, runner):
            session.stop_cluster("stack", {})
        assert runner.calls == [("stop",)]

    def test_start_cluster_calls_start(self):
        session = self._session()
        runner = _FakeRunner()
        with self._patch_context(session, runner):
            session.start_cluster("stack", {})
        assert runner.calls == [("start",)]

    def test_down_cluster_calls_down(self):
        session = self._session()
        runner = _FakeRunner()
        with self._patch_context(session, runner):
            session.down_cluster("stack", {})
        assert runner.calls == [("down",)]

    def test_destroy_cluster_downs_volumes_and_removes_file(self, tmp_path):
        session = self._session()
        runner = _FakeRunner()
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}\n")
        with self._patch_context(session, runner, str(compose_file)):
            session.destroy_cluster("stack", {})
        assert runner.calls == [("down_volumes",)]
        assert not compose_file.exists()

    def test_require_local_runtime_guardrail(self):
        session = self._session(runtime="docker-compose")
        with pytest.raises(ConfigError, match=r"requires runtime 'local'"):
            session.up_cluster("stack", {"workdir": "/wd"})

    @pytest.mark.parametrize("method,args", [
        ("start_vm", ("vm",)),
        ("destroy_vm", ("vm",)),
        ("clone_vm", ("new", "src", {}, "/wd")),
        ("define_network", ()),
        ("destroy_network", ()),
        ("remove_network", ()),
        ("snapshot_take", ()),
        ("snapshot_restore", ("vm",)),
        ("snapshot_delete", ("vm", "snap")),
        ("snapshot_list", ()),
    ])
    def test_per_vm_protocol_methods_raise(self, method, args):
        session = self._session()
        with pytest.raises(ProvisionError, match=r"cluster-scoped"):
            getattr(session, method)(*args)

    def test_compose_project_name_is_sanitized_and_scoped(self):
        session = DockerComposeSession(
            {"project": "My Proj", "provider": {"docker-compose": {}}})
        assert session._compose_project("stack-1") == "my_proj_stack-1"

    def test_sanitize_project_name(self):
        assert _sanitize_project_name("A/B C") == "a_b_c"
        assert _sanitize_project_name("__weird") == "weird"
        assert _sanitize_project_name("---") == "boxman"


# --------------------------------------------------------------------------
# Manager dispatch (coarse per-cluster seam)
# --------------------------------------------------------------------------
class _RecordingSession:
    def __init__(self):
        self.calls: list = []

    def up_cluster(self, name, cfg):
        self.calls.append(("up", name))

    def stop_cluster(self, name, cfg):
        self.calls.append(("stop", name))

    def start_cluster(self, name, cfg):
        self.calls.append(("start", name))

    def down_cluster(self, name, cfg):
        self.calls.append(("down", name))

    def destroy_cluster(self, name, cfg):
        self.calls.append(("destroy", name))


class TestManagerDispatch:

    def _mixed_manager(self):
        manager = BoxmanManager()
        manager.config = {
            "project": "proj",
            "provider": {"libvirt": {}},
            "clusters": {
                "vms": {"vms": {"vm1": {}}},
                "svc": {"provider": "docker-compose", "boxes": {"web": {"image": "x"}}},
            },
        }
        dc = _RecordingSession()
        manager.register_session("docker-compose", dc)
        return manager, dc

    def test_partition_vm_vs_compose_clusters(self):
        manager, _ = self._mixed_manager()
        assert set(manager._vm_clusters) == {"vms"}
        assert set(manager._compose_clusters()) == {"svc"}
        assert manager._is_compose_cluster("svc") is True
        assert manager._is_compose_cluster("vms") is False

    def test_provision_compose_routes_only_dc_clusters(self):
        manager, dc = self._mixed_manager()
        manager.provision_compose_clusters()
        assert dc.calls == [("up", "svc")]

    def test_all_lifecycle_helpers_route_to_session(self):
        manager, dc = self._mixed_manager()
        manager.stop_compose_clusters()
        manager.start_compose_clusters()
        manager.deprovision_compose_clusters()
        manager.destroy_compose_clusters()
        assert dc.calls == [
            ("stop", "svc"),
            ("start", "svc"),
            ("down", "svc"),
            ("destroy", "svc"),
        ]

    def test_libvirt_only_project_has_no_compose_work(self):
        manager = BoxmanManager()
        manager.config = {
            "project": "proj",
            "provider": {"libvirt": {}},
            "clusters": {"a": {"vms": {"vm1": {}}}},
        }
        assert manager._compose_clusters() == {}
        # coarse helpers are safe no-ops (no dc session registered)
        manager.provision_compose_clusters()
        manager.destroy_compose_clusters()

    def test_dc_only_project_partitions_all_clusters_as_compose(self):
        manager = BoxmanManager()
        manager.config = {
            "project": "proj",
            "provider": {"docker-compose": {}},
            "clusters": {"svc": {"boxes": {"web": {"image": "x"}}}},
        }
        dc = _RecordingSession()
        manager.register_session("docker-compose", dc)
        assert manager._vm_clusters == {}
        assert set(manager._compose_clusters()) == {"svc"}
        manager.provision_compose_clusters()
        assert dc.calls == [("up", "svc")]
