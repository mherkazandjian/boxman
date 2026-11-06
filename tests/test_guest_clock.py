"""
A guest that comes back from saved memory has its clock set from the host's
(#275), without making a restore noticeably slower.

A memory snapshot, a managed save and a pause all stop the guest's clock
with its vCPUs, and the guest carries on from that instant: after ``snapshot
restore`` it was behind by the snapshot's age, after ``down`` and ``up`` by
the downtime, after ``control resume`` by the pause. boxman now sets it with
``virsh domtime --now`` through the guest agent. It first reads the agent's
channel from the live definition: a disconnected or absent one is not
waited for. Everything else is bounded by ``guest_clock_wait`` (1 s by
default) and a small margin. It warns once, saying how far behind the clock
is left, whenever it could not set it, and never fails the restore.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import invoke
import pytest

from boxman.exceptions import ConfigError
from boxman.providers.libvirt import guest_clock
from boxman.providers.libvirt.commands import VirshCommand
from boxman.providers.libvirt.guest_clock import (
    DEFAULT_WAIT_S,
    MARGIN_S,
    POLL_S,
    parse_wait,
    set_guest_clock,
    span,
)
from boxman.providers.libvirt.session import LibVirtSession
from boxman.providers.libvirt.snapshot import SnapshotManager

pytestmark = pytest.mark.unit

PROJECT = "p"
VM = f"bprj__{PROJECT}__bprj_c_vm01"

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


class _Clock:
    """``time.monotonic``, ``time.time`` and ``time.sleep`` that move only
    when slept, or when a fake virsh call takes time."""

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

    def elapsed(self):
        return self.now - 1000.0


@pytest.fixture
def clock():
    fake = _Clock()
    with patch.object(guest_clock.time, "monotonic", fake.monotonic), \
         patch.object(guest_clock.time, "time", fake.time), \
         patch.object(guest_clock.time, "sleep", fake.sleep):
        yield fake


class _Guest:
    """
    libvirt, as virsh shows it, with one guest whose clock is *behind* the
    host's by that many seconds.

    Stateful rather than an ordered list of answers: the code under test
    looks at the agent's channel, asks the time, sets it and may ask again,
    and what it reads has to follow what it set.

    *channel* is the agent channel's state in the live definition:
    ``connected``, ``disconnected``, ``absent`` (no channel), or None (no
    state recorded yet); it reads as None for the first *settles_after*
    looks. The agent answers from the *not_up_for* + 1-th ``domtime`` on,
    never when *agent* is False. Every ``domtime`` hangs, until it is cut
    off, when *hangs* is set. Each call takes *call_s* of *clock*.
    *refuses* is the error a set fails with; *sets_anyway* makes it fail
    after setting the clock.
    """

    def __init__(self, behind=62, channel="connected", settles_after=0,
                 agent=True, not_up_for=0, hangs=False, refuses=None,
                 sets_anyway=False, configured=True, dumpxml_ok=True,
                 state="running", snapshot_state="running", taken_at=None,
                 saved_at=None, managed_saved=False, clock=None, call_s=0.0):
        self.behind = behind
        self.channel = channel
        self.settles_after = settles_after
        self.agent = agent
        self.not_up_for = not_up_for
        self.hangs = hangs
        self.refuses = refuses
        self.sets_anyway = sets_anyway
        self.configured = configured
        self.dumpxml_ok = dumpxml_ok
        self.state = state
        self.snapshot_state = snapshot_state
        self.taken_at = taken_at
        self.saved_at = saved_at
        self.managed_saved = managed_saved
        self.clock = clock
        self.call_s = call_s
        self.looks = 0
        self.calls: list[tuple] = []
        self.timeouts: list[tuple] = []
        self.shell: list[str] = []
        self.uri = "qemu:///system"

    def domtimes(self) -> list[tuple]:
        return [c[1:] for c in self.calls if c[0] == "domtime"]

    def execute(self, cmd, *args, **kwargs):
        self.calls.append((cmd, *args))
        if "timeout" in kwargs:
            self.timeouts.append((cmd, kwargs["timeout"]))
        if self.clock is not None:
            self.clock.now += self.call_s
        if cmd == "dumpxml":
            return self._dumpxml()
        if cmd == "domtime":
            return self._domtime(args, kwargs.get("timeout"))
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

    def _dumpxml(self):
        self.looks += 1
        if not self.dumpxml_ok:
            return _result(ok=False, stderr="error: failed to get domain")
        if self.channel == "absent":
            return _result("<domain><devices><disk type='file'/></devices>"
                           "</domain>")
        state = self.channel if self.looks > self.settles_after else None
        attr = f" state='{state}'" if state else ""
        return _result(
            "<domain><devices><channel type='unix'><source mode='bind'/>"
            f"<target type='virtio' name='org.qemu.guest_agent.0'{attr}/>"
            "</channel></devices></domain>")

    def _domtime(self, args, timeout):
        if self.hangs:
            if self.clock is not None:
                self.clock.now += timeout
            raise invoke.exceptions.CommandTimedOut(MagicMock(),
                                                    timeout=timeout)
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
        return _result(f"Time: {int(guest_clock.time.time() - self.behind)}\n")


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

        # the channel first, then a read, to know how far behind it was;
        # then set to the host's time
        assert [c[0] for c in guest.calls] == ["dumpxml", "domtime",
                                               "domtime"]
        assert guest.domtimes() == [(VM,), (VM, "--now")]
        assert guest.behind == 0
        assert clock.slept == []
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

    def test_the_wait_is_one_second_unless_configured(self):
        assert DEFAULT_WAIT_S == 1.0
        assert set_guest_clock.__kwdefaults__["wait"] == DEFAULT_WAIT_S

    def test_a_disconnected_channel_is_neither_waited_for_nor_asked(
            self, clock):
        """libvirt records the channel connected before a revert or a
        managed-save start returns (measured, Windows Server 2022 and
        Ubuntu 24.04): disconnected after it, no agent runs."""
        guest = _Guest(channel="disconnected", call_s=0.05, clock=clock)
        logger = MagicMock()

        assert set_guest_clock(guest, VM, stopped_at=clock.time() - 64,
                               came_back_from="snapshot 's1'",
                               logger=logger) is False

        assert guest.domtimes() == []
        assert clock.slept == []
        assert clock.elapsed() == pytest.approx(0.05)    # the one dumpxml
        warning, = _warnings(logger)
        assert "no QEMU guest agent is connected to its channel" in warning
        assert "snapshot 's1', 64 s old" in warning
        assert "about 64 s behind the host's" in warning
        assert "A QEMU guest agent running in the guest" in warning

    def test_a_domain_without_an_agent_channel_is_not_waited_for(self, clock):
        guest, logger = _Guest(channel="absent"), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="snapshot 's1'",
                               logger=logger) is False

        assert guest.domtimes() == []
        assert clock.slept == []
        assert "it has no QEMU guest agent channel" in _warnings(logger)[0]

    def test_a_channel_still_settling_is_looked_at_again_within_the_wait(
            self, clock):
        guest, logger = _Guest(channel="connected", settles_after=3), \
            MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="snapshot 's1'",
                               logger=logger) is True

        assert guest.looks == 4
        assert clock.slept == [POLL_S] * 3
        assert guest.domtimes()[-1] == (VM, "--now")

    def test_a_channel_that_never_settles_is_looked_at_for_the_wait_at_most(
            self, clock):
        guest, logger = _Guest(channel=None), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="snapshot 's1'",
                               logger=logger) is False

        assert sum(clock.slept) <= DEFAULT_WAIT_S + 1e-9
        assert guest.domtimes() == []
        warning, = _warnings(logger)
        assert "channel did not read as connected within 1 s" in warning
        assert "libvirt records no state for it yet" in warning
        assert "below its" not in warning

    def test_an_unreadable_definition_is_looked_at_again_then_named(
            self, clock):
        guest, logger = _Guest(dumpxml_ok=False), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="a pause",
                               logger=logger) is False

        assert sum(clock.slept) <= DEFAULT_WAIT_S + 1e-9
        assert "its definition could not be read: failed to get domain" \
            in _warnings(logger)[0]

    def test_an_agent_libvirt_has_not_reached_yet_is_asked_again(self, clock):
        guest, logger = _Guest(not_up_for=2), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="snapshot 's1'",
                               logger=logger) is True

        assert clock.slept == [POLL_S] * 2
        assert guest.domtimes()[-1] == (VM, "--now")
        assert not _warnings(logger)

    def test_an_agent_that_never_answers_is_asked_for_the_wait_at_most(
            self, clock):
        guest, logger = _Guest(agent=False), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="snapshot 's1'",
                               logger=logger) is False

        assert sum(clock.slept) <= DEFAULT_WAIT_S + 1e-9
        assert all("--now" not in args for args in guest.domtimes())
        warning, = _warnings(logger)
        assert "no QEMU guest agent answered within 1 s" in warning
        assert "QEMU guest agent is not connected" in warning
        assert "below its" not in warning

    def test_a_wait_below_the_default_is_named_when_it_ran_out(self, clock):
        """So that a developer who set 0.1 s knows why."""
        guest, logger = _Guest(agent=False), MagicMock()

        set_guest_clock(guest, VM, came_back_from="snapshot 's1'", wait=0.1,
                        logger=logger)

        assert sum(clock.slept) <= 0.1 + 1e-9
        warning, = _warnings(logger)
        assert "no QEMU guest agent answered within 0.1 s" in warning
        assert "guest_clock_wait is 0.1 s, below its 1 s default" in warning

    def test_an_agent_libvirt_says_is_not_configured_is_not_waited_for(
            self, clock):
        guest, logger = _Guest(configured=False), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="a pause", wait=0.1,
                               logger=logger) is False

        assert guest.domtimes() == [(VM,)]
        assert clock.slept == []
        warning, = _warnings(logger)
        assert "QEMU guest agent is not configured" in warning
        assert "below its" not in warning

    def test_a_disconnected_channel_does_not_blame_the_wait(self, clock):
        guest, logger = _Guest(channel="disconnected"), MagicMock()

        set_guest_clock(guest, VM, came_back_from="a pause", wait=0.1,
                        logger=logger)

        assert "below its" not in _warnings(logger)[0]

    def test_a_wait_of_zero_looks_and_asks_once(self, clock):
        guest, logger = _Guest(channel="connected", settles_after=1), \
            MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="a pause", wait=0,
                               logger=logger) is False

        assert guest.looks == 1
        assert guest.domtimes() == []
        assert clock.slept == []
        assert "guest_clock_wait is 0 s, below its 1 s default" in \
            _warnings(logger)[0]

    def test_a_wait_of_zero_still_sets_a_clock_whose_agent_answers(
            self, clock):
        guest, logger = _Guest(), MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="a pause", wait=0,
                               logger=logger) is True

        assert guest.domtimes() == [(VM,), (VM, "--now")]
        assert clock.slept == []

    @pytest.mark.parametrize("wait", [0, 0.1, 1, 5])
    def test_a_hung_agent_costs_the_wait_and_the_margin_at_most(
            self, clock, wait):
        guest = _Guest(hangs=True, clock=clock, call_s=0.05)
        logger = MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="snapshot 's1'",
                               wait=wait, logger=logger) is False

        assert clock.elapsed() <= wait + MARGIN_S + 0.1
        # no virsh call was given more than what was left of it
        assert all(timeout <= wait + MARGIN_S + 1e-9
                   for _cmd, timeout in guest.timeouts)
        warning, = _warnings(logger)
        assert "virsh domtime, reading the guest's time, was cut off after" \
            in warning
        assert "the guest agent may be hung" in warning

    def test_a_call_is_not_started_once_the_time_is_spent(self, clock):
        guest = _Guest(clock=clock, call_s=DEFAULT_WAIT_S + MARGIN_S)
        logger = MagicMock()

        assert set_guest_clock(guest, VM, came_back_from="a pause",
                               logger=logger) is False

        assert guest.domtimes() == []
        assert "was not run: the wait was spent" in _warnings(logger)[0]

    def test_the_age_is_how_long_the_clock_stood_not_that_plus_the_wait(
            self, clock):
        """The guest runs while boxman waits for its agent: the lag is fixed
        when it starts again."""
        guest, logger = _Guest(agent=False), MagicMock()

        set_guest_clock(guest, VM, stopped_at=clock.time() - 60,
                        came_back_from="snapshot 's1'", wait=5,
                        logger=logger)

        assert sum(clock.slept) > 4
        assert "snapshot 's1', 60 s old" in _warnings(logger)[0]

    def test_an_age_it_cannot_know_is_not_made_up(self, clock):
        guest, logger = _Guest(channel="disconnected"), MagicMock()

        set_guest_clock(guest, VM, came_back_from="a pause", logger=logger)

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

    def test_it_never_raises(self, clock):
        logger = MagicMock()

        with patch.object(guest_clock, "_epoch",
                          side_effect=ValueError("garbled")):
            assert set_guest_clock(_Guest(), VM, came_back_from="a pause",
                                   logger=logger) is False

        assert "ValueError: garbled" in _warnings(logger)[0]


class TestTheWaitSetting:

    @pytest.mark.parametrize("value", [0, 0.1, 0.5, 1, 2.5, 30])
    def test_a_number_of_seconds_0_or_more_is_taken(self, value):
        assert parse_wait(value, "here") == float(value)

    @pytest.mark.parametrize("value", [
        -1, -0.1, True, False, "1", "1s", None, [1], float("nan"),
        float("inf")])
    def test_anything_else_is_refused_naming_where_it_was_set(self, value):
        with pytest.raises(ConfigError, match=r"^here must be a number of "
                                              r"seconds, 0 or more"):
            parse_wait(value, "here")


@pytest.mark.parametrize("seconds, said", [
    (0, "0 s"), (45.4, "45 s"), (119, "119 s"), (120, "2 min"),
    (7199, "119 min"), (7200, "2 h"), (3 * 3600 + 300, "3 h 5 min"),
    (2 * 86400, "2 days"), (4 * 86400 + 7200, "4 days 2 h"), (-90, "90 s"),
])
def test_a_span_is_said_the_way_a_person_says_it(seconds, said):
    assert span(seconds) == said


class TestUnderTheDockerRuntime:

    def test_its_virsh_calls_run_in_the_runtime_container(self, clock):
        """virsh goes through ``docker exec``, and ``--now`` reads the
        container's clock, which is the host's."""
        virsh = VirshCommand(provider_config={
            "runtime": "docker-compose", "runtime_container": "boxman-c",
            "use_sudo": False})
        ran = []

        def run(command, **kwargs):
            ran.append(command)
            if " dumpxml " in command:
                return _result(
                    "<domain><devices><channel type='unix'><target "
                    "type='virtio' name='org.qemu.guest_agent.0' "
                    "state='connected'/></channel></devices></domain>")
            return _result("Time: 1000\n")

        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=run):
            set_guest_clock(virsh, VM, came_back_from="a pause",
                            logger=MagicMock())

        assert ran[0] == ("docker exec --user root boxman-c bash -c "
                          f"'virsh -c qemu:///system dumpxml {VM}'")
        assert ran[-1] == ("docker exec --user root boxman-c bash -c "
                           f"'virsh -c qemu:///system domtime {VM} --now'")


# ── where boxman calls it ──────────────────────────────────────────────────

def _session(provider: dict | None = None, clusters: dict | None = None
             ) -> LibVirtSession:
    config = {"project": PROJECT, "provider": {"libvirt": provider or {}}}
    if clusters is not None:
        config["clusters"] = clusters
    session = LibVirtSession(config=config)
    session.logger = MagicMock()
    return session


def _reverting(restored=True):
    """``SnapshotManager.snapshot_restore`` answering *restored*: a revert
    that worked calls what follows the CDROMs' put-back, the clock."""
    def revert(_self, _vm, _snapshot, paths_in_use=None, after_media=None):
        if restored and after_media is not None:
            after_media()
        return restored
    return patch.object(SnapshotManager, "snapshot_restore", autospec=True,
                        side_effect=revert)


