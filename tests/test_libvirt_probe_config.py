"""
Regression tests for libvirt-only virsh probes in mixed projects
(#85 item 11).

``_find_existing_project_vms`` / ``_find_all_existing_project_vms`` /
``_get_vm_states`` / ``_create_templates_impl`` used to build their
one-off ``VirshCommand`` from ``primary_provider_type(config)`` — the
first key of the ``provider:`` block. A mixed project listing
docker-compose first made these helpers interrogate the wrong
hypervisor. They now resolve the ``libvirt`` block explicitly and skip
entirely when the project has no libvirt clusters.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit


MIXED_DC_FIRST_CONFIG = {
    'project': 'mixed',
    'provider': {
        'docker-compose': {'project_name': 'should-not-leak'},
        'libvirt': {'uri': 'qemu+ssh://hv.example/system'},
    },
    'clusters': {
        'web': {'provider': 'docker-compose', 'workdir': '/tmp/x'},
        'compute': {
            'provider': 'libvirt',
            'workdir': '/tmp/y',
            'vms': {'node01': {}},
        },
    },
}

DC_ONLY_CONFIG = {
    'version': '2.0',
    'project': 'dconly',
    'provider': {
        'docker-compose': {'project_name': 'dconly'},
    },
    'clusters': {
        'web': {'provider': 'docker-compose', 'workdir': '/tmp/x'},
    },
}

APP_CONFIG = {
    'providers': {
        'libvirt': {'use_sudo': True},
        'docker-compose': {'compose_cmd': 'docker compose'},
    },
}


@pytest.fixture
def mgr() -> BoxmanManager:
    with patch("boxman.manager.BoxmanCache"):
        m = BoxmanManager()
    m.app_config = APP_CONFIG
    return m


def _virsh_result(stdout: str = "", ok: bool = True) -> MagicMock:
    r = MagicMock(name="virsh.Result")
    r.stdout = stdout
    r.ok = ok
    return r


class TestMixedProjectUsesLibvirtBlock:

    def test_find_existing_project_vms_uses_libvirt_config(
        self, mgr: BoxmanManager
    ):
        mgr.config = MIXED_DC_FIRST_CONFIG
        virsh = MagicMock()
        virsh.execute.return_value = _virsh_result(
            "bprj__mixed__bprj_compute_node01\n")

        with patch("boxman.manager.VirshCommand", return_value=virsh) as ctor:
            found = mgr._find_existing_project_vms()

        assert found == ["bprj__mixed__bprj_compute_node01"]
        provider_config = ctor.call_args.kwargs["provider_config"]
        # the libvirt block was used, not the first (docker-compose) key
        assert provider_config["uri"] == "qemu+ssh://hv.example/system"
        assert "project_name" not in provider_config
        # app-level libvirt defaults were merged in
        assert provider_config["use_sudo"] is True

    def test_get_vm_states_uses_libvirt_config(self, mgr: BoxmanManager):
        mgr.config = MIXED_DC_FIRST_CONFIG
        virsh = MagicMock()
        virsh.execute.return_value = _virsh_result(
            " Id   Name                              State\n"
            "----------------------------------------------\n"
            " 3    bprj__mixed__bprj_compute_node01   running\n")

        with patch("boxman.manager.VirshCommand", return_value=virsh) as ctor:
            states = mgr._get_vm_states()

        assert states == {"bprj__mixed__bprj_compute_node01": "running"}
        provider_config = ctor.call_args.kwargs["provider_config"]
        assert provider_config["uri"] == "qemu+ssh://hv.example/system"

    def test_create_templates_probe_uses_libvirt_config(
        self, mgr: BoxmanManager, tmp_path: Path
    ):
        config = dict(MIXED_DC_FIRST_CONFIG)
        config["templates"] = {"tpl1": {"name": "rocky-template"}}
        mgr.config = config
        virsh = MagicMock()
        # the template already exists -> early return before any build
        virsh.execute.return_value = _virsh_result("rocky-template\n")

        # the pre-check probe builds its VirshCommand from boxman.manager's
        # top-level import (via _libvirt_provider_config)
        with patch("boxman.manager.VirshCommand",
                   return_value=virsh) as ctor, \
             patch("boxman.providers.libvirt.cloudinit.CloudInitTemplate") as tpl:
            failed = mgr._create_templates_impl()

        assert failed == ["tpl1"]
        tpl.assert_not_called()
        provider_config = ctor.call_args.kwargs["provider_config"]
        assert provider_config["uri"] == "qemu+ssh://hv.example/system"
        assert provider_config["use_sudo"] is True


class TestDcOnlyProjectSkipsProbes:

    def test_find_existing_project_vms_skips_virsh(self, mgr: BoxmanManager):
        mgr.config = DC_ONLY_CONFIG
        with patch("boxman.manager.VirshCommand") as ctor:
            assert mgr._find_existing_project_vms() == []
        ctor.assert_not_called()

    def test_find_all_existing_project_vms_skips_virsh(
        self, mgr: BoxmanManager
    ):
        mgr.config = DC_ONLY_CONFIG
        with patch("boxman.manager.VirshCommand") as ctor:
            assert mgr._find_all_existing_project_vms() == []
        ctor.assert_not_called()

    def test_get_vm_states_skips_virsh(self, mgr: BoxmanManager):
        mgr.config = DC_ONLY_CONFIG
        with patch("boxman.manager.VirshCommand") as ctor:
            assert mgr._get_vm_states() == {}
        ctor.assert_not_called()
