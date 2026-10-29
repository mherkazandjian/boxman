r"""
Install-only media: the CD-ROMs an ISO-boot VM needs for its install alone.

An installer often needs a second medium on its first boot -- a driver ISO,
an answer file -- that has no business in the installed guest. The answer
ISO of an unattended Windows install holds the Administrator password in
plain text, and any process in the guest can read it from the drive (G7 in
doc/windows-guests.md). A ``cdroms:`` entry marked ``install_only: true``
reaches virt-install like any other (``IsoBootVM``), is ejected by boxman
once the install is done, and is never compared by ``boxman update``: it is
neither put back after the eject nor detached, as an undeclared drive,
while the install still needs it.

When is the install done? boxman starts the installer and does not wait for
it (no install phase, ``--wait=0``), so it never sees the end. What it can
see is the guest agent of the installed system: the agent lists the guest's
filesystems, and libvirt maps each one to the domain's disk targets
(``virsh guestinfo --filesystem``). The install counts as done when the
guest's root -- ``/``, or a Windows drive root such as ``C:\`` -- lives on
the VM's boot disk. The common installers say nothing like it while they
run: a live installer's root is in memory or on the CD-ROM, WinPE runs no
agent, and an installer that mounts its target disk mounts it somewhere
other than its own root. A guest that runs no agent keeps its install-only
media, and boxman says so on every ``up`` and ``update``.

So the media go on the first ``up`` or ``update`` that finds the installed
system's agent answering. That is a sign the installed system is up, not
that it is done with the media: an install whose last stage runs inside
the installed system, with its agent already up, and still reads them --
a first-boot script, AutoYaST's second stage reading the DVD -- can lose
them. Such a stage has to copy what it needs first, or install the agent
last, as the Windows box's setup script does. That is why
``install_only`` is opt-in, per medium, rather than the default for every
cdrom of an ISO-boot VM: only the box knows when its install is done with
a medium.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import invoke

from boxman.exceptions import ProvisionError

from .cdrom import CDROMManager
from .virsh_parse import parse_domblklist

#: the ``cdroms:`` key that marks an entry install-only
INSTALL_ONLY_KEY = 'install_only'

#: seconds ``virsh guestinfo`` may take. libvirt gives up on an agent that
#: does not answer its guest-sync within seconds; this only bounds an agent
#: that answers that and then hangs.
AGENT_QUERY_TIMEOUT = 60

#: one ``key : value`` line of ``virsh guestinfo``; the value may hold a
#: colon (``C:\``) and the key never does
_GUESTINFO_LINE = re.compile(r'^\s*([\w.-]+)\s*:\s?(.*?)\s*$')

#: a Windows drive root as the agent reports it (``C:\``)
_DRIVE_ROOT = re.compile(r'[A-Za-z]:\\?')


def is_install_only(entry: Any) -> bool:
    """Whether a ``cdroms:`` entry is marked ``install_only: true``.

    Only the literal ``true``: anything else is refused by the config
    validation before it gets here, and a bare-string entry has nowhere to
    carry the key.
    """
    return isinstance(entry, dict) and entry.get(INSTALL_ONLY_KEY) is True


def media_path(source: str) -> str:
    """A medium's path the way the differ compares them."""
    return os.path.abspath(os.path.expanduser(source))


def parse_guestinfo(output: str) -> dict[str, str]:
    """``virsh guestinfo`` output as a ``{key: value}`` mapping."""
    info: dict[str, str] = {}
    for line in (output or '').splitlines():
        match = _GUESTINFO_LINE.match(line)
        if match:
            info[match.group(1)] = match.group(2)
    return info


def _count(info: dict[str, str], key: str) -> int:
    try:
        return max(int(info.get(key, '0')), 0)
    except ValueError:
        return 0


def root_filesystem_disks(info: dict[str, str]) -> set[str]:
    r"""
    The disk targets holding the guest's root filesystem.

    A root is ``/`` or a Windows drive root (``C:\``). A Windows guest
    reports a root per drive letter, a CD-ROM's included, so the answer is
    every disk any of them lives on; what matters to the caller is whether
    the boot disk is among them. A root with no disk behind it -- a live
    system's overlay or tmpfs -- adds none.

    Args:
        info: :func:`parse_guestinfo` of ``virsh guestinfo --filesystem``.
    """
    targets: set[str] = set()
    for fs in range(_count(info, 'fs.count')):
        mountpoint = info.get(f'fs.{fs}.mountpoint', '')
        if mountpoint != '/' and not _DRIVE_ROOT.fullmatch(mountpoint):
            continue
        for disk in range(_count(info, f'fs.{fs}.disk.count')):
            alias = info.get(f'fs.{fs}.disk.{disk}.alias')
            if alias:
                targets.add(alias)
    return targets


