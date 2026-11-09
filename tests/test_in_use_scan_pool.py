"""
The host-wide scans read their domains side by side
(:mod:`boxman.providers.libvirt.scan_pool`), and answer exactly what reading
them one after another answered: the same files, credited to the same
domains, and the same failures.

Each test that is about the pool makes the reads overlap -- the first
listed domain's read waits for a later one's to start -- so it fails where
the domains are read one after another, and checks the answer is still the
one the listing order gives.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from unittest.mock import patch

import pytest

from boxman.providers.libvirt import scan_pool
from boxman.providers.libvirt.disk_cleanup import last_scan_failure
from boxman.providers.libvirt.scan_pool import InOrder, OnceEach
from boxman.providers.libvirt.session import LibVirtSession, _ChainReads
from conftest import domain_listing, domain_uuid
from fake_libvirt_host import FakeHost, Result

pytestmark = pytest.mark.unit

#: how long a read waits for another to start before it gives up: what a
#: test of overlapping reads costs where they do not overlap
OVERLAP_WAIT = 2.0


class Host(FakeHost):
    """:class:`FakeHost`, with domains that can be renamed (keeping their
    UUID) and snapshots, read by name or UUID."""

    def __init__(self) -> None:
        super().__init__()
        #: domain name -> its UUID, where it is not its first name's
        self.uuids: dict[str, str] = {}
        #: domain name -> {snapshot: its XML}
        self.snapshots: dict[str, dict[str, str]] = {}

    def uuid(self, name: str) -> str:
        return self.uuids.get(name, domain_uuid(name))

    def rename(self, old: str, new: str) -> None:
        self.uuids[new] = self.uuid(old)
        self.domains[new] = self.domains.pop(old)
        if old in self.snapshots:
            self.snapshots[new] = self.snapshots.pop(old)

    def undefine(self, name: str) -> None:
        self.domains.pop(name)
        self.snapshots.pop(name, None)

    def _named(self, domain: str) -> str | None:
        """The name of *domain*, given by name or UUID."""
        if domain in self.domains:
            return domain
        return next((name for name in self.domains
                     if self.uuid(name) == domain), None)

    def _list(self, args: tuple) -> Result:
        names = (sorted(self.domains) if "--all" in args
                 else sorted(self.running))
        return Result(stdout=domain_listing(
            args, *((self.uuid(name), name) for name in names)) + "\n")

    def _snapshot_list(self, args: tuple) -> Result:
        name = self._named(args[0])
        if name is None:
            return Result(stderr="error: failed to get domain\n", code=1)
        return Result(stdout="".join(f"{snap}\n" for snap in
                                     self.snapshots.get(name, {})) + "\n")

    def _snapshot_dumpxml(self, args: tuple) -> Result:
        name = self._named(args[0])
        xml = self.snapshots.get(name, {}).get(args[1]) if name else None
        if xml is None:
            return Result(stderr="error: no such snapshot\n", code=1)
        return Result(stdout=xml)


def _snapshot_xml(name: str, memory: str) -> str:
    return (f"<domainsnapshot><name>{name}</name><state>running</state>"
            f"<memory snapshot='external' file='{memory}'/>"
            f"<disks/></domainsnapshot>\n")


def _host(*domains: tuple[str, list[str]]) -> Host:
    """A host where each of *domains* -- ``(name, [disk, ...])`` -- uses
    readable images, one disk per target."""
    host = Host()
    for name, disks in domains:
        rows = []
        for i, path in enumerate(disks):
            if path not in host.files:
                host.add(path, readable=True)
            rows.append(("file", "disk", f"vd{chr(ord('a') + i)}", path))
        host.define_domain(name, *rows)
    return host


def _session(workers=None) -> LibVirtSession:
    libvirt = {} if workers is None else {"in_use_scan_workers": workers}
    return LibVirtSession(config={"provider": {"libvirt": libvirt}})


def _scan(host: Host, workers=None, method="disk_paths_in_use", **kwargs):
    with patch("boxman.providers.libvirt.commands._shell_run",
               side_effect=host.run):
        return getattr(_session(workers), method)(**kwargs)


class _Overlap:
    """Makes domain *first*'s read wait, at its first *command*, until
    domain *then*'s read has run its first *command* -- so *then* is read
    alongside *first*, and finishes first. :attr:`overlapped` says whether
    it did; where the domains are read one after another, *first* gives up
    waiting after *wait* seconds. Each is named as the command's first
    argument names it: a domain's name, or its UUID."""

    def __init__(self, host: Host, first: str, then: str,
                 command: str = "domblklist",
                 wait: float = OVERLAP_WAIT) -> None:
        self.started = threading.Event()
        self.overlapped = False

        def waiting() -> None:
            self.overlapped = self.started.wait(wait)

        host.before[(command, first)] = waiting
        host.before[(command, then)] = self.started.set


