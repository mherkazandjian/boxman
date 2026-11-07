#!/usr/bin/env python3
"""
Snapshot module for libvirt provider.
This module provides functionality to manage VM snapshots using command-line tools.
"""

import binascii
import os
import re
import shlex
import stat
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from boxman import log
from boxman.exceptions import SnapshotError, SnapshotRecoveryError

from .cdrom import CDROMManager, media_disk_xml
from .commands import VirshCommand
from .pending_overlays import PendingOverlays, identity_of, ledger_scope
from .revert_locks import RevertLock, RevertLockTimeoutError, overlay_key
from .virsh_parse import parse_domblklist

#: one ``<disk>`` element of a snapshot's XML, as libvirt formats it: an
#: empty one, or one up to its own ``</disk>`` -- a disk element never holds
#: another. Its children (``<backingStore>``) are named otherwise.
_DISK_ELEMENT = re.compile(r"<disk\b[^>]*/>|<disk\b[^>]*>.*?</disk>", re.S)


class _Backup(tuple):
    """
    ``(original, backup)``, as :meth:`SnapshotManager._preserve_snapshot_overlays`
    made it. :attr:`linked` is the identity, ``(st_dev, st_ino)``, the two
    names share when the backup is a hard link of the original, and None
    when it is a copy.
    """

    linked: tuple[int, int] | None

    def __new__(cls, original: str, backup: str,
                linked: tuple[int, int] | None = None) -> "_Backup":
        pair = super().__new__(cls, (original, backup))
        pair.linked = linked
        return pair


@dataclass
class IsoOverlayRemoval:
    """
    What a removal of the overlays libvirt made of CDROM images did
    (:meth:`SnapshotManager._remove_iso_overlays`): each one removed, and
    each one kept with why -- every one, when whether a snapshot names them
    or a domain uses them could not be told (*decided* False). *failed*
    names those whose removal was tried and did not work.
    """

    removed: list[str] = field(default_factory=list)
    kept: dict[str, str] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)
    decided: bool = True


