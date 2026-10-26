"""
``wait_for_vm_ips`` and a VM that stops running while it is waited for.

An ISO-boot guest left off by its installer's first reboot (G2 in
doc/windows-guests.md) has no lease coming, and the first ``boxman up``
spent its whole 600 s IP wait on it. Stopping is reported, not raised: the
proxmox box's unattended install powers its guest off by design.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from boxman.manager import BoxmanManager

pytestmark = pytest.mark.unit

VM1 = "bprj__demo__bprj_c1_vm1"
VM2 = "bprj__demo__bprj_c1_vm2"
IP = {"vnet0": "192.168.1.10"}


class Lab:
    """libvirt as the wait sees it, one round per sleep: *states* maps a
    round to ``_vm_states_if_known``'s answer, *ips* a (round, name) to an
    address lookup's."""

    def __init__(self, monkeypatch, states, ips):
        self.round = 0
        self.sleeps: list[int] = []
        self.asked: list[tuple[int, str]] = []
        self.mgr = BoxmanManager.__new__(BoxmanManager)
        self.mgr.logger = MagicMock()
        self.mgr.provider = MagicMock()
        self.mgr._vm_states_if_known = lambda: states(self.round)
        self.mgr.provider.get_vm_ip_addresses.side_effect = self._ip
        self._ips = ips
        monkeypatch.setattr("boxman.manager_parts.networks.time.sleep",
                            self._sleep)

    def _ip(self, name):
        self.asked.append((self.round, name))
        return self._ips(self.round, name)

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        self.round += 1

    def warnings(self) -> str:
        return " ".join(str(c.args[0])
                        for c in self.mgr.logger.warning.call_args_list)


class TestAVmThatStops:

    @staticmethod
    def _vm2_shuts_off_in_round_2(monkeypatch) -> Lab:
        return Lab(monkeypatch,
                   states=lambda r: {VM1: "running",
                                     VM2: "shut off" if r >= 2 else "running"},
                   ips=lambda r, name: IP if name == VM1 else {})

    def test_is_no_longer_waited_for(self, monkeypatch):
        lab = self._vm2_shuts_off_in_round_2(monkeypatch)

        assert lab.mgr.wait_for_vm_ips([VM1, VM2], max_wait=600) is False

        assert sum(lab.sleeps) == 3       # rounds 0 and 1, not 600 s
        assert VM2 in lab.warnings() and "shut off" in lab.warnings()

    def test_is_not_asked_for_an_address_once_it_stopped(self, monkeypatch):
        lab = self._vm2_shuts_off_in_round_2(monkeypatch)
        lab.mgr.wait_for_vm_ips([VM1, VM2], max_wait=600)
        assert (2, VM2) not in lab.asked

    def test_is_not_an_error(self, monkeypatch):
        lab = self._vm2_shuts_off_in_round_2(monkeypatch)
        lab.mgr.wait_for_vm_ips([VM1, VM2], max_wait=600)
        lab.mgr.logger.error.assert_not_called()

    def test_that_never_ran_costs_no_wait_at_all(self, monkeypatch):
        lab = Lab(monkeypatch, states=lambda r: {VM1: "shut off"},
                  ips=lambda r, name: {})
        assert lab.mgr.wait_for_vm_ips([VM1], max_wait=600) is False
        assert lab.sleeps == []
        assert lab.asked == []

    def test_that_libvirt_no_longer_lists_is_not_waited_for(self, monkeypatch):
        lab = Lab(monkeypatch, states=lambda r: {},
                  ips=lambda r, name: {})
        assert lab.mgr.wait_for_vm_ips([VM1], max_wait=600) is False
        assert lab.sleeps == []


class TestTheWaitOtherwise:

    def test_ends_when_every_vm_has_an_address(self, monkeypatch):
        lab = Lab(monkeypatch, states=lambda r: {VM1: "running", VM2: "running"},
                  ips=lambda r, name: IP if r >= 3 else {})
        assert lab.mgr.wait_for_vm_ips([VM1, VM2], max_wait=600) is True
        assert lab.sleeps == [1, 2, 4]

    def test_does_not_ask_again_for_a_vm_that_has_one(self, monkeypatch):
        lab = Lab(monkeypatch, states=lambda r: {VM1: "running", VM2: "running"},
                  ips=lambda r, name: IP if name == VM1 or r >= 2 else {})
        lab.mgr.wait_for_vm_ips([VM1, VM2], max_wait=600)
        assert [name for _, name in lab.asked].count(VM1) == 1

    def test_gives_up_after_max_wait(self, monkeypatch):
        lab = Lab(monkeypatch, states=lambda r: {VM1: "running"},
                  ips=lambda r, name: {})
        assert lab.mgr.wait_for_vm_ips([VM1], max_wait=10) is False
        assert sum(lab.sleeps) >= 10
        assert "after 10s" in lab.warnings()

    def test_waits_for_every_vm_when_the_states_cannot_be_read(self, monkeypatch):
        """None from _vm_states_if_known: every VM counts as running, as
        before the wait asked."""
        lab = Lab(monkeypatch, states=lambda r: None,
                  ips=lambda r, name: IP if r >= 2 else {})
        assert lab.mgr.wait_for_vm_ips([VM1], max_wait=600) is True
        assert lab.sleeps == [1, 2]

    def test_an_empty_list_is_done_at_once(self, monkeypatch):
        lab = Lab(monkeypatch, states=lambda r: {}, ips=lambda r, name: {})
        assert lab.mgr.wait_for_vm_ips([], max_wait=600) is True
        assert lab.sleeps == [] and lab.asked == []
