"""
Unit tests for the docker-compose provider (epic #42 / issues #51 Phase 3,
#52 Phase 4).

Everything here is mocked — no live ``docker`` and no libvirt. Per the
host-vs-VM testing policy, live container behaviour is exercised only inside
the staging VM (nested docker-compose e2e), never on the host. These tests
cover four seams:

- :class:`ComposeGenerator` — ``boxes:`` → compose ``services:`` translation,
  cluster-internal bridge networks, ``shared_networks`` → macvlan attach
  (Phase 4: static/auto IPs, IPAM, reference-gating), structured ``volumes:``
  (Phase 5: named / bind / workdir, readonly, advisory size, fail-fast),
  absolute ``build.context`` (D4), and ``compose_extra:`` deep-merge (D7).
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

import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml

from boxman.abstract.providers import ProviderSession
from boxman.exceptions import (
    ConfigError,
    ProvisionError,
    RuntimeUnavailable,
    SnapshotError,
)
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
    _snapshot_tag,
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

    def test_unknown_box_keys_dropped_with_warning(self):
        """Keys outside the Phase-3 box schema (e.g. a carried-over libvirt
        'base_image', or a typo) are dropped from the service AND warned about
        — not silently swallowed."""
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "postgres:16", "base_image": "x",
                                    "enviroment": ["TYPO=1"]}}}
        with mock.patch.object(gen.logger, "warning") as warn:
            svc = gen.generate("s", cluster, conf_dir="/proj")["services"]["db"]
        assert svc == {"image": "postgres:16"}
        assert warn.called
        msg = warn.call_args[0][0]
        assert "base_image" in msg and "enviroment" in msg

    def test_known_box_keys_do_not_warn(self):
        """A well-formed box (only recognised keys, incl. volumes/compose_extra)
        raises no unknown-key warning."""
        gen = ComposeGenerator()
        cluster = {"boxes": {"web": {"image": "nginx", "networks": [],
                                     "compose_extra": {"labels": {"a": "b"}}}}}
        with mock.patch.object(gen.logger, "warning") as warn:
            gen.generate("s", cluster, conf_dir="/proj")
        assert not any(
            "unknown key" in str(c.args[0]) for c in warn.call_args_list)

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

    @pytest.mark.parametrize("ctx", [
        "https://github.com/docker/awesome-compose.git#main",
        "git@github.com:docker/awesome-compose.git",
        "github.com/docker/awesome-compose",
        "git://example.com/repo.git",
    ])
    def test_remote_build_context_string_preserved(self, ctx):
        gen = ComposeGenerator()
        cluster = {"boxes": {"api": {"build": ctx}}}
        svc = gen.generate("s", cluster, conf_dir="/proj")["services"]["api"]
        assert svc["build"] == ctx  # not resolved onto conf_dir

    def test_remote_build_context_mapping_preserved(self):
        gen = ComposeGenerator()
        ctx = "https://github.com/docker/awesome-compose.git#main"
        cluster = {"boxes": {"api": {"build": {"context": ctx, "dockerfile": "Dockerfile"}}}}
        svc = gen.generate("s", cluster, conf_dir="/proj")["services"]["api"]
        assert svc["build"]["context"] == ctx
        assert svc["build"]["dockerfile"] == "Dockerfile"

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

    # -- Phase 5: volumes -------------------------------------------------
    def test_named_volume_emits_mount_and_top_level(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "postgres", "volumes": [
            {"name": "pg_data", "container_path": "/var/lib/postgresql/data"}]}}}
        compose = gen.generate("s", cluster, conf_dir="/proj")
        assert compose["services"]["db"]["volumes"] == [
            "pg_data:/var/lib/postgresql/data"]
        assert compose["volumes"] == {"pg_data": {"driver": "local"}}

    def test_bind_mount_resolves_abs_and_readonly(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "nginx", "volumes": [
            {"host_path": "./configs", "container_path": "/etc/app",
             "readonly": True}]}}}
        compose = gen.generate("s", cluster, conf_dir="/proj")
        assert compose["services"]["w"]["volumes"] == ["/proj/configs:/etc/app:ro"]
        assert "volumes" not in compose  # bind mounts have no top-level entry

    def test_workdir_is_a_bind_at_conf_dir(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "volumes": [
            {"host_path": ".", "container_path": "/workspace"}]}}}
        svc = gen.generate("s", cluster, conf_dir="/proj")["services"]["w"]
        assert svc["volumes"] == ["/proj:/workspace"]

    def test_named_volume_size_is_advisory_warn(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "postgres", "volumes": [
            {"name": "pg_data", "container_path": "/data", "size": "10G"}]}}}
        with mock.patch.object(gen.logger, "warning") as warn:
            compose = gen.generate("s", cluster, conf_dir="/proj")
        assert compose["services"]["db"]["volumes"] == ["pg_data:/data"]  # still emitted
        assert compose["volumes"] == {"pg_data": {"driver": "local"}}      # no size enforced
        assert any("advisory" in str(c.args[0]) for c in warn.call_args_list)

    def test_size_on_bind_warns_ignored(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "volumes": [
            {"host_path": "./d", "container_path": "/d", "size": "5G"}]}}}
        with mock.patch.object(gen.logger, "warning") as warn:
            gen.generate("s", cluster, conf_dir="/proj")
        assert any("ignored on the bind mount" in str(c.args[0])
                   for c in warn.call_args_list)

    def test_shared_named_volume_defined_once(self):
        """Two boxes mounting the same named volume → one top-level entry."""
        gen = ComposeGenerator()
        cluster = {"boxes": {
            "a": {"image": "x", "volumes": [{"name": "shared", "container_path": "/a"}]},
            "b": {"image": "x", "volumes": [{"name": "shared", "container_path": "/b"}]},
        }}
        compose = gen.generate("s", cluster, conf_dir="/proj")
        assert compose["volumes"] == {"shared": {"driver": "local"}}
        assert compose["services"]["a"]["volumes"] == ["shared:/a"]
        assert compose["services"]["b"]["volumes"] == ["shared:/b"]

    def test_named_volume_compose_extra_merges(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "x", "volumes": [
            {"name": "v", "container_path": "/d",
             "compose_extra": {"driver_opts": {"type": "none", "o": "bind"}}}]}}}
        vol = gen.generate("s", cluster, conf_dir="/proj")["volumes"]["v"]
        assert vol == {"driver": "local",
                       "driver_opts": {"type": "none", "o": "bind"}}

    def test_volume_missing_container_path_raises(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "x", "volumes": [{"name": "v"}]}}}
        with pytest.raises(ConfigError, match=r"missing 'container_path'"):
            gen.generate("s", cluster, conf_dir="/proj")

    def test_named_volume_missing_name_raises(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "x",
                                    "volumes": [{"container_path": "/d"}]}}}
        with pytest.raises(ConfigError, match=r"needs a 'name'"):
            gen.generate("s", cluster, conf_dir="/proj")

    def test_non_dict_volume_entry_raises(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "x", "volumes": ["data:/var/lib"]}}}
        with pytest.raises(ConfigError, match=r"must\s+be a mapping"):
            gen.generate("s", cluster, conf_dir="/proj")

    def test_volumes_not_a_list_raises(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "x", "volumes": {"name": "v"}}}}
        with pytest.raises(ConfigError, match=r"'volumes:' must be a list"):
            gen.generate("s", cluster, conf_dir="/proj")

    def test_unknown_volume_key_warns(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "x", "volumes": [
            {"name": "v", "container_path": "/d", "typo": 1}]}}}
        with mock.patch.object(gen.logger, "warning") as warn:
            gen.generate("s", cluster, conf_dir="/proj")
        assert any("unknown" in str(c.args[0]) for c in warn.call_args_list)

    # -- Phase 5 review: fail-fast paths + no-op warnings -----------------
    def test_relative_container_path_raises(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "x", "volumes": [
            {"name": "v", "container_path": "data"}]}}}
        with pytest.raises(ConfigError, match=r"must be an absolute path"):
            gen.generate("s", cluster, conf_dir="/proj")

    @pytest.mark.parametrize("entry", [
        {"name": "v", "container_path": "/d:x"},        # colon in container_path
        {"name": "v:x", "container_path": "/d"},        # colon in name
        {"host_path": "./h:x", "container_path": "/d"}, # colon in host_path
    ])
    def test_colon_in_path_or_name_raises(self, entry):
        gen = ComposeGenerator()
        cluster = {"boxes": {"db": {"image": "x", "volumes": [entry]}}}
        with pytest.raises(ConfigError, match=r"must not contain ':'"):
            gen.generate("s", cluster, conf_dir="/proj")

    def test_bind_named_only_keys_warn_ignored(self):
        """`name:`/`compose_extra:` on a bind mount are no-ops → warned."""
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "volumes": [
            {"host_path": "./h", "container_path": "/d",
             "name": "ignored", "compose_extra": {"x": 1}}]}}}
        with mock.patch.object(gen.logger, "warning") as warn:
            compose = gen.generate("s", cluster, conf_dir="/proj")
        assert compose["services"]["w"]["volumes"] == ["/proj/h:/d"]
        assert "volumes" not in compose  # no top-level entry for a bind
        msgs = " ".join(str(c.args[0]) for c in warn.call_args_list)
        assert "'name:' is ignored on the bind mount" in msgs
        assert "'compose_extra:' is ignored on the bind mount" in msgs

    def test_shared_named_volume_divergent_compose_extra_warns(self):
        """First-seen spec wins; a later box's differing compose_extra warns."""
        gen = ComposeGenerator()
        cluster = {"boxes": {
            "a": {"image": "x", "volumes": [
                {"name": "v", "container_path": "/a",
                 "compose_extra": {"driver_opts": {"o": "bind"}}}]},
            "b": {"image": "x", "volumes": [
                {"name": "v", "container_path": "/b",
                 "compose_extra": {"driver_opts": {"o": "tmpfs"}}}]},
        }}
        with mock.patch.object(gen.logger, "warning") as warn:
            compose = gen.generate("s", cluster, conf_dir="/proj")
        # first-seen (box a) spec is kept
        assert compose["volumes"]["v"]["driver_opts"] == {"o": "bind"}
        assert any("already defined by an earlier box" in str(c.args[0])
                   for c in warn.call_args_list)

    def test_shared_named_volume_same_spec_no_warn(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {
            "a": {"image": "x", "volumes": [{"name": "v", "container_path": "/a"}]},
            "b": {"image": "x", "volumes": [{"name": "v", "container_path": "/b"}]},
        }}
        with mock.patch.object(gen.logger, "warning") as warn:
            gen.generate("s", cluster, conf_dir="/proj")
        assert not any("already defined" in str(c.args[0])
                       for c in warn.call_args_list)

    # -- Phase 4: shared_networks → macvlan ------------------------------
    def _shared(self, **over):
        base = {"bridge": "br-lab", "subnet": "10.10.0.0/24"}
        base.update(over)
        return {"labnet": base}

    def test_shared_network_emits_macvlan_and_attaches(self):
        """A box ref to a shared_networks bridge → a top-level macvlan network
        (driver + parent + ipam subnet) and a plain-list service attachment."""
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "networks": ["labnet"]}}}
        compose = gen.generate(
            "s", cluster, conf_dir="/proj", shared_networks=self._shared()
        )
        assert compose["networks"]["labnet"] == {
            "driver": "macvlan",
            "driver_opts": {"parent": "br-lab"},
            "ipam": {"config": [{"subnet": "10.10.0.0/24"}]},
        }
        assert compose["services"]["w"]["networks"] == ["labnet"]

    def test_shared_network_static_ipv4_address_mapping_form(self):
        """A mapping ``{net: {ipv4_address: …}}`` pins a static address and
        forces the service ``networks`` into mapping form."""
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x",
                                   "networks": {"labnet": {"ipv4_address": "10.10.0.5"}}}}}
        compose = gen.generate(
            "s", cluster, conf_dir="/proj", shared_networks=self._shared()
        )
        assert compose["services"]["w"]["networks"] == {
            "labnet": {"ipv4_address": "10.10.0.5"}
        }
        assert compose["networks"]["labnet"]["driver"] == "macvlan"

    def test_shared_network_gateway_and_ip_range_in_ipam(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "networks": ["labnet"]}}}
        shared = self._shared(gateway="10.10.0.1", ip_range="10.10.0.128/25")
        net = gen.generate(
            "s", cluster, conf_dir="/proj", shared_networks=shared
        )["networks"]["labnet"]
        assert net["ipam"]["config"][0] == {
            "subnet": "10.10.0.0/24",
            "gateway": "10.10.0.1",
            "ip_range": "10.10.0.128/25",
        }

    def test_shared_network_only_emitted_when_referenced(self):
        """A shared network declared but attached by no box is not emitted."""
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x"}}}
        compose = gen.generate(
            "s", cluster, conf_dir="/proj", shared_networks=self._shared()
        )
        assert "networks" not in compose

    def test_shared_network_without_subnet_raises(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "networks": ["labnet"]}}}
        shared = {"labnet": {"bridge": "br-lab"}}  # no subnet
        with pytest.raises(ConfigError, match=r"needs a 'subnet:'"):
            gen.generate("s", cluster, conf_dir="/proj", shared_networks=shared)

    def test_shared_network_without_bridge_raises(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "networks": ["labnet"]}}}
        shared = {"labnet": {"subnet": "10.10.0.0/24"}}  # no bridge
        with pytest.raises(ConfigError, match=r"no 'bridge:'"):
            gen.generate("s", cluster, conf_dir="/proj", shared_networks=shared)

    def test_shared_and_cluster_networks_mixed_mapping_form(self):
        """A box on both a cluster-internal net and a static-IP shared net →
        mapping form with an empty opts dict for the internal one."""
        gen = ComposeGenerator()
        cluster = {
            "networks": {"backend": {"subnet": "172.30.0.0/24"}},
            "boxes": {"w": {"image": "x", "networks": {
                "backend": None,
                "labnet": {"ipv4_address": "10.10.0.9"},
            }}},
        }
        compose = gen.generate(
            "s", cluster, conf_dir="/proj", shared_networks=self._shared()
        )
        assert compose["services"]["w"]["networks"] == {
            "backend": {},
            "labnet": {"ipv4_address": "10.10.0.9"},
        }
        assert compose["networks"]["backend"]["driver"] == "bridge"
        assert compose["networks"]["labnet"]["driver"] == "macvlan"

    def test_ipv4_address_on_cluster_internal_net_warns_and_drops(self):
        gen = ComposeGenerator()
        cluster = {
            "networks": {"backend": {"subnet": "172.30.0.0/24"}},
            "boxes": {"w": {"image": "x",
                            "networks": {"backend": {"ipv4_address": "172.30.0.5"}}}},
        }
        with mock.patch.object(gen.logger, "warning") as warn:
            svc = gen.generate("s", cluster, conf_dir="/proj")["services"]["w"]
        # dropped → plain list attach, no static IP wired
        assert svc["networks"] == ["backend"]
        assert any("only wired for shared_networks" in str(c.args[0])
                   for c in warn.call_args_list)

    def test_shared_network_compose_extra_merges_onto_macvlan(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "networks": ["labnet"]}}}
        shared = self._shared(compose_extra={"driver_opts": {"macvlan_mode": "bridge"}})
        net = gen.generate(
            "s", cluster, conf_dir="/proj", shared_networks=shared
        )["networks"]["labnet"]
        assert net["driver_opts"] == {"parent": "br-lab", "macvlan_mode": "bridge"}

    def test_unknown_network_ref_warned_and_skipped(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "networks": ["ghost"]}}}
        with mock.patch.object(gen.logger, "warning") as warn:
            svc = gen.generate("s", cluster, conf_dir="/proj")["services"]["w"]
        assert "networks" not in svc
        assert warn.called

    # -- malformed networks: fail fast, never silently drop -----------------
    def test_bare_string_networks_is_attached_not_silently_dropped(self):
        """A forgotten list (`networks: labnet`) must still attach — not vanish
        (which would land the service on the default bridge, no macvlan)."""
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "networks": "labnet"}}}
        svc = gen.generate(
            "s", cluster, conf_dir="/proj", shared_networks=self._shared()
        )["services"]["w"]
        assert svc["networks"] == ["labnet"]

    def test_scalar_network_opts_raises_configerror(self):
        """`networks: {labnet: 10.10.0.5}` (a scalar where a mapping/null is
        required) is a ConfigError, not a raw ValueError."""
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x",
                                   "networks": {"labnet": "10.10.0.5"}}}}
        with pytest.raises(ConfigError, match=r"must be a mapping"):
            gen.generate("s", cluster, conf_dir="/proj",
                         shared_networks=self._shared())

    def test_non_list_non_mapping_networks_raises_configerror(self):
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "x", "networks": 123}}}
        with pytest.raises(ConfigError, match=r"must be a list or mapping"):
            gen.generate("s", cluster, conf_dir="/proj")

    def test_generate_emits_top_level_name_when_project_given(self):
        """With project_name, a top-level `name:` is emitted so the file is
        hand-runnable under the same project boxman uses (D5)."""
        gen = ComposeGenerator()
        cluster = {"boxes": {"w": {"image": "nginx"}}}
        compose = gen.generate("web", cluster, conf_dir="/proj",
                               project_name="dc_standalone_web")
        assert compose["name"] == "dc_standalone_web"
        # name comes first, services still present
        assert list(compose)[0] == "name"
        assert "w" in compose["services"]

    def test_generate_omits_name_without_project(self):
        gen = ComposeGenerator()
        compose = gen.generate("web", {"boxes": {"w": {"image": "x"}}},
                               conf_dir="/proj")
        assert "name" not in compose

    def test_corrupted_templating_warns(self):
        """A '{word}' token (the signature of a mangled bare '{{ word }}') in a
        compose command:/environment: value is flagged."""
        gen = ComposeGenerator()
        cluster = {"boxes": {"web": {"image": "nginx",
                                     "command": "echo {hostname}"}}}
        with mock.patch.object(gen.logger, "warning") as warn:
            gen.generate("s", cluster, conf_dir="/proj")
        assert any("token" in str(c.args[0]) for c in warn.call_args_list)

    def test_dollar_var_interpolation_does_not_warn(self):
        """'${VAR}' is a safe compose interpolation — not corruption."""
        gen = ComposeGenerator()
        cluster = {"boxes": {"web": {"image": "nginx",
                                     "environment": ["HOST=${HOSTVAR}"]}}}
        with mock.patch.object(gen.logger, "warning") as warn:
            gen.generate("s", cluster, conf_dir="/proj")
        assert not any("token" in str(c.args[0]) for c in warn.call_args_list)

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

    def test_base_command_label_only(self):
        """A teardown runner with no file/workdir operates by project label."""
        runner = ComposeRunner(project="proj_stack")
        assert runner._base() == "docker compose -p proj_stack"

    def test_use_sudo_prefixes_command(self):
        runner = ComposeRunner(
            project="proj_stack", compose_file="/wd/docker-compose.yml",
            workdir="/wd", use_sudo=True)
        assert runner._base().startswith("sudo docker compose -p proj_stack")

    def test_up_without_compose_file_raises(self):
        runner = ComposeRunner(project="proj_stack")  # label-only
        with pytest.raises(ProvisionError, match=r"without a compose file"):
            runner.up(10)

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

    # -- Phase 6: exec / pause / ps ------------------------------------
    def test_exec_command_interactive_and_cmd(self):
        r = self._runner()  # project proj_stack, file /wd/docker-compose.yml, wd /wd
        assert r.exec_command("web") == [
            "docker", "compose", "-p", "proj_stack",
            "-f", "/wd/docker-compose.yml", "--project-directory", "/wd",
            "exec", "web", "sh"]
        assert r.exec_command("web", ["redis-cli", "ping"])[-4:] == [
            "-T", "web", "redis-cli", "ping"]
        assert r.exec_command("web", shell="bash")[-2:] == ["web", "bash"]

    def test_pause_unpause_commands(self):
        r = self._runner()
        with mock.patch(
            "boxman.providers.docker_compose.compose_runner.run",
            return_value=_ok(),
        ) as run:
            r.pause()
            r.pause(["web"])
            r.unpause()
        cmds = [c.args[0] for c in run.call_args_list]
        assert any(c.endswith(" pause") for c in cmds)
        assert any(c.endswith(" pause web") for c in cmds)
        assert any(c.endswith(" unpause") for c in cmds)

    def test_ps_json_command(self):
        r = self._runner()
        with mock.patch(
            "boxman.providers.docker_compose.compose_runner.run",
            return_value=_ok("{}"),
        ) as run:
            r.ps_json()
        assert "ps --all --format json" in run.call_args_list[0].args[0]

    # -- Phase 7: snapshot primitives ----------------------------------
    def test_commit_image_rm_commands(self):
        r = self._runner()
        with mock.patch(
            "boxman.providers.docker_compose.compose_runner.run",
            return_value=_ok(),
        ) as run:
            r.commit("proj_stack-web-1", "boxman/proj_stack_web:v1")
            r.image_rm("boxman/proj_stack_web:v1")
        cmds = [c.args[0] for c in run.call_args_list]
        assert any(c.startswith("docker commit proj_stack-web-1 boxman/proj_stack_web:v1") for c in cmds)
        assert any("docker image rm -f boxman/proj_stack_web:v1" in c for c in cmds)

    def test_up_force_recreate_flag(self):
        r = self._runner()
        with mock.patch(
            "boxman.providers.docker_compose.compose_runner.run",
            return_value=_ok(),
        ) as run:
            r.up(30, force_recreate=True)
        assert "up -d --wait --force-recreate --wait-timeout 30" in run.call_args_list[0].args[0]


