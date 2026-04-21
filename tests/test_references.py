"""
Unit tests for boxman.utils.references.resolve_reference.

Part of Phase 2.4 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).

Pins the contract that used to live as ``BoxmanManager.fetch_value``.
The class-side wrapper still delegates here; a regression that dropped
either path would show up in the ``TestFetchValue*`` tests in
``test_manager_core.py`` as well.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boxman.utils.references import resolve_reference


pytestmark = pytest.mark.unit


class TestLiterals:

    def test_plain_string_passed_through(self):
        assert resolve_reference("just-a-string") == "just-a-string"

    def test_non_string_types_returned_as_is(self):
        assert resolve_reference(42) == 42
        assert resolve_reference(None) is None
        assert resolve_reference([1, 2, 3]) == [1, 2, 3]
        assert resolve_reference({"k": "v"}) == {"k": "v"}


class TestEnvReferences:

    def test_resolves_to_env_value(self, monkeypatch):
        monkeypatch.setenv("BOXMAN_TEST_REF", "resolved-from-env")
        assert resolve_reference("${env:BOXMAN_TEST_REF}") == "resolved-from-env"

    def test_missing_env_var_raises(self, monkeypatch):
        monkeypatch.delenv("BOXMAN_TEST_MISSING", raising=False)
        with pytest.raises(ValueError, match="is not set"):
            resolve_reference("${env:BOXMAN_TEST_MISSING}")

    def test_env_placeholder_must_span_the_whole_string(self, monkeypatch):
        """Partial matches (embedded placeholders) are not resolved —
        they come back unchanged as a literal."""
        monkeypatch.setenv("VAR", "x")
        assert resolve_reference("prefix-${env:VAR}-suffix") == "prefix-${env:VAR}-suffix"


class TestFileReferences:

    def test_reads_file_contents_stripped(self, tmp_path: Path):
        f = tmp_path / "key.pub"
        f.write_text("ssh-ed25519 AAAA... user@host\n")
        assert resolve_reference(f"file://{f}") == "ssh-ed25519 AAAA... user@host"

    def test_expands_tilde(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        f = tmp_path / "x.txt"
        f.write_text("home-file\n")
        assert resolve_reference("file://~/x.txt") == "home-file"

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            resolve_reference(f"file://{tmp_path / 'nope.txt'}")

    def test_directory_raises(self, tmp_path: Path):
        """A `file://<dir>` reference points at something that isn't
        a regular file — must error loudly rather than returning
        garbage."""
        with pytest.raises(FileNotFoundError):
            resolve_reference(f"file://{tmp_path}")


class TestBoxmanManagerFetchValueStillWorks:
    """The classmethod is now a delegator — confirm it still resolves."""

    def test_fetch_value_delegates(self, monkeypatch):
        from boxman.manager import BoxmanManager

        monkeypatch.setenv("BOXMAN_TEST_DELEGATE", "yes")
        assert BoxmanManager.fetch_value("${env:BOXMAN_TEST_DELEGATE}") == "yes"
