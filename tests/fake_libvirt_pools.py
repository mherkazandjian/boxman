"""
A stand-in for ``virsh`` (a :class:`VirshCommand`'s ``execute``) that knows
the storage pools and domains of one libvirt host, for #277: what retiring
the pool on a removed directory reads and runs, answered as libvirt 10
answers it.

- ``pool-list`` takes ``--all``, ``--transient`` and ``--name`` or
  ``--uuid``; under ``--all`` a name is padded with blanks.
- ``pool-dumpxml``, ``pool-destroy`` and ``pool-undefine`` take a name or a
  UUID; destroying a transient pool makes it go.
- ``list --all --uuid --name``, ``list --uuid`` and ``dumpxml [--inactive]``
  describe the domains, each with the pools its volume disks name.

Every call is recorded, in order, in :attr:`FakePools.calls`, and in
:data:`ISSUED` across every fake, which the tests check for ``pool-delete``
-- a verb this fake does not know, and which raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

#: every call any fake received since the last :func:`reset`
ISSUED: list[tuple[str, ...]] = []


def reset() -> None:
    ISSUED.clear()


@dataclass
class FakePool:
    uuid: str
    target: str | None
    type: str = "dir"
    active: bool = True
    persistent: bool = True


@dataclass
class FakeDomain:
    uuid: str
    running: bool = False
    #: the pools its persistent definition's volume disks name
    pools: list[str] = field(default_factory=list)
    #: the pools only its live definition names (a disk attached live)
    live_pools: list[str] = field(default_factory=list)


def _result(stdout: str = "", ok: bool = True, stderr: str = ""):
    return MagicMock(stdout=stdout, ok=ok, stderr=stderr)


class FakePools:
    """See the module docstring."""

    def __init__(self) -> None:
        self.pools: dict[str, FakePool] = {}
        self.domains: dict[str, FakeDomain] = {}
        self.calls: list[tuple[str, ...]] = []
        #: the start of a command (``"pool-destroy"``, ``"pool-list --all
        #: --transient"``) -> the stderr every command so begun fails with
        self.failing: dict[str, str] = {}
        #: called with each command before it is answered
        self.before = None
        #: uuid -> name of every pool undefined (or stopped, if transient)
        self._gone: dict[str, str] = {}

    def define(self, name: str, target: str | None, **kwargs) -> FakePool:
        uuid = f"00000000-0000-4000-8000-{len(self.pools) + 1:012d}"
        self.pools[name] = FakePool(uuid=uuid, target=target, **kwargs)
        return self.pools[name]

    def domain(self, name: str, *pools: str, running: bool = False,
               live_pools: tuple = ()) -> None:
        uuid = f"11111111-0000-4000-8000-{len(self.domains) + 1:012d}"
        self.domains[name] = FakeDomain(uuid=uuid, running=running,
                                        pools=list(pools),
                                        live_pools=list(live_pools))

    def verbs(self, *verbs: str) -> list[tuple[str, ...]]:
        """The calls of *verbs*, in order."""
        return [c for c in self.calls if c[0] in verbs]

    def retired(self) -> list[str]:
        """The pools a ``pool-destroy`` or ``pool-undefine`` was issued
        for, by name, once each, in order."""
        by_uuid = {p.uuid: n for n, p in self.pools.items()}
        by_uuid.update(self._gone)
        names = []
        for call in self.verbs("pool-destroy", "pool-undefine"):
            name = by_uuid.get(call[1], call[1])
            if name not in names:
                names.append(name)
        return names

    def _pool(self, ref: str) -> tuple[str, FakePool] | None:
        for name, pool in self.pools.items():
            if ref in (name, pool.uuid):
                return name, pool
        return None

    def execute(self, verb: str, *args: str, warn: bool = False, **kwargs):
        call = (verb, *args)
        self.calls.append(call)
        ISSUED.append(call)
        if self.before is not None:
            self.before(call)
        line = " ".join(call)
        for start, stderr in self.failing.items():
            if line == start or line.startswith(f"{start} "):
                return _result(ok=False, stderr=stderr)
        handler = {
            "pool-list": self._pool_list,
            "pool-dumpxml": self._pool_dumpxml,
            "pool-destroy": self._pool_destroy,
            "pool-undefine": self._pool_undefine,
            "list": self._list,
            "dumpxml": self._dumpxml,
        }.get(verb)
        if handler is None:
            raise AssertionError(f"the fake virsh does not know {call}")
        return handler(args)

    def _pool_list(self, args):
        every = "--all" in args
        rows = []
        for name, pool in self.pools.items():
            if not (every or pool.active):
                continue
            if "--transient" in args and pool.persistent:
                continue
            if "--uuid" in args:
                rows.append(pool.uuid)
            else:
                rows.append(f"{name:<20}" if every else name)
        return _result(stdout="".join(f"{row}\n" for row in rows) + "\n")

    def _pool_dumpxml(self, args):
        found = self._pool(args[-1])
        if found is None:
            return _result(ok=False, stderr=(
                f"error: failed to get pool '{args[-1]}'\nerror: Storage "
                f"pool not found: no storage pool with matching name or uuid"))
        name, pool = found
        target = ("" if pool.target is None else
                  f"  <target>\n    <path>{pool.target}</path>\n  </target>\n")
        return _result(stdout=(
            f"<pool type='{pool.type}'>\n  <name>{name}</name>\n"
            f"  <uuid>{pool.uuid}</uuid>\n{target}</pool>\n"))

    def _pool_destroy(self, args):
        found = self._pool(args[0])
        if found is None or not found[1].active:
            return _result(ok=False, stderr="error: pool is not active")
        name, pool = found
        pool.active = False
        if not pool.persistent:
            self._gone[pool.uuid] = name
            del self.pools[name]
        return _result(stdout=f"Pool {name} destroyed\n\n")

    def _pool_undefine(self, args):
        found = self._pool(args[0])
        if found is None:
            return _result(ok=False, stderr="error: no such pool")
        name, pool = found
        if pool.active:
            return _result(ok=False, stderr="error: pool is still active")
        self._gone[pool.uuid] = name
        del self.pools[name]
        return _result(stdout=f"Pool {name} has been undefined\n\n")

    def _list(self, args):
        if "--name" in args:
            rows = [f"{d.uuid} {name}" for name, d in self.domains.items()]
        else:
            rows = [d.uuid for d in self.domains.values() if d.running]
        return _result(stdout="".join(f"{row}\n" for row in rows) + "\n")

    def _dumpxml(self, args):
        domain = next((d for d in self.domains.values()
                       if d.uuid == args[-1]), None)
        if domain is None:
            return _result(ok=False, stderr="error: domain not found")
        pools = list(domain.pools)
        if "--inactive" not in args:
            pools += domain.live_pools
        disks = "".join(
            f"<disk type='volume' device='disk'><source pool='{pool}' "
            f"volume='vda.qcow2'/><target dev='vd{chr(97 + i)}'/></disk>"
            for i, pool in enumerate(pools))
        return _result(stdout=(
            f"<domain type='kvm'><uuid>{domain.uuid}</uuid><devices>"
            f"<disk type='file' device='disk'><source file='/x.qcow2'/>"
            f"</disk>{disks}</devices></domain>"))