class TestMergedInListingOrder:
    """What every domain uses is merged as the domains were listed, however
    the reads ended."""

    def test_a_file_two_domains_use_is_credited_to_the_one_listed_first(self):
        host = _host(("vm-a", ["/ws/shared.iso", "/ws/a.qcow2"]),
                     ("vm-b", ["/ws/shared.iso", "/ws/b.qcow2"]))
        overlap = _Overlap(host, "vm-a", then="vm-b")

        in_use = _scan(host)

        assert overlap.overlapped, "vm-b was not read alongside vm-a"
        assert in_use == {"/ws/shared.iso": "vm-a", "/ws/a.qcow2": "vm-a",
                          "/ws/b.qcow2": "vm-b"}

    def test_identities_are_credited_to_the_one_listed_first_too(
            self, tmp_path):
        shared = tmp_path / "shared.iso"
        shared.write_bytes(b"iso")
        alias = tmp_path / "alias.iso"
        alias.hardlink_to(shared)
        host = _host(("vm-a", [str(shared)]), ("vm-b", [str(alias)]))
        overlap = _Overlap(host, "vm-a", then="vm-b")

        in_use = _scan(host)

        assert overlap.overlapped
        st = shared.stat()
        assert in_use.identities == {(st.st_dev, st.st_ino): "vm-a"}
        assert in_use == {os.path.realpath(shared): "vm-a",
                          os.path.realpath(alias): "vm-b"}

    def test_media_are_credited_to_the_one_listed_first(self):
        host = Host()
        for name in ("vm-a", "vm-b"):
            host.add(f"/ws/{name}.qcow2", readable=True)
            host.define_domain(name, ("file", "disk", "vda",
                                      f"/ws/{name}.qcow2"),
                               ("file", "cdrom", "sda", "/iso/live.iso"))
        host.add("/iso/live.iso", readable=True, format="raw")
        overlap = _Overlap(host, "vm-a", then="vm-b")
        media: dict[str, str] = {}

        in_use = _scan(host, media=media)

        assert overlap.overlapped
        assert media == {"/iso/live.iso": "vm-a"}
        assert in_use["/iso/live.iso"] == "vm-a"

    def test_the_answer_is_the_one_read_one_after_another(self):
        disks = [(f"vm-{i:02d}", [f"/ws/shared-{i % 3}.qcow2",
                                  f"/ws/own-{i}.qcow2"]) for i in range(24)]
        serial = _scan(_host(*disks), workers=1)
        side_by_side = _scan(_host(*disks), workers=8)

        assert side_by_side == serial
        assert side_by_side.identities == serial.identities
        assert side_by_side["/ws/shared-0.qcow2"] == "vm-00"


