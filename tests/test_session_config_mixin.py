"""
Cross-provider tests for the shared session config surface (#85 item 34).

``boxman.providers.session_base.SessionConfigMixin`` is the single
implementation of the config surface for all three provider sessions
(libvirt / virtualbox / docker-compose). These tests drive each real
session class and pin the uniform semantics:

  - the constructor extracts only its own ``provider[<key>]`` block
    (already merged upstream by ``boxman.providers.merge_provider_configs``);
  - ``update_provider_config`` is plain last-write-wins everywhere;
  - ``uri`` / ``use_sudo`` read through ``provider_config`` with
    per-provider defaults, and their setters write straight into it;
  - ``update_provider_config_with_runtime`` is a no-op for the providers
    that only support the ``local`` runtime.

The app/project merge itself (union/eviction of the sudo lists) is
covered by tests/test_provider_config_merge.py.
"""

from __future__ import annotations

import pytest

from boxman.providers.docker_compose.session import DockerComposeSession
from boxman.providers.libvirt.session import LibVirtSession
from boxman.providers.virtualbox.session import VirtualBoxSession

pytestmark = pytest.mark.unit

#: (session class, its provider: block key, default uri) per provider
SESSIONS = [
    pytest.param(LibVirtSession, "libvirt", "qemu:///system", id="libvirt"),
    pytest.param(VirtualBoxSession, "virtualbox", "", id="virtualbox"),
    pytest.param(DockerComposeSession, "docker-compose", "", id="docker-compose"),
]


class TestSharedConfigSurface:

    @pytest.mark.parametrize("cls,key,_", SESSIONS)
    def test_reads_only_its_own_provider_block(self, cls, key, _):
        config = {
            "provider": {
                "libvirt": {"uri": "qemu+ssh://hv/system"},
                "virtualbox": {"vboxmanage_cmd": "vbm"},
                "docker-compose": {"project_name": "dc"},
            },
        }
        session = cls(config=config)
        assert session.provider_config == config["provider"][key]

    @pytest.mark.parametrize("cls,key,_", SESSIONS)
    def test_empty_or_missing_provider_block(self, cls, key, _):
        assert cls(config={"provider": {key: None}}).provider_config == {}
        assert cls(config={}).provider_config == {}
        assert cls(config=None).provider_config == {}

    @pytest.mark.parametrize("cls,key,_", SESSIONS)
    def test_update_is_last_write_wins(self, cls, key, _):
        session = cls(config={"provider": {key: {"use_sudo": True}}})
        session.update_provider_config({"use_sudo": False, "runtime": "local"})
        assert session.provider_config["use_sudo"] is False
        assert session.provider_config["runtime"] == "local"

    @pytest.mark.parametrize("cls,key,_", SESSIONS)
    def test_update_fills_in_missing_keys(self, cls, key, _):
        session = cls(config={"provider": {key: {"use_sudo": True}}})
        session.update_provider_config({"extra": 1})
        assert session.provider_config["use_sudo"] is True
        assert session.provider_config["extra"] == 1

    @pytest.mark.parametrize("cls,key,_", SESSIONS)
    def test_provider_config_setter_replaces(self, cls, key, _):
        session = cls(config={"provider": {key: {"a": 1}}})
        session.provider_config = {"b": 2}
        assert session.provider_config == {"b": 2}

    @pytest.mark.parametrize("cls,key,default_uri", SESSIONS)
    def test_uri_default_and_setter(self, cls, key, default_uri):
        session = cls(config={"provider": {key: {}}})
        assert session.uri == default_uri
        session.uri = "scheme://example"
        assert session.uri == "scheme://example"
        assert session.provider_config["uri"] == "scheme://example"

    @pytest.mark.parametrize("cls,key,_", SESSIONS)
    def test_use_sudo_default_and_setter(self, cls, key, _):
        session = cls(config={"provider": {key: {}}})
        assert session.use_sudo is False
        session.use_sudo = True
        assert session.use_sudo is True
        assert session.provider_config["use_sudo"] is True

    @pytest.mark.parametrize("cls,key,_", [
        pytest.param(VirtualBoxSession, "virtualbox", "", id="virtualbox"),
        pytest.param(DockerComposeSession, "docker-compose", "", id="docker-compose"),
    ])
    def test_update_with_runtime_is_noop_for_local_only_providers(
            self, cls, key, _):
        session = cls(config={"provider": {key: {"x": 1}}})
        assert session.update_provider_config_with_runtime() is None
        assert session.provider_config == {"x": 1}
