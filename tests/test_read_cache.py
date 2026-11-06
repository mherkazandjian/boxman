"""
Within one operation, a read of a domain's snapshots or persistent
definition made again with nothing run in between is answered from the
first (:mod:`boxman.providers.libvirt.read_cache`), and anything that may
change it -- a revert, a redefine, a media change, any command boxman does
not know as a read -- drops every read kept.
"""

from __future__ import annotations

import os
import threading
from unittest.mock import patch

import pytest

from boxman.providers.libvirt import read_cache
from boxman.providers.libvirt.commands import LibVirtCommandBase, VirshCommand
from boxman.providers.libvirt.session import LibVirtSession
from boxman.providers.libvirt.snapshot import SnapshotManager
from conftest import domain_listing
from fake_libvirt_host import Result

pytestmark = pytest.mark.unit

VM = "vm01"


class Shell:
    """Answers every command, counting how often each ran; a command that
    is not a read moves :attr:`state` on, which every read answers with."""

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.state = 0
        self.lock = threading.Lock()
        #: command fragments that fail
        self.failing: set[str] = set()

    def __call__(self, command: str, **_kwargs) -> Result:
        with self.lock:
            self.ran.append(command)
            if any(part in command for part in self.failing):
                return Result(stderr="error: failed\n", code=1)
            words = command.split()
            sub = words[3] if words[0] == "virsh" else None
            if sub is None or read_cache.kind(sub, tuple(words[4:]),
                                              {}) == read_cache.CHANGE:
                self.state += 1
                return Result(stdout="done\n")
            return Result(stdout=f"{self.state}\n")

    def count(self, fragment: str) -> int:
        return sum(fragment in command for command in self.ran)


@pytest.fixture
def shell():
    fake = Shell()
    with patch("boxman.providers.libvirt.commands._shell_run",
               side_effect=fake):
        yield fake


def _virsh() -> VirshCommand:
    return VirshCommand(provider_config={})


class TestAReadIsKept:

    def test_the_same_read_again_is_answered_from_the_first(self, shell):
        virsh = _virsh()
        with read_cache.kept_here():
            first = virsh.execute("snapshot-list", VM, "--name", warn=True)
            again = virsh.execute("snapshot-list", VM, "--name", warn=True)

        assert again is first
        assert shell.count("snapshot-list") == 1

    @pytest.mark.parametrize("args", [
        ("snapshot-dumpxml", VM, "s1", "--security-info"),
        ("snapshot-current", VM),
        ("snapshot-info", VM, "s1"),
        ("dumpxml", VM, "--inactive"),
        ("domblklist", VM, "--details", "--inactive"),
    ])
    def test_every_read_of_snapshots_or_the_persistent_definition(
            self, shell, args):
        virsh = _virsh()
        with read_cache.kept_here():
            virsh.execute(*args, warn=True)
            virsh.execute(*args, warn=True)

        assert len(shell.ran) == 1

    def test_reads_that_only_read_keep_what_is_kept(self, shell):
        virsh = _virsh()
        with read_cache.kept_here():
            virsh.execute("snapshot-list", VM, "--name", warn=True)
            virsh.execute("list", "--name", warn=True)
            virsh.execute("dumpxml", VM, warn=True)
            virsh.execute("domstate", VM, warn=True)
            virsh.execute("snapshot-list", VM, "--name", warn=True)

        assert shell.count("snapshot-list") == 1

    def test_another_command_line_is_another_read(self, shell):
        virsh = _virsh()
        with read_cache.kept_here():
            virsh.execute("snapshot-dumpxml", VM, "s1", warn=True)
            virsh.execute("snapshot-dumpxml", VM, "s1", "--security-info",
                          warn=True)
            virsh.execute("snapshot-dumpxml", "vm02", "s1", warn=True)

        assert len(shell.ran) == 3


