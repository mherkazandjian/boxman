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

**Scope, and what is deliberately outside it.** What is pinned is that libvirt
network lifecycle, driven by a cluster's ``networks:`` declaration. It is not
the broader claim that boxman never acts on a network it did not declare,
because it does, by three routes that no adapter can reach:

- template creation takes its network from ``templates.<key>.network``,
  defaulting to ``default``, and ``cloudinit._ensure_network_active`` will
  ``net-start`` it;
- under the docker-compose *runtime*, the container entrypoint checks
  ``default`` inside the container before most verbs and defines and starts it
  when absent;
- ``destroy-runtime``, and ``destroy``'s docker-runtime branch, remove that
  container's libvirt state wholesale.

No data flows from ``network_adapters[]`` into any of those -- an adapter
never selects a template's network -- but a foreign network that happens to
share a name with a template's network, or that lives inside the container
runtime, is not protected by what this file pins. Say so rather than let the
file read as a guarantee it does not make.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from boxman.exceptions import ConfigError
from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit


# the network the host owns: named by an adapter, declared nowhere
FOREIGN = 'infra_guest_net'
# a network this cluster does declare, so every test carries its own control:
# a stub that simply does nothing would otherwise pass them all
OWNED = 'lab_internal'
FULL_OWNED = 'bprj__demo__bprj__clstr__cluster_1__clstr__lab_internal'

#: the cluster declares the control network below
DEFAULT = object()
#: no ``networks:`` key at all -- distinct from a key set to null or to {}
NO_KEY = object()

GLOBAL_ADAPTER = {'name': 'adapter_1', 'network_source': FOREIGN,
                  'is_global': True}
OWNED_ADAPTER = {'name': 'adapter_2', 'network_source': OWNED}

#: Plans that reach the branches of reconcile_networks. A fixed 'none' plan
#: leaves create/live/recreate unexecuted, and an implementation that acted on
#: a foreign network there would never be observed.
PLANS = {
    'none': {'action': 'none'},
    'create': {'action': 'create', 'structural': [], 'range_ops': [],
               'host_ops': []},
    'live': {'action': 'live', 'inactive': True, 'structural': [],
             'range_ops': [],
             'host_ops': [('add-last', {'mac': '52:54:00:0c:00:01',
                                        'ip': '10.77.0.9', 'name': 'n1'})]},
    'recreate': {'action': 'recreate', 'structural': ['mode nat -> route'],
                 'range_ops': [], 'host_ops': [], 'attached_vms': []},
}

#: The destructive plan, with a guest on the network. Reached only with
#: ``allow_recreate``: without it ``_recreate_network`` returns 'skipped' at the
#: authorisation guard, ahead of every line that removes or redefines anything.
RECREATE_AUTHORIZED = {
    'action': 'recreate', 'structural': ['mode nat -> route'],
    'range_ops': [], 'host_ops': [],
    'attached_vms': ['bprj__demo__bprj_cluster_1_node01'],
}


def _manager(workdir, *, networks=DEFAULT, adapters=None, plan='none'):
    """
    A bare manager whose one cluster declares *networks* and attaches
    *adapters*, with every provider call recorded on a single mock.
    """
    cluster = {
        'workdir': str(workdir),
        'vms': {'node01': {'network_adapters':
                           [dict(a) for a in
                            (adapters if adapters is not None
                             else [GLOBAL_ADAPTER, OWNED_ADAPTER])]}},
    }
    if networks is DEFAULT:
        # mode: route, so the isolation pass has something to act on too
        cluster['networks'] = {OWNED: {'mode': 'route'}}
    elif networks is not NO_KEY:
        cluster['networks'] = networks

    mgr = BoxmanManager.__new__(BoxmanManager)
    mgr.config = {'project': 'demo', 'clusters': {'cluster_1': cluster}}
    mgr.config_path = None
    mgr.logger = MagicMock()
    mgr._netlab = None

    session = MagicMock()
    session.define_network.return_value = True
    session.remove_network.return_value = True
    session.start_network.return_value = True
    session.apply_network_live_plan.return_value = True
    session.plan_network.return_value = (
        plan if isinstance(plan, dict) else PLANS[plan])
    session.reconcile_network_isolation.return_value = 'ok'
    mgr._provider = session
    mgr.session_for_cluster = MagicMock(return_value=session)
    # the recreate path drops the removed network from the projects cache
    # before redefining it; mocking the store keeps that call in the path
    mgr.cache = MagicMock()

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


