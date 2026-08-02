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
        net.apply_net_update = lambda command, section, element: (
            seen.append((command, section, element)) or True)

        net.apply_live_plan({
            "host_ops": [("delete", {"mac": "aa", "ip": "1.1.1.2", "name": "n"})]})

        assert seen == [("delete", "ip-dhcp-host", "<host mac='aa'/>")]

    def test_add_and_modify_send_the_whole_element(self):
        net, _ = self._net_with_exec()
        seen = []
        net.apply_net_update = lambda command, section, element: (
            seen.append(element) or True)

        net.apply_live_plan({"host_ops": [
            ("add-last", {"mac": "aa", "ip": "1.1.1.2", "name": "n"}),
            ("modify", {"mac": "bb", "ip": "1.1.1.3"})]})

        assert seen == ["<host mac='aa' name='n' ip='1.1.1.2'/>",
                        "<host mac='bb' ip='1.1.1.3'/>"]

    def test_a_failing_update_is_reported(self):
        net = _network({"mode": "nat",
                        "ip": {"address": "10.5.3.1", "netmask": "255.255.255.0"}})
        net.execute = lambda *a, **k: (
            _result(stdout="demo\n") if a[0] == "net-list"
            else _result(ok=False, stderr="boom"))
        assert net.apply_live_plan(
            {"host_ops": [("add-last", {"mac": "aa", "ip": "1.1.1.2"})]}) is False
