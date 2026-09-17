"""
A libvirt network boxman only *attaches* to is one it never manages.

``network_adapters[].is_global: true`` names a network that already exists on
the host and belongs to someone else. doc/network.md states the contract in one
line: boxman "will not create, validate or remove any of it -- that is the
point of the escape hatch."

Something now depends on that: the infra side owns a routed guest network on
the host, with its own firewall role, and boxman's guests reach it through a
global adapter. Were boxman ever to manage the networks its adapters merely
mention, two owners would hold the same bridge -- boxman would define it, hang
BXM_ISO chains off it, or remove it on teardown, against rules it did not
write. The failure would be silent on the boxman side and visible only as a
host that has lost its own network.

Structurally the property holds because every network loop in
``manager_parts/networks.py`` iterates ``cluster['networks']``, which a global
adapter never writes to. Only the naming precedence was pinned, so the loops
were free to grow an adapter pass. These tests pin the loops themselves, one
per lifecycle entry point:

    provision, and a first ``up``    -> define_networks
    ``up`` and ``update``            -> reconcile_networks, which also drives
                                        _reconcile_network_isolation
    ``deprovision`` and ``destroy``  -> destroy_networks
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit


# the network the host owns: named by an adapter, declared nowhere
FOREIGN = 'infra_guest_net'
# a network this cluster does declare, so every test carries its own control:
# a stub that simply does nothing would otherwise pass them all
OWNED = 'lab_internal'
FULL_OWNED = 'bprj__demo__bprj__clstr__cluster_1__clstr__lab_internal'


def _adapters():
    return [
        {'name': 'adapter_1', 'network_source': FOREIGN, 'is_global': True},
        {'name': 'adapter_2', 'network_source': OWNED},
    ]


def _cluster(workdir, *, declare_owned=True):
    cluster = {
        'workdir': str(workdir),
        'vms': {'node01': {'network_adapters': _adapters()}},
    }
    if declare_owned:
        # mode: route, so the isolation pass has something to act on too
        cluster['networks'] = {OWNED: {'mode': 'route'}}
    return cluster


def _manager(workdir, *, declare_owned=True, networks=None):
    mgr = BoxmanManager.__new__(BoxmanManager)
    cluster = _cluster(workdir, declare_owned=declare_owned)
    if networks is not None:
        cluster['networks'] = networks
    mgr.config = {'project': 'demo', 'clusters': {'cluster_1': cluster}}
    mgr.config_path = None
    mgr.logger = MagicMock()
    mgr._netlab = None

    session = MagicMock()
    session.define_network.return_value = True
    session.remove_network.return_value = True
    # a plan of 'none' keeps reconcile on its quiet path: this suite is about
    # which networks are looked at, not what is done to them
    session.plan_network.return_value = {'action': 'none'}
    session.reconcile_network_isolation.return_value = 'ok'
    mgr._provider = session
    mgr.session_for_cluster = MagicMock(return_value=session)

    def _synchronous(tasks, op_label=''):
        """Run each task in-process, mirroring _run_parallel's contract."""
        results, failures = {}, {}
        for label, target, args in tasks:
            try:
                results[label] = target(*args)
            except Exception as exc:                       # noqa: BLE001
                failures[label] = str(exc)
        return results, failures

    mgr._run_parallel = _synchronous
    return mgr, session


def _names_passed(mock_method):
    """Every ``name=`` keyword a provider method was called with."""
    return [call.kwargs.get('name') for call in mock_method.call_args_list]


def _mentions(session, needle):
    """Every call on *session*, of any method, whose repr names *needle*."""
    return [call for call in session.mock_calls if needle in str(call)]


class TestTheNameIsTakenVerbatim:
    """The half that is about naming: what the guest is attached to."""

    def test_a_global_adapter_keeps_the_host_s_own_name(self, tmp_path):
        mgr, _ = _manager(tmp_path)
        adapter = {'network_source': FOREIGN, 'is_global': True}
        mgr.resolve_adapter_network(adapter, 'cluster_1')
        assert adapter['network_source'] == FOREIGN

    def test_a_declared_network_is_still_namespaced(self, tmp_path):
        # the control for the test above: without namespacing, the two halves
        # of this file would be pinning the same trivially-true thing
        mgr, _ = _manager(tmp_path)
        adapter = {'network_source': OWNED}
        mgr.resolve_adapter_network(adapter, 'cluster_1')
        assert adapter['network_source'] == FULL_OWNED


class TestDefineNeverCreatesIt:
    """``provision``, and the first ``up``, reach networks here."""

    def test_only_the_declared_network_is_defined(self, tmp_path):
        mgr, session = _manager(tmp_path)
        mgr.define_networks()
        assert _names_passed(session.define_network) == [FULL_OWNED]

    def test_nothing_about_it_is_asked_of_the_provider(self, tmp_path):
        mgr, session = _manager(tmp_path)
        mgr.define_networks()
        assert not _mentions(session, FOREIGN)

    def test_an_adapter_alone_defines_nothing_at_all(self, tmp_path):
        # the shape the handoff actually uses: the cluster declares no
        # networks whatsoever and reaches the host's through the adapter
        mgr, session = _manager(tmp_path, declare_owned=False)
        mgr.define_networks()
        session.define_network.assert_not_called()