def _restore(guest, restored=True, snapshot="s1", session=None):
    """``LibVirtSession.snapshot_restore`` against *guest*, the revert
    itself (``SnapshotManager.snapshot_restore``) answering *restored*."""
    session = session or _session()
    with patch("boxman.providers.libvirt.session.VirshCommand",
               return_value=guest), \
         patch("boxman.providers.libvirt.snapshot.VirshCommand",
               return_value=guest), \
         _reverting(restored):
        ok = session.snapshot_restore(VM, snapshot)
    return session, ok


class TestAfterASnapshotRestore:

    def test_a_memory_restore_sets_the_clock(self, clock):
        guest = _Guest(behind=66, taken_at=clock.time() - 66)

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
                       taken_at=clock.time() - 600)

        _session_, ok = _restore(guest)

        assert ok is True
        assert guest.domtimes() == []
        assert guest.looks == 0

    def test_a_restore_of_a_shut_off_vms_snapshot_asks_nothing(self, clock):
        guest = _Guest(state="shut off", snapshot_state="shutoff")

        _session_, ok = _restore(guest)

        assert ok is True
        assert guest.domtimes() == []
        assert guest.looks == 0

    def test_one_that_comes_back_paused_has_its_clock_set_when_resumed(
            self, clock):
        guest = _Guest(state="paused", snapshot_state="paused",
                       taken_at=clock.time() - 60)

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

    def test_with_no_agent_it_warns_once_with_the_snapshots_age_at_once(
            self, clock):
        guest = _Guest(channel="disconnected", taken_at=clock.time() - 64)

        session, ok = _restore(guest)

        assert ok is True
        assert clock.slept == []
        assert guest.domtimes() == []
        warning, = _warnings(session.logger)
        assert "no QEMU guest agent is connected to its channel" in warning
        assert "snapshot 's1', 64 s old" in warning
        assert "about 64 s behind the host's" in warning

    def test_the_configured_wait_is_the_one_used(self, clock):
        guest = _Guest(agent=False, taken_at=clock.time() - 64)
        session = _session(provider={"guest_clock_wait": 0.1})

        _restore(guest, session=session)

        assert sum(clock.slept) <= 0.1 + 1e-9
        assert "guest_clock_wait is 0.1 s, below its 1 s default" in \
            _warnings(session.logger)[0]

    def test_a_snapshot_it_cannot_read_still_sets_a_running_guests_clock(
            self, clock):
        guest = _Guest(snapshot_state="running")
        answer = guest.execute

        def unreadable(cmd, *args, **kwargs):
            if cmd == "snapshot-dumpxml":
                guest.calls.append((cmd, *args))
                return _result(ok=False, stderr="error: no snapshot")
            return answer(cmd, *args, **kwargs)

        guest.execute = unreadable
        _session_, ok = _restore(guest)

        assert ok is True
        assert (VM, "--now") in guest.domtimes()

    def test_trouble_reading_the_snapshot_never_fails_the_restore(
            self, clock):
        session = _session()
        with _reverting(), \
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


