"""Unit tests for boxman.netlab.shared_bridges.

Covers idempotency, sudo-prefixed commands, STP/netfilter knobs, and
the ``is_shared_bridge`` / ``resolve_bridge`` helpers used by the
manager to resolve an adapter's ``network_source`` against the
top-level ``shared_networks:`` block.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import ConfigError
from boxman.netlab import shared_bridges

pytestmark = pytest.mark.unit


def _result(ok: bool = True) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.ok = ok
    r.failed = not ok
    return r


class TestEnsure:

    def test_noop_when_empty(self):
        with patch("boxman.netlab.shared_bridges.run") as run:
            shared_bridges.ensure(None)
            shared_bridges.ensure({})
            run.assert_not_called()

    def test_creates_missing_bridge(self):
        calls: list[str] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd.startswith("ip link show dev"):
                return _result(ok=False)
            return _result(ok=True)

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
        def fake_run(cmd, **kwargs):
            if cmd.startswith("ip link show dev"):
                return _result(ok=True)
            return _result(ok=True)

        cfg = {"lab_mgmt": {"bridge": "shared_lab_mgmt"}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run) as run:
            with patch("pathlib.Path.exists", return_value=False):
                shared_bridges.ensure(cfg)

        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert "ip link add name shared_lab_mgmt" not in all_cmds
        # Still ensures it's up, which is idempotent on an existing bridge.
        assert "ip link set dev shared_lab_mgmt up" in all_cmds

    def test_stp_on_sets_state_1(self):
        def fake_run(cmd, **kwargs):
            if cmd.startswith("ip link show dev"):
                return _result(ok=True)
            return _result(ok=True)

        cfg = {"lab_mgmt": {"bridge": "br_stp", "stp": True}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run) as run:
            with patch("pathlib.Path.exists", return_value=False):
                shared_bridges.ensure(cfg)

        assert any("stp_state 1" in c.args[0] for c in run.call_args_list)

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
        def fake_run(cmd, **kwargs):
            if cmd.startswith("ip link show dev"):
                return _result(ok=False)      # absent -> created by this run
            return _result(ok=True)

        cfg = {"lab_mgmt": {"bridge": "br1"}}
        with patch("boxman.netlab.shared_bridges.run",
                   side_effect=fake_run) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)

        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert "stp_state 0" in all_cmds

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
    ])
    def test_stp_spellings_normalised(self, value, expected):
        """A quoted `stp: "off"` must not switch STP on.

        Plain truthiness would: a non-empty "off" is truthy. Same accepted
        spellings as a libvirt network's `bridge.stp`.
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
        """A run() double for the netfilter path.

        ``rule_present`` → the ``iptables -C`` check reports the scoped rule
        already exists (so no ``-I`` insert). ``docker_user`` → the
        ``DOCKER-USER`` chain exists.
        """
        def fake(cmd, **kwargs):
            if cmd.startswith("ip link show dev"):
                return _result(ok=True)
            if "-C FORWARD" in cmd or "-C DOCKER-USER" in cmd:
                return _result(ok=rule_present)
            if "-n -L DOCKER-USER" in cmd:
                return _result(ok=docker_user)
            return _result(ok=True)
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
        def fake_run(cmd, **kwargs):
            if cmd.startswith("ip link show dev"):
                return _result(ok=True)
            return _result(ok=True)

        cfg = {"lab_mgmt": {"bridge": name}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run):
            with patch("pathlib.Path.exists", return_value=False):
                shared_bridges.ensure(cfg)  # must not raise

    def test_mtu_applied_when_configured(self):
        """`mtu:` emits `ip link set dev <br> mtu <n>` at ensure time —
        bridges default to 1500 while containerlab veth links use 9500."""
        def fake_run(cmd, **kwargs):
            if cmd.startswith("ip link show dev"):
                return _result(ok=True)
            return _result(ok=True)

        cfg = {"lab_mgmt": {"bridge": "br1", "mtu": 9500}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)

        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
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

    def test_resolve_bridge_missing_bridge_field_raises(self):
        cfg = {"lab_mgmt": {}}
        with pytest.raises(ValueError, match="missing required 'bridge' key"):
            shared_bridges.resolve_bridge("lab_mgmt", cfg)