def _adapter_only(workdir, networks):
    """The shape the handoff actually uses: one global adapter, nothing else."""
    return _manager(workdir, networks=networks, adapters=[GLOBAL_ADAPTER])


def _names_passed(mock_method):
    """Every ``name=`` keyword a provider method was called with."""
    return [call.kwargs.get('name') for call in mock_method.call_args_list]


def _mentions(session, needle):
    """
    Every call on *session*, of any method, whose repr names *needle*.

    Deliberately blunt: a method-specific assertion only rejects the wrong
    implementation someone thought of. This one rejects any call that so much
    as names the foreign network, positional arguments and nested dicts
    included.
    """
    return [call for call in session.mock_calls if needle in str(call)]


#: the three shapes "this cluster declares no networks" arrives in. A config
#: that renders its `networks:` block empty keeps the key, set to null -- the
#: case #183 had to normalise elsewhere -- so all three are real.
NO_NETWORKS = pytest.mark.parametrize(
    'networks', [NO_KEY, None, {}], ids=['absent', 'null', 'empty'])


class TestTheNameIsTakenVerbatim:
    """The half that is about naming: what the guest is attached to."""

    def test_a_global_adapter_keeps_the_host_s_own_name(self, tmp_path):
        mgr, _ = _manager(tmp_path)
        adapter = dict(GLOBAL_ADAPTER)
        mgr.resolve_adapter_network(adapter, 'cluster_1')
        assert adapter['network_source'] == FOREIGN

    def test_a_declared_network_is_still_namespaced(self, tmp_path):
        # the control for the test above: without namespacing, the two halves
        # of this file would be pinning the same trivially-true thing
        mgr, _ = _manager(tmp_path)
        adapter = dict(OWNED_ADAPTER)
        mgr.resolve_adapter_network(adapter, 'cluster_1')
        assert adapter['network_source'] == FULL_OWNED

    def test_resolving_an_adapter_asks_the_provider_nothing(self, tmp_path):
        """
        Resolution is a rename, not an activation.

        Checking only the resulting string leaves room for a resolver that
        "helpfully" starts the network it just recognised as global -- which
        is boxman activating a network the host owns, on the way to attaching
        a guest to it.
        """
        mgr, session = _manager(tmp_path)
        for adapter in (dict(GLOBAL_ADAPTER), dict(OWNED_ADAPTER)):
            mgr.resolve_adapter_network(adapter, 'cluster_1')
        assert session.mock_calls == []


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

    @NO_NETWORKS
    def test_an_adapter_alone_asks_the_provider_nothing_at_all(
            self, tmp_path, networks):
        # the handoff's own shape, in both spellings of "no networks"
        mgr, session = _adapter_only(tmp_path, networks)
        mgr.define_networks()
        assert session.mock_calls == []


