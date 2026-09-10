"""
Which disks boxman may detach (#164 F2).

The removal rule went through four designs, each of which detached
something it should not have. Every one of those scenarios is a test here,
named after what it destroys, so a regression fails with the specific
mistake rather than a generic assertion.
"""

from unittest.mock import MagicMock

import pytest

from boxman.exceptions import ProvisionError
from boxman.providers.libvirt.disk_ownership import (
    DiskRecord,
    plan_disk_removals,
    read_disk_records,
    record_attached_disk,
    records_from_xml,
    records_to_xml,
    unowned_disks,
)

pytestmark = pytest.mark.unit

ROOT = "/vm/bprj__demo__bprj_cluster_node.qcow2"
DATA = "/vm/bprj__demo__bprj_cluster_node_data.qcow2"


def _attached(target, source, size_mb=1024):
    return {'target': target, 'source': source, 'size_mb': size_mb}


def _record(name="data", target="vdb", role="data", source=DATA):
    return DiskRecord(name=name, target=target, role=role, source=source)


class TestTheRuleRemovesWhatItShould:

    def test_undeclared_recorded_disk_is_removed(self):
        removals, refusals = plan_disk_removals(
            records=[_record()],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert [r.name for r in removals] == ['data']
        assert refusals == []

    def test_still_declared_disk_is_kept(self):
        removals, refusals = plan_disk_removals(
            records=[_record()],
            desired_disks=[{'name': 'data', 'target': 'vdb'}],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert removals == []
        assert refusals == []

    def test_already_detached_disk_is_not_reported(self):
        """Nothing to do, and nothing worth telling the operator about."""
        removals, refusals = plan_disk_removals(
            records=[_record()],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT)])

        assert removals == []
        assert refusals == []


class TestTheRootDiskSurvivesEveryDesign:
    """Design 1 -- subtraction: attached minus declared.

    ``get_actual_disks()`` returns every file-backed disk, the root disk
    included, and the root disk is never in ``disks:``. Subtraction
    therefore removes it first.
    """

    def test_root_disk_is_not_removed_when_nothing_is_declared(self):
        removals, _refusals = plan_disk_removals(
            records=[],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT)])

        assert removals == []

    def test_root_disk_is_not_removed_even_when_recorded(self):
        removals, _refusals = plan_disk_removals(
            records=[DiskRecord(name='root', target='vda', role='root',
                                source=ROOT)],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT)])

        assert removals == []


class TestSiblingVmDisksSurvive:
    """Design 2 -- "the file is named after this VM".

    ``disk_path_for(wd, 'db_data', prefix='..._node')`` and
    ``disk_path_for(wd, 'data', prefix='..._node_db')`` produce the same
    path, and a sibling VM named ``node_db`` has a boot disk whose name
    starts with ``node``'s prefix. Nothing in the rule looks at filenames.
    """

    SIBLING_ROOT = "/vm/bprj__demo__bprj_cluster_node_db.qcow2"

    def test_a_sibling_whose_name_extends_this_one_is_untouched(self):
        removals, _refusals = plan_disk_removals(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert [r.source for r in removals] == [DATA]
        assert self.SIBLING_ROOT not in [r.source for r in removals]


class TestReplacementDiskAtAReusedTarget:
    """Design 3 -- a name/target record with no source check.

    The record says "I put ``data`` at ``vdb``". If ``data`` is dropped
    from the config and a different disk is attached at ``vdb``, a rule
    that trusts the target alone detaches the replacement.
    """

    REPLACEMENT = "/vm/bprj__demo__bprj_cluster_node_scratch.qcow2"

    def test_a_replacement_at_the_recorded_target_is_refused(self):
        removals, refusals = plan_disk_removals(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT),
                          _attached('vdb', self.REPLACEMENT)])

        assert removals == []
        assert len(refusals) == 1
        record, reason = refusals[0]
        assert record.name == 'data'
        assert self.REPLACEMENT in reason

    def test_a_declared_disk_claiming_the_target_is_refused(self):
        removals, refusals = plan_disk_removals(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[{'name': 'scratch', 'target': 'vdb'}],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert removals == []
        assert 'claims target vdb' in refusals[0][1]


class TestIndependentOverlayBackedByTheRecordedFile:
    """Design 4 -- adding a backing-chain identity check.

    An overlay created independently, backed by the recorded file, passes
    a backing-chain test: its chain contains the recorded source. Exact
    source matching is what rejects it -- the overlay's own path differs.
    """

    OVERLAY = "/vm/bprj__demo__bprj_cluster_node_data.overlay.qcow2"

    def test_an_overlay_backed_by_the_recorded_disk_is_refused(self):
        removals, refusals = plan_disk_removals(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT),
                          _attached('vdb', self.OVERLAY)])

        assert removals == []
        assert self.OVERLAY in refusals[0][1]

    def test_a_snapshot_head_is_refused_the_same_way(self):
        """A snapshot moves the head; the source no longer matches.

        Phrased as "the source differs", not "while a snapshot exists":
        collapse_to() rebases the existing head rather than restoring the
        original pathname, so collapsing does not necessarily make this
        removable again.
        """
        snap = "/vm/bprj__demo__bprj_cluster_node_data.snap1"
        removals, refusals = plan_disk_removals(
            records=[_record(name='data', target='vdb', source=DATA)],
            desired_disks=[],
            actual_disks=[_attached('vdb', snap)])

        assert removals == []
        assert 'not the' in refusals[0][1]


