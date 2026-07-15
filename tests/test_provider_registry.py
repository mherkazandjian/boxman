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

    def test_docker_compose_builds_a_session(self):
        """Phase 3 (#51): the docker-compose provider is now implemented, so
        the registry constructs a real ``DockerComposeSession`` (it no longer
        raises the Phase-1 ``NotImplementedError`` stub)."""
        from boxman.providers.docker_compose.session import DockerComposeSession
        session = create_session(
            'docker-compose', {"provider": {"docker-compose": {}}})
        assert isinstance(session, DockerComposeSession)

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

    def test_vm_cluster_map_empty_without_project(self):
        """import-image-style slice configs lack ``project``; the map is
        empty so callers fall back to the default session (not KeyError)."""
        manager = self._manager({
            'provider': {'libvirt': {}},
            'clusters': {'c': {'vms': {'v': {}}}},
        })
        assert manager._vm_cluster_map() == {}

    def test_vm_cluster_map_empty_when_config_none(self):
        manager = BoxmanManager()  # config is None
        assert manager._vm_cluster_map() == {}

    def test_first_registered_stays_default_when_second_type_registers(self):
        """A single-registration test can't tell first-wins from last-wins;
        register two *different* types and pin that the first stays default."""
        manager = self._manager({'provider': {'libvirt': {}}, 'clusters': {}})
        first, second = object(), object()
        manager.register_session('libvirt', first)
        manager.register_session('virtualbox', second)
        assert manager.provider is first
        assert manager._get_sessions()['virtualbox'] is second

    def test_direct_provider_poke_resolves_through_threaded_flows(self):
        """Call sites/tests that assign ``_provider`` directly (bypassing
        the setter — the ``test_manager_core`` pattern) must still resolve
        the same session through ``session_for_cluster``/``session_for_vm``;
        otherwise the shim's keep-listed and threaded halves diverge."""
        manager = self._manager({
            'project': 'proj',
            'provider': {'libvirt': {}},
            'clusters': {'cluster_a': {'vms': {'vm1': {}}}},
        })
        mock = object()
        manager._provider = mock  # direct poke, no register_session/setter
        assert manager.session_for_cluster('cluster_a') is mock
        assert manager.session_for_vm('bprj__proj__bprj_cluster_a_vm1') is mock

    def test_provider_poke_does_not_mask_missing_nonprimary_session(self):
        """The ``_provider`` fallback is scoped to the *primary* type; a
        cluster with a non-primary provider override and no registered
        session still fails with the friendly per-cluster error."""
        manager = self._manager({
            'project': 'proj',
            'provider': {'libvirt': {}},
            'clusters': {
                'services': {'provider': 'docker-compose', 'vms': {'c1': {}}},
            },
        })
        manager._provider = object()
        with pytest.raises(ValueError, match=r"'docker-compose'.*'services'"):
            manager.session_for_cluster('services')

    def test_setter_is_new_safe(self):
        """A ``__new__``-built manager (no ``__init__``, so no ``config``)
        can still take a direct ``provider`` assignment — the setter must
        not dereference a missing ``config``."""
        manager = BoxmanManager.__new__(BoxmanManager)
        sentinel = object()
        manager.provider = sentinel
        assert manager.provider is sentinel
        assert manager._get_sessions()['libvirt'] is sentinel


class TestSessionForVm:
    """``session_for_vm`` is the highest-traffic new resolver (~12 threaded
    call sites route through it); pin both the map-hit and the
    not-in-config fallback that the removed-VM flows rely on."""

    @staticmethod
    def _manager(config):
        manager = BoxmanManager()
        manager.config = config
        return manager

    def test_maps_full_name_to_its_cluster_session(self):
        manager = self._manager({
            'project': 'proj',
            'provider': {'libvirt': {}},
            'clusters': {'cluster_a': {'vms': {'vm1': {}}}},
        })
        session = object()
        manager.register_session('libvirt', session)
        assert manager.session_for_vm('bprj__proj__bprj_cluster_a_vm1') is session

    def test_unknown_name_falls_back_to_default_session(self):
        """VMs no longer in the config (e.g. removed then destroyed) are not
        in the cluster map and resolve to the default session."""
        manager = self._manager({
            'project': 'proj',
            'provider': {'libvirt': {}},
            'clusters': {'cluster_a': {'vms': {'vm1': {}}}},
        })
        default = object()
        manager.register_session('libvirt', default)
        assert manager.session_for_vm('bprj__proj__bprj_cluster_a_ghost') is default

    def test_underivable_config_returns_default_without_raising(self):
        """A provider-slice config (import-image) has no ``project`` — the
        fallback must be reached instead of raising ``KeyError``."""
        manager = self._manager({'uri': 'qemu:///system'})
        default = object()
        manager.provider = default
        assert manager.session_for_vm('bprj__x__bprj_c_v') is default

    def test_none_config_returns_default_without_raising(self):
        manager = BoxmanManager()  # config is None, no default registered
        assert manager.session_for_vm('bprj__x__bprj_c_v') is None