class TestADomainsDisksSideBySide:
    """The backing chains of one domain's disks are read side by side too,
    and taken in the order they were read before."""

    def test_a_domain_with_many_disks_reads_their_chains_at_once(self):
        disks = [f"/ws/data{i:02d}.qcow2" for i in range(12)]
        host = _host(("vm-a", disks))
        overlap = _Overlap(host, disks[0], then=disks[-1], command="qemu-img")

        in_use = _scan(host)

        assert overlap.overlapped
        assert in_use == {disk: "vm-a" for disk in disks}
        assert in_use == _scan(_host(("vm-a", disks)), workers=1)

    def test_the_first_chain_that_cannot_be_read_still_fails_the_scan(self):
        host = _host(("vm-a", ["/ws/a.qcow2", "/ws/b.qcow2", "/ws/c.qcow2"]))
        host.files["/ws/b.qcow2"].corrupt = True
        overlap = _Overlap(host, "/ws/a.qcow2", then="/ws/c.qcow2",
                           command="qemu-img")

        assert _scan(host) is None
        assert overlap.overlapped

    def test_a_deleted_disk_of_a_shut_off_domain_still_holds_nothing(self):
        host = _host(("vm-a", ["/ws/a.qcow2", "/ws/c.qcow2"]))
        host.define_domain("vm-a", ("file", "disk", "vda", "/ws/a.qcow2"),
                           ("file", "cdrom", "sda", "/ws/b-gone.iso"),
                           ("file", "disk", "vdb", "/ws/c.qcow2"))
        overlap = _Overlap(host, "/ws/a.qcow2", then="/ws/c.qcow2",
                           command="qemu-img")

        in_use = _scan(host)

        assert overlap.overlapped
        assert in_use == {"/ws/a.qcow2": "vm-a", "/ws/c.qcow2": "vm-a"}


class TestStillFailsClosed:
    """One domain whose read fails, or answers None, fails the whole scan,
    whichever reads ended first."""

    def test_a_domain_whose_inventory_cannot_be_read_fails_the_scan(self):
        host = _host(("vm-a", ["/ws/a.qcow2"]), ("vm-b", ["/ws/b.qcow2"]),
                     ("vm-c", ["/ws/c.qcow2"]))
        overlap = _Overlap(host, "vm-a", then="vm-c")
        # vm-b is still listed, and its definition cannot be read
        host.before[("domblklist", "vm-b")] = lambda: host.domains.update(
            {"vm-b": None})

        assert _scan(host) is None
        assert overlap.overlapped

    def test_a_failure_after_the_others_have_answered_still_fails_it(self):
        """The last listed domain fails, read while the first still waits:
        the answers before it were all good, and the scan fails all the
        same."""
        host = _host(("vm-a", ["/ws/a.qcow2"]), ("vm-z", ["/ws/z.qcow2"]))
        host.files["/ws/z.qcow2"].corrupt = True
        overlap = _Overlap(host, "/ws/a.qcow2", then="/ws/z.qcow2",
                           command="qemu-img")

        assert _scan(host) is None
        assert overlap.overlapped

    def test_a_read_that_raises_raises_from_the_scan(self):
        host = _host(("vm-a", ["/ws/a.qcow2"]), ("vm-b", ["/ws/b.qcow2"]))

        def boom() -> None:
            raise RuntimeError("the runner broke")

        host.before[("domblklist", "vm-b")] = boom
        with pytest.raises(RuntimeError, match="the runner broke"):
            _scan(host)

    def test_an_earlier_failure_wins_over_a_later_exception(self):
        """Read one after another, the scan stopped at the first domain that
        failed and never read the one that would have raised; side by side
        it answers the same."""
        host = _host(("vm-a", ["/ws/a.qcow2"]), ("vm-b", ["/ws/b.qcow2"]))
        host.files["/ws/a.qcow2"].corrupt = True

        def boom() -> None:
            raise RuntimeError("never reached one after another")

        host.before[("domblklist", "vm-b")] = boom
        assert _scan(host) is None

    def test_every_snapshot_scan_fails_on_one_unreadable_domain(self):
        host = _host(("vm-a", ["/ws/a.qcow2"]), ("vm-b", ["/ws/b.qcow2"]))
        host.snapshots = {"vm-a": {"s1": _snapshot_xml("s1", "/m/a.raw")},
                          "vm-b": {"s1": _snapshot_xml("s1", "/m/b.raw")}}
        host.fail.add("snapshot-dumpxml")

        assert _scan(host, method="memory_images_recorded") is None
        assert _scan(host, method="snapshot_references") is None