@dataclass
class InstallMediaOutcome:
    """What :func:`eject_finished_install_media` found and did for one VM.

    Every list holds ``(target, source)`` pairs, ``failed`` a reason too.
    """

    #: the drives holding an install-only medium when it looked
    held: list[tuple[str, str]] = field(default_factory=list)
    #: the targets holding one in the live definition of an active domain
    live: set[str] = field(default_factory=set)
    #: the targets holding one in the persistent definition
    persistent: set[str] = field(default_factory=set)
    #: why the install does not count as done, when it does not
    waiting: str | None = None
    #: the drives a dry run would eject
    would_eject: list[tuple[str, str]] = field(default_factory=list)
    ejected: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str, str]] = field(default_factory=list)

    def scope(self, target: str) -> tuple[str, ...]:
        """The ``change-media`` flags that eject *target*'s medium from
        each definition holding it, and from no other: ``--live`` fails
        for a drive the guest does not have, and either flag would eject
        whatever else the other definition holds there."""
        return ((('--live',) if target in self.live else ())
                + (('--config',) if target in self.persistent else ()))


def _cause(text: str | None) -> str:
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    # virsh prints "error: <what failed>" first and the cause after it
    return lines[-1] if lines else 'no error output'


def boot_disk_target(virsh, vm_name: str) -> str | None:
    """The target of the disk the VM boots from: its persistent
    definition's first ``disk`` device (``vda`` for every VM boxman
    creates with virt-install), or None when it has none.

    Raises:
        ProvisionError: if the block devices cannot be listed.
    """
    result = virsh.execute(
        'domblklist', vm_name, '--details', '--inactive', warn=True)
    if not result.ok:
        raise ProvisionError(
            f"could not list the block devices of {vm_name}: "
            f"{_cause(result.stderr)}")
    for row in parse_domblklist(result.stdout):
        if row.device == 'disk':
            return row.target
    return None


def installed_system_is_up(virsh, vm_name: str,
                           boot_target: str) -> tuple[bool, str]:
    """
    Whether the guest agent reports the guest's root on *boot_target*.

    Returns:
        ``(True, '')``, or ``(False, why not)`` -- the reason reads after
        "the install is not known to be done:".
    """
    try:
        result = virsh.execute('guestinfo', vm_name, '--filesystem',
                               warn=True, timeout=AGENT_QUERY_TIMEOUT)
    except invoke.exceptions.CommandTimedOut:
        return False, (f"its guest agent did not answer within "
                       f"{AGENT_QUERY_TIMEOUT}s")
    if not result.ok:
        return False, (f"its guest agent does not answer "
                       f"({_cause(result.stderr)})")
    roots = root_filesystem_disks(parse_guestinfo(result.stdout))
    if boot_target in roots:
        return True, ''
    where = (f"on {', '.join(sorted(roots))}" if roots
             else "on no disk of the domain")
    return False, (f"its guest agent reports the guest's root {where}, "
                   f"not on the boot disk {boot_target}")


def eject_finished_install_media(virsh, vm_name: str, sources: Iterable[str],
                                 provider_config: dict[str, Any] | None = None,
                                 dry_run: bool = False) -> InstallMediaOutcome:
    """
    Eject *sources* from *vm_name* once its installed system is up.

    A drive holds an install-only medium when its source, in the live or
    the persistent definition, is one of *sources*. The medium is ejected,
    not the drive detached: the media of a SATA or IDE drive can be changed
    live, the drive itself only in the persistent definition (#222), and an
    empty drive is one ``update`` leaves alone. ``--force`` gets past a
    guest's tray lock, as the seed eject does.

    Only a running domain is asked: an agent answers nothing otherwise, and
    a domain that is shut off may be in the middle of its install.

    Args:
        virsh: the :class:`VirshCommand` to run.
        sources: the media paths the VM's ``install_only`` entries resolve
            to (:func:`media_path` spelling).
        dry_run: find and decide, eject nothing.

    Raises:
        ProvisionError: if the domain's block devices cannot be listed.
    """
    wanted = {media_path(source) for source in sources}
    outcome = InstallMediaOutcome()
    if not wanted:
        return outcome

    cdroms = CDROMManager(vm_name, provider_config=provider_config)
    state_result = virsh.execute('domstate', vm_name, warn=True)
    state = (state_result.stdout or '').strip() if state_result.ok else ''
    active = state in ('running', 'paused', 'pmsuspended')

    def holding(drives: list[dict[str, Any]]) -> dict[str, str]:
        return {c['target']: c['source'] for c in drives
                if c['source'] and media_path(c['source']) in wanted}

    persistent = holding(cdroms.get_attached_cdroms(inactive=True))
    # an inactive domain has one definition, which both queries report
    live = holding(cdroms.get_attached_cdroms()) if active else {}
    outcome.persistent, outcome.live = set(persistent), set(live)
    outcome.held = sorted({**persistent, **live}.items())
    if not outcome.held:
        return outcome

    if state != 'running':
        outcome.waiting = (f"it is {state}" if state
                           else "its state cannot be read")
        return outcome

    boot_target = boot_disk_target(virsh, vm_name)
    if boot_target is None:
        outcome.waiting = "it has no boot disk to look for the installed system on"
        return outcome
    done, why_not = installed_system_is_up(virsh, vm_name, boot_target)
    if not done:
        outcome.waiting = why_not
        return outcome

    if dry_run:
        outcome.would_eject = outcome.held
        return outcome

    for target, source in outcome.held:
        result = virsh.execute('change-media', vm_name, target, '--eject',
                               '--force', *outcome.scope(target), warn=True)
        if result.ok:
            outcome.ejected.append((target, source))
        else:
            outcome.failed.append(
                (target, source, _cause(result.stderr)))
    return outcome
