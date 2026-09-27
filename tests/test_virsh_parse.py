"""
Golden-output tests for boxman.providers.libvirt.virsh_parse.

The fixtures are realistic ``virsh domblklist --details`` / ``virsh
domiflist`` captures, including the edge cases the shared parsers were
built for (#85 item 32): disk paths containing spaces, empty device
lists, cdrom entries with and without media.
"""

from __future__ import annotations

import pytest

from boxman.providers.libvirt.virsh_parse import (
    DomblkRow,
    DomifRow,
    parse_domblklist,
    parse_domblklist_strict,
    parse_domiflist,
)

pytestmark = pytest.mark.unit

DOMBLKLIST_GOLDEN = """\
 Type   Device   Target   Source
------------------------------------------------
 file   disk     vda      /var/lib/libvirt/images/boxman-vm01.qcow2
 file   disk     vdb      /vm images/boxman-vm01_data with spaces.qcow2
 file   cdrom    hda      /var/lib/libvirt/images/seed-vm01.iso
 file   cdrom    hdb      -
 block  disk     vdc      /dev/sdb1
"""

DOMBLKLIST_EMPTY = """\
 Type   Device   Target   Source
------------------------------------------------
"""

DOMBLKLIST_HEADER_ONLY_NO_SEPARATOR = """\
Type   Device  Target  Source
file   cdrom   hdc     /iso/seed.iso
file   disk    vda     /vms/vm.qcow2
"""

DOMIFLIST_GOLDEN = """\
Interface   Type       Source     Model       MAC
------------------------------------------------------
vnet0       network    default    virtio      52:54:00:aa:bb:cc
vnet1       bridge     br0        virtio      52:54:00:aa:bb:dd
"""

DOMIFLIST_EMPTY = """\
Interface   Type       Source     Model       MAC
------------------------------------------------------
"""


class TestParseDomblklist:

    def test_parses_all_rows(self):
        rows = parse_domblklist(DOMBLKLIST_GOLDEN)
        assert rows == [
            DomblkRow('file', 'disk', 'vda',
                      '/var/lib/libvirt/images/boxman-vm01.qcow2'),
            DomblkRow('file', 'disk', 'vdb',
                      '/vm images/boxman-vm01_data with spaces.qcow2'),
            DomblkRow('file', 'cdrom', 'hda',
                      '/var/lib/libvirt/images/seed-vm01.iso'),
            DomblkRow('file', 'cdrom', 'hdb', '-'),
            DomblkRow('block', 'disk', 'vdc', '/dev/sdb1'),
        ]

    def test_path_with_spaces_survives(self):
        rows = parse_domblklist(DOMBLKLIST_GOLDEN)
        assert rows[1].source == '/vm images/boxman-vm01_data with spaces.qcow2'

    def test_empty_device_list(self):
        assert parse_domblklist(DOMBLKLIST_EMPTY) == []

    def test_empty_output(self):
        assert parse_domblklist('') == []
        assert parse_domblklist('\n') == []

    def test_header_without_separator_line(self):
        rows = parse_domblklist(DOMBLKLIST_HEADER_ONLY_NO_SEPARATOR)
        assert [row.target for row in rows] == ['hdc', 'vda']

    def test_missing_source_column_is_none(self):
        rows = parse_domblklist("file   cdrom   hdc\n")
        assert rows == [DomblkRow('file', 'cdrom', 'hdc', None)]

    def test_short_line_skipped(self):
        rows = parse_domblklist("garbage\nfile   disk   vda   /x.qcow2\n")
        assert rows == [DomblkRow('file', 'disk', 'vda', '/x.qcow2')]


class TestParseDomblklistStrict:
    """For callers that need every source, where a skipped line or a
    missing Source column would read as "no such device" (#212 review
    round 2, R2-3)."""

    def test_complete_table_parses_like_the_lenient_parser(self):
        assert (parse_domblklist_strict(DOMBLKLIST_GOLDEN)
                == parse_domblklist(DOMBLKLIST_GOLDEN))

    def test_an_explicitly_empty_slot_is_a_complete_answer(self):
        rows = parse_domblklist_strict(DOMBLKLIST_GOLDEN)
        assert DomblkRow('file', 'cdrom', 'hdb', '-') in rows

    def test_empty_device_list_is_complete(self):
        assert parse_domblklist_strict(DOMBLKLIST_EMPTY) == []

    def test_none_when_a_line_cannot_be_read(self):
        assert parse_domblklist_strict(
            "garbage\nfile   disk   vda   /x.qcow2\n") is None

    def test_none_when_a_source_column_is_missing(self):
        assert parse_domblklist_strict("file   cdrom   hdc\n") is None


class TestParseDomiflist:

    def test_parses_all_rows(self):
        rows = parse_domiflist(DOMIFLIST_GOLDEN)
        assert rows == [
            DomifRow('vnet0', 'network', 'default', 'virtio',
                     '52:54:00:aa:bb:cc'),
            DomifRow('vnet1', 'bridge', 'br0', 'virtio',
                     '52:54:00:aa:bb:dd'),
        ]

    def test_empty_device_list(self):
        assert parse_domiflist(DOMIFLIST_EMPTY) == []

    def test_empty_output(self):
        assert parse_domiflist('') == []

    def test_short_row_padded(self):
        rows = parse_domiflist("vnet0   network   default\n")
        assert rows == [DomifRow('vnet0', 'network', 'default', '-', '-')]
