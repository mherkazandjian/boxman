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

import shlex
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import NetworkError
from boxman.manager import BoxmanManager
from boxman.providers.libvirt.net import Network
from boxman.providers.libvirt.session import LibVirtSession
from fake_libvirt_host import Result
from test_libvirt_net import _installed

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
    mgr.provider.live_network_state.return_value = {
        "mode": "nat", "bridge_name": "virbr2",
        "ip_address": "10.9.0.1", "netmask": "255.255.255.0"}
    mgr.provider.network_bridges.return_value = {}
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
        attached" and lets the removal proceed. Under --prune-networks the
        removal that was asked for could not be done safely, so the run
        fails rather than reporting a skip."""
        mgr = self._orphaned()
        mgr.provider.network_attached_domains.side_effect = NetworkError("down")
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.provider.remove_network.assert_not_called()
        mgr.cache.unregister_network.assert_not_called()

    def test_without_the_flag_an_unanswerable_question_is_only_reported(self):
        mgr = self._orphaned()
        mgr.provider.network_attached_domains.side_effect = NetworkError("down")
        results = mgr._prune_orphaned_networks(prune=False)
        assert results == {f"{PREFIX}gone": "skipped"}
        assert any("could not be determined" in str(call)
                   for call in mgr.logger.warning.call_args_list)

    def test_a_dry_run_with_an_unanswerable_question_is_not_a_failure(self):
        mgr = self._orphaned()
        mgr.provider.network_attached_domains.side_effect = NetworkError("down")
        results = mgr._prune_orphaned_networks(dry_run=True, prune=True)
        assert results == {f"{PREFIX}gone": "skipped"}
        mgr.provider.remove_network.assert_not_called()

    def test_a_provider_that_cannot_report_attachments_blocks_removal(self):
        mgr = self._orphaned()
        del mgr.provider.network_attached_domains
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.provider.remove_network.assert_not_called()


class TestTheModeComesFromTheLiveNetwork:
    """The cache records an address and a bridge, not a forward mode — and
    the mode decides whether removal also tears down iptables rules."""

    def _orphaned(self):
        return _manager(declared={},
                        provisioned={f"{PREFIX}gone": {"bridge_name": "virbr2"}})

    def test_the_live_mode_and_bridge_are_passed_to_the_removal(self):
        """The bridge is passed pinned, so ``Network`` does not look it up
        again on its own -- a lookup that answers a failure with None."""
        mgr = self._orphaned()
        mgr.provider.live_network_state.return_value = {
            "mode": "route", "bridge_name": "virbr7"}
        mgr._prune_orphaned_networks(prune=True)
        info = mgr.provider.remove_network.call_args.kwargs["info"]
        assert info["mode"] == "route"
        assert info["bridge"] == {"name": "virbr7"}

    def test_the_live_mode_is_recorded_before_anything_is_torn_down(self):
        mgr = self._orphaned()
        mgr.provider.live_network_state.return_value = {
            "mode": "route", "bridge_name": "virbr7"}
        record = mgr.cache.projects[PROJECT]["networks"][f"{PREFIX}gone"]
        seen = {}
        mgr.provider.remove_network.side_effect = (
            lambda **_kw: seen.update(record) or False)
        mgr._prune_orphaned_networks(prune=True)
        assert seen["mode"] == "route"
        assert seen["bridge_name"] == "virbr7"
        mgr.cache.write_projects_cache.assert_called()

    def test_an_unreadable_live_definition_stops_the_removal(self):
        """Leaving the mode out does not leave it unset: ``Network`` takes a
        missing mode for "nat", which skips the route-mode teardown -- the
        iptables leak this fix is about (Copilot review)."""
        mgr = self._orphaned()
        mgr.provider.live_network_state.side_effect = NetworkError("no xml")
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.provider.remove_network.assert_not_called()
        mgr.cache.unregister_network.assert_not_called()

    def test_a_provider_without_a_live_definition_stops_the_removal(self):
        mgr = self._orphaned()
        del mgr.provider.live_network_state
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.provider.remove_network.assert_not_called()

    def test_a_definition_without_a_forward_mode_stops_the_removal(self):
        mgr = self._orphaned()
        mgr.provider.live_network_state.return_value = {
            "mode": None, "bridge_name": "virbr2"}
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.provider.remove_network.assert_not_called()

    def test_a_routed_definition_without_a_bridge_stops_the_removal(self):
        """Its isolation chains are named after the bridge; without one the
        teardown would find nothing and report success."""
        mgr = self._orphaned()
        mgr.provider.live_network_state.return_value = {
            "mode": "route", "bridge_name": None}
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.provider.remove_network.assert_not_called()

    def test_a_mode_that_cannot_be_recorded_stops_the_removal(self):
        mgr = self._orphaned()
        mgr.cache.write_projects_cache.side_effect = OSError("read-only")
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.provider.remove_network.assert_not_called()


class TestANetworkLibvirtNoLongerHas:
    """Nothing is left in libvirt to read the mode from, so it comes from
    the cache record an earlier, interrupted removal left -- or not at
    all."""

    @staticmethod
    def _orphaned(record: dict):
        mgr = _manager(declared={}, provisioned={f"{PREFIX}gone": record})
        mgr.provider.live_network_state.return_value = None
        return mgr

    def test_without_a_recorded_mode_it_is_not_forgotten(self):
        mgr = self._orphaned({"bridge_name": "virbr2"})
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.provider.remove_network.assert_not_called()
        mgr.cache.unregister_network.assert_not_called()

    def test_a_recorded_route_mode_finishes_the_teardown(self):
        mgr = self._orphaned({"bridge_name": "virbr2", "mode": "route"})
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "removed"}
        info = mgr.provider.remove_network.call_args.kwargs["info"]
        assert info == {"mode": "route", "bridge": {"name": "virbr2"}}

    def test_a_recorded_nat_mode_needs_no_bridge_check(self):
        mgr = self._orphaned({"bridge_name": "virbr2", "mode": "nat"})
        mgr.provider.network_bridges.side_effect = NetworkError("down")
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "removed"}
        info = mgr.provider.remove_network.call_args.kwargs["info"]
        assert info["mode"] == "nat"

    def test_a_bridge_another_network_now_holds_is_left_alone(self):
        """The isolation chains are keyed on the bridge name: withdrawing
        them would withdraw the new holder's isolation."""
        mgr = self._orphaned({"bridge_name": "virbr2", "mode": "route"})
        mgr.provider.network_bridges.return_value = {"newcomer": "virbr2"}
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.provider.remove_network.assert_not_called()
        assert any("newcomer" in str(call)
                   for call in mgr.logger.error.call_args_list)

    def test_an_unanswerable_bridge_question_stops_the_removal(self):
        mgr = self._orphaned({"bridge_name": "virbr2", "mode": "route"})
        mgr.provider.network_bridges.side_effect = NetworkError("down")
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.provider.remove_network.assert_not_called()

    def test_a_recorded_route_without_a_bridge_stops_the_removal(self):
        mgr = self._orphaned({"mode": "route"})
        results = mgr._prune_orphaned_networks(prune=True)
        assert results == {f"{PREFIX}gone": "failed"}
        mgr.provider.remove_network.assert_not_called()