class TestDomainsWithoutRecords:
    """A VM boxman has no ownership record for is reported, never touched."""

    def test_no_metadata_removes_nothing(self):
        removals, refusals = plan_disk_removals(
            records=None,
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA)])

        assert removals == []
        assert refusals == []

    def test_unowned_disks_are_not_listed_without_metadata(self):
        assert unowned_disks(None, [], [_attached('vdb', DATA)]) == []

    def test_unowned_disks_are_listed_when_metadata_exists(self):
        stray = "/vm/attached-by-hand.qcow2"
        listed = unowned_disks(
            records=[_record()],
            desired_disks=[],
            actual_disks=[_attached('vda', ROOT), _attached('vdb', DATA),
                          _attached('vdc', stray)],
            root_source=ROOT)

        assert [d['source'] for d in listed] == [stray]


class TestMetadataRoundTrip:

    def test_records_survive_a_round_trip(self):
        records = [
            DiskRecord('data', 'vdb', 'data', DATA),
            DiskRecord('root', 'vda', 'root', ROOT),
        ]
        assert records_from_xml(records_to_xml(records)) == records

    def test_unparseable_metadata_raises(self):
        """Not "this domain has no records" -- that is the dangerous read."""
        with pytest.raises(ProvisionError, match='could not parse'):
            records_from_xml("<disks><disk name=")

    def test_incomplete_entry_raises(self):
        with pytest.raises(ProvisionError, match='missing'):
            records_from_xml('<disks><disk name="data" target="vdb"/></disks>')


class TestReadingRecordsFromADomain:

    def _virsh(self, ok=True, stdout="", stderr=""):
        virsh = MagicMock()
        virsh.execute.return_value = MagicMock(
            ok=ok, stdout=stdout, stderr=stderr)
        return virsh

    def test_absent_metadata_is_none_not_empty(self):
        virsh = self._virsh(ok=False, stderr="error: metadata not found")

        assert read_disk_records(virsh, 'node') is None

    def test_a_failed_query_raises(self):
        """A query that could not be answered is not an answer."""
        virsh = self._virsh(ok=False, stderr="error: failed to connect")

        with pytest.raises(ProvisionError, match='could not read'):
            read_disk_records(virsh, 'node')

    def test_records_are_parsed(self):
        virsh = self._virsh(
            ok=True, stdout=records_to_xml([_record()]))

        assert read_disk_records(virsh, 'node') == [_record()]

    def test_recording_a_disk_replaces_a_stale_entry_at_the_target(self):
        """A new disk at a reused target must not leave the old record."""
        virsh = self._virsh(ok=True, stdout=records_to_xml([_record()]))

        record_attached_disk(virsh, 'node', name='scratch', target='vdb',
                             source='/vm/scratch.qcow2')

        written = virsh.execute.call_args.kwargs['set']
        records = records_from_xml(written)
        assert [(r.name, r.source) for r in records] == [
            ('scratch', '/vm/scratch.qcow2')]
