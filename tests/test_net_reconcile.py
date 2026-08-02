"""
Unit tests for network reconciliation.

Two halves:

1. ``net_reconcile`` -- the pure diff between the configured network and the
   XML libvirt reports. No virsh involved.
2. ``Network.apply_live_plan`` / ``apply_net_update`` -- the command building
   for ``virsh net-update``, with the executor mocked.

The behaviour being encoded here was checked against libvirt 11.2.0: dhcp
hosts and ranges can be changed on a running network, the ip and bridge
sections cannot, and ``modify`` is refused for a range ("dhcp ranges cannot be
modified, only added or deleted").
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt import net_reconcile as nr
from boxman.providers.libvirt.net import Network


pytestmark = pytest.mark.unit


LIVE_XML = """
<network>
  <name>demo</name>
  <uuid>9f8e7d6c-0000-0000-0000-000000000000</uuid>
  <forward mode='nat'/>
  <bridge name='virbr9' stp='on' delay='0'/>
  <mac address='52:54:00:0A:0B:0C'/>
  <ip address='10.5.3.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='10.5.3.50' end='10.5.3.100'/>
      <host mac='52:54:00:00:00:01' name='one' ip='10.5.3.10'/>
    </dhcp>
  </ip>
</network>
"""


def _result(stdout: str = "", ok: bool = True, stderr: str = "") -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    return r


def _network(info: dict) -> Network:
    with patch.object(Network, "find_available_bridge_name",
                      return_value="virbr9"):
        return Network(name="demo", info=info, assign_new_bridge=True,
                       provider_config={"use_sudo": False})


class TestParseNetworkXml:

    def test_reads_every_compared_field(self):
        state = nr.parse_network_xml(LIVE_XML)
        assert state["mode"] == "nat"
        assert state["bridge_name"] == "virbr9"
        assert state["bridge_stp"] == "on"
        assert state["ip_address"] == "10.5.3.1"
        assert state["netmask"] == "255.255.255.0"
        assert state["dhcp_range"] == {"start": "10.5.3.50", "end": "10.5.3.100"}
        assert state["dhcp_hosts"] == [
            {"mac": "52:54:00:00:00:01", "ip": "10.5.3.10", "name": "one"}]

    def test_mac_is_lowercased(self):
        # libvirt echoes back whatever case it was given, boxman stores lower
        assert nr.parse_network_xml(LIVE_XML)["mac"] == "52:54:00:0a:0b:0c"

    def test_uuid_is_not_part_of_the_state(self):
        # it is regenerated on every definition and would read as permanent drift
        assert "uuid" not in nr.parse_network_xml(LIVE_XML)

    def test_prefix_is_normalised_to_a_netmask(self):
        xml = ("<network><name>d</name><forward mode='nat'/>"
               "<ip address='10.5.3.1' prefix='24'/></network>")
        assert nr.parse_network_xml(xml)["netmask"] == "255.255.255.0"

    def test_network_without_dhcp_or_ip(self):
        xml = "<network><name>d</name><forward mode='open'/></network>"
        state = nr.parse_network_xml(xml)
        assert state["dhcp_hosts"] == []
        assert state["dhcp_range"] is None
        assert state["ip_address"] is None


class TestDesiredState:

    def test_built_from_the_network_object(self):
        net = _network({
            "mode": "nat",
            "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0",
                   "dhcp": {"range": {"start": "10.5.3.50", "end": "10.5.3.100"},
                            "hosts": [{"mac": "52:54:00:00:00:01",
                                       "ip": "10.5.3.10", "name": "one"}]}},
        })
        state = nr.desired_state(net)
        assert state["mode"] == "nat"
        assert state["ip_address"] == "10.5.3.1"
        assert state["dhcp_range"] == {"start": "10.5.3.50", "end": "10.5.3.100"}
        assert state["dhcp_hosts"][0]["name"] == "one"

    def test_matches_what_libvirt_reports_for_the_same_config(self):
        # the round trip must be drift-free, otherwise every reconcile would
        # think the network had changed
        net = _network({
            "mode": "nat",
            "mac": "52:54:00:0a:0b:0c",
            "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0",
                   "dhcp": {"range": {"start": "10.5.3.50", "end": "10.5.3.100"},
                            "hosts": [{"mac": "52:54:00:00:00:01",
                                       "ip": "10.5.3.10", "name": "one"}]}},
        })
        plan = nr.diff_network(nr.desired_state(net),
                               nr.parse_network_xml(LIVE_XML))
        assert plan["action"] == "none"
        assert plan["structural"] == []


class TestDiffDhcpHosts:

    def test_added_reservation(self):
        ops = nr.diff_dhcp_hosts([{"mac": "aa", "ip": "1.1.1.1"}], [])
        assert ops == [("add-last", {"mac": "aa", "ip": "1.1.1.1"})]

    def test_removed_reservation(self):
        ops = nr.diff_dhcp_hosts([], [{"mac": "aa", "ip": "1.1.1.1"}])
        assert ops == [("delete", {"mac": "aa", "ip": "1.1.1.1"})]

    def test_moved_address_is_a_modify_not_a_remove_and_add(self):
        # libvirt keys on the mac, so this is one operation
        ops = nr.diff_dhcp_hosts([{"mac": "aa", "ip": "1.1.1.2"}],
                                 [{"mac": "aa", "ip": "1.1.1.1"}])
        assert ops == [("modify", {"mac": "aa", "ip": "1.1.1.2"})]

    def test_added_name_is_a_modify(self):
        ops = nr.diff_dhcp_hosts([{"mac": "aa", "ip": "1.1.1.1", "name": "n"}],
                                 [{"mac": "aa", "ip": "1.1.1.1"}])
        assert ops[0][0] == "modify"

    def test_identical_lists_produce_nothing(self):
        entries = [{"mac": "aa", "ip": "1.1.1.1", "name": "n"}]
        assert nr.diff_dhcp_hosts(entries, list(entries)) == []

    def test_deletions_are_ordered_before_additions(self):
        # so that an address being handed from one mac to another is free by
        # the time the new reservation asks for it
        ops = nr.diff_dhcp_hosts([{"mac": "bb", "ip": "1.1.1.1"}],
                                 [{"mac": "aa", "ip": "1.1.1.1"}])
        assert [command for command, _ in ops] == ["delete", "add-last"]


    def test_two_reservations_swapping_addresses_converge(self):
        # a modify cannot claim an address another surviving entry still holds,
        # so libvirt rejects it and the pair never converges; both have to be
        # deleted before either is added back
        ops = nr.diff_dhcp_hosts(
            [{"mac": "aa", "ip": "1.1.1.2"}, {"mac": "bb", "ip": "1.1.1.1"}],
            [{"mac": "aa", "ip": "1.1.1.1"}, {"mac": "bb", "ip": "1.1.1.2"}])
        commands = [command for command, _ in ops]
        assert commands == ["delete", "delete", "add-last", "add-last"]

    def test_an_uncontested_move_is_still_a_modify(self):
        ops = nr.diff_dhcp_hosts([{"mac": "aa", "ip": "1.1.1.9"}],
                                 [{"mac": "aa", "ip": "1.1.1.1"}])
        assert [command for command, _ in ops] == ["modify"]


class TestDiffDhcpRange:

    def test_change_becomes_delete_then_add(self):
        # libvirt: "dhcp ranges cannot be modified, only added or deleted"
        ops = nr.diff_dhcp_range({"start": "1.1.1.10", "end": "1.1.1.20"},
                                 {"start": "1.1.1.1", "end": "1.1.1.9"})
        assert [command for command, _ in ops] == ["delete", "add-last"]

    def test_unchanged_range_produces_nothing(self):
        same = {"start": "1.1.1.1", "end": "1.1.1.9"}
        assert nr.diff_dhcp_range(same, dict(same)) == []

    def test_range_added_to_a_reservations_only_network(self):
        ops = nr.diff_dhcp_range({"start": "1.1.1.1", "end": "1.1.1.9"}, None)
        assert [command for command, _ in ops] == ["add-last"]


class TestDiffNetwork:

    def test_dhcp_only_changes_stay_live(self):
        actual = nr.parse_network_xml(LIVE_XML)
        desired = dict(actual)
        desired["dhcp_hosts"] = [{"mac": "52:54:00:00:00:09", "ip": "10.5.3.30"}]
        assert nr.diff_network(desired, actual)["action"] == "live"

    @pytest.mark.parametrize("field, value", [
        ("mode", "route"),
        ("ip_address", "10.9.9.1"),
        ("netmask", "255.255.0.0"),
        ("bridge_stp", "off"),
        ("mac", "52:54:00:ff:ff:ff"),
    ])
    def test_structural_changes_force_a_recreate(self, field, value):
        actual = nr.parse_network_xml(LIVE_XML)
        desired = dict(actual)
        desired[field] = value
        plan = nr.diff_network(desired, actual)
        assert plan["action"] == "recreate"
        assert len(plan["structural"]) == 1

    def test_an_unpinned_bridge_name_is_not_drift(self):
        # boxman assigns virbrX when the config does not pin one, so libvirt is
        # authoritative and a difference here must not trigger a recreate
        actual = nr.parse_network_xml(LIVE_XML)
        desired = dict(actual)
        desired["bridge_name"] = None
        assert nr.diff_network(desired, actual)["action"] == "none"

    def test_a_pinned_bridge_name_is_drift(self):
        actual = nr.parse_network_xml(LIVE_XML)
        desired = dict(actual)
        desired["bridge_name"] = "virbr42"
        assert nr.diff_network(desired, actual)["action"] == "recreate"

    def test_structural_wins_over_live(self):
        # no point updating dnsmasq on a network about to be destroyed
        actual = nr.parse_network_xml(LIVE_XML)
        desired = dict(actual)
        desired["ip_address"] = "10.9.9.1"
        desired["dhcp_hosts"] = [{"mac": "52:54:00:00:00:09", "ip": "10.9.9.5"}]
        assert nr.diff_network(desired, actual)["action"] == "recreate"


class TestPinnedBridgeAndStp:
    """Fields whose configured form differs from what libvirt echoes back."""

    def test_pinned_bridge_name_reaches_the_diff(self, ):
        # the plan builds its Network with assign_new_bridge=False, which reads
        # the bridge from libvirt; the *configured* name has to survive that or
        # the pinned-name drift check can never fire
        info = {"mode": "nat", "bridge": {"name": "virbr42"},
                "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0"}}
        with patch.object(Network, "get_bridge_from_network",
                          return_value="virbr9"):
            net = Network(name="demo", info=info, assign_new_bridge=False,
                          provider_config={"use_sudo": False})
        assert net.bridge_name == "virbr9"          # what is in use
        assert nr.desired_state(net)["bridge_name"] == "virbr42"   # what was asked for

    def test_unpinned_bridge_name_is_not_reported_as_desired(self):
        info = {"mode": "nat",
                "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0"}}
        with patch.object(Network, "get_bridge_from_network",
                          return_value="virbr9"):
            net = Network(name="demo", info=info, assign_new_bridge=False,
                          provider_config={"use_sudo": False})
        assert nr.desired_state(net)["bridge_name"] is None

    def test_a_short_network_mac_does_not_read_as_drift(self):
        # libvirt zero-pads the network's own mac when it stores it, so a
        # short-form config would be permanent structural drift -- and with
        # --recreate-networks that rebuilds the network, rebooting its guests,
        # on every single run
        info = {"mode": "nat", "mac": "52:54:0:a:b:c",
                "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0",
                       "dhcp": {"range": {"start": "10.5.3.50",
                                          "end": "10.5.3.100"},
                                "hosts": [{"mac": "52:54:00:00:00:01",
                                           "ip": "10.5.3.10",
                                           "name": "one"}]}}}
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            net = Network(name="demo", info=info, assign_new_bridge=True,
                          provider_config={"use_sudo": False})
        assert net.mac_address == "52:54:00:0a:0b:0c"
        plan = nr.diff_network(nr.desired_state(net),
                               nr.parse_network_xml(LIVE_XML))
        assert plan["action"] == "none", plan["structural"]

    def test_a_short_reservation_mac_is_left_alone(self):
        # the opposite of the network mac: libvirt stores reservation macs
        # verbatim, so padding one here would create the mismatch
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            net = Network(
                name="demo", assign_new_bridge=True,
                provider_config={"use_sudo": False},
                info={"mode": "nat",
                      "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0",
                             "dhcp": {"hosts": [{"mac": "52:54:0:c:1:1",
                                                 "ip": "10.5.3.10"}]}}})
        assert net.dhcp_hosts[0]["mac"] == "52:54:0:c:1:1"

    def test_an_unrecognised_stp_value_is_rejected(self):
        # quietly turning `stp: enabled` into 'off' would disable stp silently
        with pytest.raises(ValueError, match="stp must be on or off"):
            Network(name="demo", info={"bridge": {"stp": "enabled"}},
                    assign_new_bridge=False,
                    provider_config={"use_sudo": False})

    @pytest.mark.parametrize("configured, expected", [
        (True, "on"), (False, "off"),          # yaml reads unquoted on/off as bool
        ("on", "on"), ("off", "off"),
        ("true", "on"), ("yes", "on"), (1, "on"), (0, "off"),
    ])
    def test_stp_is_normalised(self, configured, expected):
        assert Network._normalise_stp(configured) == expected

    def test_yaml_boolean_stp_does_not_read_as_drift(self):
        # `stp: on` unquoted is True in yaml; libvirt accepts stp='True' and
        # stores it as 'on'. Without normalising, that mismatch is permanent
        # drift and --recreate-networks would rebuild the network on every run
        info = {"mode": "nat", "bridge": {"stp": True, "delay": 0},
                "mac": "52:54:00:0a:0b:0c",
                "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0",
                       "dhcp": {"range": {"start": "10.5.3.50",
                                          "end": "10.5.3.100"},
                                "hosts": [{"mac": "52:54:00:00:00:01",
                                           "ip": "10.5.3.10",
                                           "name": "one"}]}}}
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            net = Network(name="demo", info=info, assign_new_bridge=True,
                          provider_config={"use_sudo": False})
        plan = nr.diff_network(nr.desired_state(net),
                               nr.parse_network_xml(LIVE_XML))
        assert plan["action"] == "none", plan["structural"]

    def test_numeric_reservation_name_does_not_read_as_drift(self):
        # `name: 101` is an int in yaml and would never equal the '101' read
        # back from the network XML
        info = {"mode": "nat",
                "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0",
                       "dhcp": {"hosts": [{"mac": "52:54:00:00:00:01",
                                           "ip": "10.5.3.10", "name": 101}]}}}
        with patch.object(Network, "find_available_bridge_name",
                          return_value="virbr9"):
            net = Network(name="demo", info=info, assign_new_bridge=True,
                          provider_config={"use_sudo": False})
        assert net.dhcp_hosts[0]["name"] == "101"


class TestTeardownInfo:
    """A recreate must withdraw the rules of the network that is there now."""

    @staticmethod
    def _teardown(actual, network_info):
        from boxman.manager import BoxmanManager
        return BoxmanManager._teardown_info({'actual': actual}, network_info)

    def test_uses_the_actual_mode_not_the_desired_one(self):
        # nat -> route: withdrawing route rules would leave the nat rules in
        # place and remove rules that were never added
        info = self._teardown(
            {'mode': 'nat', 'bridge_name': 'virbr9', 'ip_address': '10.5.3.1',
             'netmask': '255.255.255.0'},
            {'mode': 'route', 'ip': {'address': '10.9.9.1'}})
        assert info['mode'] == 'nat'
        assert info['ip']['address'] == '10.5.3.1'
        assert info['bridge']['name'] == 'virbr9'

    def test_no_dhcp_block_is_carried_over(self):
        # reservations validated against the new subnet would be rejected when
        # paired with the old address
        info = self._teardown(
            {'mode': 'nat', 'ip_address': '10.5.3.1', 'netmask': '255.255.255.0'},
            {'mode': 'nat', 'ip': {'address': '10.9.9.1', 'dhcp': {
                'hosts': [{'mac': '52:54:00:00:00:01', 'ip': '10.9.9.10'}]}}})
        assert 'dhcp' not in info['ip']

    def test_falls_back_to_the_configuration_when_nothing_is_known(self):
        network_info = {'mode': 'nat'}
        assert self._teardown({}, network_info) is network_info


class TestRecreateSequence:
    """
    The destructive path, with the provider and the cache stubbed.

    The ordering here is not cosmetic: ``define_network`` starts with
    ``check_network_exists()``, which walks every cached project *including
    this one*. A network's own leftover cache entry therefore collides with
    itself and the redefine raises -- after the network has already been
    destroyed, which wedges the project until the cache is edited by hand.
    """

    @staticmethod
    def _manager(remove=True, define=True, reattach='hot'):
        from boxman.manager import BoxmanManager

        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {'project': 'p1', 'clusters': {}}
        mgr.logger = MagicMock()

        calls = []

        mgr.cache = MagicMock()
        mgr.cache.projects = {'p1': {'networks': {'full-net': {'ip_address': '10.5.3.1'}}}}
        mgr.cache.read_projects_cache.side_effect = lambda: calls.append('cache-read')
        mgr.cache.write_projects_cache.side_effect = lambda: calls.append('cache-write')

        provider = MagicMock()
        provider.remove_network.side_effect = lambda **kw: (
            calls.append('remove') or remove)
        provider.define_network.side_effect = lambda **kw: (
            calls.append('define') or define)
        provider.reattach_domain_network.side_effect = lambda *a: (
            calls.append('reattach') or reattach)
        mgr.provider = provider

        return mgr, calls

    def _recreate(self, mgr, plan=None):
        return mgr._recreate_network(
            cluster={'workdir': '/tmp/wd'},
            network_name='cluster_1/mgmt',
            full_name='full-net',
            network_info={'mode': 'route'},
            plan=plan if plan is not None else {
                'structural': ["ip address '10.5.3.1' -> '10.9.9.1'"],
                'attached_vms': ['vm1'],
                'actual': {'mode': 'nat', 'ip_address': '10.5.3.1',
                           'netmask': '255.255.255.0'}},
            dry_run=False, allow_recreate=True, auto_accept=True)

    def test_cache_entry_is_dropped_before_the_redefine(self):
        mgr, calls = self._manager()
        assert self._recreate(mgr) == 'recreated'
        assert 'full-net' not in mgr.cache.projects['p1']['networks']
        assert calls.index('cache-write') < calls.index('define')
        assert calls.index('remove') < calls.index('define')

    def test_a_failed_removal_does_not_redefine_on_top(self):
        mgr, calls = self._manager(remove=False)
        assert self._recreate(mgr) == 'failed'
        assert 'define' not in calls

    def test_a_conflict_on_redefine_is_a_result_not_a_traceback(self):
        mgr, calls = self._manager()
        mgr.provider.define_network.side_effect = RuntimeError(
            "found 2 conflicts for network full-net")
        assert self._recreate(mgr) == 'failed'

    def test_a_vm_that_cannot_be_reconnected_is_not_reported_as_success(self):
        mgr, _ = self._manager(reattach='failed')
        assert self._recreate(mgr) == 'partial'

    def test_teardown_uses_the_actual_mode(self):
        mgr, _ = self._manager()
        self._recreate(mgr)
        info = mgr.provider.remove_network.call_args.kwargs['info']
        assert info['mode'] == 'nat'          # what is there, not the new 'route'

    def test_nothing_is_touched_without_the_flag(self):
        mgr, calls = self._manager()
        outcome = mgr._recreate_network(
            cluster={'workdir': '/tmp/wd'}, network_name='cluster_1/mgmt',
            full_name='full-net', network_info={'mode': 'route'},
            plan={'structural': ['x'], 'attached_vms': ['vm1'], 'actual': {}},
            dry_run=False, allow_recreate=False, auto_accept=True)
        assert outcome == 'skipped'
        assert calls == []

    def test_dry_run_touches_nothing(self):
        mgr, calls = self._manager()
        outcome = mgr._recreate_network(
            cluster={'workdir': '/tmp/wd'}, network_name='cluster_1/mgmt',
            full_name='full-net', network_info={'mode': 'route'},
            plan={'structural': ['x'], 'attached_vms': ['vm1'], 'actual': {}},
            dry_run=True, allow_recreate=True, auto_accept=True)
        assert outcome == 'skipped'
        assert calls == []

    def test_no_stdin_aborts_instead_of_raising(self):
        mgr, calls = self._manager()
        with patch('builtins.input', side_effect=EOFError):
            outcome = mgr._recreate_network(
                cluster={'workdir': '/tmp/wd'}, network_name='cluster_1/mgmt',
                full_name='full-net', network_info={'mode': 'route'},
                plan={'structural': ['x'], 'attached_vms': [], 'actual': {}},
                dry_run=False, allow_recreate=True, auto_accept=False)
        assert outcome == 'skipped'
        assert calls == []


class TestCacheSelfConflict:
    """
    The wedge: a network must not conflict with its own cache entry.

    ``define_network`` used to write the cache entry *before* ``net-define``,
    so a define that failed left the cache claiming a network that does not
    exist; ``check_network_exists`` then refused to create it on every later
    run, by name and by address, against itself. These drive the real
    ``BoxmanCache`` against a temporary cache file rather than a mock, because
    the mocked-provider tests cannot see inside ``define_network`` at all.
    """

    @staticmethod
    def _net_with_cache(tmp_path, cached: dict | None):
        from boxman.config_cache import BoxmanCache

        cache = BoxmanCache.__new__(BoxmanCache)
        cache.cache_dir = str(tmp_path)
        cache.projects_cache_file = str(tmp_path / 'projects.json')
        cache.projects = None
        if cached is not None:
            (tmp_path / 'projects.json').write_text(json.dumps(cached))

        manager = MagicMock()
        manager.cache = cache
        manager.config = {'project': 'p1'}
        manager._runtime_name = 'local'

        info = {"mode": "nat", "bridge": {"name": "virbr9"},
                "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0"}}
        net = Network(name="bprj__p1__bprj__clstr__c1__clstr__nat", info=info,
                      assign_new_bridge=True,
                      provider_config={"use_sudo": False}, manager=manager)
        return net, cache

    def test_a_networks_own_entry_is_not_a_conflict(self, tmp_path):
        # exactly the state a failed define, or a recreate, leaves behind
        net, _ = self._net_with_cache(tmp_path, {
            'p1': {'runtime': 'local', 'networks': {
                'bprj__p1__bprj__clstr__c1__clstr__nat': {
                    'ip_address': '10.5.3.1', 'bridge_name': 'virbr9'}}}})
        net.check_network_exists()      # must not raise

    def test_another_project_with_the_same_address_still_conflicts(self, tmp_path):
        net, _ = self._net_with_cache(tmp_path, {
            'p2': {'runtime': 'local', 'networks': {
                'bprj__p2__bprj__clstr__c1__clstr__nat': {
                    'ip_address': '10.5.3.1', 'bridge_name': 'virbr9'}}}})
        with pytest.raises(RuntimeError, match="conflict"):
            net.check_network_exists()

    def test_the_cache_is_written_only_after_the_define_succeeds(self, tmp_path):
        # a failed net-define must not leave an entry behind
        net, cache = self._net_with_cache(tmp_path, {'p1': {'runtime': 'local'}})

        def fake_execute(*args, **kwargs):
            if args[0] == "net-define":
                raise RuntimeError("boom")
            return _result()

        net.execute = fake_execute
        net._get_libvirt_bridges = lambda: set()
        assert net.define_network(str(tmp_path / 'net.xml')) is False

        cache.read_projects_cache()
        assert not cache.projects['p1'].get('networks')

    def test_a_bridge_already_in_use_raises_instead_of_exiting(self, tmp_path):
        # SystemExit would take the whole reconcile down, after a recreate has
        # already destroyed the previous network
        net, _ = self._net_with_cache(tmp_path, {'p1': {'runtime': 'local'}})
        net._get_libvirt_bridges = lambda: {"virbr9"}
        net._log_bridge_usage = lambda _: None
        with pytest.raises(RuntimeError, match="already in use"):
            net.define_network(str(tmp_path / 'net.xml'))


class TestElementRendering:

    def test_host_element_includes_the_optional_name(self):
        assert nr.host_element({"mac": "aa", "ip": "1.1.1.1", "name": "n"}) == \
            "<host mac='aa' name='n' ip='1.1.1.1'/>"

    def test_host_element_without_a_name(self):
        assert nr.host_element({"mac": "aa", "ip": "1.1.1.1"}) == \
            "<host mac='aa' ip='1.1.1.1'/>"

    def test_range_element(self):
        assert nr.range_element({"start": "1.1.1.1", "end": "1.1.1.9"}) == \
            "<range start='1.1.1.1' end='1.1.1.9'/>"

    def test_name_is_escaped(self):
        # the template escapes with |e, and this path has to match: a name
        # holding an & defines fine and would then break every net-update
        element = nr.host_element(
            {"mac": "aa", "ip": "1.1.1.1", "name": "a&b'c"})
        assert "a&amp;b&apos;c" in element
        ET.fromstring(element)   # parses, which is the whole point

    def test_a_numeric_value_is_rendered(self):
        assert nr.host_element({"mac": "aa", "ip": "1.1.1.1", "name": 101}) == \
            "<host mac='aa' name='101' ip='1.1.1.1'/>"


class TestApplyLivePlan:
    """The virsh net-update calls, with the executor mocked."""

    @staticmethod
    def _net_with_exec(active: bool = True):
        net = _network({"mode": "nat",
                        "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0"}})
        calls = []

        def fake_execute(*args, **kwargs):
            calls.append(args)
            if args[0] == "net-list":
                return _result(stdout="demo\n" if active else "")
            return _result()

        net.execute = fake_execute
        return net, calls

    def test_range_is_applied_before_the_reservations(self):
        net, calls = self._net_with_exec()
        net.apply_live_plan({
            "range_ops": [("add-last", {"start": "1.1.1.1", "end": "1.1.1.9"})],
            "host_ops": [("add-last", {"mac": "aa", "ip": "1.1.1.2"})],
        })
        sections = [args[3] for args in calls if args[0] == "net-update"]
        assert sections == ["ip-dhcp-range", "ip-dhcp-host"]

    def test_live_flag_only_when_the_network_is_running(self):
        net, calls = self._net_with_exec(active=True)
        net.apply_live_plan({"host_ops": [("add-last", {"mac": "aa", "ip": "1.1.1.2"})]})
        update = [args for args in calls if args[0] == "net-update"][0]
        assert "--config" in update and "--live" in update

    def test_a_stopped_network_is_only_updated_in_config(self):
        net, calls = self._net_with_exec(active=False)
        net.apply_live_plan({"host_ops": [("add-last", {"mac": "aa", "ip": "1.1.1.2"})]})
        update = [args for args in calls if args[0] == "net-update"][0]
        assert "--config" in update and "--live" not in update

    def test_delete_matches_on_the_mac_alone(self):
        # net-update matches every attribute it is given, so passing the whole
        # element would fail to match an entry whose ip already moved
        net, _ = self._net_with_exec()
        seen = []
        net.apply_net_update = lambda command, section, element, live=None: (
            seen.append((command, section, element)) or True)

        net.apply_live_plan({
            "host_ops": [("delete", {"mac": "aa", "ip": "1.1.1.2", "name": "n"})]})

        assert seen == [("delete", "ip-dhcp-host", "<host mac='aa'/>")]

    def test_add_and_modify_send_the_whole_element(self):
        net, _ = self._net_with_exec()
        seen = []
        net.apply_net_update = lambda command, section, element, live=None: (
            seen.append(element) or True)

        net.apply_live_plan({"host_ops": [
            ("add-last", {"mac": "aa", "ip": "1.1.1.2", "name": "n"}),
            ("modify", {"mac": "bb", "ip": "1.1.1.3"})]})

        assert seen == ["<host mac='aa' name='n' ip='1.1.1.2'/>",
                        "<host mac='bb' ip='1.1.1.3'/>"]

    def test_a_failed_range_delete_stops_the_add(self):
        # otherwise the network ends up with two ranges, and the diff only
        # reads the first -- so every later run tries to add the second again
        net, _ = self._net_with_exec()
        seen = []
        net.apply_net_update = lambda command, section, element, live=None: (
            seen.append(command) or command != 'delete')

        ok = net.apply_live_plan({"range_ops": [
            ("delete", {"start": "1.1.1.1", "end": "1.1.1.9"}),
            ("add-last", {"start": "1.1.1.10", "end": "1.1.1.20"})]})

        assert seen == ["delete"]
        assert ok is False

    def test_active_state_is_looked_up_once_per_plan(self):
        net, calls = self._net_with_exec()
        net.apply_live_plan({"host_ops": [
            ("add-last", {"mac": "aa", "ip": "1.1.1.1"}),
            ("add-last", {"mac": "bb", "ip": "1.1.1.2"}),
            ("add-last", {"mac": "cc", "ip": "1.1.1.3"})]})
        assert len([args for args in calls if args[0] == "net-list"]) == 1

    def test_a_failing_update_is_reported(self):
        net = _network({"mode": "nat",
                        "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0"}})
        net.execute = lambda *a, **k: (
            _result(stdout="demo\n") if a[0] == "net-list"
            else _result(ok=False, stderr="boom"))
        assert net.apply_live_plan(
            {"host_ops": [("add-last", {"mac": "aa", "ip": "1.1.1.2"})]}) is False
