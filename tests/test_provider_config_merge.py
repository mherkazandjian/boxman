"""
Regression tests for the unified provider-config merge (#85 item 8).

``boxman.providers.merge_provider_configs`` is now the single merge used
by the CLI session-creation path, ``boxman conf`` (show_conf) and the
manager's one-off virsh probes. These tests drive the real app.py paths
(with boxman.yml / create_session / the verb itself mocked out) and assert
the sudo-list union/eviction semantics end to end:

  - a project-level ``sudo_skip_commands`` entry evicts the command from
    the app-level ``force_sudo_commands`` list (previously the CLI path
    wholesale-replaced the app list with the project list, or vice versa);
  - commands that only appear in app-level lists are preserved;
  - scalar project keys win, app-level scalars fill in.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from boxman.scripts.app import main

pytestmark = pytest.mark.unit


APP_CONFIG = {
    'runtime': 'local',
    'providers': {
        'libvirt': {
            'uri': 'qemu+ssh://hypervisor.example/system',
            'use_sudo': False,
            'force_sudo_commands': ['virsh', 'qemu-img'],
            'sudo_skip_commands': ['ls'],
        },
    },
}

PROJECT_CONFIG = {
    'project': 'demo',
    'provider': {
        'libvirt': {
            'use_sudo': True,
            'sudo_skip_commands': ['virsh'],
        },
    },
    'clusters': {
        'cluster_1': {
            'workdir': '/tmp/boxman-test-merge',
            'vms': {'node01': {}},
        },
    },
}


def _run_cli(argv: list[str]) -> int:
    """Invoke ``main()`` with *argv*, returning the exit code."""
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


class TestSessionCreationMerge:
    """The provision-path session must receive the sudo-list-aware merge."""

    def test_session_config_uses_unified_merge(self, conf_yml: Path):
        captured: dict[str, dict] = {}

        def fake_create_session(provider_type, config):
            captured[provider_type] = config
            return MagicMock(name=f"session-{provider_type}")

        with patch("boxman.manager.BoxmanCache"), \
             patch("boxman.scripts.app.load_boxman_config",
                   return_value=APP_CONFIG), \
             patch("boxman.scripts.app.create_session",
                   side_effect=fake_create_session), \
             patch("boxman.manager.BoxmanManager.provision",
                   return_value=None):
            code = _run_cli(["--conf", str(conf_yml), "provision"])

        assert code == 0
        merged = captured["libvirt"]["provider"]["libvirt"]

        # eviction: project skips 'virsh' -> gone from the app force list
        assert merged["force_sudo_commands"] == ["qemu-img"]
        # union: app-level 'ls' preserved, project-level 'virsh' added
        assert merged["sudo_skip_commands"] == ["ls", "virsh"]
        # scalars: project wins, app fills in
        assert merged["use_sudo"] is True
        assert merged["uri"] == "qemu+ssh://hypervisor.example/system"
        # runtime metadata was injected before the merge
        assert merged["runtime"] == "local"


class TestShowConfMerge:
    """``boxman conf`` must report the same merged provider config."""

    def test_show_conf_uses_unified_merge(self, conf_yml: Path):
        captured: dict = {}

        def fake_show_conf(args, merged_provider=None):
            captured["merged_provider"] = merged_provider

        with patch("boxman.manager.BoxmanCache"), \
             patch("boxman.scripts.app.load_boxman_config",
                   return_value=APP_CONFIG), \
             patch("boxman.manager.BoxmanManager.show_conf",
                   side_effect=fake_show_conf):
            code = _run_cli(["--conf", str(conf_yml), "conf"])

        assert code == 0
        merged = captured["merged_provider"]
        assert merged["force_sudo_commands"] == ["qemu-img"]
        assert merged["sudo_skip_commands"] == ["ls", "virsh"]
        assert merged["use_sudo"] is True
        assert merged["uri"] == "qemu+ssh://hypervisor.example/system"


DC_APP_CONFIG = {
    'runtime': 'local',
    'providers': {
        'docker-compose': {
            'project_name': 'app_level_default',
            'compose_cmd': 'docker compose',
        },
    },
}

DC_PROJECT_CONFIG = {
    'version': '2.0',
    'project': 'dc_demo',
    'provider': {
        'docker-compose': {
            'project_name': 'dc_demo',
        },
    },
    'clusters': {
        'web': {
            'provider': 'docker-compose',
            'workdir': '/tmp/boxman-test-dc-merge',
        },
    },
}


class TestNonLibvirtSessionMerge:
    """App-level provider settings must reach non-libvirt sessions too
    (#85 item 10): previously the app->runtime->project merge only ran
    under ``if _ptype == 'libvirt'``, so ``providers.docker-compose.*``
    in boxman.yml was silently ignored."""

    def test_docker_compose_session_sees_app_level_keys(self, tmp_path: Path):
        conf_yml = tmp_path / "conf.yml"
        conf_yml.write_text(yaml.safe_dump(DC_PROJECT_CONFIG))
        captured: dict[str, dict] = {}

        def fake_create_session(provider_type, config):
            captured[provider_type] = config
            return MagicMock(name=f"session-{provider_type}")

        with patch("boxman.manager.BoxmanCache"), \
             patch("boxman.scripts.app.load_boxman_config",
                   return_value=DC_APP_CONFIG), \
             patch("boxman.scripts.app.create_session",
                   side_effect=fake_create_session), \
             patch("boxman.manager.BoxmanManager.provision",
                   return_value=None):
            code = _run_cli(["--conf", str(conf_yml), "provision"])

        assert code == 0
        merged = captured["docker-compose"]["provider"]["docker-compose"]
        # app-level keys are no longer dropped
        assert merged["compose_cmd"] == "docker compose"
        # project-level scalar still wins
        assert merged["project_name"] == "dc_demo"
        # runtime metadata was injected
        assert merged["runtime"] == "local"
