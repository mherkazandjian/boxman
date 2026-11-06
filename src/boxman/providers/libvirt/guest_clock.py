"""
Set a guest's clock from the host's when it comes back from saved memory
(#275).

A guest restored from memory -- a snapshot's, or a managed save's -- or
resumed after a pause carries on from the instant its vCPUs stopped. KVM
holds its clock still while they are stopped, so it comes back behind the
host by as long as that lasted, and nothing in the guest notices. An NTP
client corrects it in the end, often by slewing it over minutes; a lab with
no route out never does, and until then TLS, Kerberos and every log line
see the old time.

``virsh domtime --now`` sets it through the QEMU guest agent, to the time of
the host virsh runs on (``guest-set-time`` with that time; under the
docker-compose runtime, the container's, which is the host's clock). Not
``--sync``: that asks the guest to set itself from its own RTC, and the
Windows agent does it by running ``w32tm /resync /nowait``, which reports
success at once and, with no time source configured, changes nothing
(measured on Windows Server 2022: 73 s behind before it, and after).

A fast ``snapshot restore`` comes first. Before any ``domtime`` the agent's
channel is read from the live definition, which costs no more than a
``dumpxml``: a guest whose channel reads disconnected has no agent to ask,
and is not waited for. The wait -- ``guest_clock_wait``, 1 s by default --
bounds every look and every ask, and each virsh call is cut off so the
whole cannot outlast it by more than :data:`MARGIN_S`.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any
from xml.etree import ElementTree as ET

import invoke

from boxman import log
from boxman.exceptions import ConfigError

#: The config key: under ``provider.libvirt`` (conf.yml) or
#: ``providers.libvirt`` (boxman.yml), on a cluster, or on a VM.
WAIT_KEY = "guest_clock_wait"

#: Seconds a restore or a resume waits, at most, for the guest agent's
#: channel to read as connected and for the agent to answer.
DEFAULT_WAIT_S = 1.0

#: Between two looks at the channel, or two asks of the agent.
POLL_S = 0.1

#: How long past the wait a virsh call that has started may still run: the
#: read and the set themselves, about 0.1 s each with an agent that answers.
#: No call runs past the wait and this.
MARGIN_S = 1.0

#: The name libvirt gives the guest agent's virtio-serial channel.
AGENT_CHANNEL = "org.qemu.guest_agent.0"


def parse_wait(value: Any, where: str) -> float:
    """
    *value* as a wait in seconds: a finite number, 0 or more (0 looks at
    the channel and asks the agent once, without waiting).

    Raises:
        ConfigError: anything else, naming *where* it was set.
    """
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ConfigError(
            f"{where} must be a number of seconds, 0 or more (0 asks the "
            f"guest agent once, without waiting), not {value!r}")
    return float(value)


def set_guest_clock(virsh: Any, vm_name: str, *,
                    stopped_at: float | None = None,
                    came_back_from: str,
                    wait: float = DEFAULT_WAIT_S,
                    logger: Any = log) -> bool:
    """
    Set the clock of *vm_name*, which is running, from the host's.

    Says so in one line when it worked, and warns once whenever it did not
    -- no agent channel, no agent connected to it, no answer within *wait*,
    a call cut off, a refusal -- with how far behind the clock is left.
    Never raises: a guest clock boxman could not set is worth a warning,
    never a failed restore.

    Args:
        virsh: the ``VirshCommand`` to run ``dumpxml`` and ``domtime`` with.
        vm_name: the domain.
        stopped_at: when the guest's clock stopped, in Unix epoch seconds
            (a snapshot's creation time, a save image's mtime), or None
            when that is not known.
        came_back_from: what the guest came back from, for the warning:
            ``"snapshot 's1'"``, ``"its managed save"``, ``"a pause"``.
        wait: seconds to wait, at most, for the channel and the agent
            (``guest_clock_wait``); 0 looks and asks once.
        logger: where to say it.

    Returns:
        bool: whether the clock was set.
    """
    # how long the clock stood still: taken now, as the guest has just
    # started again, not after a wait for its agent, which the guest spends
    # running
    stood = None if stopped_at is None else time.time() - stopped_at
    try:
        return _Attempt(virsh, vm_name, stood, came_back_from, wait,
                        logger).run()
    except Exception as exc:
        logger.warning(
            f"could not set the clock of {vm_name} from the host's "
            f"({type(exc).__name__}: {exc}). "
            + _how_far_behind(stood, came_back_from, agent_hint=False))
        return False


class _Attempt:
    """One go at setting a guest's clock, within its time budget."""

    def __init__(self, virsh, vm_name, stood, came_back_from, wait, logger):
        self.virsh = virsh
        self.vm_name = vm_name
        self.stood = stood
        self.came_back_from = came_back_from
        self.wait = wait
        self.logger = logger
        start = time.monotonic()
        #: no look or ask starts a wait after this
        self.deadline = start + wait
        #: no call runs after this
        self.cutoff = self.deadline + MARGIN_S

    # --- the budget -------------------------------------------------------

    def can_wait(self) -> bool:
        return time.monotonic() + POLL_S <= self.deadline

    def time_left(self) -> float:
        """What a call may take: what is left before :attr:`cutoff`."""
        return max(self.cutoff - time.monotonic(), 0.0)

    def run_virsh(self, *args: str):
        """``virsh`` *args*, cut off at :attr:`cutoff`; one that was cut
        off, or raised, reads as one that failed."""
        left = self.time_left()
        if left <= 0:
            return _Failed("was not run: the wait was spent", timed_out=True)
        try:
            return self.virsh.execute(*args, warn=True, timeout=left)
        except invoke.exceptions.CommandTimedOut:
            return _Failed(f"was cut off after {left:.1f} s", timed_out=True)
        except Exception as exc:
            # its first line: invoke's errors run to a page, the command
            # and its streams included
            first = next((line.strip() for line in str(exc).splitlines()
                          if line.strip()), "")
            return _Failed(f"{type(exc).__name__}: {first}")

    # --- the attempt ------------------------------------------------------

    def run(self) -> bool:
        # 1. the agent's channel, from the live definition: as cheap as a
        #    dumpxml, and it asks nothing of the guest. Disconnected, or
        #    absent, there is no agent to wait for. Not yet known -- no state
        #    in it, or the definition not readable -- it is looked at again,
        #    within the wait.
        while True:
            state, detail = self.channel_state()
            if state == "connected":
                break
            if state == "absent":
                return self.warn("it has no QEMU guest agent channel")
            if state == "disconnected":
                return self.warn("no QEMU guest agent is connected to its "
                                 "channel")
            if not self.can_wait():
                return self.warn(
                    f"its QEMU guest agent channel did not read as "
                    f"connected within {self.wait:g} s ({detail})",
                    slow=True)
            time.sleep(POLL_S)

        # 2. the guest's time: the read is safe to repeat, and says how far
        #    behind the clock was. libvirt may not have reached an agent
        #    whose channel has just opened, so it is asked again, within
        #    the wait.
        while True:
            asked = self.run_virsh("domtime", self.vm_name)
            if asked.ok:
                break
            if _timed_out(asked):
                return self.cut_off("reading the guest's time", asked)
            why = _last_line(asked.stderr)
            if "not configured" in why.lower():
                # no channel after all: nothing will answer
                return self.warn(why)
            if not self.can_wait():
                if self.wait > 0:
                    why = (f"no QEMU guest agent answered within "
                           f"{self.wait:g} s: {why}")
                return self.warn(why, slow=True)
            time.sleep(POLL_S)

        guest_at = _epoch(asked.stdout)
        behind = None if guest_at is None else time.time() - guest_at
        if behind is not None and abs(behind) < 2:
            # as close as domtime's whole seconds tell, and as setting it
            # gets
            self.logger.info(
                f"the clock of {self.vm_name} already reads the host's time")
            return True

        # 3. set it
        done = self.run_virsh("domtime", self.vm_name, "--now")
        if _timed_out(done):
            return self.cut_off("setting the guest's time", done, behind)
        if not done.ok and not self.reads_right():
            refusal = _last_line(done.stderr)
            if "not currently supported" in refusal:
                # qemu-ga on Linux refuses guest-set-time outright where it
                # finds no /sbin/hwclock (8.2, on Ubuntu 24.04's cloud
                # image), and looks for it once, at the first ask: installed
                # later, it takes a restart of the agent (measured)
                refusal += ("; the Linux guest agent sets the clock only "
                            "where the guest has /sbin/hwclock, which Debian "
                            "and Ubuntu ship in util-linux-extra, and looks "
                            "for it once: restart qemu-guest-agent after "
                            "installing it")
            return self.warn(f"its guest agent answered, but refused "
                             f"({refusal})", behind=behind, agent_hint=False)
        if not done.ok:
            self.logger.debug(
                f"{self.vm_name}: its guest agent set the clock, and then "
                f"reported {_last_line(done.stderr)}")

        self.logger.info(
            f"set the clock of {self.vm_name} from the host's"
            + ("" if behind is None else f" (it was {_offset(behind)})"))
        return True

    def channel_state(self) -> tuple[str, str]:
        """
        ``(state, detail)`` of the guest agent's channel in the live
        definition: ``connected`` or ``disconnected`` as libvirt records
        it, ``absent`` when the domain has none, ``unknown`` when it records
        no state yet or the definition cannot be read (*detail* says which).
        """
        listed = self.run_virsh("dumpxml", self.vm_name)
        if not listed.ok:
            return "unknown", ("its definition could not be read: "
                               f"{_last_line(listed.stderr)}")
        try:
            root = ET.fromstring(listed.stdout)
        except ET.ParseError as exc:
            return "unknown", f"its definition could not be parsed: {exc}"
        for channel in root.iterfind("devices/channel"):
            target = channel.find("target")
            if (target is not None and target.get("type") == "virtio"
                    and target.get("name") == AGENT_CHANNEL):
                state = target.get("state")
                if state in ("connected", "disconnected"):
                    return state, state
                return "unknown", "libvirt records no state for it yet"
        return "absent", "no guest agent channel"

    def reads_right(self) -> bool:
        """
        Whether the guest clock now reads the host's time, give or take the
        whole seconds ``domtime`` deals in.

        Asked when setting it reported a failure. The Linux agent sets the
        system time first and then writes it to the RTC with ``hwclock
        -w``; where only that second step fails, the whole is reported as
        failed with the clock already set. Read back, rather than believed.
        """
        again = self.run_virsh("domtime", self.vm_name)
        guest_at = _epoch(again.stdout) if again.ok else None
        return guest_at is not None and abs(time.time() - guest_at) < 3

    def cut_off(self, doing: str, result, behind: float | None = None):
        """The warning for a ``domtime`` cut off at :attr:`cutoff`."""
        return self.warn(f"virsh domtime, {doing}, {result.stderr}: the "
                         f"guest agent may be hung", slow=True, behind=behind,
                         agent_hint=False)

    def warn(self, why: str, slow: bool = False,
             behind: float | None = None, agent_hint: bool = True) -> bool:
        """
        The one warning: why the clock was not set and how far behind it
        is left. *slow* says that the wait ran out, and with a
        ``guest_clock_wait`` below its default, the warning says so.
        """
        if slow and self.wait < DEFAULT_WAIT_S:
            why += (f"; {WAIT_KEY} is {self.wait:g} s, below its "
                    f"{DEFAULT_WAIT_S:g} s default")
        self.logger.warning(
            f"could not set the clock of {self.vm_name} from the host's "
            f"({why}). "
            + (_how_far_behind(self.stood, self.came_back_from, agent_hint)
               if behind is None else f"It is {_offset(behind)} {_UNTIL}."))
        return False