class TestADomainGoneOrRenamedMidScan:
    """A domain undefined while the scan reads it holds nothing; one renamed
    fails the scan -- read alongside the others as before."""

    def test_a_domain_undefined_meanwhile_holds_nothing(self):
        host = _host(("vm-a", ["/ws/a.qcow2"]), ("vm-b", ["/ws/b.qcow2"]),
                     ("vm-c", ["/ws/c.qcow2"]))
        overlap = _Overlap(host, "vm-a", then="vm-c")
        host.before[("domblklist", "vm-b")] = lambda: host.undefine("vm-b")

        in_use = _scan(host)

        assert overlap.overlapped
        assert in_use == {"/ws/a.qcow2": "vm-a", "/ws/c.qcow2": "vm-c"}

    def test_a_domain_renamed_meanwhile_fails_the_scan(self, captured_logs):
        host = _host(("vm-a", ["/ws/a.qcow2"]), ("vm-b", ["/ws/b.qcow2"]),
                     ("vm-c", ["/ws/c.qcow2"]))
        overlap = _Overlap(host, "vm-a", then="vm-c")
        host.before[("domblklist", "vm-b")] = lambda: host.rename(
            "vm-b", "vm-renamed")

        assert _scan(host) is None
        assert overlap.overlapped
        assert "domain vm-b was renamed vm-renamed" in captured_logs.text

    def test_a_snapshot_scan_skips_one_undefined_and_fails_on_one_renamed(
            self):
        host = _host(("vm-a", ["/ws/a.qcow2"]), ("vm-b", ["/ws/b.qcow2"]))
        host.snapshots = {"vm-a": {"s1": _snapshot_xml("s1", "/m/a.raw")},
                          "vm-b": {"s1": _snapshot_xml("s1", "/m/b.raw")}}
        host.before[("snapshot-list", host.uuid("vm-b"))] = (
            lambda: host.undefine("vm-b"))

        recorded = _scan(host, method="memory_images_recorded")
        assert recorded == {"/m/a.raw": "vm-a", "/m/a.raw.zst": "vm-a"}

        host = _host(("vm-a", ["/ws/a.qcow2"]), ("vm-b", ["/ws/b.qcow2"]))
        host.snapshots = {"vm-a": {"s1": _snapshot_xml("s1", "/m/a.raw")},
                          "vm-b": {"s1": _snapshot_xml("s1", "/m/b.raw")}}
        host.before[("snapshot-list", host.uuid("vm-b"))] = (
            lambda: (host.rename("vm-b", "vm-x"), host.fail.add(
                "snapshot-list")))
        assert _scan(host, method="memory_images_recorded") is None


