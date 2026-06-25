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
        "storage",
    ])
    def test_subcommand_help_exits_zero(self, subcommand: str, capsys):
        assert _run_cli([subcommand, "--help"]) == 0
        out = capsys.readouterr().out
        assert subcommand in out or "usage" in out.lower()

    @pytest.mark.parametrize("subverb",
                             ["df", "trim", "compact", "optimize",
                              "compress-snapshots"])
    def test_storage_subverb_help_exits_zero(self, subverb: str, capsys):
        assert _run_cli(["storage", subverb, "--help"]) == 0
        out = capsys.readouterr().out
        assert subverb in out or "usage" in out.lower()


class TestStorageDispatch:
    """Storage subverbs must wire to the correct BoxmanManager methods."""

    def test_storage_df_dispatch(self):
        from boxman.manager import BoxmanManager
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(["storage", "df"])
        assert args.func is BoxmanManager.storage_df
        assert args.vms == "all"

    def test_storage_trim_dispatch(self):
        from boxman.manager import BoxmanManager
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(["storage", "trim"])
        assert args.func is BoxmanManager.storage_trim
        assert args.dry_run is False

    def test_storage_compact_defaults(self):
        from boxman.manager import BoxmanManager
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(["storage", "compact"])
        assert args.func is BoxmanManager.storage_compact
        assert args.method == "auto"
        assert args.no_shutdown is False
        assert args.drop_snapshots is False
        assert args.dry_run is False

    def test_storage_compact_flags(self):
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args([
            "storage", "compact",
            "--method", "convert-compressed",
            "--drop-snapshots",
            "--no-shutdown",
            "--dry-run",
        ])
        assert args.method == "convert-compressed"
        assert args.drop_snapshots is True
        assert args.no_shutdown is True
        assert args.dry_run is True

    def test_storage_optimize_dispatch(self):
        from boxman.manager import BoxmanManager
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(["storage", "optimize", "--skip-trim"])
        assert args.func is BoxmanManager.storage_optimize
        assert args.skip_trim is True
        assert args.skip_compact is False

    def test_storage_compact_rejects_bad_method(self):
        import pytest as _pytest
        from boxman.scripts.app import parse_args
        parser = parse_args()
        with _pytest.raises(SystemExit):
            parser.parse_args(["storage", "compact", "--method", "nuke-from-orbit"])

    def test_storage_compress_snapshots_dispatch(self):
        from boxman.manager import BoxmanManager
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(["storage", "compress-snapshots"])
        assert args.func is BoxmanManager.storage_compress_snapshots
        assert args.level == 3
        assert args.decompress is False

    def test_storage_compress_snapshots_decompress_flag(self):
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(["storage", "compress-snapshots",
                                  "--decompress", "--level", "10"])
        assert args.decompress is True
        assert args.level == 10

    def test_snapshot_take_compress_memory_flag(self):
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(["snapshot", "take", "--compress-memory",
                                  "--memory-compress-level", "5"])
        assert args.compress_memory is True
        assert args.memory_compress_level == 5

    def test_snapshot_take_compress_memory_default_off(self):
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(["snapshot", "take"])
        assert args.compress_memory is False
        assert args.memory_compress_level == 3

    def test_snapshot_take_cluster_scope(self):
        from boxman.manager import BoxmanManager
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(
            ["snapshot", "take", "--cluster", "cluster_2", "--vms", "node01"])
        assert args.func is BoxmanManager.snapshot_take
        assert args.cluster == "cluster_2"
        assert args.vms == "node01"

    def test_snapshot_take_cluster_defaults_none(self):
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(["snapshot", "take"])
        assert args.cluster is None
        assert args.vms == "all"

    def test_snapshot_restore_cluster_scope(self):
        from boxman.manager import BoxmanManager
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(
            ["snapshot", "restore", "--cluster", "cluster_1", "--name", "s1"])
        assert args.func is BoxmanManager.snapshot_restore
        assert args.cluster == "cluster_1"
        assert args.snapshot_name == "s1"

    def test_snapshot_delete_cluster_scope(self):
        from boxman.manager import BoxmanManager
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(
            ["snapshot", "delete", "--cluster", "cluster_2", "--name", "s1"])
        assert args.func is BoxmanManager.snapshot_delete
        assert args.cluster == "cluster_2"

    def test_snapshot_collapse_requires_to(self):
        import pytest as _pytest
        from boxman.scripts.app import parse_args
        parser = parse_args()
        with _pytest.raises(SystemExit):
            parser.parse_args(["snapshot", "collapse"])

    def test_snapshot_collapse_dispatch(self):
        from boxman.manager import BoxmanManager
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(
            ["snapshot", "collapse", "--to", "before-slurm"])
        assert args.func is BoxmanManager.snapshot_collapse
        assert args.target == "before-slurm"
        assert args.dry_run is False
        assert args.no_shutdown is False
        assert args.yes is False

    def test_snapshot_log_dispatch(self):
        from boxman.manager import BoxmanManager
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(["snapshot", "log"])
        assert args.func is BoxmanManager.snapshot_log
        assert args.vms == "all"
        assert args.max_count is None
        assert args.as_json is False
        assert args.reverse is False
        assert args.no_graph is False

    def test_snapshot_log_all_flags(self):
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args([
            "snapshot", "log",
            "--vms", "node01,node02",
            "-n", "5",
            "--json",
            "--reverse",
            "--no-graph",
        ])
        assert args.vms == "node01,node02"
        assert args.max_count == 5
        assert args.as_json is True
        assert args.reverse is True
        assert args.no_graph is True

    def test_snapshot_collapse_all_flags(self):
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args([
            "snapshot", "collapse",
            "--to", "before-slurm",
            "--vms", "node01,node02",
            "--no-shutdown",
            "--dry-run",
            "--yes",
        ])
        assert args.target == "before-slurm"
        assert args.vms == "node01,node02"
        assert args.no_shutdown is True
        assert args.dry_run is True
        assert args.yes is True


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


class TestImageDispatch:
    """`image` subverbs must wire to the correct BoxmanManager methods."""

    def test_image_inspect_dispatch(self):
        from boxman.manager import BoxmanManager
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(["image", "inspect", "oci://reg/repo:tag"])
        assert args.func is BoxmanManager.inspect_image
        assert args.image_ref == "oci://reg/repo:tag"

    def test_image_push_dispatch(self):
        from boxman.manager import BoxmanManager
        from boxman.scripts.app import parse_args
        parser = parse_args()
        args = parser.parse_args(
            ["image", "push", "reg/repo:tag", "--qcow2", "/tmp/disk.qcow2"])
        assert args.func is BoxmanManager.push_image
        assert args.image_ref == "reg/repo:tag"
        assert args.qcow2 == "/tmp/disk.qcow2"
        assert args.metadata is None