# -- the removal path against libvirt, at the shell boundary ------------------
#
# The tests above stub the provider. These run the real LibVirtSession and
# Network code and fake only the shell under them, so a virsh failure is what
# it is on a real host -- a non-zero exit with an error on stderr -- and it
# travels through VirshCommand.execute's own handling of it.

SHELL_RUN = "boxman.providers.libvirt.commands._shell_run"
GONE = f"{PREFIX}gone"


def _domiflist(rows: list[tuple[str, str]]) -> str:
    table = (" Interface   Type      Source    Model    MAC\n"
             "------------------------------------------------------\n")
    table += "".join(
        f" vnet{i}   {kind}   {source}   virtio   52:54:00:00:00:{i:02x}\n"
        for i, (kind, source) in enumerate(rows))
    return table + "\n"


class NetHost:
    """
    A libvirt host's networks and domains, plus an iptables filter table,
    answering the commands boxman hands to the shell.

    A virsh subcommand in :attr:`fail`, or a ``(subcommand, first argument)``
    pair in :attr:`fail_for`, exits 1 with an error on stderr, as virsh does
    when libvirtd is unreachable or a domain vanished mid-scan: nothing here
    answers a failure with an empty listing. ``net-undefine`` on a running
    network leaves it running, as a transient network, as libvirt does.
    """

    def __init__(self, iptables=None):
        #: name -> {"mode", "bridge", "active", "persistent"}
        self.networks: dict[str, dict] = {}
        #: domain -> its interfaces, as (type, source)
        self.domains: dict[str, list[tuple[str, str]]] = {}
        self.fail: set[str] = set()
        self.fail_for: set[tuple[str, str]] = set()
        #: a callable answering iptables command lines (FakeIptables)
        self.iptables = iptables
        #: name -> what ``net-dumpxml`` prints for it instead of its XML
        self.xml: dict[str, str] = {}
        #: every command, as argv, sudo stripped
        self.log: list[list[str]] = []

    def define_network(self, name: str, mode: str | None = "route",
                       bridge: str | None = "virbr2",
                       active: bool = True) -> None:
        self.networks[name] = {"mode": mode, "bridge": bridge,
                               "active": active, "persistent": True}

    def ran(self, sub: str) -> list[list[str]]:
        return [argv[4:] for argv in self.log
                if argv[0] == "virsh" and argv[3] == sub]

    def run(self, command: str, **_kwargs):
        if command.startswith("sudo "):
            command = command[len("sudo "):]
        argv = shlex.split(command)
        self.log.append(argv)
        if argv[0] == "iptables":
            if self.iptables is None:
                raise AssertionError(f"unexpected firewall command: {command}")
            return self.iptables(command)
        assert argv[0] == "virsh", command
        sub, args = argv[3], argv[4:]
        if sub in self.fail or (sub, args[0] if args else "") in self.fail_for:
            return Result(stderr=f"error: failed to connect to the hypervisor "
                                 f"({sub})\n", code=1)
        return getattr(self, "_" + sub.replace("-", "_"))(args)

    def _missing(self, name: str) -> Result:
        return Result(stderr=f"error: failed to get network '{name}'\n"
                             f"error: Network not found\n", code=1)

    def _net_list(self, args):
        names = [name for name, net in sorted(self.networks.items())
                 if "--all" in args or net["active"]]
        return Result(stdout="".join(f"{name}\n" for name in names) + "\n")

    def _net_dumpxml(self, args):
        net = self.networks.get(args[0])
        if net is None:
            return self._missing(args[0])
        if args[0] in self.xml:
            return Result(stdout=self.xml[args[0]])
        forward = f"<forward mode='{net['mode']}'/>" if net["mode"] else ""
        bridge = (f"<bridge name='{net['bridge']}' stp='on' delay='0'/>"
                  if net["bridge"] else "")
        return Result(stdout=(
            f"<network><name>{args[0]}</name>{forward}{bridge}"
            f"<ip address='10.9.0.1' netmask='255.255.255.0'/></network>\n"))

    def _net_destroy(self, args):
        net = self.networks.get(args[0])
        if net is None or not net["active"]:
            return Result(stderr=f"error: network '{args[0]}' is not active\n",
                          code=1)
        net["active"] = False
        if not net["persistent"]:
            del self.networks[args[0]]
        return Result(stdout=f"Network {args[0]} destroyed\n")

    def _net_autostart(self, args):
        return Result() if args[0] in self.networks else self._missing(args[0])

    def _net_undefine(self, args):
        net = self.networks.get(args[0])
        if net is None or not net["persistent"]:
            return self._missing(args[0])
        if net["active"]:
            net["persistent"] = False
        else:
            del self.networks[args[0]]
        return Result(stdout=f"Network {args[0]} has been undefined\n")

    def _list(self, args):
        return Result(stdout="".join(f"{name}\n"
                                     for name in sorted(self.domains)) + "\n")

    def _domiflist(self, args):
        rows = self.domains.get(args[0])
        if rows is None:
            return Result(stderr=f"error: failed to get domain '{args[0]}'\n",
                          code=1)
        return Result(stdout=_domiflist(rows))