class TestAFailedScanSaysWhy:
    """A scan that fails closed says why once, as a warning, whichever of
    its threads read what failed it: which domain, which file, why (the
    failing command's error), that every file is kept, and how to get past
    it; and it leaves that for the callers that keep files to name (#294,
    finding 3; #219). A source it could not read is probed for absence as
    qemu-img ran: through ``sudo -n`` where qemu-img got sudo."""

    #: another user's 0700 home: only root may search it
    HOME = "/u/other"

    def _host(self, *foreign: str, there: bool = True) -> Host:
        """vm-a, whose disk anyone reads, and each of *foreign*, a shut-off
        domain whose disk is in :attr:`HOME` -- unless not *there*: it was
        deleted, and the domain still names it."""
        host = _host(("vm-a", ["/ws/a.qcow2"]))
        host.hidden.add(self.HOME)
        for name in foreign:
            disk = self._disk(name)
            if there:
                host.add(disk, readable=True)
            host.define_domain(name, ("file", "disk", "vda", disk))
        return host

    def _disk(self, domain: str) -> str:
        return f"{self.HOME}/vms/{domain}.qcow2"

    @staticmethod
    def _scan(host: Host, provider: dict | None = None):
        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=host.run):
            return LibVirtSession(config={"provider": {
                "libvirt": provider or {}}}).disk_paths_in_use()

    @staticmethod
    def _warnings(captured_logs) -> list[str]:
        return [record.getMessage() for record in captured_logs.records
                if record.levelno == logging.WARNING]

    def test_an_unreadable_disk_is_named_with_its_domain_and_why(
            self, captured_logs):
        """No sudo: qemu-img cannot look in the home, and neither can the
        absence probe, so the disk may be there: the scan fails on it."""
        host = self._host("vm-foreign")
        disk = self._disk("vm-foreign")

        assert self._scan(host) is None

        (warning,) = self._warnings(captured_logs)
        assert (f"could not read the backing chain of {disk}, a disk of "
                f"domain vm-foreign (Could not open '{disk}': Permission "
                f"denied)") in warning
        assert "every file this command would remove is kept" in warning
        assert (f"make {disk} readable, fix or undefine domain vm-foreign, "
                f"or set use_sudo: true") in warning
        assert str(last_scan_failure()) in warning

    def test_it_warns_once_however_many_reads_fail(self, captured_logs):
        """Three domains fail, read side by side, the last listed first:
        the first listed is named, once, as one after another named it."""
        host = self._host("vm-f1", "vm-f2", "vm-f3")
        overlap = _Overlap(host, "vm-f1", then="vm-f3")

        assert self._scan(host) is None

        assert overlap.overlapped, "vm-f3 was not read alongside vm-f1"
        assert (self._disk("vm-f3"),) in host.asked("qemu-img")
        (warning,) = self._warnings(captured_logs)
        assert "domain vm-f1" in warning
        assert "vm-f2" not in warning and "vm-f3" not in warning

    def test_with_use_sudo_a_deleted_disk_in_the_home_is_absent(self):
        """qemu-img ran as root and found no file; the probe, as root too,
        searches the home and confirms it, and a shut-off domain holds
        nothing of a disk that is gone (#294)."""
        host = self._host("vm-foreign", there=False)

        in_use = self._scan(host, {"use_sudo": True})

        assert in_use == {"/ws/a.qcow2": "vm-a"}
        assert host.asked("absence-probe") == [(self._disk("vm-foreign"),)]
        assert host.identities("absence-probe") == {"root"}
        assert last_scan_failure() is None

    def test_without_use_sudo_it_cannot_be_told_and_is_named(
            self, captured_logs):
        host = self._host("vm-foreign", there=False)

        assert self._scan(host) is None

        assert host.identities("absence-probe") == {"user"}
        (warning,) = self._warnings(captured_logs)
        assert f"{self._disk('vm-foreign')}, a disk of domain vm-foreign" \
            in warning

    def test_a_probe_sudo_refuses_proves_nothing(self, captured_logs):
        """A sudoers rule that lets qemu-img run as root, and not sh:
        sudo -n refuses the probe rather than prompt, and a probe that
        fails proves nothing -- the scan fails, naming qemu-img's answer,
        and use_sudo, already on, is not offered."""
        host = self._host("vm-foreign", there=False)
        host.sudo_refuses.add("sh")
        disk = self._disk("vm-foreign")

        assert self._scan(host, {"use_sudo": True}) is None

        assert any(command.startswith("sudo -n sh -c ")
                   for command in host.commands)
        (warning,) = self._warnings(captured_logs)
        assert (f"a disk of domain vm-foreign (Could not open '{disk}': No "
                f"such file or directory)") in warning
        assert warning.endswith(f"— make {disk} readable, or fix or undefine "
                                f"domain vm-foreign")

    def test_a_scan_that_passes_leaves_no_failure_on_record(self):
        assert self._scan(self._host("vm-foreign")) is None
        assert last_scan_failure() is not None

        assert self._scan(self._host("vm-foreign"), {"use_sudo": True}) == {
            "/ws/a.qcow2": "vm-a",
            self._disk("vm-foreign"): "vm-foreign"}
        assert last_scan_failure() is None

    def test_a_failed_domain_list_is_named_too(self, captured_logs):
        host = self._host()
        host.fail.add("list")

        assert self._scan(host) is None

        (warning,) = self._warnings(captured_logs)
        assert "could not read the domain list (virsh list: error: list " \
            "failed)" in warning

    def test_a_domain_whose_disks_cannot_be_listed_is_named(
            self, captured_logs):
        host = self._host()
        host.fail.add("domblklist")

        assert self._scan(host) is None

        (warning,) = self._warnings(captured_logs)
        assert ("could not list the disks of domain vm-a (virsh domblklist: "
                "error: domblklist failed)") in warning
        assert warning.endswith("— fix or undefine domain vm-a")

    def test_files_in_use_hands_the_failure_on(self, tmp_path):
        """``files_in_use`` answers None, as before, and the failure is on
        record for the caller that keeps the files to name."""
        host = self._host("vm-foreign")

        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=host.run):
            assert _session().files_in_use([str(tmp_path / "x")]) is None

        assert "domain vm-foreign" in str(last_scan_failure())