class TestReconcileNeverPlansIt:
    """``up`` and ``update`` reach networks here."""

    def test_only_the_declared_network_is_planned(self, tmp_path):
        mgr, session = _manager(tmp_path)
        mgr.reconcile_networks()
        assert _names_passed(session.plan_network) == [FULL_OWNED]

    def test_nothing_about_it_is_asked_of_the_provider(self, tmp_path):
        mgr, session = _manager(tmp_path)
        mgr.reconcile_networks()
        assert not _mentions(session, FOREIGN)

    def test_an_adapter_alone_plans_nothing_at_all(self, tmp_path):
        mgr, session = _manager(tmp_path, declare_owned=False)
        assert mgr.reconcile_networks() == {}
        session.plan_network.assert_not_called()

    def test_a_dry_run_does_not_look_at_it_either(self, tmp_path):
        mgr, session = _manager(tmp_path)
        mgr.reconcile_networks(dry_run=True)
        assert not _mentions(session, FOREIGN)


class TestIsolationNeverTouchesIt:
    """
    The one that would hurt most.

    Isolation is host iptables state: BXM_ISO chains, an INPUT/OUTPUT hook and
    a FORWARD accept, keyed on the network's bridge. Applying that to a bridge
    the infra role has its own rules on is boxman writing into someone else's
    firewall.
    """

    def test_the_declared_routed_network_is_isolated(self, tmp_path):
        # control: this pass does run, and does reach a network
        mgr, session = _manager(tmp_path)
        mgr._reconcile_network_isolation()
        assert _names_passed(session.reconcile_network_isolation) == [FULL_OWNED]

    def test_the_foreign_network_is_not(self, tmp_path):
        mgr, session = _manager(tmp_path)
        mgr._reconcile_network_isolation()
        assert not _mentions(session, FOREIGN)

    def test_an_adapter_alone_isolates_nothing(self, tmp_path):
        mgr, session = _manager(tmp_path, declare_owned=False)
        assert mgr._reconcile_network_isolation() == {}
        session.reconcile_network_isolation.assert_not_called()

    def test_it_is_not_isolated_through_reconcile_either(self, tmp_path):
        # reconcile_networks() calls the isolation pass itself; a global
        # adapter must not reach it by that route either
        mgr, session = _manager(tmp_path)
        mgr.reconcile_networks()
        assert _names_passed(session.reconcile_network_isolation) == [FULL_OWNED]


class TestDestroyNeverRemovesIt:
    """``deprovision`` and ``destroy`` reach networks here."""

    def test_only_the_declared_network_is_removed(self, tmp_path):
        mgr, session = _manager(tmp_path)
        assert mgr.destroy_networks() == {}
        assert _names_passed(session.remove_network) == [FULL_OWNED]

    def test_nothing_about_it_is_asked_of_the_provider(self, tmp_path):
        mgr, session = _manager(tmp_path)
        mgr.destroy_networks()
        assert not _mentions(session, FOREIGN)

    def test_an_adapter_alone_removes_nothing_at_all(self, tmp_path):
        mgr, session = _manager(tmp_path, declare_owned=False)
        assert mgr.destroy_networks() == {}
        session.remove_network.assert_not_called()


class TestTheExemptionIsDeclaration:
    """
    Not a name, not a flag on the network: the rule is who declared it.

    Pinning this keeps the exemption from being reimplemented as a list of
    blessed names, which would exempt a cluster's own network the moment
    someone reused the host's name for it.
    """

    def test_declaring_that_same_name_makes_it_boxman_s(self, tmp_path):
        mgr, session = _manager(tmp_path, networks={FOREIGN: {'mode': 'nat'}})
        mgr.define_networks()
        full = f'bprj__demo__bprj__clstr__cluster_1__clstr__{FOREIGN}'
        assert _names_passed(session.define_network) == [full]

    def test_and_it_is_namespaced_away_from_the_host_s(self, tmp_path):
        # the declared network is a *different* network that happens to share
        # a base name; the host's keeps its bare one
        mgr, _ = _manager(tmp_path, networks={FOREIGN: {'mode': 'nat'}})
        declared = mgr.full_network_name(
            project_config=mgr.config,
            cluster_name='cluster_1',
            network_name=FOREIGN)
        assert declared != FOREIGN

        adapter = {'network_source': FOREIGN, 'is_global': True}
        mgr.resolve_adapter_network(adapter, 'cluster_1')
        assert adapter['network_source'] == FOREIGN
