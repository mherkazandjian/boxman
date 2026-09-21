"""
Tests for the network garbage collector (#189).

A network removed from ``conf.yml`` used to leak: every lifecycle loop
iterates the *declared* networks, so the dropped one was destroyed by
nothing. It kept its bridge and subnet, its isolation chains stayed in
iptables, and its cache record then counted as a conflict against any
later network of the same name.

The cache is the record of what was provisioned, so the orphans are the
difference between it and the config. What is dangerous is not finding
them but removing them: a network is infrastructure, and the guests
attached to it are not mentioned in the config that dropped it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit


PROJECT = "demo"
PREFIX = f"bprj__{PROJECT}__bprj__clstr__cluster_1__clstr__"


def _manager(declared: dict | None = None,
             provisioned: dict | None = None) -> BoxmanManager:
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = {
        "project": PROJECT,
        "clusters": {
            "cluster_1": {
                "workdir": "/tmp/ws/c1",
                "vms": {"node01": {}},
                "networks": declared if declared is not None else {},
            },
        },
    }
    mgr.logger = MagicMock()
    mgr.cache = MagicMock()
    mgr.cache.projects = {
        PROJECT: {"conf": "/tmp/conf.yml",
                  "networks": provisioned if provisioned is not None else {}},
    }
    mgr.provider = MagicMock()
    mgr.provider.network_attached_domains.return_value = []
    mgr.provider.live_network_mode.return_value = "nat"
    mgr.provider.remove_network.return_value = True
    return mgr


class TestFindingTheOrphans:

    def test_a_network_dropped_from_the_config_is_found(self):
        mgr = _manager(declared={"keep": {}},
                       provisioned={f"{PREFIX}keep": {"bridge_name": "virbr1"},
                                    f"{PREFIX}gone": {"bridge_name": "virbr2"}})
        assert list(mgr._orphaned_networks()) == [f"{PREFIX}gone"]

    def test_a_config_that_declares_everything_has_no_orphans(self):
        mgr = _manager(declared={"a": {}, "b": {}},
                       provisioned={f"{PREFIX}a": {}, f"{PREFIX}b": {}})
        assert mgr._orphaned_networks() == {}

    def test_another_projects_network_is_never_an_orphan_of_this_one(self):
        """A `project::cluster::net` reference resolves to a network this
        project may use but does not own. Reaping it would destroy
        somebody else's infrastructure."""
        foreign = "bprj__other__bprj__clstr__c1__clstr__shared"
        mgr = _manager(declared={}, provisioned={foreign: {}})
        assert mgr._orphaned_networks() == {}

    def test_an_unreadable_cache_reports_nothing_rather_than_everything(self):
        """"I cannot read what was provisioned" must not be answered with
        "nothing was", which would hide every orphan there is."""
        mgr = _manager(declared={}, provisioned={f"{PREFIX}gone": {}})
        mgr.cache.read_projects_cache.side_effect = OSError("no such file")
        assert mgr._orphaned_networks() == {}
        assert mgr.logger.warning.called

    def test_a_project_absent_from_the_cache_has_no_orphans(self):
        mgr = _manager(declared={}, provisioned={})
        mgr.cache.projects = {}
        assert mgr._orphaned_networks() == {}


class TestRemovingThemIsOptIn:

    def _orphaned(self):
        return _manager(declared={},
                        provisioned={f"{PREFIX}gone": {"bridge_name": "virbr2"}})

    def test_without_the_flag_it_reports_and_removes_nothing(self):
        mgr = self._orphaned()
        results = mgr._prune_orphaned_networks(prune=False)
        assert results == {f"{PREFIX}gone": "skipped"}
        mgr.provider.remove_network.assert_not_called()
        # and it says how to act on it
        assert any("--prune-networks" in str(call)
                   for call in mgr.logger.warning.call_args_list)

    def test_with_the_flag_it_removes_and_forgets_the_entry(self):
        mgr = self._orphaned()
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "removed"}
        mgr.provider.remove_network.assert_called_once()
        mgr.cache.unregister_network.assert_called_once_with(
            PROJECT, f"{PREFIX}gone")

    def test_a_dry_run_changes_nothing(self):
        mgr = self._orphaned()
        results = mgr._prune_orphaned_networks(dry_run=True, prune=True)
        assert results == {f"{PREFIX}gone": "skipped"}
        mgr.provider.remove_network.assert_not_called()

    def test_a_failed_removal_keeps_the_cache_entry(self):
        """The entry is the only record that the network is there. Dropping
        it on a failed removal would lose the leak rather than fix it."""
        mgr = self._orphaned()
        mgr.provider.remove_network.return_value = False
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.cache.unregister_network.assert_not_called()

    def test_a_raising_removal_is_a_failure_not_a_crash(self):
        mgr = self._orphaned()
        mgr.provider.remove_network.side_effect = RuntimeError("libvirt gone")
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.cache.unregister_network.assert_not_called()


class TestAttachedGuestsAreNeverDisconnected:

    def _orphaned(self):
        return _manager(declared={},
                        provisioned={f"{PREFIX}gone": {"bridge_name": "virbr2"}})

    def test_a_network_with_guests_on_it_is_left_alone(self):
        """Removing it deletes the bridge, and the guests are not mentioned
        in the config that dropped the network."""
        mgr = self._orphaned()
        mgr.provider.network_attached_domains.return_value = ["node01", "node02"]
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "skipped"}
        mgr.provider.remove_network.assert_not_called()
        # and the refusal names them, so it can be acted on
        assert any("node01" in str(call)
                   for call in mgr.logger.warning.call_args_list)

    def test_an_unanswerable_attachment_question_counts_as_attached(self):
        """An empty list is the dangerous default: it reads as "nothing is
        attached" and lets the removal proceed."""
        mgr = self._orphaned()
        mgr.provider.network_attached_domains.side_effect = RuntimeError("down")
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "skipped"}
        mgr.provider.remove_network.assert_not_called()

    def test_a_provider_that_cannot_report_attachments_blocks_removal(self):
        mgr = self._orphaned()
        del mgr.provider.network_attached_domains
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "skipped"}
        mgr.provider.remove_network.assert_not_called()


class TestTheModeComesFromTheLiveNetwork:
    """The cache records an address and a bridge, not a forward mode — and
    the mode decides whether removal also tears down iptables rules."""

    def _orphaned(self):
        return _manager(declared={},
                        provisioned={f"{PREFIX}gone": {"bridge_name": "virbr2"}})

    def test_the_live_mode_is_passed_to_the_removal(self):
        mgr = self._orphaned()
        mgr.provider.live_network_mode.return_value = "route"
        mgr._prune_orphaned_networks(prune=True)
        info = mgr.provider.remove_network.call_args.kwargs["info"]
        assert info["mode"] == "route"
        assert info["bridge_name"] == "virbr2"

    def test_an_unreadable_live_mode_does_not_invent_one(self):
        """Guessing "nat" would silently skip the route-mode teardown, which
        is exactly the iptables leak this fix is about."""
        mgr = self._orphaned()
        mgr.provider.live_network_mode.return_value = None
        mgr._prune_orphaned_networks(prune=True)
        info = mgr.provider.remove_network.call_args.kwargs["info"]
        assert "mode" not in info
