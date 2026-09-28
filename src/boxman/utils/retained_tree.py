"""
Remove a directory tree except the files other domains use (#221).

``destroy`` scans the workspace for those files once the project's VMs are
gone (:func:`scan_tree`) and removes the rest later (:func:`remove_except`),
with the docker runtime's teardown and the generated-file cleanup in
between. The scan walks the tree through directory descriptors and records
the identity (``st_dev``, ``st_ino``) of the root, of every directory on the
way to a kept file, and of each kept file. The removal walks it again the
same way — each of those directories opened ``O_DIRECTORY | O_NOFOLLOW``
relative to its verified parent — and stops at the first one, or kept file,
that is no longer what the scan saw: a directory on the way renamed away
and replaced by a symlink can no longer lead the removal out of the tree,
or turn the kept file into something to delete (#221 review R2). Every
other entry is removed relative to its parent's descriptor, a directory
through descriptors all the way down, never following a symlink; Python
3.10 has no ``shutil.rmtree(dir_fd=...)``, so that walk is done here.

Neither the scan nor the removal crosses a mount point: every directory
either opens must be on the root's mount, told by its mount id — a bind
mount of a directory of the same filesystem shares its device and inode —
or the command stops, with nothing on the far side touched; and the
leftovers handed to the privileged fallback, whose ``rm -rf`` would cross
one too, must have no mount point at or under them (#221 review R2b).
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field

from boxman.exceptions import ProvisionError

#: (st_dev, st_ino)
Identity = tuple[int, int]

#: how every directory is opened: for listing, and never through a symlink
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

#: how many directories below the root the scan and the removal go: far
#: deeper than a workspace is. A tree deeper than this is refused — by the
#: scan before anything is removed, by the removal before it enters one —
#: with ProvisionError, instead of running out of stack or file
#: descriptors (each level holds one open) (#221 review N1).
MAX_DEPTH = 256

#: where the kernel says which mount an open file is on (``mnt_id``)
_FDINFO = "/proc/self/fdinfo/{fd}"
#: every mount of this process's mount namespace
_MOUNTINFO = "/proc/self/mountinfo"


def identity(st: os.stat_result) -> Identity:
    """The identity of the file *st* describes."""
    return (st.st_dev, st.st_ino)


def _mount_id(fd: int, shown: str) -> int:
    """
    The id of the mount the open directory *fd* (*shown*) is on: ``mnt_id``
    in ``/proc/self/fdinfo``. A bind mount of a directory of the same
    filesystem shares its device and its inode with it, but not its mount
    id, so this, not ``st_dev``, tells a mount point.

    Raises:
        ProvisionError: If it cannot be read: whether *shown* is a mount
            point cannot be told.
    """
    fdinfo = _FDINFO.format(fd=fd)
    try:
        with open(fdinfo) as fh:
            for line in fh:
                key, _, value = line.partition(":")
                if key == "mnt_id":
                    return int(value)
    except (OSError, ValueError) as exc:
        raise ProvisionError(
            f"could not tell whether {shown} is a mount point ({exc}), so "
            f"nothing more was removed from the workspace") from exc
    raise ProvisionError(
        f"could not tell whether {shown} is a mount point ({fdinfo} names no "
        f"mnt_id), so nothing more was removed from the workspace")


def _level(rel: str) -> int:
    """How many directories below the root *rel* is (the root: 0)."""
    return rel.count(os.sep) + 1 if rel else 0


def _too_deep(shown: str, root: str, removed: str) -> ProvisionError:
    return ProvisionError(
        f"{shown} is more than {MAX_DEPTH} directories below {root}, deeper "
        f"than the workspace's removal goes, so {removed} — remove it by "
        f"hand, then retry")


def _same_mount(root_mnt: int, fd: int, shown: str) -> None:
    """The open directory *fd* (*shown*) must be on the root's mount."""
    if _mount_id(fd, shown) != root_mnt:
        raise ProvisionError(
            f"{shown} is a mount point — a filesystem, or a bind mount, is "
            f"mounted there — and the workspace's removal never crosses one, "
            f"so nothing more was removed from the workspace: unmount it "
            f"first, then retry")


