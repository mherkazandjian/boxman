"""
A stateful stand-in for a libvirt host, at the level boxman's command
wrappers hand a command to the shell
(``boxman.providers.libvirt.commands._shell_run``), for #221.

It answers what a backing-chain read and the host-wide in-use scan run --
``qemu-img info --backing-chain``, the shell probes, and ``virsh``'s
``vol-pool`` / ``pool-refresh`` / ``vol-dumpxml`` / ``list`` /
``domblklist`` / ``domstate`` / ``dumpxml`` -- the way libvirt 10.0 answers
on the test runner, where the output shapes below were captured:

- libvirt creates pool volumes (every clone disk) mode 0600, owned by
  ``root`` or ``libvirt-qemu``, so ``qemu-img`` run as the user cannot open
  them ("Permission denied"), while ``virsh`` reads them through libvirtd,
  as root;
- a pool describes a file as it was at the pool's last refresh:
  ``vol-dumpxml`` still shows the old backing file after an out-of-band
  rebase until ``pool-refresh``, and a file created since is no volume at
  all (``vol-pool`` and ``vol-dumpxml`` fail) until then;
- ``vol-dumpxml`` describes one level: the volume's ``<target>`` (path,
  format) and, when it has one, a ``<backingStore>`` (absolute path,
  format).

A command wrapped as ``docker exec --user root <container> bash -c '...'``
or prefixed with ``sudo`` runs as root, which reads every file unless
:attr:`FakeHost.root_reads` is off (a container root without
``CAP_DAC_OVERRIDE``). Every command is logged, in order, in
:attr:`FakeHost.log` as ``(identity, what, args)``.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, replace

from conftest import domain_listing


@dataclass
class Image:
    """A file on the fake host."""

    #: what its header says it is ("iso" is what libvirt calls an ISO image)
    format: str = "qcow2"
    #: the absolute path of its backing file, if it has one
    backing: str | None = None
    #: whether the user may read it (root reads it regardless)
    readable: bool = False
    #: its header is damaged: nobody can open it with qemu-img
    corrupt: bool = False


class Result:
    """What ``invoke`` returns for a command that ran."""

    def __init__(self, stdout: str = "", stderr: str = "", code: int = 0):
        self.stdout, self.stderr, self.return_code = stdout, stderr, code
        self.ok = code == 0
        self.failed = not self.ok


class FakeHost:
    """See the module docstring."""

    def __init__(self) -> None:
        #: path -> the file there
        self.files: dict[str, Image] = {}
        #: pool name -> its target directory
        self.pools: dict[str, str] = {}
        #: pool name -> {path: the file as the pool last saw it}
        self.listed: dict[str, dict[str, Image]] = {}
        #: domain name -> its ``domblklist --details`` rows
        #: ``(type, device, target, source)``, both definitions alike
        self.domains: dict[str, list[tuple[str, str, str, str]]] = {}
        #: the domains that run
        self.running: set[str] = set()
        #: virsh subcommands that fail, printing nothing
        self.fail: set[str] = set()
        #: virsh subcommands that print their answer, then exit non-zero
        self.fail_after_answering: set[str] = set()
        #: path -> what ``vol-dumpxml`` prints for it instead of its XML
        self.dumpxml_override: dict[str, str] = {}
        #: path -> what ``vol-pool`` prints for it instead of its pool
        self.vol_pool_override: dict[str, str] = {}
        #: whether root reads every file (see the module docstring)
        self.root_reads = True
        #: whether ``sudo`` is refused (a password would be needed)
        self.sudo_refused = False
        #: every command, in order: (identity, what, args)
        self.log: list[tuple[str, str, tuple]] = []
        #: the full command lines, as handed to the shell
        self.commands: list[str] = []

    # -- building the host ----------------------------------------------------

    def add(self, path: str, **image) -> str:
        self.files[path] = Image(**image)
        return path

    def define_pool(self, name: str, target: str) -> None:
        """A pool on *target*, listing what is there now."""
        self.pools[name] = target
        self.refresh(name)

    def refresh(self, name: str) -> None:
        target = self.pools[name]
        self.listed[name] = {path: replace(image)
                             for path, image in self.files.items()
                             if os.path.dirname(path) == target}

    def define_domain(self, name: str, *rows: tuple[str, str, str, str],
                      running: bool = False) -> None:
        self.domains[name] = list(rows)
        if running:
            self.running.add(name)

    # -- what the log shows -----------------------------------------------------

    def asked(self, what: str) -> list[tuple]:
        """The args of every *what* (a virsh subcommand, ``qemu-img``,
        ``unreadable-probe`` or ``absence-probe``), in order."""
        return [args for _, done, args in self.log if done == what]

    def identities(self, what: str) -> set[str]:
        """Who ran *what*: ``user`` and/or ``root``."""
        return {who for who, done, _ in self.log if done == what}

    def order(self) -> list[str]:
        """What ran, in order."""
        return [done for _, done, _ in self.log]

    # -- the shell --------------------------------------------------------------

    def run(self, command: str, **_kwargs) -> Result:
        self.commands.append(command)
        root = False
        if command.startswith("docker exec "):
            command = shlex.split(command)[-1]
            root = True
        if command.startswith("sudo "):
            if self.sudo_refused:
                return Result(stderr="sudo: a password is required\n", code=1)
            command = command[len("sudo "):]
            root = True
        who = "root" if root else "user"
        argv = shlex.split(command)
        if argv[0] == "if" and "exit" in argv:
            return self._absence_probe(who, argv)
        if argv[0] == "if":
            return self._probe(who, argv)
        if argv[0] == "qemu-img":
            self.log.append((who, "qemu-img", (argv[-1],)))
            return self._qemu_img(root, argv[-1])
        if argv[0] == "virsh":
            sub, args = argv[3], tuple(argv[4:])
            self.log.append((who, sub, args))
            if sub in self.fail:
                return Result(stderr=f"error: {sub} failed\n", code=1)
            result = getattr(self, "_" + sub.replace("-", "_"))(args)
            if sub in self.fail_after_answering:
                result = Result(stdout=result.stdout,
                                stderr=f"error: {sub} failed late\n", code=1)
            return result
        raise AssertionError(f"the fake host cannot run: {command}")

    def _reads(self, root: bool, image: Image) -> bool:
        return image.readable or (root and self.root_reads)

    @staticmethod
    def _echoed(argv: list[str]) -> str:
        return argv[argv.index("echo") + 1].rstrip(";")

    def _test(self, root: bool, operator: str, path: str) -> bool:
        """``[ <operator> <path> ]`` on this host."""
        image = self.files.get(path)
        if operator == "-e":
            return image is not None
        if operator == "-r":
            return image is not None and self._reads(root, image)
        raise AssertionError(f"the fake host has no test {operator}")

    def _probe(self, who: str, argv: list[str]) -> Result:
        """``if [ ... ] && [ ! ... ] ...; then echo M; fi``, evaluated: each
        bracketed test of a path on this host, negated by ``!``, joined by
        ``&&`` / ``||`` left to right; logged as ``unreadable-probe`` of the
        first path it tests."""
        words = [word.rstrip(";") for word in argv[1:argv.index("then")]]
        value, joiner, paths = None, None, []
        while words:
            close = words.index("]")
            test, words = words[1:close], words[close + 1:]
            negate = test[0] == "!"
            operator, path = test[-2], test[-1]
            paths.append(path)
            result = self._test(who == "root", operator, path) != negate
            value = (result if joiner is None
                     else value and result if joiner == "&&"
                     else value or result)
            if words:
                joiner, words = words[0], words[1:]
        self.log.append((who, "unreadable-probe", (paths[0],)))
        return Result(stdout=self._echoed(argv) + "\n" if value else "")

    def _absence_probe(self, who: str, argv: list[str]) -> Result:
        """The absence probe: prints its marker when the path is not there
        (every directory on the way searchable)."""
        path = argv[3]
        self.log.append((who, "absence-probe", (path,)))
        return Result(stdout="" if path in self.files
                      else self._echoed(argv) + "\n")

    def _qemu_img(self, root: bool, source: str) -> Result:
        chain, path = [], source
        while path is not None:
            image = self.files.get(path)
            why = ("No such file or directory" if image is None
                   else "Permission denied"
                   if not self._reads(root, image) else None)
            if why:
                return Result(stderr=f"qemu-img: Could not open '{source}': "
                                     f"Could not open '{path}': {why}\n",
                              code=1)
            if image.corrupt:
                return Result(stderr=f"qemu-img: Could not open '{source}': "
                                     f"Image is not in qcow2 format\n", code=1)
            entry = {"filename": path, "format": image.format,
                     "virtual-size": 16777216}
            if image.backing:
                entry.update({"backing-filename": image.backing,
                              "full-backing-filename": image.backing,
                              "backing-filename-format": "qcow2"})
            chain.append(entry)
            path = image.backing
        return Result(stdout=json.dumps(chain, indent=4) + "\n")

    # -- virsh ----------------------------------------------------------------

    def _pool_listing(self, path: str) -> str | None:
        return next((name for name, listed in self.listed.items()
                     if path in listed), None)

    @staticmethod
    def _not_a_volume(path: str, hint: str = "") -> Result:
        return Result(
            stdout="\n",
            stderr=f"error: failed to get vol '{path}'{hint}\n"
                   f"error: Storage volume not found: no storage vol with "
                   f"matching path '{path}'\n", code=1)

    def _vol_pool(self, args: tuple) -> Result:
        path = args[0]
        if path in self.vol_pool_override:
            return Result(stdout=self.vol_pool_override[path])
        pool = self._pool_listing(path)
        if pool is None:
            return self._not_a_volume(path)
        return Result(stdout=f"{pool}\n\n")

    def _pool_refresh(self, args: tuple) -> Result:
        name = args[0]
        if name not in self.pools:
            return Result(stdout="\n",
                          stderr=f"error: failed to get pool '{name}'\n"
                                 f"error: Storage pool not found: no storage "
                                 f"pool with matching name '{name}'\n", code=1)
        self.refresh(name)
        return Result(stdout=f"Pool {name} refreshed\n\n")

    @staticmethod
    def _permissions(mode: str, owner: int, group: int, indent: str) -> str:
        return (f"{indent}<permissions>\n"
                f"{indent}  <mode>{mode}</mode>\n"
                f"{indent}  <owner>{owner}</owner>\n"
                f"{indent}  <group>{group}</group>\n"
                f"{indent}</permissions>\n")

    def _vol_dumpxml(self, args: tuple) -> Result:
        path = args[0]
        if path in self.dumpxml_override:
            return Result(stdout=self.dumpxml_override[path])
        pool = self._pool_listing(path)
        if pool is None:
            return self._not_a_volume(path, ", specifying --pool might help\n")
        image = self.listed[pool][path]
        mode, owner, group = (("0644", 1001, 110) if image.readable
                              else ("0600", 0, 0))
        xml = ("<volume type='file'>\n"
               f"  <name>{os.path.basename(path)}</name>\n"
               f"  <key>{path}</key>\n"
               "  <capacity unit='bytes'>16777216</capacity>\n"
               "  <allocation unit='bytes'>200704</allocation>\n"
               "  <physical unit='bytes'>196616</physical>\n"
               "  <target>\n"
               f"    <path>{path}</path>\n"
               f"    <format type='{image.format}'/>\n"
               + self._permissions(mode, owner, group, "    ")
               + "    <timestamps>\n"
               "      <atime>1790607458.391851323</atime>\n"
               "      <mtime>1790607458.324851356</mtime>\n"
               "      <ctime>1790607458.324851356</ctime>\n"
               "      <btime>0</btime>\n"
               "    </timestamps>\n"
               "  </target>\n")
        if image.backing:
            xml += ("  <backingStore>\n"
                    f"    <path>{image.backing}</path>\n"
                    "    <format type='qcow2'/>\n")
            if image.backing in self.files:
                xml += self._permissions("0600", 0, 0, "    ")
            xml += "  </backingStore>\n"
        return Result(stdout=xml + "</volume>\n\n")

    def _list(self, args: tuple) -> Result:
        names = (sorted(self.domains) if "--all" in args
                 else sorted(self.running))
        return Result(stdout=domain_listing(args, *names) + "\n")

    def _domblklist(self, args: tuple) -> Result:
        rows = self.domains.get(args[0])
        if rows is None:
            return Result(stderr=f"error: failed to get domain '{args[0]}'\n",
                          code=1)
        table = (" Type   Device   Target   Source\n"
                 "------------------------------------\n")
        table += "".join(f" {kind}   {device}   {target}   {source}\n"
                         for kind, device, target, source in rows)
        return Result(stdout=table + "\n")

    def _domstate(self, args: tuple) -> Result:
        if args[0] not in self.domains:
            return Result(stderr=f"error: failed to get domain '{args[0]}'\n",
                          code=1)
        return Result(stdout=("running" if args[0] in self.running
                              else "shut off") + "\n\n")

    def _dumpxml(self, args: tuple) -> Result:
        rows = self.domains.get(args[0])
        if rows is None:
            return Result(stderr=f"error: failed to get domain '{args[0]}'\n",
                          code=1)
        disks = "".join(
            f"<disk type='{kind}' device='{device}'>"
            f"<source file='{source}'/><target dev='{target}'/></disk>"
            for kind, device, target, source in rows)
        return Result(stdout=f"<domain><name>{args[0]}</name><devices>"
                             f"{disks}</devices></domain>\n")
