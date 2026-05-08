"""
Unit tests for the lane-based ASCII graph layout used by
``boxman snapshot log``.

The algorithm is pure-function — feed it ordered (newest-first) row
dicts with ``name``/``parent`` keys and it returns a list of
``(graph_prefix, row | None)`` tuples ready to print.
"""

from __future__ import annotations

import pytest

from boxman.utils.snapshot_graph import (
    _collapse_duplicate_lanes,
    _row_prefix,
    render_graph,
)


pytestmark = pytest.mark.unit


def _names(entries):
    """Helper: extract just the snapshot name (or '' for transitions)."""
    return [(prefix.rstrip(), row['name'] if row else '')
            for prefix, row in entries]


class TestRenderGraphLinear:

    def test_empty_input(self):
        assert render_graph([]) == []

    def test_single_snapshot(self):
        rows = [{'name': 'only', 'parent': None}]
        assert _names(render_graph(rows)) == [('*', 'only')]

    def test_three_step_linear_chain(self):
        rows = [
            {'name': 'c', 'parent': 'b'},
            {'name': 'b', 'parent': 'a'},
            {'name': 'a', 'parent': None},
        ]
        # Single lane all the way down — no transitions.
        assert _names(render_graph(rows)) == [
            ('*', 'c'),
            ('*', 'b'),
            ('*', 'a'),
        ]


class TestRenderGraphDivergence:

    def test_two_tip_divergence_collapses_with_pipe_slash(self):
        rows = [
            {'name': 'foo', 'parent': 'base'},
            {'name': 'bar', 'parent': 'base'},
            {'name': 'base', 'parent': None},
        ]
        out = _names(render_graph(rows))
        # foo on lane 0, bar on lane 1, then |/ transition, then base on lane 0.
        assert out == [
            ('* |', 'foo'),
            ('| *', 'bar'),
            ('|/', ''),
            ('*', 'base'),
        ]

    def test_three_tip_divergence_collapses_in_two_steps(self):
        rows = [
            {'name': 'a', 'parent': 'base'},
            {'name': 'b', 'parent': 'base'},
            {'name': 'c', 'parent': 'base'},
            {'name': 'base', 'parent': None},
        ]
        out = _names(render_graph(rows))
        # First two collapse, then c collapses with the surviving lane.
        assert ('* | |', 'a') in out
        assert ('| * |', 'b') in out
        assert ('| *', 'c') in out
        assert ('*', 'base') in out
        # Two transition rows are emitted.
        transitions = [p for p, n in out if n == '']
        assert any('/' in t for t in transitions)


class TestRowPrefix:

    def test_single_lane(self):
        assert _row_prefix(['foo'], 0) == '* '

    def test_two_lanes_first(self):
        assert _row_prefix(['foo', 'bar'], 0) == '* | '

    def test_two_lanes_second(self):
        assert _row_prefix(['foo', 'bar'], 1) == '| * '

    def test_terminated_lane_renders_as_space(self):
        # A None lane (chain bottoms out) becomes a space placeholder.
        assert _row_prefix(['foo', None, 'bar'], 0) == '*   | '


class TestCollapseDuplicateLanes:

    def test_no_duplicates_returns_input_unchanged(self):
        lanes = ['a', 'b', 'c']
        new, transition = _collapse_duplicate_lanes(lanes)
        assert new == lanes
        assert transition == ''

    def test_two_lanes_with_same_parent_collapse(self):
        lanes = ['p', 'p']
        new, transition = _collapse_duplicate_lanes(lanes)
        assert new == ['p']
        # Tightened "| /" → "|/" for visual alignment with git's output.
        assert '|/' in transition

    def test_collapse_preserves_unrelated_lanes(self):
        lanes = ['p', 'p', 'other']
        new, transition = _collapse_duplicate_lanes(lanes)
        assert new == ['p', 'other']
        assert '|/' in transition or '/' in transition

    def test_none_lanes_render_as_space_in_transition(self):
        lanes = ['p', None, 'p']
        new, transition = _collapse_duplicate_lanes(lanes)
        assert new == ['p', None]
        # The middle column is a None lane → rendered as a space.
        assert ' ' in transition


class TestParentTermination:

    def test_chain_ending_at_none_drops_lane(self):
        rows = [
            {'name': 'top', 'parent': None},
        ]
        out = render_graph(rows)
        # Only one row, no continuation.
        assert len(out) == 1
        assert out[0][1]['name'] == 'top'