def _mount_points() -> list[str]:
    """
    Every mount point of this process's mount namespace, from
    ``/proc/self/mountinfo``, its octal escapes (``\\040`` for a blank, and
    so on) decoded.

    Raises:
        ProvisionError: If it cannot be read, or a line of it has no mount
            point field.
    """
    try:
        with open(_MOUNTINFO, "rb") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        raise ProvisionError(
            f"could not read {_MOUNTINFO} ({exc.strerror}) to tell whether "
            f"a filesystem is mounted in what is left of the workspace, so "
            f"the containerised fallback was not run") from exc
    points = []
    for line in lines:
        fields = line.split(b" ")
        if len(fields) < 5:
            raise ProvisionError(
                f"could not read {_MOUNTINFO} (a line of it names no mount "
                f"point) to tell whether a filesystem is mounted in what is "
                f"left of the workspace, so the containerised fallback was "
                f"not run")
        points.append(os.fsdecode(re.sub(
            rb"\\([0-7]{3})", lambda m: bytes([int(m.group(1), 8)]),
            fields[4])))
    return points


def _refuse_mounts(tree: RetainedTree, left: list[str]) -> None:
    """None of *left* — what the privileged fallback is about to ``rm -rf``
    — may be, or hold, a mount point: the container's bind mount of the
    root carries the mounts under it, and ``rm -rf`` crosses them."""
    points = _mount_points()
    for rel in left:
        path = tree.path(rel)
        for point in points:
            if point == path or point.startswith(path + os.sep):
                shown = path if point == path else f"{point}, inside {path}"
                raise ProvisionError(
                    f"a filesystem is mounted at {shown}, which the "
                    f"containerised fallback would have to remove: it never "
                    f"crosses a mount point, so nothing more was removed "
                    f"from the workspace — unmount it first, then retry")


class InUse:
    """
    What other domains use, as the host-wide in-use scan reports it
    (``LibVirtSession.disk_paths_in_use``): files by resolved path, and by
    identity, each mapped to the domain that uses it.
    """

    def __init__(self) -> None:
        #: resolved path -> domain
        self.by_path: dict[str, str] = {}
        #: identity -> domain
        self.by_identity: dict[Identity, str] = {}

    def add(self, found) -> None:
        """Merge one scan's answer (a ``FilesInUse``): the first domain
        named for a file keeps it."""
        for path, domain in found.items():
            self.by_path.setdefault(path, domain)
        for ident, domain in getattr(found, "identities", {}).items():
            self.by_identity.setdefault(ident, domain)

    def why_kept(self, path: str, target: Identity | None) -> str | None:
        """
        Why *path* must stay — which domain uses it, directly or as a
        backing file — or ``None`` when none does. *target* is the identity
        of the file it names, a symlink followed (``None`` when it names
        nothing): a hard link or a bind-mounted alias is known by it, and a
        symlink to such a file by its resolved path.
        """
        domain = self.by_path.get(os.path.realpath(path))
        if domain is None and target is not None:
            domain = self.by_identity.get(target)
        if domain is None:
            return None
        return f"domain {domain} uses it, directly or as a backing file"


