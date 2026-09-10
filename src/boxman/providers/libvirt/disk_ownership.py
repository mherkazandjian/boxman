"""
Which disks boxman attached to a domain, and which of them it may detach.

Boxman needs to know that a disk on a domain is one *it* created from a
``disks:`` entry, and not the root disk, a disk someone attached by hand,
or a different disk that has since taken the same target. Nothing in the
domain XML says so: a qcow2 attached at ``vdb`` looks the same either way.

So it is recorded, at attach time, in the domain's own ``<metadata>``:
the logical name from ``conf.yml``, the target it went to, its role, and
the exact source path attached. Removal is then decided against that
record rather than inferred.

The inference that does *not* work is subtraction -- "attached, but not
declared, so remove it". :func:`plan_disk_removals` documents the four
variants of it that were tried and what each one detaches by mistake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from boxman import log
from boxman.exceptions import ProvisionError

#: namespace for boxman's per-domain metadata block
DISK_METADATA_URI = "https://github.com/mherkazandjian/boxman/disks/1.0"

#: the key virsh files the block under
DISK_METADATA_KEY = "boxman"

#: target a ``disks:`` entry gets when it does not name one. The differ and
#: DiskManager both default to this, so the removal rule has to as well: a
#: declaration without an explicit target still occupies vdb, and treating
#: it as claiming nothing detached the disk the operator had just renamed
#: (#164 F2 review).
DEFAULT_DISK_TARGET = "vdb"

#: role of a disk boxman created from a ``disks:`` entry. Only these are
#: ever candidates for removal.
ROLE_DATA = "data"

#: role of a disk boxman attached but did **not** create -- its image file
#: already existed at the expected pathname, so it was attached as-is
#: rather than recreated. An existence check at a predictable path is not
#: proof boxman made the file: it could have been supplied by hand, or be
#: a naming collision. Never removed automatically, and reported rather
#: than silently skipped (#164 F2 review, finding 5).
ROLE_ADOPTED = "adopted"

#: role of the disk the VM boots from. Recorded so that a future reader
#: never has to infer it, and refused explicitly by the removal rule.
ROLE_ROOT = "root"


@dataclass(frozen=True)
class DiskRecord:
    """One disk boxman attached, as recorded on the domain."""

    name: str
    target: str
    role: str
    source: str


def records_to_xml(records: list[DiskRecord]) -> str:
    """Render *records* as the metadata block's XML."""
    root = ET.Element("disks")
    for record in records:
        ET.SubElement(root, "disk", {
            "name": record.name,
            "target": record.target,
            "role": record.role,
            "source": record.source,
        })
    return ET.tostring(root, encoding="unicode")


def records_from_xml(xml_text: str) -> list[DiskRecord]:
    """Parse a metadata block into records.

    Raises:
        ProvisionError: if the block is present but unreadable. A domain
            whose ownership record cannot be parsed must not be treated as
            a domain that has none -- that would read as "boxman attached
            nothing here", which is the answer that makes a removal rule
            dangerous.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ProvisionError(
            f"could not parse boxman's disk ownership metadata: {exc}"
        ) from exc

    records = []
    for element in root.findall("disk"):
        missing = [key for key in ("name", "target", "role", "source")
                   if not element.get(key)]
        if missing:
            raise ProvisionError(
                f"boxman's disk ownership metadata has an entry missing "
                f"{', '.join(missing)}"
            )
        records.append(DiskRecord(
            name=element.get("name"),
            target=element.get("target"),
            role=element.get("role"),
            source=element.get("source"),
        ))
    return records


def read_disk_records(virsh, domain_name: str) -> list[DiskRecord] | None:
    """
    Read the ownership records off *domain_name*.

    Returns:
        The records, or ``None`` when the domain carries no boxman metadata
        at all. The distinction matters: ``None`` means "boxman does not
        know what it attached here", which is never grounds for removing
        anything, while ``[]`` means "boxman attached nothing".
    """
    result = virsh.execute(
        'metadata', domain_name,
        uri=DISK_METADATA_URI, config=True,
        warn=True)
    if not result.ok:
        stderr = (result.stderr or '')
        # virsh says so explicitly when the namespace is simply absent
        if 'metadata not found' in stderr.lower():
            return None
        raise ProvisionError(
            f"could not read the disk ownership metadata of {domain_name}: "
            f"{stderr.strip() or 'virsh metadata failed'}"
        )
    text = (result.stdout or '').strip()
    if not text:
        return None
    return records_from_xml(text)


def write_disk_records(virsh, domain_name: str,
                       records: list[DiskRecord]) -> None:
    """Replace the ownership records on *domain_name*."""
    result = virsh.execute(
        'metadata', domain_name,
        uri=DISK_METADATA_URI,
        key=DISK_METADATA_KEY,
        set=records_to_xml(records),
        config=True,
        warn=True)
    if not result.ok:
        raise ProvisionError(
            f"could not record which disks boxman attached to "
            f"{domain_name}: "
            f"{(result.stderr or '').strip() or 'virsh metadata failed'}"
        )


def record_attached_disk(virsh, domain_name: str, *, name: str, target: str,
                         source: str, role: str = ROLE_DATA) -> None:
    """Add (or update) one disk's ownership record on *domain_name*."""
    existing = read_disk_records(virsh, domain_name) or []
    kept = [r for r in existing
            if r.name != name and r.target != target]
    kept.append(DiskRecord(name=name, target=target, role=role, source=source))
    write_disk_records(virsh, domain_name, kept)


