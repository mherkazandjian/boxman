"""
Manager-level behaviour of routed-network isolation reconciliation.

Isolation is host iptables state rather than libvirt state, so it does not
survive a reboot while libvirt autostarts the network regardless. Reconciling
it on every run is only half the job: the outcome has to reach the caller,
otherwise `up` exits 0 while a routed network sits unisolated.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit


def _mgr(tmp_path: Path, mode: str = "route") -> BoxmanManager:
    with patch("boxman.manager.BoxmanCache"):
        m = BoxmanManager()
    m.config = {
        "project": "demo",
        "clusters": {
            "cluster_1": {
                "workdir": str(tmp_path),
                "networks": {"isolated1": {"mode": mode}},
            },
        },
    }
    m._provider = MagicMock()
    m._provider.provider_config = {}
    m._provider.plan_network.return_value = {"action": "none"}
    return m


class TestIsolationOutcomeReachesTheCaller:

    def test_failed_isolation_fails_the_network(self, tmp_path: Path):
        # without this the run is reported as successful while the guests can
        # still reach the host -- the exact property the mode exists to deny
        m = _mgr(tmp_path)
        m._provider.reconcile_network_isolation.return_value = 'failed'
        results = m.reconcile_networks()
        assert set(results.values()) == {'failed'}
        assert all('isolated1' in key for key in results)

    def test_an_exception_is_also_a_failure(self, tmp_path: Path):
        m = _mgr(tmp_path)
        m._provider.reconcile_network_isolation.side_effect = RuntimeError("boom")
        assert set(m.reconcile_networks().values()) == {'failed'}

    @pytest.mark.parametrize("outcome", ["ok", "repaired", "absent", "skipped"])
    def test_non_failure_outcomes_do_not_fail_the_network(
        self, tmp_path: Path, outcome
    ):
        m = _mgr(tmp_path)
        m._provider.reconcile_network_isolation.return_value = outcome
        assert m.reconcile_networks() == {}

    def test_repaired_is_reported_loudly(self, tmp_path: Path):
        # a network that lost its rules was reachable for however long it was
        # up; repairing it silently would hide that entirely
        m = _mgr(tmp_path)
        m.logger = MagicMock()
        m._provider.reconcile_network_isolation.return_value = 'repaired'
        m.reconcile_networks()
        warnings = [c.args[0] for c in m.logger.warning.call_args_list if c.args]
        assert any("isolation rules were missing" in w for w in warnings)

    def test_dry_run_checks_without_touching_anything(self, tmp_path: Path):
        m = _mgr(tmp_path)
        m.logger = MagicMock()
        m._provider.reconcile_network_isolation.return_value = 'drifted'
        m.reconcile_networks(dry_run=True)
        assert m._provider.reconcile_network_isolation.call_args.kwargs[
            "check_only"] is True
        warnings = [c.args[0] for c in m.logger.warning.call_args_list if c.args]
        assert any("would be re-applied" in w for w in warnings)

    def test_non_routed_networks_are_not_probed(self, tmp_path: Path):
        m = _mgr(tmp_path, mode="nat")
        m.reconcile_networks()
        m._provider.reconcile_network_isolation.assert_not_called()