class TestSnapshotScansInListingOrder:
    """What the snapshots of every domain record, or name, is merged as the
    domains were listed."""

    def _host(self) -> Host:
        host = _host(("vm-a", ["/ws/a.qcow2"]), ("vm-b", ["/ws/b.qcow2"]))
        # two VMs' snapshots naming one memory image (#253)
        host.snapshots = {
            "vm-a": {"s1": _snapshot_xml("s1", "/m/shared.raw")},
            "vm-b": {"s1": _snapshot_xml("s1", "/m/shared.raw"),
                     "s2": _snapshot_xml("s2", "/m/b.raw")}}
        return host

    def test_a_memory_image_two_domains_record_is_the_first_listed(self):
        host = self._host()
        overlap = _Overlap(host, host.uuid("vm-a"), then=host.uuid("vm-b"),
                           command="snapshot-list")

        recorded = _scan(host, method="memory_images_recorded")

        assert overlap.overlapped
        assert recorded == {"/m/shared.raw": "vm-a",
                            "/m/shared.raw.zst": "vm-a",
                            "/m/b.raw": "vm-b", "/m/b.raw.zst": "vm-b"}

    def test_a_file_two_domains_snapshots_name_is_the_first_listed(self):
        host = self._host()
        overlap = _Overlap(host, host.uuid("vm-a"), then=host.uuid("vm-b"),
                           command="snapshot-list")

        references = _scan(host, method="snapshot_references")

        assert overlap.overlapped
        assert references["/m/shared.raw"] == "snapshot 's1' of vm-a"
        assert references["/m/b.raw"] == "snapshot 's2' of vm-b"
        assert list(references.snapshots) == ["vm-a", "vm-b"]
        assert references.snapshots["vm-b"]["s2"].memory == "/m/b.raw"


