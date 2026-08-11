"""
Regression tests for #85 item 24 (b+c): cache/network divergence.

- ``deprovision`` must keep the project registered in the cache when the
  VM or network teardown left resources behind, so the leftovers stay
  visible to ``boxman list`` and a later deprovision can finish the job.
- ``_forget_cached_network`` drops the network entry through the normal
  ``BoxmanCache.unregister_network`` path.
"""

import types
from unittest.mock import MagicMock

import pytest

from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit


def _manager():
    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = {
        "project": "demo",
        "clusters": {
            "cluster_1": {
                "workdir": "/tmp/ws/c1",
                "vms": {"service01": {}},
            },
        },
    }
    mgr.logger = MagicMock()
    mgr.cache = MagicMock()
    mgr.unregister_from_cache = MagicMock()
    mgr._update_sessions_with_runtime = MagicMock()
    mgr.destroy_netlab = MagicMock()
    mgr.deprovision_compose_clusters = MagicMock()
    return mgr


class TestDeprovisionUnregister:

    def _run(self, mgr, vm_failures, net_failures):
        mgr._run_parallel = MagicMock(return_value=({}, vm_failures))
        mgr.destroy_networks = MagicMock(return_value=net_failures)
        BoxmanManager.deprovision(mgr, types.SimpleNamespace(cleanup=False))

    def test_unregisters_when_teardown_succeeds(self):
        mgr = _manager()
        self._run(mgr, vm_failures={}, net_failures={})
        mgr.unregister_from_cache.assert_called_once()

    def test_skips_unregister_when_vm_teardown_failed(self):
        mgr = _manager()
        self._run(mgr, vm_failures={"cluster_1/service01": "boom"},
                  net_failures={})
        mgr.unregister_from_cache.assert_not_called()
        mgr.logger.warning.assert_called()

    def test_skips_unregister_when_network_teardown_failed(self):
        mgr = _manager()
        self._run(mgr, vm_failures={}, net_failures={"cluster_1/net": "boom"})
        mgr.unregister_from_cache.assert_not_called()
        mgr.logger.warning.assert_called()


class TestForgetCachedNetwork:

    def test_delegates_to_cache_unregister_network(self):
        mgr = _manager()
        mgr.cache.unregister_network.return_value = True
        mgr._forget_cached_network("bprj__demo__bprj_cluster_1_net")
        mgr.cache.unregister_network.assert_called_once_with(
            "demo", "bprj__demo__bprj_cluster_1_net")

    def test_swallows_cache_errors_with_warning(self):
        mgr = _manager()
        mgr.cache.unregister_network.side_effect = OSError("disk gone")
        mgr._forget_cached_network("net")  # must not raise
        mgr.logger.warning.assert_called_once()


class TestDestroyNetworksFailures:

    def test_returns_run_parallel_failures(self):
        mgr = _manager()
        mgr.config["clusters"]["cluster_1"]["networks"] = {"net": {}}
        mgr._run_parallel = MagicMock(return_value=({}, {"cluster_1/net": "x"}))
        assert mgr.destroy_networks() == {"cluster_1/net": "x"}

    def test_returns_empty_dict_on_success(self):
        mgr = _manager()
        mgr._run_parallel = MagicMock(return_value=({}, {}))
        assert mgr.destroy_networks() == {}