def _timed_out(result) -> bool:
    """Whether :meth:`_Attempt.run_virsh` cut *result* off."""
    return isinstance(result, _Failed) and result.timed_out


class _Failed:
    """What :meth:`_Attempt.run_virsh` returns for a run that was cut off
    or raised."""

    ok = False
    stdout = ""

    def __init__(self, stderr: str, timed_out: bool = False):
        self.stderr = stderr
        self.timed_out = timed_out


_UNTIL = "until something in the guest corrects it, such as an NTP client"


def _how_far_behind(stood: float | None, came_back_from: str,
                    agent_hint: bool = True) -> str:
    """
    The rest of the warning: how far behind the clock is left, *stood*
    being how long it stood still, if that is known.
    """
    until = _UNTIL + (". A QEMU guest agent running in the guest lets "
                      "boxman set it." if agent_hint else ".")
    if stood is None:
        return (f"It came back from {came_back_from}, so its clock is behind "
                f"the host's by as long as that lasted, {until}")
    age = span(stood)
    return (f"It came back from {came_back_from}, {age} old, so its clock "
            f"is about {age} behind the host's, {until}")


def _last_line(text: str | None) -> str:
    """The last line of virsh's stderr, without its ``error:`` label."""
    lines = [line.strip() for line in (text or "").splitlines()
             if line.strip()]
    if not lines:
        return "no reason given"
    return re.sub(r"^error:\s*", "", lines[-1])


def _epoch(stdout: str | None) -> int | None:
    """The guest's time from ``virsh domtime`` (``Time: 1762362000``)."""
    found = re.search(r"(-?\d+)\s*$", (stdout or "").strip())
    return int(found.group(1)) if found else None


def _offset(seconds: float) -> str:
    """How far the guest clock is from the host's, in words."""
    return f"{span(seconds)} {'behind' if seconds > 0 else 'ahead'}"


def span(seconds: float) -> str:
    """
    *seconds*, unsigned, the way a person says it: 45 s, 12 min, 3 h 5 min,
    4 days 2 h.
    """
    total = int(round(abs(seconds)))
    if total < 120:
        return f"{total} s"
    minutes = total // 60
    if minutes < 120:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} days {hours} h" if hours else f"{days} days"
