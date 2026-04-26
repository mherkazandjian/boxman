"""CLI smoke tests for ``boxman netlab`` subcommands.

Just verifies the argparse wiring: each subcommand is reachable, accepts
--help, and dispatches to the expected BoxmanManager handler.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from boxman.manager import BoxmanManager
from boxman.scripts.app import main, parse_args


pytestmark = pytest.mark.smoke


def _run_cli(argv: list[str]) -> int:
    with patch.object(sys, "argv", ["boxman"] + argv):
        try:
            main()
            return 0
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 0


class TestNetlabHelp:

    def test_netlab_help_exits_zero(self, capsys):
        assert _run_cli(["netlab", "--help"]) == 0
        out = capsys.readouterr().out
        assert "netlab" in out
        assert "deploy" in out
        assert "destroy" in out
        assert "inspect" in out
        assert "ssh" in out

    @pytest.mark.parametrize("sub", ["deploy", "destroy", "inspect", "ssh"])
    def test_netlab_subcommand_help(self, sub: str, capsys):
        code = _run_cli(["netlab", sub, "--help"])
        # ssh requires a positional 'node', but --help bypasses the
        # required-arg check and exits 0.
        assert code == 0


class TestNetlabParserWiring:
    """Directly inspect parse_args() output — no SystemExit dance."""

    def test_deploy_dispatches_to_handler(self):
        parser = parse_args()
        args = parser.parse_args(["netlab", "deploy"])
        assert args.func is BoxmanManager.netlab_deploy

    def test_destroy_dispatches_to_handler(self):
        parser = parse_args()
        args = parser.parse_args(["netlab", "destroy"])
        assert args.func is BoxmanManager.netlab_destroy

    def test_inspect_dispatches_to_handler(self):
        parser = parse_args()
        args = parser.parse_args(["netlab", "inspect"])
        assert args.func is BoxmanManager.netlab_inspect

    def test_ssh_requires_node_arg(self):
        parser = parse_args()
        with pytest.raises(SystemExit):
            parser.parse_args(["netlab", "ssh"])

    def test_ssh_dispatches_and_captures_node(self):
        parser = parse_args()
        args = parser.parse_args(["netlab", "ssh", "sw1"])
        assert args.func is BoxmanManager.netlab_ssh
        assert args.node == "sw1"
        assert args.user is None

    def test_ssh_user_flag(self):
        parser = parse_args()
        args = parser.parse_args(["netlab", "ssh", "sw1", "--user", "cumulus"])
        assert args.user == "cumulus"
