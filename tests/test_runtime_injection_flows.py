"""
Regression tests for runtime injection in snapshot/control verb flows
(#85 item 9a, manager half).

``update_provider_config_with_runtime()`` was only applied in ~5 of the
verb flows; snapshot_take/list/restore/delete and the control verbs
(suspend/resume/save/start) ran their provider commands with a session
config that never saw the runtime — under the docker-compose runtime
that meant virsh ran on the local host instead of inside the container.

Every flow now calls ``BoxmanManager._update_sessions_with_runtime()``
first; these tests drive each verb with all targets mocked out and
assert the session's provider config picked up the runtime metadata
(``runtime`` / ``runtime_container`` — the keys LibVirtCommandBase uses
to wrap commands with ``docker exec``).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from boxman.manager import BoxmanManager
from boxman.providers.libvirt.session import LibVirtSession

pytestmark = pytest.mark.unit


def _mgr_with_libvirt_session() -> tuple[BoxmanManager, LibVirtSession]:
    """A manager with one registered libvirt session, running under the
    docker-compose runtime, with the session config NOT yet enriched."""
    with patch("boxman.manager.BoxmanCache"):
        m = BoxmanManager()
    m.config = {
        'project': 'demo',
        'clusters': {
            'cluster_1': {'workdir': '/tmp/x', 'vms': {'node01': {}}},
        },
    }
    m.app_config = {'runtime_config': {}}
    m.runtime = 'docker-compose'
    m.runtime_instance.project_name = 'demo'

    session = LibVirtSession(
        config={'provider': {'libvirt': {'uri': 'qemu:///system'}}})
    session.manager = m
    m.register_session('libvirt', session)
    assert 'runtime' not in session.provider_config  # not yet injected
    return m, session


def _cli_args(**overrides) -> MagicMock:
    args = MagicMock(name="cli_args")
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


#: (verb, extra cli_args) pairs — every flow that gained the injection
FLOWS = [
    (BoxmanManager.snapshot_list, {}),
    (BoxmanManager.snapshot_take, {'snapshot_name': 's1'}),
    (BoxmanManager.snapshot_restore, {'snapshot_name': 's1'}),
    (BoxmanManager.snapshot_delete, {'snapshot_name': 's1'}),
    (BoxmanManager.suspend_vm, {}),
    (BoxmanManager.resume_vm, {}),
    (BoxmanManager.save_vm, {}),
    (BoxmanManager.start_vm, {'restore': False}),
]


class TestRuntimeInjectionInVerbFlows:

    @pytest.mark.parametrize("verb,extra", FLOWS,
                             ids=[v.__name__ for v, _ in FLOWS])
    def test_verb_injects_runtime_into_session_config(self, verb, extra):
        m, session = _mgr_with_libvirt_session()

        with patch.object(BoxmanManager, '_select_vm_targets',
                          return_value=[]), \
             patch.object(BoxmanManager, '_select_dc_clusters',
                          return_value=[]), \
             patch.object(BoxmanManager, '_for_each_dc_cluster',
                          return_value=([], [])), \
             patch.object(BoxmanManager, '_control_vm_targets',
                          return_value=[]):
            verb(m, _cli_args(**extra))

        # the keys LibVirtCommandBase uses to wrap commands with
        # ``docker exec --user root <container> bash -c ...``
        assert session.provider_config['runtime'] == 'docker-compose'
        assert session.provider_config['runtime_container'] == \
            'boxman-libvirt-demo'
        # the session's own keys survive the injection
        assert session.provider_config['uri'] == 'qemu:///system'
