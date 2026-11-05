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
"""

from __future__ import annotations

import re
import time
from typing import Any

from boxman import log

#: How long to keep asking an agent that does not answer yet. After a revert
#: or a managed-save restore the guest runs in a new QEMU process, and
#: libvirt reaches its agent only once the guest's end of the channel reads
#: as open again. Measured, it answered at once (Windows Server 2022, Ubuntu
#: 24.04): this is margin, and what a guest with no agent adds to a restore.
AGENT_WAIT_S = 15

#: How long to wait between two asks.
AGENT_POLL_S = 1

#: The longest boxman waits on one ``virsh domtime``, for an agent that
#: answers libvirt's sync and then hangs on the command itself.
_DOMTIME_TIMEOUT_S = 30


def set_guest_clock(virsh: Any, vm_name: str, *,
                    stopped_at: float | None = None,
                    came_back_from: str,
                    wait: float = AGENT_WAIT_S,
                    logger: Any = log) -> bool:
    """
    Set the clock of *vm_name*, which is running, from the host's.

    Says so in one line when it worked, and warns once when it did not --
    no guest agent, or none that answered within *wait* seconds -- with
    how far behind the clock is left. Never raises: a guest clock boxman
    could not set is worth a warning, never a failed restore.

    Args:
        virsh: the ``VirshCommand`` to run ``domtime`` with.
        vm_name: the domain.
        stopped_at: when the guest's clock stopped, in Unix epoch seconds
            (a snapshot's creation time, a save image's mtime), or None
            when that is not known.
        came_back_from: what the guest came back from, for the warning:
            ``"snapshot 's1'"``, ``"its managed save"``, ``"a pause"``.
        wait: how long to keep asking while the agent does not answer;
            0 asks once.
        logger: where to say it.

    Returns:
        bool: whether the clock was set.
    """
    # how long the clock stood still: taken now, as the guest has just
    # started again, not after the wait for its agent, which the guest
    # spends running
    stood = None if stopped_at is None else time.time() - stopped_at
    try:
        return _set_guest_clock(virsh, vm_name, stood, came_back_from,
                                wait, logger)
    except Exception as exc:
        logger.warning(
            f"could not set the clock of {vm_name} from the host's "
            f"({type(exc).__name__}: {exc}). "
            + _how_far_behind(stood, came_back_from, agent_hint=False))
        return False


def _set_guest_clock(virsh, vm_name, stood, came_back_from, wait,
                     logger) -> bool:
    deadline = time.monotonic() + wait
    while True:
        # the read is the probe: it is safe to repeat, and says how far
        # behind the clock was
        asked = _domtime(virsh, vm_name)
        if asked.ok:
            break
        why = _last_line(asked.stderr)
        # no agent channel at all: nothing will answer however long this
        # waits (libvirt's "QEMU guest agent is not configured")
        hopeless = "not configured" in why.lower()
        if hopeless or time.monotonic() >= deadline:
            if not hopeless and wait > 0:
                why = (f"no QEMU guest agent answered within {wait:g} s: "
                       f"{why}")
            logger.warning(
                f"could not set the clock of {vm_name} from the host's "
                f"({why}). " + _how_far_behind(stood, came_back_from))
            return False
        time.sleep(AGENT_POLL_S)

    guest_at = _epoch(asked.stdout)
    behind = None if guest_at is None else time.time() - guest_at
    if behind is not None and abs(behind) < 2:
        # as close as domtime's whole seconds tell, and as setting it gets
        logger.info(f"the clock of {vm_name} already reads the host's time")
        return True

    done = _domtime(virsh, vm_name, "--now")
    if not done.ok and not _reads_right(virsh, vm_name):
        refusal = _last_line(done.stderr)
        if "not currently supported" in refusal:
            # qemu-ga on Linux refuses guest-set-time outright where it
            # finds no /sbin/hwclock (8.2, on Ubuntu 24.04's cloud image),
            # and looks for it once, at the first ask: installed later, it
            # takes a restart of the agent (measured)
            refusal += ("; the Linux guest agent sets the clock only where "
                        "the guest has /sbin/hwclock, which Debian and "
                        "Ubuntu ship in util-linux-extra, and looks for it "
                        "once: restart qemu-guest-agent after installing "
                        "it")
        logger.warning(
            f"could not set the clock of {vm_name} from the host's: its "
            f"guest agent answered, but refused ({refusal}). "
            + (_how_far_behind(stood, came_back_from, agent_hint=False)
               if behind is None else
               f"It is {_offset(behind)} {_UNTIL}."))
        return False
    if not done.ok:
        logger.debug(
            f"{vm_name}: its guest agent set the clock, and then reported "
            f"{_last_line(done.stderr)}")

    logger.info(
        f"set the clock of {vm_name} from the host's"
        + ("" if behind is None else f" (it was {_offset(behind)})"))
    return True


def _reads_right(virsh, vm_name: str) -> bool:
    """
    Whether the guest clock now reads the host's time, give or take the
    whole seconds ``domtime`` deals in.

    Asked when setting it reported a failure. The Linux agent sets the
    system time first and then writes it to the RTC with ``hwclock -w``;
    where only that second step fails, the whole is reported as failed with
    the clock already set. Read back, rather than believed.
    """
    again = _domtime(virsh, vm_name)
    guest_at = _epoch(again.stdout) if again.ok else None
    return guest_at is not None and abs(time.time() - guest_at) < 3


def _domtime(virsh, vm_name: str, *args: str):
    """``virsh domtime``; a run that timed out reads as one that failed."""
    try:
        return virsh.execute("domtime", vm_name, *args, warn=True,
                             timeout=_DOMTIME_TIMEOUT_S)
    except Exception as exc:
        # invoke's CommandTimedOut runs to a page, the command and its
        # streams included; its first line says what happened
        first = next((line.strip() for line in str(exc).splitlines()
                      if line.strip()), "")
        return _Failed(f"{type(exc).__name__}: {first}")


class _Failed:
    """What :func:`_domtime` returns for a run that raised."""

    ok = False
    stdout = ""

    def __init__(self, stderr: str):
        self.stderr = stderr


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
