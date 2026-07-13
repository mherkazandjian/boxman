"""
Unit tests for the provider registry and per-cluster session resolution.

Phase 1 of the docker-compose provider epic
(https://github.com/mherkazandjian/boxman/issues/49).

The registry (``boxman.providers``) maps provider-type names to session
factories; ``BoxmanManager.session_for_cluster`` resolves the session
that manages a given cluster. These tests pin:

- the factory surface (known types, friendly errors for unknown /
  not-yet-implemented types),
- the ``primary_provider_type`` default rules,
- per-cluster resolution semantics, including the legacy
  ``manager.provider`` setter compat shim that older call sites and
  tests rely on.
"""

from __future__ import annotations

import pytest

from boxman.abstract.providers import ProviderSession
from boxman.manager import BoxmanManager
from boxman.providers import PROVIDERS, create_session, primary_provider_type
from boxman.providers.libvirt.session import LibVirtSession

pytestmark = pytest.mark.unit


class TestCreateSession:

    def test_libvirt_factory_returns_protocol_satisfying_session(self):
        session = create_session('libvirt', {"provider": {"libvirt": {}}})
        assert isinstance(session, LibVirtSession)
        assert isinstance(session, ProviderSession)

    def test_virtualbox_factory_wiring(self, monkeypatch):
        """The Virtualbox constructor shells out to ``vboxmanage`` at
        init, so wiring is verified against a stand-in class instead of
        requiring the binary on the test host."""
        created_with: list = []

        class FakeVirtualbox:
            def __init__(self, conf):
                created_with.append(conf)

        import boxman.virtualbox.vboxmanage as vboxmanage
        monkeypatch.setattr(vboxmanage, 'Virtualbox', FakeVirtualbox)

        conf = {"provider": {"virtualbox": {}}}
        session = create_session('virtualbox', conf)
        assert isinstance(session, FakeVirtualbox)
        assert created_with == [conf]

    def test_docker_compose_is_a_friendly_not_implemented(self):
        with pytest.raises(NotImplementedError, match=r"Phase 3"):
            create_session('docker-compose', {})

    def test_unknown_provider_lists_supported_types(self):
        with pytest.raises(ValueError) as excinfo:
            create_session('warpdrive', {})
        message = str(excinfo.value)
        assert "warpdrive" in message
        for known in ('docker-compose', 'libvirt', 'virtualbox'):
            assert known in message

    def test_registry_covers_expected_types(self):
        assert {'libvirt', 'virtualbox', 'docker-compose'} == set(PROVIDERS)


class TestPrimaryProviderType:

    def test_first_provider_key_wins(self):
        assert primary_provider_type({'provider': {'libvirt': {}}}) == 'libvirt'

    def test_defaults_to_libvirt_without_provider_section(self):
        assert primary_provider_type({}) == 'libvirt'
        assert primary_provider_type(None) == 'libvirt'


class TestPerClusterResolution:

    @staticmethod
    def _manager(config):
        manager = BoxmanManager()
        manager.config = config
        return manager

    def test_two_clusters_same_provider_share_one_session(self):
        """AC: two-libvirt-cluster config — both clusters resolve to the
        same registered session, which is also the legacy default."""
        manager = self._manager({
            'project': 'proj',
            'provider': {'libvirt': {}},
            'clusters': {'cluster_a': {}, 'cluster_b': {}},
        })
        session = LibVirtSession(config={"provider": {"libvirt": {}}})
        manager.register_session('libvirt', session)

        assert manager.session_for_cluster('cluster_a') is session
        assert manager.session_for_cluster('cluster_b') is session
        assert manager.provider is session

    def test_provider_setter_compat_feeds_resolution(self):
        """Older call sites / tests assign ``manager.provider`` directly;
        threaded flows must still resolve that session per cluster."""
        manager = self._manager({
            'provider': {'libvirt': {}},
            'clusters': {'cluster_a': {}},
        })

        class DuckSession:
            pass

        duck = DuckSession()
        manager.provider = duck
        assert manager.session_for_cluster('cluster_a') is duck
        assert manager.provider is duck

    def test_missing_session_raises_with_cluster_context(self):
        manager = self._manager({
            'provider': {'libvirt': {}},
            'clusters': {'cluster_a': {}},
        })
        with pytest.raises(ValueError, match=r"no provider session.*'libvirt'.*'cluster_a'"):
            manager.session_for_cluster('cluster_a')

    def test_cluster_level_provider_key_wins(self):
        """Forward-compat: a per-cluster ``provider:`` key (official in
        Phase 2) already steers resolution."""
        manager = self._manager({
            'provider': {'libvirt': {}},
            'clusters': {'services': {'provider': 'docker-compose'}},
        })
        assert manager.provider_type_for_cluster('services') == 'docker-compose'

    def test_unknown_cluster_falls_back_to_primary_type(self):
        manager = self._manager({'provider': {'libvirt': {}}, 'clusters': {}})
        assert manager.provider_type_for_cluster('nope') == 'libvirt'

    def test_first_registered_session_becomes_default(self):
        manager = self._manager({'provider': {'libvirt': {}}, 'clusters': {}})
        session = LibVirtSession(config={"provider": {"libvirt": {}}})
        manager.register_session('libvirt', session)
        assert manager.provider is session

    def test_vm_cluster_map_uses_flow_name_construction(self):
        manager = self._manager({
            'project': 'proj',
            'provider': {'libvirt': {}},
            'clusters': {
                'cluster_a': {'vms': {'vm1': {}, 'vm2': {}}},
                'cluster_b': {'vms': {'vm1': {}}},
            },
        })
        mapping = manager._vm_cluster_map()
        assert mapping == {
            'bprj__proj__bprj_cluster_a_vm1': 'cluster_a',
            'bprj__proj__bprj_cluster_a_vm2': 'cluster_a',
            'bprj__proj__bprj_cluster_b_vm1': 'cluster_b',
        }