def _live_manager(provisioned: dict | None = None) -> BoxmanManager:
    """A manager whose provider is a real LibVirtSession, and whose cache
    forgets an entry when asked to."""
    mgr = _manager(declared={}, provisioned=provisioned or {
        GONE: {"bridge_name": "virbr2", "ip_address": "10.9.0.1"}})
    session = LibVirtSession(config={"provider": {"libvirt": {
        "use_sudo": False, "uri": "qemu:///system"}}})
    session.manager = mgr
    mgr.provider = session
    networks = mgr.cache.projects[PROJECT]["networks"]
    mgr.cache.unregister_network.side_effect = (
        lambda _project, name: networks.pop(name, None) is not None)
    return mgr


def _prune(mgr: BoxmanManager, host: NetHost) -> dict[str, str]:
    with patch(SHELL_RUN, side_effect=host.run):
        return mgr._prune_orphaned_networks(prune=True)


class TestAnUnreadableModeStopsTheRemoval:
    """Copilot review, finding 1: a mode left out of ``info`` is not "no
    mode" -- ``Network`` defaults it to ``nat``, which skips the route
    teardown, and the cache entry was then forgotten anyway."""

    def test_a_defined_network_whose_xml_cannot_be_read_is_not_removed(self):
        host = NetHost()
        host.define_network(GONE, mode="route")
        host.fail_for.add(("net-dumpxml", GONE))
        mgr = _live_manager()

        results = _prune(mgr, host)

        assert results == {GONE: "failed"}
        assert host.ran("net-destroy") == []
        assert host.ran("net-undefine") == []
        assert host.networks[GONE]["persistent"]
        assert GONE in mgr.cache.projects[PROJECT]["networks"]

    def test_an_interrupted_route_removal_finishes_on_the_next_run(self):
        """The removal undefines the network before it withdraws the
        isolation rules, so a teardown refused half way leaves no definition
        to read the mode from. The next run must still finish the job, not
        forget the entry with the chains left in the firewall."""
        table = _installed("virbr2",
                           refuse={"iptables -X BXM_ISO_I_virbr2": [4, 0]})
        host = NetHost(iptables=table)
        host.define_network(GONE, mode="route", bridge="virbr2")
        mgr = _live_manager()

        assert _prune(mgr, host) == {GONE: "failed"}
        assert GONE not in host.networks
        assert table.mentions("virbr2")
        assert GONE in mgr.cache.projects[PROJECT]["networks"]

        assert _prune(mgr, host) == {GONE: "removed"}
        assert table.mentions("virbr2") == []
        assert GONE not in mgr.cache.projects[PROJECT]["networks"]