@dataclass
class RetainedTree:
    """What :func:`scan_tree` found in a directory tree."""

    #: the tree's canonical path
    root: str
    #: its identity
    root_id: Identity
    #: the id of the mount it is on (:func:`_mount_id`)
    root_mnt: int
    #: each entry that stays, relative to :attr:`root` -> why
    kept: dict[str, str] = field(default_factory=dict)
    #: each of those entries -> its identity (a final symlink not followed)
    kept_ids: dict[str, Identity] = field(default_factory=dict)
    #: every directory on the way to one of them, relative to :attr:`root`
    #: (the root itself left out) -> its identity
    dirs: dict[str, Identity] = field(default_factory=dict)

    def path(self, rel: str) -> str:
        """The path of *rel*, an entry relative to :attr:`root`."""
        return os.path.join(self.root, rel) if rel else self.root

    def holds(self, path: str) -> bool:
        """Whether the entry *path* — the directory it is in resolved, its
        own name not, so a symlink in the tree counts — is in the tree."""
        entry = os.path.join(os.path.realpath(os.path.dirname(path)),
                             os.path.basename(path))
        return entry.startswith(self.root + os.sep)


def _open_dir(name: str, dir_fd: int | None, shown: str) -> int:
    """Open the directory *name* (relative to *dir_fd*) for listing, never
    through a symlink."""
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise ProvisionError(
            f"could not list {shown} ({exc.strerror}) to tell whether "
            f"another domain uses a file in it, so none of the workspace was "
            f"removed — make it readable") from exc


def scan_tree(root: str,
              why_kept: Callable[[str, Identity | None], str | None],
              ) -> RetainedTree:
    """
    Walk *root*, a canonical path to a directory, through directory
    descriptors, never following a symlink, and ask *why_kept* about every
    entry that is not a directory: its path, and the identity of the file it
    names, a symlink followed (``None`` when it names nothing). An entry it
    gives a reason for stays.

    Returns:
        What stays, and the identity of the root, of every directory on the
        way to what stays, and of each entry that stays.

    Raises:
        ProvisionError: If a directory cannot be opened or listed, is a
            mount point, or is more than :data:`MAX_DEPTH` directories below
            the root, or an entry's identity cannot be read other than for
            naming nothing: what is there could be any file.
    """
    fd = _open_dir(root, None, root)
    try:
        tree = RetainedTree(root, identity(os.fstat(fd)),
                            _mount_id(fd, root))
        walked: dict[str, Identity] = {}
        _scan(tree, fd, "", why_kept, walked)
    finally:
        os.close(fd)
    for rel in tree.kept:
        parent = os.path.dirname(rel)
        while parent:
            tree.dirs[parent] = walked[parent]
            parent = os.path.dirname(parent)
    return tree


def _scan(tree: RetainedTree, fd: int, rel: str,
          why_kept: Callable[[str, Identity | None], str | None],
          walked: dict[str, Identity]) -> None:
    """:func:`scan_tree` of the directory *fd*, *rel* under the root."""
    try:
        names = os.listdir(fd)
    except OSError as exc:
        raise ProvisionError(
            f"could not list {tree.path(rel)} ({exc.strerror}) to tell "
            f"whether another domain uses a file in it, so none of the "
            f"workspace was removed — make it readable") from exc
    for name in sorted(names):
        child = os.path.join(rel, name) if rel else name
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                target = None
            else:
                try:
                    target = identity(os.stat(name, dir_fd=fd))
                except FileNotFoundError:
                    target = None          # a symlink to nothing
        except FileNotFoundError:
            continue                       # gone since it was listed
        except OSError as exc:
            raise ProvisionError(
                f"could not read the identity of {tree.path(child)} "
                f"({exc.strerror}) to tell whether another domain uses it, "
                f"so none of the workspace was removed — make it "
                f"accessible") from exc
        if stat.S_ISDIR(st.st_mode):
            if _level(child) > MAX_DEPTH:
                raise _too_deep(tree.path(child), tree.root,
                                "none of the workspace was removed")
            sub = _open_dir(name, fd, tree.path(child))
            try:
                _same_mount(tree.root_mnt, sub, tree.path(child))
                walked[child] = identity(os.fstat(sub))
                _scan(tree, sub, child, why_kept, walked)
            finally:
                os.close(sub)
            continue
        why = why_kept(tree.path(child), target)
        if why:
            tree.kept[child] = why
            tree.kept_ids[child] = identity(st)