class TestReconcileNeverPlansIt:
    """``up`` and ``update`` reach networks here."""

    @pytest.mark.parametrize('plan', list(PLANS))
    def test_only_the_declared_network_is_planned(self, tmp_path, plan):
        mgr, session = _manager(tmp_path, plan=plan)
        mgr.reconcile_networks()
        assert _names_passed(session.plan_network) == [FULL_OWNED]

    @pytest.mark.parametrize('plan', list(PLANS))
    def test_nothing_about_it_is_asked_of_the_provider(self, tmp_path, plan):
        """
        Every branch, not just the quiet one.

        With a fixed ``action: none`` the create, live and recreate arms never
        execute, so an implementation that reached for a foreign network while
        creating or starting a declared one would never be observed.
        """
        mgr, session = _manager(tmp_path, plan=plan)
        mgr.reconcile_networks()
        assert not _mentions(session, FOREIGN)

    def test_the_create_branch_really_runs(self, tmp_path):
        # control for the parametrisation above: proves 'create' is not a
        # no-op that would make its foreign-call assertion vacuous
        mgr, session = _manager(tmp_path, plan='create')
        assert mgr.reconcile_networks() == {FULL_OWNED: 'created'}
        assert _names_passed(session.define_network) == [FULL_OWNED]

    def test_the_live_branch_really_runs(self, tmp_path):
        mgr, session = _manager(tmp_path, plan='live')
        assert mgr.reconcile_networks() == {FULL_OWNED: 'updated'}
        assert _names_passed(session.start_network) == [FULL_OWNED]
        assert _names_passed(session.apply_network_live_plan) == [FULL_OWNED]

    @NO_NETWORKS
    @pytest.mark.parametrize('plan', list(PLANS))
    def test_an_adapter_alone_asks_the_provider_nothing_at_all(
            self, tmp_path, networks, plan):
        mgr, session = _adapter_only(tmp_path, networks)
        session.plan_network.return_value = PLANS[plan]
        assert mgr.reconcile_networks() == {}
        assert session.mock_calls == []

    def test_a_dry_run_does_not_look_at_it_either(self, tmp_path):
        mgr, session = _manager(tmp_path, plan='recreate')
        mgr.reconcile_networks(dry_run=True)
        assert not _mentions(session, FOREIGN)


class TestTheDestructiveBranchesToo:
    """
    Where a wrong "while we are here, clean up" belongs.

    `skipped` and the quiet plans are the easy arms. The arms that actually
    remove things -- an authorised recreate, and the error path of a failed
    definition -- are where extra cleanup would plausibly be written, and both
    return before their work with the fixtures above.
    """

    def test_an_authorized_recreate_acts_only_on_the_declared_network(
            self, tmp_path):
        # control: prove the destructive path really executes, so its
        # foreign-call guard below is not vacuous
        mgr, session = _manager(tmp_path, plan=RECREATE_AUTHORIZED)
        results = mgr.reconcile_networks(allow_recreate=True, auto_accept=True)

        assert results == {FULL_OWNED: 'recreated'}
        assert _names_passed(session.remove_network) == [FULL_OWNED]
        assert _names_passed(session.define_network) == [FULL_OWNED]
        session.reattach_domain_network.assert_called_once_with(
            RECREATE_AUTHORIZED['attached_vms'][0], FULL_OWNED)

    def test_an_authorized_recreate_asks_nothing_about_the_foreign_one(
            self, tmp_path):
        mgr, session = _manager(tmp_path, plan=RECREATE_AUTHORIZED)
        mgr.reconcile_networks(allow_recreate=True, auto_accept=True)
        assert not _mentions(session, FOREIGN)

    def test_an_authorized_dry_run_removes_nothing_at_all(self, tmp_path):
        # the other early return: authorised, but told not to do it
        mgr, session = _manager(tmp_path, plan=RECREATE_AUTHORIZED)
        results = mgr.reconcile_networks(
            dry_run=True, allow_recreate=True, auto_accept=True)
        assert results == {FULL_OWNED: 'skipped'}
        session.remove_network.assert_not_called()
        assert not _mentions(session, FOREIGN)

    @pytest.mark.parametrize('failure', [ConfigError('bad network block'),
                                         RuntimeError('libvirt said no')],
                             ids=['config-error', 'runtime-error'])
    def test_a_failed_definition_asks_nothing_about_the_foreign_one(
            self, tmp_path, failure):
        """
        `_define_network` catches both of these and returns 'failed'. Nothing
        exercised that arm, so a cleanup written into it was unobserved.
        """
        mgr, session = _manager(tmp_path, plan='create')
        session.define_network.side_effect = failure
        assert mgr.reconcile_networks() == {FULL_OWNED: 'failed'}
        assert not _mentions(session, FOREIGN)