class SnapshotManager:
    """
    Class to manage snapshots of VMs in libvirt using virsh commands.
    """

    #: bytes: written into a reserved backup path so the file is not empty.
    #: libvirt refuses to create an external overlay over a file that
    #: already exists *with content*, and silently adopts an empty one —
    #: measured, not assumed — so the reservation has to carry something.
    _RESERVATION_MARKER = b"boxman: reserved for a snapshot overlay backup\n"

    def __init__(self, provider_config: dict[str, Any] | None = None):
        """
        Initialize the snapshot manager.

        Args:
            provider_config: Configuration for the libvirt provider
        """
        #: Dict[str, Any]: Configuration for the libvirt provider
        self.provider_config = provider_config or {}

        #: VirshCommand: Command executor for virsh
        self.virsh = VirshCommand(provider_config=provider_config)

        #: str: URI for libvirt connection
        self.uri = provider_config.get('uri', 'qemu:///system') if provider_config else 'qemu:///system'

        #: bool: Whether to use sudo
        self.use_sudo = provider_config.get('use_sudo', False) if provider_config else False

        #: logging.Logger: Logger instance
        self.logger = log

    def _flatten_cdrom_overlays(self, vm_name: str, files_in_use=None) -> None:
        """
        Before a take: put every CDROM of *vm_name* back on its ISO
        (:meth:`_put_cdroms_back_on_their_isos`), and remove the overlays of
        its ISOs that its restores left for later
        (:mod:`.pending_overlays`): this is where a restore's deferred
        removal runs, one scan of every domain for all of them.

        ``snapshot-create-as --memspec`` saves the domain as it is, a CDROM
        on a qcow2 overlay of its ISO included, and every revert to the
        snapshot then builds its new overlay on that one (#273). A restore
        puts the CDROMs back already; this catches one left on an overlay
        since — by a restore that could not, or by one before that did.

        Args:
            vm_name: the domain about to be snapshotted.
            files_in_use: which of some files a domain uses
                (``LibVirtSession.files_in_use``), asked once, before an
                overlay is removed; without it nothing is done.
        """
        ledger = self._ledger()
        waiting = ledger.chains(vm_name)
        self._name_unreadable_records(ledger)
        self._put_cdroms_back_on_their_isos(
            vm_name, files_in_use=files_in_use,
            pending=[(chain.layers, chain.iso) for chain in waiting])

    def _ledger(self) -> PendingOverlays:
        """The records of overlays left for later removal on this
        connection (:func:`ledger_scope`)."""
        return PendingOverlays(scope=ledger_scope(self.provider_config))

    @staticmethod
    def _asking(files_in_use, paths_in_use):
        """*files_in_use* (``LibVirtSession.files_in_use``), or, where only
        *paths_in_use* (``LibVirtSession.disk_paths_in_use``) is at hand,
        the same answer from it (:meth:`FilesInUse.reasons`); None without
        either."""
        if files_in_use is not None or paths_in_use is None:
            return files_in_use

        def asking(paths):
            in_use = paths_in_use()
            return None if in_use is None else in_use.reasons(paths)
        return asking

    @staticmethod
    def _why_not_iso_overlays(layers: list[tuple[str, str | None]]) -> str | None:
        """
        Why a CDROM's chain *layers* — ``(path, format)``, its own image
        first — is not an ISO under overlays libvirt made of it, if it is
        not.

        A revert builds the overlay of a disk beside the disk's source and
        names it after it, its last suffix dropped: ``<ISO stem>.<epoch>``,
        qcow2 (``virDomainSnapshotDefAssignExternalNames``; the snapshot
        definition's name is the revert's creation time). Over an overlay
        it builds the next one beside that, under the same stem, so a stack
        of them keeps the name. Anything else under a CDROM — a
        ``<stem>.patched`` of the user's beside the ISO, say — is not
        boxman's to take apart.
        """
        iso, fmt = layers[-1]
        if fmt != "raw":
            return f"{iso}, at the bottom of its chain, is not a raw image"
        directory = os.path.dirname(iso)
        named = re.compile(
            re.escape(os.path.splitext(os.path.basename(iso))[0]) + r"\.[0-9]+")
        for path, layer_format in layers[:-1]:
            if (layer_format != "qcow2" or path == iso
                    or os.path.dirname(path) != directory
                    or not named.fullmatch(os.path.basename(path))):
                return f"{path} is not an overlay libvirt made of {iso}"
        return None

    def _domain_is_active(self, vm_name: str) -> bool | None:
        """
        Whether *vm_name* is active — running or paused — asked by name, as
        :meth:`_pause_for_backup` asks; ``None`` when that cannot be told.
        """
        listed = self.virsh.execute("list", "--name", warn=True)
        if not listed.ok:
            return None
        return vm_name in listed.stdout.split('\n')

    def _cdroms_on_overlays(self, vm_name: str, cdroms: CDROMManager,
                            active: bool, quiet: bool = False):
        """
        The CDROMs of *vm_name* that read their ISO through overlays libvirt
        made of it (:meth:`_why_not_iso_overlays`), in its live definition
        when it is *active* and in its persistent one.

        A persistent definition records no chain, or a stale piece of one
        (libvirt 10.10), so its CDROM's chain is taken from the live one
        when that has the same source, and read with ``qemu-img`` otherwise
        (:meth:`CDROMManager.media_layers`): a revert makes its overlays
        ``root`` and ``0600``, which a ``qemu-img`` without ``sudo`` cannot
        read.

        Args:
            vm_name: the domain.
            cdroms: its :class:`CDROMManager`.
            active: whether it is active.
            quiet: say nothing of a CDROM left alone.

        Returns:
            ``(entries, isos)``: an entry per CDROM per definition — its
            ``target``, ``scope`` (``live`` or ``config``), ``disk`` (its
            element), ``layers`` (the overlays, top first) and ``iso`` — and
            every ISO its CDROMs read, on an overlay or not; or ``None``
            when a definition cannot be read. A CDROM on anything else is
            left alone, and named unless *quiet*; so is one whose chain
            cannot be told.
        """
        entries: list[dict] = []
        isos: set[str] = set()
        known: dict[str, list] = {}
        for scope in (("live", "config") if active else ("config",)):
            elements = cdroms.cdrom_elements(inactive=scope == "config")
            if elements is None:
                return None
            for target, disk in elements.items():
                source = disk.find("source")
                path = source.get("file") if source is not None else None
                if not path:
                    continue
                layers = known.get(path) or cdroms.media_layers(disk)
                if not layers:
                    if not quiet:
                        self.logger.warning(
                            f"{vm_name}: what CDROM {target} ({path}) is "
                            f"built on cannot be told, so it is left as it "
                            f"is")
                    continue
                known[path] = layers
                if len(layers) == 1:
                    if layers[0][1] == "raw":
                        isos.add(path)
                    continue
                why = self._why_not_iso_overlays(layers)
                if why:
                    if not quiet:
                        self.logger.warning(
                            f"{vm_name}: CDROM {target} is left on {path}: "
                            f"{why}")
                    continue
                isos.add(layers[-1][0])
                entries.append({"target": target, "scope": scope,
                                "disk": disk, "iso": layers[-1][0],
                                "layers": [p for p, _f in layers[:-1]]})
        return entries, isos

    def _snapshot_xmls(self, vm_name: str) -> dict[str, str] | None:
        """
        ``{snapshot: its XML}`` for every snapshot of *vm_name*, secrets
        included (``--security-info``): what a redefine of one has to give
        back. ``None`` when any cannot be read, or the list cannot be
        trusted (see :meth:`recorded_files`).
        """
        listed = self.virsh.execute("snapshot-list", vm_name, "--name",
                                    warn=True)
        if not listed.ok:
            return None
        names = self._listed_snapshot_names(listed.stdout)
        if any(n == '' for n in names) or len(set(names)) != len(names):
            return None
        xmls = {}
        for name in names:
            result = self.virsh.execute("snapshot-dumpxml", vm_name, name,
                                        "--security-info", warn=True)
            if not result.ok:
                return None
            xmls[name] = result.stdout
        return xmls

    @staticmethod
    def _written_disks(xml_text: str) -> tuple:
        """
        ``(root, disks)`` for *xml_text*, a snapshot's XML: the parsed
        document, and ``(span, element, section)`` for each ``<disk>``
        libvirt wrote in it -- where it is in the text, the element parsed
        alone, and the child of the snapshot it is under (``disks``,
        ``revertDisks``, ``domain``, ``inactiveDomain``). Each ``<disk>``
        written is matched to the parsed element in its place, so that an
        edit (:meth:`_edited`) changes those elements alone, as text, and
        everything else goes back to libvirt as it gave it: an ElementTree
        round trip renames the namespace prefixes of a definition's
        ``<metadata>``.

        Raises:
            ValueError: the XML does not parse, or its disk elements cannot
                be matched up.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise ValueError(f"it does not parse ({exc})") from exc
        section_of = {}
        for section in root:
            if section.tag in ("disks", "revertDisks", "domain",
                               "inactiveDomain"):
                for disk in section.iter("disk"):
                    section_of[id(disk)] = section.tag
        elements = list(root.iter("disk"))
        matches = list(_DISK_ELEMENT.finditer(xml_text))
        if len(matches) != len(elements):
            raise ValueError(f"{len(matches)} disk elements are written in "
                             f"it, and {len(elements)} parsed")
        disks = []
        for match, element in zip(matches, elements, strict=True):
            try:
                written = ET.fromstring(match.group(0))
            except ET.ParseError as exc:
                raise ValueError(f"a disk element of it does not parse "
                                 f"alone ({exc})") from exc
            if written.tag != element.tag or written.attrib != element.attrib:
                raise ValueError("its disk elements are not written in the "
                                 "order they parse")
            disks.append((match.span(), written, section_of.get(id(element))))
        return root, disks

    @staticmethod
    def _edited(xml_text: str, edits: list[tuple]) -> str:
        """*xml_text* with each ``(span, text)`` of *edits* -- spans
        :meth:`_written_disks` gave -- replaced by its text."""
        for (start, end), text in reversed(edits):
            xml_text = xml_text[:start] + text + xml_text[end:]
        ET.fromstring(xml_text)
        return xml_text

    @classmethod
    def _retarget_snapshot_xml(cls, xml_text: str,
                               retired: dict[str, str]) -> str | None:
        """
        *xml_text*, a snapshot's XML, recording no overlay of a CDROM and
        no longer naming the overlays of *retired* — ``{overlay: the ISO
        under it}`` — or ``None`` when there is nothing to change.

        A disk it records an external overlay of — under ``<disks>``, or
        ``<revertDisks>``, where a revert to a snapshot with children
        records them — becomes ``snapshot='no'``, as a take records a CDROM,
        when that is one of *retired* or the disk is a CDROM of its saved
        definitions. libvirt's own delete of a snapshot block-commits each
        overlay it records into the image under it, a CDROM's into the ISO;
        a CDROM overlay holds nothing, so no record of one is needed. A
        CDROM of its saved definitions (``<domain>``, ``<inactiveDomain>``)
        on one of *retired* reads that ISO instead (:func:`media_disk_xml`),
        and no revert to the snapshot builds on the overlay again: neither
        the source nor the format of a disk is part of the ABI a revert
        checks the memory image against.

        Only those elements change (:meth:`_written_disks`).

        Raises:
            ValueError: the XML does not parse, or its disk elements cannot
                be matched up.
        """
        root, disks = cls._written_disks(xml_text)
        cdroms = set()
        for section in root:
            if section.tag in ("domain", "inactiveDomain"):
                cdroms.update(
                    target.get("dev") for disk in section.iter("disk")
                    if disk.get("device") == "cdrom"
                    and (target := disk.find("target")) is not None)
        edits = []
        for span, written, section in disks:
            source = written.find("source")
            path = source.get("file") if source is not None else None
            if (section in ("disks", "revertDisks")
                    and written.get("snapshot") == "external"
                    and (path in retired or written.get("name") in cdroms)):
                text = ET.tostring(ET.Element("disk", {
                    "name": written.get("name", ""), "snapshot": "no"}),
                    encoding="unicode")
            elif (section in ("domain", "inactiveDomain")
                    and written.get("device") == "cdrom" and path in retired):
                text = media_disk_xml(written, retired[path])
            else:
                continue
            edits.append((span, text))
        return cls._edited(xml_text, edits) if edits else None

    @classmethod
    def _snapshot_xml_recording(cls, xml_text: str,
                                heads: dict[str, str]) -> str | None:
        """
        *xml_text*, a snapshot's XML, recording *heads* -- ``{disk target:
        image}`` -- as the overlays of those of its disks it records as
        external, under ``<disks>`` and ``<revertDisks>``; ``None`` when it
        records them already. Only those elements change
        (:meth:`_written_disks`).

        Raises:
            ValueError: the XML does not parse, or its disk elements cannot
                be matched up.
        """
        _root, disks = cls._written_disks(xml_text)
        edits = []
        for span, written, section in disks:
            source = written.find("source")
            head = heads.get(written.get("name"))
            if (section not in ("disks", "revertDisks") or head is None
                    or written.get("snapshot") != "external"
                    or source is None or source.get("file") in (None, head)):
                continue
            source.set("file", head)
            edits.append((span, ET.tostring(written, encoding="unicode")))
        return cls._edited(xml_text, edits) if edits else None

    def _redefine_snapshot(self, vm_name: str,
                           xml_text: str) -> tuple[bool, str]:
        """
        ``virsh snapshot-create --redefine`` of *vm_name* with *xml_text*:
        libvirt replaces the record of the snapshot it names, which stays
        the current one if it was. Never ``--current``: with it libvirt
        clears the current snapshot's ``<revertDisks>``
        (``qemuSnapshotRedefine``), which here is the snapshot itself.

        The file holds what ``--security-info`` gave, so it is created
        ``0600`` and removed afterwards. ``(ok, stderr)``.
        """
        with tempfile.NamedTemporaryFile(mode="w", suffix=".xml",
                                         delete=False) as tmp:
            tmp.write(xml_text)
            path = tmp.name
        try:
            result = self.virsh.execute("snapshot-create", vm_name, path,
                                        "--redefine", warn=True)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return bool(result.ok), (result.stderr or "").strip()

    @classmethod
    def _names_a_cdrom_overlay(cls, xml_text: str) -> bool:
        """
        Whether a snapshot's XML records an external overlay of a CDROM of
        its saved definitions — what libvirt's own delete of it commits
        into the ISO. One that does not parse is taken to.
        """
        try:
            return cls._retarget_snapshot_xml(xml_text, {}) is not None
        except ValueError:
            return True

    def _unrecord_cdrom_overlays(self, vm_name: str, snapshots: dict[str, str],
                                 retired: dict[str, str]) -> set[str]:
        """
        Redefine each of *snapshots* — ``{name: XML}`` of *vm_name* — that
        records an overlay of a CDROM or names one of *retired*, without it
        (:meth:`_retarget_snapshot_xml`); the names of those that could not
        be, and still record one.
        """
        left: set[str] = set()
        for name, xml in snapshots.items():
            try:
                new_xml = self._retarget_snapshot_xml(xml, retired)
            except ValueError as exc:
                self.logger.warning(
                    f"{vm_name}: snapshot '{name}' could not be edited "
                    f"({exc}), so the CDROM overlays it names are kept")
                left.add(name)
                continue
            if new_xml is None:
                continue
            ok, error = self._redefine_snapshot(vm_name, new_xml)
            if ok:
                self.logger.info(
                    f"{vm_name}: snapshot '{name}' names its CDROMs' ISOs "
                    f"again, not overlays of them")
            else:
                self.logger.warning(
                    f"{vm_name}: snapshot '{name}' could not be redefined "
                    f"({error}), so the CDROM overlays it names are kept")
                left.add(name)
        return left

    def _has_managed_save(self, vm_name: str) -> bool | None:
        """
        Whether *vm_name* holds managed saved state (``boxman down`` saves
        it that way), asked by name; ``None`` when that cannot be told.
        """
        listed = self.virsh.execute("list", "--all", "--with-managed-save",
                                    "--name", warn=True)
        if not listed.ok:
            return None
        return vm_name in listed.stdout.split('\n')

    def _pause_for_media(self, vm_name: str) -> bool | None:
        """
        Pause *vm_name* if it is running, for a media change. The tray may
        be opened through QMP apart from the change that then takes the
        medium out (:meth:`_switch_cdrom_media`), and a running guest told
        its tray opened can close it again in between: a Linux one closes
        an open tray when it next opens the drive.

        Returns:
            True if this paused it, False if it was not running; ``None``
            when it runs and could not be paused, or that cannot be told.
        """
        listed = self.virsh.execute("list", "--state-running", "--name",
                                    warn=True)
        if not listed.ok:
            return None
        if vm_name not in listed.stdout.split('\n'):
            return False
        return True if self.virsh.execute("suspend", vm_name,
                                          warn=True).ok else None

    def _resume_after_media(self, vm_name: str) -> None:
        """Undo :meth:`_pause_for_media`, loudly when it cannot be."""
        try:
            result = self.virsh.execute("resume", vm_name, warn=True)
            error = (result.stderr or "").strip() if not result.ok else None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        if error is not None:
            self.logger.error(
                f"{vm_name} is still paused: it could not be resumed after "
                f"its CDROMs were switched ({error}). Resume it with: virsh "
                f"resume {vm_name}")

    def _switch_cdrom_media(self, vm_name: str, cdroms: CDROMManager,
                            target: str, disk: ET.Element, iso: str,
                            scope: list[str],
                            alias: str | None) -> tuple[bool, str, bool]:
        """
        ``virsh update-device`` of the CDROM *disk* (*target*) holding *iso*
        as a raw image (:func:`media_disk_xml`), in *scope*; ``(ok, stderr,
        whether the drive may have been left empty)``.

        Live, by force and with the guest paused (:meth:`_pause_for_media`).
        libvirt opens the tray before it takes a medium out — except on a
        domain a revert started, which it does not know has a tray, and qemu
        then refuses: "Tray of device 'sata0-0-0' is not open"
        (:meth:`CDROMManager.move_tray`). The tray is opened through QMP
        then, by the drive's *alias*, and the change made again.

        libvirt takes the old medium out before it puts the new one in, so
        a change that fails past that leaves the drive empty. What qemu
        holds is asked then (:meth:`CDROMManager.medium_in`): an empty
        drive gets one more try at the ISO, and is reported as empty if
        that fails too, its tray left open for a manual insert; a tray this
        opened over a medium still in is closed again — whatever raised.
        """
        xml = media_disk_xml(disk, iso)
        if "--live" not in scope:
            ok, error = cdroms.update_media(xml, scope)
            return ok, error, False
        paused = self._pause_for_media(vm_name)
        if paused is None:
            return False, "the guest could not be paused for the change", False
        flags = [*scope, "--force"]
        opened = ok = empty = False
        try:
            ok, error = cdroms.update_media(xml, flags)
            if ok or "is not open" not in error or not alias:
                return ok, error, False
            opened = cdroms.move_tray(alias, open_it=True)
            if not opened:
                return (False, f"{error}; nor could its tray be opened "
                               f"through QMP", False)
            ok, error = cdroms.update_media(xml, flags)
            if ok:
                return True, "", False
            held = cdroms.medium_in(alias)
            if held is not None and held[0] is not None:
                return False, error, False
            ok, again = cdroms.update_media(xml, flags)
            if ok:
                return True, "", False
            empty = True
            return (False,
                    f"{error}; the drive may be left EMPTY, libvirt having "
                    f"taken the medium out before it failed, and putting "
                    f"{iso} in again failed too ({again}). Its tray is left "
                    f"open. A power cycle of the VM gives it media again "
                    f"from its persistent definition, and so does another "
                    f"restore", True)
        finally:
            if opened and not ok and not empty:
                try:
                    closed = cdroms.move_tray(alias, open_it=False)
                except Exception:
                    closed = False
                if closed:
                    self.logger.info(f"{vm_name}: closed the tray of CDROM "
                                     f"{target} again")
                else:
                    self.logger.error(
                        f"{vm_name}: the tray of CDROM {target} was opened "
                        f"for the change and could not be closed again")
            if paused:
                self._resume_after_media(vm_name)

    def _put_cdrom_back(self, vm_name: str, cdroms: CDROMManager,
                        target: str, group: list[dict]) -> bool:
        """
        Point the CDROM *target* of *vm_name* at its ISO in every definition
        that has it on overlays — *group*, its entries from
        :meth:`_cdroms_on_overlays` — and say whether that worked.

        Both definitions in one call when they hold the same chain: libvirt
        saves the persistent one only once the live change has worked, so
        neither moves without the other.
        """
        live = next((e for e in group if e["scope"] == "live"), None)
        config = next((e for e in group if e["scope"] == "config"), None)
        if live and config and live["layers"] == config["layers"]:
            steps = [(config, ["--live", "--config"])]
        else:
            steps = [(e, ["--live"] if e is live else ["--config"])
                     for e in (live, config) if e is not None]
        alias_elem = live["disk"].find("alias") if live else None
        alias = alias_elem.get("name") if alias_elem is not None else None
        for entry, scope in steps:
            ok, error, empty = self._switch_cdrom_media(
                vm_name, cdroms, target, entry["disk"], entry["iso"], scope,
                alias)
            where = " and ".join("live" if s == "--live" else "persistent"
                                 for s in scope)
            if empty:
                self.logger.error(
                    f"{vm_name}: CDROM {target} could not be put back on "
                    f"{entry['iso']}: {error}")
                return False
            if not ok:
                self.logger.warning(
                    f"{vm_name}: CDROM {target} is left on "
                    f"{entry['layers'][0]} in its {where} definition: it "
                    f"could not be put back on {entry['iso']} ({error})")
                return False
            self.logger.info(
                f"{vm_name}: CDROM {target} reads {entry['iso']} again, not "
                f"an overlay of it ({where})")
        return True

    @staticmethod
    def _why_keep_overlay(layer: str, iso: str, named: set,
                          held: dict, identities=None) -> str | None:
        """
        Why the overlay *layer* of *iso* may not be removed, if it may not:
        it is not a plain file, it is the ISO itself, it is not the file
        recorded for later removal (*identities*, ``{layer: identity}``, as
        :mod:`.pending_overlays` recorded them), a snapshot names it
        (*named*), or *held* -- ``LibVirtSession.files_in_use``'s answer --
        says why it has to stay: a domain uses it, by path or by identity.
        """
        try:
            info = os.lstat(layer)
            under = os.stat(iso)
        except OSError as exc:
            return f"it cannot be inspected ({exc.strerror})"
        if not stat.S_ISREG(info.st_mode):
            return "it is not a plain file"
        identity = (info.st_dev, info.st_ino)
        if identity == (under.st_dev, under.st_ino):
            return "it is the ISO itself"
        if identities is not None and layer in identities:
            if identities[layer] is None:
                return ("its identity could not be read when it was left for "
                        "later removal, so it cannot be told to be that file")
            if identities[layer] != identity:
                return ("it is not the file left there for later removal: "
                        "another one has taken its name since")
        if layer in named or os.path.realpath(layer) in named:
            return "a snapshot of the VM still names it"
        return held.get(layer)

    def _remove_file(self, path: str) -> bool:
        """
        ``rm -f`` *path*; whether it is gone. ``rm`` takes no ``sudo``
        (:meth:`LibVirtCommandBase._should_use_sudo_for_command`), but an ISO can
        sit in a directory only root may write to — ``/usr/share/virtio-win``
        — where libvirt made its overlay as root. There, under
        ``use_sudo``, it is retried with ``sudo``.
        """
        command = f"rm -f -- {shlex.quote(path)}"
        try:
            self.virsh.execute_shell(command, warn=True)
            if (os.path.lexists(path) and self.use_sudo
                    and not os.access(os.path.dirname(path), os.W_OK)):
                self.virsh.execute_shell(command, warn=True, force_sudo=True)
        except Exception as exc:
            self.logger.warning(f"could not run the removal of {path} "
                                f"({type(exc).__name__}: {exc})")
        return not os.path.lexists(path)

    def _remove_iso_overlays(self, vm_name: str,
                             chains: list[tuple[list[str], str]],
                             files_in_use, identities=None,
                             gone: bool = False) -> IsoOverlayRemoval:
        """
        Remove the overlays of *chains* — ``(overlays top first, the ISO
        under them)`` — that nothing needs any more, each above the one it
        is built on; what was removed and what was kept, and why
        (:class:`IsoOverlayRemoval`).

        Kept, together with every layer under it: one a domain uses
        (*files_in_use*, ``LibVirtSession.files_in_use``, asked now, after
        the CDROMs moved, once for every overlay: one scan of every domain
        however many there are), one a snapshot of *vm_name* still names
        anywhere in its XML, one that is not a plain file, one that is not
        the file *identities* recorded for later removal
        (:meth:`_why_keep_overlay`), or one that could not be removed.
        Everything is kept when either cannot be told. The ISO is never in a
        chain (:meth:`_why_not_iso_overlays`), and is told apart by identity
        besides.

        With *gone*, the domain of *vm_name* is confirmed undefined, and its
        snapshots went with it: none names anything.
        """
        present = list(dict.fromkeys(
            layer for layers, _iso in chains for layer in layers
            if os.path.lexists(layer)))
        outcome = IsoOverlayRemoval()
        if not present:
            return outcome
        left = f"left beside their ISO: {', '.join(present)}"
        snapshots = {} if gone else self._snapshot_xmls(vm_name)
        held = files_in_use(present) if snapshots is not None else None
        named: set = set()
        for xml in (snapshots or {}).values():
            try:
                named.update(s.get("file") for s in ET.fromstring(xml).iter(
                    "source") if s.get("file"))
            except ET.ParseError:
                snapshots = None
        if snapshots is None or held is None:
            self.logger.warning(
                f"{vm_name}: whether a snapshot of it names these overlays, "
                f"or a domain uses them, cannot be told, so they are {left}")
            outcome.decided = False
            outcome.kept = dict.fromkeys(
                present, "whether a snapshot names it, or a domain uses it, "
                         "cannot be told")
            return outcome

        kept = outcome.kept
        for layers, iso in chains:
            for i, layer in enumerate(layers):
                if not os.path.lexists(layer):
                    continue
                why = self._why_keep_overlay(layer, iso, named, held,
                                             identities)
                if why:
                    kept.setdefault(layer, why)
                    for below in layers[i + 1:]:
                        kept.setdefault(below, f"{layer} is built on it")
                    break
        removed: set = set()
        for layers, iso in chains:
            for i, layer in enumerate(layers):
                if layer in kept:
                    break
                if layer in removed or not os.path.lexists(layer):
                    continue
                if not self._remove_file(layer):
                    kept[layer] = "it could not be removed"
                    outcome.failed.append(layer)
                    for below in layers[i + 1:]:
                        kept.setdefault(below, f"{layer} is built on it")
                    break
                removed.add(layer)
                outcome.removed.append(layer)
                self.logger.info(
                    f"{vm_name}: removed {layer}, an overlay libvirt made of "
                    f"{iso} that nothing needs")
        for layer, why in kept.items():
            if os.path.lexists(layer):
                self.logger.warning(f"{vm_name}: kept {layer} beside its ISO: "
                                    f"{why}")
        return outcome

    def _put_cdroms_back_on_their_isos(self, vm_name: str, files_in_use=None,
                                       also=(), pending=(),
                                       defer: bool = False) -> None:
        """
        Point every CDROM of *vm_name* at the ISO it reads again, and remove
        the overlays libvirt made of it beside it (#273) -- now, or, with
        *defer*, later.

        Reverting to an external snapshot, libvirt builds a new overlay on
        every disk of the domain, its CDROMs included, whatever the snapshot
        or the domain says of them: ``qemuSnapshotRevertExternalPrepare``
        aligns a fresh definition with ``force_default_location``, so a
        ``snapshot='no'`` is never read, and only an empty drive is left out
        (libvirt 10.10 and master alike). It names the overlay after the
        drive's source — ``<ISO stem>.<epoch>`` beside the ISO, in a
        directory that may not be boxman's — moves the CDROM onto it in
        both definitions, and records it as the reverted-to snapshot's
        external overlay of that drive: under ``<disks>``, or
        ``<revertDisks>`` for a snapshot with children. A snapshot taken
        while a CDROM is on one saves it so, and a revert to that one
        stacks the next overlay on it.

        Undone in that order, each step for a drive only once the one before
        worked for it:

        1. every overlay this is about to move a CDROM off, and those of
           *also*, is recorded for later removal (:mod:`.pending_overlays`)
           before anything moves: moved off and then not removed, nothing
           would record it any more, nor remove it;
        2. each CDROM on such overlays (:meth:`_cdroms_on_overlays`) reads
           the ISO at the bottom of its chain again, in its live and
           persistent definition when *vm_name* is active, in the persistent
           one alone when it is not (:meth:`_put_cdrom_back`). The record of
           one still on them is dropped again;
        3. no snapshot of *vm_name* names those overlays any more, nor
           records any overlay of a CDROM (:meth:`_retarget_snapshot_xml`),
           whatever step 2 managed. libvirt's own delete of a
           snapshot block-commits each overlay it records into the image
           under it: left recording a CDROM's, the delete opens the ISO
           read-write and commits into it — failing on the write lock while
           another VM runs from that ISO — or, once the CDROM is off it,
           fails "failed to find parent disk source in backing chain".
           Naming a file that is gone, it also fails ``validate_snapshot``,
           and one that saved a CDROM on it fails its revert;
        4. each one nothing needs any more is removed, top first
           (:meth:`_remove_iso_overlays`), and its record dropped. With
           *defer* -- a restore -- this is left to the next command that
           asks: the VM's next ``snapshot take`` or its teardown
           (:meth:`remove_pending_iso_overlays`). Whether nothing uses an
           overlay takes a scan of every domain on the host, which costs
           more than the revert; the overlay holds nothing, and once
           nothing records or uses it, it is harmless where it is.

        *also* holds more chains — ``(overlays top first, the ISO under
        them)`` — the overlays the CDROMs were on before the revert
        (:meth:`_cdrom_overlay_chains`). The revert deleted those the
        current snapshot recorded, and the put-back brought back those the
        backup kept; it left every other record of them: none may name them
        afterwards, gone or not, and those still there go in step 4.
        *pending* holds the chains recorded for later removal already, which
        go in steps 3 and 4 as they were recorded.

        A VM holding managed saved state is left alone: its next start
        restores the definition saved with it, which names whatever its
        CDROMs were on.

        Without *files_in_use* (``LibVirtSession.files_in_use``) nothing is
        done unless *defer*: an overlay moved off that cannot be removed
        must be recorded, and is removed now when it cannot be.

        Housekeeping, after a revert that worked: what it cannot do is
        warned about, never raised.
        """
        if files_in_use is None and not defer:
            self.logger.debug(f"{vm_name}: no domain-usage check was given, "
                              f"so its CDROMs are left as they are")
            return
        cdroms = CDROMManager(vm_name, provider_config=self.provider_config)
        active = self._domain_is_active(vm_name)
        saved = self._has_managed_save(vm_name) if active is False else False
        if active is None or saved is None:
            self.logger.warning(
                f"{vm_name}: whether it runs, or holds saved state, cannot be "
                f"told, so its CDROMs are left as they are")
            return
        if saved:
            self.logger.warning(
                f"{vm_name} holds managed saved state, whose next start "
                f"restores the definition saved with it, so its CDROMs are "
                f"left as they are")
            return
        found = self._cdroms_on_overlays(vm_name, cdroms, active)
        if found is None:
            self.logger.warning(
                f"{vm_name}: its CDROMs could not be read, so one left on an "
                f"overlay of its ISO stays there")
            return
        entries, _isos = found
        taken = {layer for entry in entries for layer in entry["layers"]}

        def untaken(given):
            kept = [([layer for layer in layers if layer not in taken], iso)
                    for layers, iso in given]
            return [(layers, iso) for layers, iso in kept if layers]
        chains = untaken(also)
        waiting = untaken(pending)
        snapshots = self._snapshot_xmls(vm_name)
        if snapshots is None:
            overlays = sorted(taken | {layer for layers, _iso in chains
                                       + waiting for layer in layers})
            self.logger.warning(
                f"{vm_name}: its snapshots could not be read, so its CDROMs "
                f"are left as they are"
                + (f", on {', '.join(overlays)}" if overlays else ""))
            return

        ledger = self._ledger()
        recorded = self._record_pending(
            vm_name, ledger,
            chains + [(entry["layers"], entry["iso"]) for entry in entries],
            replacing=[layers[0] for layers, _iso in also if layers])
        groups: dict[str, list[dict]] = {}
        for entry in entries:
            groups.setdefault(entry["target"], []).append(entry)
        for target, group in groups.items():
            if self._put_cdrom_back(vm_name, cdroms, target, group):
                chains.extend((entry["layers"], entry["iso"])
                              for entry in group)
            elif recorded:
                # still on them: what needs them is the VM itself, and the
                # next take or restore finds them there
                self._forget_pending(vm_name, ledger, [
                    entry["layers"][0] for entry in group])

        chains += waiting
        retired = {layer: iso for layers, iso in chains for layer in layers}
        self._unrecord_cdrom_overlays(vm_name, snapshots, retired)
        if not chains:
            return
        if defer and recorded:
            self._name_pending(vm_name, ledger, chains)
            return
        if files_in_use is None:
            self.logger.warning(
                f"{vm_name}: the overlays its CDROMs were moved off could not "
                f"be recorded for later removal, and whether anything uses "
                f"them cannot be asked here, so they are left beside their "
                f"ISO: "
                + ", ".join(dict.fromkeys(layer for layers, _iso in chains
                                          for layer in layers
                                          if os.path.lexists(layer))))
            return
        self._remove_iso_overlays(vm_name, chains, files_in_use,
                                  identities=self._pending_identities(
                                      vm_name, ledger))
        self._settle_pending(vm_name, ledger)

    def remove_pending_iso_overlays(self, vm_name: str, files_in_use,
                                    gone: bool = False,
                                    only=None) -> IsoOverlayRemoval:
        """
        Remove the overlays of its CDROM images that restores of *vm_name*
        left for later (:mod:`.pending_overlays`), behind the checks a
        removal right after the revert asked (:meth:`_remove_iso_overlays`):
        each is still the file recorded, no snapshot of *vm_name* names it,
        and no domain uses it -- *files_in_use* (``LibVirtSession.
        files_in_use``), asked once for all of them.
        Then drop the record of each that is gone, or is no longer the file
        recorded; one kept stays recorded, and is named at every removal.
        Returns what was removed and what was kept.

        With *only*, a set of overlays, a chain whose top is not one of them
        is left whole: ``snapshot prune`` removes what its prompt listed,
        and a chain a restore recorded since, or topped with another
        overlay, is not that.

        A teardown of the VM calls this, with *gone* once its domain is
        confirmed undefined, and so does ``snapshot prune``. A take removes
        them on its way (:meth:`_flatten_cdrom_overlays`). Housekeeping:
        what it cannot do is warned about, never raised.
        """
        ledger = self._ledger()
        waiting = ledger.chains(vm_name)
        self._name_unreadable_records(ledger)
        if only is not None:
            waiting = [chain for chain in waiting
                       if next((layer for layer in chain.layers
                                if os.path.lexists(layer)), None) in only]
        if not waiting:
            return IsoOverlayRemoval()
        outcome = self._remove_iso_overlays(
            vm_name, [(chain.layers, chain.iso) for chain in waiting],
            files_in_use,
            identities=self._pending_identities(vm_name, ledger), gone=gone)
        self._settle_pending(vm_name, ledger)
        return outcome

    def _record_pending(self, vm_name: str, ledger: PendingOverlays,
                        chains: list[tuple[list[str], str]],
                        replacing=()) -> bool:
        """
        Record *chains* of *vm_name* for later removal in *ledger*, in place
        of the records topped by *replacing*; whether that worked. The new
        records are written before the ones they replace go: a restore
        killed in between leaves both, never neither. What could not be
        recorded is said, and is removed right away where a usage check is
        at hand.
        """
        try:
            written = ledger.record(vm_name, chains)
            ledger.forget(vm_name, [top for top in replacing
                                    if top not in written])
        except OSError as exc:
            self.logger.warning(
                f"{vm_name}: the overlays of its CDROM images could not be "
                f"recorded for later removal in {ledger.directory} ({exc})")
            return False
        return True

    def _forget_pending(self, vm_name: str, ledger: PendingOverlays,
                        tops) -> None:
        """Drop the records of *vm_name* topped by *tops*, saying so when
        that cannot be done."""
        try:
            ledger.forget(vm_name, tops)
        except OSError as exc:
            self.logger.warning(
                f"{vm_name}: the record of {', '.join(tops)} for later "
                f"removal could not be dropped ({exc}); the removal keeps it "
                f"while it is used")

    def _settle_pending(self, vm_name: str, ledger: PendingOverlays) -> None:
        """:meth:`PendingOverlays.settle`, saying so when it cannot be
        done."""
        try:
            ledger.settle(vm_name)
        except OSError as exc:
            self.logger.warning(
                f"{vm_name}: the records of the overlays left for later "
                f"removal could not be brought up to date ({exc}); the next "
                f"removal does it")

    @staticmethod
    def _pending_identities(vm_name: str, ledger: PendingOverlays) -> dict:
        """``{layer: identity}`` for every overlay recorded for later
        removal of *vm_name*, as recorded."""
        return {layer: chain.identity(layer)
                for chain in ledger.chains(vm_name) for layer in chain.layers}

    def _name_unreadable_records(self, ledger: PendingOverlays) -> None:
        """Name each record *ledger* could not read: never removed, it
        is left for a person to check."""
        for path, why in sorted(ledger.unreadable.items()):
            self.logger.warning(
                f"{path}, a record of CDROM overlays left for later removal, "
                f"cannot be read ({why}): check the overlays it names by "
                f"hand, then remove it")

    def _name_pending(self, vm_name: str, ledger: PendingOverlays,
                      chains: list[tuple[list[str], str]]) -> None:
        """Say which overlays of *chains* wait beside their ISO for later
        removal, and, at the default verbosity, how many of *vm_name*'s do
        once more than these do."""
        now = list(dict.fromkeys(layer for layers, _iso in chains
                                 for layer in layers
                                 if os.path.lexists(layer)))
        if now:
            self.logger.info(
                f"{vm_name}: {', '.join(now)} left beside its ISO for later "
                f"removal: `boxman snapshot prune`, the next `boxman snapshot "
                f"take` of the VM, or its destroy, removes it once nothing "
                f"uses it")
        waiting = [layer for chain in ledger.chains(vm_name)
                   for layer in chain.layers]
        if len(waiting) > len(now):
            self.logger.status(
                f"{vm_name}: {len(waiting)} overlays libvirt made of its "
                f"CDROM images wait beside them for removal: `boxman "
                f"snapshot prune`, its next `boxman snapshot take`, or its "
                f"destroy, removes them once nothing uses them")

    def _cdrom_overlay_chains(self,
                              vm_name: str) -> list[tuple[list[str], str]]:
        """
        The overlays the CDROMs of *vm_name* read their ISOs through now,
        as chains -- ``(overlays top first, the ISO)`` -- read from its own
        definitions alone, no other domain's
        (:meth:`_cdroms_on_overlays`); empty when there are none, or that
        cannot be told.
        """
        cdroms = CDROMManager(vm_name, provider_config=self.provider_config)
        active = self._domain_is_active(vm_name)
        found = (None if active is None else self._cdroms_on_overlays(
            vm_name, cdroms, active, quiet=True))
        if not found:
            return []
        chains: dict[tuple, str] = {}
        for entry in found[0]:
            chains.setdefault(tuple(entry["layers"]), entry["iso"])
        return [(list(layers), iso) for layers, iso in chains.items()]

    def _unbackable_cdrom_overlays(self, vm_name: str,
                                   chains: list[tuple[list[str], str]],
                                   paths_in_use) -> list[str]:
        """
        Of the overlays in *chains*, those the backup is to leave to the
        revert: the ones in a directory boxman may not write to -- where no
        backup can be reserved, as ``/usr/share/virtio-win`` -- that
        :meth:`_cdrom_overlays_a_revert_discards` finds no revert opens and
        no other domain uses. That check scans every domain, so it is asked
        only for those. Every other overlay at risk, a CDROM's included, is
        backed up: a hard link, which writes no data (#273).
        """
        unwritable = {layer for layers, _iso in chains for layer in layers
                      if not os.access(os.path.dirname(layer),
                                       os.W_OK | os.X_OK)}
        if not unwritable:
            return []
        try:
            discarded = self._cdrom_overlays_a_revert_discards(vm_name,
                                                               paths_in_use)
        except Exception as exc:
            self.logger.debug(f"{vm_name}: every overlay at risk is backed "
                              f"up ({type(exc).__name__}: {exc})")
            return []
        return [layer for layers, _iso in discarded for layer in layers
                if layer in unwritable]

    def _cdrom_overlays_a_revert_discards(
            self, vm_name: str,
            paths_in_use=None) -> list[tuple[list[str], str]]:
        """
        The overlays *vm_name*'s CDROMs are on that the overlay backup need
        not keep, before a revert, as chains — ``(overlays top first, the
        ISO under them)``: no snapshot of it saves a CDROM on one, so no
        revert opens it, and no other domain uses one.

        One a snapshot records as its overlay of a CDROM is at risk like any
        other (:meth:`_preserve_snapshot_overlays`). It holds nothing — a
        guest cannot write to a CDROM — but it is backed up all the same,
        by a hard link, unless its directory is one boxman may not write to
        (:meth:`_unbackable_cdrom_overlays`): only there is this asked, as
        it scans every domain. The revert deletes it when the current
        snapshot records it — and leaves every other record of it — and
        :meth:`_put_cdroms_back_on_their_isos` takes them over afterwards:
        it rewrites the records, gone or not, and records what is still
        there for removal.

        Empty when any of that cannot be told: then everything at risk is
        backed up, as before.
        """
        if paths_in_use is None:
            return []
        cdroms = CDROMManager(vm_name, provider_config=self.provider_config)
        active = self._domain_is_active(vm_name)
        found = (None if active is None else self._cdroms_on_overlays(
            vm_name, cdroms, active, quiet=True))
        if not found or not found[0]:
            return []
        snapshots = self._snapshot_xmls(vm_name)
        if snapshots is None:
            return []
        opened: set = set()
        for xml in snapshots.values():
            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                return []
            for section in root:
                if section.tag in ("domain", "inactiveDomain"):
                    opened.update(s.get("file") for s in section.iter("source")
                                  if s.get("file"))
        in_use = paths_in_use()
        if in_use is None:
            return []
        def unneeded(layer: str) -> bool:
            if layer in opened or os.path.realpath(layer) in opened:
                return False
            try:
                info = os.stat(layer)
            except OSError:
                return False
            users = {in_use.get(layer), in_use.get(os.path.realpath(layer)),
                     getattr(in_use, "identities", {}).get(
                         (info.st_dev, info.st_ino))} - {None, vm_name}
            return not users

        chains: dict[tuple, str] = {}
        for entry in found[0]:
            layers = tuple(layer for layer in entry["layers"]
                           if unneeded(layer))
            if layers:
                chains[layers] = entry["iso"]
        return [(list(layers), iso) for layers, iso in chains.items()]

    def _cdrom_diskspec_args(self, vm_name: str) -> list[str]:
        """
        Return --diskspec args that exclude cdrom devices from a snapshot.

        Even after _flatten_cdrom_overlays switches the cdrom to the raw ISO,
        snapshot-create-as would still create a new qcow2 overlay for it,
        reintroducing the problem for the next revert.  Passing
        --diskspec <target>,snapshot=no prevents that overlay from being
        created so the cdrom stays at the raw ISO permanently.

        Each flag is returned as two separate tokens (``--diskspec`` and the
        spec) — a single ``"--diskspec <spec>"`` token only worked because
        command building used to join parts without quoting; with central
        quoting it would reach virsh as one literal argument.
        """
        result = self.virsh.execute("domblklist", vm_name, "--details", warn=True)
        if not result.ok:
            return []
        args = []
        for row in parse_domblklist(result.stdout):
            if row.device == 'cdrom':
                args.extend(["--diskspec", f"{row.target},snapshot=no"])
        return args

    @staticmethod
    def _overlay_name(source: str, snapshot_name: str) -> str:
        """
        The name libvirt gives the external overlay of a disk whose current
        source is *source*, when no ``file=`` is given: the source's last
        suffix dropped, then ``.<snapshot_name>`` — ``…_data.qcow2`` and
        ``…_data.1785187648`` both give ``…_data.<snapshot_name>``.
        """
        stem = os.path.splitext(os.path.basename(source))[0]
        return f"{stem}.{snapshot_name}"

    def _snapshot_excluded_targets(self, vm_name: str) -> set[str] | None:
        """
        The targets of *vm_name*'s disks a snapshot takes no external
        overlay of unless a diskspec says otherwise — those set a mode of
        their own other than ``external`` (``no``, ``manual``,
        ``internal``), and read-only ones that set none — which
        ``domblklist`` does not show; ``None`` when its definition cannot
        be read. Naming one in a diskspec would change its mode, not just
        where its overlay goes.
        """
        result = self.virsh.execute("dumpxml", vm_name, warn=True)
        if not result.ok:
            return None
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError:
            return None
        excluded = set()
        for disk in root.findall("./devices/disk"):
            target = disk.find("target")
            dev = target.get("dev") if target is not None else None
            mode = disk.get("snapshot")
            if dev and (mode not in (None, "external")
                        or (mode is None
                            and disk.find("readonly") is not None)):
                excluded.add(dev)
        return excluded

    def _overlay_diskspec_args(self, vm_name: str, snapshot_name: str,
                               disk_dir: str | None) -> list[str] | None:
        """
        ``--diskspec <target>,snapshot=external,file=<overlay>`` for each
        disk of *vm_name* a snapshot takes: its overlay in *disk_dir*, under
        the name libvirt would give it beside the disk
        (:meth:`_overlay_name`). ``[]`` when *disk_dir* is ``None``: libvirt
        then creates each beside its disk.

        A disk a snapshot leaves out unless told otherwise
        (:meth:`_snapshot_excluded_targets`) is not named: a diskspec would
        force it in.

        A ``,`` in the path is written ``,,``: virsh splits a diskspec at
        commas.

        Returns ``None`` — the reason logged — when the overlays cannot be
        placed there: the disks cannot be listed (taken anyway, the overlays
        would land beside the disks), the definition cannot be read, or two
        disks would share an overlay.
        """
        if disk_dir is None:
            return []
        where = f"cannot place the overlays of {vm_name} in {disk_dir}"
        result = self.virsh.execute("domblklist", vm_name, "--details",
                                    warn=True)
        if not result.ok:
            self.logger.error(
                f"{where}: its disks could not be listed "
                f"({(result.stderr or '').strip()})")
            return None
        excluded = self._snapshot_excluded_targets(vm_name)
        if excluded is None:
            self.logger.error(
                f"{where}: its definition could not be read, to tell which "
                f"disks a snapshot leaves out")
            return None
        args: list[str] = []
        taken: dict[str, str] = {}
        for row in parse_domblklist(result.stdout):
            if (row.device != 'disk' or not row.source or row.source == '-'
                    or row.target in excluded):
                continue
            overlay = os.path.join(
                disk_dir, self._overlay_name(row.source, snapshot_name))
            if overlay in taken:
                self.logger.error(
                    f"{where}: disks {taken[overlay]} and {row.target} would "
                    f"both get {overlay}")
                return None
            taken[overlay] = row.target
            spec = (f"{row.target},snapshot=external,"
                    f"file={overlay.replace(',', ',,')}")
            args.extend(["--diskspec", spec])
        return args

    def _take_state(self, vm_name: str) -> str | None:
        """
        What a snapshot of *vm_name* taken now can hold:

        - ``'active'``: it is running, or paused, and its memory is saved
          with its disks. A paused one stays paused, and a revert to the
          snapshot brings it back paused (measured on libvirt 10.10).
        - ``'inactive'``: it is shut off, and only its disks can be taken:
          libvirt refuses a memory spec for it ("memory state cannot be
          saved with offline or disk-only snapshot").
        - ``'managedsave'``: it is shut off, but holds managed saved state
          (``boxman down`` saves it that way); see :meth:`create_snapshot`.
        - ``None``: that could not be told.

        Asked by name, as :meth:`_pause_for_backup` asks: ``domstate``
        prints a translated state name, and domain names are not
        translated. A domain that does not exist reads as active, and the
        take fails in libvirt as it always has.
        """
        inactive = self.virsh.execute("list", "--inactive", "--name",
                                      warn=True)
        if not inactive.ok:
            return None
        # exact names, one per line: libvirt refuses a newline in a domain
        # name (see _pause_for_backup)
        if vm_name not in inactive.stdout.split('\n'):
            return 'active'
        saved = self.virsh.execute("list", "--inactive", "--with-managed-save",
                                   "--name", warn=True)
        if not saved.ok:
            return None
        if vm_name in saved.stdout.split('\n'):
            return 'managedsave'
        return 'inactive'

    def create_snapshot(self,
                        vm_name: str,
                        vm_dir: str,
                        snapshot_name: str,
                        description: str,
                        compress_memory: bool = False,
                        compress_level: int = 3,
                        force: bool = False,
                        paths_in_use=None,
                        recorded_in_use=None,
                        disk_dir: str | None = None,
                        files_in_use=None) -> bool:
        """
        Create a snapshot of a VM.

        What it holds follows the VM's state (:meth:`_take_state`). A
        running or paused VM's snapshot saves its memory with its disks. A
        shut-off VM's is disk-only: there is no memory to save, and
        restoring it leaves the VM shut off. A VM holding managed saved
        state is refused: libvirt takes a disk-only snapshot of it all the
        same, but the next start restores the saved memory against the
        disks as they were before the snapshot, so the guest writes into
        the snapshot's backing files (measured on libvirt 10.10). So is one
        whose state cannot be told.

        The memory image is written only where no file is: one already
        there — a leftover, or another VM's image under the same name (VM
        ``v``'s snapshot ``r_snapshot_s1`` and ``s1`` of VM ``v_snapshot_r``
        name one file) — refuses the take, after *force* has cleared what
        it may.

        Args:
            vm_name: Name of the VM
            vm_dir: Directory the memory image is written to, as
                ``<vm>_snapshot_<name>.raw`` (the cluster's
                ``memory_snapshot_dir``, by default its workdir)
            disk_dir: Directory the disk overlays are created in (the
                cluster's ``disk_snapshot_dir``); ``None``, beside their
                disks (:meth:`_overlay_diskspec_args`)
            snapshot_name: Name for the snapshot
            description: Description for the snapshot
            compress_memory: If True, zstd-compress the .raw memory file
                after successful snapshot creation. The libvirt snapshot
                metadata still references the original .raw path; the
                ``snapshot_restore`` path detects the .raw.zst sibling and
                decompresses transparently. A disk-only snapshot has no
                memory file, which is noted, not failed.
            compress_level: zstd compression level (default 3 — sweet spot
                of ~71% reduction at sub-second/GB on commodity CPUs).
            force: If True, first clear anything that would make
                *snapshot_name* collide — an existing libvirt snapshot with
                that name, or orphaned per-disk overlay / memory files a
                prior deletion left behind — so the name can be re-used.
                See :meth:`_force_clear_for_retake`.
            paths_in_use: What every domain uses
                (``LibVirtSession.disk_paths_in_use``), asked before a
                leftover is removed with *force*; without it none is.
            files_in_use: Which of some files a domain uses
                (``LibVirtSession.files_in_use``), asked before a CDROM
                overlay is removed (:meth:`_flatten_cdrom_overlays`); from
                *paths_in_use* when not given, and without either none is.
            recorded_in_use: With *force*, the memory images domains'
                snapshots record (``LibVirtSession.snapshot_files_in_use``),
                asked likewise.

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # before --force removes anything: a take refused here must
            # leave the snapshot it would have replaced in place
            state = self._take_state(vm_name)
            if state is None:
                self.logger.error(
                    f"cannot tell whether {vm_name} is running, so not "
                    f"whether snapshot '{snapshot_name}' can save its "
                    f"memory; refusing to take it")
                return False
            if state == 'managedsave':
                self.logger.error(
                    f"{vm_name} holds managed saved state (`boxman down` "
                    f"saves it that way); refusing to take snapshot "
                    f"'{snapshot_name}' of it: the snapshot would hold its "
                    f"disks but not that memory, and the next start would "
                    f"restore the memory against the disks as they were "
                    f"before the snapshot. Run `boxman up` first, then take "
                    f"it; or discard the saved memory with `virsh "
                    f"managedsave-remove {vm_name}` and take a disk-only "
                    f"snapshot of the shut-off VM")
                return False
            with_memory = state == 'active'
            # Every CDROM back on its ISO first (#273): the saved memory and
            # definition name whatever the CDROMs are on, and a revert to
            # the snapshot would stack its next overlay on one -- and before
            # --force deletes the snapshot it replaces, whose record of a
            # CDROM overlay libvirt's delete would commit into the ISO.
            # Housekeeping: it never stops the take.
            try:
                self._flatten_cdrom_overlays(
                    vm_name, files_in_use=self._asking(files_in_use,
                                                       paths_in_use))
            except Exception as exc:
                self.logger.warning(
                    f"{vm_name}: its CDROMs could not be put back on their "
                    f"ISOs before the take ({type(exc).__name__}: {exc})")
            if force and not self._force_clear_for_retake(
                    vm_name, vm_dir, snapshot_name,
                    paths_in_use=paths_in_use, recorded_in_use=recorded_in_use,
                    disk_dir=disk_dir):
                return False
            snap_fname = f"{vm_name}_snapshot_{snapshot_name}.raw"
            mem_path = os.path.join(vm_dir, snap_fname)
            # a disk-only snapshot writes no memory image, so a file at its
            # name is not in its way
            for path in ((mem_path, f"{mem_path}.zst") if with_memory else ()):
                try:
                    os.lstat(path)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    self.logger.error(
                        f"cannot tell whether {path} already exists ({exc}); "
                        f"refusing to take snapshot '{snapshot_name}' of "
                        f"{vm_name}, which could write over it")
                    return False
                self.logger.error(
                    f"{path} already exists — a leftover of an earlier "
                    f"snapshot, or another VM's memory image under the same "
                    f"name; refusing to write snapshot '{snapshot_name}' of "
                    f"{vm_name} over it (snapshot take --force clears a "
                    f"leftover no domain uses)")
                return False
            overlay_args = self._overlay_diskspec_args(
                vm_name, snapshot_name, disk_dir)
            if overlay_args is None:
                return False
            # Exclude cdroms from the snapshot so no new qcow2 overlay is
            # created for them (they stay at the raw ISO after this call).
            cdrom_args = self._cdrom_diskspec_args(vm_name)
            # separate tokens for every flag and value: build_command quotes
            # each token centrally, so "--domain vm01" as one token would
            # reach virsh as a single literal argument, and a pre-quoted
            # description would be quoted twice
            result = self.virsh.execute(
                "snapshot-create-as",
                "--domain", vm_name,
                "--name", snapshot_name,
                "--description", description,
                "--atomic",
                f"--memspec={mem_path}" if with_memory else "--disk-only",
                *overlay_args,
                *cdrom_args)

            if result.ok:
                if with_memory:
                    self.logger.info(
                        f"snapshot '{snapshot_name}' created for vm {vm_name}")
                else:
                    self.logger.info(
                        f"snapshot '{snapshot_name}' created for vm {vm_name}: "
                        f"disk-only, as the vm is shut off; restoring it "
                        f"leaves the vm shut off")
                if compress_memory and not with_memory:
                    self.logger.info(
                        f"snapshot '{snapshot_name}' of vm {vm_name} saved no "
                        f"memory, so there is none to compress")
                elif compress_memory:
                    if not self.compress_memory_file(mem_path, level=compress_level):
                        self.logger.warning(
                            f"snapshot '{snapshot_name}' for vm {vm_name}: memory "
                            f"compression failed; .raw kept on disk")
                return True
            else:
                self.logger.error(f"failed to create snapshot for vm {vm_name}: {result.stderr}")
                return False
        except Exception as exc:
            self.logger.error(f"error creating snapshot for vm {vm_name}: {exc}")
            return False

    def _would_be_overlay_paths(self,
                                vm_name: str,
                                snapshot_name: str,
                                disk_dir: str | None = None) -> list[str]:
        """
        Predict the overlay file ``snapshot-create-as`` would create for
        each data disk of *vm_name* — in *disk_dir* when one is given
        (:meth:`_overlay_diskspec_args`), else beside the disk.

        With no explicit ``file=`` in the diskspec, libvirt derives the new
        external-overlay path from the disk's *current* source by dropping
        its final suffix and appending ``.<snapshot_name>`` — e.g. a live
        source ``…_provider01.qcow2`` (or ``…_provider01.1785187648``)
        yields ``…_provider01.<snapshot_name>`` (:meth:`_overlay_name`). A
        stale file at that exact path is what triggers libvirt's "external
        snapshot file … already exists and is not a block device" error on
        a re-take.

        A disk a snapshot leaves out (:meth:`_snapshot_excluded_targets`)
        gets no overlay, so a file at its name is no leftover: none is
        predicted for it, and none at all when that cannot be told.
        """
        excluded = self._snapshot_excluded_targets(vm_name)
        if excluded is None:
            return []
        paths = []
        for target, source in self._data_disk_targets(vm_name):
            if not source or source == '-' or target in excluded:
                continue
            directory = (os.path.dirname(source) if disk_dir is None
                         else disk_dir)
            paths.append(os.path.join(
                directory, self._overlay_name(source, snapshot_name)))
        return paths

    @staticmethod
    def _snapshot_info_reports_absent(stderr: str | None) -> bool:
        """
        Whether a failed ``snapshot-info`` clearly means the snapshot does
        not exist (vs. a transient/connectivity error). libvirt reports a
        missing snapshot as e.g. "Domain snapshot not found: no domain
        snapshot with matching name 'X'".
        """
        text = (stderr or "").lower()
        return any(marker in text for marker in (
            "not found", "no domain snapshot", "no snapshot",
            "does not exist"))

    def _force_clear_for_retake(self,
                                vm_name: str,
                                vm_dir: str,
                                snapshot_name: str,
                                paths_in_use=None,
                                recorded_in_use=None,
                                disk_dir: str | None = None) -> bool:
        """
        Remove anything that would make a fresh snapshot named
        *snapshot_name* collide, so ``snapshot take --force`` can re-use the
        name — its memory image in *vm_dir*, its overlays in *disk_dir* or,
        with none, beside their disks. Two cases:

        1. A live libvirt snapshot with that name still exists → delete it
           properly via :meth:`delete_snapshot` (blockcommit/collapse merges
           its overlay back into base). If that delete fails (e.g. it is a
           buried, non-most-recent snapshot), abort rather than overwrite.
        2. The snapshot was already deleted but its per-disk overlay files
           and/or memory ``.raw``/``.raw.zst`` were left on disk (the orphan
           case that produced the "file already exists" error) → remove those
           files.

        Files that are part of the VM's *current* active disk chain are never
        removed. Nor is a leftover any domain uses, the VM itself included,
        or one a snapshot records, as its memory image or an overlay: each
        is matched by resolved path and by identity against *paths_in_use*
        (``LibVirtSession.disk_paths_in_use``) and *recorded_in_use*
        (``LibVirtSession.snapshot_files_in_use``), asked once there is
        anything to remove. The memory image's directory can be a cluster's
        memory_snapshot_dir outside the workdir, reaching a disk under
        another name — a symlink to the directory a live disk is in — or
        holding another VM's image under the same name. One in use or
        recorded, a scan that fails or none given, or a leftover that cannot
        be inspected refuses the retake: the snapshot would be written over
        that file.

        Leftovers already there are asked about before case 1 deletes the
        snapshot, but for the files it records, which the delete deals with:
        a refusal then leaves the snapshot in place, rather than costing the
        one the retake was to replace. A restore puts back the overlays a
        revert replaced, under the very names a retake writes (#272).

        Returns True when the name is clear to re-use.
        """
        info = self.virsh.execute(
            "snapshot-info", vm_name, snapshot_name, warn=True)
        if info.ok:
            own = self._files_recorded_by(vm_name, snapshot_name)
            if own is not None:
                early = [path for path in self._retake_leftovers(
                             vm_name, vm_dir, snapshot_name, disk_dir)
                         if path not in own
                         and os.path.realpath(path) not in own]
                refusal = self._leftover_in_use(early, paths_in_use,
                                                recorded_in_use)
                if refusal:
                    self.logger.error(
                        f"--force: {refusal}; refusing to remove it, and so "
                        f"to delete '{snapshot_name}' on {vm_name} and take "
                        f"it again over it; '{snapshot_name}' is left as it "
                        f"is")
                    return False
            self.logger.info(
                f"--force: removing existing snapshot '{snapshot_name}' on "
                f"{vm_name} before re-taking")
            # its memory image goes with it, wherever it was recorded -- a
            # memory_snapshot_dir set otherwise since, say -- unless another
            # snapshot records it, which the leftover check below refuses
            if not self.delete_snapshot(vm_name, snapshot_name,
                                        recorded_in_use=recorded_in_use):
                self.logger.error(
                    f"--force: could not remove existing snapshot "
                    f"'{snapshot_name}' on {vm_name}; refusing to overwrite")
                return False
        elif not self._snapshot_info_reports_absent(info.stderr):
            # snapshot-info failed for a reason other than "snapshot does not
            # exist" (libvirt connectivity, auth, a transient error). We can't
            # tell whether the snapshot is really there, so removing candidate
            # files risks deleting a live snapshot's overlay/memory — abort.
            self.logger.error(
                f"--force: cannot determine whether snapshot "
                f"'{snapshot_name}' exists on {vm_name} (snapshot-info "
                f"failed: {(info.stderr or '').strip()}); refusing to "
                f"remove any files")
            return False

        removed = []
        leftovers = self._retake_leftovers(vm_name, vm_dir, snapshot_name,
                                           disk_dir)
        refusal = self._leftover_in_use(leftovers, paths_in_use,
                                        recorded_in_use)
        if refusal:
            self.logger.error(
                f"--force: {refusal}; refusing to remove it, or to take "
                f"'{snapshot_name}' on {vm_name} over it")
            return False
        for path in leftovers:
            self.virsh.execute_shell(
                f"rm -f -- {shlex.quote(path)}", warn=True)
            removed.append(path)
        if removed:
            self.logger.info(
                f"--force: removed {len(removed)} leftover snapshot file(s) "
                f"for '{snapshot_name}' on {vm_name}")
        return True

    def _retake_leftovers(self, vm_name: str, vm_dir: str,
                          snapshot_name: str,
                          disk_dir: str | None) -> list[str]:
        """
        The files at the names a take of *snapshot_name* writes — each
        disk's overlay (:meth:`_would_be_overlay_paths`) and the memory
        image, compressed or not — that are there now, but for those in the
        live chain, which are never removed.
        """
        active = {src for _t, src in self._data_disk_targets(vm_name)}
        candidates = list(self._would_be_overlay_paths(
            vm_name, snapshot_name, disk_dir=disk_dir))
        snap_fname = f"{vm_name}_snapshot_{snapshot_name}.raw"
        candidates.append(os.path.join(vm_dir, snap_fname))
        candidates.append(os.path.join(vm_dir, f"{snap_fname}.zst"))
        return [path for path in candidates
                if path not in active and os.path.isfile(path)]

    def _files_recorded_by(self, vm_name: str,
                           snapshot_name: str) -> set[str] | None:
        """
        The files snapshot *snapshot_name* of *vm_name* records — its
        overlays, its memory image and that image's ``.zst`` — as recorded
        and resolved; ``None`` when they cannot be read
        (:meth:`recorded_files`).
        """
        recorded = self.recorded_files(vm_name)
        if recorded is None:
            return None
        images, overlays, _unnamed = recorded
        files = list(overlays.get(snapshot_name, []))
        image = images.get(snapshot_name)
        if image:
            files += [image, f"{image}.zst"]
        return set(files) | {os.path.realpath(path) for path in files}

    @staticmethod
    def _leftover_in_use(leftovers: list[str], paths_in_use,
                         recorded_in_use) -> str | None:
        """
        Why one of *leftovers* may not be removed, if one may not: a domain
        uses it, or a domain's snapshot records it (as its memory image or
        an overlay), by resolved path or by identity (``(st_dev, st_ino)``, links
        followed); it cannot be inspected; or either cannot be told — the
        scan is ``None``, or answers ``None``.
        """
        if not leftovers:
            return None
        identities = {}
        for path in leftovers:
            try:
                st = os.stat(path)
            except OSError as exc:
                return f"{path} could not be inspected ({exc})"
            identities[path] = (st.st_dev, st.st_ino)
        in_use = paths_in_use() if paths_in_use is not None else None
        if in_use is None:
            return (f"could not check whether a domain uses "
                    f"{', '.join(leftovers)}")
        recorded = recorded_in_use() if recorded_in_use is not None else None
        if recorded is None:
            return (f"could not check whether a snapshot of another domain "
                    f"records {', '.join(leftovers)}")
        for scan, what in ((in_use, "domain {} uses it"),
                           (recorded, "a snapshot of domain {} records it")):
            used_ids = getattr(scan, "identities", {})
            for path in leftovers:
                domain = (scan.get(os.path.realpath(path))
                          or used_ids.get(identities[path]))
                if domain:
                    return f"{path}: {what.format(domain)}"
        return None

    # ── memory file compression (zstd) ──────────────────────────────────
    #
    # libvirt's --memspec writes a raw memory dump that's typically the
    # full RAM size of the VM (multi-GB).  zstd -3 reduces this by ~71%
    # at sub-second/GB.  We keep the libvirt snapshot metadata pointing
    # at the original .raw path; the .raw.zst sits next to it.  At
    # restore time, snapshot_restore checks for the sibling and
    # decompresses just-in-time before issuing snapshot-revert.

    def _is_zstd_available(self) -> bool:
        """Whether ``zstd`` is reachable in the configured runtime."""
        result = self.virsh.execute_shell("command -v zstd", warn=True, hide=True)
        return result.ok

    def _memory_path_from_xml(self, vm_name: str, snapshot_name: str) -> str | None:
        """Return the memory file path libvirt has recorded for a snapshot."""
        result = self.virsh.execute(
            "snapshot-dumpxml", vm_name, snapshot_name, warn=True)
        if not result.ok:
            return None
        return self._memory_file_in(result.stdout)

    @staticmethod
    def _memory_file_in(xml_text: str | None) -> str | None:
        """The memory file a snapshot's XML records, or None: a disk-only
        snapshot records none, and XML that does not parse names none."""
        try:
            root = ET.fromstring(xml_text or "")
        except ET.ParseError:
            return None
        memory_elem = root.find("memory")
        if memory_elem is None:
            return None
        return memory_elem.get("file") or None

    def memory_images(self, vm_name: str) -> dict[str, str] | None:
        """
        ``{snapshot name: memory file}`` for every snapshot of *vm_name* —
        a domain's name or UUID, as virsh takes either — that saved the
        guest's memory, the file as libvirt recorded it: the ``.raw``, also
        once compression left only its ``.raw.zst``.

        Returns ``None`` when they cannot all be read — the list, or any
        one snapshot's XML — which is not "none recorded"; or when the list
        cannot be trusted, as :meth:`_strict_overlay_inventory` refuses it:
        a blank entry, which cannot be told apart from a name made only of
        newlines, or a name listed twice.
        """
        recorded = self.recorded_files(vm_name)
        return None if recorded is None else recorded[0]

    def overlay_files(self, vm_name: str) -> dict[str, list[str]] | None:
        """
        ``{snapshot name: [overlay, ...]}`` for every snapshot of *vm_name*
        — a domain's name or UUID — that created external overlays, each as
        libvirt recorded it (:meth:`recorded_files`); ``None`` when they
        cannot all be told, an external one that names no local file
        included.
        """
        recorded = self.recorded_files(vm_name)
        if recorded is None or recorded[2]:
            return None
        return recorded[1]

    def recorded_files(self, vm_name: str) -> tuple[
            dict[str, str], dict[str, list[str]], bool] | None:
        """
        The files every snapshot of *vm_name* — a domain's name or UUID —
        recorded, in one read: ``({snapshot: memory file}, {snapshot:
        [overlay, ...]}, unnamed)``, each as libvirt recorded it, a snapshot
        only where it recorded one, and *unnamed* whether an external disk
        named no local file — a network source, or none — whose overlay is
        then somewhere unknown, and can be built on local files.

        Read for what they name, not to revert: a disk whose mode names no
        file — ``no``, ``internal``, ``manual`` — records none, and an
        external one names its ``file`` or, a block device, its ``dev``.
        :meth:`_strict_overlay_inventory` refuses those forms it cannot back
        up; a teardown, and the scan of what other domains record, would
        then fail over a domain whose snapshots are only unusual.

        Returns ``None`` when they cannot all be read — the list, or any one
        snapshot's XML — which is not "none recorded"; or when the list
        cannot be trusted: a blank entry, which cannot be told apart from a
        name made only of newlines, or a name listed twice.
        """
        roots = self._snapshot_roots(vm_name)
        if roots is None:
            return None
        images: dict[str, str] = {}
        overlays: dict[str, list[str]] = {}
        unnamed = False
        for name, root in roots.items():
            memory_elem = root.find("memory")
            path = memory_elem.get("file") if memory_elem is not None else None
            if path:
                images[name] = path
            files = []
            for disk in root.findall("disks/disk"):
                if disk.get("snapshot") != "external":
                    continue
                source = disk.find("source")
                path = (source.get("file") or source.get("dev")
                        if source is not None else None)
                if path:
                    files.append(path)
                else:
                    unnamed = True
            if files:
                overlays[name] = files
        return images, overlays, unnamed

    def _snapshot_roots(self, vm_name: str) -> dict[str, Any] | None:
        """
        ``{snapshot: its parsed XML}`` for every snapshot of *vm_name*;
        ``None`` when they cannot all be read, or the list cannot be
        trusted (see :meth:`recorded_files`).
        """
        list_result = self.virsh.execute(
            "snapshot-list", vm_name, "--name", warn=True)
        if not list_result.ok:
            return None
        names = self._listed_snapshot_names(list_result.stdout)
        if any(n == '' for n in names) or len(set(names)) != len(names):
            return None
        roots = {}
        for name in names:
            xml_result = self.virsh.execute(
                "snapshot-dumpxml", vm_name, name, warn=True)
            if not xml_result.ok:
                return None
            try:
                roots[name] = ET.fromstring(xml_result.stdout)
            except ET.ParseError:
                return None
        return roots

    def disk_layers(self, vm_name: str) -> dict[str, set[str]] | None:
        """
        ``{disk target: {file, ...}}``: every file a snapshot of *vm_name*
        records for each of its disks, as libvirt recorded it — the overlay
        a take made (``<disks>``), the one a revert made (``<revertDisks>``,
        or ``<disks>``, which a revert rewrites for a snapshot with no
        children), and the disk's source in each definition it saved
        (``<domain>``, ``<inactiveDomain>``), which a later take, or a
        revert, built on. Together they are every layer a take or a restore
        of this domain left under a disk (:class:`SnapshotOverlays`).

        A disk whose mode names no file records none. ``None`` when the
        snapshots cannot all be read, which is not "none recorded"; ``{}``
        when it has none.
        """
        roots = self._snapshot_roots(vm_name)
        if roots is None:
            return None
        layers: dict[str, set[str]] = {}
        for root in roots.values():
            for section in root:
                if section.tag in ("disks", "revertDisks"):
                    disks = [(disk.get("name"), disk)
                             for disk in section.findall("disk")
                             if disk.get("snapshot") == "external"]
                elif section.tag in ("domain", "inactiveDomain"):
                    disks = [(target.get("dev"), disk)
                             for disk in section.findall("devices/disk")
                             if (target := disk.find("target")) is not None]
                else:
                    continue
                for target, disk in disks:
                    source = disk.find("source")
                    path = (source.get("file") or source.get("dev")
                            if source is not None else None)
                    if target and path:
                        layers.setdefault(target, set()).add(path)
        return layers

    def compress_memory_file(self,
                             raw_path: str,
                             level: int = 3,
                             threads: int = 0) -> bool:
        """
        zstd-compress *raw_path* in place to ``<raw_path>.zst`` (and remove
        the .raw on success — ``--rm``).

        Idempotent: if .raw is already gone and .raw.zst exists, returns True.
        """
        zst_path = f"{raw_path}.zst"
        if not os.path.isfile(raw_path):
            if os.path.isfile(zst_path):
                return True
            self.logger.warning(f"memory file not found: {raw_path}")
            return False
        if not self._is_zstd_available():
            self.logger.error(
                "zstd not found in runtime — install zstd in the docker "
                "image / host before using --compress-memory")
            return False
        sudo = "sudo " if self.use_sudo else ""
        cmd = (f"{sudo}zstd -{level} -T{threads} --rm -q -f "
               f"-o '{zst_path}' '{raw_path}'")
        self.logger.info(f"compressing memory file {raw_path} (zstd -{level})")
        result = self.virsh.execute_shell(cmd, warn=True)
        if not result.ok:
            self.logger.error(
                f"zstd compress failed for {raw_path}: {result.stderr.strip()}")
            return False
        return True

    def compress_all_memory(self, vm_name: str, level: int = 3) -> tuple[int, int]:
        """
        Compress every snapshot's memory ``.raw`` file for *vm_name*.

        Returns ``(compressed, total)`` — total counts snapshots that *had*
        an uncompressed memory file at start; compressed counts ones we
        successfully shrunk. Snapshots whose memory was already compressed
        (or absent) are not counted.
        """
        compressed = 0
        candidates = 0
        for snap in self.list_snapshots(vm_name):
            mem_path = self._memory_path_from_xml(vm_name, snap['name'])
            if not mem_path:
                continue
            if not os.path.isfile(mem_path):
                continue  # already compressed or missing
            candidates += 1
            if self.compress_memory_file(mem_path, level=level):
                compressed += 1
        return compressed, candidates

    def decompress_all_memory(self, vm_name: str) -> tuple[int, int]:
        """Inverse of :meth:`compress_all_memory`."""
        decompressed = 0
        candidates = 0
        for snap in self.list_snapshots(vm_name):
            mem_path = self._memory_path_from_xml(vm_name, snap['name'])
            if not mem_path:
                continue
            if not os.path.isfile(f"{mem_path}.zst"):
                continue
            if os.path.isfile(mem_path):
                continue  # already decompressed
            candidates += 1
            if self.decompress_memory_file(mem_path, keep_zst=False):
                decompressed += 1
        return decompressed, candidates

    def decompress_memory_file(self,
                               raw_path: str,
                               keep_zst: bool = True) -> bool:
        """
        Decompress ``<raw_path>.zst`` → ``<raw_path>``.

        ``keep_zst`` defaults to True because callers (snapshot_restore)
        typically want to re-compress after revert; storing both during
        the revert is a small price for cheap recompression.
        """
        zst_path = f"{raw_path}.zst"
        if not os.path.isfile(zst_path):
            return False
        if os.path.isfile(raw_path):
            return True  # already decompressed
        if not self._is_zstd_available():
            self.logger.error(
                "zstd not found in runtime — cannot decompress memory file")
            return False
        sudo = "sudo " if self.use_sudo else ""
        keep_flag = "-k " if keep_zst else ""
        cmd = (f"{sudo}zstd -d {keep_flag}-q -f "
               f"-o '{raw_path}' '{zst_path}'")
        self.logger.info(f"decompressing memory file {zst_path}")
        result = self.virsh.execute_shell(cmd, warn=True)
        if not result.ok:
            self.logger.error(
                f"zstd decompress failed for {zst_path}: {result.stderr.strip()}")
            return False
        return True

    def get_latest_snapshot(self, vm_name: str) -> str | None:
        """
        Get the name of the current (latest) snapshot for a VM.

        Args:
            vm_name: Name of the VM

        Returns:
            str: Name of the current snapshot, or None if none exists
        """
        result = self.virsh.execute(
            "snapshot-current", vm_name, "--name", warn=True)
        if result.ok:
            name = result.stdout.strip()
            return name if name else None
        return None

    def snapshot_info(self,
                      vm_name: str,
                      snapshot_name: str) -> dict | None:
        """
        Parse ``virsh snapshot-info`` text output into a dict.

        Returns ``None`` if the snapshot is unknown or virsh fails. Keys::

            name, domain, current (bool), state, location,
            parent (str|None — '-' is mapped to None),
            children (int), descendants (int), metadata (bool),
            creation_time (raw string from libvirt).

        Creation time is reported by ``snapshot-info`` only — it is not
        present in ``snapshot-dumpxml``. We keep it as the libvirt-formatted
        string (e.g. ``2026-04-22 18:30:00 +0200``) so callers can sort it
        lexicographically and humans can read it.
        """
        result = self.virsh.execute(
            "snapshot-info", vm_name, snapshot_name, warn=True)
        if not result.ok:
            return None
        info: dict = {}
        for line in result.stdout.splitlines():
            if ':' not in line:
                continue
            key, _, val = line.partition(':')
            info[key.strip().lower().replace(' ', '_')] = val.strip()
        info['current'] = info.get('current', '').lower() == 'yes'
        info['metadata'] = info.get('metadata', '').lower() == 'yes'
        for k in ('children', 'descendants'):
            try:
                info[k] = int(info.get(k, 0))
            except (TypeError, ValueError):
                info[k] = 0
        if info.get('parent') in ('-', ''):
            info['parent'] = None
        return info

    def _parse_snapshot_xml(self, xml_text: str) -> dict:
        """
        Pull description / parent / creation_time out of a single
        ``<domainsnapshot>`` XML in one pass.

        Creation time comes from the ``<creationTime>`` element (Unix
        epoch seconds) — that's the format that exists on every libvirt
        version. ``virsh snapshot-info``'s text output adds a "Creation
        Time:" line on newer libvirt only, so the XML is the portable
        source.
        """
        from datetime import datetime

        out: dict = {}
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return out
        desc_elem = root.find("description")
        if desc_elem is not None and desc_elem.text:
            out['description'] = desc_elem.text
        parent_elem = root.find("parent/name")
        if parent_elem is not None and parent_elem.text:
            out['parent'] = parent_elem.text
        state_elem = root.find("state")
        if state_elem is not None and state_elem.text:
            out['state'] = state_elem.text.strip()
        ct_elem = root.find("creationTime")
        if ct_elem is not None and ct_elem.text:
            try:
                ts = int(ct_elem.text.strip())
                out['creation_time'] = datetime.fromtimestamp(ts).strftime(
                    '%Y-%m-%d %H:%M:%S')
            except (ValueError, OSError):
                pass
        return out

    @staticmethod
    def _holds_no_memory(info: dict) -> bool:
        """
        Whether the snapshot that *info* (:meth:`_parse_snapshot_xml`)
        describes saved no memory, so that restoring it leaves the VM shut
        off: its domain was not running or paused -- 'shutoff' for one
        taken of a shut-off VM, 'disk-snapshot' for a disk-only one of a
        running VM. A state it does not record claims neither.
        """
        state = info.get('state')
        return state is not None and state not in ('running', 'paused')

    def state_and_creation_time(self, vm_name: str, snapshot_name: str
                                ) -> tuple[str | None, int | None]:
        """
        The state snapshot *snapshot_name* of *vm_name* recorded its domain
        in, and when it was taken, in Unix epoch seconds.

        The state is the one a revert to it leaves the domain in, as boxman
        reverts (no ``--running`` or ``--paused``): 'running' or 'paused'
        for one that saved the guest's memory, 'shutoff' or 'disk-snapshot'
        for one that did not (:meth:`_holds_no_memory`). Read after a
        revert, to set the guest's clock (#275). Either is None where the
        XML does not hold it, both where it cannot be read.
        """
        result = self.virsh.execute(
            "snapshot-dumpxml", vm_name, snapshot_name, warn=True)
        if not result.ok:
            return None, None
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError:
            return None, None
        state = (root.findtext("state") or "").strip() or None
        created = (root.findtext("creationTime") or "").strip()
        return state, int(created) if created.isdigit() else None

    @classmethod
    def _definition_a_revert_builds_on(cls, root):
        """
        The definition the snapshot whose XML is *root* saved that
        ``snapshot-revert`` lays its new overlays on: its ``<domain>`` when
        it saved the guest's memory, else its ``<inactiveDomain>`` if it has
        one (``qemuSnapshotRevertPrep``, then ``qemuSnapshotRevertInactive``
        for one that saved none). None when it saved neither.
        """
        state = root.findtext("state")
        if cls._holds_no_memory({'state': state.strip() if state else None}):
            inactive = root.find("inactiveDomain")
            if inactive is not None:
                return inactive
        return root.find("domain")

    def _snapshot_graph(self, vm_name: str) -> dict[str, dict]:
        """
        Return ``{name: info}`` for every snapshot of *vm_name*.

        Runs ``snapshot-list --name`` once and ``snapshot-dumpxml`` once
        per snapshot. Each *info* carries the parsed fields from
        :meth:`_parse_snapshot_xml` (``description`` / ``parent`` /
        ``creation_time``, when present) plus the raw ``xml`` (``None``
        when the dumpxml failed, so callers can skip those entries).
        """
        list_result = self.virsh.execute(
            "snapshot-list", vm_name, "--name", warn=True)
        if not list_result.ok:
            return {}

        graph: dict[str, dict] = {}
        # exact names, newline-delimited: see _strict_overlay_inventory
        for name in list_result.stdout.split('\n'):
            if not name:
                continue
            xml_result = self.virsh.execute(
                "snapshot-dumpxml", vm_name, name, warn=True)
            xml_text = xml_result.stdout if xml_result.ok else None
            info = (self._parse_snapshot_xml(xml_text)
                    if xml_text is not None else {})
            info['xml'] = xml_text
            graph[name] = info
        return graph

    def list_snapshots_detailed(self, vm_name: str) -> list[dict]:
        """
        Return per-snapshot dicts for *vm_name* in chain order
        (oldest first, head last) in a single pass.

        Each dict: ``{name, description, creation_time, parent, depth,
        disk_only}`` — *disk_only* when it saved no memory
        (:meth:`_holds_no_memory`). Calls ``snapshot-list`` once, then
        ``snapshot-dumpxml`` once per snapshot — everything else is parsed
        out of the XML via :meth:`_parse_snapshot_xml`.
        """
        parsed = self._snapshot_graph(vm_name)
        if not parsed:
            return []
        names = list(parsed)

        # Topological sort by parent → child.
        children: dict[str | None, list[str]] = {}
        for name in names:
            parent = parsed[name].get('parent')
            children.setdefault(parent, []).append(name)

        ordered: list[str] = []
        queue: list[str | None] = list(children.get(None, []))
        while queue:
            n = queue.pop(0)
            ordered.append(n)
            queue.extend(children.get(n, []))
        # Defensive: pick up any snapshot whose parent isn't visible
        # (e.g. libvirt-orphaned metadata) — append in original list order.
        for name in names:
            if name not in ordered:
                ordered.append(name)

        return [
            {
                'name': name,
                'description': parsed[name].get('description', ''),
                'creation_time': parsed[name].get('creation_time'),
                'parent': parsed[name].get('parent'),
                'depth': depth,
                'disk_only': self._holds_no_memory(parsed[name]),
            }
            for depth, name in enumerate(ordered)
        ]

    def validate_snapshot(self, vm_name: str, snapshot_name: str):
        """
        Validate that a snapshot is intact and can be safely restored.

        Checks:
        1. ``virsh snapshot-info`` succeeds (snapshot exists in libvirt).
        2. The memory file referenced in the snapshot XML exists on disk:
           the ``.raw`` itself, or its compressed ``.raw.zst`` sibling
           (``snapshot take --compress-memory``, ``storage
           compress-snapshots``), which :meth:`snapshot_restore`
           decompresses before the revert. libvirt keeps recording the
           ``.raw`` path either way.
        3. For each disk it made an external overlay of, the image that
           overlay was laid on exists: the disk's source in the definition
           it saved (:meth:`_definition_a_revert_builds_on`), which a
           revert lays a new overlay on. Not the overlay its take made,
           which its ``<disks>`` record and no revert to it reads.

        Args:
            vm_name: Name of the VM
            snapshot_name: Name of the snapshot to validate

        Returns:
            tuple[bool, List[str]]: (valid, errors) where errors is empty on success.
        """
        errors = []

        # 1. Check snapshot exists in libvirt
        info_result = self.virsh.execute("snapshot-info", vm_name, snapshot_name, warn=True)
        if not info_result.ok:
            errors.append(f"snapshot-info failed: {info_result.stderr.strip()}")
            return False, errors

        # 2. Parse snapshot XML to check files
        dumpxml_result = self.virsh.execute(
            "snapshot-dumpxml", vm_name, snapshot_name, warn=True)
        if not dumpxml_result.ok:
            errors.append(f"snapshot-dumpxml failed: {dumpxml_result.stderr.strip()}")
            return False, errors

        try:
            root = ET.fromstring(dumpxml_result.stdout)

            # Check memory file: a compressed snapshot keeps only the
            # .raw.zst next to the .raw path libvirt recorded
            memory_elem = root.find("memory")
            if memory_elem is not None:
                mem_file = memory_elem.get("file")
                if (mem_file and not os.path.isfile(mem_file)
                        and not os.path.isfile(f"{mem_file}.zst")):
                    errors.append(
                        f"memory file missing: {mem_file} (nor {mem_file}.zst)")

            # Check what a revert builds on: for each disk the snapshot made
            # an external overlay of, the disk's source in the definition it
            # saved, which the revert lays a new overlay on
            # (qemuSnapshotRevertExternalPrepare). Not the overlay its take
            # made, which its <disks> record: no revert to it reads that, nor
            # libvirt's delete of it, which merges what its child was taken
            # on (qemuSnapshotExternalGetSnapDiskSrc). And it can be gone:
            # once a restore of this snapshot and a take have cleared its
            # <revertDisks>, libvirt's delete of a child taken on it, a leaf
            # that is not current, unlinks it and leaves the record
            # (qemuSnapshotDiscardExternal, #289).
            definition = self._definition_a_revert_builds_on(root)
            saved = {}
            for device in (definition.iter("disk")
                           if definition is not None else ()):
                target, source = device.find("target"), device.find("source")
                if target is not None and source is not None:
                    saved[target.get("dev")] = source.get("file")
            for disk in root.findall("disks/disk"):
                if disk.get("snapshot") != "external":
                    continue
                name = disk.get("name")
                src_file = saved.get(name)
                if src_file and not os.path.isfile(src_file):
                    errors.append(f"disk image missing: {src_file} "
                                  f"(a revert builds {name} on it)")

        except ET.ParseError as exc:
            errors.append(f"failed to parse snapshot XML: {exc}")
            return False, errors

        return len(errors) == 0, errors

    def list_snapshots(self, vm_name: str) -> list[dict[str, str]]:
        """
        List all snapshots for a vm.

        Args:
            vm_name: Name of the vm

        Returns:
            list: List of snapshot info dictionaries
        """
        try:
            # fetch the available snapshots and their descriptions
            graph = self._snapshot_graph(vm_name)
            return [
                {'name': name, 'description': info.get('description', ''),
                 'disk_only': self._holds_no_memory(info)}
                for name, info in graph.items()
                if info['xml'] is not None
            ]
        except Exception as exc:
            self.logger.error(f"error listing snapshots for vm {vm_name}: {exc}")
            return []

    def _get_snapshot_overlay_files(self, vm_name: str) -> dict[str, str]:
        """
        Return a mapping of snapshot_name -> set of overlay file paths
        for every external snapshot on *vm_name*.
        """
        overlays: dict[str, list[str]] = {}
        for snap, info in self._snapshot_graph(vm_name).items():
            if info['xml'] is None:
                continue
            try:
                root = ET.fromstring(info['xml'])
                files = []
                for disk in root.findall(".//disks/disk"):
                    if disk.get("snapshot") != "external":
                        continue
                    source = disk.find("source")
                    if source is not None and source.get("file"):
                        files.append(source.get("file"))
                if files:
                    overlays[snap] = files
            except ET.ParseError:
                continue
        return overlays

    @staticmethod
    def _listed_snapshot_names(stdout: str) -> list[str]:
        """
        The entries ``virsh snapshot-list --name`` printed, split on newline
        alone, without the terminators virsh adds.

        Exactly those, and no more: one from the newline after the last
        name, and one from the blank line vsh prints after a command.
        Popping *every* trailing empty instead erases the evidence a caller
        needs — a list whose only snapshot is named LF is all empty
        fragments, and would come back as a domain with no snapshots at
        all.
        """
        names = stdout.split('\n')
        if names and names[-1] == '':
            names.pop()
        if names and names[-1] == '':
            names.pop()
        return names

    def _strict_overlay_inventory(
            self, vm_name: str) -> tuple[dict[str, list[str]], list[str]]:
        """
        Enumerate every snapshot of *vm_name*, its external overlays and the
        chain order — refusing on any gap rather than skipping it.

        The permissive helpers behind the display verbs skip whatever they
        cannot read: :meth:`_snapshot_graph` returns ``{}`` when the list
        command fails and records ``xml=None`` for a dumpxml that failed,
        and :meth:`_get_snapshot_overlay_files` then ``continue``s past
        those entries and past XML that will not parse. That is the right
        shape for drawing a table and the wrong one for deciding what a
        revert is about to delete: a snapshot missing from the inventory
        is a snapshot whose overlay nothing backs up, and
        ``snapshot-revert`` deletes it all the same.

        Args:
            vm_name: the domain to enumerate.

        Returns:
            tuple: ``({snapshot: [overlay, ...]}, [snapshot, ...])`` — the
            external overlays of every snapshot that has any, and every
            snapshot name in chain order, oldest first.

        Raises:
            SnapshotError: the snapshot list failed, a dumpxml failed, an
                XML did not parse, or the parent links do not reach every
                snapshot.
        """
        refusal = ("refusing to revert: a snapshot this backup cannot "
                   "account for is one whose overlay it cannot preserve")

        list_result = self.virsh.execute(
            "snapshot-list", vm_name, "--name", warn=True)
        if not list_result.ok:
            raise SnapshotError(
                f"cannot list the snapshots of {vm_name} "
                f"({(list_result.stderr or '').strip()}); {refusal}")

        # split on newline alone, and keep the names exactly as libvirt
        # gave them. `splitlines()` also breaks on U+2028 and friends,
        # which libvirt accepts *inside* a snapshot name, and `.strip()`
        # erases a name made of spaces -- either way the snapshot drops out
        # of the inventory before the duplicate and topology checks can
        # notice, and the revert deletes its overlay regardless.
        names = self._listed_snapshot_names(list_result.stdout)

        # A name containing a newline splits into fragments that name no
        # snapshot, and the dumpxml below refuses on those. A name made
        # *only* of newlines leaves no fragment at all to refuse on -- it
        # shows up here as a blank entry, indistinguishable from a blank
        # line, so this is as far as the output format can be trusted.
        if any(n == '' for n in names):
            raise SnapshotError(
                f"the snapshot list of {vm_name} has a blank entry, which "
                f"cannot be told apart from a name made only of newlines; "
                f"{refusal}")

        # a repeated name collapses in the dictionaries below, so one
        # snapshot's overlays would silently replace the other's
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise SnapshotError(
                f"{vm_name} lists the snapshot name(s) "
                f"{', '.join(duplicates)} more than once; {refusal}")

        overlays: dict[str, list[str]] = {}
        parents: dict[str, str | None] = {}
        for name in names:
            # read as _snapshot_xmls reads it: the same XML, with secrets
            # nothing here looks at, so that a restore's next read of every
            # snapshot is answered from this one (read_cache)
            xml_result = self.virsh.execute(
                "snapshot-dumpxml", vm_name, name, "--security-info",
                warn=True)
            if not xml_result.ok:
                raise SnapshotError(
                    f"cannot read the metadata of snapshot '{name}' on "
                    f"{vm_name} ({(xml_result.stderr or '').strip()}); "
                    f"{refusal}")
            try:
                root = ET.fromstring(xml_result.stdout)
            except ET.ParseError as exc:
                raise SnapshotError(
                    f"the metadata of snapshot '{name}' on {vm_name} does "
                    f"not parse ({exc}); {refusal}") from exc

            files = self._overlays_from_snapshot_xml(
                root, name, vm_name, refusal)
            if files:
                overlays[name] = files

            parent_elem = root.find("parent/name")
            parents[name] = (parent_elem.text
                             if parent_elem is not None and parent_elem.text
                             else None)

        # a breadth-first walk from the roots, but its result has to
        # account for every snapshot: one whose parent is missing is never
        # reached from the root and drops out of the ordering without
        # saying so, which then puts it outside the at-risk set the revert
        # will delete from
        children: dict[str | None, list[str]] = {}
        for name, parent in parents.items():
            children.setdefault(parent, []).append(name)

        order: list[str] = []
        queue: list[str | None] = [None]
        while queue:
            for child in children.get(queue.pop(0), []):
                order.append(child)
                queue.append(child)

        unplaced = sorted(set(names) - set(order))
        if unplaced:
            raise SnapshotError(
                f"the snapshot chain of {vm_name} does not place "
                f"{', '.join(unplaced)} (a parent is missing from it); "
                f"{refusal}")

        return overlays, order

    @staticmethod
    def _overlays_from_snapshot_xml(root,
                                    name: str,
                                    vm_name: str,
                                    refusal: str,
                                    section: str = "disks") -> list[str]:
        """
        The external overlay files a snapshot's metadata declares in
        *section* — ``<disks>``, or ``<revertDisks>`` — or a refusal, when
        the metadata cannot be read as a complete answer.

        Well-formed XML is not the same as usable metadata. A document with
        no ``<disks>``, an external disk with no ``<source>``, a disk that
        does not say whether it is external: each of those yields an empty
        or short file list that looks exactly like "this snapshot owns no
        overlays", and the revert then deletes overlays nothing backed up.
        So every disk has to classify itself, and every external one has to
        name its file.

        An empty ``<disks>`` is refused too. libvirt enumerates the
        domain's disks there, so an empty list is truncated metadata rather
        than a domain without disks — and a domain without disks has no
        overlay to lose by refusing.

        Args:
            root: the parsed ``<domainsnapshot>`` element.
            name: the snapshot name that was asked for.
            vm_name: the domain, for the message.
            refusal: the shared tail explaining why a gap refuses.
            section: the element whose disks are read.

        Returns:
            list: the external overlay paths, possibly empty for a snapshot
            whose disks are all internal.

        Raises:
            SnapshotError: the metadata does not answer the question.
        """
        if root.tag != 'domainsnapshot':
            raise SnapshotError(
                f"the metadata of snapshot '{name}' on {vm_name} is a "
                f"<{root.tag}>, not a <domainsnapshot>; {refusal}")

        name_elem = root.find("name")
        # exactly, not stripped: ' s1 ' and 's1' are different snapshots
        reported = name_elem.text if name_elem is not None else None
        if reported != name:
            raise SnapshotError(
                f"asked libvirt for snapshot '{name}' of {vm_name} and got "
                f"metadata naming '{reported}'; {refusal}")

        disks = root.find(section)
        if disks is None:
            raise SnapshotError(
                f"the metadata of snapshot '{name}' on {vm_name} has no "
                f"<{section}> element, so there is no telling which disks it "
                f"owns overlays for; {refusal}")

        entries = disks.findall("disk")
        if not entries:
            raise SnapshotError(
                f"the metadata of snapshot '{name}' on {vm_name} lists no "
                f"disks at all, which is truncated metadata rather than a "
                f"domain without disks; {refusal}")

        files = []
        for disk in entries:
            label = disk.get("name") or "?"
            kind = disk.get("snapshot")
            if kind is None:
                raise SnapshotError(
                    f"disk '{label}' of snapshot '{name}' on {vm_name} does "
                    f"not say whether it is an external snapshot; {refusal}")
            # an empty or unknown mode is not a positively identified
            # internal disk -- treating it as one drops a real overlay
            if kind not in ('external', 'internal', 'no'):
                raise SnapshotError(
                    f"disk '{label}' of snapshot '{name}' on {vm_name} "
                    f"reports the unrecognised snapshot mode '{kind}'; "
                    f"{refusal}")
            if kind != "external":
                continue
            source = disk.find("source")
            path = source.get("file") if source is not None else None
            if not path or not path.strip():
                raise SnapshotError(
                    f"the external disk '{label}' of snapshot '{name}' on "
                    f"{vm_name} names no overlay file; {refusal}")
            # a relative path resolves against whatever directory boxman
            # happens to be run from, which is not where the overlay is
            if not os.path.isabs(path):
                raise SnapshotError(
                    f"the external disk '{label}' of snapshot '{name}' on "
                    f"{vm_name} names the relative overlay path '{path}', "
                    f"which this cannot resolve to a file; {refusal}")
            files.append(path)
        return files

    def _overlays_the_revert_deletes(self, vm_name: str,
                                     names: list[str]) -> list[str]:
        """
        The overlays ``snapshot-revert`` of *vm_name* deletes, whichever
        snapshot it goes to: the external ones its *current* snapshot
        records — its ``<revertDisks>`` when it has any, else its
        ``<disks>`` (``qemuSnapshotRevertExternalFinish``, libvirt 10.10;
        the revert of a disk-only snapshot ends the same way).

        The current snapshot is not always the target or newer than it in
        chain order (#287):

        - libvirt's own delete of the current snapshot makes its parent
          current (``qemuSnapshotDiscardMetadata``). A take after a restore
          of that parent has cleared its ``<revertDisks>``
          (``qemuSnapshotClearRevertdisks`` in ``qemuSnapshotCreate``), so
          its ``<disks>`` name its take-time overlays, which its other
          children are built on. ``snapshot take --force`` deletes first,
          and a delete boxman carries out by collapsing ends in libvirt's
          metadata delete, which does the same.
        - deleting a child of the current snapshot, restored and not taken
          since, moves its ``<revertDisks>`` into its ``<disks>``
          (``qemuSnapshotDeleteUpdateParent``): it then records the
          overlays the guest runs on.
        - ``snapshot-list`` lists the snapshots by name, so a current one in
          another branch can come before the target.

        The XML comes from ``snapshot-current`` itself, so the name is
        libvirt's own, not one parsed back out of a line of text.

        Args:
            vm_name: the domain about to be reverted.
            names: every snapshot of it, as the strict inventory listed them.

        Returns:
            list: the overlay paths; empty when it has no snapshots.

        Raises:
            SnapshotError: which snapshot is current cannot be told, or its
                metadata does not say completely what it records.
        """
        if not names:
            return []
        refusal = ("refusing to revert: the revert deletes the overlays the "
                   "current snapshot records, and a backup that does not "
                   "know them cannot cover them")
        result = self.virsh.execute("snapshot-current", vm_name, warn=True)
        if not result.ok:
            raise SnapshotError(
                f"cannot tell which snapshot of {vm_name} is current "
                f"({(result.stderr or '').strip()}); {refusal}")
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError as exc:
            raise SnapshotError(
                f"the metadata of the current snapshot of {vm_name} does "
                f"not parse ({exc}); {refusal}") from exc
        current = root.findtext("name")
        if current not in names:
            raise SnapshotError(
                f"libvirt gives '{current}' as the current snapshot of "
                f"{vm_name}, which is not one it listed; {refusal}")
        section = ("revertDisks" if root.find("revertDisks") is not None
                   else "disks")
        return self._overlays_from_snapshot_xml(root, current, vm_name,
                                                refusal, section=section)

    def _pause_for_backup(self, vm_name: str) -> bool:
        """
        Pause *vm_name* if it is running, so its overlays hold still while
        they are copied.

        The newest snapshot's overlay **is** the live disk — ``domblklist``
        names it — and reverting to an older snapshot deletes it, which is
        why it is in the backup set at all. Copying it while qemu writes
        produces a mix of blocks from different instants; for qcow2 that
        can be an inconsistent L1/L2 mapping rather than merely stale
        data, and the put-back afterwards would install exactly that as
        the newest snapshot's overlay.

        The guest stays paused until the revert, which sets the domain
        state itself from the snapshot's memory image. Every path that
        gives up before the revert resumes it.

        Args:
            vm_name: the domain about to have its overlays copied.

        Returns:
            bool: True if this call paused the domain — and so is the one
            that has to resume it. False if it was not running.

        Raises:
            SnapshotError: the domain is running and could not be paused.
        """
        # `domstate` prints a *translated* state name -- virsh runs it
        # through gettext, and nothing here fixes the locale -- so
        # comparing it against the English "running" reads a running guest
        # as idle under any other locale, which is precisely the unpaused
        # copy this exists to prevent. Domain names are not translated, so
        # ask which domains are running and look for this one.
        listed = self.virsh.execute(
            "list", "--state-running", "--name", warn=True)
        if not listed.ok:
            raise SnapshotError(
                f"cannot tell whether {vm_name} is running "
                f"({(listed.stderr or '').strip()}); refusing to copy its "
                f"overlays without knowing whether the guest is writing "
                f"to them")

        # Splitting on newline is unambiguous *here*, unlike the snapshot
        # list: libvirt refuses a newline in a domain name outright
        # ("invalid char in name"), where it accepts one in a snapshot
        # name. Spaces are allowed and are kept -- the names are compared
        # exactly, not stripped.
        running = [n for n in listed.stdout.split('\n') if n]
        if vm_name not in running:
            # paused and pmsuspended domains are not listed either, and
            # neither is writing -- which is the actual question
            return False

        result = self.virsh.execute("suspend", vm_name, warn=True)
        if not result.ok:
            raise SnapshotError(
                f"could not pause {vm_name} before copying its overlays "
                f"({(result.stderr or '').strip()}); refusing to copy the "
                f"live disk while the guest writes to it")
        self.logger.info(
            f"paused {vm_name} while its overlays are copied")
        return True

    def _report_interrupted_revert(self, vm_name: str, paused: bool,
                                   preserved: list[tuple]) -> None:
        """
        Say what a restore stopped during its revert leaves (#294): every
        backup of an overlay, kept, as the revert may have deleted the
        overlay -- libvirtd finishes a revert virsh has left -- and the
        backup is the only copy then; and the guest, perhaps paused.
        """
        if preserved:
            missing = [(o, b) for o, b in preserved if not self._is_regular_file(o)]
            self.logger.error(
                f"the restore of {vm_name} was stopped during its revert, "
                f"which libvirt may finish on its own. Every backup of its "
                f"overlays is kept, and the next restore refuses until they "
                f"are dealt with: once `virsh domstate {vm_name}` shows the "
                f"revert done, a backup whose overlay is missing is the only "
                f"copy of it and goes back in place"
                + ("" if not missing else " ("
                   + "; ".join(f"mv -fT {shlex.quote(b)} {shlex.quote(o)}"
                               for o, b in missing) + ")")
                + "; one whose overlay is there is a spare copy and can be "
                  "removed. Backups: "
                + ", ".join(f"{b} (of {o})" for o, b in preserved))
        self._report_pause_after_failed_revert(vm_name, paused)

    def _report_pause_after_failed_revert(self,
                                          vm_name: str,
                                          paused: bool) -> None:
        """
        Say that the guest may still be paused, instead of resuming it.

        Once ``snapshot-revert`` has been attempted, the paused domain is
        no longer necessarily the one this call paused. libvirt installs
        the restored domain's running/paused state *before* writing the
        snapshot metadata, and that write can fail — so a revert reported
        as failed can already have put a legitimately paused snapshot in
        place. Resuming on a failed result would silently start it.

        A failed command result cannot tell those two apart, so this says
        what it knows and leaves the decision where it belongs.

        Args:
            vm_name: the domain that was paused.
            paused: whether this call paused it.
        """
        if not paused:
            return
        self.logger.error(
            f"{vm_name} was paused before its overlays were copied, and the "
            f"revert did not succeed. It is deliberately not resumed here: a "
            f"revert can install the snapshot's own paused state before "
            f"failing, and resuming would override that. Check with `virsh "
            f"domstate {vm_name}`, and if that is still this restore's own "
            f"pause: virsh resume {vm_name}")

    def _resume_after_backup(self, vm_name: str, paused: bool) -> None:
        """
        Undo :meth:`_pause_for_backup` on a path that will not revert.

        Best-effort, but loud: a guest left paused because a restore
        refused is its own kind of failure, and the message says how to
        put it right.

        Args:
            vm_name: the domain to resume.
            paused: whether this call was the one that paused it.
        """
        if not paused:
            return
        try:
            result = self.virsh.execute("resume", vm_name, warn=True)
        except Exception as exc:
            # this runs while another error is on its way up; raising here
            # would replace it with this one
            self.logger.error(
                f"{vm_name} is still paused: the resume could not be run "
                f"after the restore gave up ({type(exc).__name__}: {exc}). "
                f"Resume it with: virsh resume {vm_name}")
            return
        if result.ok:
            self.logger.info(f"resumed {vm_name}")
            return
        self.logger.error(
            f"{vm_name} is still paused: it could not be resumed after the "
            f"restore gave up ({(result.stderr or '').strip()}). Resume it "
            f"with: virsh resume {vm_name}")

    def _preserve_snapshot_overlays(self,
                                    vm_name: str,
                                    snapshot_name: str,
                                    skip=()) -> list[tuple]:
        """
        Back up the overlay files that ``snapshot-revert`` to
        *snapshot_name* can delete, so they can be restored afterwards
        and every snapshot stays reachable: each a hard link of the
        overlay where one can be made (:meth:`_link_backups`), which writes
        no data, and a copy where not.

        Two sets. The overlays of the target and of every snapshot newer
        than it in the chain; older ones are not copied, which avoids
        full-copying the whole chain on every restore (restoring the latest
        snapshot copies one overlay per disk instead of one per disk per
        snapshot). And what the revert itself deletes, wherever it sits in
        the chain: the overlays the *current* snapshot records
        (:meth:`_overlays_the_revert_deletes`). That one is usually the
        target or newer, but after libvirt's own delete of the current
        snapshot has made its parent current, a revert to another child
        deletes the parent's overlay, which that child is built on (#287).

        The inventory it works from is the strict one, so "the target is
        not in the chain" now means the target is not a snapshot of this
        domain at all, rather than that its metadata could not be read.
        Preserving everything remains the answer for that case -- safe,
        slow, and about to fail at the revert anyway.

        Args:
            vm_name: the domain whose overlays are at risk.
            snapshot_name: the snapshot about to be reverted to.
            skip: overlays at risk that need no backup — a CDROM's, which
                holds nothing and which no revert opens, in a directory no
                backup can be reserved in (:meth:`_unbackable_cdrom_overlays`).

        Returns:
            list: ``(original_path, backup_path)`` pairs (:class:`_Backup`,
            which says whether the backup is a hard link). Empty means there
            was nothing at risk, and nothing else — the caller reverts on
            that answer, so a failed backup must not be able to produce it.

        Raises:
            SnapshotError: the backup could not be made, or could not be
                shown to be complete — the current snapshot's records
                included. Either way the revert is refused rather than run
                on a partial backup (#164 CL-D1).
        """
        all_overlays, order = self._strict_overlay_inventory(vm_name)

        # the target and any newer snapshots in the (oldest-first) chain
        if snapshot_name in order:
            at_risk = set(order[order.index(snapshot_name):])
        else:
            at_risk = set(all_overlays)

        # collect the overlay files referenced by the at-risk snapshots
        files_to_preserve: set = set()
        for snap, files in all_overlays.items():
            if snap in at_risk:
                files_to_preserve.update(files)
        # and what the revert deletes, though the current snapshot be older
        # than the target: after a delete made it current, its overlays are
        # what the target is built on (#287)
        files_to_preserve.update(
            self._overlays_the_revert_deletes(vm_name, order))
        # not even claimed: its .preserve would be written beside the ISO
        files_to_preserve.difference_update(skip)

        # claim across *every* at-risk overlay before dropping the ones
        # whose original is missing. A .preserve whose original is gone is
        # the one that matters most -- it is what an interrupted restore
        # leaves behind, and filtering first meant the next restore never
        # looked at it, reverted again, and reported success.
        candidates = sorted(files_to_preserve)
        # The scratch each copy lands on before being renamed over its
        # reservation is a real file in the overlay directory, so it is
        # claimed and reserved exactly like the backup itself. A generated
        # name would only be *probably* nobody's -- and cleanup removes
        # these, so "probably" is not the standard. Deterministic, too:
        # one left behind is detectable, and refuses the next restore
        # rather than being silently reused.
        self._claim_backup_paths(
            [(f, f + '.preserve') for f in candidates]
            + [(f, f + '.preserve.copy') for f in candidates],
            all_overlays)

        pairs = [(f, f + '.preserve') for f in candidates
                 if self._overlay_is_present(f)]
        if not pairs:
            return []

        linked = self._link_backups(vm_name, pairs)
        if linked is not None:
            return linked

        # the copy does not go straight to the reserved path. `cp` truncates
        # its destination before filling it, and a zero-length file at an
        # overlay path is one libvirt will adopt as the overlay it was about
        # to create -- the very thing the reservation exists to prevent. So
        # each copy lands on a scratch name and is renamed over the
        # reservation, which is atomic within a directory: the reserved path
        # is the marker one instant and the finished backup the next.
        scratch = {dst: f"{dst}.copy" for _src, dst in pairs}
        to_reserve = list(pairs) + [(src, scratch[dst]) for src, dst in pairs]
        signatures = self._reserve_backup_paths(to_reserve)
        owned = [path for _src, path in to_reserve]

        # where no hard link could be made: a copy, in one command → one
        # sudo prompt. --reflink=auto shares the extents on a CoW filesystem
        # instead of writing a second full copy beside the original, which is
        # the ENOSPC this backup used to die of (#164 CL-R2). Elsewhere it
        # falls back to the sparse copy `rsync -aW --sparse` was making.
        #
        # Every command carries its own prefix: execute_shell prefixes the
        # first word of the string it is handed, which in a chain reaches
        # the first command only.
        try:
            steps = []
            for src, dst in pairs:
                steps.append(f"cp --reflink=auto --sparse=always -p "
                             f"{shlex.quote(src)} {shlex.quote(scratch[dst])}")
                steps.append(f"mv -fT {shlex.quote(scratch[dst])} "
                             f"{shlex.quote(dst)}")
            cmd = " && ".join(self.virsh.sudo_prefix(c) + c for c in steps)
            result = self.virsh.execute_shell(cmd, warn=True)
        except Exception as exc:
            # execute_shell converts a command that *ran* and failed into a
            # result, but a runner that cannot start the command at all
            # still raises. Every path this call created is its own, and no
            # revert has happened yet, so none of them is anybody's last
            # copy of anything.
            self._reap_preserve_files(owned)
            raise SnapshotError(
                f"could not run the overlay backup for {vm_name} "
                f"({type(exc).__name__}: {exc}); refusing to revert without "
                f"it") from exc

        if result.ok:
            # cp and mv exit 0 in cases that leave no usable backup at the
            # recorded path, so ask the filesystem rather than the exit code
            unusable = [dst for _src, dst in pairs
                        if not self._backup_was_written(dst, signatures[dst])]
            if not unusable:
                for src, _dst in pairs:
                    self.logger.debug(f"preserved snapshot overlay: {src}")
                return [_Backup(src, dst) for src, dst in pairs]

            self._reap_preserve_files(owned)
            raise SnapshotError(
                f"the overlay backup of {vm_name} reported success, but "
                f"{', '.join(unusable)} is not a usable copy afterwards. "
                f"Refusing to revert: a backup that cannot be read back is "
                f"not a backup.")

        # The steps are one && chain, so a failure part-way through leaves
        # the earlier backups -- and one half-written one -- behind with
        # nothing to reap them (#164 CL-D2). Every one of these paths was
        # created by this call, which is what makes removing them safe.
        self._reap_preserve_files(owned)

        raise SnapshotError(
            f"could not back up the snapshot overlays of {vm_name} before "
            f"reverting to '{snapshot_name}': "
            f"{(result.stderr or '').strip() or 'the copy failed'}. "
            f"Refusing to revert — snapshot-revert deletes those overlays, "
            f"so without the backup every snapshot from '{snapshot_name}' "
            f"onwards would be gone.")

    def _link_backups(self, vm_name: str,
                      pairs: list[tuple]) -> list[_Backup] | None:
        """
        Back up each original of *pairs* -- ``(original, backup path)``,
        every backup path claimed (:meth:`_claim_backup_paths`) -- as a hard
        link of it at its backup path, in one command; the pairs, or None
        when the links could not all be made, any this made removed again.
        The caller copies then.

        A hard link writes no data, where a copy writes everything the guest
        wrote since the last restore, with the guest paused (ext4 has no
        reflinks): ~1.2 s per GiB measured. It keeps the overlay as it is
        only because nothing writes into an overlay once it is backed up:
        the guest is paused for the backup (:meth:`_pause_for_backup`) and
        stays paused up to the revert; the revert builds new overlays and
        unlinks the ones it discards rather than writing into them
        (``qemuSnapshotRevertExternalFinish``, measured on libvirt 10.10);
        a revert that fails stops before that; the CDROM steps after it
        change which image a drive reads, never an image; and the put-back
        renames the backup over the original's name. A revert that wrote
        into an overlay it keeps would write into the original as much, and
        no backup is needed for one the revert keeps.

        ``ln`` creates the link only where nothing is (``link(2)``), so the
        backup path needs no reservation: the file appears there whole, or
        not at all. What it cannot link -- across devices, a file the user
        may not link under ``fs.protected_hardlinks``, a filesystem without
        hard links -- fails the command. A backup path holding a link of its
        original is this call's; nothing else is touched.

        A directory the user may not write to takes no link either, though a
        ``sudo ln`` could make one: removing a backup the revert left
        redundant takes no ``sudo`` (:meth:`_reap_preserve_files`), and one
        left there refuses the next restore. The copy refuses such a
        directory up front, at its reservation, as it always has.
        """
        identities: dict[str, tuple[int, int]] = {}
        for src, dst in pairs:
            identity = identity_of(src)
            if identity is None or not os.access(os.path.dirname(dst),
                                                 os.W_OK | os.X_OK):
                return None
            identities[src] = identity
        steps = [f"ln -T -- {shlex.quote(src)} {shlex.quote(dst)}"
                 for src, dst in pairs]
        cmd = " && ".join(self.virsh.sudo_prefix(c) + c for c in steps)
        try:
            result = self.virsh.execute_shell(cmd, warn=True)
            why = (result.stderr or "").strip() if not result.ok else ""
        except Exception as exc:
            result, why = None, f"{type(exc).__name__}: {exc}"
        made = [dst for src, dst in pairs
                if self._backup_was_written(dst, None, linked=identities[src])]
        if result is not None and result.ok and len(made) == len(pairs):
            for src, _dst in pairs:
                self.logger.debug(f"preserved snapshot overlay: {src} "
                                  f"(a hard link)")
            return [_Backup(src, dst, identities[src]) for src, dst in pairs]
        self._reap_preserve_files(made)
        self.logger.debug(
            f"{vm_name}: the overlays at risk could not all be backed up by "
            f"hard links ({why or 'a link is not where it was made'}); "
            f"copying them")
        return None

    @staticmethod
    def _backup_was_written(dst: str, reserved: tuple | None,
                            linked: tuple[int, int] | None = None) -> bool:
        """
        Whether the copy actually replaced this call's reservation -- or,
        with *linked*, whether *dst* is a hard link of the original, whose
        identity that is.

        The reservation is deliberately non-empty, so neither "is it a
        regular file" nor "is it larger than zero" tells a real backup from
        an untouched placeholder — and ``cp`` can exit 0 having written
        nothing. A hard link is told by its identity alone: it is the
        original's inode, the same size and mtime, and was never a
        reservation.

        The question is asked of the inode rather than of the contents.
        Reading the file would need permission the copy does not: a
        ``sudo cp -p`` of a root-owned ``0600`` overlay hands back a
        root-owned ``0600`` backup, and an unprivileged read of it fails —
        which would condemn a perfectly good backup. ``lstat`` needs only
        the directory, which this code already requires for the reservation
        and the cleanup.

        Args:
            dst: the backup path to inspect.
            reserved: the ``(inode, size, mtime_ns)`` the reservation had.
            linked: the original's ``(st_dev, st_ino)``, for a hard link.

        Returns:
            bool: True when the file at *dst* is a regular file that is no
            longer the reservation, or is the original's inode. A path that
            cannot be stat'ed counts as not written.
        """
        try:
            now = os.lstat(dst)
        except OSError:
            return False
        if not stat.S_ISREG(now.st_mode):
            return False
        if linked is not None:
            return (now.st_dev, now.st_ino) == linked
        return (now.st_ino, now.st_size, now.st_mtime_ns) != reserved

    @staticmethod
    def _is_regular_file(path: str) -> bool:
        """
        Whether *path* is a regular file, with "cannot tell" counting as no.

        Used to check a backup that has just been written, where the
        cautious answer is that an unverifiable file is not a backup.

        Args:
            path: the path to classify.

        Returns:
            bool: True only for a regular file that could be stat'ed.
        """
        try:
            return stat.S_ISREG(os.lstat(path).st_mode)
        except OSError:
            return False

    def _overlay_is_present(self, path: str) -> bool:
        """
        Whether *path* is an overlay this can back up — or a refusal to
        guess.

        ``os.path.isfile`` folds three different answers into two. It says
        False both for "it is not there" and for "I cannot tell" (an
        unreadable parent directory, say): taking the second as the first
        drops that overlay from the backup set, and the revert then deletes
        a file nothing copied, which is CL-D1 one level down. And it says
        True for a symlink to a regular file, which ``cp`` would follow --
        copying the referent under the link's name, so that the put-back
        afterwards replaces the link with a plain file.

        ``lstat``, then, and only a regular file will do: a missing path is
        genuinely absent, and anything else is refused rather than guessed.

        Args:
            path: an overlay file named by a snapshot's XML.

        Returns:
            True for a regular file; False when it genuinely is not there.

        Raises:
            SnapshotError: the path could not be classified, or is not a
                regular file.
        """
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SnapshotError(
                f"cannot tell whether the snapshot overlay {path} is there "
                f"({exc.strerror}); refusing to revert, because an overlay "
                f"left out of the backup is one the revert can delete for "
                f"good") from exc

        if stat.S_ISREG(mode):
            return True
        raise SnapshotError(
            f"the snapshot overlay {path} is not a regular file; refusing "
            f"to revert rather than copy something whose backup would not "
            f"be the same kind of thing")

    def _claim_backup_paths(self,
                            pairs: list[tuple],
                            all_overlays: dict[str, list[str]]) -> None:
        """
        Establish that every backup path is this call's to write, and
        therefore its to reap.

        ``.preserve`` is not a reserved namespace. Snapshot names are
        free-form, so a snapshot may perfectly well be called
        ``<other>.preserve``, and libvirt then names its overlay exactly
        what this backup wants for its scratch copy: the copy overwrites a
        live overlay, and the cleanup afterwards deletes it outright. The
        same goes for anything else already sitting at that path — ``cp``
        writes *through* an existing symlink or hard link, so the bytes land
        in whatever it points at, and it drops the copy *inside* a directory
        of that name while still exiting 0.

        So nothing is guessed. A backup path must name the overlay of no
        snapshot, and must not exist at all. What survives both checks is a
        path this call creates, which is the only kind the failure path is
        allowed to remove.

        Between the check and the copy is a window, but not a shared one:
        these paths are derived from overlay names and written by this
        function alone.

        Args:
            pairs: the ``(original, backup)`` pairs about to be copied.
            all_overlays: every snapshot's overlays, from the strict
                inventory — complete by construction, which is what makes
                the first check worth anything.

        Raises:
            SnapshotError: a backup path names a live overlay, already
                exists, or cannot be classified.
        """
        declared = {f for files in all_overlays.values() for f in files}
        collisions = sorted({dst for _src, dst in pairs} & declared)
        if collisions:
            raise SnapshotError(
                f"the backup path(s) {', '.join(collisions)} are overlays of "
                f"a snapshot of this domain — a snapshot named "
                f"'<something>.preserve' collides with how backups are "
                f"named here. Refusing to revert: writing there would "
                f"overwrite a live overlay. Rename or delete that snapshot.")

        for src, dst in pairs:
            try:
                os.lstat(dst)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SnapshotError(
                    f"cannot tell what is at the backup path {dst} "
                    f"({exc.strerror}); refusing to revert rather than write "
                    f"over something unexamined") from exc

            # never "remove it": a leftover .preserve can be the only copy
            # of an overlay the interrupted revert deleted, and one whose
            # overlay is there can still be: another file may have taken
            # the overlay's name since (prune calls that one unknown)
            raise SnapshotError(
                f"the backup path {dst} already exists. Refusing to revert: "
                f"a copy there would be written through a symlink or a hard "
                f"link, or land inside a directory, and the cleanup would "
                f"then delete something this restore did not create. It is "
                f"most likely left over from an interrupted restore — check "
                f"it: if {src} is missing, it is the only copy of it, and "
                f"goes back there (mv -fT {shlex.quote(dst)} "
                f"{shlex.quote(src)}); if {src} is there, run `boxman "
                f"snapshot prune --dry-run`: it says whether this is a "
                f"redundant copy or the only one.")

    def _reserve_backup_paths(self, pairs: list[tuple]) -> dict[str, tuple]:
        """
        Create each backup path exclusively, so it is this call's file.

        Checking that a path is free and then writing to it is not the same
        as owning it. Between the two, another restore of the same domain
        can take it, and so can a ``snapshot take``: a snapshot named
        ``<other>.preserve`` makes libvirt create exactly the path this
        backup wants. Either way the copy writes through somebody else's
        file and the cleanup afterwards deletes it.

        ``O_EXCL`` closes that. The file is created empty and the ``cp``
        below fills it; whoever loses the race is told the path is taken
        and refuses. It also means every path handed to
        :meth:`_reap_preserve_files` was made here, which is what makes
        reaping safe.

        Creating a file needs write permission on the parent directory --
        the same permission the cleanup ``rm`` already needs, and which it
        already assumes (see
        :meth:`VirshCommand._should_use_sudo_for_command`).

        Args:
            pairs: the ``(original, backup)`` pairs about to be copied.

        Returns:
            dict: ``{backup_path: signature}`` — what each reservation
            looked like on disk, for :meth:`_backup_was_written` to
            compare against afterwards.

        Raises:
            SnapshotError: a path could not be reserved. Reservations
                already made in this call are removed first.
        """
        signatures: dict[str, tuple] = {}
        for _src, dst in pairs:
            try:
                signatures[dst] = self._reserve_one_path(dst)
            except OSError as exc:
                # only this call's own reservations, and only the ones it
                # got as far as making
                self._reap_preserve_files(list(signatures))
                raise SnapshotError(
                    f"could not reserve the backup path {dst} "
                    f"({exc.strerror}); refusing to revert rather than copy "
                    f"to a path something else may already own") from exc
        return signatures

    def _reserve_one_path(self, dst: str) -> tuple:
        """
        Make *dst* appear, atomically, as a non-empty file nobody else owns.

        Two properties are needed at once, and ``O_EXCL`` alone gives only
        the first. The file has to appear in one step, so that two racers
        cannot both believe they created it; and it has to be **non-empty**
        the instant it appears, because libvirt adopts a zero-length file
        at an overlay path as the overlay it was about to create, and
        refuses only one that already has content. An ``O_EXCL`` create
        followed by a write is empty for the moment in between, which is
        exactly the state that invites the adoption.

        So the marker is written to a private temporary file first, and
        ``link()`` puts it at *dst* in a single step that fails if anything
        is already there.

        Args:
            dst: the backup path to reserve.

        Returns:
            tuple: the reservation's ``(inode, size, mtime_ns)``, which is
            what :meth:`_backup_was_written` later compares against.

        Raises:
            OSError: the path is taken, or could not be created.
        """
        tmp = f"{dst}.{os.getpid()}.{binascii.hexlify(os.urandom(6)).decode()}"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            try:
                # all of it, or the marker is truncated -- and a truncated
                # marker is one _backup_was_written would not recognise,
                # so an untouched reservation would pass for a backup
                marker = self._RESERVATION_MARKER
                written = 0
                while written < len(marker):
                    written += os.write(fd, marker[written:])
            finally:
                os.close(fd)
            # stat the scaffold, not the published name: link() shares
            # this inode, so the signature is the same either way, and
            # taking it here keeps the path un-published until the caller
            # is holding everything it needs to clean up
            info = os.stat(tmp)
            os.link(tmp, dst)
        finally:
            # scaffolding: gone whether the link landed, failed, or was
            # never reached. Only the caller's own reservations are tracked
            # for cleanup, and this name was never one of them.
            try:
                os.unlink(tmp)
            except OSError as exc:
                self.logger.warning(
                    f"could not remove the reservation scaffold {tmp} "
                    f"({exc.strerror}); remove it by hand")

        return (info.st_ino, info.st_size, info.st_mtime_ns)

    def _reap_preserve_files(self, backups: list[str]) -> None:
        """
        Remove ``.preserve`` copies that this call created and no longer
        wants.

        One ``rm -f`` taking every path rather than an ``&&`` chain: cleanup
        must not stop at the first path that resists, and a path already
        gone is not an error. The privilege is whatever the sudo policy
        gives ``rm``, which by default is none — unlinking needs write
        permission on the parent directory, not root (see
        :meth:`VirshCommand._should_use_sudo_for_command`).

        Best-effort: a failure names the paths, so that whatever is left
        behind can be removed by hand.

        Args:
            backups: backup paths to unlink; an empty list is a no-op.
        """
        if not backups:
            return
        cmd = "rm -f " + " ".join(shlex.quote(b) for b in backups)
        try:
            result = self.virsh.execute_shell(cmd, warn=True)
        except Exception as exc:
            # this is the function that promises to name what it left
            # behind, so it must not be the one that dies quietly
            self.logger.warning(
                f"could not run the overlay backup cleanup "
                f"({type(exc).__name__}: {exc}); {self._unreaped(backups)}")
            return
        if not result.ok:
            self.logger.warning(
                f"could not remove the overlay backups "
                f"({(result.stderr or '').strip()}); "
                f"{self._unreaped(backups)}")

    @staticmethod
    def _unreaped(backups: list[str]) -> str:
        """
        What to do with those of *backups* -- ``.preserve`` files a restore
        made and no longer needs -- that its cleanup left. ``boxman snapshot
        prune`` removes a spare copy or an unfilled reservation once it has
        told it is one; a second name of its overlay, a hard link, it keeps
        (removing it frees nothing), and those go by hand.
        """
        links, others = [], []
        for path in backups:
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError:
                others.append(path)
                continue
            (links if stat.S_ISREG(st.st_mode) and st.st_nlink > 1
             else others).append(path)
        said = []
        if others:
            said.append(f"`boxman snapshot prune` removes each of these "
                        f"that is a spare copy or an unfilled reservation: "
                        f"{', '.join(others)}")
        if links:
            said.append(f"these are second names of their overlays (hard "
                        f"links), which prune keeps: remove them by hand: "
                        f"{', '.join(links)}")
        return "; ".join(said) or "none of them is left"

    def _name_overlays_no_snapshot_records(self, vm_name: str,
                                           originals: list[str]) -> None:
        """
        Name in a warning each of *originals*, overlays a revert of
        *vm_name* that succeeded deleted and that were put back, that no
        snapshot of it records any more.

        Reverting to a snapshot with no children, libvirt deletes its
        overlays and records the new ones it made in their place
        (``qemuSnapshotRevertExternalFinish``). Put back, since whether
        another domain needs one cannot be told while it is gone, the old
        ones are recorded by nothing, and nothing removes them. When the
        snapshots cannot be read, nothing is said.
        """
        if not originals:
            return
        try:
            recorded, _order = self._strict_overlay_inventory(vm_name)
        except Exception:
            return
        names = {path for files in recorded.values() for path in files}
        names |= {os.path.realpath(path) for path in names}
        unrecorded = [original for original in originals
                      if original not in names
                      and os.path.realpath(original) not in names]
        if unrecorded:
            # a rename sets ctime: prune counts them from now, --min-age
            self.logger.warning(
                f"{vm_name}: the revert recorded new overlays in place of "
                f"{', '.join(unrecorded)}, put back: no snapshot of it "
                f"records them any more. `boxman snapshot prune` removes "
                f"them once nothing uses them, an hour from now (or sooner "
                f"with --min-age).")

    def _restore_preserved_overlays(
            self, preserved: list[tuple],
            vm_name: str | None = None) -> list[tuple]:
        """
        Put back the overlay files the revert deleted, and reap the backups
        that turned out not to be needed.

        With *vm_name*, the revert of it succeeded: each one put back that no
        snapshot records any more is named
        (:meth:`_name_overlays_no_snapshot_records`).

        Args:
            preserved: ``(original_path, backup_path)`` pairs as returned by
                :meth:`_preserve_snapshot_overlays`.
            vm_name: the domain reverted, when the revert succeeded.

        Returns:
            list: the pairs still waiting to be put back, empty when every
            overlay is where it belongs. A non-empty list means the
            snapshots that need them are unreachable until someone moves
            them into place.
        """
        if not preserved:
            return []

        to_restore = [(o, b) for o, b in preserved
                      if not self._is_regular_file(o)
                      and self._is_regular_file(b)]

        if to_restore:
            # a rename, not a copy: the backup sits beside the original, so
            # this is the same directory by construction and moves no data.
            # That matters here of all places — the overlay is gone and its
            # backup is the only copy left, so a recovery that has to find
            # room for a second copy is one that fails when it is needed.
            # -T: treat the destination as a plain name. Without it, a
            # directory sitting at the original path swallows the backup --
            # `mv` exits 0, the overlay is still missing, and the recovery
            # instructions then point at a file that has been moved inside
            # that directory.
            moves = [f"mv -fT {shlex.quote(b)} {shlex.quote(o)}"
                     for o, b in to_restore]
            cmd = " && ".join(self.virsh.sudo_prefix(m) + m for m in moves)
            # a runner that cannot start the command raises rather than
            # returning a failed result, and escaping here would skip the
            # postcondition below -- so the caller would never learn that
            # an overlay is still sitting in its backup
            try:
                result = self.virsh.execute_shell(cmd, warn=True)
            except Exception as exc:
                self.logger.warning(
                    f"could not run the put-back "
                    f"({type(exc).__name__}: {exc})")
                result = None
            if result is not None and result.ok:
                for o, _b in to_restore:
                    self.logger.info(
                        f"restored overlay deleted by revert: {o}")
            elif result is not None:
                self.logger.warning(
                    f"a move failed while putting the overlays back "
                    f"({(result.stderr or '').strip()})")

        # The exit code is not the question. `mv -f` into a *directory* at
        # the original path succeeds and leaves the backup sitting inside
        # it; a pair whose original and backup have both gone never entered
        # to_restore at all. Only where each overlay actually ended up says
        # whether this worked, so every pair is judged on that -- and a
        # retry then has just the stragglers to move.
        unrecovered: list[tuple] = []
        redundant: list[str] = []
        replaced: list[tuple] = []
        for pair in preserved:
            original, backup = pair
            linked = getattr(pair, "linked", None)
            if self._is_regular_file(original):
                # the overlay is where it belongs; its backup, if one is
                # still lying about, is a spare copy and safe to drop -- a
                # hard link, only while the original is the file it links:
                # a name taken by another file since is no proof the
                # backed-up one is safe
                if not self._is_regular_file(backup):
                    continue
                if linked is None or identity_of(original) == linked:
                    redundant.append(backup)
                else:
                    replaced.append((original, backup))
            else:
                unrecovered.append((original, backup))

        if replaced:
            self.logger.error(
                f"another file has taken the name of each overlay below since "
                f"it was backed up, so its backup is kept: check which of the "
                f"two the snapshots need: "
                f"{', '.join(f'{o} (backed up as {b})' for o, b in replaced)}")

        if unrecovered:
            # deliberately no reap for these: their .preserve file may be
            # the only copy of that overlay left in existence, and removing
            # it is the loss this function exists to prevent
            self.logger.error(
                f"the overlays below are not back in place after the "
                f"revert, and the snapshots that need them stay unreachable "
                f"until they are moved there by hand: "
                f"{', '.join(f'{b} -> {o}' for o, b in unrecovered)}")

        if vm_name is not None:
            self._name_overlays_no_snapshot_records(
                vm_name, [o for o, _b in to_restore
                          if self._is_regular_file(o)])
        self._reap_preserve_files(redundant)
        return unrecovered

    def _finish_restore(self,
                        preserved: list[tuple],
                        mem_path: str | None,
                        decompressed_for_revert: bool,
                        vm_name: str | None = None) -> None:
        """
        Close out a revert: put the overlays back, re-compress the memory
        file, and refuse to call the result a success if an overlay is
        still sitting in its backup.

        Args:
            preserved: pairs from :meth:`_preserve_snapshot_overlays`.
            mem_path: the snapshot's memory file, if it has one.
            decompressed_for_revert: whether this call decompressed it.
            vm_name: the domain reverted, when the revert succeeded (see
                :meth:`_restore_preserved_overlays`).

        Raises:
            SnapshotRecoveryError: an overlay the revert deleted could not
                be put back. Terminal by design — the revert has already
                run, so retrying the restore would run it a second time.
        """
        unrecovered = self._restore_preserved_overlays(preserved,
                                                       vm_name=vm_name)

        # re-compressing the memory image is housekeeping. It runs after
        # the overlays have been judged, and inside its own guard, so that
        # it cannot throw away the one thing the caller has to be told.
        try:
            self._recompress_after_revert(mem_path, decompressed_for_revert)
        except Exception as exc:
            self.logger.warning(
                f"could not re-compress the memory image of {mem_path} "
                f"({type(exc).__name__}: {exc})")

        if not unrecovered:
            return

        raise SnapshotRecoveryError(
            f"reverted, but {len(unrecovered)} overlay(s) the revert deleted "
            f"could not be put back, and the snapshots that need them are "
            f"unreachable until they are. Finish by hand: "
            # -T, like the automatic move: without it, following this
            # instruction while a directory sits at the original path puts
            # the last copy inside that directory and reports success
            + "; ".join(f"mv -fT {shlex.quote(b)} {shlex.quote(o)}"
                        for o, b in unrecovered)
            + ". Clear whatever occupies the destination first if that "
              "move refuses.")

    def snapshot_restore(self, vm_name: str, snapshot_name: str,
                         paths_in_use=None, after_media=None,
                         files_in_use=None) -> bool:
        """
        Revert a VM to a specific snapshot.

        Before reverting, the overlay files that ``snapshot-revert`` can
        delete are backed up — the target snapshot's overlays and any newer
        ones in the chain, and the ones the current snapshot records, which
        are what the revert deletes, though it be older than the target
        (see :meth:`_preserve_snapshot_overlays`, which falls back to
        backing up every overlay if the target isn't found in the chain).
        Each is a hard link where one can be made, and a copy where not
        (:meth:`_link_backups`). After the revert any overlays libvirt
        deleted are restored so that every snapshot remains reachable.

        That backup is a precondition, not a courtesy. If it cannot be made,
        :meth:`_preserve_snapshot_overlays` raises and no revert is
        attempted: reverting without it destroys every snapshot from the
        target onwards, and the old code could not tell a failed backup
        apart from "there was nothing to back up" (#164 CL-D1).

        Each backup path is taken exclusively rather than merely checked --
        a hard link is made only where nothing is, and a copy's path is
        *reserved* first -- so a second restore -- or a ``snapshot take``
        that happens to name its overlay the same thing -- cannot land on a
        path this call is about to write, and the cleanup only ever removes
        files this call created.

        Compressed memory: if the snapshot's ``.raw`` memory file is missing
        but a ``.raw.zst`` sibling exists (created by ``snapshot take
        --compress-memory`` or ``storage compress-snapshots``), it is
        decompressed in-place before the revert and re-compressed afterwards
        so the next revert remains compressed.

        Retries up to 3 times on write-lock contention errors, which can
        occur transiently when multiple snapshot-reverts run in parallel and
        QEMU races to acquire an exclusive lock while creating the new disk
        overlay for the post-revert state.

        One revert at a time of the VMs whose disks read one image, the next
        a second later (:mod:`.revert_locks`, #303): libvirt names the
        overlay a revert builds of a disk after the image and the time in
        whole seconds, so two VMs booted from one ISO reverted in the same
        second were given one overlay of it. The turn is taken before the
        guest is paused, so a guest never waits paused for another's revert
        -- only, up to two seconds, for the second after it -- and is held
        until the last revert attempt returns.

        CDROMs: the revert builds a qcow2 overlay of each one's ISO beside
        the ISO, although the snapshot left them out, and moves the CDROM
        onto it. After a revert that worked, each is put back on its ISO and
        no snapshot record names those overlays any more; they are recorded
        for removal by the VM's next take or its teardown, which ask whether
        anything uses them (:meth:`_put_cdroms_back_on_their_isos`, #273).
        One the CDROMs were on already is backed up like any other overlay
        at risk, a hard link (:meth:`_unbackable_cdrom_overlays`).

        Args:
            vm_name: Name of the vm
            snapshot_name: Name of the snapshot to revert to
            paths_in_use: what every domain uses
                (``LibVirtSession.disk_paths_in_use``), asked only before a
                CDROM overlay the backup cannot keep is left to the revert.
                Without it, nor *files_in_use*, the CDROMs are left as they
                are.
            files_in_use: which of some files a domain uses
                (``LibVirtSession.files_in_use``), asked only before a CDROM
                overlay that cannot be recorded for later removal is removed
                at once; from *paths_in_use* when not given.
            after_media: called once the CDROMs are back on their ISOs, after
                a revert that worked: the guest's clock is set there
                (``LibVirtSession._set_clock_after_revert``), with nothing
                slow before it. A failure in it is warned about.

        Returns:
            bool: True if successful, False otherwise

        Raises:
            SnapshotError: the overlay backup could not be made or
                reserved, or the revert lock could not be taken, so no
                revert was attempted.
            SnapshotRecoveryError: the revert ran, but an overlay it deleted
                could not be put back. Never retry this one — the retry
                would revert a second time.
        """
        # read before the pause: the guest holds still for the copy alone.
        # Without a usage check its CDROMs are left as they are
        checked = paths_in_use is not None or files_in_use is not None
        before: list[tuple[list[str], str]] = []
        if checked:
            try:
                before = self._cdrom_overlay_chains(vm_name)
            except Exception as exc:
                self.logger.debug(f"{vm_name}: the overlays its CDROMs are "
                                  f"on could not be told "
                                  f"({type(exc).__name__}: {exc})")
        skip = self._unbackable_cdrom_overlays(vm_name, before, paths_in_use)

        # The revert's turn among the VMs whose disks read the same images
        # (#303), taken before the guest is paused: while it waits for
        # another VM's revert, this guest runs on. It is held across the
        # copy too, which makes the turn a little longer and never less
        # safe, and released -- stamped -- however what follows ends. The
        # wait for the second the last holder stamped comes just before the
        # revert (lock.reverting): up to two seconds, the guest paused,
        # the copy done meanwhile.
        keys = self._revert_lock_keys(vm_name, snapshot_name)
        lock = self._take_revert_lock(vm_name, keys)
        try:
            # the overlays have to hold still while they are copied, and
            # the newest one is the disk the guest is writing to (#195)
            paused = self._pause_for_backup(vm_name)
            try:
                preserved = self._preserve_snapshot_overlays(
                    vm_name, snapshot_name, skip=skip)
            except Exception:
                self._resume_after_backup(vm_name, paused)
                raise

            # Everything from here to the revert still owns those backups:
            # a raise in the memory handling would otherwise leave them
            # behind, and the next restore refuses on them. Nothing has
            # been reverted yet, so every original is still in place and
            # the backups are redundant.
            mem_path, decompressed_for_revert = None, False
            try:
                # Just-in-time decompress if the memory file was zstd'd.
                mem_path = self._memory_path_from_xml(vm_name, snapshot_name)
                if mem_path and not os.path.isfile(mem_path) and os.path.isfile(f"{mem_path}.zst"):
                    self.logger.info(
                        f"memory file is compressed; decompressing before revert: "
                        f"{mem_path}.zst")
                    if self.decompress_memory_file(mem_path, keep_zst=True):
                        decompressed_for_revert = True
                    else:
                        self.logger.error(
                            f"could not decompress memory for {vm_name}/{snapshot_name}; "
                            f"revert will likely fail")
            except Exception as exc:
                self._reap_preserve_files([b for _o, b in preserved])
                self._resume_after_backup(vm_name, paused)
                self._undo_decompression(mem_path, decompressed_for_revert)
                raise SnapshotError(
                    f"could not prepare {vm_name} for the revert to "
                    f"'{snapshot_name}' ({type(exc).__name__}: {exc}); "
                    f"refusing to revert") from exc

            ledger = self._ledger()
            intended = False
            max_retries = 3
            last_stderr = ''
            success = False
            reverting = False

            for attempt in range(1, max_retries + 1):
                try:
                    if not intended:
                        # what the CDROMs are on now, recorded for later
                        # removal before the revert leaves it unused. Kept
                        # whatever the revert does: one that fails can have
                        # moved them already, and the removal keeps what a
                        # domain still uses
                        intended = self._record_pending(vm_name, ledger,
                                                        before)
                    lock.reverting()
                    reverting = True
                    result = self.virsh.execute(
                        "snapshot-revert", vm_name, snapshot_name, warn=True)
                except KeyboardInterrupt:
                    # stopped (#294). Before the revert, nothing has changed
                    # but the copies and the pause, and both are undone. Once
                    # it is under way, libvirtd may finish it on its own after
                    # virsh has gone: which overlays it deleted is not known
                    # yet, so every backup stays, and the user is told how to
                    # tell the copies still needed from the spare ones
                    if reverting:
                        self._report_interrupted_revert(vm_name, paused,
                                                        preserved)
                    else:
                        self._reap_preserve_files([b for _o, b in preserved])
                        self._resume_after_backup(vm_name, paused)
                        self._undo_decompression(mem_path,
                                                 decompressed_for_revert)
                    raise
                except Exception as exc:
                    self.logger.error(
                        f"error reverting vm {vm_name} to snapshot '{snapshot_name}': {exc}")
                    self._report_pause_after_failed_revert(vm_name, paused)
                    self._finish_restore(
                        preserved, mem_path, decompressed_for_revert)
                    return False

                if result.ok:
                    self.logger.info(f"vm {vm_name} reverted to snapshot '{snapshot_name}'")
                    success = True
                    break

                last_stderr = result.stderr or ''
                if 'write' in last_stderr and 'lock' in last_stderr and attempt < max_retries:
                    self.logger.warning(
                        f"write lock contention reverting {vm_name}, "
                        f"retrying in 2s (attempt {attempt}/{max_retries})")
                    time.sleep(2)
                    continue
                break
        finally:
            lock.__exit__(None, None, None)

        if not success:
            self._report_pause_after_failed_revert(vm_name, paused)
        # only a revert that succeeded tells what it discarded
        self._finish_restore(preserved, mem_path, decompressed_for_revert,
                             vm_name=vm_name if success else None)

        if success:
            # a successful revert sets the domain state itself, from the
            # snapshot's memory image -- resuming here would fight it.
            # What it did to the CDROMs is undone after the overlays are
            # back: a failure there is housekeeping, not a failed restore.
            try:
                if checked:
                    self._put_cdroms_back_on_their_isos(
                        vm_name,
                        files_in_use=self._asking(files_in_use, paths_in_use),
                        also=before, defer=True)
            except Exception as exc:
                self.logger.warning(
                    f"{vm_name}: its CDROMs could not be put back on their "
                    f"ISOs after the revert ({type(exc).__name__}: {exc})")
            # right after the media change, which pauses the guest: nothing
            # slow is left before it
            if after_media is not None:
                try:
                    after_media()
                except Exception as exc:
                    self.logger.warning(
                        f"{vm_name}: what follows the CDROMs' put-back failed "
                        f"({type(exc).__name__}: {exc})")
            return True

        self.logger.error(
            f"failed to revert vm {vm_name} to snapshot '{snapshot_name}': {last_stderr}")
        return False

    def _undo_decompression(self, mem_path: str | None,
                            decompressed: bool) -> None:
        """:meth:`_recompress_after_revert` on a path that refuses the
        revert: what it cannot do is warned about, never raised over the
        refusal."""
        try:
            self._recompress_after_revert(mem_path, decompressed)
        except Exception as exc:
            self.logger.warning(
                f"could not remove the memory image decompressed for the "
                f"revert, {mem_path} ({type(exc).__name__}: {exc})")

    def _revert_lock_keys(self, vm_name: str,
                          snapshot_name: str) -> dict[str, str]:
        """
        ``{key: image}`` for each overlay name (:func:`overlay_key`) a
        revert of *vm_name* to *snapshot_name* builds that another VM's
        revert can build too: of each CDROM or floppy of the snapshot's
        saved definitions with media, and of each disk there that is
        read-only or shareable. libvirt builds the revert's overlays on what
        those definitions name (``qemuSnapshotRevertExternalPrepare``), not
        on what the domain reads now. A VM's own disks give names of its
        own, and take no lock.

        Read as the backup's strict inventory reads it, secrets included,
        so that the restore's read cache answers that one from this.

        Raises:
            SnapshotError: the snapshot's XML cannot be read: whether the
                revert may collide cannot be told.
        """
        refusal = ("refusing to revert without knowing which images its "
                   "overlays are named after (#303)")
        result = self.virsh.execute("snapshot-dumpxml", vm_name,
                                    snapshot_name, "--security-info",
                                    warn=True)
        if not result.ok:
            raise SnapshotError(
                f"cannot read snapshot '{snapshot_name}' of {vm_name} "
                f"({(result.stderr or '').strip()}); {refusal}")
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError as exc:
            raise SnapshotError(
                f"the metadata of snapshot '{snapshot_name}' of {vm_name} "
                f"does not parse ({exc}); {refusal}") from exc
        keys: dict[str, str] = {}
        for section in root:
            if section.tag not in ("domain", "inactiveDomain"):
                continue
            for disk in section.iter("disk"):
                if (disk.get("device") not in ("cdrom", "floppy")
                        and disk.find("readonly") is None
                        and disk.find("shareable") is None):
                    continue
                source = disk.find("source")
                path = source.get("file") if source is not None else None
                if path:
                    keys.setdefault(overlay_key(path), path)
        return keys

    def _revert_lock(self, vm_name: str, keys: dict[str, str]) -> RevertLock:
        """The :class:`RevertLock` of *keys*, saying when it waits."""
        def waiting(key: str) -> None:
            self.logger.info(
                f"{vm_name}: waiting for the revert of another VM whose disk "
                f"reads {keys[key]}: reverted in the same second, the two "
                f"would be given one overlay of it (#303)")
        return RevertLock(keys, on_wait=waiting)

    def _take_revert_lock(self, vm_name: str,
                          keys: dict[str, str]) -> RevertLock:
        """
        The :class:`RevertLock` of *keys*, held: taken, and the second the
        last holder stamped waited for. Asked before the guest is paused,
        nothing is to undo when it cannot be taken.

        Raises:
            SnapshotError: a lock stayed held elsewhere past its timeout, or
                a lock file could not be opened.
        """
        lock = self._revert_lock(vm_name, keys)
        try:
            lock.__enter__()
        except (RevertLockTimeoutError, OSError) as exc:
            raise SnapshotError(
                f"could not take the revert lock of {vm_name} ({exc}); "
                f"refusing to revert while a VM whose disk reads the same "
                f"image may revert in the same second (#303)") from exc
        return lock

    def _recompress_after_revert(self,
                                 mem_path: str | None,
                                 decompressed_for_revert: bool) -> None:
        """
        Re-compress the memory ``.raw`` file (and remove it) if we
        decompressed it for this revert. Best-effort — failures are
        warnings, not errors.
        """
        if not (decompressed_for_revert and mem_path and os.path.isfile(mem_path)):
            return
        zst_path = f"{mem_path}.zst"
        if os.path.isfile(zst_path):
            # We kept the .zst around during revert, so delete the
            # decompressed copy rather than re-running zstd.
            self.virsh.execute_shell(f"rm -f '{mem_path}'", warn=True)
            return
        # No .zst on disk (unusual) — re-compress from scratch.
        if not self.compress_memory_file(mem_path):
            self.logger.warning(
                f"could not re-compress memory file after revert: {mem_path}")

    def _remove_memory_images(self, vm_name: str,
                              images: dict[str, str | None],
                              recorded_in_use=None) -> None:
        """
        Remove the memory image of each snapshot in *images* -- ``{snapshot:
        the memory file it recorded}``, read before it was deleted -- in both
        forms: the ``.raw`` libvirt records, and the ``.raw.zst`` that
        ``--compress-memory`` leaves in its place, which libvirt does not
        know of (#274). From wherever the snapshot recorded it: a
        memory_snapshot_dir, or the workdir. A disk-only snapshot recorded
        none, and has none to remove.

        Asked only once those snapshots are deleted, and only for the ones
        whose delete went through: a delete that fails removes nothing.

        Not an image another snapshot still records, of this domain or of
        another (#253): VM ``v``'s snapshot ``r_snapshot_s1`` and ``s1`` of
        VM ``v_snapshot_r`` name one file where their images share a
        directory. *recorded_in_use*
        (``LibVirtSession.memory_images_recorded``) maps what every domain's
        snapshots record, by resolved path and by identity; asked after the
        delete, it no longer counts the deleted one. Fails closed: with no
        scan, a scan that fails, or a file that cannot be inspected, the
        image is kept and a warning names it.
        """
        # {snapshot: (image, {file there: its identity, or None}, why kept)}
        found: dict[str, tuple] = {}
        for snap, image in images.items():
            if not image:
                continue
            files: dict[str, tuple[int, int] | None] = {}
            why = None
            for path in (image, f"{image}.zst"):
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    files[path] = None
                    why = why or f"{path} could not be inspected ({exc})"
                    continue
                if stat.S_ISREG(st.st_mode):
                    files[path] = (st.st_dev, st.st_ino)
            if files:
                found[snap] = (image, files, why)
        if not found:
            return

        try:
            recorded = (recorded_in_use() if recorded_in_use is not None
                        else None)
        except Exception as exc:
            self.logger.warning(
                f"the check of what other snapshots record failed on "
                f"{vm_name} ({type(exc).__name__}: {exc})")
            recorded = None

        for snap, (image, files, why) in found.items():
            why = why or self._why_a_memory_image_stays(image, files, recorded)
            if why:
                self.logger.warning(
                    f"not removing {', '.join(files)}, the memory image of "
                    f"deleted snapshot '{snap}' of {vm_name}: {why}. `boxman "
                    f"snapshot prune` removes it once no snapshot records "
                    f"it.")
                continue
            for path in files:
                result = self.virsh.execute_shell(
                    f"rm -f -- {shlex.quote(path)}", warn=True)
                if result.ok:
                    self.logger.info(
                        f"removed {path}, the memory image of deleted "
                        f"snapshot '{snap}' of {vm_name}")
                else:
                    self.logger.warning(
                        f"could not remove {path}, the memory image of "
                        f"deleted snapshot '{snap}' of {vm_name}: "
                        f"{(result.stderr or '').strip()}. `boxman snapshot "
                        f"prune` lists it.")

    @staticmethod
    def _why_a_memory_image_stays(image: str, files: dict,
                                  recorded) -> str | None:
        """
        Why the memory image *image* -- *files*, its forms there, each with
        its identity -- has to stay: *recorded*, the scan of what every
        domain's snapshots record, cannot tell (None), or names it, by
        resolved path (a recorded image counts with its ``.raw.zst``) or by
        identity (one file under another name). None when nothing does.
        """
        if recorded is None:
            return "whether another snapshot records it could not be checked"
        for path in (image, f"{image}.zst"):
            domain = recorded.get(os.path.realpath(path))
            if domain:
                return f"a snapshot of domain {domain} still records it"
        identities = getattr(recorded, "identities", {})
        for identity in files.values():
            domain = identities.get(identity)
            if domain:
                return (f"a snapshot of domain {domain} still records it, "
                        f"under another name")
        return None

    def delete_snapshot(self, vm_name: str, snapshot_name: str,
                        recorded_in_use=None) -> bool:
        """
        Delete a snapshot, and then its memory image, in both forms, unless
        another snapshot records it (:meth:`_remove_memory_images`, asking
        *recorded_in_use*): libvirt's own delete removes the ``.raw`` its
        record names and leaves the ``.raw.zst`` that ``--compress-memory``
        put in its place (#274).

        Internal (libvirt-native) snapshots are removed by ``virsh
        snapshot-delete`` directly. External snapshots — which is what
        boxman creates via ``snapshot-create-as --memspec`` — are not
        deletable that way; libvirt errors with "deletion of external
        snapshots is not supported". For external snapshots we dispatch:

        - **Only external snapshot**: ``virsh blockcommit --active --pivot``
          per disk merges the overlay back into base while online; then
          we remove the overlay/memory files and clear the libvirt
          metadata with ``--metadata``.
        - **The current snapshot, built on another**: equivalent to
          :meth:`collapse_to` against its parent, which refuses when
          another branch is built on that parent too — the VM is shut
          down, ``qemu-img rebase`` rewires the chain, and the dropped
          snapshot's files + metadata are cleaned up.
        - **Any other external snapshot** (one with snapshots built on it,
          or one the VM does not run on): refused, with a pointer to
          ``boxman snapshot collapse --to <parent>`` where that collapse
          would drop it; rebasing a middle layer while preserving both
          ends is out of v1 scope.

        Which snapshot is which is read from the snapshot tree and from
        libvirt's current snapshot, not from an order of the snapshots:
        after a restore and a take they branch (see :meth:`collapse_drops`).

        First, no snapshot of the VM may record an overlay of a CDROM
        (:meth:`_unrecord_cdrom_overlays`): libvirt's own delete would
        commit it into the ISO (#273). One whose record cannot be rewritten
        is not deleted.
        """
        info = self.virsh.execute("snapshot-info", vm_name, snapshot_name, warn=True)
        if not info.ok:
            self.logger.error(
                f"snapshot '{snapshot_name}' not found on vm {vm_name}: "
                f"{info.stderr.strip()}")
            return False

        # libvirt's own delete block-commits each overlay a snapshot records
        # into the image under it -- a CDROM's into its ISO, opened
        # read-write, shared by every VM booted from it (#273). A revert
        # records a CDROM overlay in the reverted-to snapshot; one restored
        # before that was undone, or where undoing it failed, still does.
        # No record of one is needed: a CDROM overlay holds nothing.
        snapshots = self._snapshot_xmls(vm_name)
        if snapshots is None:
            self.logger.error(
                f"cannot delete snapshot '{snapshot_name}' of {vm_name}: its "
                f"snapshots could not be read, to make sure none records an "
                f"overlay of a CDROM, which libvirt's delete would commit "
                f"into the ISO")
            return False
        left = self._unrecord_cdrom_overlays(vm_name, snapshots, {})
        if snapshot_name in left:
            self.logger.error(
                f"cannot delete snapshot '{snapshot_name}' of {vm_name}: it "
                f"records an overlay of a CDROM, which libvirt's delete would "
                f"commit into the ISO, and that record could not be "
                f"rewritten")
            return False

        # read before the delete, which takes the record with it
        image = self._memory_file_in(snapshots.get(snapshot_name))

        # Try the native delete first — works for internal snapshots.
        simple = self.virsh.execute(
            "snapshot-delete", vm_name, snapshot_name, warn=True)
        if simple.ok:
            self.logger.info(f"snapshot '{snapshot_name}' deleted from vm {vm_name}")
            # libvirt unlinked the .raw its record names, not the .raw.zst
            self._remove_memory_images(vm_name, {snapshot_name: image},
                                       recorded_in_use)
            self._name_overlays_the_delete_left(
                vm_name, snapshot_name, snapshots.get(snapshot_name))
            return True

        err_lower = (simple.stderr or "").lower()
        if "external" not in err_lower and "not supported" not in err_lower:
            self.logger.error(
                f"snapshot-delete failed: {simple.stderr.strip()}")
            return False

        # External snapshot — needs the rebase/blockcommit dance.
        return self._delete_external_snapshot(
            vm_name, snapshot_name, recorded_in_use=recorded_in_use)

    def _name_overlays_the_delete_left(self, vm_name: str,
                                       snapshot_name: str,
                                       xml_before: str | None) -> None:
        """
        Name in a warning each overlay that snapshot *snapshot_name* of
        *vm_name* recorded -- *xml_before*, its XML read before libvirt's
        own delete of it -- that the delete left, and that nothing of the VM
        needs now: no snapshot of it names it, and no disk the VM runs on
        is built on it. libvirt's delete of a leaf that is not the current
        snapshot unlinks the overlay its parent's take made (#289), not its
        own, which an earlier restore put back (#290).

        A hint: when the snapshots, the VM's disks or a chain cannot be
        read, nothing is said. ``snapshot prune`` asks everything again.
        """
        try:
            root = ET.fromstring(xml_before or "")
        except ET.ParseError:
            return
        recorded = [source.get("file")
                    for section in ("disks", "revertDisks")
                    for disk in root.findall(f"{section}/disk")
                    for source in disk.findall("source")
                    if source.get("file")]
        left = [path for path in dict.fromkeys(recorded)
                if os.path.isfile(path)]
        if not left:
            return
        snapshots = self._snapshot_xmls(vm_name)
        disks = self._data_disk_targets(vm_name)
        if snapshots is None or not disks:
            return
        needed: set[str] = set()
        for xml in snapshots.values():
            try:
                needed.update(s.get("file") for s in ET.fromstring(xml).iter(
                    "source") if s.get("file"))
            except ET.ParseError:
                return
        layers = CDROMManager(vm_name, provider_config=self.provider_config)
        for _target, source in disks:
            chain = layers.image_layers(source)
            if chain is None:
                return
            needed.update(path for path, _format in chain)
        needed |= {os.path.realpath(path) for path in needed}
        left = [path for path in left if path not in needed
                and os.path.realpath(path) not in needed]
        if left:
            self.logger.warning(
                f"{vm_name}: libvirt's delete of snapshot '{snapshot_name}' "
                f"left {', '.join(left)}, which it recorded: no snapshot of "
                f"the VM records them now, and it runs on none of them. "
                f"`boxman snapshot prune` removes them once nothing uses "
                f"them.")

    def _delete_external_snapshot(self,
                                  vm_name: str,
                                  snapshot_name: str,
                                  recorded_in_use=None) -> bool:
        """
        Delete an external snapshot libvirt refused to delete itself:
        before 9.0 it refuses every one, and later ones refuse a snapshot
        with children in some places, or while a block job runs.

        boxman drops only the current snapshot this way, and only one
        nothing is built on: by collapsing onto its parent, or for the only
        snapshot by committing it back into the base online. Parent and
        children are read from the snapshot tree, not from chain order:
        after a restore and a take two snapshots can share a parent, and
        the snapshot before this one in that order can be its sibling, the
        snapshot after it one on another branch (#291). Either way its
        memory image goes as :meth:`_remove_memory_images` says, asking
        *recorded_in_use*.
        """
        parents = self._snapshot_tree(vm_name)
        if parents is None:
            return False
        if snapshot_name not in parents:
            self.logger.error(
                f"snapshot {snapshot_name} not in chain for vm {vm_name}")
            return False

        current = self.get_latest_snapshot(vm_name)
        parent = parents[snapshot_name]
        built_on = self._descendants(parents, snapshot_name)
        if built_on:
            # a collapse onto the parent drops this one with what is built
            # on it, so it is named only where it would: where all of that
            # is the line up to the snapshot the VM runs on
            line = (self._line_between(parents, parent, current)
                    if parent is not None and current else None)
            hint = (f" use `boxman snapshot collapse --to {parent}` to drop "
                    f"it (and the snapshots above it) in one step."
                    if line is not None and sorted(line) == sorted(
                        self._descendants(parents, parent)) else "")
            self.logger.error(
                f"cannot delete external snapshot '{snapshot_name}' from "
                f"{vm_name}: it has newer snapshots built on it "
                f"({built_on}).{hint}")
            return False

        if snapshot_name != current:
            self.logger.error(
                f"cannot delete external snapshot '{snapshot_name}' from "
                f"{vm_name}: libvirt refused to, and boxman deletes one "
                f"itself only when it is the current snapshot, the one the "
                f"VM runs on "
                f"({f'{current!r} is' if current else 'it has none'})")
            return False

        if parent is None:
            others = sorted(n for n in parents if n != snapshot_name)
            if others:
                # blockcommit would merge it into the base those are built on
                self.logger.error(
                    f"cannot delete external snapshot '{snapshot_name}' from "
                    f"{vm_name}: it is built on no other snapshot, and the "
                    f"commit into the base that would drop it changes what "
                    f"{others} are built on")
                return False
            # Only snapshot in the chain — collapse it back to base online.
            return self._collapse_only_external_snapshot_online(
                vm_name, snapshot_name, recorded_in_use=recorded_in_use)

        # the current leaf, built on another: collapse_to(parent) drops just
        # this one, and refuses where another branch is built on the parent
        return self.collapse_to(vm_name, parent, dry_run=False,
                                recorded_in_use=recorded_in_use)

    def _collapse_only_external_snapshot_online(self,
                                                vm_name: str,
                                                snapshot_name: str,
                                                recorded_in_use=None) -> bool:
        """
        Online deletion of the only external snapshot via
        ``virsh blockcommit --active --pivot``. After this, the chain is
        a single base qcow2 again.

        Its memory image goes once its metadata is gone, as
        :meth:`_remove_memory_images` says, asking *recorded_in_use*: while
        the snapshot is still recorded, so is the image.
        """
        disks = self._data_disk_targets(vm_name)
        if not disks:
            self.logger.error(f"no data disks found for vm {vm_name}")
            return False
        image = self._memory_path_from_xml(vm_name, snapshot_name)

        for disk_target, _src in disks:
            commit = self.virsh.execute(
                "blockcommit", vm_name, disk_target,
                "--active", "--pivot", "--wait", "--verbose",
                warn=True)
            if not commit.ok:
                self.logger.error(
                    f"blockcommit failed for {vm_name}/{disk_target}: "
                    f"{commit.stderr.strip()}")
                return False

        for disk_target, _src in disks:
            overlay = self._overlay_path_for_snapshot(
                vm_name, snapshot_name, disk_target)
            if overlay and os.path.isfile(overlay):
                self.virsh.execute_shell(f"rm -f '{overlay}'", warn=True)

        meta = self.virsh.execute(
            "snapshot-delete", vm_name, snapshot_name, "--metadata", warn=True)
        if meta.ok:
            self._remove_memory_images(vm_name, {snapshot_name: image},
                                       recorded_in_use)
        else:
            self.logger.warning(
                f"overlays cleaned for {snapshot_name} but metadata delete "
                f"failed: {meta.stderr.strip()}; its memory image is kept, "
                f"as the snapshot still records it")
        self.logger.info(
            f"external snapshot '{snapshot_name}' deleted from vm {vm_name}")
        return True

    # ── snapshot collapse (qemu-img rebase) ─────────────────────────────
    #
    # collapse_to(target) merges the snapshots between *target* and the
    # current one, and *target*'s own overlay, into the live head, which it
    # rebases onto the disk *target* was taken on and records in *target*'s
    # place; it drops their overlays and metadata, and refuses when anything
    # else is built on *target* (collapse_drops). Every other snapshot
    # remains revertable. The VM must be off
    # because qemu-img rebase is offline-only — the BoxmanManager
    # orchestrator handles shutdown/start.

    def _snapshot_tree(self, vm_name: str) -> dict[str, str | None] | None:
        """
        ``{snapshot: parent}`` for every snapshot of *vm_name*, None as the
        parent of one built on none -- or None, said why, when a snapshot
        cannot be read. A collapse or a delete decides from it what it
        drops, and one left out of it could be built on one of those.
        """
        parents: dict[str, str | None] = {}
        for name, info in self._snapshot_graph(vm_name).items():
            try:
                if info['xml'] is None:
                    raise ET.ParseError("its snapshot-dumpxml failed")
                ET.fromstring(info['xml'])
            except ET.ParseError as exc:
                self.logger.error(
                    f"cannot read snapshot '{name}' of {vm_name} ({exc}), so "
                    f"nothing shows what is built on it; refusing to drop "
                    f"any snapshot")
                return None
            parents[name] = info.get('parent')
        return parents

    @staticmethod
    def _descendants(parents: dict[str, str | None],
                     name: str) -> list[str]:
        """Every snapshot built on *name*, nearest first, in *parents*'
        order (see :meth:`_snapshot_tree`)."""
        children: dict[str | None, list[str]] = {}
        for child, parent in parents.items():
            children.setdefault(parent, []).append(child)
        found: list[str] = []
        queue = [name]
        while queue:
            for child in children.get(queue.pop(0), []):
                # metadata in a loop would otherwise walk it forever
                if child != name and child not in found:
                    found.append(child)
                    queue.append(child)
        return found

    @staticmethod
    def _line_between(parents: dict[str, str | None], ancestor: str,
                      current: str | None) -> list[str] | None:
        """
        The snapshots after *ancestor* up to and including *current*,
        oldest first: empty when they are one snapshot, None when *current*
        is not built on *ancestor* (or is None).
        """
        line: list[str] = []
        node = current
        while node is not None and node != ancestor and node not in line:
            line.append(node)
            node = parents.get(node)
        return line[::-1] if node == ancestor else None

    def _collapse_plan(self, vm_name: str,
                       target_name: str) -> tuple[list, list] | None:
        """
        ``(drop, keep)`` for :meth:`collapse_drops`: what it returns, and
        every other snapshot.
        """
        parents = self._snapshot_tree(vm_name)
        if parents is None:
            return None
        if target_name not in parents:
            self.logger.error(
                f"snapshot '{target_name}' not in chain for vm {vm_name}")
            return None
        after = self._descendants(parents, target_name)
        refusal = f"cannot collapse {vm_name} to '{target_name}'"
        current = self.get_latest_snapshot(vm_name)
        line = self._line_between(parents, target_name, current)
        if line is None and not current:
            if not after:
                return [], list(parents)
            self.logger.error(
                f"{refusal}: libvirt names no current snapshot, so nothing "
                f"shows that {after} are the line the VM runs on, which is "
                f"all a collapse can merge into its disk")
            return None
        if line is None:
            self.logger.error(
                f"{refusal}: the VM runs on '{current}', which is not built "
                f"on '{target_name}', so there is nothing between them to "
                f"merge into its disk"
                + (f"; {after} would only be dropped" if after else ""))
            return None
        off_line = [n for n in after if n not in line]
        if off_line:
            path = " -> ".join([target_name, *line])
            self.logger.error(
                f"{refusal}: {off_line} are built on '{target_name}' on "
                f"another branch, not on the line the VM runs on ({path}), "
                f"so the collapse would drop them without merging them into "
                f"its disk. Delete them first (`boxman snapshot delete "
                f"--name <name>`, newest first), or collapse to a later "
                f"snapshot of that line.")
            return None
        return line, [n for n in parents if n not in line]

    def collapse_drops(self, vm_name: str,
                       target_name: str) -> list[str] | None:
        """
        The snapshots ``collapse --to target_name`` merges into the live
        disk and drops, oldest first: those between *target_name* and the
        current snapshot, the one the VM runs on. Empty when nothing is
        built on the target.

        The snapshots form a tree, not a line: take s1, take s2, restore s1,
        take s3, and s2 and s3 are both built on s1, s3 current (#291). The
        live disk is built on s3 alone, so a collapse to s1 can merge s3
        into it but not s2: dropping s2 would lose it, and the restore its
        overlays and memory serve. So this refuses -- None, logged -- when
        anything built on the target is off that line, and when the current
        snapshot is not built on the target at all. Keeping such a branch
        while dropping the line would mean working out which overlays of a
        dropped snapshot the branch is built on, and the collapse merges
        the target's overlay into the live disk and removes it, though a
        branch taken right after the target is built on it too -- the image
        libvirt's own delete of that branch unlinks (#289). Refusing loses
        nothing, and the error names what to delete first.

        Also None when the target is not there or a snapshot cannot be read
        (:meth:`_snapshot_tree`).
        """
        plan = self._collapse_plan(vm_name, target_name)
        return None if plan is None else plan[0]

    def _data_disk_targets(self, vm_name: str) -> list[tuple[str, str]]:
        """
        Return ``[(target_name, source_path), ...]`` for each
        ``<disk device='disk'>`` of *vm_name* (excludes cdroms).
        """
        result = self.virsh.execute(
            "domblklist", vm_name, "--details", warn=True)
        if not result.ok:
            return []
        disks: list[tuple[str, str]] = []
        for row in parse_domblklist(result.stdout):
            if row.device != 'disk' or row.source is None:
                continue
            disks.append((row.target, row.source))
        return disks

    def _overlay_path_for_snapshot(self,
                                   vm_name: str,
                                   snapshot_name: str,
                                   disk_target: str) -> str | None:
        """Source file recorded for *disk_target* in the snapshot XML."""
        result = self.virsh.execute(
            "snapshot-dumpxml", vm_name, snapshot_name, warn=True)
        if not result.ok:
            return None
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError:
            return None
        for disk in root.findall(".//disks/disk"):
            if disk.get("name") == disk_target:
                source = disk.find("source")
                if source is not None:
                    return source.get("file")
        return None

    def _qemu_img_rebase(self,
                         head_path: str,
                         new_base_path: str) -> bool:
        """
        ``qemu-img rebase -p -b <new_base> -F qcow2 <head>`` — VM must
        be offline. ``-p`` shows progress. The rebase merges intermediate
        overlays' content into *head* so reading *head* alone produces
        the same view as the original chain did.
        """
        sudo = "sudo " if self.use_sudo else ""
        cmd = (f"{sudo}qemu-img rebase -p "
               f"-b '{new_base_path}' -F qcow2 '{head_path}'")
        self.logger.info(f"rebasing {head_path} onto {new_base_path}")
        result = self.virsh.execute_shell(cmd, warn=True, hide=False)
        if not result.ok:
            self.logger.error(
                f"qemu-img rebase failed for {head_path}: "
                f"{result.stderr.strip()}")
            return False
        return True

    def _strip_backing_store_cache(self, vm_name: str) -> bool:
        """
        Remove cached ``<backingStore>`` children from each ``<disk
        device='disk'>`` in *vm_name*'s domain XML and ``virsh define``
        the cleaned XML.

        After modifying the qcow2 chain externally (rebase / blockcommit),
        libvirt's cached backingStore points at files that no longer exist
        and ``virsh start`` fails until the cache is cleared. Use a real
        XML parser — nested elements break naive regex.
        """
        import tempfile

        result = self.virsh.execute("dumpxml", vm_name, warn=True)
        if not result.ok:
            self.logger.error(
                f"dumpxml failed for {vm_name}: {result.stderr.strip()}")
            return False
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError as exc:
            self.logger.error(f"could not parse domain XML for {vm_name}: {exc}")
            return False

        modified = False
        for disk in root.findall(".//disk"):
            if disk.get("device") not in ("disk", None):
                continue
            for backing in list(disk.findall("backingStore")):
                disk.remove(backing)
                modified = True

        if not modified:
            return True

        new_xml = ET.tostring(root, encoding="unicode")
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".xml", delete=False) as tmp:
            tmp.write(new_xml)
            tmp_path = tmp.name
        try:
            define_result = self.virsh.execute("define", tmp_path, warn=True)
            if not define_result.ok:
                self.logger.error(
                    f"virsh define failed: {define_result.stderr.strip()}")
                return False
            self.logger.info(
                f"stripped cached <backingStore> from {vm_name} domain XML")
            return True
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @staticmethod
    def _same_file(path: str, other: str) -> bool:
        """
        Whether *path* and *other* name one file: by ``(st_dev, st_ino)``,
        so a symlinked or bind-mounted image directory cannot hide it, and
        by resolved path only when one of them cannot be stat'ed.
        """
        try:
            return os.path.samefile(path, other)
        except OSError:
            return os.path.realpath(path) == os.path.realpath(other)

    def _one_file_where_qemu_runs(self, path: str, other: str) -> bool | None:
        """
        Whether *path* and *other* are one file where qemu-img opens them:
        by ``(st_dev, st_ino)``, read with the commands boxman runs (in a
        container, with its own mounts; with sudo as the force and skip
        lists say), not as boxman sees them. The same path is one file
        without asking.

        Returns:
            bool: or None when either cannot be stat'ed there.
        """
        if path == other:
            return True
        result = self.virsh.execute_shell(
            f"stat -L -c '%d %i' -- {shlex.quote(path)} "
            f"{shlex.quote(other)}", warn=True, hide=True)
        if not result.ok:
            return None
        identities = [line.split() for line in result.stdout.splitlines()
                      if line.strip()]
        if len(identities) != 2 or not all(
                len(i) == 2 and all(n.isdigit() for n in i)
                for i in identities):
            return None
        return identities[0] == identities[1]

    def _collapse_keeps(self,
                        vm_name: str,
                        kept: list[str],
                        disks: list[tuple[str, str]],
                        records: dict | None = None) -> list[tuple] | None:
        """
        The files :meth:`collapse_to` must not remove, each with the reason:
        every disk's head, the disk each *kept* snapshot was taken on, which
        a revert to it builds on (what :meth:`validate_snapshot` checks),
        and every overlay the *kept* snapshots record. Each head is rebased
        onto the disk the target was taken on (:meth:`_collapse_base`), so
        that covers each disk's live chain after the rebase.

        *records*, ``{(snapshot, disk target): overlay}``, stands in for
        what those snapshots record now: what the collapse records in its
        target once the heads stand on that disk (:meth:`_settle_target`).

        The head needs saying. An external snapshot records the overlay it
        created, and that overlay became the active disk, so until a revert
        moves the domain the newest snapshot's recorded overlay *is* the
        head -- and the rebase rewrites the head in place.

        Returns:
            list: ``(path, reason)`` pairs, or None when one of them cannot
            be stat'ed -- not even when it seems missing, since the removal
            may run where it is there (a container). Then no comparison can
            show that a dropped overlay is not that file under another name,
            so none may be removed. One missing where qemu-img runs as well
            is no file at all, and is left out: libvirt's delete of a
            branch unlinks the overlay its parent's take made (#289).
        """
        keeps = []
        for disk_target, head in disks:
            keeps.append((head, f"it is the disk {disk_target} runs on"))
        for snap in kept:
            for disk_target, _head in disks:
                path = ((records or {}).get((snap, disk_target))
                        or self._overlay_path_for_snapshot(
                            vm_name, snap, disk_target))
                if path:
                    keeps.append(
                        (path, f"kept snapshot '{snap}' still records it"))
                path = self._source_in_snapshot_domain(
                    vm_name, snap, disk_target)
                if path:
                    keeps.append(
                        (path, f"a revert to kept snapshot '{snap}' "
                               f"builds on it"))
        examined = []
        for path, reason in keeps:
            try:
                os.stat(path)
            except FileNotFoundError:
                if not self._gone_where_qemu_runs(path):
                    return None
                continue
            except OSError:
                return None
            examined.append((path, reason))
        return examined

    def _gone_where_qemu_runs(self, path: str) -> bool:
        """Whether nothing is at *path* where qemu-img runs (see
        :meth:`_one_file_where_qemu_runs`): False when that cannot be
        asked."""
        quoted = shlex.quote(path)
        # one command, so that a sudo prefix takes all of it
        script = (f"if test -e {quoted} || test -L {quoted}; then "
                  f"echo present; else echo absent; fi")
        result = self.virsh.execute_shell(
            f"sh -c {shlex.quote(script)}", warn=True, hide=True)
        return bool(result.ok) and (result.stdout or "").strip() == "absent"

    def _collapse_base(self, vm_name: str, target_name: str,
                       disk_target: str, head: str) -> tuple:
        """
        ``(base, same, why)``: the image a collapse to *target_name*
        rebases *disk_target*'s *head* onto, and whether the head is that
        image (None when that cannot be told where qemu-img runs) -- or
        ``(None, None, why)`` when the target records no overlay for it.

        The disk the target was taken on
        (:meth:`_source_in_snapshot_domain`), wherever the head's chain
        shows it is built on that: the overlay the target records, made by
        its take or by a revert to it, is merged into the head with the
        snapshots dropped. That is what libvirt's own delete of each of
        them would leave, the target's disk right under the head
        (``qemuSnapshotDiscardExternal``), and what its delete of the
        target, current then, requires: it takes the domain's disk for the
        target's and refuses unless the image right under it is the one the
        target was taken on, "snapshot VM disk source and parent disk
        source are not the same" (``qemuSnapshotDeleteExternalPrepareData``,
        libvirt 10.10). Rebased onto the overlay instead, as on a line of
        takes it was, the target could not be deleted afterwards. The
        target then records the head (:meth:`_settle_target`).

        After a restore of the target and a take, the overlay the take made
        is under another branch -- or gone with it, which libvirt's delete
        of that branch unlinks (#289) -- and the head is built on the
        revert's overlay on that disk (#291). Where the head's chain cannot
        be read, it is the overlay, as before.
        """
        recorded = self._overlay_path_for_snapshot(
            vm_name, target_name, disk_target)
        if not recorded:
            return None, None, (f"cannot find overlay for disk "
                                f"'{disk_target}' in snapshot "
                                f"'{target_name}' of vm {vm_name}")
        same = self._one_file_where_qemu_runs(head, recorded)
        if same:
            return recorded, True, None
        layers = CDROMManager(
            vm_name, provider_config=self.provider_config).image_layers(head)
        if layers is None:
            return recorded, same, None
        under = [path for path, _format in layers[1:]]

        def is_under(path: str | None) -> bool:
            return bool(path) and (
                os.path.normpath(path) in map(os.path.normpath, under)
                or any(self._one_file_where_qemu_runs(path, layer)
                       for layer in under))
        taken_on = self._source_in_snapshot_domain(
            vm_name, target_name, disk_target)
        if is_under(taken_on):
            return taken_on, False, None
        return recorded, same, None

    def _source_in_snapshot_domain(self, vm_name: str, snapshot_name: str,
                                   disk_target: str) -> str | None:
        """*disk_target*'s source in the definition *snapshot_name* saved
        that a revert to it builds on
        (:meth:`_definition_a_revert_builds_on`): the disk it was taken on,
        which :meth:`validate_snapshot` checks."""
        result = self.virsh.execute(
            "snapshot-dumpxml", vm_name, snapshot_name, warn=True)
        if not result.ok:
            return None
        try:
            root = ET.fromstring(result.stdout)
        except ET.ParseError:
            return None
        definition = self._definition_a_revert_builds_on(root)
        for disk in (definition.iter("disk")
                     if definition is not None else ()):
            target, source = disk.find("target"), disk.find("source")
            if (target is not None and target.get("dev") == disk_target
                    and source is not None):
                return source.get("file")
        return None

    def _collapse_keep_reason(self,
                              path: str,
                              keeps: list[tuple] | None) -> str | None:
        """Why *path* has to stay (see :meth:`_collapse_keeps`), or None."""
        if keeps is None:
            return ("the domain's disks, or the overlays its kept snapshots "
                    "record, could not be examined, so nothing shows it is "
                    "not still in use")
        for kept_path, reason in keeps:
            if self._same_file(path, kept_path):
                return reason
        return None

    def collapse_to(self,
                    vm_name: str,
                    target_name: str,
                    dry_run: bool = False,
                    recorded_in_use=None) -> bool:
        """
        Collapse the snapshots between *target_name* and the current one
        into the live head (:meth:`collapse_drops`, which refuses when
        another branch is built on *target_name*). *target_name* and every
        other snapshot remain revertable.

        Workflow per disk: ``qemu-img rebase`` the head onto the disk the
        target was taken on (:meth:`_collapse_base`), so the rebased head's
        read-alone view equals what the full chain produced. Then strip
        ``<backingStore>`` cache, ``virsh define``, delete the dropped
        snapshots' overlays, clear their libvirt metadata, and then remove
        their memory images, ``.raw`` and ``.raw.zst``, as
        :meth:`_remove_memory_images` says, asking *recorded_in_use*: not
        one another snapshot still records. Last, the target records each
        head in place of the overlay it recorded, which is removed
        (:meth:`_settle_target`): what libvirt's own delete of the dropped
        snapshots would leave, which its delete of the target needs.

        A dropped overlay that is still live -- usually the newest one,
        which is the head itself -- is kept, and so is anything the kept
        snapshots record (see :meth:`_collapse_keeps`).

        VM must be offline. Returns True on success.
        """
        info = self.virsh.execute(
            "snapshot-info", vm_name, target_name, warn=True)
        if not info.ok:
            self.logger.error(
                f"target snapshot '{target_name}' not found on vm "
                f"{vm_name}: {info.stderr.strip()}")
            return False

        plan = self._collapse_plan(vm_name, target_name)
        if plan is None:
            return False
        drop_set, kept = plan

        if not drop_set:
            self.logger.info(
                f"no snapshot is built on '{target_name}' on vm {vm_name}; "
                f"nothing to collapse")
            return True

        disks = self._data_disk_targets(vm_name)
        if not disks:
            self.logger.error(f"no data disks found for vm {vm_name}")
            return False

        # the heads do not move in the rebase, so this is already the
        # set the cleanup below has to leave alone
        keeps = self._collapse_keeps(vm_name, kept, disks)

        if dry_run:
            self.logger.info(
                f"[dry-run] {vm_name}: would drop {len(drop_set)} snapshot(s) "
                f"({drop_set}), the line from '{target_name}' to the one the "
                f"VM runs on; would keep {kept}")
            stops = False
            moved: dict[str, tuple[str, str]] = {}
            retiring: list[str] = []
            for disk_target, source in disks:
                new_base, same, why = self._collapse_base(
                    vm_name, target_name, disk_target, source)
                if same:
                    self.logger.info(
                        f"[dry-run]   {source} is the overlay of "
                        f"'{target_name}' itself; nothing to rebase")
                elif new_base is None:
                    stops = True
                    self.logger.error(
                        f"[dry-run]   {why}; the collapse would stop here")
                elif same is None:
                    stops = True
                    self.logger.error(
                        f"[dry-run]   whether {source} is {new_base} cannot "
                        f"be told where qemu-img runs; the collapse would "
                        f"stop here")
                else:
                    self.logger.info(
                        f"[dry-run]   would rebase {source} onto {new_base}")
                    stranded = self._stranded_overlay(
                        vm_name, target_name, disk_target, new_base)
                    if stranded:
                        moved[disk_target] = (source, stranded)
                        retiring.append(stranded)
                        self.logger.info(
                            f"[dry-run]   '{target_name}' would record "
                            f"{source} as its overlay of {disk_target}, in "
                            f"place of {stranded}")
                    retiring.extend(self._merged_layers(
                        vm_name, source, new_base) or [])
            if stops:
                # before anything is rebased or removed
                return False
            told: set[str] = set()
            for snap in drop_set:
                for disk_target, _ in disks:
                    overlay = self._overlay_path_for_snapshot(
                        vm_name, snap, disk_target)
                    if not overlay or not os.path.isfile(overlay):
                        continue
                    told.add(overlay)
                    reason = self._collapse_keep_reason(overlay, keeps)
                    if reason:
                        self.logger.info(
                            f"[dry-run]   would keep {overlay}: {reason}")
                    else:
                        self.logger.info(f"[dry-run]   would remove {overlay}")
            retiring = [p for p in dict.fromkeys(retiring)
                        if p not in told and os.path.isfile(p)]
            # what is left once the target records the heads (_settle_target)
            settled = self._collapse_keeps(
                vm_name, kept, disks,
                records={(target_name, disk): head
                         for disk, (head, _old) in moved.items()}
            ) if retiring else None
            for path in retiring:
                reason = self._collapse_keep_reason(path, settled)
                self.logger.info(
                    f"[dry-run]   would keep {path}: {reason}" if reason
                    else f"[dry-run]   would remove {path}, merged into the "
                         f"head")
            return True

        # Per-disk rebase — must succeed for every disk before we touch
        # metadata. What each disk needs is told for all of them first, so a
        # disk that cannot be told stops the collapse before any is rebased.
        rebases = []
        for disk_target, head_path in disks:
            new_base, same, why = self._collapse_base(
                vm_name, target_name, disk_target, head_path)
            if new_base is None:
                self.logger.error(f"{why}; not collapsing vm {vm_name}")
                return False
            if same is None:
                self.logger.error(
                    f"cannot tell whether {head_path} is {new_base} where "
                    f"qemu-img runs (one of them could not be stat'ed "
                    f"there); not collapsing vm {vm_name}")
                return False
            rebases.append((disk_target, head_path, new_base, same))
        # read before anything changes: the overlays the target records that
        # no head will stand on, which it records the heads in place of, and
        # the images each rebase merges into its head
        moved: dict[str, tuple[str, str]] = {}
        merged: dict[str, tuple[str, list[str]]] = {}
        unread: list[tuple[str, str]] = []
        for disk_target, head_path, new_base, same in rebases:
            if same:
                continue
            stranded = self._stranded_overlay(
                vm_name, target_name, disk_target, new_base)
            if stranded:
                moved[disk_target] = (head_path, stranded)
            layers = self._merged_layers(vm_name, head_path, new_base)
            if layers is None:
                unread.append((head_path, new_base))
            merged[disk_target] = (head_path, layers or [])
        for disk_target, head_path, new_base, same in rebases:
            if same:
                # nothing stands between them: the head is the target's own
                # overlay (a revert and a new take put it there). Rebasing a
                # file onto itself trips qemu's image lock at best, and
                # without one writes a backing chain that loops. One file as
                # boxman sees it need not be one where qemu-img runs (a
                # container's own mounts), so it is asked there.
                self.logger.info(
                    f"{head_path} is the overlay of '{target_name}' itself; "
                    f"nothing to rebase for {disk_target}")
                continue
            if not self._qemu_img_rebase(head_path, new_base):
                return False

        if not self._strip_backing_store_cache(vm_name):
            return False

        images: dict[str, str | None] = {}
        for snap in drop_set:
            for disk_target, _ in disks:
                overlay = self._overlay_path_for_snapshot(
                    vm_name, snap, disk_target)
                if not overlay or not os.path.isfile(overlay):
                    continue
                reason = self._collapse_keep_reason(overlay, keeps)
                if keeps is None:
                    self.logger.warning(
                        f"not removing {overlay} of dropped snapshot "
                        f"'{snap}' on {vm_name}: {reason}. `boxman snapshot "
                        f"prune` removes it once nothing uses it.")
                    continue
                if reason:
                    self.logger.info(
                        f"keeping {overlay} of dropped snapshot '{snap}': "
                        f"{reason}")
                    continue
                result = self.virsh.execute_shell(
                    f"rm -f '{overlay}'", warn=True)
                if os.path.lexists(overlay):
                    # its record goes next, and then nothing names it
                    self.logger.warning(
                        f"could not remove {overlay} of dropped snapshot "
                        f"'{snap}' on {vm_name}"
                        + (f": {(result.stderr or '').strip()}"
                           if result is not None and not result.ok
                           and (result.stderr or '').strip() else "")
                        + ". `boxman snapshot prune` removes it once nothing "
                          "uses it.")
            # read before its record goes; removed once every dropped one's
            # has, so the scan no longer counts them (_remove_memory_images)
            image = self._memory_path_from_xml(vm_name, snap)
            meta = self.virsh.execute(
                "snapshot-delete", vm_name, snap, "--metadata", warn=True)
            if meta.ok:
                images[snap] = image
            else:
                self.logger.warning(
                    f"could not clear metadata for snapshot '{snap}' on "
                    f"{vm_name}: {meta.stderr.strip()}; its memory image is "
                    f"kept, as the snapshot still records it")
        self._remove_memory_images(vm_name, images, recorded_in_use)

        if moved or merged:
            self._settle_target(vm_name, target_name, moved, merged, disks)
        for head_path, new_base in unread:
            # a revert's overlay under the head, after a restore of the
            # target and a take (#292): merged into the head, recorded by
            # nothing now, and not known here to be removed
            self.logger.warning(
                f"{vm_name}: the backing chain of {head_path} could not be "
                f"read down to {new_base}, so the images the rebase merged "
                f"into it are left where they are, if there were any; "
                f"`boxman snapshot prune` lists those nothing records or "
                f"uses.")

        self.logger.info(
            f"collapsed {len(drop_set)} snapshot(s) on vm {vm_name}; "
            f"'{target_name}' is its current snapshot now")
        return True

    def _stranded_overlay(self, vm_name: str, target_name: str,
                          disk_target: str, new_base: str) -> str | None:
        """
        The overlay *target_name* records for *disk_target*, when a collapse
        rebases that disk's head onto another image, *new_base*: the disk
        the target was taken on (:meth:`_collapse_base`). No head stands on
        the overlay then, and the target records the head in its place
        (:meth:`_settle_target`). None when the head goes onto the overlay.
        """
        recorded = self._overlay_path_for_snapshot(
            vm_name, target_name, disk_target)
        return recorded if recorded and recorded != new_base else None

    def _record_the_heads(self, vm_name: str, snapshot_name: str,
                          heads: dict[str, str]) -> str | None:
        """
        Redefine *snapshot_name* of *vm_name* to record *heads* -- ``{disk
        target: image}`` -- as its overlays of those disks
        (:meth:`_snapshot_xml_recording`): None once it does, else why it
        does not.
        """
        result = self.virsh.execute("snapshot-dumpxml", vm_name,
                                    snapshot_name, "--security-info",
                                    warn=True)
        if not result.ok:
            return (f"its record could not be read "
                    f"({(result.stderr or '').strip()})")
        try:
            xml_text = self._snapshot_xml_recording(result.stdout, heads)
        except ValueError as exc:
            return f"its record could not be edited ({exc})"
        if xml_text is None:
            return None
        ok, error = self._redefine_snapshot(vm_name, xml_text)
        return None if ok else f"its redefine failed ({error})"

    def _merged_layers(self, vm_name: str, head: str,
                       new_base: str) -> list[str] | None:
        """
        The images between *head* and *new_base* in the head's chain, top
        first: what a rebase of the head onto *new_base* merges into it.
        None when the chain cannot be read, or does not reach *new_base*:
        which images those are cannot be told.
        """
        layers = CDROMManager(
            vm_name, provider_config=self.provider_config).image_layers(head)
        between: list[str] = []
        for path, _format in (layers or [])[1:]:
            if (os.path.normpath(path) == os.path.normpath(new_base)
                    or self._one_file_where_qemu_runs(path, new_base)):
                return between
            between.append(path)
        return None

    def _settle_target(self, vm_name: str, target_name: str,
                       moved: dict[str, tuple[str, str]],
                       merged: dict[str, tuple[str, list[str]]],
                       disks: list[tuple[str, str]]) -> None:
        """
        Once a collapse has put heads on the disks *target_name* was taken
        on, record each head as the target's overlay in place of the one it
        recorded -- *moved*, ``{disk target: (head, that overlay)}``. Then
        remove those overlays and the images the rebase merged into the
        heads -- *merged*, ``{disk target: (head, [image, ...])}`` -- which
        no head stands on now, where nothing still needs them
        (:meth:`_collapse_keeps`, asked of every snapshot left).

        That is the record libvirt's own delete of the dropped snapshots
        leaves: the current snapshot records the disk the domain runs on,
        and a revert to it unlinks that, the state it discards
        (``qemuSnapshotRevertExternalFinish``). Still naming the old
        overlay, the target had that unlinked instead and the head left
        where nothing records it; and a collapse onto it again left this
        one's head behind, merged into the next. So did a revert's overlay
        under the head, after a restore of the target and a take: nothing
        records it once the take it was taken on is dropped.

        A record that cannot be rewritten keeps the overlays it names, and
        is named in a warning.
        """
        if moved:
            heads = {disk: head for disk, (head, _old) in moved.items()}
            why = self._record_the_heads(vm_name, target_name, heads)
            if why:
                self.logger.warning(
                    f"'{target_name}' on {vm_name} still records "
                    f"{', '.join(old for _head, old in moved.values())}, "
                    f"though it runs on {', '.join(heads.values())}, built on "
                    f"the disks '{target_name}' was taken on: {why}. A delete "
                    f"or a restore of it still works, and leaves those files "
                    f"behind; `boxman snapshot prune` removes them once "
                    f"nothing uses them.")
            else:
                self.logger.info(
                    f"'{target_name}' on {vm_name} records "
                    f"{', '.join(heads.values())}, the disks it runs on, now")
        parents = self._snapshot_tree(vm_name)
        keeps = (None if parents is None
                 else self._collapse_keeps(vm_name, list(parents), disks))
        done: set[str] = set()
        for disk_target, (head, layers) in merged.items():
            stranded = moved.get(disk_target, (None, None))[1]
            for path in ([stranded] if stranded else []) + layers:
                if path in done or not os.path.isfile(path):
                    # a dropped overlay is gone already, and libvirt's
                    # delete of a branch unlinks the target's (#289)
                    continue
                done.add(path)
                reason = self._collapse_keep_reason(path, keeps)
                if keeps is None:
                    self.logger.warning(
                        f"not removing {path}, merged into {head}: {reason}. "
                        f"`boxman snapshot prune` removes it once nothing "
                        f"uses it.")
                    continue
                if reason:
                    self.logger.info(
                        f"keeping {path}, merged into {head}: {reason}")
                    continue
                result = self.virsh.execute_shell(
                    f"rm -f -- {shlex.quote(path)}", warn=True)
                if result.ok:
                    self.logger.info(
                        f"removed {path}, merged into {head}, which "
                        f"{disk_target} runs on")
                else:
                    self.logger.warning(
                        f"could not remove {path}, merged into {head}: "
                        f"{(result.stderr or '').strip()}. `boxman snapshot "
                        f"prune` removes it once nothing uses it.")