class TestPoolSize:
    """``in_use_scan_workers`` sets how many domains are read at once."""

    def test_one_worker_reads_one_domain_after_another(self):
        host = _host(("vm-a", ["/ws/a.qcow2"]), ("vm-b", ["/ws/b.qcow2"]))
        overlap = _Overlap(host, "vm-a", then="vm-b", wait=0.3)

        in_use = _scan(host, workers=1)

        assert not overlap.overlapped
        assert in_use == {"/ws/a.qcow2": "vm-a", "/ws/b.qcow2": "vm-b"}

    def test_one_worker_reads_nothing_past_the_first_failure(self):
        host = _host(("vm-a", ["/ws/a.qcow2"]), ("vm-b", ["/ws/b.qcow2"]))
        host.files["/ws/a.qcow2"].corrupt = True

        assert _scan(host, workers=1) is None
        assert ("vm-b", "--details") not in host.asked("domblklist")

    @pytest.mark.parametrize("value", [0, -1, "8", 2.5, True, None])
    def test_a_value_that_is_not_a_whole_number_is_warned_about(
            self, value, captured_logs):
        assert scan_pool.scan_workers({"in_use_scan_workers": value},
                                      _session().logger) == 8
        assert "in_use_scan_workers" in captured_logs.text

    def test_the_default_reads_eight_at_once(self):
        assert scan_pool.scan_workers({}, _session().logger) == 8
        assert scan_pool.scan_workers({"in_use_scan_workers": 3},
                                      _session().logger) == 3


class TestInOrder:
    """:class:`InOrder` hands outcomes back in order, and no read outlives
    the block that asked for it."""

    def test_outcomes_come_back_in_order_whichever_ends_first(self):
        def read(i: int) -> int:
            time.sleep(0.01 * (5 - i))
            return i * i

        with InOrder(read, range(5), workers=5) as outcomes:
            assert [o.result() for o in outcomes] == [0, 1, 4, 9, 16]

    def test_an_exception_comes_back_at_its_turn(self):
        def read(i: int) -> int:
            if i == 1:
                raise LookupError(i)
            return i

        with InOrder(read, range(3), workers=3) as outcomes:
            assert outcomes[0].result() == 0
            with pytest.raises(LookupError):
                outcomes[1].result()
            assert outcomes[2].result() == 2

    def test_leaving_early_starts_no_more_reads_and_waits_for_the_rest(self):
        started: list[int] = []
        ended: list[int] = []

        def read(i: int) -> int:
            started.append(i)
            time.sleep(0.05)
            ended.append(i)
            return i

        with InOrder(read, range(40), workers=2) as outcomes:
            outcomes[0].result()
        left = len(started)
        time.sleep(0.2)

        assert left < 40
        assert len(started) == left            # nothing started since
        assert sorted(ended) == sorted(started)  # every started read ended

    def test_a_ctrl_c_while_waiting_stops_the_reads_too(self):
        """A SIGINT reaches the main thread, waiting on the first outcome:
        the KeyboardInterrupt is raised there, no read is started after it,
        and the reads under way end before it leaves the block."""
        started: list[int] = []
        release = threading.Event()

        def read(i: int) -> int:
            started.append(i)
            release.wait(5)
            time.sleep(0.05)
            return i

        def interrupt() -> None:
            signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
            time.sleep(0.05)
            release.set()

        timer = threading.Timer(0.1, interrupt)
        timer.start()
        with pytest.raises(KeyboardInterrupt):
            with InOrder(read, range(20), workers=4) as outcomes:
                outcomes[0].result()
        timer.join()

        assert len(started) == 4
        assert not any(thread.name.startswith("boxman-scan")
                       for thread in threading.enumerate())

    def test_one_worker_reads_each_when_asked(self):
        asked: list[int] = []

        def read(i: int) -> int:
            asked.append(i)
            return i

        with InOrder(read, range(3), workers=1) as outcomes:
            assert asked == []
            assert outcomes[1].result() == 1
        assert asked == [1]


