"""
``boxman up`` on a registered docker-compose-only project (#164 C3).

That branch reconciled the shared bridges and the compose clusters and
returned. It never reconciled the containerlab lab -- so on a compose-only
project the lab was deployed once, by the first ``up`` (which routes
through ``provision()``), and then never again. A host reboot or a manual
``docker stop`` left it down with no way back short of a re-provision.

The hybrid path (Case 3, "all VMs are already running") has always called
``ensure_netlab_up()``; a compose-only project simply never reaches it.
"""

from unittest.mock import MagicMock

import pytest

from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit


def _compose_only_manager(registered: bool = True) -> BoxmanManager:
    """A manager whose project is compose-only, optionally already known."""
    mgr = BoxmanManager.__new__(BoxmanManager)
    # a real compose-only project, so ``_compose_clusters`` derives the way
    # it does in production rather than being patched into existence
    mgr.config = {
        'project': 'lab-project',
        'version': '2.0',
        'clusters': {
            'containers': {
                'provider': 'docker-compose',
                'boxes': {'node1': {'image': 'debian:12'}},
            },
        },
    }
    mgr.logger = MagicMock()

    # compose-only: no libvirt VMs anywhere
    mgr._get_project_vm_names = MagicMock(return_value=[])
    assert mgr._compose_clusters, 'fixture is not a compose-only project'

    cache = MagicMock()
    cache.projects = {'lab-project': {}} if registered else {}
    mgr.cache = cache

    for name in ('ensure_shared_bridges', 'provision_compose_clusters',
                 'ensure_netlab_up', 'reconcile_networks', 'provision'):
        setattr(mgr, name, MagicMock())
    return mgr


def _cli():
    return MagicMock(force=False, yes=True, recreate_networks=False)


class TestRegisteredComposeOnlyUp:

    def test_reconciles_the_lab(self):
        mgr = _compose_only_manager(registered=True)

        mgr.up(_cli())

        mgr.ensure_netlab_up.assert_called_once()

    def test_still_reconciles_bridges_and_clusters(self):
        """The new call must not displace what the branch already did."""
        mgr = _compose_only_manager(registered=True)

        mgr.up(_cli())

        mgr.ensure_shared_bridges.assert_called_once()
        mgr.provision_compose_clusters.assert_called_once()
        mgr.provision.assert_not_called()

    def test_does_not_reconcile_libvirt_networks(self):
        """A compose-only project has no libvirt networks to reconcile."""
        mgr = _compose_only_manager(registered=True)

        mgr.up(_cli())

        mgr.reconcile_networks.assert_not_called()

    def test_lab_comes_up_after_the_containers(self):
        """Same order as provision(): compose clusters, then the lab."""
        mgr = _compose_only_manager(registered=True)
        order = []
        mgr.provision_compose_clusters.side_effect = lambda: order.append('compose')
        mgr.ensure_netlab_up.side_effect = lambda: order.append('netlab')

        mgr.up(_cli())

        assert order == ['compose', 'netlab']


class TestUnregisteredComposeOnlyUp:
    """A first run still goes through provision(), which deploys the lab."""

    def test_routes_through_provision(self):
        mgr = _compose_only_manager(registered=False)

        mgr.up(_cli())

        mgr.provision.assert_called_once()
        # provision() owns the lab deploy on this path
        mgr.ensure_netlab_up.assert_not_called()