# --------------------------------------------------------------------------
# DockerComposeSession
# --------------------------------------------------------------------------
class _FakeRunner:
    """Records coarse-method calls without touching docker."""

    def __init__(self):
        self.project = "proj_stack"
        self.calls: list = []
        self.ps_json_result = _ok("")
        #: knobs for the failure paths (snapshot review fixes)
        self.commit_fail_on = None      # container name whose commit fails
        self.image_rm_result = None     # override the image_rm Result
        self.image_exists_result = True

    def preflight(self):
        self.calls.append(("preflight",))

    def up(self, timeout, force_recreate=False):
        self.calls.append(("up", timeout, force_recreate))

    def down(self):
        self.calls.append(("down",))

    def down_volumes(self):
        self.calls.append(("down_volumes",))

    def stop(self):
        self.calls.append(("stop",))

    def start(self):
        self.calls.append(("start",))

    def pause(self, services=None):
        self.calls.append(("pause", services))
        return _ok()

    def unpause(self, services=None):
        self.calls.append(("unpause", services))
        return _ok()

    # snapshots (Phase 7)
    def ps_json(self):
        self.calls.append(("ps_json",))
        return self.ps_json_result

    def exec_command(self, box, cmd=None, shell="sh"):
        self.calls.append(("exec_command", box, cmd, shell))
        return ["compose", "exec", box, *(cmd or [shell])]

    def commit(self, container, tag):
        self.calls.append(("commit", container, tag))
        if self.commit_fail_on == container:
            return _fail(stderr="no such container")
        return _ok()

    def image_rm(self, tag):
        self.calls.append(("image_rm", tag))
        return self.image_rm_result if self.image_rm_result is not None else _ok()

    def image_exists(self, tag):
        self.calls.append(("image_exists", tag))
        return self.image_exists_result


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

    def _patch_teardown(self, session, runner, compose_file="/wd/docker-compose.yml"):
        return mock.patch.object(
            session, "_teardown_runner",
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
        assert ("up", 30, False) in runner.calls

    def test_up_cluster_default_timeout(self):
        session = self._session()
        runner = _FakeRunner()
        with self._patch_context(session, runner):
            session.up_cluster("stack", {})
        assert ("up", DEFAULT_READINESS_TIMEOUT, False) in runner.calls

    def test_up_cluster_precreates_bind_dirs(self):
        """Bind-mount host dirs are mkdir -p'd before compose up; named volumes
        (docker-managed) are not."""
        session = self._session()
        runner = _FakeRunner()
        cluster = {"boxes": {"w": {"image": "x", "volumes": [
            {"host_path": "./data", "container_path": "/d"},
            {"name": "vol", "container_path": "/v"},
        ]}}}
        with self._patch_context(session, runner), \
             mock.patch("boxman.providers.docker_compose.session.os.path.exists",
                        return_value=False), \
             mock.patch("boxman.providers.docker_compose.session.os.makedirs") as md:
            session.up_cluster("stack", cluster)
        made = [c.args[0] for c in md.call_args_list]
        assert "/proj/data" in made                 # bind dir created
        assert all("vol" not in p for p in made)     # named volume is not a dir

    def test_ensure_bind_dirs_skips_existing_and_named(self):
        session = self._session()
        cluster = {"boxes": {"w": {"volumes": [
            {"host_path": "./data", "container_path": "/d"},   # exists → skip
            {"name": "vol", "container_path": "/v"},           # named → skip
        ]}}}
        with mock.patch("boxman.providers.docker_compose.session.os.path.exists",
                        return_value=True), \
             mock.patch("boxman.providers.docker_compose.session.os.makedirs") as md:
            session._ensure_bind_dirs("stack", cluster)
        md.assert_not_called()

    def test_readiness_timeout_non_integer_raises(self):
        session = self._session()
        with pytest.raises(ConfigError, match=r"readiness_timeout must be an integer"):
            session.up_cluster("stack", {"readiness_timeout": "soon"})

    def test_readiness_timeout_non_positive_raises(self):
        session = self._session()
        with pytest.raises(ConfigError, match=r"must be a positive integer"):
            session.up_cluster("stack", {"readiness_timeout": 0})

    def test_stop_cluster_calls_stop(self):
        session = self._session()
        runner = _FakeRunner()
        with self._patch_teardown(session, runner):
            session.stop_cluster("stack", {})
        assert runner.calls == [("stop",)]

    def test_start_cluster_calls_start(self):
        session = self._session()
        runner = _FakeRunner()
        with self._patch_teardown(session, runner):
            session.start_cluster("stack", {})
        assert runner.calls == [("start",)]

    def test_down_cluster_calls_down(self):
        session = self._session()
        runner = _FakeRunner()
        with self._patch_teardown(session, runner):
            session.down_cluster("stack", {})
        assert runner.calls == [("down",)]

    def test_destroy_cluster_downs_volumes_and_removes_file(self, tmp_path):
        session = self._session()
        runner = _FakeRunner()
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}\n")
        with self._patch_teardown(session, runner, str(compose_file)):
            session.destroy_cluster("stack", {})
        assert runner.calls == [("down_volumes",)]
        assert not compose_file.exists()

    # -- teardown is best-effort but not silent (Finding 7) ----------------

    def test_teardown_warns_and_returns_false_on_failure(self):
        session = self._session()

        class _FailRunner:
            def stop(self):
                return SimpleNamespace(ok=False, stdout="", stderr="daemon down")

        with self._patch_teardown(session, _FailRunner()):
            assert session.stop_cluster("stack", {}) is False

    def test_destroy_keeps_file_when_teardown_fails(self, tmp_path):
        session = self._session()
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}\n")

        class _FailRunner:
            def down_volumes(self):
                return SimpleNamespace(ok=False, stdout="", stderr="boom")

        with self._patch_teardown(session, _FailRunner(), str(compose_file)):
            assert session.destroy_cluster("stack", {}) is False
        assert compose_file.exists()  # kept for retry

    # -- teardown never regenerates (Finding 5) ----------------------------

    def test_teardown_runner_uses_ondisk_file_without_regenerating(self, tmp_path):
        session = self._session()
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        with mock.patch.object(session._generator, "generate") as gen:
            runner, wd, cf = session._teardown_runner("app", {"workdir": str(tmp_path)})
        gen.assert_not_called()
        assert runner.compose_file == str(tmp_path / "docker-compose.yml")
        assert "-f " in runner._base()

    def test_teardown_runner_label_only_when_file_absent(self, tmp_path):
        session = self._session()
        runner, wd, cf = session._teardown_runner("app", {"workdir": str(tmp_path)})
        assert runner.compose_file is None
        base = runner._base()
        assert "-f " not in base
        assert base.startswith("docker compose -p ")

    def test_teardown_missing_workdir_raises_configerror(self):
        session = self._session()
        with pytest.raises(ConfigError, match=r"no 'workdir:'"):
            session._teardown_runner("app", {})

    # -- Phase 6: exec / status / pause -----------------------------------
    def test_exec_command_for_valid_box(self):
        session = self._session()
        runner = _FakeRunner()
        with self._patch_teardown(session, runner):
            session.exec_command_for(
                "services", {"boxes": {"web": {}}}, "web", cmd=["ls"])
        assert ("exec_command", "web", ["ls"], "sh") in runner.calls

    def test_exec_command_for_unknown_box_raises(self):
        session = self._session()
        with pytest.raises(ConfigError, match=r"not a service"):
            session.exec_command_for("services", {"boxes": {"web": {}}}, "nope")

    def test_container_status_parses_ps_json(self):
        session = self._session()
        runner = _FakeRunner()
        runner.ps_json_result = _ok(
            '{"Service":"web","Name":"p-web-1","State":"running",'
            '"Health":"healthy","Publishers":[{"PublishedPort":8080,"TargetPort":80}]}\n'
            '{"Service":"db","Name":"p-db-1","State":"exited","Health":"","Publishers":[]}'
        )
        with self._patch_teardown(session, runner):
            rows = session.container_status("services", {"boxes": {}})
        assert rows[0] == {"service": "web", "name": "p-web-1", "state": "running",
                           "health": "healthy", "ports": "8080->80"}
        assert rows[1]["service"] == "db" and rows[1]["ports"] == ""

    def test_container_status_empty_on_failure(self):
        session = self._session()
        runner = _FakeRunner()
        runner.ps_json_result = _fail()
        with self._patch_teardown(session, runner):
            assert session.container_status("services", {"boxes": {}}) == []

    def test_pause_unpause_cluster(self):
        session = self._session()
        runner = _FakeRunner()
        with self._patch_teardown(session, runner):
            session.pause_cluster("services", {})
            session.unpause_cluster("services", {})
        assert ("pause", None) in runner.calls
        assert ("unpause", None) in runner.calls

    # -- Phase 7: snapshots (docker commit-backed, D3) -----------------
    def _ps_two_containers(self):
        return _ok(
            '{"Service":"web","Name":"p-web-1","State":"running"}\n'
            '{"Service":"cache","Name":"p-cache-1","State":"running"}'
        )

    def test_snapshot_take_commits_and_writes_metadata(self, tmp_path):
        session = self._session()
        runner = _FakeRunner()
        runner.ps_json_result = self._ps_two_containers()
        cfg = {"workdir": str(tmp_path), "boxes": {"web": {}, "cache": {}}}
        with self._patch_teardown(session, runner):
            session.snapshot_take_cluster("services", cfg, "v1", "first")
        # each container committed to boxman/<compose-project>_<box>:v1,
        # where the compose project is <project>_<cluster> (boxman_services)
        commits = [c for c in runner.calls if c[0] == "commit"]
        assert ("commit", "p-web-1", "boxman/boxman_services_web:v1") in commits
        assert ("commit", "p-cache-1", "boxman/boxman_services_cache:v1") in commits
        # metadata written to <workdir>/snapshots.json
        meta = json.loads((tmp_path / "snapshots.json").read_text())
        assert meta["v1"]["description"] == "first"
        assert meta["v1"]["boxes"]["web"] == "boxman/boxman_services_web:v1"

    def test_snapshot_take_no_containers_raises(self, tmp_path):
        session = self._session()
        runner = _FakeRunner()
        runner.ps_json_result = _ok("")  # nothing running
        cfg = {"workdir": str(tmp_path), "boxes": {"web": {}}}
        with self._patch_teardown(session, runner):
            with pytest.raises(ProvisionError, match=r"no containers to snapshot"):
                session.snapshot_take_cluster("services", cfg, "v1")

    def test_snapshot_list_and_delete(self, tmp_path):
        session = self._session()
        cfg = {"workdir": str(tmp_path), "boxes": {"web": {}}}
        session._save_snapshots("services", cfg, {
            "v1": {"created": "t", "boxes": {"web": "boxman/p_web:v1"}}})
        assert "v1" in session.snapshot_list_cluster("services", cfg)
        runner = _FakeRunner()
        with self._patch_teardown(session, runner):
            assert session.snapshot_delete_cluster("services", cfg, "v1") is True
        assert ("image_rm", "boxman/p_web:v1") in runner.calls
        assert session.snapshot_list_cluster("services", cfg) == {}

    def test_snapshot_delete_missing_returns_false(self, tmp_path):
        session = self._session()
        cfg = {"workdir": str(tmp_path), "boxes": {}}
        assert session.snapshot_delete_cluster("services", cfg, "nope") is False

    def test_snapshot_restore_regenerates_with_snapshot_images(self, tmp_path):
        session = self._session()
        cfg = {"workdir": str(tmp_path), "boxes": {"web": {"image": "nginx"}}}
        session._save_snapshots("services", cfg, {
            "v1": {"created": "t", "boxes": {"web": "boxman/p_web:v1"}}})
        fake = _FakeRunner()
        with mock.patch.object(session._generator, "generate",
                               return_value={"services": {}}) as gen, \
             mock.patch.object(session._generator, "write", return_value="/wd/dc.yml"), \
             mock.patch("boxman.providers.docker_compose.session.ComposeRunner",
                        return_value=fake):
            session.snapshot_restore_cluster("services", cfg, "v1")
        # the box image was overridden to the snapshot tag before regeneration
        assert gen.call_args.args[1]["boxes"]["web"]["image"] == "boxman/p_web:v1"
        # and up ran with force_recreate
        assert any(c[0] == "up" and c[2] is True for c in fake.calls)

    def test_snapshot_restore_unknown_raises(self, tmp_path):
        session = self._session()
        cfg = {"workdir": str(tmp_path), "boxes": {}}
        with pytest.raises(ConfigError, match=r"no snapshot 'nope'"):
            session.snapshot_restore_cluster("services", cfg, "nope")

    # -- Phase 7 review fixes -----------------------------------------

    def test_snapshot_take_rejects_duplicate_name(self, tmp_path):
        """A repeat take would re-point tags one box at a time, so a failure
        midway would leave a half-old/half-new snapshot — reject instead."""
        session = self._session()
        cfg = {"workdir": str(tmp_path), "boxes": {"web": {}}}
        session._save_snapshots("services", cfg, {
            "v1": {"created": "2026-01-01", "boxes": {"web": "boxman/x:v1"}}})
        runner = _FakeRunner()
        with self._patch_teardown(session, runner):
            with pytest.raises(ConfigError, match=r"already exists"):
                session.snapshot_take_cluster("services", cfg, "v1")
        assert not [c for c in runner.calls if c[0] == "commit"]  # nothing committed

    def test_snapshot_take_rejects_tag_collision(self, tmp_path):
        """`v:1` and `v-1` sanitize to the same tag — the second take would
        silently overwrite the first snapshot's image."""
        session = self._session()
        cfg = {"workdir": str(tmp_path), "boxes": {"web": {}}}
        session._save_snapshots("services", cfg, {
            "v-1": {"created": "t",
                    "boxes": {"web": "boxman/boxman_services_web:v-1"}}})
        with self._patch_teardown(session, _FakeRunner()):
            with pytest.raises(ConfigError, match=r"same docker tag"):
                session.snapshot_take_cluster("services", cfg, "v:1")

    def test_snapshot_take_rolls_back_partial_commits(self, tmp_path):
        """If a later box fails to commit, images already written for this
        take are removed — otherwise they leak with no metadata to reclaim."""
        session = self._session()
        runner = _FakeRunner()
        runner.ps_json_result = self._ps_two_containers()
        runner.commit_fail_on = "p-cache-1"
        cfg = {"workdir": str(tmp_path), "boxes": {"web": {}, "cache": {}}}
        with self._patch_teardown(session, runner):
            with pytest.raises(ProvisionError, match=r"docker commit failed"):
                session.snapshot_take_cluster("services", cfg, "v1")
        assert ("image_rm", "boxman/boxman_services_web:v1") in runner.calls
        assert not (tmp_path / "snapshots.json").exists()   # no metadata written

    def test_snapshot_take_requires_local_runtime(self, tmp_path):
        session = self._session(runtime="docker-compose")
        with pytest.raises(ConfigError, match=r"requires runtime 'local'"):
            session.snapshot_take_cluster(
                "services", {"workdir": str(tmp_path), "boxes": {}}, "v1")

    def test_snapshot_take_rejects_overlong_name(self, tmp_path):
        session = self._session()
        cfg = {"workdir": str(tmp_path), "boxes": {"web": {}}}
        with self._patch_teardown(session, _FakeRunner()):
            with pytest.raises(ConfigError, match=r"too long"):
                session.snapshot_take_cluster("services", cfg, "x" * 129)

    def test_container_names_raises_on_ps_failure(self, tmp_path):
        """A failed ps must surface, not masquerade as 'no containers'."""
        session = self._session()
        runner = _FakeRunner()
        runner.ps_json_result = _fail(stderr="cannot connect to docker daemon")
        cfg = {"workdir": str(tmp_path), "boxes": {"web": {}}}
        with self._patch_teardown(session, runner):
            with pytest.raises(ProvisionError, match=r"cannot connect to docker daemon"):
                session.snapshot_take_cluster("services", cfg, "v1")

    def test_snapshot_delete_keeps_metadata_when_image_rm_fails(self, tmp_path):
        """`image rm` fails while a restored container holds the image —
        dropping the entry would leak it with nothing left to reclaim it by."""
        session = self._session()
        cfg = {"workdir": str(tmp_path), "boxes": {"web": {}}}
        session._save_snapshots("services", cfg, {
            "v1": {"created": "t", "boxes": {"web": "boxman/p_web:v1"}}})
        runner = _FakeRunner()
        runner.image_rm_result = _fail("image is being used by running container")
        with self._patch_teardown(session, runner):
            assert session.snapshot_delete_cluster("services", cfg, "v1") is False
        assert "v1" in session.snapshot_list_cluster("services", cfg)  # retryable

    def test_validate_snapshot_cluster_flags_missing_image(self, tmp_path):
        """Metadata outlives a pruned image; restore must not start."""
        session = self._session()
        cfg = {"workdir": str(tmp_path), "boxes": {"web": {}}}
        session._save_snapshots("services", cfg, {
            "v1": {"created": "t", "boxes": {"web": "boxman/p_web:v1"}}})
        runner = _FakeRunner()
        runner.image_exists_result = False
        with self._patch_teardown(session, runner):
            valid, errors = session.validate_snapshot_cluster("services", cfg, "v1")
        assert valid is False and "boxman/p_web:v1" in errors[0]
        # and restore refuses rather than starting the destructive recreate
        with self._patch_teardown(session, runner):
            with pytest.raises(ConfigError, match=r"cannot restore snapshot"):
                session.snapshot_restore_cluster("services", cfg, "v1")

    def test_snapshot_resolve_cluster(self, tmp_path):
        session = self._session()
        cfg = {"workdir": str(tmp_path), "boxes": {}}
        assert session.snapshot_resolve_cluster("services", cfg, None) is None
        session._save_snapshots("services", cfg, {
            "old": {"created": "2026-01-01"}, "new": {"created": "2026-02-01"}})
        assert session.snapshot_resolve_cluster("services", cfg, None) == "new"
        assert session.snapshot_resolve_cluster("services", cfg, "old") == "old"

    def test_snapshot_restore_ensures_bind_dirs_and_preflight(self, tmp_path):
        """Restore is a bring-up: same bind-dir precreation as up_cluster, so
        a host dir removed between take and restore isn't created root-owned."""
        session = self._session()
        cfg = {"workdir": str(tmp_path), "boxes": {"web": {"image": "nginx"}}}
        session._save_snapshots("services", cfg, {
            "v1": {"created": "t", "boxes": {"web": "boxman/p_web:v1"}}})
        fake = _FakeRunner()
        with mock.patch.object(session._generator, "generate",
                               return_value={"services": {}}), \
             mock.patch.object(session._generator, "write", return_value="/wd/dc.yml"), \
             mock.patch("boxman.providers.docker_compose.session.ComposeRunner",
                        return_value=fake), \
             mock.patch.object(session, "_ensure_bind_dirs") as binds:
            session.snapshot_restore_cluster("services", cfg, "v1")
        binds.assert_called_once()
        assert ("preflight",) in fake.calls

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

    def compose_project_name(self, name):
        # unique per cluster → no spurious collision in dispatch tests
        return f"proj_{name}"

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