# Every way a reconcile can go wrong, because "while we are here, tidy up" is
# written in recovery arms far more often than on the happy path. Each entry
# breaks one step and states what the declared network's outcome becomes: the
# outcome is the control (it proves the arm was actually taken) and the
# foreign-call guard is the claim.
def _removal_raises(session, _mgr):
    # remove_network destroys and undefines before it touches iptables, so the
    # network is already gone: the recreate carries on
    session.remove_network.side_effect = RuntimeError('iptables cleanup failed')


def _removal_refused(session, _mgr):
    session.remove_network.return_value = False


def _redefinition_refused(session, _mgr):
    session.define_network.return_value = False


def _reattachment_fails(session, _mgr):
    session.reattach_domain_network.return_value = 'failed'


def _cache_unregister_raises(_session, mgr):
    mgr.cache.unregister_network.side_effect = OSError('cache file is gone')


def _planning_raises(session, _mgr):
    session.plan_network.side_effect = ConfigError('bad dhcp reservation')


def _isolation_raises(session, _mgr):
    session.reconcile_network_isolation.side_effect = RuntimeError('no iptables')


RECOVERY_CASES = [
    # id, plan, break it, expected outcome, needs authorisation
    ('removal-raises', RECREATE_AUTHORIZED, _removal_raises, 'recreated', True),
    ('removal-refused', RECREATE_AUTHORIZED, _removal_refused, 'failed', True),
    ('redefinition-refused', RECREATE_AUTHORIZED, _redefinition_refused,
     'failed', True),
    ('reattachment-fails', RECREATE_AUTHORIZED, _reattachment_fails,
     'partial', True),
    ('cache-unregister-raises', RECREATE_AUTHORIZED, _cache_unregister_raises,
     'recreated', True),
    ('planning-raises', 'none', _planning_raises, 'failed', False),
    ('isolation-raises', 'none', _isolation_raises, 'failed', False),
]


class TestEveryRecoveryArmToo:
    """
    The arms reached only when something has already gone wrong.

    A reconcile that half-failed is exactly where an extra "clean up the other
    networks while we are here" would be written, and every one of these arms
    returns before the fixtures above could observe it.
    """

    @pytest.mark.parametrize(
        'plan,break_it,expected,authorised',
        [case[1:] for case in RECOVERY_CASES],
        ids=[case[0] for case in RECOVERY_CASES])
    def test_a_broken_step_still_asks_nothing_about_the_foreign_network(
            self, tmp_path, plan, break_it, expected, authorised):
        mgr, session = _manager(tmp_path, plan=plan)
        break_it(session, mgr)

        results = mgr.reconcile_networks(
            allow_recreate=authorised, auto_accept=authorised)

        # the control: this is the arm we meant to take
        assert results == {FULL_OWNED: expected}
        assert not _mentions(session, FOREIGN)

    def test_an_error_plan_asks_nothing_about_the_foreign_network(
            self, tmp_path):
        # 'error' returns before the plan is even described
        mgr, session = _manager(tmp_path, plan={'action': 'error'})
        assert mgr.reconcile_networks() == {FULL_OWNED: 'failed'}
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

    @NO_NETWORKS
    def test_an_adapter_alone_asks_the_provider_nothing_at_all(
            self, tmp_path, networks):
        mgr, session = _adapter_only(tmp_path, networks)
        assert mgr._reconcile_network_isolation() == {}
        assert session.mock_calls == []

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

    @NO_NETWORKS
    def test_an_adapter_alone_asks_the_provider_nothing_at_all(
            self, tmp_path, networks):
        mgr, session = _adapter_only(tmp_path, networks)
        assert mgr.destroy_networks() == {}
        assert session.mock_calls == []


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

        adapter = dict(GLOBAL_ADAPTER)
        mgr.resolve_adapter_network(adapter, 'cluster_1')
        assert adapter['network_source'] == FOREIGN
