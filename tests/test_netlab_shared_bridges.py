"""Unit tests for boxman.netlab.shared_bridges.

Covers idempotency, sudo-prefixed commands, STP/netfilter knobs, and
the ``is_shared_bridge`` / ``resolve_bridge`` helpers used by the
manager to resolve an adapter's ``network_source`` against the
top-level ``shared_networks:`` block.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from invoke.exceptions import UnexpectedExit
from invoke.runners import Result

from boxman.exceptions import ConfigError
from boxman.netlab import shared_bridges

pytestmark = pytest.mark.unit

#: The real decision function, captured before the autouse fixture below
#: replaces the module attribute with a mock.
_REAL_NEEDS_SUDO = shared_bridges._needs_sudo


@pytest.fixture(autouse=True)
def _unprivileged_by_default():
    """Pin the privilege decision the sudo prefix is derived from.

    ``shared_bridges`` decides on ``sudo`` from the running process, so without
    this every ``sudo``-prefix assertion below would quietly depend on who runs
    pytest -- passing on a workstation and failing in a root CI container.

    The seam is ``_needs_sudo`` rather than ``os.geteuid`` so the patch stays
    inside the module under test instead of mutating a stdlib global for every
    test in the file. ``_needs_sudo``'s own mapping from euid is covered
    directly by ``test_needs_sudo_is_exactly_not_root``.
    """
    with patch("boxman.netlab.shared_bridges._needs_sudo", return_value=True):
        yield


def _result(ok: bool = True) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.ok = ok
    r.failed = not ok
    return r


def _recorder(bridge_exists: bool = False,
              rule_present: bool = False,
              docker_user: bool = True,
              fails: tuple[str, ...] = ()):
    """A ``run`` stub plus the list it appends to.

    A factory rather than a closure written inline, so a stub built inside a
    loop binds its own list instead of whatever the loop variable happens to
    hold when it is finally called (ruff B023).

    The defaults put ``ensure()`` on its *longest* path -- bridge absent so it
    is created, accept rule absent so it is inserted, ``DOCKER-USER`` present
    so that chain is handled too. An earlier version answered ok to every
    ``iptables`` call, which meant the ``-C`` check always reported the rule
    already present and **the insertion was never executed by any test**: a
    privilege mutation on ``iptables -I`` passed the entire suite.

    It also **honours ``warn``**, raising :class:`UnexpectedExit` on a failed
    command exactly as :func:`invoke.run` does. Without that, the ``warn``
    argument of every call site is untested, and it is ``warn`` that decides
    whether a failure stops the run or is quietly stepped over: flipping the
    ``-C`` probe to ``warn=False`` (which would abort before any rule is ever
    inserted) or giving ``_run_sudo`` a ``warn=True`` (which would let a
    failed bridge create sail on) both passed the entire suite before this.

    *fails* holds command substrings that should come back non-zero, so a test
    can make one specific privileged write fail.
    """
    calls: list[str] = []

    def fake_run(cmd, warn=False, **_kwargs):
        calls.append(cmd)
        # the stub is asked about the command, not about its privilege
        base = cmd[len("sudo "):] if cmd.startswith("sudo ") else cmd
        if base.startswith("ip link show"):
            ok = bridge_exists
        elif "iptables -t filter -C " in base:
            ok = rule_present
        elif "iptables -t filter -n -L " in base:
            ok = docker_user
        else:
            ok = not any(f in base for f in fails)
        if not ok and not warn:
            raise UnexpectedExit(Result(command=cmd, exited=1))
        return _result(ok=ok)

    return calls, fake_run


def _is_privileged(cmd: str) -> bool:
    """Every command the module runs needs root except the presence probe."""
    return not cmd.startswith("ip link show")


def _carries_prefix(cmd: str) -> bool:
    """``sudo`` leads the command, or follows the pipe in ``echo x | sudo tee``."""
    return cmd.startswith("sudo ") or "| sudo tee " in cmd


class TestEnsure:

    def test_noop_when_empty(self):
        with patch("boxman.netlab.shared_bridges.run") as run:
            shared_bridges.ensure(None)
            shared_bridges.ensure({})
            run.assert_not_called()

    def test_creates_missing_bridge(self):
        calls, fake_run = _recorder()          # bridge absent -> created

        cfg = {"lab_mgmt": {"bridge": "shared_lab_mgmt"}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run):
            with patch("pathlib.Path.exists", return_value=False):
                shared_bridges.ensure(cfg)

        # Must include: presence probe, add (since absent), set up, stp_state
        assert any("ip link show dev shared_lab_mgmt" in c for c in calls)
        assert any("sudo ip link add name shared_lab_mgmt type bridge" in c
                   for c in calls)
        assert any("sudo ip link set dev shared_lab_mgmt up" in c for c in calls)
        assert any("stp_state 0" in c for c in calls)

    def test_skips_add_when_bridge_exists(self):
        calls, fake_run = _recorder(bridge_exists=True)

        cfg = {"lab_mgmt": {"bridge": "shared_lab_mgmt"}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run):
            with patch("pathlib.Path.exists", return_value=False):
                shared_bridges.ensure(cfg)

        all_cmds = " | ".join(calls)
        assert "ip link add name shared_lab_mgmt" not in all_cmds
        # Still ensures it's up, which is idempotent on an existing bridge.
        assert "ip link set dev shared_lab_mgmt up" in all_cmds

    def test_stp_on_sets_state_1(self):
        calls, fake_run = _recorder(bridge_exists=True)

        cfg = {"lab_mgmt": {"bridge": "br_stp", "stp": True}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run):
            with patch("pathlib.Path.exists", return_value=False):
                shared_bridges.ensure(cfg)

        assert any("stp_state 1" in c for c in calls)

    def test_stp_not_touched_when_absent_on_an_existing_bridge(self):
        """An entry that omits `stp:` must not write it.

        Bridge names are global and not namespaced, so writing the default on
        every run is not a no-op: it would switch STP off for every other
        project sharing the bridge (#162).
        """
        cfg = {"lab_mgmt": {"bridge": "br1"}}
        with patch("boxman.netlab.shared_bridges.run",
                   side_effect=self._fake_run()) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)

        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert "stp_state" not in all_cmds

    def test_stp_initialised_off_on_a_bridge_boxman_creates(self):
        """A bridge boxman creates still gets a defined initial state."""
        calls, fake_run = _recorder()         # absent -> created by this run

        cfg = {"lab_mgmt": {"bridge": "br1"}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run):
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)

        assert "stp_state 0" in " | ".join(calls)

    def test_stp_false_declared_is_still_applied(self):
        """An explicit `stp: false` is an opinion and must be written."""
        cfg = {"lab_mgmt": {"bridge": "br1", "stp": False}}
        with patch("boxman.netlab.shared_bridges.run",
                   side_effect=self._fake_run()) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)

        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert "stp_state 0" in all_cmds

    @pytest.mark.parametrize("value,expected", [
        (True, 1), ("on", 1), ("true", 1), ("yes", 1), ("1", 1),
        (False, 0), ("off", 0), ("false", 0), ("no", 0), ("0", 0),
        # case and surrounding whitespace: _normalise_bool strips and lowers
        # before matching, and a config file written by hand has both. Without
        # these, deleting either .strip() or .lower() passes the whole suite --
        # and the failure is not benign: `stp: "OFF"` would raise ConfigError
        # rather than switching STP off.
        ("ON", 1), ("On", 1), ("TRUE", 1), ("Yes", 1),
        ("OFF", 0), ("Off", 0), ("FALSE", 0), ("No", 0),
        (" on ", 1), ("\ttrue\n", 1), (" off ", 0), ("  no  ", 0),
        (" OFF ", 0), (" On ", 1),
    ])
    def test_stp_spellings_normalised(self, value, expected):
        """A quoted `stp: "off"` must not switch STP on.

        Plain truthiness would: a non-empty "off" is truthy. Same accepted
        spellings as a libvirt network's `bridge.stp`, including case and
        surrounding whitespace.
        """
        cfg = {"lab_mgmt": {"bridge": "br1", "stp": value}}
        with patch("boxman.netlab.shared_bridges.run",
                   side_effect=self._fake_run()) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)

        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert f"stp_state {expected}" in all_cmds

    @pytest.mark.parametrize("value", ["enabled", "maybe", 2, "", []])
    def test_invalid_stp_rejected_before_touching_the_host(self, value):
        """A typo is rejected, and rejected before anything is mutated."""
        cfg = {"lab_mgmt": {"bridge": "br1", "stp": value}}
        with patch("boxman.netlab.shared_bridges.run") as run:
            with pytest.raises(ConfigError, match="'stp' must be on or off"):
                shared_bridges.ensure(cfg)
            run.assert_not_called()

    @staticmethod
    def _fake_run(rule_present=False, docker_user=False):
        """A run() double for the netfilter path, on an existing bridge.

        ``rule_present`` → the ``iptables -C`` check reports the scoped rule
        already exists (so no ``-I`` insert). ``docker_user`` → the
        ``DOCKER-USER`` chain exists.

        Delegates to the module-level :func:`_recorder` so this file has
        exactly one set of stub semantics -- in particular one that honours
        ``warn`` and raises as invoke does. This used to be a second, parallel
        stub that ignored ``warn``, which is how a mutation making the
        DOCKER-USER probe fatal passed the entire suite: the one test covering
        an absent chain was driven by the stub that could not notice.
        """
        _calls, fake = _recorder(bridge_exists=True,
                                 rule_present=rule_present,
                                 docker_user=docker_user)
        return fake

    def test_default_applies_scoped_forward_rule_not_global_disable(self):
        """D8 default (disable_netfilter unset → False): a scoped physdev
        ACCEPT rule is inserted into FORWARD, and the host-global sysctl is
        left untouched."""
        cfg = {"lab_mgmt": {"bridge": "br1"}}  # default disable_netfilter=False
        with patch("boxman.netlab.shared_bridges.run",
                   side_effect=self._fake_run()) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)
        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert ("iptables -t filter -I FORWARD 1 -i br1 -o br1 "
                "-m physdev --physdev-is-bridged -j ACCEPT") in all_cmds
        assert "bridge-nf-call-iptables" not in all_cmds
        assert "echo 0" not in all_cmds

    def test_scoped_rule_idempotent_when_already_present(self):
        """When ``iptables -C`` reports the rule exists, no ``-I`` insert runs."""
        cfg = {"lab_mgmt": {"bridge": "br1"}}
        with patch("boxman.netlab.shared_bridges.run",
                   side_effect=self._fake_run(rule_present=True)) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)
        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert "-C FORWARD" in all_cmds       # checked
        assert "-I FORWARD" not in all_cmds   # but not re-inserted

    def test_scoped_rule_added_to_docker_user_when_chain_exists(self):
        cfg = {"lab_mgmt": {"bridge": "br1"}}
        with patch("boxman.netlab.shared_bridges.run",
                   side_effect=self._fake_run(docker_user=True)) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)
        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert "-I DOCKER-USER 1 -i br1 -o br1" in all_cmds

    def test_scoped_rule_skips_docker_user_when_chain_absent(self):
        cfg = {"lab_mgmt": {"bridge": "br1"}}
        with patch("boxman.netlab.shared_bridges.run",
                   side_effect=self._fake_run(docker_user=False)) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)
        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert "-I DOCKER-USER" not in all_cmds

    def test_disable_netfilter_opt_in_sets_global_and_warns(self, captured_logs):
        """Explicit disable_netfilter: true → host-global sysctl=0, a loud
        warning, and NO scoped FORWARD rule."""
        cfg = {"lab_mgmt": {"bridge": "br1", "disable_netfilter": True}}
        with patch("boxman.netlab.shared_bridges.run",
                   side_effect=self._fake_run()) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)
        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert "bridge-nf-call-iptables" in all_cmds and "echo 0" in all_cmds
        assert "-I FORWARD" not in all_cmds  # global disable, no scoped rule
        assert any("HOST-WIDE" in r.message for r in captured_logs.records)

    def test_disable_netfilter_skips_the_write_when_br_netfilter_is_absent(
            self, captured_logs):
        """`br_netfilter` not loaded → the procfs file does not exist.

        The guard must skip the write and warn, not attempt a `tee` into a
        path that is not there. Removing the guard entirely (`if True:`)
        passed all 81 tests and the full suite until this case existed: with
        the file absent the write fails and the run aborts, where the intended
        behaviour is to warn and carry on.
        """
        calls, fake_run = _recorder(bridge_exists=True)
        cfg = {"lab_mgmt": {"bridge": "br1", "disable_netfilter": True}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run), \
                patch("pathlib.Path.exists", return_value=False):
            shared_bridges.ensure(cfg)          # must not raise

        assert not any("bridge-nf-call-iptables" in c for c in calls), calls
        assert not any("tee" in c for c in calls), calls
        assert any("br_netfilter not loaded" in r.message
                   for r in captured_logs.records)

    def test_disable_netfilter_string_false_does_not_disable(self):
        """A quoted `disable_netfilter: "false"` must not disable host-wide.

        Plain truthiness would: a non-empty "false" is truthy, so the
        host-global sysctl would be zeroed by a config asking for the exact
        opposite. Same input-class bug as a quoted `stp: "off"`.
        """
        cfg = {"lab_mgmt": {"bridge": "br1", "disable_netfilter": "false"}}
        with patch("boxman.netlab.shared_bridges.run",
                   side_effect=self._fake_run()) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)
        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert "bridge-nf-call-iptables" not in all_cmds
        assert "-I FORWARD" in all_cmds          # scoped rule instead

    @pytest.mark.parametrize("value", [
        "true", "yes", "on", "1", True,
        "TRUE", "Yes", "ON", " on ", "\tTrue\n",   # case + whitespace
    ])
    def test_disable_netfilter_truthy_spellings_disable(self, value):
        cfg = {"lab_mgmt": {"bridge": "br1", "disable_netfilter": value}}
        with patch("boxman.netlab.shared_bridges.run",
                   side_effect=self._fake_run()) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)
        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert "bridge-nf-call-iptables" in all_cmds

    @pytest.mark.parametrize("value", ["maybe", 2, "", []])
    def test_invalid_disable_netfilter_rejected_before_touching_the_host(
            self, value):
        cfg = {"lab_mgmt": {"bridge": "br1", "disable_netfilter": value}}
        with patch("boxman.netlab.shared_bridges.run") as run:
            with pytest.raises(ConfigError,
                               match="'disable_netfilter' must be on or off"):
                shared_bridges.ensure(cfg)
            run.assert_not_called()

    @pytest.mark.parametrize("key,message", [
        ("stp", "'stp' must be on or off"),
        ("disable_netfilter", "'disable_netfilter' must be on or off"),
        ("mtu", "'mtu' must be a positive"),
    ])
    def test_key_present_with_no_value_is_rejected(self, key, message):
        """`stp:` with nothing after it is a mistake, not "leave it alone".

        `entry.get(k)` collapses an explicit null into absent, which would
        make a config typo silently mean the opposite of what these keys now
        do on omission.
        """
        cfg = {"lab_mgmt": {"bridge": "br1", key: None}}
        with patch("boxman.netlab.shared_bridges.run") as run:
            with pytest.raises(ConfigError, match=message):
                shared_bridges.ensure(cfg)
            run.assert_not_called()

    def test_missing_bridge_key_raises(self):
        cfg = {"lab_mgmt": {"stp": False}}  # no 'bridge'
        with pytest.raises(ConfigError, match="missing required 'bridge' key"):
            shared_bridges.ensure(cfg)

    @pytest.mark.parametrize("name", [
        "a" * 16,               # over the IFNAMSIZ 15-char limit
        "br;rm -rf /",          # shell metacharacters
        "br name",              # whitespace
        "br$(id)",              # command substitution
    ])
    def test_invalid_bridge_names_rejected(self, name):
        cfg = {"lab_mgmt": {"bridge": name}}
        with patch("boxman.netlab.shared_bridges.run") as run:
            with pytest.raises(ConfigError, match="invalid bridge name"):
                shared_bridges.ensure(cfg)
            run.assert_not_called()  # rejected before any shell-out

    @pytest.mark.parametrize("name", [
        "a" * 15,               # exactly at the IFNAMSIZ limit
        "br_0",
        "br-0",
        "br.100",               # vlan-style dotted name
    ])
    def test_valid_bridge_names_accepted(self, name):
        _calls, fake_run = _recorder(bridge_exists=True)

        cfg = {"lab_mgmt": {"bridge": name}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run):
            with patch("pathlib.Path.exists", return_value=False):
                shared_bridges.ensure(cfg)  # must not raise

    def test_mtu_applied_when_configured(self):
        """`mtu:` emits `ip link set dev <br> mtu <n>` at ensure time —
        bridges default to 1500 while containerlab veth links use 9500."""
        calls, fake_run = _recorder(bridge_exists=True)

        cfg = {"lab_mgmt": {"bridge": "br1", "mtu": 9500}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run):
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)

        all_cmds = " | ".join(calls)
        assert "sudo ip link set dev br1 mtu 9500" in all_cmds

    def test_mtu_not_touched_when_absent(self):
        cfg = {"lab_mgmt": {"bridge": "br1"}}
        with patch("boxman.netlab.shared_bridges.run",
                   side_effect=self._fake_run()) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)
        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert " mtu " not in all_cmds

    @pytest.mark.parametrize("mtu", ["9500", 0, -1, True, 1.5])
    def test_invalid_mtu_rejected(self, mtu):
        cfg = {"lab_mgmt": {"bridge": "br1", "mtu": mtu}}
        with patch("boxman.netlab.shared_bridges.run") as run:
            with pytest.raises(ConfigError, match="'mtu' must be a positive"):
                shared_bridges.ensure(cfg)
            run.assert_not_called()


class TestHelpers:

    def test_is_shared_bridge_positive(self):
        cfg = {"lab_mgmt": {"bridge": "x"}}
        assert shared_bridges.is_shared_bridge("lab_mgmt", cfg) is True

    def test_is_shared_bridge_negative(self):
        cfg = {"lab_mgmt": {"bridge": "x"}}
        assert shared_bridges.is_shared_bridge("nope", cfg) is False
        assert shared_bridges.is_shared_bridge("nope", None) is False
        assert shared_bridges.is_shared_bridge("nope", {}) is False

    def test_resolve_bridge_returns_underlying_name(self):
        cfg = {"lab_mgmt": {"bridge": "shared_lab_mgmt"}}
        assert shared_bridges.resolve_bridge("lab_mgmt", cfg) == "shared_lab_mgmt"

    def test_resolve_bridge_unknown_key_raises(self):
        with pytest.raises(KeyError):
            shared_bridges.resolve_bridge("missing", {})

    def test_resolve_bridge_raises_keyerror_when_there_are_none_at_all(self):
        """`None` must give the documented KeyError, not a TypeError.

        The guard's `not shared_networks` half is what makes this a KeyError;
        without it the call falls through to `None[name]`. A dict-based test
        cannot tell the difference, because an ordinary lookup miss raises
        KeyError anyway -- so dropping the guard passed the whole suite.
        """
        with pytest.raises(KeyError, match="is not a shared_networks entry"):
            shared_bridges.resolve_bridge("lab", None)

    def test_resolve_bridge_missing_bridge_field_raises(self):
        cfg = {"lab_mgmt": {}}
        with pytest.raises(ValueError, match="missing required 'bridge' key"):
            shared_bridges.resolve_bridge("lab_mgmt", cfg)


class TestPrivilegeIsDerivedFromTheProcess:
    """#164 FBN-12, and the regression its fix introduced.

    FBN-12 was right that hard-coding ``sudo`` is wrong on a host where boxman
    already runs as root. The fix took the answer from
    ``provider.libvirt.use_sudo``, which answers a different question -- does
    *virsh* need sudo -- and so dropped sudo from ``ip link`` for every project
    that set it false, leaving them unable to bring a shared bridge up at all.

    The privilege requirement belongs to the command, not to the provider:
    netlink writes need CAP_NET_ADMIN and no group membership grants it.
    """

    @staticmethod
    def _calls_for(needs_sudo, entry=None, **recorder_kwargs):
        calls, fake_run = _recorder(**recorder_kwargs)

        cfg = {"lab": {"bridge": "br_lab", **(entry or {})}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run), \
                patch("boxman.netlab.shared_bridges._needs_sudo",
                      return_value=needs_sudo), \
                patch("pathlib.Path.exists", return_value=True):
            shared_bridges.ensure(cfg)
        return calls

    def test_needs_sudo_is_exactly_not_root(self):
        """The one test that runs the real decision function.

        Everything else patches it, so this is what stops the mapping from
        euid drifting underneath the rest of the class.
        """
        with patch("boxman.netlab.shared_bridges.os.geteuid", return_value=0):
            assert _REAL_NEEDS_SUDO() is False
        for uid in (1, 1000, 65534):
            with patch("boxman.netlab.shared_bridges.os.geteuid",
                       return_value=uid):
                assert _REAL_NEEDS_SUDO() is True, uid

    def test_ordinary_user_gets_sudo_on_the_netlink_writes(self):
        calls = self._calls_for(True)
        assert any("sudo ip link add name br_lab" in c for c in calls), calls
        assert any("sudo ip link set dev br_lab up" in c for c in calls), calls

    def test_ordinary_user_gets_sudo_on_the_privileged_probe(self):
        """`iptables -L` needs root exactly as `-I` does, so sudo'ing only the
        write would leave the probe failing and the rule re-inserted every
        run."""
        calls = self._calls_for(True)
        assert any(c.startswith("sudo iptables -t filter -n -L") for c in calls), calls

    def test_root_gets_no_sudo_anywhere(self):
        """As root the prefix is not merely redundant: a container image
        commonly carries no sudo at all."""
        calls = self._calls_for(False)
        assert any(c.startswith("ip link add name br_lab") for c in calls), calls
        assert not any(c.startswith("sudo ") for c in calls), calls
        assert not any("sudo " in c for c in calls), calls

    def test_sysfs_write_follows_the_same_rule(self):
        """The `tee` into bridge-nf-call-iptables is a root write too."""
        entry = {"disable_netfilter": True}
        assert any("| sudo tee /proc/sys/net/bridge/bridge-nf-call-iptables" in c
                   for c in self._calls_for(True, entry))
        assert any("| tee /proc/sys/net/bridge/bridge-nf-call-iptables" in c
                   and "sudo" not in c
                   for c in self._calls_for(False, entry))

    def test_link_probe_stays_unprivileged_either_way(self):
        """`ip link show` reads netlink and must not require root, or a host
        running boxman unprivileged for read-only work would break."""
        for needs_sudo in (False, True):
            calls = self._calls_for(needs_sudo)
            assert any(c.startswith("ip link show dev br_lab")
                       for c in calls), needs_sudo

    def test_the_real_decision_is_wired_through_to_the_commands(self):
        """End to end, with nothing stubbed between the euid and the command.

        Every other behavioural test in this class patches ``_needs_sudo``, so
        on their own they would still pass if ``ensure`` stopped consulting it
        and hard-coded a prefix. Only the euid is faked here.
        """
        for euid, expect_sudo in ((0, False), (1000, True)):
            calls, fake_run = _recorder()

            with patch("boxman.netlab.shared_bridges.run",
                       side_effect=fake_run), \
                    patch("boxman.netlab.shared_bridges._needs_sudo",
                          _REAL_NEEDS_SUDO), \
                    patch("boxman.netlab.shared_bridges.os.geteuid",
                          return_value=euid), \
                    patch("pathlib.Path.exists", return_value=False):
                shared_bridges.ensure({"lab": {"bridge": "br_lab"}})

            writes = [c for c in calls if "ip link add name br_lab" in c
                      or "ip link set dev br_lab up" in c]
            assert len(writes) == 2, (euid, calls)
            assert all(c.startswith("sudo ") for c in writes) is expect_sudo, \
                (euid, writes)

    # The accept-rule body, spelled once so the expectations below stay
    # readable. Mirrors _scoped_rule_body().
    _BODY = "-i br_lab -o br_lab -m physdev --physdev-is-bridged -j ACCEPT"

    #: Every command ensure() issues on its longest path: bridge absent so it
    #: is created, MTU and STP declared, accept rule absent so it is inserted,
    #: DOCKER-USER present so that chain is handled too.
    _FULL_PATH = [
        "ip link show dev br_lab",
        "ip link add name br_lab type bridge",
        "ip link set dev br_lab up",
        "ip link set dev br_lab mtu 9500",
        "ip link set dev br_lab type bridge stp_state 1",
        f"iptables -t filter -C FORWARD {_BODY}",
        f"iptables -t filter -I FORWARD 1 {_BODY}",
        "iptables -t filter -n -L DOCKER-USER",
        f"iptables -t filter -C DOCKER-USER {_BODY}",
        f"iptables -t filter -I DOCKER-USER 1 {_BODY}",
    ]

    #: The disable_netfilter path: no accept rules, one sysfs write instead.
    _SYSFS_PATH = [
        "ip link show dev br_lab",
        "ip link add name br_lab type bridge",
        "ip link set dev br_lab up",
        "ip link set dev br_lab type bridge stp_state 0",
        "echo 0 | tee /proc/sys/net/bridge/bridge-nf-call-iptables",
    ]

    @staticmethod
    def _base(cmd: str) -> str:
        """The command with any sudo prefix removed, wherever it sits."""
        return cmd.replace("| sudo tee ", "| tee ").removeprefix("sudo ")

    def _assert_prefixes(self, calls, expected, needs_sudo):
        """Every command is the one expected, and carries the prefix iff it is
        privileged and we are not root.

        Asserting the *set* as well as the prefixes means a dropped command
        fails here too, not only a mis-privileged one.
        """
        assert [self._base(c) for c in calls] == expected, calls
        for cmd in calls:
            want = needs_sudo and _is_privileged(self._base(cmd))
            assert _carries_prefix(cmd) is want, cmd

    def test_every_command_on_the_full_path_is_privileged_correctly(self):
        """Per command, by name.

        The earlier version of this class asserted only that *some* command
        carried a prefix, so four separate privilege mutations -- on
        `iptables -I`, on `iptables -C`, on the MTU write and on the explicit
        STP write -- passed all 74 tests and the full 2969-test suite.
        """
        entry = {"mtu": 9500, "stp": True}
        for needs_sudo in (True, False):
            calls = self._calls_for(needs_sudo, entry)
            self._assert_prefixes(calls, self._FULL_PATH, needs_sudo)

    def test_every_command_on_the_sysfs_path_is_privileged_correctly(self):
        entry = {"disable_netfilter": True}
        for needs_sudo in (True, False):
            calls = self._calls_for(needs_sudo, entry)
            self._assert_prefixes(calls, self._SYSFS_PATH, needs_sudo)

    def test_the_accept_rule_is_actually_inserted_when_missing(self):
        """Guards the recorder itself.

        If the stub ever goes back to answering ok to every `iptables -C`, the
        insertion stops being executed and the tests above silently stop
        covering it.
        """
        calls = [self._base(c) for c in self._calls_for(True, {})]
        assert f"iptables -t filter -I FORWARD 1 {self._BODY}" in calls
        assert f"iptables -t filter -I DOCKER-USER 1 {self._BODY}" in calls

    def test_present_rule_is_not_reinserted(self):
        """The counterpart: `-C` succeeding must skip `-I`, or every run would
        stack a duplicate."""
        calls = [self._base(c) for c in
                 self._calls_for(True, {}, rule_present=True)]
        assert not any(" -I " in c for c in calls), calls

    def test_a_failed_privileged_write_stops_the_run(self):
        """A bridge that cannot be created must abort, not be stepped over.

        `_run_sudo` deliberately does not pass `warn`, so invoke raises. Giving
        it `warn=True` would let `ensure()` report success over a bridge that
        does not exist -- and that mutation passed all 78 tests until the
        recorder started honouring `warn`.
        """
        calls, fake_run = _recorder(fails=("ip link add",))
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run), \
                patch("pathlib.Path.exists", return_value=False):
            with pytest.raises(UnexpectedExit):
                shared_bridges.ensure({"lab": {"bridge": "br_lab"}})

        # and it stopped there: nothing after the failed create was attempted
        assert not any("ip link set dev br_lab up" in c for c in calls), calls

    def test_a_failed_sysctl_write_stops_the_run(self):
        """`_set_sysfs` is a separate write helper and needs its own case.

        The bridge-creation failure test above goes through `_run_sudo` and
        never reaches this one, so giving `_set_sysfs` a `warn=True` -- which
        would let a failed `tee` complete silently -- passed the whole suite.
        """
        calls, fake_run = _recorder(fails=("tee",))
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run), \
                patch("pathlib.Path.exists", return_value=True):
            with pytest.raises(UnexpectedExit):
                shared_bridges.ensure(
                    {"lab": {"bridge": "br_lab", "disable_netfilter": True}})
        assert any("bridge-nf-call-iptables" in c for c in calls), calls

    def test_a_missing_accept_rule_does_not_abort_the_run(self):
        """The `-C` probe must stay `warn=True`.

        A missing rule is the normal first-run case and exits non-zero, so a
        raising probe would abort before anything could be inserted.
        """
        calls, fake_run = _recorder()          # rule_present=False
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run), \
                patch("pathlib.Path.exists", return_value=False):
            shared_bridges.ensure({"lab": {"bridge": "br_lab"}})   # must not raise
        bases = [self._base(c) for c in calls]
        assert f"iptables -t filter -I FORWARD 1 {self._BODY}" in bases, bases

    def test_every_declared_bridge_is_processed(self):
        """Three differently configured bridges, each with its own operations.

        Every other test in this file declares exactly one, so truncating the
        loop to `list(shared_networks.items())[:1]` processed the first, skipped
        the rest and returned successfully -- passing the whole suite while a
        real project silently lost every bridge after the first.
        """
        cfg = {
            "a": {"bridge": "br_a", "mtu": 9000},
            "b": {"bridge": "br_b", "stp": True},
            "c": {"bridge": "br_c"},
        }
        calls, fake_run = _recorder()          # all three absent -> created
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run), \
                patch("pathlib.Path.exists", return_value=False):
            shared_bridges.ensure(cfg)

        for bridge in ("br_a", "br_b", "br_c"):
            assert any(f"ip link add name {bridge} type bridge" in c
                       for c in calls), (bridge, calls)
            assert any(f"ip link set dev {bridge} up" in c
                       for c in calls), (bridge, calls)

        # and each one's own settings, not the first entry's applied to all
        assert any("ip link set dev br_a mtu 9000" in c for c in calls), calls
        assert not any("mtu" in c and "br_b" in c for c in calls), calls
        assert any("dev br_b type bridge stp_state 1" in c for c in calls), calls
        assert any("dev br_c type bridge stp_state 0" in c for c in calls), calls

    def test_a_later_bridge_failing_does_not_hide_behind_an_earlier_success(self):
        """The second bridge's failure must surface, not be swallowed by the
        first one having worked."""
        cfg = {"a": {"bridge": "br_a"}, "b": {"bridge": "br_b"}}
        calls, fake_run = _recorder(fails=("ip link add name br_b",))
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run), \
                patch("pathlib.Path.exists", return_value=False):
            with pytest.raises(UnexpectedExit):
                shared_bridges.ensure(cfg)
        assert any("ip link add name br_a" in c for c in calls), calls

    def test_ensure_exposes_no_privilege_knob(self):
        """The defect was a caller handing in the wrong flag. The parameter is
        gone so that cannot be expressed again."""
        import inspect
        assert list(inspect.signature(shared_bridges.ensure).parameters) == [
            "shared_networks"]


class TestDockerRuntimeIsRefused:
    """#164 FBN-12 — under the docker runtime the bridges land in the host
    netns while the guests live in the container's, so boxman would report
    creating a bridge and then fail to use it in the same run."""

    @staticmethod
    def _manager(runtime_name):
        from unittest.mock import MagicMock

        from boxman.manager import BoxmanManager
        mgr = BoxmanManager.__new__(BoxmanManager)
        mgr.config = {"shared_networks": {"lab": {"bridge": "br_lab"}}}
        mgr.logger = MagicMock()
        mgr._runtime_name = runtime_name
        mgr._provider = MagicMock()
        mgr._provider.provider_config = {"use_sudo": True}
        return mgr

    def test_docker_runtime_refuses(self):
        from boxman.exceptions import ConfigError
        mgr = self._manager("docker-compose")
        with pytest.raises(ConfigError, match="not supported with the docker runtime"):
            mgr.ensure_shared_bridges()

    def test_docker_alias_refuses(self):
        from boxman.exceptions import ConfigError
        mgr = self._manager("docker")
        with pytest.raises(ConfigError, match="not supported with the docker runtime"):
            mgr.ensure_shared_bridges()

    def test_local_runtime_proceeds(self):
        mgr = self._manager("local")
        with patch("boxman.netlab.shared_bridges.ensure") as ensure:
            mgr.ensure_shared_bridges()
        # The declaration and nothing else: forwarding a privilege flag from
        # here is what broke `use_sudo: false` projects.
        ensure.assert_called_once_with({"lab": {"bridge": "br_lab"}})

    def test_provider_use_sudo_false_still_sudoes_the_bridge_commands(self):
        """The regression this replaces.

        `provider.libvirt.use_sudo: false` is the *correct* setting for a user
        in the `libvirt` group -- virsh needs no sudo for them. It says nothing
        about netlink: `ip link add` still answers `Operation not permitted`,
        group membership or not. Forwarding the flag made `boxman up` fail at
        the first bridge for exactly those projects.
        """
        mgr = self._manager("local")
        mgr._provider.provider_config = {"use_sudo": False}

        calls, fake_run = _recorder()

        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run), \
                patch("pathlib.Path.exists", return_value=False):
            mgr.ensure_shared_bridges()   # _needs_sudo -> True via the fixture

        assert any("sudo ip link add name br_lab" in c for c in calls), calls
        assert any("sudo ip link set dev br_lab up" in c for c in calls), calls

    def test_no_shared_networks_is_a_noop_under_docker(self):
        """The guard must not fire for a project that declares none."""
        mgr = self._manager("docker-compose")
        mgr.config = {}
        mgr.ensure_shared_bridges()
