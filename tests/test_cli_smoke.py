"""
CLI smoke tests — the boxman command-line interface must boot cleanly,
parse its subcommands, and respond to ``--help`` / ``--version`` without
touching libvirt, Docker, or the project cache.

These tests invoke ``boxman.scripts.app.main`` in-process and catch the
``SystemExit`` it raises (the CLI uses ``sys.exit()`` throughout; a Phase
2.5 follow-up will refactor to ``main(argv) -> int`` so these can be
written more cleanly).

Part of Phase 1.3 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import boxman.metadata
from boxman.scripts.app import main, parse_args


pytestmark = pytest.mark.smoke


def _run_cli(argv: list[str]) -> int:
    """
    Invoke boxman's ``main()`` with *argv* and return its exit code.

    Catches ``SystemExit`` because ``main()`` uses ``sys.exit(N)`` rather
    than returning — standard pattern for CLI tests until a proper
    ``main(argv) -> int`` refactor lands.
    """
    with patch.object(sys, "argv", ["boxman"] + argv):
        try:
            main()
            return 0
        except SystemExit as exc:
            # argparse --help / our exit calls all come through here
            return exc.code if isinstance(exc.code, int) else 0


class TestHelp:

    def test_help_flag_exits_zero(self, capsys):
        assert _run_cli(["--help"]) == 0
        out = capsys.readouterr().out
        assert "Boxman" in out

    def test_help_mentions_subcommands(self, capsys):
        _run_cli(["--help"])
        out = capsys.readouterr().out
        for sub in ("provision", "destroy", "snapshot", "list"):
            assert sub in out

    def test_no_args_prints_help_and_exits_nonzero(self, capsys):
        code = _run_cli([])
        out = capsys.readouterr().out
        # exit code 1 per app.py:1033 when no subcommand given
        assert code == 1
        assert "Boxman" in out or "usage" in out.lower()


class TestVersion:

    def test_version_flag_prints_version(self, capsys):
        assert _run_cli(["--version"]) == 0
        out = capsys.readouterr().out.strip()
        assert out == f"v{boxman.metadata.version}"


class TestSubcommandHelps:
    """Each subcommand must accept --help and exit cleanly."""

    @pytest.mark.parametrize("subcommand", [
        "provision",
        "destroy",
        "snapshot",
        "list",
    ])
    def test_subcommand_help_exits_zero(self, subcommand: str, capsys):
        assert _run_cli([subcommand, "--help"]) == 0
        out = capsys.readouterr().out
        assert subcommand in out or "usage" in out.lower()


class TestInvalidArgs:

    def test_unknown_subcommand_exits_nonzero(self, capsys):
        code = _run_cli(["bogus-subcommand"])
        assert code != 0
        err = capsys.readouterr().err
        assert "bogus-subcommand" in err or "invalid choice" in err.lower()

    def test_unknown_flag_exits_nonzero(self, capsys):
        code = _run_cli(["--nonexistent-flag"])
        assert code != 0


class TestParserConstruction:
    """Direct tests against the parser — no SystemExit dance needed."""

    def test_parse_args_returns_argument_parser(self):
        import argparse
        parser = parse_args()
        assert isinstance(parser, argparse.ArgumentParser)

    def test_parser_has_known_subcommands(self):
        parser = parse_args()
        # argparse exposes subparsers via _subparsers
        subactions = [
            a for a in parser._actions
            if a.__class__.__name__ == "_SubParsersAction"
        ]
        assert subactions, "expected a subparsers group"
        choices = subactions[0].choices
        # the CLI's documented subcommand set per .claude/CLAUDE.md
        for sub in ("provision", "destroy", "snapshot", "list"):
            assert sub in choices, f"subcommand {sub!r} missing from parser"

    def test_parser_version_flag_exists(self):
        parser = parse_args()
        flag_names = [
            name
            for action in parser._actions
            for name in getattr(action, "option_strings", [])
        ]
        assert "--version" in flag_names or "-V" in flag_names


class TestListOnEmptyCache:
    """
    ``boxman list`` must not crash when no projects have been registered.
    Uses a tmp_path-backed cache so we don't read the real user cache.
    """

    def test_list_empty_cache_exits_zero(self, tmp_path: Path, capsys):
        with patch("boxman.config_cache.DEFAULT_CACHE_DIR", str(tmp_path / "cache")):
            assert _run_cli(["list"]) == 0


class TestConfigDryRun:
    """
    Exercise the Jinja2-rendered + YAML-parsed config loading path without
    invoking libvirt. Uses the bundled template from data/templates/.
    """

    def test_template_config_renders_and_parses(self):
        from pathlib import Path as P
        import yaml
        from boxman.utils.jinja_env import create_jinja_env

        tpl_dir = P(__file__).resolve().parent.parent / "data" / "templates"
        candidates = list(tpl_dir.glob("conf*.yml"))
        if not candidates:
            pytest.skip("no example template config found in data/templates/")

        env = create_jinja_env(str(tpl_dir))
        template = env.get_template(candidates[0].name)
        rendered = template.render()
        # must be parseable YAML
        data = yaml.safe_load(rendered)
        # bare minimum: has a project key or something structurally similar
        assert isinstance(data, (dict, type(None)))