def remove_except(tree: RetainedTree,
                  fallback: Callable[[list[str]], None]) -> None:
    """
    Remove everything under ``tree.root`` except what it keeps and the
    directories on the way to it.

    The root, which must still be the directory scanned, and every
    directory on the way to a kept entry are opened relative to their
    verified parent, never through a symlink, and must still be the ones the
    scan saw; so must every kept entry. Every other entry of those
    directories is removed relative to that parent's descriptor
    (:func:`_remove`), each retained directory's only once its path is
    checked again (:func:`_still_in_place`); nothing that is the root, a
    directory on the way or a kept entry is removed or entered under any
    name. The tree is verified again once the removal is done, whether or
    not anything is left (#221 review R2a). What the user cannot remove
    (root-owned leftovers of the libvirt container) goes to *fallback*, as
    paths relative to the root, right after that; it is verified once more
    afterwards.

    Raises:
        ProvisionError: If a directory on the way or a kept entry is no
            longer what the scan saw — nothing more is removed then — or
            something is still there after *fallback*.
    """
    _sweep(tree, remove=True)
    # verified again even when nothing was left: a directory on the way, or
    # a kept file, moved while the removal ran is not what destroy says it
    # kept (#221 review R2a)
    left = _sweep(tree, remove=False)
    if left:
        _refuse_mounts(tree, left)
        fallback(left)
        left = _sweep(tree, remove=False)
    if left:
        raise ProvisionError(
            f"could not remove {', '.join(tree.path(rel) for rel in left)}: "
            f"still there after both the direct removal and the "
            f"containerised fallback")


def _depth(rel: str) -> int:
    return rel.count(os.sep)


def _open_verified(name: str, dir_fd: int | None, expected: Identity,
                   shown: str, root_mnt: int) -> int:
    """Open the directory *name* relative to *dir_fd*, never through a
    symlink, and check that it is still the one with identity *expected*,
    on the root's mount."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise ProvisionError(
            f"could not open {shown} again after the in-use scan "
            f"({exc.strerror}; a symlink is never followed), so nothing more "
            f"was removed from the workspace — check it") from exc
    try:
        if identity(os.fstat(fd)) != expected:
            raise ProvisionError(
                f"{shown} was replaced after the in-use scan, so nothing more "
                f"was removed from the workspace — check what replaced it")
        _same_mount(root_mnt, fd, shown)
    except ProvisionError:
        os.close(fd)
        raise
    return fd


def _check_kept(dir_fd: int, name: str, expected: Identity,
                shown: str) -> None:
    """The kept entry *name* must still be the one with identity
    *expected*."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        raise ProvisionError(
            f"{shown}, which another domain uses, could not be found again "
            f"after the in-use scan ({exc.strerror}), so nothing more was "
            f"removed from the workspace — check it") from exc
    if identity(st) != expected:
        raise ProvisionError(
            f"{shown}, which another domain uses, was replaced after the "
            f"in-use scan, so nothing more was removed from the workspace — "
            f"check it")


def _still_in_place(tree: RetainedTree, fds: dict[str, int],
                    rel: str) -> None:
    """
    Before the removal empties the retained directory *rel* through its
    descriptor, check again that its path still leads to it — the root, then
    every directory on the way, each relative to its parent's descriptor: a
    directory on the way moved or replaced while the removal ran stops it,
    with nothing more removed (#221 review R2a).
    """
    try:
        if identity(os.stat(tree.root, follow_symlinks=False)) != tree.root_id:
            raise FileNotFoundError(tree.root)
        parts = rel.split(os.sep)
        for depth in range(1, len(parts) + 1):
            here = os.sep.join(parts[:depth])
            st = os.stat(parts[depth - 1],
                         dir_fd=fds[os.path.dirname(here)],
                         follow_symlinks=False)
            if identity(st) != tree.dirs[here]:
                raise FileNotFoundError(here)
    except OSError as exc:
        raise ProvisionError(
            f"{tree.path(rel)}, or a directory above it, was moved or "
            f"replaced while the workspace was being removed, so nothing "
            f"more was removed from it — check it") from exc