class TestAnUnanswerableAttachmentStopsTheRemoval:
    """Copilot review, finding 2: ``attached_domains()`` answered a failed
    ``virsh list`` with ``[]`` and skipped a domain whose ``domiflist``
    failed, so "could not ask" read as "nothing is attached"."""

    def test_a_failed_domain_listing_is_not_an_empty_one(self):
        host = NetHost()
        host.define_network(GONE, mode="nat")
        host.domains["node01"] = [("network", GONE)]
        host.fail.add("list")
        mgr = _live_manager()

        results = _prune(mgr, host)

        assert results[GONE] != "removed"
        assert host.ran("net-destroy") == []
        assert host.ran("net-undefine") == []

    def test_a_guest_whose_interfaces_cannot_be_read_is_not_ignored(self):
        host = NetHost()
        host.define_network(GONE, mode="nat")
        host.domains["node01"] = [("network", GONE)]
        host.fail_for.add(("domiflist", "node01"))
        mgr = _live_manager()

        results = _prune(mgr, host)

        assert results[GONE] != "removed"
        assert host.ran("net-destroy") == []
        assert host.ran("net-undefine") == []


class TestARunningNetworkIsNotUndefinedBlind:
    """``destroy_network`` read a failed "which networks are running?" as
    "not running", skipped ``net-destroy`` and let ``net-undefine`` run: on a
    running network that only makes it transient. It keeps its bridge, and
    with the cache entry then forgotten nothing records it any more."""

    def test_an_unanswerable_running_check_stops_before_the_undefine(self):
        host = NetHost()
        host.define_network(GONE, mode="nat", active=True)
        # only the running-networks listing fails; `net-list --all` answers
        host.fail_for.add(("net-list", "--name"))
        mgr = _live_manager()

        results = _prune(mgr, host)

        assert results == {GONE: "failed"}
        assert host.ran("net-undefine") == []
        assert host.networks[GONE]["persistent"]
        assert GONE in mgr.cache.projects[PROJECT]["networks"]


def _ask(host: NetHost, call: str, *args):
    """Call the session method *call* with every command answered by
    *host*."""
    session = LibVirtSession(config={"provider": {"libvirt": {
        "use_sudo": False, "uri": "qemu:///system"}}})
    session.manager = MagicMock()
    with patch(SHELL_RUN, side_effect=host.run):
        return getattr(session, call)(*args)


