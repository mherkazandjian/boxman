"""
Validation and extraction of the tar streams that ``docker cp`` produces.

The docker-compose runtime uses these to move libvirt's own state
(``/etc/libvirt`` and ``/var/lib/libvirt/qemu``) out of a container's
writable layer and onto the host before the container is recreated
(#164 FB-2).

The reason this is a module rather than three lines at the call site is
that **an archive which extracts cleanly is not evidence that the copy
completed**. GNU tar tolerates a missing end-of-archive marker, so a
stream cut between two complete members extracts without complaint and
silently drops every member after the cut — the exact shape of a
``docker cp`` interrupted by a full disk or a killed daemon.

Nor can the archive's *tail* answer the question: a member whose payload
ends in zeros contributes 1,024 zero bytes of its own, and after
truncation those sit at the tail looking precisely like the two zero
blocks that terminate a POSIX archive.

So the check is made at **header positions**. The archive is walked
member by member — each header's ``size`` field says how much payload to
skip, so trailing zeros inside a member are stepped over rather than
inspected — and the two zero blocks are required at the position where
the next header would have begun. Payload is never examined.

The walk itself is delegated to :mod:`tarfile`, which already implements
the format's extensions correctly: GNU base-256 sizes, PAX extended
headers whose ``size`` keyword overrides the ustar field of the member
that follows, and the GNU long-name entries that carry a payload of their
own. Re-deriving that by hand is how a validator ends up mis-parsing the
one archive it was written to catch.
"""

import inspect
import os
import posixpath
import tarfile

from boxman.exceptions import ProvisionError

#: Size of a tar record, in bytes. Every header starts at a multiple of it.
BLOCK_SIZE = 512

#: An archive ends with two zero-filled records.
TERMINATOR_SIZE = BLOCK_SIZE * 2

#: ``extractall`` gained a ``filter`` keyword in 3.12 (backported to later
#: 3.10/3.11 patch releases), and in 3.14 it defaults to ``"data"``. That
#: default is wrong here: these archives legitimately carry absolute
#: symlinks — ``/etc/libvirt/qemu/networks/autostart/default.xml`` points at
#: an absolute path in the image — and the ``data`` and ``tar`` filters both
#: reject those, which would turn a valid migration into a failure.
#: :func:`_checked_members` performs the containment check that matters.
_EXTRACT_KWARGS: dict = {}
if "filter" in inspect.signature(tarfile.TarFile.extractall).parameters:
    _EXTRACT_KWARGS["filter"] = "fully_trusted"


class ArchiveError(ProvisionError):
    """An archive is truncated, corrupt, or names a path outside its root."""


def _normalize(name: str) -> str:
    """Return *name* as a clean relative POSIX path, or '' if it is empty."""
    cleaned = posixpath.normpath(name.strip("/")) if name.strip("/") else ""
    return "" if cleaned in (".", "") else cleaned


def _checked_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """
    Return every member of *tar*, refusing any that would write outside
    the extraction root.

    Absolute paths and ``..`` components are rejected. Symlink and hardlink
    *targets* are deliberately not restricted — see ``_EXTRACT_KWARGS``.
    """
    members = []
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/") or posixpath.isabs(name):
            raise ArchiveError(
                f"archive member has an absolute path: {name!r}")
        if ".." in posixpath.normpath(name).split("/"):
            raise ArchiveError(
                f"archive member escapes its root: {name!r}")
        if member.issparse():
            # docker cp does not emit sparse members; one here means the
            # archive is not the shape this walk assumes, so the terminator
            # position cannot be computed. Fail closed.
            raise ArchiveError(
                f"archive member is sparse, which this reader cannot "
                f"validate: {name!r}")
        members.append(member)
    return members


def read_archive(archive_path: str) -> list[tarfile.TarInfo]:
    """
    Walk *archive_path* at header positions and return its members.

    Raises :class:`ArchiveError` when the archive is truncated (in a member's
    payload, or between two complete members), corrupt, or missing its
    end-of-archive marker.
    """
    try:
        with tarfile.open(archive_path, "r:") as tar:
            members = _checked_members(tar)
    except ArchiveError:
        raise
    except tarfile.TarError as exc:
        # ReadError covers both a corrupt header and a payload that ends
        # before the size field said it would.
        raise ArchiveError(
            f"{archive_path} is not a readable tar archive: {exc}") from exc
    except OSError as exc:
        raise ArchiveError(
            f"cannot read {archive_path}: {exc}") from exc

    _assert_terminated(archive_path, members)
    return members


