"""
The closing summary for clones that kept their template's identity (#202).

The per-clone warning is emitted inside a ``multiprocessing`` worker, right
where the clone happens, and on a project of any size that is thousands of
lines before the prompt returns. These tests cover the two things that make it
hard to miss instead: the records travelling back from the worker, and the
one-line-per-VM summary at the end of the run.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from tests.conftest import make_bare_manager

from boxman.exceptions import ProvisionError
from boxman.providers.libvirt.clone_vm import (
    CloneDegradation,
)

pytestmark = pytest.mark.unit


def _record(vm: str, properties=("machine id",), reason="the offline identity pass failed",
            policies=("clone_machine_id=auto",)) -> CloneDegradation:
    return CloneDegradation(
        vm=vm,
        properties=tuple(properties),
        policies=tuple(policies),
        reason=reason,
        cause="virt-sysprep could not inspect the guest",
        message=f"could not complete the offline identity pass for vm {vm}",
    )


def _warnings(manager) -> list[str]:
    """The warnings the manager emitted.

    ``make_bare_manager`` installs a MagicMock logger, so the messages are
    read off the mock rather than through caplog.
    """
    return [call.args[0] for call in manager.logger.warning.call_args_list]


class TestCollectCloneDegradations:

    def test_collects_records_from_every_worker(self):
        manager = make_bare_manager()
        manager.collect_clone_degradations({
            "vm01": [_record("vm01")],
            "vm02": [_record("vm02")],
            "vm03": [],
        })
        assert [r.vm for r in manager._clone_degradations] == ["vm01", "vm02"]

    def test_accumulates_across_batches(self):
        """provision clones once, update clones again into the same manager."""
        manager = make_bare_manager()
        manager.collect_clone_degradations({"vm01": [_record("vm01")]})
        manager.collect_clone_degradations({"vm02": [_record("vm02")]})
        assert len(manager._clone_degradations) == 2

    @pytest.mark.parametrize("results", [None, {}, {"vm01": None}])
    def test_nothing_to_collect_is_not_an_error(self, results):
        manager = make_bare_manager()
        manager.collect_clone_degradations(results)
        assert getattr(manager, "_clone_degradations", []) == []

    def test_ignores_payloads_that_are_not_records(self):
        """A worker's return value is whatever that target returned."""
        manager = make_bare_manager()
        manager.collect_clone_degradations(
            {"vm01": ["a bare string", 42, _record("vm01")]})
        assert [r.vm for r in manager._clone_degradations] == ["vm01"]


class TestReportCloneDegradations:

    def test_a_clean_run_reports_nothing(self):
        manager = make_bare_manager()
        manager.report_clone_degradations()
        assert _warnings(manager) == []

    def test_one_line_per_degraded_vm(self):
        manager = make_bare_manager()
        manager.collect_clone_degradations({
            "vm02": [_record("vm02", properties=("machine id", "ssh host keys"),
                             policies=("clone_machine_id=auto",
                                       "clone_ssh_host_keys=auto"))],
            "vm01": [_record("vm01")],
        })
        manager.report_clone_degradations()

        lines = _warnings(manager)
        per_vm = [line for line in lines if line.strip().startswith("vm")]
        assert len(per_vm) == 2
        # sorted, so the summary reads the same way on every run regardless of
        # the order workers happened to finish in
        assert per_vm[0].strip().startswith("vm01:")
        assert per_vm[1].strip().startswith("vm02:")

    def test_each_line_names_the_vm_the_properties_and_the_cause(self):
        manager = make_bare_manager()
        manager.collect_clone_degradations({
            "vm01": [_record(
                "vm01", properties=("machine id", "ssh host keys"),
                reason="a required host tool is missing or not permitted",
                policies=("clone_machine_id=auto", "clone_ssh_host_keys=auto"))],
        })
        manager.report_clone_degradations()

        line = next(
            line for line in _warnings(manager)
            if line.strip().startswith("vm01:"))
        assert "machine id and ssh host keys" in line
        assert "a required host tool is missing or not permitted" in line
        assert "clone_machine_id=auto" in line

    def test_the_summary_says_how_to_fail_closed_instead(self):
        manager = make_bare_manager()
        manager.collect_clone_degradations({"vm01": [_record("vm01")]})
        manager.report_clone_degradations()
        joined = "\n".join(_warnings(manager))
        assert "required" in joined
        assert "virt-sysprep" in joined

    def test_reporting_twice_does_not_repeat_the_same_clone(self):
        manager = make_bare_manager()
        manager.collect_clone_degradations({"vm01": [_record("vm01")]})
        manager.report_clone_degradations()
        first = len(_warnings(manager))
        manager.report_clone_degradations()
        assert len(_warnings(manager)) == first


