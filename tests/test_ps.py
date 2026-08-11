"""
Regression tests for ``boxman ps`` config/runtime handling (#85 item 7).

ps used to construct ``BoxmanManager(config=args.conf)`` without loading
boxman.yml and without setting the runtime, and built its VirshCommand
from only the project provider block — so app-level settings (uri, sudo
lists) and the docker-compose runtime were ignored. ps now goes through
the same load/merge/inject path as the other verbs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from boxman.scripts.app import main

pytestmark = pytest.mark.unit


PROJECT_CONFIG = {
    'project': 'demo',
    'clusters': {
        'cluster_1': {
            'workdir': '/tmp/boxman-test-ps',
            'vms': {'node01': {}},
        },
    },
}

APP_CONFIG_LOCAL = {
    'runtime': 'local',
    'providers': {
        'libvirt': {
            'uri': 'qemu+ssh://hv.example/system',
            'use_sudo': True,
        },
    },
}

APP_CONFIG_DC_RUNTIME = {
    'runtime': 'docker-compose',
    'providers': {
        'libvirt': {
            'uri': 'qemu+ssh://hv.example/system',
        },
    },
}


def _run_cli(argv: list[str]) -> int:
    """Invoke ``main()`` with *argv``, returning the exit code."""
    with patch.object(sys, "argv", ["boxman"] + argv):
        try:
            main()
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 0


@pytest.fixture
def conf_yml(tmp_path: Path) -> Path:
    path = tmp_path / "conf.yml"
    path.write_text(yaml.safe_dump(PROJECT_CONFIG))
    return path


def _empty_virsh_list() -> MagicMock:
    r = MagicMock(name="virsh.Result")
    r.stdout = " Id   Name   State\n------------------\n"
    r.ok = True
    return r


def _run_ps(conf_yml: Path, app_config: dict) -> dict:
    """Run ``boxman ps`` with mocked boxman.yml; return the provider_config
    the VirshCommand was built with."""
    captured: dict = {}
    virsh = MagicMock()
    virsh.execute.return_value = _empty_virsh_list()

    def fake_virsh(provider_config=None, **_kwargs):
        captured["provider_config"] = provider_config
        return virsh

    with patch("boxman.manager.BoxmanCache"), \
         patch("boxman.scripts.app.load_boxman_config",
               return_value=app_config), \
         patch("boxman.manager.VirshCommand", side_effect=fake_virsh):
        code = _run_cli(["--conf", str(conf_yml), "ps"])

    assert code == 0
    return captured


class TestPsHonorsBoxmanYml:

    def test_ps_uses_app_level_uri(self, conf_yml: Path):
        captured = _run_ps(conf_yml, APP_CONFIG_LOCAL)
        provider_config = captured["provider_config"]
        assert provider_config["uri"] == "qemu+ssh://hv.example/system"
        assert provider_config["use_sudo"] is True
        assert provider_config["runtime"] == "local"


class TestPsDockerComposeRuntime:

    def test_ps_targets_the_runtime_container(self, conf_yml: Path):
        captured = _run_ps(conf_yml, APP_CONFIG_DC_RUNTIME)
        provider_config = captured["provider_config"]
        assert provider_config["runtime"] == "docker-compose"
        # the container is scoped to the project name from conf.yml
        assert provider_config["runtime_container"] == "boxman-libvirt-demo"
        # boxman.yml uri still applies inside the container
        assert provider_config["uri"] == "qemu+ssh://hv.example/system"