class TestNothingIsKeptThatCouldHaveChanged:

    @pytest.mark.parametrize("change", [
        ("snapshot-revert", VM, "s1"),
        ("snapshot-create", VM, "/tmp/s1.xml", "--redefine"),
        ("update-device", VM, "/tmp/cdrom.xml", "--live", "--config"),
        ("change-media", VM, "sda", "--eject"),
        ("suspend", VM),
        ("resume", VM),
        ("qemu-monitor-command", VM, "{}"),
        ("snapshot-delete", VM, "s1"),
        ("pool-refresh", "default"),
        ("some-subcommand-boxman-does-not-know", VM),
    ])
    def test_a_libvirt_change_drops_every_kept_read(self, shell, change):
        virsh = _virsh()
        with read_cache.kept_here():
            before = virsh.execute("snapshot-list", VM, "--name", warn=True)
            virsh.execute(*change, warn=True)
            after = virsh.execute("snapshot-list", VM, "--name", warn=True)

        assert shell.count("snapshot-list") == 2
        assert after.stdout != before.stdout

    @pytest.mark.parametrize("command", [
        "cp --reflink=auto -- a b", "mv -fT a b", "rm -f -- a",
        "qemu-img info a", "virsh -c qemu:///system snapshot-revert vm01 s1"])
    def test_so_does_any_shell_command(self, shell, command):
        virsh = _virsh()
        with read_cache.kept_here():
            virsh.execute("snapshot-list", VM, "--name", warn=True)
            LibVirtCommandBase(provider_config={}).execute_shell(command,
                                                                 warn=True)
            virsh.execute("snapshot-list", VM, "--name", warn=True)

        assert shell.count("snapshot-list") == 2

    def test_a_change_that_raises_drops_them_too(self, shell):
        virsh = _virsh()
        shell.failing.add("snapshot-revert")
        with read_cache.kept_here():
            virsh.execute("snapshot-list", VM, "--name", warn=True)
            with pytest.raises(RuntimeError):
                virsh.execute("snapshot-revert", VM, "s1")   # warn=False
            virsh.execute("snapshot-list", VM, "--name", warn=True)

        assert shell.count("snapshot-list") == 2

    def test_a_change_from_another_thread_drops_them(self, shell):
        virsh = _virsh()
        with read_cache.kept_here():
            virsh.execute("snapshot-list", VM, "--name", warn=True)
            other = threading.Thread(target=lambda: _virsh().execute(
                "suspend", VM, warn=True))
            other.start()
            other.join()
            virsh.execute("snapshot-list", VM, "--name", warn=True)

        assert shell.count("snapshot-list") == 2

    def test_the_live_definition_is_never_kept(self, shell):
        virsh = _virsh()
        with read_cache.kept_here():
            virsh.execute("dumpxml", VM, warn=True)
            virsh.execute("dumpxml", VM, warn=True)
            virsh.execute("domblklist", VM, "--details", warn=True)
            virsh.execute("domblklist", VM, "--details", warn=True)

        assert len(shell.ran) == 4

    def test_a_failed_read_is_read_again(self, shell):
        virsh = _virsh()
        shell.failing.add("snapshot-list")
        with read_cache.kept_here():
            virsh.execute("snapshot-list", VM, "--name", warn=True)
            shell.failing.clear()
            again = virsh.execute("snapshot-list", VM, "--name", warn=True)

        assert again.ok
        assert shell.count("snapshot-list") == 2

    def test_a_read_shown_as_it_runs_is_not_kept(self, shell):
        virsh = _virsh()
        with read_cache.kept_here():
            virsh.execute("snapshot-list", VM, "--name", hide=False,
                          warn=True)
            virsh.execute("snapshot-list", VM, "--name", hide=False,
                          warn=True)

        assert shell.count("snapshot-list") == 2

    def test_nothing_is_kept_outside_an_operation(self, shell):
        virsh = _virsh()
        virsh.execute("snapshot-list", VM, "--name", warn=True)
        virsh.execute("snapshot-list", VM, "--name", warn=True)
        with read_cache.kept_here():
            virsh.execute("snapshot-list", VM, "--name", warn=True)

        assert shell.count("snapshot-list") == 3

    def test_an_operation_forgets_its_reads_when_it_ends(self, shell):
        virsh = _virsh()
        with read_cache.kept_here():
            virsh.execute("snapshot-list", VM, "--name", warn=True)
        with read_cache.kept_here():
            virsh.execute("snapshot-list", VM, "--name", warn=True)

        assert shell.count("snapshot-list") == 2

    def test_another_thread_keeps_nothing_of_this_ones(self, shell):
        virsh = _virsh()
        with read_cache.kept_here():
            virsh.execute("snapshot-list", VM, "--name", warn=True)
            other = threading.Thread(target=lambda: _virsh().execute(
                "snapshot-list", VM, "--name", warn=True))
            other.start()
            other.join()

        assert shell.count("snapshot-list") == 2

    def test_a_child_process_answers_nothing_its_parent_kept(self, shell):
        virsh = _virsh()
        with read_cache.kept_here():
            virsh.execute("snapshot-list", VM, "--name", warn=True)
            pid = os.fork()
            if pid == 0:     # the child: is the read made again?
                before = len(shell.ran)
                virsh.execute("snapshot-list", VM, "--name", warn=True)
                os._exit(0 if len(shell.ran) == before + 1 else 1)
            _, status = os.waitpid(pid, 0)

        assert os.waitstatus_to_exitcode(status) == 0

    def test_kept_reads_never_answer_older_than_a_change_under_threads(
            self, shell):
        """16 threads, each in an operation of its own, read and change at
        once: a read answered from one kept is never older than the state
        seen just before it was asked for."""
        stale: list[tuple[int, int]] = []
        barrier = threading.Barrier(16)

        def work(n: int) -> None:
            virsh = _virsh()
            barrier.wait()
            with read_cache.kept_here():
                for i in range(200):
                    if (i + n) % 7 == 0:
                        virsh.execute("suspend", VM, warn=True)
                    seen = shell.state
                    answer = int(virsh.execute("snapshot-list", VM, "--name",
                                               warn=True).stdout)
                    if answer < seen:
                        stale.append((seen, answer))

        threads = [threading.Thread(target=work, args=(n,))
                   for n in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert stale == []
        assert shell.count("snapshot-list") < 16 * 200


class TestScansReadAfresh:
    """A host-wide scan decides what may be removed: none of its reads is
    answered from one kept, even in an operation that keeps them."""

    def test_a_scan_in_a_restore_reads_every_definition_again(self, shell):
        session = LibVirtSession(config={"provider": {"libvirt": {
            "in_use_scan_workers": 1}}})

        def run(command: str, **kwargs) -> Result:
            shell.ran.append(command)
            if " list " in command:     # one domain, not running
                return Result(stdout=domain_listing(
                    tuple(command.split()), VM) if "--all" in command
                    else "")
            if "domblklist" in command:
                return Result(stdout=" Type   Device   Target   Source\n"
                                     "-----------------------------\n")
            return Result(stdout="")

        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=run), read_cache.kept_here():
            _virsh().execute("domblklist", VM, "--details", "--inactive",
                             warn=True)
            assert session.disk_paths_in_use() == {}
            assert session.disk_paths_in_use() == {}

        assert shell.count("--inactive") == 3