# --------------------------------------------------------------------------
# Phase 7 — snapshot tag helper + manager dc-branch wiring
# --------------------------------------------------------------------------
class TestSnapshotTag:

    def test_lowercases_and_sanitizes(self):
        assert _snapshot_tag("demo_services", "Web", "v1.0") == "boxman/demo_services_web:v1.0"

    def test_replaces_invalid_chars(self):
        tag = _snapshot_tag("demo_services", "my/box", "feat branch")
        assert tag == "boxman/demo_services_my-box:feat-branch"


class TestSnapshotDcWiring:

    def _mgr(self, extra=None):
        import logging
        m = BoxmanManager.__new__(BoxmanManager)
        m.logger = logging.getLogger("boxman")
        m.config = {
            "project": "demo",
            "provider": {"docker-compose": {}},
            "clusters": {"services": {"provider": "docker-compose",
                                      "boxes": {"web": {"image": "x"}}}},
        }
        if extra:
            m.config["clusters"].update(extra)
        return m

    def test_take_routes_to_cluster_session(self):
        m = self._mgr()
        sess = mock.Mock()
        m.session_for_cluster = lambda c: sess
        args = SimpleNamespace(snapshot_name="v1", snapshot_descr="d", cluster=None, vms="all")
        with mock.patch.object(BoxmanManager, "_select_vm_targets", staticmethod(lambda cls, a: [])):
            BoxmanManager.snapshot_take(m, args)
        sess.snapshot_take_cluster.assert_called_once_with("services", m.config["clusters"]["services"], "v1", "d")

    def test_delete_routes_to_cluster_session(self):
        m = self._mgr()
        sess = mock.Mock()
        m.session_for_cluster = lambda c: sess
        args = SimpleNamespace(snapshot_name="v1", cluster=None, vms="all")
        with mock.patch.object(BoxmanManager, "_select_vm_targets", staticmethod(lambda cls, a: [])):
            BoxmanManager.snapshot_delete(m, args)
        sess.snapshot_delete_cluster.assert_called_once_with("services", m.config["clusters"]["services"], "v1")

    @staticmethod
    def _restore_session(latest="new", valid=True, errors=()):
        sess = mock.Mock()
        sess.snapshot_resolve_cluster.return_value = latest
        sess.validate_snapshot_cluster.return_value = (valid, list(errors))
        return sess

    def test_restore_resolves_latest_when_unnamed(self):
        m = self._mgr()
        sess = self._restore_session(latest="new")
        m.session_for_cluster = lambda c: sess
        args = SimpleNamespace(snapshot_name=None, cluster=None, vms="all")
        with mock.patch.object(BoxmanManager, "_select_vm_targets", staticmethod(lambda cls, a: [])):
            BoxmanManager.snapshot_restore(m, args)
        sess.snapshot_resolve_cluster.assert_called_once_with(
            "services", m.config["clusters"]["services"], None)
        sess.snapshot_restore_cluster.assert_called_once_with(
            "services", m.config["clusters"]["services"], "new")

    def test_restore_plan_ensures_shared_bridges_before_compose_up(self):
        """Regression (#85 item 27): after a host reboot the macvlan parent
        bridges are gone; the dc restore plan must recreate them before the
        compose recreate, or it fails with a cryptic "parent interface does
        not exist"."""
        m = self._mgr()
        sess = self._restore_session(latest="new")
        m.session_for_cluster = lambda c: sess
        order = []
        m.ensure_shared_bridges = lambda: order.append("bridges")
        sess.snapshot_restore_cluster.side_effect = (
            lambda *a: order.append("restore"))
        args = SimpleNamespace(snapshot_name=None, cluster=None, vms="all")
        with mock.patch.object(BoxmanManager, "_select_vm_targets", staticmethod(lambda cls, a: [])):
            BoxmanManager.snapshot_restore(m, args)
        assert order == ["bridges", "restore"]

    def test_restore_dc_plan_empty_skips_bridges(self):
        m = self._mgr()
        m.ensure_shared_bridges = mock.Mock()
        assert m._restore_dc_plan([]) == []
        m.ensure_shared_bridges.assert_not_called()

    def test_restore_validates_before_mutating(self):
        """An invalid container snapshot must abort *before* the destructive
        up --force-recreate (mher: partial-restore ordering)."""
        m = self._mgr()
        sess = self._restore_session(valid=False, errors=["image missing: x"])
        m.session_for_cluster = lambda c: sess
        args = SimpleNamespace(snapshot_name="v1", cluster=None, vms="all")
        with mock.patch.object(BoxmanManager, "_select_vm_targets", staticmethod(lambda cls, a: [])):
            BoxmanManager.snapshot_restore(m, args)
        sess.validate_snapshot_cluster.assert_called_once()
        sess.snapshot_restore_cluster.assert_not_called()

    def test_restore_defers_dc_until_vm_prevalidation_passes(self):
        """Mixed project: a bad VM snapshot aborts with containers untouched."""
        m = self._mgr()
        sess = self._restore_session()
        vm_sess = mock.Mock()
        vm_sess.validate_snapshot.return_value = (False, ["corrupt overlay"])
        m.session_for_cluster = lambda c: sess
        m.session_for_vm = lambda v: vm_sess
        args = SimpleNamespace(snapshot_name="v1", cluster=None, vms="all")
        with mock.patch.object(
            BoxmanManager, "_select_vm_targets",
            staticmethod(lambda cls, a: [("node01", "vms", "node01", "/w")]),
        ):
            with pytest.raises(SnapshotError, match="aborting restore"):
                BoxmanManager.snapshot_restore(m, args)
        # dc snapshot was validated, but never recreated
        sess.validate_snapshot_cluster.assert_called_once()
        sess.snapshot_restore_cluster.assert_not_called()

    def test_take_isolates_per_cluster_failure(self):
        """One failing dc cluster must not stop the other, nor the VMs."""
        m = self._mgr({"other": {"provider": "docker-compose", "boxes": {"z": {}}}})
        seen = []

        def _take(cname, *a, **kw):
            seen.append(cname)
            if cname == "services":
                raise ProvisionError("cluster is not up")
            return True

        sess = mock.Mock()
        sess.snapshot_take_cluster.side_effect = _take
        m.session_for_cluster = lambda c: sess
        args = SimpleNamespace(snapshot_name="v1", snapshot_descr="", cluster=None, vms="all")
        with mock.patch.object(BoxmanManager, "_select_vm_targets", staticmethod(lambda cls, a: [])):
            with pytest.raises(SystemExit) as exc:   # non-zero overall
                BoxmanManager.snapshot_take(m, args)
        assert exc.value.code == 1
        assert seen == ["services", "other"]   # kept going after the failure

    def test_restore_failure_exits_nonzero(self):
        m = self._mgr()
        sess = self._restore_session()
        sess.snapshot_restore_cluster.side_effect = ProvisionError("up failed")
        m.session_for_cluster = lambda c: sess
        args = SimpleNamespace(snapshot_name="v1", cluster=None, vms="all")
        with mock.patch.object(BoxmanManager, "_select_vm_targets", staticmethod(lambda cls, a: [])):
            with pytest.raises(SystemExit) as exc:
                BoxmanManager.snapshot_restore(m, args)
        assert exc.value.code == 1

    def test_select_dc_clusters_honors_cluster_filter(self):
        m = self._mgr({"other": {"provider": "docker-compose", "boxes": {"z": {}}}})
        got = dict(m._select_dc_clusters(SimpleNamespace(cluster="other")))
        assert set(got) == {"other"}
        allc = dict(m._select_dc_clusters(SimpleNamespace(cluster=None)))
        assert set(allc) == {"services", "other"}

    def test_narrowed_vms_deselects_dc_clusters(self):
        """`--vms node01` is libvirt-only: containers must be left alone
        rather than force-recreated (mher's --vms scoping question)."""
        m = self._mgr()
        assert m._select_dc_clusters(SimpleNamespace(cluster=None, vms="node01")) == []
        # the default, and an explicit --cluster, still select
        assert dict(m._select_dc_clusters(SimpleNamespace(cluster=None, vms="all")))
        assert dict(m._select_dc_clusters(SimpleNamespace(cluster="services", vms="node01")))


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
        assert set(manager._compose_clusters) == {"svc"}
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
        assert manager._compose_clusters == {}
        # coarse helpers are safe no-ops (no dc session registered)
        manager.provision_compose_clusters()
        manager.destroy_compose_clusters()

    def test_provision_rejects_compose_project_name_collision(self):
        """Two dc clusters whose names sanitize to the same compose project
        (web.api vs web_api) are rejected before any compose state is created
        — teardown's --remove-orphans would otherwise cross-delete."""
        manager = BoxmanManager()
        manager.config = {
            "project": "proj",
            "provider": {"docker-compose": {}},
            "clusters": {
                "web.api": {"provider": "docker-compose",
                            "boxes": {"a": {"image": "x"}}},
                "web_api": {"provider": "docker-compose",
                            "boxes": {"b": {"image": "y"}}},
            },
        }
        session = DockerComposeSession(manager.config)
        session.manager = manager
        manager.register_session("docker-compose", session)
        with pytest.raises(ConfigError, match=r"both map to docker compose project"):
            manager.provision_compose_clusters()

    def test_storage_pool_uses_cluster_libvirt_session_not_dc_default(self, monkeypatch):
        """Finding 2: in a compose-primary mixed project, self.provider is the
        dc session; _ensure_libvirt_storage_pool must build VirshCommand from
        the libvirt cluster's own session config (its URI), not the dc default."""
        from boxman import manager as mgr_mod

        m = BoxmanManager()
        m.config = {
            "project": "proj",
            "provider": {"docker-compose": {}, "libvirt": {"uri": "qemu+ssh://remote/system"}},
            "clusters": {
                "svc": {"provider": "docker-compose", "workdir": "/tmp/dc",
                        "boxes": {"w": {"image": "x"}}},
                "vms": {"provider": "libvirt", "workdir": "/tmp/vm", "vms": {"n": {}}},
            },
        }
        # dc session registered first → becomes the legacy default self.provider
        m.register_session("docker-compose", DockerComposeSession(m.config))
        m.register_session(
            "libvirt", SimpleNamespace(provider_config={"uri": "qemu+ssh://remote/system"}))

        captured = {}

        class _FakeVirsh:
            def __init__(self, provider_config):
                captured["cfg"] = provider_config

            def execute(self, *a, **k):
                return SimpleNamespace(ok=True, stdout="", stderr="")

        monkeypatch.setattr(mgr_mod, "VirshCommand", _FakeVirsh)
        m._ensure_libvirt_storage_pool("/tmp/vm", "vms")
        assert captured["cfg"] == {"uri": "qemu+ssh://remote/system"}

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
        assert set(manager._compose_clusters) == {"svc"}
        manager.provision_compose_clusters()
        assert dc.calls == [("up", "svc")]


