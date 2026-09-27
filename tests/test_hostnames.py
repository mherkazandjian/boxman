"""
Unit tests for boxman.utils.hostnames.expand_name_range.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import pytest

from boxman.utils.hostnames import expand_name_range, hostname_problem

pytestmark = pytest.mark.unit


class TestExpandNameRange:

    def test_two_digit_padded_range(self):
        assert expand_name_range("node0[1:3]") == ["node01", "node02", "node03"]

    def test_single_digit_range(self):
        assert expand_name_range("host[1:3]") == ["host1", "host2", "host3"]

    def test_range_with_suffix(self):
        assert (
            expand_name_range("web0[1:2].example.com")
            == ["web01.example.com", "web02.example.com"]
        )

    def test_range_starting_from_zero(self):
        assert expand_name_range("node0[0:2]") == ["node00", "node01", "node02"]

    def test_single_element_range(self):
        assert expand_name_range("node[5:5]") == ["node5"]

    def test_three_digit_padding(self):
        out = expand_name_range("server[001:003]")
        assert out == ["server001", "server002", "server003"]

    def test_padding_width_derived_from_start(self):
        # start is two digits ('01'); end 100 keeps the same two-digit pad
        # format, so formatted values widen naturally for numbers >= 100.
        out = expand_name_range("host[01:100]")
        assert out[0] == "host01"
        assert out[9] == "host10"
        assert out[99] == "host100"

    def test_missing_brackets_raises(self):
        with pytest.raises(AttributeError):
            expand_name_range("node-no-range")

    def test_missing_colon_raises(self):
        with pytest.raises(ValueError):
            expand_name_range("node[1-3]")

    def test_non_integer_range_raises(self):
        with pytest.raises(ValueError):
            expand_name_range("node[a:b]")


class TestHostnameProblem:
    """Validation of a guest hostname before it reaches a guest (#200)."""

    @pytest.mark.parametrize("value", [
        "node01",
        "a",
        "ctrl.example.com",
        "Node-01",
        "a" * 63,
        ".".join(["a" * 63] * 3) + ".abcdefghijklmnopqrstuvwxyz0123456789ab",
    ])
    def test_accepts_valid_names(self, value):
        assert hostname_problem(value) is None

    @pytest.mark.parametrize("value, fragment", [
        ("-bad", "inner hyphens"),
        ("bad-", "inner hyphens"),
        ("a" * 64, "at most 63"),
        ("a." * 126 + "ab", "at most 253"),
        ("my_vm", "inner hyphens"),
        ("x..y", "empty label"),
        ("trailing.", "empty label"),
        ("", "must not be empty"),
        ("sp ace", "inner hyphens"),
    ])
    def test_refuses_invalid_names(self, value, fragment):
        problem = hostname_problem(value)
        assert problem is not None and fragment in problem

    @pytest.mark.parametrize("value, fragment", [
        (True, "boolean"),
        (False, "boolean"),
        (42, "int"),
        (1.5, "float"),
    ])
    def test_refuses_what_yaml_turned_into_a_non_string(self, value, fragment):
        """``hostname: 101`` / ``hostname: no`` are not what the author meant."""
        problem = hostname_problem(value)
        assert problem is not None and fragment in problem

    @pytest.mark.parametrize("value", [
        "node01\n", "node01\r\n", "node\n01", "node01\n.example.com", "node01 ",
    ])
    def test_refuses_line_breaks_and_trailing_space(self, value):
        """From Codex's review: Python's $ matches before a final newline, so
        a YAML block scalar's trailing newline passed validation."""
        assert hostname_problem(value) is not None

    def test_the_length_limit_counts_the_dots(self):
        assert len("a." * 126 + "a") == 253
        assert hostname_problem("a." * 126 + "a") is None