class TestARestoreKeepsItsReads:

    def test_the_inventory_and_the_put_back_share_their_reads(self, shell):
        """After the revert, the backup's inventory and the CD-ROM put-back
        each read every snapshot: in one restore, once."""
        xml = ("<domainsnapshot><name>{0}</name>{1}<disks>"
               "<disk name='vda' snapshot='external'>"
               "<source file='/ws/vm01.{0}'/></disk></disks>"
               "</domainsnapshot>\n")

        def run(command: str, **kwargs) -> Result:
            shell.ran.append(command)
            if "snapshot-list" in command:
                return Result(stdout="s1\ns2\n\n")
            if "snapshot-dumpxml" in command:
                name = command.split()[5]
                parent = ("<parent><name>s1</name></parent>"
                          if name == "s2" else "")
                return Result(stdout=xml.format(name, parent))
            return Result(stdout="")

        manager = SnapshotManager({})
        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=run), read_cache.kept_here():
            _overlays, order = manager._strict_overlay_inventory(VM)
            xmls = manager._snapshot_xmls(VM)

        assert order == ["s1", "s2"]
        assert set(xmls) == {"s1", "s2"}
        assert shell.count("snapshot-list") == 1
        assert shell.count("snapshot-dumpxml") == 2

    def test_a_restore_keeps_its_reads_and_forgets_them_after(self, shell):
        session = LibVirtSession(config={"provider": {"libvirt": {}}})

        def restore(manager, vm_name, snapshot_name, paths_in_use=None):
            manager.virsh.execute("snapshot-list", vm_name, "--name",
                                  warn=True)
            manager.virsh.execute("snapshot-list", vm_name, "--name",
                                  warn=True)
            return False

        with patch.object(SnapshotManager, "snapshot_restore", restore):
            session.snapshot_restore(VM, "s1")
        _virsh().execute("snapshot-list", VM, "--name", warn=True)

        assert shell.count("snapshot-list") == 2
