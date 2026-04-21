"""
Unit tests for the Protocol contract in boxman.abstract.providers.

Part of Phase 2.3 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).

The protocols are ``runtime_checkable`` so ``isinstance`` works; these
tests pin the structural contract so a future refactor that drops a
method (e.g. ``snapshot_list``) from ``LibVirtSession`` fails loudly.
"""

from __future__ import annotations

import pytest

from boxman.abstract.providers import Provider, ProviderSession
from boxman.providers.libvirt.session import LibVirtSession


pytestmark = pytest.mark.unit


class TestProviderSessionProtocol:

    def test_libvirt_session_satisfies_protocol(self):
        session = LibVirtSession(config={"provider": {"libvirt": {}}})
        # Protocol instance check for attributes + methods defined in the shape
        assert isinstance(session, ProviderSession)

    def test_arbitrary_object_does_not_satisfy_protocol(self):
        class NotASession:
            pass

        assert not isinstance(NotASession(), ProviderSession)

    def test_minimal_duck_typed_object_satisfies_protocol(self):
        """Any object with the right attributes and methods satisfies it."""

        class DuckSession:
            provider_config: dict = {}
            uri: str = ""
            use_sudo: bool = False

            def update_provider_config(self, new_config): pass
            def start_vm(self, vm_name): return True
            def destroy_vm(self, name, force=False): return True
            def clone_vm(self, new_vm_name, src_vm_name, info, workdir): return True
            def define_network(self, name=None, info=None, workdir=None): return True
            def destroy_network(self, name=None, info=None): return True
            def remove_network(self, name=None, info=None): return True
            def snapshot_take(self, *args, **kwargs): return True
            def snapshot_restore(self, vm_name, snapshot_name=None): return True
            def snapshot_delete(self, vm_name, snapshot_name): return True
            def snapshot_list(self, vm_name=None): return []

        assert isinstance(DuckSession(), ProviderSession)

    def test_missing_method_fails_isinstance(self):
        """If someone removes a method from the protocol contract, the
        check must flip to False — pins the public contract surface."""

        class NearlyASession:
            provider_config: dict = {}
            uri: str = ""
            use_sudo: bool = False

            def update_provider_config(self, new_config): pass
            def start_vm(self, vm_name): return True
            # destroy_vm deliberately missing → NOT a ProviderSession
            def clone_vm(self, new_vm_name, src_vm_name, info, workdir): return True

        assert not isinstance(NearlyASession(), ProviderSession)


class TestProviderProtocol:

    def test_object_with_name_satisfies_provider(self):
        class P:
            name = "libvirt"

        assert isinstance(P(), Provider)

    def test_object_without_name_does_not_satisfy_provider(self):
        class P:
            pass

        assert not isinstance(P(), Provider)
