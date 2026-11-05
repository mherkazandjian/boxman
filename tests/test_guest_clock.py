"""
A guest that comes back from saved memory has its clock set from the host's
(#275).

A memory snapshot, a managed save and a pause all stop the guest's clock
with its vCPUs, and the guest carries on from that instant: after ``snapshot
restore`` it was behind by the snapshot's age, after ``down`` and ``up`` by
the downtime, after ``control resume`` by the pause. boxman now sets it with
``virsh domtime --now`` through the guest agent, waits a little for an agent
that is not up yet after a restore, and warns once, saying how far behind
the clock is left, when no agent answers. It never fails the restore.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import invoke
import pytest

from boxman.providers.libvirt import guest_clock
from boxman.providers.libvirt.commands import VirshCommand
from boxman.providers.libvirt.guest_clock import set_guest_clock, span
from boxman.providers.libvirt.session import LibVirtSession
from boxman.providers.libvirt.snapshot import SnapshotManager

pytestmark = pytest.mark.unit

VM = "bprj__p__bprj_c_vm01"

NOT_CONNECTED = ("error: Guest agent is not responding: QEMU guest agent is "
                 "not connected")
NOT_CONFIGURED = ("error: argument unsupported: QEMU guest agent is not "
                  "configured")
NO_HWCLOCK = ("error: internal error: unable to execute QEMU agent command "
              "'guest-set-time': this feature or command is not currently "
              "supported")


def _result(stdout="", ok=True, stderr=""):
    r = MagicMock(name="invoke.Result")
    r.stdout, r.stderr, r.ok = stdout, stderr, ok
    return r


class _Guest:
    """
    libvirt, as virsh shows it, with one guest whose clock is *behind* the
    host's by that many seconds.

    Stateful rather than an ordered list of answers: the code under test
    asks the time, sets it, and may ask again, and what it reads has to
    follow what it set. The agent answers from the *not_up_for* + 1-th
    ``domtime`` on (it is not connected before that), never when *agent*
    is False, and every one fails as libvirt fails a domain without a
    channel when *configured* is False. *refuses* is the error a set fails
    with; *sets_anyway* makes it fail after setting the clock.
    """

    def __init__(self, behind=62, agent=True, configured=True, not_up_for=0,
                 refuses=None, sets_anyway=False, state="running",
                 snapshot_state="running", taken_at=None, saved_at=None,
                 managed_saved=False):
        self.behind = behind
        self.agent = agent
        self.configured = configured
        self.not_up_for = not_up_for
        self.refuses = refuses
        self.sets_anyway = sets_anyway
        self.state = state
        self.snapshot_state = snapshot_state
        self.taken_at = taken_at
        self.saved_at = saved_at
        self.managed_saved = managed_saved
        self.calls: list[tuple] = []
        self.shell: list[str] = []
        self.uri = "qemu:///system"

    def domtimes(self) -> list[tuple]:
        return [c[1:] for c in self.calls if c[0] == "domtime"]

    def execute(self, cmd, *args, **kwargs):
        self.calls.append((cmd, *args))
        if cmd == "domtime":
            return self._domtime(args)
        if cmd == "domstate":
            return _result(f"{self.state}\n")
        if cmd == "list" and "--with-managed-save" in args:
            return _result(f"{VM}\n" if self.managed_saved else "\n")
        if cmd == "list" and "--state-running" in args:
            return _result(f"{VM}\n" if self.state == "running" else "\n")
        if cmd == "resume":
            self.state = "running"
            return _result()
        if cmd == "start":
            self.state = "running"
            self.managed_saved = False
            return _result()
        if cmd == "snapshot-dumpxml":
            created = ("" if self.taken_at is None else
                       f"<creationTime>{int(self.taken_at)}</creationTime>")
            return _result(f"<domainsnapshot><name>{args[1]}</name>"
                           f"<state>{self.snapshot_state}</state>{created}"
                           f"</domainsnapshot>")
        raise AssertionError(f"unexpected virsh call: {(cmd, *args)}")

    def execute_shell(self, command, **kwargs):
        self.shell.append(command)
        if command.startswith("stat -c %Y ") and self.saved_at is not None:
            return _result(f"{int(self.saved_at)}\n")
        return _result(ok=False, stderr="stat: cannot stat")

    def _domtime(self, args):
        if not self.configured:
            return _result(ok=False, stderr=NOT_CONFIGURED)
        if not self.agent or len(self.domtimes()) <= self.not_up_for:
            return _result(ok=False, stderr=NOT_CONNECTED)
        if "--now" in args:
            if self.refuses:
                if self.sets_anyway:
                    self.behind = 0
                return _result(ok=False, stderr=self.refuses)
            self.behind = 0
            return _result()
        return _result(f"Time: {int(time.time() - self.behind)}\n")


class _Clock:
    """``time.monotonic``, ``time.time`` and ``time.sleep`` that move only
    when slept."""

    def __init__(self):
        self.now = 1000.0
        # on a whole second, as domtime reads the guest's: a fraction would
        # round the lag it measures either way
        self.wall = float(int(time.time()))
        self.slept: list[float] = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.wall + self.now - 1000.0

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    fake = _Clock()
    with patch.object(guest_clock.time, "monotonic", fake.monotonic), \
         patch.object(guest_clock.time, "time", fake.time), \
         patch.object(guest_clock.time, "sleep", fake.sleep):
        yield fake


def _warnings(logger) -> list[str]:
    return [c.args[0] for c in logger.warning.call_args_list]


def _infos(logger) -> list[str]:
    return [c.args[0] for c in logger.info.call_args_list]


class TestSettingTheClock:

    def test_the_clock_is_set_from_the_host_and_that_is_said_in_one_line(
            self, clock):
        guest, logger = _Guest(behind=62), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="snapshot 's1'",
                               logger=logger) is True

        # read, to know how far behind it was; then set to the host's time
        assert guest.domtimes() == [(VM,), (VM, "--now")]
        assert guest.behind == 0
        assert _infos(logger) == [
            f"set the clock of {VM} from the host's (it was 62 s behind)"]
        assert not _warnings(logger)

    def test_it_is_set_to_the_hosts_time_not_resynced_from_the_guests_rtc(
            self, clock):
        """``--sync`` makes the Windows agent run ``w32tm /resync
        /nowait``: it reports success and, with no time source, changes
        nothing (measured: 73 s behind before and after)."""
        guest = _Guest()

        set_guest_clock(guest, VM, came_back_from="a pause",
                        logger=MagicMock())

        assert all("--sync" not in args for args in guest.domtimes())

    def test_an_agent_not_up_yet_after_a_restore_is_asked_again(self, clock):
        guest, logger = _Guest(not_up_for=3), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="snapshot 's1'",
                               logger=logger) is True

        assert guest.domtimes()[-1] == (VM, "--now")
        assert clock.slept == [guest_clock.AGENT_POLL_S] * 3
        assert not _warnings(logger)

    def test_the_wait_for_an_agent_is_bounded(self, clock):
        guest, logger = _Guest(agent=False), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="snapshot 's1'",
                               wait=15, logger=logger) is False

        assert sum(clock.slept) == pytest.approx(15)
        assert all("--now" not in args for args in guest.domtimes())
        warnings = _warnings(logger)
        assert len(warnings) == 1
        assert "no QEMU guest agent answered within 15 s" in warnings[0]
        assert "QEMU guest agent is not connected" in warnings[0]

    def test_a_wait_of_zero_asks_once(self, clock):
        guest, logger = _Guest(agent=False), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="a pause", wait=0,
                               logger=logger) is False

        assert guest.domtimes() == [(VM,)]
        assert clock.slept == []
        assert len(_warnings(logger)) == 1

    def test_a_domain_without_an_agent_channel_is_not_waited_for(self, clock):
        guest, logger = _Guest(configured=False), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="snapshot 's1'",
                               logger=logger) is False

        assert guest.domtimes() == [(VM,)]
        assert clock.slept == []
        assert "QEMU guest agent is not configured" in _warnings(logger)[0]

    def test_with_no_agent_the_warning_says_the_clock_is_behind_by_the_snapshots_age(
            self, clock):
        guest, logger = _Guest(agent=False), MagicMock()

        set_guest_clock(guest, VM, stopped_at=time.time() - 3 * 3600 - 300,
                        came_back_from="snapshot 's1'", logger=logger)

        warning, = _warnings(logger)
        assert f"could not set the clock of {VM}" in warning
        assert "snapshot 's1', 3 h 5 min old" in warning
        assert "about 3 h 5 min behind the host's" in warning
        assert "A QEMU guest agent running in the guest" in warning

    def test_the_age_is_how_long_the_clock_stood_not_that_plus_the_wait(
            self, clock):
        """The guest runs while boxman waits for its agent: the lag is fixed
        when it starts again."""
        guest, logger = _Guest(agent=False), MagicMock()

        set_guest_clock(guest, VM, stopped_at=time.time() - 60,
                        came_back_from="snapshot 's1'", wait=15,
                        logger=logger)

        assert sum(clock.slept) == pytest.approx(15)
        assert "snapshot 's1', 60 s old" in _warnings(logger)[0]

    def test_an_age_it_cannot_know_is_not_made_up(self, clock):
        guest, logger = _Guest(agent=False), MagicMock()

        set_guest_clock(guest, VM, came_back_from="a pause", wait=0,
                        logger=logger)

        warning, = _warnings(logger)
        assert "came back from a pause, so its clock is behind the host's " \
               "by as long as that lasted" in warning

    def test_a_clock_that_already_reads_the_hosts_time_is_left_alone(
            self, clock):
        guest, logger = _Guest(behind=0), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="a pause",
                               logger=logger) is True

        assert guest.domtimes() == [(VM,)]
        assert _infos(logger) == [
            f"the clock of {VM} already reads the host's time"]

    def test_an_agent_that_refuses_is_named_with_how_far_behind_the_clock_is(
            self, clock):
        """qemu-ga 8.2 on Ubuntu 24.04's cloud image, which has no
        hwclock, refuses guest-set-time outright (measured)."""
        guest, logger = _Guest(behind=67, refuses=NO_HWCLOCK), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="snapshot 's1'",
                               logger=logger) is False

        warning, = _warnings(logger)
        assert "its guest agent answered, but refused" in warning
        assert "not currently supported" in warning
        assert "util-linux-extra" in warning
        # it looks for hwclock once: installed later, it needs a restart
        assert "restart qemu-guest-agent after installing it" in warning
        assert "It is 67 s behind until something in the guest corrects it" \
            in warning
        assert "A QEMU guest agent running in the guest" not in warning

    def test_a_refusal_after_the_clock_was_set_is_read_back_not_believed(
            self, clock):
        """The Linux agent sets the system time before ``hwclock -w``, and
        reports the whole as failed if only that second step fails."""
        guest = _Guest(behind=67, refuses="error: hwclock failed",
                       sets_anyway=True)
        logger = MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="snapshot 's1'",
                               logger=logger) is True

        assert guest.domtimes() == [(VM,), (VM, "--now"), (VM,)]
        assert not _warnings(logger)
        assert _infos(logger) == [
            f"set the clock of {VM} from the host's (it was 67 s behind)"]

    def test_a_domtime_that_hangs_reads_as_no_answer(self, clock):
        virsh, logger = MagicMock(), MagicMock()
        virsh.execute.side_effect = invoke.exceptions.CommandTimedOut(
            MagicMock(), timeout=30)

        assert set_guest_clock(virsh, VM, came_back_from="a pause", wait=0,
                               logger=logger) is False

        assert virsh.execute.call_args.kwargs["timeout"] == 30
        assert "(CommandTimedOut: Command did not complete within 30 " \
               "seconds!)" in _warnings(logger)[0]

    def test_it_never_raises(self, clock):
        logger = MagicMock()

        with patch.object(guest_clock, "_epoch",
                          side_effect=ValueError("garbled")):
            assert set_guest_clock(_Guest(), VM, came_back_from="a pause",
                                   logger=logger) is False

        assert "ValueError: garbled" in _warnings(logger)[0]


@pytest.mark.parametrize("seconds, said", [
    (0, "0 s"), (45.4, "45 s"), (119, "119 s"), (120, "2 min"),
    (7199, "119 min"), (7200, "2 h"), (3 * 3600 + 300, "3 h 5 min"),
    (2 * 86400, "2 days"), (4 * 86400 + 7200, "4 days 2 h"), (-90, "90 s"),
])
def test_a_span_is_said_the_way_a_person_says_it(seconds, said):
    assert span(seconds) == said


class TestUnderTheDockerRuntime:

    def test_domtime_runs_in_the_runtime_container(self, clock):
        """virsh goes through ``docker exec``, and ``--now`` reads the
        container's clock, which is the host's."""
        virsh = VirshCommand(provider_config={
            "runtime": "docker-compose", "runtime_container": "boxman-c",
            "use_sudo": False})
        ran = []

        def run(command, **kwargs):
            ran.append(command)
            return _result("Time: 1000\n")

        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=run):
            set_guest_clock(virsh, VM, came_back_from="a pause",
                            logger=MagicMock())

        assert ran[-1] == (
            "docker exec --user root boxman-c bash -c "
            f"'virsh -c qemu:///system domtime {VM} --now'")


# ── where boxman calls it ──────────────────────────────────────────────────

def _session() -> LibVirtSession:
    session = LibVirtSession(config={"provider": {"libvirt": {}}})
    session.logger = MagicMock()
    return session


def _restore(guest, restored=True, snapshot="s1"):
    """``LibVirtSession.snapshot_restore`` against *guest*, the revert
    itself (``SnapshotManager.snapshot_restore``) answering *restored*."""
    session = _session()
    with patch("boxman.providers.libvirt.session.VirshCommand",
               return_value=guest), \
         patch("boxman.providers.libvirt.snapshot.VirshCommand",
               return_value=guest), \
         patch.object(SnapshotManager, "snapshot_restore",
                      return_value=restored):
        ok = session.snapshot_restore(VM, snapshot)
    return session, ok


class TestAfterASnapshotRestore:

    def test_a_memory_restore_sets_the_clock(self, clock):
        guest = _Guest(behind=66, taken_at=time.time() - 66)

        session, ok = _restore(guest)

        assert ok is True
        assert (VM, "--now") in guest.domtimes()
        assert guest.behind == 0
        assert f"set the clock of {VM} from the host's (it was 66 s behind)" \
            in _infos(session.logger)

    def test_a_disk_only_restore_asks_nothing_of_the_agent(self, clock):
        """It leaves the VM shut off; its guest reads the time when it
        boots."""
        guest = _Guest(state="shut off", snapshot_state="disk-snapshot",
                       taken_at=time.time() - 600)

        _session_, ok = _restore(guest)

        assert ok is True
        assert guest.domtimes() == []

    def test_a_restore_of_a_shut_off_vms_snapshot_asks_nothing(self, clock):
        guest = _Guest(state="shut off", snapshot_state="shutoff")

        _session_, ok = _restore(guest)

        assert ok is True
        assert guest.domtimes() == []

    def test_one_that_comes_back_paused_has_its_clock_set_when_resumed(
            self, clock):
        guest = _Guest(state="paused", snapshot_state="paused",
                       taken_at=time.time() - 60)

        session, ok = _restore(guest)

        assert ok is True
        assert guest.domtimes() == []
        assert any("came back paused" in line and "control resume" in line
                   for line in _infos(session.logger))

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.resume_vm(VM) is True
        assert (VM, "--now") in guest.domtimes()

    def test_a_revert_that_failed_sets_no_clock(self, clock):
        guest = _Guest()

        _session_, ok = _restore(guest, restored=False)

        assert ok is False
        assert guest.calls == []

    def test_with_no_agent_it_warns_once_with_the_snapshots_age_and_still_succeeds(
            self, clock):
        guest = _Guest(agent=False, taken_at=time.time() - 64)

        session, ok = _restore(guest)

        assert ok is True
        warning, = _warnings(session.logger)
        assert "no QEMU guest agent answered within 15 s" in warning
        assert "snapshot 's1', 64 s old" in warning
        assert "about 64 s behind the host's" in warning

    def test_a_snapshot_it_cannot_read_still_sets_a_running_guests_clock(
            self, clock):
        guest = _Guest(snapshot_state="running")
        guest.execute_snapshot = guest.execute

        def unreadable(cmd, *args, **kwargs):
            if cmd == "snapshot-dumpxml":
                guest.calls.append((cmd, *args))
                return _result(ok=False, stderr="error: no snapshot")
            return guest.execute_snapshot(cmd, *args, **kwargs)

        guest.execute = unreadable
        session, ok = _restore(guest)

        assert ok is True
        assert (VM, "--now") in guest.domtimes()

    def test_trouble_reading_the_snapshot_never_fails_the_restore(
            self, clock):
        session = _session()
        with patch.object(SnapshotManager, "snapshot_restore",
                          return_value=True), \
             patch.object(SnapshotManager, "state_and_creation_time",
                          side_effect=RuntimeError("virsh vanished")):
            assert session.snapshot_restore(VM, "s1") is True
        assert "virsh vanished" in _warnings(session.logger)[0]


class TestTheSnapshotsStateAndAge:

    def test_both_come_from_the_snapshots_xml(self):
        guest = _Guest(snapshot_state="running", taken_at=1762362000)
        with patch("boxman.providers.libvirt.snapshot.VirshCommand",
                   return_value=guest):
            sm = SnapshotManager({})
            assert sm.state_and_creation_time(VM, "s1") == ("running",
                                                            1762362000)

    def test_an_unreadable_snapshot_tells_neither(self):
        sm = SnapshotManager({})
        with patch.object(sm.virsh, "execute",
                          return_value=_result(ok=False)):
            assert sm.state_and_creation_time(VM, "s1") == (None, None)


class TestTheSiblings:
    """The same stopped clock, by other roads."""

    def test_a_resumed_guest_has_its_clock_set(self, clock):
        guest = _Guest(state="paused", behind=1800)
        session = _session()

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.resume_vm(VM) is True

        assert guest.domtimes() == [(VM,), (VM, "--now")]
        assert f"set the clock of {VM} from the host's (it was 30 min " \
               f"behind)" in _infos(session.logger)

    def test_a_resume_with_no_agent_asks_once_and_warns(self, clock):
        """The agent's channel stays open through a pause: one that does not
        answer at once is not coming."""
        guest = _Guest(state="paused", agent=False)
        session = _session()

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.resume_vm(VM) is True

        assert guest.domtimes() == [(VM,)]
        assert clock.slept == []
        warning, = _warnings(session.logger)
        assert "came back from a pause" in warning

    def test_up_after_down_sets_the_clock_of_a_guest_restored_from_its_managed_save(
            self, clock):
        guest = _Guest(state="shut off", managed_saved=True, behind=3600,
                       saved_at=time.time() - 3600)
        session = _session()

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.restore_vm(VM, "/tmp/wd") is True

        assert guest.domtimes()[-1] == (VM, "--now")
        assert f"set the clock of {VM} from the host's (it was 60 min " \
               f"behind)" in _infos(session.logger)

    def test_without_an_agent_the_warning_says_how_long_ago_it_was_saved(
            self, clock):
        guest = _Guest(state="shut off", managed_saved=True, agent=False,
                       saved_at=time.time() - 2 * 86400)
        session = _session()

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.restore_vm(VM, "/tmp/wd") is True

        # read before the start that consumes the image
        assert guest.shell == [
            f"stat -c %Y /var/lib/libvirt/qemu/save/{VM}.save"]
        warning, = _warnings(session.logger)
        assert "its managed save, 2 days old" in warning
        assert "about 2 days behind the host's" in warning

    def test_a_save_image_it_cannot_stat_leaves_the_age_unsaid(self, clock):
        guest = _Guest(state="shut off", managed_saved=True, agent=False)
        session = _session()

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.restore_vm(VM, "/tmp/wd") is True

        assert "its managed save, so its clock is behind the host's by as " \
               "long as that lasted" in _warnings(session.logger)[0]

    def test_a_managed_save_under_another_uri_is_not_looked_for_where_it_is_not(
            self, clock):
        guest = _Guest(state="shut off", managed_saved=True, agent=False)
        session = LibVirtSession(config={"provider": {"libvirt": {
            "uri": "qemu:///session"}}})
        session.logger = MagicMock()

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.restore_vm(VM, "/tmp/wd") is True

        assert guest.shell == []

    def test_control_start_of_a_managed_saved_guest_sets_its_clock(
            self, clock):
        guest = _Guest(state="shut off", managed_saved=True, behind=90,
                       saved_at=time.time() - 90)
        session = _session()

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.start_vm(VM) is True

        assert guest.domtimes()[-1] == (VM, "--now")

    def test_a_cold_start_asks_nothing_of_the_agent(self, clock):
        """A guest that boots reads the time from its RTC, which QEMU sets
        from the host's."""
        guest = _Guest(state="shut off", managed_saved=False)
        session = _session()

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.start_vm(VM) is True

        assert guest.domtimes() == []
        assert guest.shell == []

    def test_an_external_save_applied_on_request_sets_the_clock(
            self, clock, tmp_path):
        """``control start --restore`` of a save file an older boxman
        left."""
        save = tmp_path / f"{VM}.save"
        save.write_bytes(b"memory")
        guest = _Guest(state="shut off", managed_saved=False, behind=300)
        session = _session()

        def restore_too(cmd, *args, **kwargs):
            if cmd == "restore":
                guest.calls.append((cmd, *args))
                guest.state = "running"
                return _result()
            return _Guest.execute(guest, cmd, *args, **kwargs)

        guest.execute = restore_too
        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.restore_vm(VM, str(tmp_path),
                                      allow_legacy=True) is True

        assert guest.domtimes()[-1] == (VM, "--now")
