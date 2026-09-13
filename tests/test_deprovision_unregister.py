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

from boxman.exceptions import ProvisionError
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
    mgr.deprovision_files = MagicMock()
    mgr._confirm_project_torn_down = MagicMock(return_value=(True, ''))
    return mgr


class TestDeprovisionUnregister:

    def _run(self, mgr, vm_failures, net_failures, cleanup=False):
        mgr._run_parallel = MagicMock(return_value=({}, vm_failures))
        mgr.destroy_networks = MagicMock(return_value=net_failures)
        mgr.deprovision(types.SimpleNamespace(cleanup=cleanup))

    def test_unregisters_when_teardown_succeeds(self):
        mgr = _manager()
        self._run(mgr, vm_failures={}, net_failures={})
        mgr.unregister_from_cache.assert_called_once()

    def test_skips_unregister_when_vm_teardown_failed(self):
        mgr = _manager()
        with pytest.raises(ProvisionError):
            self._run(mgr, vm_failures={"cluster_1/service01": "boom"},
                      net_failures={})
        mgr.unregister_from_cache.assert_not_called()
        mgr.logger.error.assert_called()

    def test_skips_unregister_when_network_teardown_failed(self):
        mgr = _manager()
        with pytest.raises(ProvisionError):
            self._run(mgr, vm_failures={},
                      net_failures={"cluster_1/net": "boom"})
        mgr.unregister_from_cache.assert_not_called()
        mgr.logger.error.assert_called()

    def test_keeps_generated_files_when_teardown_failed(self):
        """``--cleanup`` must not run when resources survived: a retry needs
        the ssh keys and inventory that deprovision_files() would delete."""
        mgr = _manager()
        with pytest.raises(ProvisionError):
            self._run(mgr, vm_failures={"cluster_1/service01": "boom"},
                      net_failures={}, cleanup=True)
        mgr.deprovision_files.assert_not_called()

    def test_unconfirmed_teardown_is_a_failure(self):
        """A libvirt query that could not be answered must never be read as
        'nothing is left' — the whole point of the fail-closed check."""
        mgr = _manager()
        mgr._confirm_project_torn_down = MagicMock(
            return_value=(False, "could not query libvirt to confirm the "
                                 "teardown completed"))
        with pytest.raises(ProvisionError) as exc:
            self._run(mgr, vm_failures={}, net_failures={})
        assert "could not query libvirt" in str(exc.value)
        mgr.unregister_from_cache.assert_not_called()

    def test_survivors_block_unregister(self):
        mgr = _manager()
        mgr._confirm_project_torn_down = MagicMock(
            return_value=(False, "VMs are still defined: bprj__demo__bprj_c1_s1"))
        with pytest.raises(ProvisionError) as exc:
            self._run(mgr, vm_failures={}, net_failures={})
        assert "still defined" in str(exc.value)
        mgr.unregister_from_cache.assert_not_called()

    def test_compose_teardown_failure_blocks_unregister(self):
        mgr = _manager()
        mgr.deprovision_compose_clusters.side_effect = RuntimeError(
            "compose down failed")
        with pytest.raises(ProvisionError) as exc:
            self._run(mgr, vm_failures={}, net_failures={})
        assert "compose down failed" in str(exc.value)
        mgr.unregister_from_cache.assert_not_called()

    def test_a_compose_exception_with_no_message_still_blocks_cleanup(self):
        """The failure flag is a bool of its own. Deriving it from the
        exception's text let an exception raised with an empty message read
        as success, and the file + cache cleanup went ahead over a compose
        cluster that is still up."""
        mgr = _manager()
        mgr.deprovision_compose_clusters.side_effect = RuntimeError()

        with pytest.raises(ProvisionError):
            self._run(mgr, vm_failures={}, net_failures={}, cleanup=True)

        mgr.deprovision_files.assert_not_called()
        mgr.unregister_from_cache.assert_not_called()

    def test_finalize_false_defers_cleanup_to_the_caller(self):
        """``destroy`` owns the final cleanup so it can finish its own
        teardown steps before anything irreversible happens."""
        mgr = _manager()
        mgr._run_parallel = MagicMock(return_value=({}, {}))
        mgr.destroy_networks = MagicMock(return_value={})
        mgr.deprovision(types.SimpleNamespace(cleanup=True), finalize=False)
        mgr.unregister_from_cache.assert_not_called()
        mgr.deprovision_files.assert_not_called()


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