def _assert_terminated(archive_path: str,
                       members: list[tarfile.TarInfo]) -> None:
    """
    Require the two zero blocks at the position where the header after the
    last member would have begun.

    This is the check that catches a cut between complete members, which
    :mod:`tarfile` accepts in silence.
    """
    if members:
        last = members[-1]
        payload_blocks = (last.size + BLOCK_SIZE - 1) // BLOCK_SIZE
        end = last.offset_data + payload_blocks * BLOCK_SIZE
    else:
        end = 0

    try:
        size = os.path.getsize(archive_path)
        with open(archive_path, "rb") as fobj:
            fobj.seek(end)
            tail = fobj.read(TERMINATOR_SIZE)
    except OSError as exc:
        raise ArchiveError(
            f"cannot read {archive_path}: {exc}") from exc

    if len(tail) < TERMINATOR_SIZE:
        raise ArchiveError(
            f"{archive_path} is truncated: {len(members)} member(s) end at "
            f"byte {end} of a {size}-byte archive, leaving no room for the "
            f"end-of-archive marker — the copy did not complete")
    if tail != b"\0" * TERMINATOR_SIZE:
        raise ArchiveError(
            f"{archive_path} has no end-of-archive marker at byte {end}, "
            f"where the header after its last member would start — the "
            f"archive is truncated or corrupt")


def extract_archive(archive_path: str, dest_dir: str) -> list[str]:
    """
    Validate *archive_path*, extract it into *dest_dir*, and confirm every
    declared member reached the disk. Returns the extracted relative paths.

    *dest_dir* must not already exist: a pre-existing tree would make the
    "did every member arrive?" comparison meaningless, since leftovers from
    an earlier attempt are indistinguishable from members of this one.

    Ownership is restored only when running as root; :mod:`tarfile` skips
    the ``chown`` otherwise. That is the right behaviour here — the
    destination is a host directory under the user's ``.boxman``, and
    libvirtd runs as root inside the container, so it can read what the
    user's uid owns either way.
    """
    if os.path.exists(dest_dir):
        raise ArchiveError(
            f"extraction target already exists: {dest_dir}")

    members = read_archive(archive_path)
    declared = {n for n in (_normalize(m.name) for m in members) if n}

    os.makedirs(dest_dir)
    try:
        with tarfile.open(archive_path, "r:") as tar:
            tar.extractall(dest_dir, members=members, **_EXTRACT_KWARGS)
    except (tarfile.TarError, OSError) as exc:
        raise ArchiveError(
            f"failed to extract {archive_path} into {dest_dir}: "
            f"{exc}") from exc

    extracted = _walk_relative(dest_dir)

    missing = sorted(declared - extracted)
    if missing:
        shown = ", ".join(missing[:5])
        more = f" (and {len(missing) - 5} more)" if len(missing) > 5 else ""
        raise ArchiveError(
            f"{len(missing)} of {len(declared)} archive member(s) did not "
            f"reach {dest_dir}: {shown}{more}")

    # Extras can only be the parent directories tarfile creates implicitly
    # for a member whose own parent was not declared. An extra *file* means
    # something other than this extraction wrote into the target.
    for extra in sorted(extracted - declared):
        full = os.path.join(dest_dir, extra)
        if not os.path.isdir(full) or os.path.islink(full):
            raise ArchiveError(
                f"{dest_dir} holds {extra!r}, which the archive does not "
                f"declare — the target was not clean")

    return sorted(extracted)


def _walk_relative(root: str) -> set[str]:
    """
    Every path under *root*, relative to it, without following symlinks.

    ``os.walk`` ignores unreadable directories by default, which would make
    this under-report and turn "could not read the directory" into a
    "members are missing" verdict. The error is raised instead, so the
    caller says what actually went wrong. (Reaching it takes a source
    directory with no owner-execute bit, which neither ``/etc/libvirt`` nor
    ``/var/lib/libvirt/qemu`` has; refusing is still the safe direction.)
    """
    def _raise(exc: OSError) -> None:
        raise ArchiveError(
            f"cannot verify the extracted tree, {exc.filename} is not "
            f"readable: {exc}") from exc

    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=_raise):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            found.add(os.path.relpath(full, root))
    return found
