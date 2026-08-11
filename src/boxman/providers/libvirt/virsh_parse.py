"""
Shared parsers for ``virsh`` tabular output.

``virsh domblklist --details`` and ``virsh domiflist`` print fixed-column
tables with a header row and a dashed separator row.  The parsers below
skip both, tolerate empty output, and — for ``domblklist`` — split the
Source column with ``maxsplit=3`` so disk paths containing spaces survive
(a trailing ``-`` printed by virsh for an empty source is kept verbatim;
a genuinely missing column comes back as ``None``).
"""

from typing import NamedTuple


class DomblkRow(NamedTuple):
    """One row of ``virsh domblklist --details`` output."""

    type: str          # file, block, ...
    device: str        # disk, cdrom, ...
    target: str        # vda, hda, ...
    source: str | None  # path, '-' for an empty slot, None if column missing


class DomifRow(NamedTuple):
    """One row of ``virsh domiflist`` output."""

    interface: str     # vnet0, ...
    type: str          # network, bridge, direct, ...
    source: str        # network/bridge name
    model: str         # virtio, ...
    mac: str


def parse_domblklist(output: str) -> list[DomblkRow]:
    """
    Parse ``virsh domblklist --details`` output into rows.

    Header and separator lines are skipped, as are lines with fewer than
    three columns.  The Source column is split with ``maxsplit=3`` so
    paths containing spaces survive; a missing Source column yields
    ``None`` (virsh prints ``-`` for an empty slot, which is kept as-is).
    """
    rows = []
    for line in output.splitlines():
        if _is_header_or_separator(line):
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        rows.append(DomblkRow(
            type=parts[0],
            device=parts[1],
            target=parts[2],
            source=parts[3] if len(parts) >= 4 else None,
        ))
    return rows


def parse_domiflist(output: str) -> list[DomifRow]:
    """
    Parse ``virsh domiflist`` output into rows.

    Header and separator lines are skipped, as are lines with fewer than
    three columns; rows short of the full five columns are padded with
    ``-`` so callers can read any column uniformly.
    """
    rows = []
    for line in output.splitlines():
        if _is_header_or_separator(line):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        parts += ['-'] * (5 - len(parts))
        rows.append(DomifRow(
            interface=parts[0],
            type=parts[1],
            source=parts[2],
            model=parts[3],
            mac=parts[4],
        ))
    return rows


def _is_header_or_separator(line: str) -> bool:
    """True for the table's header row and its dashed separator row."""
    stripped = line.strip()
    if not stripped:
        return True
    if set(stripped) <= {'-', ' '}:
        return True
    first = stripped.split(None, 1)[0]
    return first in ('Type', 'Device', 'Target', 'Source', 'Interface')