def forget_disk(virsh, domain_name: str, name: str) -> None:
    """Drop one disk's ownership record after it has been detached."""
    existing = read_disk_records(virsh, domain_name) or []
    write_disk_records(virsh, domain_name,
                       [r for r in existing if r.name != name])


def plan_disk_removals(
    records: list[DiskRecord] | None,
    desired_disks: list[dict[str, Any]],
    actual_disks: list[dict[str, Any]],
) -> tuple[list[DiskRecord], list[tuple[DiskRecord, str]]]:
    """
    Decide which recorded disks may be detached.

    A record is removable only when **all three** hold:

    1. its logical name is gone from the desired config,
    2. no declared disk claims its target, and
    3. the disk currently at that target is the exact source that was
       recorded when boxman attached it.

    Returns ``(removals, refusals)``; each refusal carries the reason, to
    be reported rather than acted on.

    Every simpler rule that was tried detaches something it should not:

    * **Subtraction** -- attached minus declared. ``get_actual_disks()``
      returns every file-backed disk, so the first thing this removes is
      the root disk.
    * **Filename prefix** -- "the file is named after this VM". Underscore
      collisions make two different disks produce the same path, and a
      sibling VM whose name extends this one's (``node`` and ``node_db``)
      has a boot disk that matches ``node``'s prefix.
    * **Name/target record without a source check** -- a record naming a
      target is enough to detach whatever is at it, so a *replacement*
      disk attached at a reused target is removed in place of the one that
      was recorded.
    * **Adding a backing-chain identity check** -- an overlay created
      independently, backed by the recorded file, satisfies it and is
      detached.

    Condition 3 is what closes all of them: the source has to be the same
    path, not merely related to it. Note that this refuses while the disk
    at the target is something else *for any reason* -- including after a
    snapshot has moved the domain's head to an overlay. Collapsing
    snapshots does not necessarily restore the recorded pathname, because
    ``collapse_to()`` rebases the existing head rather than returning to
    the original file, so the refusal is phrased in terms of the source
    differing rather than promising a remedy.
    """
    if records is None:
        # No ownership record: boxman does not know what it attached here.
        # Never grounds for detaching anything.
        return [], []

    desired_names = {d.get('name') for d in desired_disks if d.get('name')}
    # ... including the ones that did not spell a target out: they still
    # claim one, and reading them as claiming nothing is how a rename
    # detached a disk that was still declared.
    desired_targets = {
        d.get('target') or DEFAULT_DISK_TARGET for d in desired_disks}
    actual_by_target = {d['target']: d for d in actual_disks}

    removals: list[DiskRecord] = []
    refusals: list[tuple[DiskRecord, str]] = []

    for record in records:
        if record.role == ROLE_ROOT:
            # the root disk is recorded so it never has to be guessed at
            continue
        if record.name in desired_names:
            continue
        if record.role != ROLE_DATA:
            # adopted, or a role a future version added: boxman did not
            # create it, so it says so instead of quietly doing nothing
            refusals.append((
                record,
                f"boxman attached it but did not create it (role "
                f"{record.role!r}); detach it by hand if you want it gone"))
            continue

        if record.target in desired_targets:
            refusals.append((
                record,
                f"a declared disk now claims target {record.target}"))
            continue

        attached = actual_by_target.get(record.target)
        if attached is None:
            # already detached; nothing to do and nothing to report
            continue

        if attached.get('source') != record.source:
            refusals.append((
                record,
                f"the disk at {record.target} is now "
                f"{attached.get('source')!r}, not the {record.source!r} "
                f"boxman attached"))
            continue

        removals.append(record)

    return removals, refusals


