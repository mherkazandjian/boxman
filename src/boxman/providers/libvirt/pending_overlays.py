"""
The overlays libvirt made of a VM's CD-ROM images that a restore left for
later removal (#273).

Reverting to an external snapshot, libvirt builds a qcow2 overlay of every
disk with media, CD-ROMs included, ``<image stem>.<epoch>`` beside the image.
A restore puts each CD-ROM back on its image at once, and rewrites every
snapshot record that names such an overlay; whether nothing else uses the
overlay, so that it may go, takes a scan of every domain on the host, which
costs more than the revert itself. So the restore records it here instead,
before the CD-ROM moves off it, and the overlay is removed later, behind the
same checks, by the next command that asks them for that VM: its next
``snapshot take`` (``SnapshotManager._flatten_cdrom_overlays``) or its
teardown. ``snapshot prune --dry-run`` lists what is waiting.

One record per chain -- the overlays a CD-ROM read, top first, and the
image under them -- in boxman's per-user cache dir, read when called. Each
record holds the identity, ``(st_dev, st_ino)``, of every overlay as it was
when recorded: a file that has taken the name since is not removed. A
record that cannot be read is named, and never removed. Each is kept under
its connection too (:func:`ledger_scope`): a VM of one name on another
libvirt is not this one.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass

from boxman import config_cache
from boxman.utils.io import mkstemp_in

#: the version of the record format
VERSION = 1

#: what a layer's identity is when it could not be read as it was recorded
UNREAD = None


def ledger_dir() -> str:
    """Where the records are: under boxman's per-user cache dir, read when
    called."""
    return os.path.join(os.path.expanduser(config_cache.DEFAULT_CACHE_DIR),
                        "pending-iso-overlays")


def ledger_scope(provider_config: dict | None) -> str:
    """
    Which libvirt a record is of: the runtime boxman reaches it through,
    the container for the docker-compose runtime, and the connection URI.
    Domain names are unique on one libvirt only.
    """
    config = provider_config or {}
    runtime = config.get("runtime") or "local"
    container = config.get("runtime_container")
    return " ".join(part for part in (
        runtime, container, config.get("uri") or "qemu:///system") if part)


def identity_of(path: str) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of *path* itself, a link not followed; None when
    it cannot be read."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


@dataclass
class PendingChain:
    """One record: the overlays a CD-ROM of *vm* read, top first, each with
    its identity as recorded, over *iso*."""

    vm: str
    iso: str
    layers: list[str]
    identities: list[tuple[int, int] | None]
    #: the record's file
    path: str
    #: when it was recorded, seconds since the epoch
    recorded: float = 0.0
    #: the libvirt it is of (:func:`ledger_scope`)
    scope: str = ""

    def identity(self, layer: str) -> tuple[int, int] | None:
        return self.identities[self.layers.index(layer)]


class PendingOverlays:
    """The records of the libvirt *scope* names (:func:`ledger_scope`), in
    *directory* (:func:`ledger_dir` when not given)."""

    def __init__(self, directory: str | None = None,
                 scope: str | None = None) -> None:
        self.directory = directory or ledger_dir()
        self.scope = scope if scope is not None else ledger_scope(None)
        #: ``{record file: why}`` for each record :meth:`chains` could not
        #: read, from its last call
        self.unreadable: dict[str, str] = {}

    def _path(self, vm: str, top: str) -> str:
        digest = hashlib.sha256(
            f"{self.scope}\0{vm}\0{top}".encode()).hexdigest()[:32]
        return os.path.join(self.directory, f"{digest}.json")

    def record(self, vm: str, chains: list[tuple[list[str], str]]) -> list[str]:
        """
        Record each of *chains* -- ``(overlays top first, the image under
        them)`` -- for *vm*, with the identity of each overlay read now; a
        layer that is not there is left out, and a chain left with none is
        not recorded. A record of the same top replaces the one there.

        Returns:
            The top of each chain recorded.

        Raises:
            OSError: a record could not be written. Those written before it
                stay.
        """
        os.makedirs(self.directory, exist_ok=True)
        written = []
        for layers, iso in chains:
            present = [layer for layer in layers if os.path.lexists(layer)]
            if not present:
                continue
            self._write(self._path(vm, present[0]), {
                "version": VERSION, "scope": self.scope, "vm": vm,
                "iso": iso, "layers": present,
                "identities": [list(i) if (i := identity_of(layer)) else UNREAD
                               for layer in present],
                "recorded": time.time()})
            written.append(present[0])
        return written

    def _write(self, path: str, data: dict) -> None:
        fd, staging = mkstemp_in(self.directory, prefix=".record.",
                                 suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(staging, path)
        except BaseException:
            try:
                os.unlink(staging)
            except OSError:
                pass
            raise
        self._sync_directory()

    def _sync_directory(self) -> None:
        """fsync the directory, so a rename or an unlink in it outlasts a
        crash of the host as the data does."""
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def forget(self, vm: str, tops) -> None:
        """Remove the record of each chain of *vm* topped by one of
        *tops*, where there is one."""
        removed = False
        for top in tops:
            try:
                os.unlink(self._path(vm, top))
                removed = True
            except FileNotFoundError:
                pass
        if removed:
            self._sync_directory()

    def chains(self, vm: str | None = None) -> list[PendingChain]:
        """Every record of *vm* on this libvirt, every record of this
        libvirt when it is None, oldest first. One that cannot be read is
        in :attr:`unreadable`."""
        self.unreadable = {}
        try:
            names = sorted(os.listdir(self.directory))
        except FileNotFoundError:
            return []
        except OSError as exc:
            self.unreadable[self.directory] = str(exc)
            return []
        found = []
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            path = os.path.join(self.directory, name)
            try:
                chain = self._read(path)
            except FileNotFoundError:
                continue        # settled meanwhile
            except (OSError, ValueError) as exc:
                self.unreadable[path] = str(exc)
                continue
            if chain.scope == self.scope and vm in (None, chain.vm):
                found.append(chain)
        return sorted(found, key=lambda c: (c.recorded, c.path))

    @staticmethod
    def _read(path: str) -> PendingChain:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("version") != VERSION:
            raise ValueError("not a record this boxman writes")
        vm, iso = data.get("vm"), data.get("iso")
        scope = data.get("scope")
        layers, identities = data.get("layers"), data.get("identities")
        if (not isinstance(vm, str) or not vm or not isinstance(scope, str)
                or not isinstance(iso, str)
                or not os.path.isabs(iso) or not isinstance(layers, list)
                or not layers or not isinstance(identities, list)
                or len(identities) != len(layers)
                or not all(isinstance(layer, str) and os.path.isabs(layer)
                           for layer in layers)):
            raise ValueError("its fields are not what a record holds")
        read: list[tuple[int, int] | None] = []
        for identity in identities:
            if identity is UNREAD:
                read.append(UNREAD)
            elif (isinstance(identity, list) and len(identity) == 2
                  and all(isinstance(n, int) and not isinstance(n, bool)
                          for n in identity)):
                read.append((identity[0], identity[1]))
            else:
                raise ValueError("an identity in it is not one")
        recorded = data.get("recorded")
        return PendingChain(
            vm=vm, iso=iso, layers=layers, identities=read, path=path,
            recorded=recorded if isinstance(recorded, (int, float)) else 0.0,
            scope=scope)

    def settle(self, vm: str) -> None:
        """
        Drop from each record of *vm* every overlay that is gone, or is no
        longer the file recorded -- another one has its name, which is not
        the restore's to remove -- and remove a record left with none.

        Raises:
            OSError: a record could not be rewritten or removed.
        """
        for chain in self.chains(vm):
            left = [(layer, identity) for layer, identity
                    in zip(chain.layers, chain.identities, strict=True)
                    if identity is not UNREAD
                    and identity_of(layer) == identity]
            if not left:
                try:
                    os.unlink(chain.path)
                except FileNotFoundError:
                    pass
                self._sync_directory()
            elif len(left) != len(chain.layers):
                self._write(chain.path, {
                    "version": VERSION, "scope": chain.scope,
                    "vm": chain.vm, "iso": chain.iso,
                    "layers": [layer for layer, _i in left],
                    "identities": [list(i) for _l, i in left],
                    "recorded": chain.recorded})