class TestWhereTheWaitIsSet:
    """``guest_clock_wait``: a VM's over its cluster's over the provider's
    (conf.yml's over boxman.yml's, merged before the session sees it), over
    the 1 s default."""

    def _clusters(self, cluster=None, vm=None, other=None):
        vms = {"vm01": dict(vm or {}), "vm02": {}}
        return {"c": {**(cluster or {}), "vms": vms},
                "d": {**(other or {}), "vms": {"vm01": {}}}}

    def test_unset_it_is_one_second(self):
        assert _session(clusters=self._clusters()).guest_clock_wait(VM) == 1.0

    def test_the_provider_sets_it_for_every_vm(self):
        session = _session(provider={"guest_clock_wait": 0.1},
                           clusters=self._clusters())
        assert session.guest_clock_wait(VM) == 0.1

    def test_a_cluster_overrides_the_provider_for_its_vms_only(self):
        session = _session(provider={"guest_clock_wait": 0.1},
                           clusters=self._clusters(
                               cluster={"guest_clock_wait": 3},
                               other={"guest_clock_wait": 7}))
        assert session.guest_clock_wait(VM) == 3.0
        assert session.guest_clock_wait(
            f"bprj__{PROJECT}__bprj_c_vm02") == 3.0
        assert session.guest_clock_wait(
            f"bprj__{PROJECT}__bprj_d_vm01") == 7.0

    def test_a_vm_overrides_its_cluster(self):
        session = _session(provider={"guest_clock_wait": 0.1},
                           clusters=self._clusters(
                               cluster={"guest_clock_wait": 3},
                               vm={"guest_clock_wait": 0}))
        assert session.guest_clock_wait(VM) == 0.0
        assert session.guest_clock_wait(
            f"bprj__{PROJECT}__bprj_c_vm02") == 3.0

    def test_a_vm_not_in_the_config_gets_the_providers(self):
        session = _session(provider={"guest_clock_wait": 2},
                           clusters=self._clusters(
                               cluster={"guest_clock_wait": 3}))
        assert session.guest_clock_wait("bprj__other__bprj_c_vm01") == 2.0

    def test_every_bad_one_is_refused_up_front_naming_where(self):
        session = _session(provider={"guest_clock_wait": "1s"},
                           clusters=self._clusters(
                               cluster={"guest_clock_wait": -1},
                               vm={"guest_clock_wait": True}))
        with pytest.raises(ConfigError) as caught:
            session.validate_guest_clock_wait()
        message = str(caught.value)
        assert "clusters.c.vms.vm01.guest_clock_wait must be a number" \
            in message
        assert "clusters.c.guest_clock_wait must be a number" in message
        assert "provider.libvirt (conf.yml) or providers.libvirt " \
               "(boxman.yml)" in message

    def test_good_ones_pass(self):
        session = _session(provider={"guest_clock_wait": 0.1},
                           clusters=self._clusters(
                               cluster={"guest_clock_wait": 0},
                               vm={"guest_clock_wait": 5}))
        session.validate_guest_clock_wait()

    def test_one_met_after_a_revert_all_the_same_is_warned_about_not_raised(
            self):
        session = _session(provider={"guest_clock_wait": "fast"})
        assert session.guest_clock_wait(VM) == DEFAULT_WAIT_S
        assert "must be a number of seconds" in \
            _warnings(session.logger)[0]

    @staticmethod
    def _cli(tmp_path, monkeypatch, wait):
        """``boxman snapshot list`` with *wait* as the provider's
        guest_clock_wait; its handler, a mock."""
        from boxman.scripts import app

        conf = tmp_path / "conf.yml"
        conf.write_text(
            "version: '1.0'\nproject: p\n"
            f"provider:\n  libvirt:\n    guest_clock_wait: {wait}\n"
            "clusters:\n  c:\n    workdir: " + str(tmp_path) + "\n"
            "    vms:\n      vm01: {}\n")
        appconf = tmp_path / "boxman.yml"
        appconf.write_text("providers:\n  libvirt: {}\n")
        monkeypatch.setattr("sys.argv", [
            "boxman", "--boxman-conf", str(appconf), "--conf", str(conf),
            "snapshot", "list"])
        handler = MagicMock()
        monkeypatch.setattr(app.BoxmanManager, "snapshot_list", handler)
        return app, handler

    def test_the_cli_refuses_a_bad_one_before_any_command_runs(
            self, tmp_path, monkeypatch):
        app, handler = self._cli(tmp_path, monkeypatch, "-2")

        with pytest.raises(SystemExit) as caught:
            app.main()

        assert caught.value.code == 2
        handler.assert_not_called()

    def test_the_cli_takes_a_good_one(self, tmp_path, monkeypatch):
        app, handler = self._cli(tmp_path, monkeypatch, "0.1")

        app.main()

        handler.assert_called_once()


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

    def test_a_resume_with_no_agent_warns_at_once(self, clock):
        guest = _Guest(state="paused", channel="disconnected")
        session = _session()

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.resume_vm(VM) is True

        assert guest.domtimes() == []
        assert clock.slept == []
        warning, = _warnings(session.logger)
        assert "came back from a pause" in warning

    def test_up_after_down_sets_the_clock_of_a_guest_restored_from_its_managed_save(
            self, clock):
        guest = _Guest(state="shut off", managed_saved=True, behind=3600,
                       saved_at=clock.time() - 3600)
        session = _session()

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.restore_vm(VM, "/tmp/wd") is True

        assert guest.domtimes()[-1] == (VM, "--now")
        assert f"set the clock of {VM} from the host's (it was 60 min " \
               f"behind)" in _infos(session.logger)

    def test_without_an_agent_the_warning_says_how_long_ago_it_was_saved(
            self, clock):
        guest = _Guest(state="shut off", managed_saved=True,
                       channel="disconnected",
                       saved_at=clock.time() - 2 * 86400)
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
        guest = _Guest(state="shut off", managed_saved=True,
                       channel="disconnected")
        session = _session()

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.restore_vm(VM, "/tmp/wd") is True

        assert "its managed save, so its clock is behind the host's by as " \
               "long as that lasted" in _warnings(session.logger)[0]

    def test_a_managed_save_under_another_uri_is_not_looked_for_where_it_is_not(
            self, clock):
        guest = _Guest(state="shut off", managed_saved=True,
                       channel="disconnected")
        session = _session(provider={"uri": "qemu:///session"})

        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.restore_vm(VM, "/tmp/wd") is True

        assert guest.shell == []

    def test_control_start_of_a_managed_saved_guest_sets_its_clock(
            self, clock):
        guest = _Guest(state="shut off", managed_saved=True, behind=90,
                       saved_at=clock.time() - 90)
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
        assert guest.looks == 0
        assert guest.shell == []

    def test_an_external_save_applied_on_request_sets_the_clock(
            self, clock, tmp_path):
        """``control start --restore`` of a save file an older boxman
        left."""
        save = tmp_path / f"{VM}.save"
        save.write_bytes(b"memory")
        guest = _Guest(state="shut off", managed_saved=False, behind=300)
        session = _session()
        answer = guest.execute

        def restore_too(cmd, *args, **kwargs):
            if cmd == "restore":
                guest.calls.append((cmd, *args))
                guest.state = "running"
                return _result()
            return answer(cmd, *args, **kwargs)

        guest.execute = restore_too
        with patch("boxman.providers.libvirt.session.VirshCommand",
                   return_value=guest):
            assert session.restore_vm(VM, str(tmp_path),
                                      allow_legacy=True) is True

        assert guest.domtimes()[-1] == (VM, "--now")