def _doomed(tree: RetainedTree, fd: int, rel: str) -> list[str]:
    """The entries of the verified directory *fd* (*rel*) that go."""
    try:
        names = os.listdir(fd)
    except OSError as exc:
        raise ProvisionError(
            f"could not list {tree.path(rel)} ({exc.strerror}), which holds "
            f"a file another domain uses; nothing more was removed from "
            f"it") from exc
    return [name for name in sorted(names)
            if (os.path.join(rel, name) if rel else name) not in tree.kept
            and (os.path.join(rel, name) if rel else name) not in tree.dirs]


def _sweep(tree: RetainedTree, remove: bool) -> list[str]:
    """
    Verify the tree (:func:`remove_except`) and, when *remove*, remove what
    goes; return what goes and is still there, relative to the root.
    """
    fds: dict[str, int] = {}
    left: list[str] = []
    # never removed, nor entered, under any name (#221 review R2a)
    protected = frozenset({tree.root_id, *tree.dirs.values(),
                           *tree.kept_ids.values()})
    try:
        fds[""] = _open_verified(tree.root, None, tree.root_id, tree.root,
                                 tree.root_mnt)
        for rel in sorted(tree.dirs, key=_depth):
            fds[rel] = _open_verified(os.path.basename(rel),
                                      fds[os.path.dirname(rel)],
                                      tree.dirs[rel], tree.path(rel),
                                      tree.root_mnt)
        for rel, expected in tree.kept_ids.items():
            _check_kept(fds[os.path.dirname(rel)], os.path.basename(rel),
                        expected, tree.path(rel))
        for rel, fd in fds.items():
            if remove and rel:
                _still_in_place(tree, fds, rel)
            names = _doomed(tree, fd, rel)
            if remove and names:
                for name in names:
                    _remove(tree, protected, fd, name,
                            tree.path(os.path.join(rel, name)),
                            _level(rel) + 1)
                names = _doomed(tree, fd, rel)
            left.extend(os.path.join(rel, name) if rel else name
                        for name in names)
    finally:
        for fd in fds.values():
            os.close(fd)
    return left


def _remove(tree: RetainedTree, protected: frozenset[Identity],
            parent_fd: int, name: str, shown: str, level: int) -> None:
    """
    Remove the entry *name* (*shown*) of the directory *parent_fd*: anything
    but a directory is unlinked; a directory is opened relative to its
    parent, never through a symlink, emptied the same way and removed.
    Failures are not raised — what is left is listed again by the caller.

    An entry whose identity is in *protected* — the root, a directory on the
    way to a kept entry, a kept entry — is neither removed nor entered,
    whatever it is called now, and neither is a directory that is not, once
    opened, the one just looked up: renamed in between, it could be any of
    those (#221 review R2a). Left there, it fails the verification that
    follows.

    Raises:
        ProvisionError: If a directory it opens is on another mount than the
            root (:func:`_same_mount`), or is more than :data:`MAX_DEPTH`
            directories below it: it is never entered.
    """
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return
    if identity(st) in protected:
        return
    if not stat.S_ISDIR(st.st_mode):
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=parent_fd)
        return
    if level > MAX_DEPTH:
        raise _too_deep(shown, tree.root, "nothing more was removed from it")
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError:
        return
    try:
        if identity(os.fstat(fd)) != identity(st):
            return
        _same_mount(tree.root_mnt, fd, shown)
        for sub in os.listdir(fd):
            _remove(tree, protected, fd, sub, os.path.join(shown, sub),
                    level + 1)
    except OSError:
        pass
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.rmdir(name, dir_fd=parent_fd)
