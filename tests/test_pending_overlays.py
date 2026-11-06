"""
The overlays of a VM's CD-ROM images a restore left for later removal
(:mod:`boxman.providers.libvirt.pending_overlays`): one record per chain,
each overlay with its identity as recorded.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from boxman import config_cache
from boxman.providers.libvirt.pending_overlays import (
    PendingOverlays,
    identity_of,
    ledger_dir,
)

pytestmark = pytest.mark.unit

VM = "bprj__demo__bprj_c1_vm01"


@pytest.fixture
def iso(tmp_path: Path) -> Path:
    path = tmp_path / "nixos.iso"
    path.write_bytes(b"the ISO")
    return path


def _overlay(iso: Path, epoch: int) -> str:
    path = iso.with_name(f"nixos.{epoch}")
    path.write_bytes(b"QFI\xfb")
    return str(path)


class TestTheRecords:

    def test_they_are_in_the_per_user_cache_dir(self):
        assert ledger_dir() == os.path.join(
            os.path.expanduser(config_cache.DEFAULT_CACHE_DIR),
            "pending-iso-overlays")

    def test_a_chain_is_recorded_with_each_overlays_identity(self, iso):
        top, under = _overlay(iso, 2), _overlay(iso, 1)
        ledger = PendingOverlays()

        ledger.record(VM, [([top, under], str(iso))])

        [chain] = ledger.chains(VM)
        assert (chain.vm, chain.iso, chain.layers) == (VM, str(iso),
                                                       [top, under])
        assert chain.identities == [identity_of(top), identity_of(under)]
        assert chain.recorded > 0

    def test_an_overlay_that_is_not_there_is_left_out(self, iso):
        top = _overlay(iso, 2)
        ledger = PendingOverlays()
        ledger.record(VM, [([top, str(iso.with_name("nixos.1"))], str(iso)),
                           ([str(iso.with_name("nixos.0"))], str(iso))])
        assert [chain.layers for chain in ledger.chains(VM)] == [[top]]

    def test_a_chain_recorded_again_replaces_its_record(self, iso):
        top = _overlay(iso, 2)
        ledger = PendingOverlays()
        ledger.record(VM, [([top], str(iso))])
        ledger.record(VM, [([top], str(iso))])
        assert len(ledger.chains(VM)) == 1

    def test_each_vm_has_its_own(self, iso):
        ledger = PendingOverlays()
        ledger.record(VM, [([_overlay(iso, 1)], str(iso))])
        ledger.record("other01", [([_overlay(iso, 2)], str(iso))])
        assert [c.vm for c in ledger.chains(VM)] == [VM]
        assert len(ledger.chains()) == 2

    def test_one_forgotten_is_gone(self, iso):
        top = _overlay(iso, 1)
        ledger = PendingOverlays()
        ledger.record(VM, [([top], str(iso))])
        ledger.forget(VM, [top, "/never/recorded"])
        assert ledger.chains(VM) == []

    def test_nothing_is_left_half_written(self, iso):
        ledger = PendingOverlays()
        ledger.record(VM, [([_overlay(iso, 1)], str(iso))])
        assert all(name.endswith(".json")
                   for name in os.listdir(ledger.directory))


class TestOneThatCannotBeRead:

    @pytest.mark.parametrize("text", [
        "{not json",
        json.dumps({"version": 99}),
        json.dumps({"version": 1, "vm": VM, "iso": "relative.iso",
                    "layers": ["/a"], "identities": [None]}),
        json.dumps({"version": 1, "vm": VM, "iso": "/x.iso",
                    "layers": ["/a", "/b"], "identities": [[1, 2]]}),
        json.dumps({"version": 1, "vm": VM, "iso": "/x.iso",
                    "layers": ["/a"], "identities": [[1, True]]}),
    ], ids=["not-json", "another-version", "relative", "short", "bool"])
    def test_is_named_and_left_alone(self, text):
        ledger = PendingOverlays()
        os.makedirs(ledger.directory)
        path = os.path.join(ledger.directory, "0123.json")
        Path(path).write_text(text)

        assert ledger.chains(VM) == []
        assert list(ledger.unreadable) == [path]
        ledger.settle(VM)
        assert os.path.exists(path)


class TestSettling:
    """After a removal: what is gone, or is no longer the file recorded,
    drops out of its record; a record left with nothing goes."""

    def test_a_record_whose_overlays_are_gone_goes(self, iso):
        top = _overlay(iso, 1)
        ledger = PendingOverlays()
        ledger.record(VM, [([top], str(iso))])
        os.unlink(top)

        ledger.settle(VM)

        assert ledger.chains(VM) == []

    def test_one_kept_stays_on_record(self, iso):
        top, under = _overlay(iso, 2), _overlay(iso, 1)
        ledger = PendingOverlays()
        ledger.record(VM, [([top, under], str(iso))])
        os.unlink(top)

        ledger.settle(VM)

        [chain] = ledger.chains(VM)
        assert chain.layers == [under]
        assert chain.identities == [identity_of(under)]

    def test_a_file_that_took_the_name_since_drops_out(self, iso):
        top = _overlay(iso, 1)
        ledger = PendingOverlays()
        ledger.record(VM, [([top], str(iso))])
        newcomer = iso.with_name("newcomer")
        newcomer.write_bytes(b"another file")
        os.replace(newcomer, top)

        ledger.settle(VM)

        assert ledger.chains(VM) == [] and os.path.exists(top)

    def test_another_vms_records_are_left_alone(self, iso):
        mine, theirs = _overlay(iso, 1), _overlay(iso, 2)
        ledger = PendingOverlays()
        ledger.record(VM, [([mine], str(iso))])
        ledger.record("other01", [([theirs], str(iso))])
        os.unlink(theirs)

        ledger.settle(VM)

        assert len(ledger.chains("other01")) == 1