# --------------------------------------------------------------------------
# Phase 6 — CLI/UX parity (exec, ps, control, inventory) for dc clusters
# --------------------------------------------------------------------------
class TestPhase6CliParity:

    def _mgr(self, extra_clusters=None):
        import logging
        m = BoxmanManager.__new__(BoxmanManager)
        m.logger = logging.getLogger("boxman")
        m.app_config = {}
        m.config = {
            "project": "demo",
            "provider": {"docker-compose": {}},
            "clusters": {
                "services": {"provider": "docker-compose",
                             "boxes": {"web": {"image": "x"}, "cache": {"image": "y"}}},
            },
        }
        if extra_clusters:
            m.config["clusters"].update(extra_clusters)
        return m

    # -- target resolution --------------------------------------------
    def test_resolve_target_dotted(self):
        assert self._mgr()._resolve_container_target("services.web") == ("services", "web")

    def test_resolve_target_bare_unique(self):
        assert self._mgr()._resolve_container_target("cache") == ("services", "cache")

    def test_resolve_target_libvirt_rejected(self):
        m = self._mgr({"compute": {"provider": "libvirt", "vms": {"node01": {}}}})
        with pytest.raises(ConfigError, match="libvirt"):
            m._resolve_container_target("compute.node01")

    def test_resolve_target_unknown_rejected(self):
        with pytest.raises(ConfigError, match="no docker-compose container"):
            self._mgr()._resolve_container_target("nope")

    def test_compose_project_for(self):
        assert self._mgr()._compose_project_for("services") == "demo_services"

    # -- exec verb -----------------------------------------------------
    def test_exec_container_builds_and_runs(self):
        m = self._mgr()
        sess = mock.Mock()
        sess.exec_command_for.return_value = ["docker", "compose", "exec", "web", "sh"]
        m._dc_session = lambda c: sess
        args = SimpleNamespace(target="services.web", cmd=[], shell=None)
        with mock.patch("subprocess.run",
                        return_value=SimpleNamespace(returncode=0)) as sp:
            BoxmanManager.exec_container(m, args)
        assert sess.exec_command_for.call_args.args[:3] == ("services", m.config["clusters"]["services"], "web")
        # runs the argv LIST with shell=False (no shell=True kwarg)
        assert sp.call_args.args[0] == ["docker", "compose", "exec", "web", "sh"]
        assert sp.call_args.kwargs.get("shell") in (None, False)

    # -- control -------------------------------------------------------
    def test_control_suspend_pauses_dc(self):
        m = self._mgr()
        sess = mock.Mock()
        m.session_for_cluster = lambda c: sess
        m.process_vm_list = lambda a: []
        BoxmanManager.suspend_vm(m, SimpleNamespace())
        sess.pause_cluster.assert_called_once()

    def test_control_resume_unpauses_dc(self):
        m = self._mgr()
        sess = mock.Mock()
        m.session_for_cluster = lambda c: sess
        m.process_vm_list = lambda a: []
        BoxmanManager.resume_vm(m, SimpleNamespace())
        sess.unpause_cluster.assert_called_once()

    def test_control_save_dc_warns_not_raises(self):
        m = self._mgr()
        m.process_vm_list = lambda a: []
        with mock.patch.object(m.logger, "warning") as warn:
            BoxmanManager.save_vm(m, SimpleNamespace())
        assert any("not supported" in str(c.args[0]) for c in warn.call_args_list)

    def test_control_suspend_honors_cluster_scope(self):
        """--cluster scopes the dc control op to that cluster only."""
        m = self._mgr({"other": {"provider": "docker-compose", "boxes": {"z": {}}}})
        paused = []
        sess = mock.Mock()
        sess.pause_cluster.side_effect = lambda name, cfg: paused.append(name)
        m._dc_session = lambda c: sess
        m.process_vm_list = lambda a: []
        BoxmanManager.suspend_vm(m, SimpleNamespace(cluster="other"))
        assert paused == ["other"]      # 'services' left running

    def test_compose_project_for_matches_session(self):
        """The manager's inventory derivation and the session's compose-op
        derivation are single-sourced — they must agree."""
        m = self._mgr()
        session = create_session("docker-compose", m.config)
        assert m._compose_project_for("services") == session._compose_project("services")

    # -- exec parser (regression: --shell after the target) ------------
    def test_exec_parser_shell_after_target(self):
        from boxman.scripts.cli_parser import parse_args
        parser = parse_args()
        a, _ = parser.parse_known_args(["exec", "services.web", "--shell", "bash"])
        assert a.target == "services.web" and a.shell == "bash" and a.cmd == []

    def test_exec_parser_command_after_separator(self):
        from boxman.scripts.cli_parser import parse_args
        parser = parse_args()
        a, _ = parser.parse_known_args(
            ["exec", "services.web", "--", "redis-cli", "ping"])
        assert a.target == "services.web" and a.cmd == ["redis-cli", "ping"]

    # -- ps ------------------------------------------------------------
    def test_ps_includes_containers(self, capsys):
        m = self._mgr()
        sess = mock.Mock()
        sess.container_status.return_value = [
            {"service": "web", "name": "demo_services-web-1", "state": "running",
             "health": "healthy", "ports": ""},
        ]
        m.session_for_cluster = lambda c: sess
        BoxmanManager.ps(m, SimpleNamespace(provider_info=False, json=False))
        out = capsys.readouterr().out
        assert "docker-compose" in out and "web" in out and "running (healthy)" in out

    # -- inventory -----------------------------------------------------
    def test_inventory_includes_dc_containers(self):
        m = self._mgr()
        m.config["workspace"] = {"path": "/tmp/ws"}
        m.resolve_workspace_defaults()
        inv = yaml.safe_load(
            m.config["workspace"]["files"]["inventory/01-hosts.yml"])
        web = inv["all"]["hosts"]["services_web"]
        assert web["ansible_connection"] == "community.docker.docker"
        assert web["ansible_host"] == "demo_services-web-1"
        assert "services" in inv["all"]["children"]


