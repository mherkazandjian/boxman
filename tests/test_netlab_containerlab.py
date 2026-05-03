"""Unit tests for boxman.netlab.containerlab.ContainerlabManager.

Covers preflight detection, topology rendering (Jinja2 startup-config
pass-through, file layout), CLI shell-outs for deploy/destroy/inspect,
and ssh_command helper. No actual ``containerlab`` binary required.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from boxman.netlab.containerlab import (
    ContainerlabManager,
    ContainerlabNotInstalled,
)
from boxman.utils.jinja_env import create_jinja_env


pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.ok = ok
    r.failed = not ok
    return r


@pytest.fixture
def simple_lab_config():
    return {
        "enabled": True,
        "lab_name": "netlab",
        "topology": {
            "nodes": {
                "sw1": {
                    "kind": "arista_ceos",
                    "image": "ceos:4.32.0F",
                },
                "r1": {
                    "kind": "cisco_iol",
                    "image": "vrnetlab/vr-cisco_iol:17.12.1",
                },
            },
            "links": [
                {"endpoints": ["r1:eth1", "sw1:eth1"]},
                {"endpoints": ["sw1:eth2", "host:shared_lab_mgmt"]},
            ],
        },
    }


class TestPreflight:

    def test_passes_when_both_binaries_present(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        with patch("shutil.which", return_value="/usr/bin/fake"):
            mgr.preflight()  # no raise

    def test_raises_when_containerlab_missing(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        def which(name):
            return None if name == "containerlab" else "/usr/bin/docker"
        with patch("shutil.which", side_effect=which):
            with pytest.raises(ContainerlabNotInstalled, match="containerlab"):
                mgr.preflight()

    def test_raises_when_docker_missing(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        def which(name):
            return "/usr/bin/containerlab" if name == "containerlab" else None
        with patch("shutil.which", side_effect=which):
            with pytest.raises(ContainerlabNotInstalled, match="docker"):
                mgr.preflight()

    def test_error_includes_install_hint(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        with patch("shutil.which", return_value=None):
            with pytest.raises(ContainerlabNotInstalled) as exc_info:
                mgr.preflight()
        assert "get.containerlab.dev" in str(exc_info.value)


class TestLabNameAndPaths:

    def test_lab_name_required(self, tmp_path):
        mgr = ContainerlabManager({}, tmp_path)
        with pytest.raises(ValueError, match="lab_name is required"):
            _ = mgr.lab_name

    def test_topology_path_under_netlab_dir(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        assert mgr.topology_path == tmp_path / "netlab" / "netlab.clab.yml"

    def test_enabled_defaults_true(self, simple_lab_config, tmp_path):
        simple_lab_config.pop("enabled", None)
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        assert mgr.enabled is True

    def test_enabled_false_respected(self, simple_lab_config, tmp_path):
        simple_lab_config["enabled"] = False
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        assert mgr.enabled is False


class TestRenderTopology:

    def test_emits_valid_clab_yaml_with_expected_shape(
            self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        path = mgr.render_topology()

        assert path.exists()
        rendered = yaml.safe_load(path.read_text())
        assert rendered["name"] == "netlab"
        assert "sw1" in rendered["topology"]["nodes"]
        assert "r1" in rendered["topology"]["nodes"]
        assert rendered["topology"]["nodes"]["sw1"]["kind"] == "arista_ceos"
        assert len(rendered["topology"]["links"]) == 2

    def test_host_endpoint_passes_through(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        path = mgr.render_topology()
        rendered = yaml.safe_load(path.read_text())
        endpoints = rendered["topology"]["links"][1]["endpoints"]
        assert endpoints == ["sw1:eth2", "host:shared_lab_mgmt"]

    def test_non_jinja_startup_config_unchanged(self, simple_lab_config, tmp_path):
        simple_lab_config["topology"]["nodes"]["r1"]["startup-config"] = (
            "configs/r1.cfg"
        )
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        path = mgr.render_topology()
        rendered = yaml.safe_load(path.read_text())
        assert rendered["topology"]["nodes"]["r1"]["startup-config"] == (
            "configs/r1.cfg"
        )

    def test_jinja_startup_config_rendered_and_rewritten(self, tmp_path):
        src = tmp_path / "configs"
        src.mkdir()
        (src / "r1.cfg.j2").write_text(
            "hostname r1\n"
            "! rendered-by={{ env('RENDERED_BY', default='boxman') }}\n"
        )

        lab_config = {
            "lab_name": "netlab",
            "topology": {
                "nodes": {
                    "r1": {
                        "kind": "cisco_iol",
                        "startup-config": "configs/r1.cfg.j2",
                    },
                },
            },
        }
        jinja_env = create_jinja_env(str(tmp_path))
        mgr = ContainerlabManager(lab_config, tmp_path, jinja_env=jinja_env)
        mgr.render_topology()

        rendered_yaml = yaml.safe_load(mgr.topology_path.read_text())
        rendered_cfg_path = Path(
            rendered_yaml["topology"]["nodes"]["r1"]["startup-config"]
        )
        assert rendered_cfg_path.exists()
        assert rendered_cfg_path.name == "r1.cfg"  # .j2 suffix stripped
        content = rendered_cfg_path.read_text()
        assert "hostname r1" in content
        assert "rendered-by=boxman" in content

    def test_j2_without_jinja_env_raises(self, tmp_path):
        lab_config = {
            "lab_name": "netlab",
            "topology": {
                "nodes": {
                    "r1": {"startup-config": "configs/r1.cfg.j2"},
                },
            },
        }
        mgr = ContainerlabManager(lab_config, tmp_path, jinja_env=None)
        with pytest.raises(RuntimeError, match="no jinja_env was provided"):
            mgr.render_topology()

    def test_missing_template_raises(self, tmp_path):
        lab_config = {
            "lab_name": "netlab",
            "topology": {
                "nodes": {
                    "r1": {"startup-config": "configs/does_not_exist.cfg.j2"},
                },
            },
        }
        jinja_env = create_jinja_env(str(tmp_path))
        mgr = ContainerlabManager(lab_config, tmp_path, jinja_env=jinja_env)
        with pytest.raises(FileNotFoundError):
            mgr.render_topology()

    def test_passthrough_top_level_keys(self, simple_lab_config, tmp_path):
        simple_lab_config["prefix"] = "myco"
        simple_lab_config["mgmt"] = {"network": "mymgmt", "ipv4-subnet": "172.20.20.0/24"}
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        mgr.render_topology()
        rendered = yaml.safe_load(mgr.topology_path.read_text())
        assert rendered["prefix"] == "myco"
        assert rendered["mgmt"]["network"] == "mymgmt"


class TestDeployDestroyInspect:

    def test_deploy_requires_rendered_topology(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        with pytest.raises(FileNotFoundError, match="did you call render_topology"):
            mgr.deploy()

    def test_deploy_shells_out(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        mgr.render_topology()
        with patch("boxman.netlab.containerlab.run",
                   return_value=_result()) as run:
            mgr.deploy()
        assert run.call_count == 1
        cmd = run.call_args.args[0]
        assert "containerlab deploy -t" in cmd
        assert str(mgr.topology_path) in cmd

    def test_destroy_uses_topology_when_present(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        mgr.render_topology()
        with patch("boxman.netlab.containerlab.run",
                   return_value=_result()) as run:
            mgr.destroy()
        cmd = run.call_args.args[0]
        assert "containerlab destroy -t" in cmd
        assert "--cleanup" in cmd

    def test_destroy_falls_back_to_name(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        with patch("boxman.netlab.containerlab.run",
                   return_value=_result()) as run:
            mgr.destroy()
        cmd = run.call_args.args[0]
        assert "containerlab destroy --name netlab" in cmd
        assert "--cleanup" in cmd

    def test_inspect_parses_json(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        sample = {"containers": [{"name": "clab-netlab-sw1", "state": "running"}]}
        with patch("boxman.netlab.containerlab.run",
                   return_value=_result(stdout=json.dumps(sample))):
            assert mgr.inspect() == sample

    def test_inspect_returns_empty_on_failure(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        with patch("boxman.netlab.containerlab.run",
                   return_value=_result(stdout="", ok=False)):
            assert mgr.inspect() == {}

    def test_inspect_returns_empty_on_non_json(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        with patch("boxman.netlab.containerlab.run",
                   return_value=_result(stdout="not json at all", ok=True)):
            assert mgr.inspect() == {}


class TestEnsureUp:
    """Idempotent reconciliation invoked by `boxman up`."""

    def test_no_containers_triggers_deploy(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        calls: list[str] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            # docker ps returns empty -> nothing deployed
            if "docker ps" in cmd:
                return _result(stdout="", ok=True)
            return _result(ok=True)

        with patch("boxman.netlab.containerlab.run", side_effect=fake_run):
            mgr.ensure_up()

        joined = " | ".join(calls)
        assert "docker ps" in joined
        assert "containerlab deploy" in joined
        # Topology should have been rendered on the way to deploy.
        assert mgr.topology_path.exists()

    def test_all_running_is_noop(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)

        docker_out = (
            "clab-netlab-sw1 running\n"
            "clab-netlab-r1 running\n"
        )

        def fake_run(cmd, **kwargs):
            if "docker ps" in cmd:
                return _result(stdout=docker_out, ok=True)
            return _result(ok=True)

        with patch("boxman.netlab.containerlab.run", side_effect=fake_run) as run:
            mgr.ensure_up()

        cmds = [c.args[0] for c in run.call_args_list]
        assert any("docker ps" in c for c in cmds)
        assert not any("containerlab deploy" in c for c in cmds)
        assert not any("docker start" in c for c in cmds)

    def test_some_stopped_triggers_docker_start(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)

        docker_out = (
            "clab-netlab-sw1 running\n"
            "clab-netlab-r1 exited\n"
        )

        def fake_run(cmd, **kwargs):
            if "docker ps" in cmd:
                return _result(stdout=docker_out, ok=True)
            return _result(ok=True)

        with patch("boxman.netlab.containerlab.run", side_effect=fake_run) as run:
            mgr.ensure_up()

        cmds = [c.args[0] for c in run.call_args_list]
        assert any("docker start clab-netlab-r1" in c for c in cmds)
        assert not any("docker start clab-netlab-sw1" in c for c in cmds)
        assert not any("containerlab deploy" in c for c in cmds)


class TestSshCommand:

    def test_default_user_admin(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        assert mgr.ssh_command("sw1") == "ssh admin@clab-netlab-sw1"

    def test_login_user_from_topology_used(self, simple_lab_config, tmp_path):
        simple_lab_config["topology"]["nodes"]["sw1"]["login-user"] = "cumulus"
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        assert mgr.ssh_command("sw1") == "ssh cumulus@clab-netlab-sw1"

    def test_explicit_user_overrides(self, simple_lab_config, tmp_path):
        simple_lab_config["topology"]["nodes"]["sw1"]["login-user"] = "cumulus"
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        assert mgr.ssh_command("sw1", user="root") == "ssh root@clab-netlab-sw1"

    def test_unknown_node_raises(self, simple_lab_config, tmp_path):
        mgr = ContainerlabManager(simple_lab_config, tmp_path)
        with pytest.raises(KeyError, match="not declared"):
            mgr.ssh_command("ghost")