class TestSharedStateUnderThreads:
    """What the reads of one scan share is read once, however many threads
    ask for it at the same time."""

    def test_the_pools_are_listed_once_for_every_thread(self):
        listed: list[str] = []
        barrier = threading.Barrier(32)

        class Virsh:
            def execute(self, *args, **kwargs):
                listed.append(args[0])
                time.sleep(0.01)
                if args[0] == "pool-list":
                    return Result(stdout="pool-a\n\n")
                return Result(stdout="<pool><target><path>/ws/a</path>"
                                     "</target></pool>")

        reads = _ChainReads({})
        reads.virsh = Virsh()
        answers: list[str | None] = []

        def ask() -> None:
            barrier.wait()
            answers.append(reads.pool_on("/ws/a"))

        threads = [threading.Thread(target=ask) for _ in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert answers == ["pool-a"] * 32
        assert listed == ["pool-list", "pool-dumpxml"]

    def test_once_each_computes_each_key_once_under_many_threads(self):
        memo = OnceEach()
        computed: dict[int, int] = {}
        lock = threading.Lock()
        barrier = threading.Barrier(64)

        def compute(key: int) -> int:
            with lock:
                computed[key] = computed.get(key, 0) + 1
            time.sleep(0.001)
            return key * 10

        answers: list[tuple[int, int]] = []

        def ask(n: int) -> None:
            barrier.wait()
            for key in range(50):
                answers.append((key, memo.get(key, lambda k=key: compute(k))))

        threads = [threading.Thread(target=ask, args=(n,)) for n in range(64)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert computed == {key: 1 for key in range(50)}
        assert len(answers) == 64 * 50
        assert all(value == key * 10 for key, value in answers)

    def test_once_each_gives_every_thread_the_same_failure(self):
        memo = OnceEach()
        calls: list[int] = []
        barrier = threading.Barrier(16)
        raised: list[BaseException] = []

        def compute() -> None:
            calls.append(1)
            time.sleep(0.01)
            raise OSError("unreadable")

        def ask() -> None:
            barrier.wait()
            try:
                memo.get("/ws/x", compute)
            except OSError as exc:
                raised.append(exc)

        threads = [threading.Thread(target=ask) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert calls == [1]
        assert len(raised) == 16


class TestFilesInUse:
    """``files_in_use`` asks about many files at the cost of one scan: what
    a command that removes several VMs' files at once needs to know."""

    def _host(self, tmp_path) -> tuple[Host, dict[str, str]]:
        used = tmp_path / "used.qcow2"
        used.write_bytes(b"disk")
        files = {"used": str(used),
                 "alias": str(tmp_path / "alias.qcow2"),
                 "link": str(tmp_path / "link.qcow2"),
                 "free": str(tmp_path / "free.qcow2"),
                 "absent": str(tmp_path / "absent.qcow2")}
        os.link(files["used"], files["alias"])
        os.symlink(files["used"], files["link"])
        (tmp_path / "free.qcow2").write_bytes(b"overlay")
        host = _host(("vm-a", [os.path.realpath(files["used"])]),
                     ("vm-b", ["/ws/b.qcow2"]))
        return host, files

    def test_many_files_are_asked_about_in_one_scan(self, tmp_path):
        host, files = self._host(tmp_path)

        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=host.run):
            why = _session().files_in_use(list(files.values()))

        listings = [args for args in host.asked("list") if "--all" in args]
        assert len(listings) == 1
        assert set(why) == {files["used"], files["alias"], files["link"]}
        assert all(reason == "domain vm-a uses it" for reason in why.values())

    def test_a_file_no_domain_uses_or_that_is_not_there_may_go(self, tmp_path):
        host, files = self._host(tmp_path)

        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=host.run):
            why = _session().files_in_use([files["free"], files["absent"]])

        assert why == {}

    def test_a_failed_scan_keeps_everything(self, tmp_path):
        host, files = self._host(tmp_path)
        host.fail.add("domblklist")

        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=host.run):
            assert _session().files_in_use([files["free"]]) is None

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
    def test_a_file_that_cannot_be_inspected_stays(self, tmp_path):
        host, _files = self._host(tmp_path)
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "overlay").write_bytes(b"x")
        locked.chmod(0)
        try:
            with patch("boxman.providers.libvirt.commands._shell_run",
                       side_effect=host.run):
                why = _session().files_in_use([str(locked / "overlay")])
        finally:
            locked.chmod(0o700)

        assert why == {str(locked / "overlay"):
                       "it cannot be inspected (Permission denied)"}
