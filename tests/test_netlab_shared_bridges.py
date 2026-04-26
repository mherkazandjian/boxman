"""Unit tests for boxman.netlab.shared_bridges.

Covers idempotency, sudo-prefixed commands, STP/netfilter knobs, and
the ``is_shared_bridge`` / ``resolve_bridge`` helpers used by the
manager to resolve an adapter's ``network_source`` against the
top-level ``shared_networks:`` block.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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

    def test_disables_bridge_netfilter_when_proc_exists(self):
        def fake_run(cmd, **kwargs):
            if cmd.startswith("ip link show dev"):
                return _result(ok=True)
            return _result(ok=True)

        cfg = {"lab_mgmt": {"bridge": "br1"}}  # disable_netfilter defaults True
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)

        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert "bridge-nf-call-iptables" in all_cmds
        assert "echo 0" in all_cmds

    def test_skips_netfilter_when_opted_out(self):
        def fake_run(cmd, **kwargs):
            if cmd.startswith("ip link show dev"):
                return _result(ok=True)
            return _result(ok=True)

        cfg = {"lab_mgmt": {"bridge": "br1", "disable_netfilter": False}}
        with patch("boxman.netlab.shared_bridges.run", side_effect=fake_run) as run:
            with patch("pathlib.Path.exists", return_value=True):
                shared_bridges.ensure(cfg)

        all_cmds = " | ".join(c.args[0] for c in run.call_args_list)
        assert "bridge-nf-call-iptables" not in all_cmds

    def test_missing_bridge_key_raises(self):
        cfg = {"lab_mgmt": {"stp": False}}  # no 'bridge'
        with pytest.raises(ValueError, match="missing required 'bridge' key"):
            shared_bridges.ensure(cfg)


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