def occupied_target_conflicts(
    records: list[DiskRecord] | None,
    desired_disks: list[dict[str, Any]],
    actual_disks: list[dict[str, Any]],
    expected_paths: dict[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    """
    Declarations whose target is held by a disk that is not theirs.

    Rename ``data`` to ``logs``, keep its explicit target, change the size:
    the removal is refused because the target is claimed, but nothing
    stopped reconciliation, which matches the occupant by target and grew
    the old ``data`` image — so the operator got the disk they renamed away
    from, enlarged, reported as success (#164 F2 review, finding 4).

    Keyed on the **occupant** rather than on removal eligibility. Whether
    boxman may *detach* what is there is a separate question from whether
    this declaration may *write to* it, and answering only the first let a
    replacement disk, an adopted disk, or a swapped pair through to a
    resize (#164 F2 review round 2, finding 2).

    *actual_disks* must be the view the add/resize path will act on, so the
    two cannot disagree about whether the target is occupied.

    *expected_paths* maps a declared name to the image file boxman would
    use for it. An occupant that *is* that file belongs to the
    declaration, whatever the metadata says -- which is how a domain
    predating the ownership record keeps working: nothing is recorded
    there, and without this every one of its disks would read as a
    conflict and no such project could be updated at all.

    Returns:
        ``(declared_name, target, occupant_source)`` per conflict.
    """
    actual_by_target = {d['target']: d for d in actual_disks}
    recorded_by_target = {r.target: r for r in (records or ())}
    expected_paths = expected_paths or {}
    conflicts = []
    for declared in desired_disks:
        target = declared.get('target') or DEFAULT_DISK_TARGET
        name = declared.get('name')
        occupant = actual_by_target.get(target)
        if occupant is None:
            # vacant: a plain addition, whatever stale metadata may say
            continue

        record = recorded_by_target.get(target)
        if record is not None and record.name == name:
            # this declaration's own disk, still where it was put
            continue
        if occupant.get('source') == expected_paths.get(name):
            # the occupant IS this declaration's image file -- unrecorded
            # (a domain predating the record) but unambiguously its own
            continue
        # something else holds the target: a replacement, a disk boxman
        # never recorded, an adopted one, or the other half of a swap
        # between two declared disks. Writing to it is not this
        # declaration's to do.
        conflicts.append((name, target, occupant.get('source')))
    return conflicts


def unowned_disks(records: list[DiskRecord] | None,
                  desired_disks: list[dict[str, Any]],
                  actual_disks: list[dict[str, Any]],
                  root_source: str | None = None) -> list[dict[str, Any]]:
    """
    Attached disks that are neither declared nor recorded.

    These are reported so an operator can see them, and never removed:
    boxman has no evidence it put them there.

    A domain with **no** record at all (``records is None``) predates the
    ownership metadata, so everything on it that is not declared lands
    here. That is the point: such a domain used to produce no removals and
    no report, so dropping a disk from its config looked like a no-op
    (#164 F2 review, finding 10). The caller distinguishes the two cases
    when it words the message.
    """
    recorded_targets = {r.target for r in (records or ())}
    desired_targets = {
        d.get('target') or DEFAULT_DISK_TARGET for d in desired_disks}
    return [
        disk for disk in actual_disks
        if disk['target'] not in recorded_targets
        and disk['target'] not in desired_targets
        and disk.get('source') != root_source
    ]


def detach_disk(virsh, domain_name: str, target: str,
                live: bool = False) -> None:
    """
    Detach *target* from *domain_name*. Never deletes the image file.

    The qcow2 is left on disk deliberately: an operator who removed a disk
    from ``conf.yml`` has asked for it to stop being attached, which is not
    the same as asking for its contents to be destroyed.

    Goes through ``execute()``, like every query that decided this detach
    was safe. ``execute_shell()`` applies the sudo and runtime wrappers but
    supplies neither the configured virsh executable nor ``-c <uri>``, so
    the checks ran against the configured connection while the mutation
    went to the default one -- with a same-named domain on both, it would
    detach a disk nothing had verified (#164 F2 review).
    """
    result = virsh.execute(
        'detach-disk', domain_name, target,
        config=True, live=live or None,
        warn=True)
    if not result.ok:
        raise ProvisionError(
            f"could not detach {target} from {domain_name}: "
            f"{(result.stderr or '').strip() or 'virsh detach-disk failed'}"
        )
    log.info(f"detached {target} from {domain_name} "
             f"(the disk image was left on disk)")