class TestWhatTheSessionSaysIsAttached:
    """``network_attached_domains`` either answers or raises: a failure is
    never an empty list."""

    def test_the_attached_guests_are_named(self):
        host = NetHost()
        host.define_network(GONE)
        host.domains = {"node01": [("network", GONE)],
                        "node02": [("network", "elsewhere")],
                        "node03": [("bridge", "br0"), ("network", GONE)]}
        assert _ask(host, "network_attached_domains", GONE) == [
            "node01", "node03"]

    def test_a_failed_domain_listing_raises(self):
        host = NetHost()
        host.define_network(GONE)
        host.fail.add("list")
        with pytest.raises(NetworkError, match="could not list the domains"):
            _ask(host, "network_attached_domains", GONE)

    def test_a_failed_interface_listing_raises_naming_the_guest(self):
        host = NetHost()
        host.define_network(GONE)
        host.domains = {"node01": [("network", "elsewhere")],
                        "node02": [("network", GONE)]}
        host.fail_for.add(("domiflist", "node02"))
        with pytest.raises(NetworkError, match="node02"):
            _ask(host, "network_attached_domains", GONE)

    def test_the_recreate_prompt_keeps_its_lenient_answer(self):
        """``plan_network`` lists the guests a recreate would disconnect
        through the default, which still answers what it could read."""
        host = NetHost()
        host.fail.add("list")
        with patch(SHELL_RUN, side_effect=host.run):
            network = Network(GONE, info={"bridge": {"name": "virbr2"}},
                              assign_new_bridge=False,
                              provider_config={"use_sudo": False})
            assert network.attached_domains() == []


class TestWhatTheSessionSaysIsDefined:
    """``live_network_state`` and ``network_bridges`` either answer or
    raise: a failure is never "not defined" or "no bridge"."""

    def test_a_defined_network_is_read_from_its_live_xml(self):
        host = NetHost()
        host.define_network(GONE, mode="route", bridge="virbr4")
        state = _ask(host, "live_network_state", GONE)
        assert state["mode"] == "route"
        assert state["bridge_name"] == "virbr4"

    def test_a_network_that_is_not_defined_is_none(self):
        host = NetHost()
        host.define_network("someone-else")
        assert _ask(host, "live_network_state", GONE) is None

    def test_a_failed_network_listing_is_not_absence(self):
        host = NetHost()
        host.define_network(GONE)
        host.fail.add("net-list")
        with pytest.raises(NetworkError, match="could not list"):
            _ask(host, "live_network_state", GONE)

    def test_an_unreadable_definition_raises(self):
        host = NetHost()
        host.define_network(GONE)
        host.fail_for.add(("net-dumpxml", GONE))
        with pytest.raises(NetworkError, match="could not be read"):
            _ask(host, "live_network_state", GONE)

    def test_an_unparseable_definition_raises(self):
        host = NetHost()
        host.define_network(GONE)
        host.xml[GONE] = "<network><forward mode='route'"
        with pytest.raises(NetworkError, match="could not be parsed"):
            _ask(host, "live_network_state", GONE)

    def test_every_defined_network_bridge_is_reported(self):
        host = NetHost()
        host.define_network("a", bridge="virbr1")
        host.define_network("b", bridge="virbr2", active=False)
        assert _ask(host, "network_bridges") == {
            "a": "virbr1", "b": "virbr2"}

    def test_a_bridge_listing_that_misses_one_network_raises(self):
        host = NetHost()
        host.define_network("a", bridge="virbr1")
        host.define_network("b", bridge="virbr2")
        host.fail_for.add(("net-dumpxml", "b"))
        with pytest.raises(NetworkError):
            _ask(host, "network_bridges")

    def test_a_failed_bridge_listing_raises(self):
        host = NetHost()
        host.fail.add("net-list")
        with pytest.raises(NetworkError):
            _ask(host, "network_bridges")


class TestAnInterruptedRemovalLeavesOthersAlone:

    def test_a_bridge_taken_since_keeps_its_new_owners_isolation(self):
        table = _installed("virbr2",
                           refuse={"iptables -X BXM_ISO_I_virbr2": [4, 0]})
        host = NetHost(iptables=table)
        host.define_network(GONE, mode="route", bridge="virbr2")
        mgr = _live_manager()
        assert _prune(mgr, host) == {GONE: "failed"}

        # a network pinned to virbr2 is defined in the meantime, and its
        # isolation is installed on the same names
        host.define_network("newcomer", mode="route", bridge="virbr2")
        fresh = _installed("virbr2")
        table.chains, table.rules = fresh.chains, fresh.rules

        assert _prune(mgr, host) == {GONE: "failed"}
        assert table.rule("INPUT", "-i virbr2 -j BXM_ISO_I_virbr2")
        assert table.rule("BXM_ISO_O_virbr2", "-j DROP")
        assert GONE in mgr.cache.projects[PROJECT]["networks"]

    def test_a_network_removed_by_hand_without_a_record_is_kept(self):
        host = NetHost()
        mgr = _live_manager()
        assert _prune(mgr, host) == {GONE: "failed"}
        assert GONE in mgr.cache.projects[PROJECT]["networks"]
        assert host.ran("net-destroy") == []