def _batch(results, failures):
    """A stand-in for ``_run_parallel`` that honours its ``on_result`` contract:
    each success is handed over as it arrives, then the dicts are returned."""
    def run(tasks, op_label="", max_workers=None, on_result=None):
        for label, payload in results.items():
            if on_result is not None:
                on_result(label, payload)
        return results, failures
    return run


class TestReportedHoweverTheRunEnds:
    """From the Copilot review of #204: the summary was skipped on failure."""

    def test_provision_reports_even_when_it_raises(self):
        manager = make_bare_manager({})
        manager.collect_clone_degradations({"vm01": [_record("vm01")]})
        with patch.object(manager, "_provision",
                          side_effect=ProvisionError("vm02 never started")):
            with pytest.raises(ProvisionError, match="never started"):
                manager.provision(None)
        assert any(line.strip().startswith("vm01:")
                   for line in _warnings(manager))

    def test_update_reports_even_when_it_raises(self):
        manager = make_bare_manager({})
        manager.collect_clone_degradations({"vm01": [_record("vm01")]})
        with patch.object(manager, "_update",
                          side_effect=ProvisionError("update finished with 1 failure")):
            with pytest.raises(ProvisionError):
                manager.update(None)
        assert any(line.strip().startswith("vm01:")
                   for line in _warnings(manager))

    def test_a_failed_clone_does_not_swallow_a_degraded_one(self):
        """The case the clone step's own comment promised: one clone fails,
        another in the same batch degrades, and the batch raises."""
        manager = make_bare_manager({"project": "p", "clusters": {"c1": {
            "workdir": "/tmp/boxman-test-workdir",
            "vms": {"vm01": {}, "vm02": {}}}}})
        manager.provider = MagicMock()
        degraded = "bprj__p__bprj_c1_vm01"
        with patch.object(manager, "_ensure_libvirt_storage_pool"), \
             patch.object(manager, "_resolve_iso_config"), \
             patch.object(manager, "_run_parallel", side_effect=_batch(
                 {degraded: [_record(degraded)]},
                 {"bprj__p__bprj_c1_vm02": "CloneSanitizerError: boom"})), \
             patch.object(manager, "_provision",
                          side_effect=lambda _args: manager.clone_vms()):
            with pytest.raises(ProvisionError, match="clone failed"):
                manager.provision(None)
        assert any(line.strip().startswith(f"{degraded}:")
                   for line in _warnings(manager))

    def test_an_interrupted_clone_batch_still_reports_what_came_back(self):
        """Ctrl-C while vm02 is still cloning must not lose vm01's record."""
        manager = make_bare_manager({"project": "p", "clusters": {"c1": {
            "workdir": "/tmp/boxman-test-workdir",
            "vms": {"vm01": {}, "vm02": {}}}}})
        manager.provider = MagicMock()
        degraded = "bprj__p__bprj_c1_vm01"

        def interrupted(tasks, op_label="", max_workers=None, on_result=None):
            on_result(degraded, [_record(degraded)])
            raise KeyboardInterrupt

        with patch.object(manager, "_ensure_libvirt_storage_pool"), \
             patch.object(manager, "_resolve_iso_config"), \
             patch.object(manager, "_run_parallel", side_effect=interrupted), \
             patch.object(manager, "_provision",
                          side_effect=lambda _args: manager.clone_vms()):
            with pytest.raises(KeyboardInterrupt):
                manager.provision(None)
        assert any(line.strip().startswith(f"{degraded}:")
                   for line in _warnings(manager))

    def test_a_successful_run_reports_once(self):
        manager = make_bare_manager({})

        def body(_args):
            manager.collect_clone_degradations({"vm01": [_record("vm01")]})

        with patch.object(manager, "_provision", side_effect=body):
            manager.provision(None)
        assert sum(line.strip().startswith("vm01:")
                   for line in _warnings(manager)) == 1


class TestTheSummaryDoesNotOverclaim:
    """The pass is not atomic, so the summary must not say every property
    was kept when some may already have been reset."""

    def test_each_line_says_may_have_kept(self):
        assert "may have kept" in _record("vm01").summary_line()

    def test_the_header_says_may_have_kept(self):
        manager = make_bare_manager({})
        manager.collect_clone_degradations({"vm01": [_record("vm01")]})
        manager.report_clone_degradations()
        assert "may have kept" in _warnings(manager)[0]