# --------------------------------------------------------------------------
# Flow wiring — pin the compose hooks inside provision/up/down/deprovision/
# destroy (and their ordering vs libvirt/netlab). Without these, deleting a
# hook keeps the rest of the suite green (Finding 18). The compose helpers and
# the libvirt/cache/netlab collaborators are replaced with recorders/no-ops so
# no real docker or libvirt is touched.
# --------------------------------------------------------------------------
class TestFlowWiring:

    def _mgr(self):
        m = BoxmanManager()
        m.config = {
            "project": "proj",
            "provider": {"docker-compose": {}},
            "clusters": {
                "app": {"provider": "docker-compose", "workdir": "/tmp/dc",
                        "boxes": {"w": {"image": "nginx"}}},
            },
        }
        return m

    def _neutralize(self, m, monkeypatch, order):
        """Stub the resource-touching collaborators; record the ordering hooks."""
        m.cache.projects = {}
        monkeypatch.setattr(m.cache, "read_projects_cache", lambda: None)
        # no-op the libvirt/cache/ssh/files collaborators
        for name in [
            "register_project_in_cache", "unregister_from_cache",
            "_expand_oci_base_images",
            "validate_base_images", "provision_files", "deprovision_files",
            "setup_ssh_access", "connect_info",
            "write_ssh_config", "define_networks", "destroy_networks",
        ]:
            monkeypatch.setattr(m, name, lambda *a, **k: None)
        # provision() aborts early when this returns falsy
        monkeypatch.setattr(m, "ensure_templates_exist", lambda *a, **k: True)
        monkeypatch.setattr(m, "_find_existing_project_vms", lambda *a, **k: [])
        monkeypatch.setattr(m, "_get_vm_states", lambda *a, **k: {})
        monkeypatch.setattr(m, "get_connect_info", lambda *a, **k: True)
        # record the ordering-relevant hooks
        monkeypatch.setattr(m, "configure_and_start_vms", lambda: order.append("vms"))
        monkeypatch.setattr(m, "ensure_shared_bridges", lambda: order.append("bridges"))
        monkeypatch.setattr(m, "deploy_netlab", lambda: order.append("netlab"))
        monkeypatch.setattr(m, "ensure_netlab_up", lambda: order.append("netlab_up"))
        monkeypatch.setattr(m, "destroy_netlab", lambda: order.append("netlab_down"))
        for hook in ["provision_compose_clusters", "stop_compose_clusters",
                     "start_compose_clusters", "deprovision_compose_clusters",
                     "destroy_compose_clusters"]:
            monkeypatch.setattr(m, hook, (lambda h: lambda: order.append(h))(hook))

    def test_provision_brings_compose_up_after_vms_before_netlab(self, monkeypatch):
        m = self._mgr()
        order = []
        self._neutralize(m, monkeypatch, order)
        BoxmanManager.provision(m, SimpleNamespace(force=False, rebuild_templates=False))
        assert "provision_compose_clusters" in order
        assert order.index("vms") < order.index("provision_compose_clusters") < order.index("netlab")
        # shared bridges (macvlan parents) must exist before compose comes up
        assert order.index("bridges") < order.index("provision_compose_clusters")

    def test_down_stops_compose_clusters(self, monkeypatch):
        m = self._mgr()
        order = []
        self._neutralize(m, monkeypatch, order)
        BoxmanManager.down(m, SimpleNamespace(suspend=False))
        assert "stop_compose_clusters" in order

    def test_deprovision_tears_down_compose_after_netlab(self, monkeypatch):
        m = self._mgr()
        order = []
        self._neutralize(m, monkeypatch, order)
        BoxmanManager.deprovision(m, SimpleNamespace(cleanup=False))
        assert "deprovision_compose_clusters" in order
        assert order.index("netlab_down") < order.index("deprovision_compose_clusters")

    def test_destroy_destroys_compose_clusters(self, monkeypatch):
        m = self._mgr()
        order = []
        self._neutralize(m, monkeypatch, order)
        m.cache.projects = {"proj": {"conf": "x", "runtime": "local"}}
        monkeypatch.setattr(m, "_force_rmtree", lambda *a, **k: None)
        BoxmanManager.destroy(m, SimpleNamespace(auto_accept=True, templates=False))
        assert "destroy_compose_clusters" in order

    def test_up_dc_only_first_run_routes_through_provision(self, monkeypatch):
        m = self._mgr()
        order = []
        self._neutralize(m, monkeypatch, order)
        monkeypatch.setattr(m, "_get_project_vm_names", lambda *a, **k: [])
        # project not in cache → first run → full provision()
        monkeypatch.setattr(m, "provision", lambda *a, **k: order.append("provision"))
        BoxmanManager.up(m, SimpleNamespace(force=False))
        assert order == ["provision"]

    def test_up_dc_only_reconcile_ensures_bridges_before_compose_up(self, monkeypatch):
        m = self._mgr()
        order = []
        self._neutralize(m, monkeypatch, order)
        monkeypatch.setattr(m, "_get_project_vm_names", lambda *a, **k: [])
        m.cache.projects = {"proj": {"conf": "x", "runtime": "local"}}  # already provisioned
        BoxmanManager.up(m, SimpleNamespace(force=False))
        # a host reboot drops the non-persistent bridge; recreate it before
        # the macvlan-attached containers reconcile.
        assert order == ["bridges", "provision_compose_clusters"]

    def test_up_all_vms_running_reconciles_compose(self, monkeypatch):
        m = self._mgr()
        order = []
        self._neutralize(m, monkeypatch, order)
        # simulate a mixed project whose libvirt VMs are all already running
        monkeypatch.setattr(m, "_get_project_vm_names", lambda *a, **k: ["vm1"])
        monkeypatch.setattr(m, "_get_vm_states", lambda *a, **k: {"vm1": "running"})
        BoxmanManager.up(m, SimpleNamespace(force=False))
        assert "provision_compose_clusters" in order
        assert order.index("netlab_up") < order.index("provision_compose_clusters")
        # bridges reconciled before compose on the hybrid all-running path too
        assert order.index("bridges") < order.index("provision_compose_clusters")
