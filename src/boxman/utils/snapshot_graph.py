"""
Lane-based ASCII graph layout for ``boxman snapshot log``.

Mirrors the small subset of ``git log --graph`` that boxman snapshots
need: a single ``*`` column for the linear case, ``|/`` transitions
when lanes converge, and ``|`` continuations between rows.

Pure functions — no I/O, no virsh calls. Inputs are the aggregated
snapshot rows from :class:`boxman.manager.BoxmanManager.snapshot_log`,
ordered newest-first; outputs are the graph-prefix strings to print
left of each row plus any transition lines that go between them.

Boxman snapshots are linear per-VM (every snapshot has a single
parent), so we never emit ``\\`` (fork) characters — only ``|`` and
``/``. If real branched chains appear later, the algorithm extends
naturally to multi-parent.
"""

from __future__ import annotations


def render_graph(rows: list[dict]) -> list[tuple[str, dict | None]]:
    """
    Compute graph prefixes for *rows* (newest-first).

    Returns a list of ``(graph_prefix, row)`` pairs. ``row`` is ``None``
    for transition rows that carry no snapshot data — only the connector
    line between two snapshot rows.

    Each row is expected to have at least the keys ``name`` and
    ``parent``; *parent* is the snapshot's parent name or ``None`` when
    the snapshot sits at the base of a chain.
    """
    if not rows:
        return []

    referenced_as_parent = {r['parent'] for r in rows if r.get('parent')}
    # Tips = snapshots no other snapshot in the visible set claims as parent.
    tip_names = [r['name'] for r in rows if r['name'] not in referenced_as_parent]

    # Seed lanes with tips in the order they appear in `rows` so that
    # the leftmost lane is always the topmost (newest) snapshot.
    lanes: list[str | None] = list(tip_names)

    output: list[tuple[str, dict | None]] = []

    for row in rows:
        name = row['name']

        if name in lanes:
            lane_idx = lanes.index(name)
        else:
            # Defensive: would happen if `rows` has a snapshot whose
            # tip-status changed because parents were filtered out.
            # Append a new lane so the row still renders.
            lanes.append(name)
            lane_idx = len(lanes) - 1

        output.append((_row_prefix(lanes, lane_idx), row))

        # Replace this lane's expected name with the parent (or None when
        # the chain bottoms out).
        lanes[lane_idx] = row.get('parent')

        # Trim trailing terminated lanes so the prefix doesn't grow
        # unbounded.
        while lanes and lanes[-1] is None:
            lanes.pop()

        # If two lanes now hold the same parent name, the right-side
        # lanes fold into the leftmost — emit a transition.
        new_lanes, transition = _collapse_duplicate_lanes(lanes)
        if transition:
            output.append((transition, None))
            lanes = new_lanes

    return output


def _row_prefix(lanes: list[str | None], this_lane_idx: int) -> str:
    """``* | `` style prefix for a snapshot row.

    One char per lane separated by spaces, plus a trailing space so the
    caller can append the row content directly.
    """
    chars: list[str] = []
    for i, l in enumerate(lanes):
        if i == this_lane_idx:
            chars.append('*')
        elif l is None:
            chars.append(' ')
        else:
            chars.append('|')
    return ' '.join(chars) + ' '


def _collapse_duplicate_lanes(
    lanes: list[str | None],
) -> tuple[list[str | None], str]:
    """
    Return ``(new_lanes, transition_prefix)``.

    If multiple non-``None`` lanes hold the same name, the right-side
    ones fold into the leftmost. The transition prefix uses ``/`` for
    the folding lanes and ``|`` for lanes that continue straight down.
    """
    first_seen: dict[str, int] = {}
    fold_idxs: list[int] = []
    for i, name in enumerate(lanes):
        if name is None:
            continue
        if name in first_seen:
            fold_idxs.append(i)
        else:
            first_seen[name] = i

    if not fold_idxs:
        return lanes, ''

    # Transition line: keep `|` for stable lanes, `/` for folding ones.
    # Collapse the space between adjacent `|` and `/` so the slant visually
    # connects to the lane below — matches `git log --graph` rendering.
    chars: list[str] = []
    fold_set = set(fold_idxs)
    for i, name in enumerate(lanes):
        if i in fold_set:
            chars.append('/')
        elif name is None:
            chars.append(' ')
        else:
            chars.append('|')
    transition = ' '.join(chars).rstrip()
    transition = transition.replace('| /', '|/').replace('| \\', '|\\')

    # Drop the folded lane positions; remaining lanes preserve order.
    new_lanes = [name for i, name in enumerate(lanes) if i not in fold_set]
    return new_lanes, transition
