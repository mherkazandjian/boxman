"""
The host-wide in-use scan names what made it fail, and proves a deleted
disk absent through sudo where it read it through sudo (#294, finding 3 of
the real-host test pass).

A teardown keeps every file once the scan fails, which is right, but it
used to say only that it "could not check whether another domain uses" each
of its own files: never which domain, which file, or why. On a shared host
another user's domain is the usual cause -- its disk in a home directory
the boxman user may not search (no ``use_sudo``), or, with ``use_sudo``, a
disk deleted while the domain stayed defined, which the absence probe, run
without sudo, could not prove gone.

What these check: the warning names the domain, the source and the error,
once per cause at default verbosity; with ``use_sudo`` a source whose read
failed through sudo is probed for absence through sudo too, and a deleted
disk then uses nothing; without it, nothing runs through sudo and the scan
still fails closed; a shut-off domain is never skipped for being shut off;
a foreign disk that is built on a workspace file still protects it; and a
clean scan warns about nothing.
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import shlex
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from boxman.providers.libvirt import session as session_module
from boxman.providers.libvirt.session import _ABSENT, LibVirtSession, _ChainReads
from conftest import domain_listing

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _nothing_named_yet():
    """Each test starts as a fresh command: no cause named yet."""
    session_module._NAMED.clear()
    yield
    session_module._NAMED.clear()

HEADER = (" Type   Device   Target   Source\n"
          "------------------------------------\n")


def _result(stdout: str = "", ok: bool = True, stderr: str = "",
            return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout, r.stderr, r.ok, r.failed = stdout, stderr, ok, not ok
    r.return_code = return_code if ok else (return_code or 1)
    return r


def _session(provider: dict) -> LibVirtSession:
    return LibVirtSession(config={"provider": {"libvirt": provider}})


def _blk(*disks: str) -> str:
    return HEADER + "".join(
        f" file   disk     vd{chr(ord('a') + i)}      {disk}\n"
        for i, disk in enumerate(disks))


def _virsh(domains: dict[str, list[str]], running=(), calls=None):
    """A stand-in for ``VirshCommand(...).execute``: *domains* maps each
    domain to its disks (the same live and persistent), *running* lists
    the ones that run; every call is appended to *calls*."""
    def execute(*args, **kwargs):
        if calls is not None:
            calls.append(args)
        if args[0] == "list":
            if "--all" in args:
                return _result(stdout=domain_listing(args, *domains))
            return _result(stdout="".join(f"{d}\n" for d in running))
        if args[0] == "domblklist":
            return _result(stdout=_blk(*domains[args[1]]))
        if args[0] == "domstate":
            return _result(stdout=("running" if args[1] in running
                                   else "shut off") + "\n")
        if args[0] == "vol-pool":
            return _result(ok=False, stderr="error: no storage vol")
        if args[0] == "pool-list":
            return _result(stdout="")
        raise AssertionError(f"unexpected virsh call: {args}")
    return execute


def _absence_probe_path(command: str) -> tuple[bool, str]:
    """``(through sudo, path probed)`` of an absence probe."""
    words = shlex.split(command)
    if words[:4] == ["sudo", "-n", "sh", "-c"]:
        return True, shlex.split(words[4])[3]
    return False, words[3]


class FakeShell:
    """
    The shell every command wrapper runs through (``_shell_run``).

    *chains* maps a source to the JSON ``qemu-img`` prints for it; any
    other source fails with *errors*' text for it, else "No such file or
    directory". *absent* lists the paths the plain absence probe proves
    absent, *absent_as_root* the ones the probe through sudo does.
    """

    def __init__(self, chains=None, errors=None, absent=(),
                 absent_as_root=(), stat_as_root=None, unreadable=()):
        self.unreadable = set(unreadable)
        self.chains = chains or {}
        self.errors = errors or {}
        self.absent = set(absent)
        self.absent_as_root = set(absent_as_root)
        self.stat_as_root = stat_as_root or {}
        self.commands: list[str] = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if _ABSENT in command:
            as_root, path = _absence_probe_path(command)
            proved = path in (self.absent_as_root if as_root else self.absent)
            return _result(stdout=_ABSENT if proved else "")
        if "boxman-source-unreadable" in command:
            path = shlex.split(command)[3]
            return _result(stdout="boxman-source-unreadable"
                           if path in self.unreadable else "")
        if command.startswith("sudo -n stat "):
            path = shlex.split(command)[-1]
            ok, out, err = self.stat_as_root.get(
                path, (False, "", f"stat: cannot statx '{path}': No such "
                                  f"file or directory"))
            return _result(ok=ok, stdout=out, stderr=err)
        if "qemu-img info" in command:
            path = shlex.split(command)[-1]
            if path in self.chains:
                return _result(stdout=self.chains[path])
            why = self.errors.get(path, "No such file or directory")
            return _result(ok=False, stderr=(
                f"qemu-img: Could not open '{path}': Could not open "
                f"'{path}': {why}"))
        raise AssertionError(f"unexpected shell command: {command}")

    def sudo_probes(self) -> list[str]:
        return [c for c in self.commands
                if c.startswith("sudo -n sh -c") and _ABSENT in c]


def _scan(provider, domains, shell, running=(), times=1, calls=None):
    """Run the host-wide scan *times* on one session, every shell command
    answered by *shell*; the last answer."""
    session = _session(provider)
    with patch("boxman.providers.libvirt.session.VirshCommand") as virsh, \
         patch("boxman.providers.libvirt.commands._shell_run",
               side_effect=shell):
        virsh.return_value.execute.side_effect = _virsh(
            domains, running, calls)
        for _ in range(times):
            answer = session.disk_paths_in_use()
    return answer


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING]


FOREIGN = "/home/other/ws/c1/foreign01.qcow2"
OWN = "/srv/mine/ws/c1/node01.qcow2"
OWN_CHAIN = f'{{"filename": "{OWN}"}}'


class TestUnreadableWithoutSudo:
    """No ``use_sudo``: another user's disk in a directory the boxman user
    may not search. The scan fails closed, as before, and now says on
    which domain and file, and why."""

    def _run(self, caplog, times=1):
        shell = FakeShell(chains={OWN: OWN_CHAIN},
                          errors={FOREIGN: "Permission denied"})
        calls = []
        answer = _scan({}, {"mine01": [OWN], "foreign01": [FOREIGN]},
                       shell, times=times, calls=calls)
        return answer, shell, calls

    def test_fails_closed_naming_domain_source_and_error(self, captured_logs):
        answer, _, _ = self._run(captured_logs)
        assert answer is None
        (warning,) = _warnings(captured_logs)
        assert "domain foreign01" in warning
        assert FOREIGN in warning
        assert "Permission denied" in warning
        assert "could not be proved absent as this user" in warning
        assert "use_sudo: true" in warning

    def test_a_file_that_is_there_is_not_called_deleted(self, captured_logs):
        """A 0600 disk of another user's: the probe finds it there, and
        libvirt, asked instead (#221), lists it in no pool. The warning
        says so, and does not suggest the disk was deleted."""
        shell = FakeShell(chains={OWN: OWN_CHAIN},
                          errors={FOREIGN: "Permission denied"},
                          unreadable={FOREIGN})
        answer = _scan({}, {"mine01": [OWN], "foreign01": [FOREIGN]}, shell)
        assert answer is None
        (warning,) = _warnings(captured_logs)
        assert "domain foreign01" in warning and FOREIGN in warning
        assert "the file is there" in warning
        assert "libvirt lists it in no storage pool" in warning
        assert "use_sudo: true reads it" in warning
        assert "deleted" not in warning and "stale" not in warning

    def test_nothing_runs_through_sudo(self, captured_logs):
        _, shell, _ = self._run(captured_logs)
        assert not [c for c in shell.commands if c.startswith("sudo")]

    def test_a_shut_off_domain_is_not_skipped(self, captured_logs):
        """foreign01 is shut off (not listed as running) and still fails
        the scan: shut off, it uses its disks on its next start. Its state
        is not even asked while its source is not proved absent."""
        answer, _, calls = self._run(captured_logs)
        assert answer is None
        assert not [c for c in calls if c[0] == "domstate"]

    def test_named_once_however_often_the_command_scans(self, captured_logs):
        """A teardown scans per VM and for the workspace; the cause is
        warned about once, the repeats go to the debug log."""
        answer, _, _ = self._run(captured_logs, times=3)
        assert answer is None
        assert len(_warnings(captured_logs)) == 1
        repeats = [r for r in captured_logs.records
                   if r.levelno == logging.DEBUG and FOREIGN in r.getMessage()
                   and "could not tell which files" in r.getMessage()]
        assert len(repeats) == 2


class TestDeletedDiskWithSudo:
    """``use_sudo: true``: a shut-off domain of another user whose disk was
    deleted from that user's 0700 home. ``sudo qemu-img`` cannot open it,
    and the plain absence probe cannot search the home."""

    PROVIDER = {"use_sudo": True}

    def test_proved_absent_through_sudo_it_uses_nothing(self, captured_logs):
        shell = FakeShell(chains={OWN: OWN_CHAIN}, absent_as_root={FOREIGN})
        calls = []
        answer = _scan(self.PROVIDER,
                       {"mine01": [OWN], "foreign01": [FOREIGN]},
                       shell, calls=calls)
        assert answer is not None
        assert set(answer.reasons([OWN])) == {OWN}
        assert FOREIGN not in answer
        assert _warnings(captured_logs) == []
        # the plain probe first, then the same probe as root, -n
        probes = [c for c in shell.commands if _ABSENT in c]
        assert [_absence_probe_path(c) for c in probes] == [
            (False, FOREIGN), (True, FOREIGN)]
        assert probes[1].startswith("sudo -n sh -c ")
        # absence proved, its state was asked: a running one could hold it
        assert ("domstate", "foreign01") in calls

    def test_not_proved_even_as_root_fails_closed_named(self, captured_logs):
        shell = FakeShell(chains={OWN: OWN_CHAIN})
        answer = _scan(self.PROVIDER,
                       {"mine01": [OWN], "foreign01": [FOREIGN]}, shell)
        assert answer is None
        (warning,) = _warnings(captured_logs)
        assert "domain foreign01" in warning and FOREIGN in warning
        assert "sudo qemu-img info" in warning
        assert "No such file or directory" in warning
        assert "even through sudo -n" in warning

    def test_no_sudo_probe_when_the_plain_one_proves_it(self, captured_logs):
        shell = FakeShell(chains={OWN: OWN_CHAIN}, absent={FOREIGN})
        answer = _scan(self.PROVIDER,
                       {"mine01": [OWN], "foreign01": [FOREIGN]}, shell)
        assert answer is not None
        assert shell.sudo_probes() == []

    def test_a_running_domain_still_fails_on_what_it_may_hold(
            self, captured_logs):
        """Proved absent, a running domain can still hold the unlinked
        image open: its live chain decides, as before (here, unreadable)."""
        shell = FakeShell(chains={OWN: OWN_CHAIN}, absent_as_root={FOREIGN})
        session = _session(self.PROVIDER)
        domains = {"mine01": [OWN], "foreign01": [FOREIGN]}
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh, \
             patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=shell):
            plain = _virsh(domains, running=("foreign01",))

            def execute(*args, **kwargs):
                if args[0] == "dumpxml":
                    return _result(ok=False, stderr="error: failed")
                return plain(*args, **kwargs)
            virsh.return_value.execute.side_effect = execute
            assert session.disk_paths_in_use() is None
        assert any("foreign01" in w and FOREIGN in w
                   for w in _warnings(captured_logs))

    def test_without_sudo_the_same_deleted_disk_is_not_proved(
            self, captured_logs):
        """The probe as root only where qemu-img ran as root."""
        shell = FakeShell(chains={OWN: OWN_CHAIN}, absent_as_root={FOREIGN})
        answer = _scan({}, {"mine01": [OWN], "foreign01": [FOREIGN]}, shell)
        assert answer is None
        assert shell.sudo_probes() == []


class TestIdentityOfAMissingFileThroughSudo:
    """``snapshot prune`` and collapse read the identity of every file a
    snapshot names; one under another user's 0700 home is read through
    ``sudo -n stat``. Gone, it names no file once the probe as root proves
    it -- never from ``stat``'s error text."""

    @pytest.fixture
    def hidden(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root searches past mode 000")
        home = tmp_path / "home"
        home.mkdir()
        home.chmod(0)
        yield str(home / "vm_snapshot_s1.raw.zst")
        home.chmod(0o700)

    def _identify(self, path, shell):
        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=shell):
            return _session({"use_sudo": True})._identify(path)

    def test_proved_absent_it_names_no_file(self, hidden):
        shell = FakeShell(absent_as_root={hidden})
        assert self._identify(hidden, shell) is None
        assert shell.sudo_probes()

    def test_not_proved_it_cannot_be_told(self, hidden):
        with pytest.raises(OSError, match="No such file or directory"):
            self._identify(hidden, FakeShell())

    def test_a_file_root_can_stat_is_identified(self, hidden):
        shell = FakeShell(stat_as_root={hidden: (True, "64769 1234", "")})
        assert self._identify(hidden, shell) == (hidden, (64769, 1234))
        assert shell.sudo_probes() == []


class TestChainsThroughSudo:
    """The other readers of a chain probe through sudo the same way: a
    teardown's own disks (:meth:`backing_chains`) and what a snapshot names
    (``_chain_or_none``)."""

    def test_backing_chains_leaves_out_a_disk_absent_for_root(self):
        shell = FakeShell(chains={OWN: OWN_CHAIN}, absent_as_root={FOREIGN})
        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=shell):
            chains = _session({"use_sudo": True}).backing_chains(
                [OWN, FOREIGN])
        assert chains == {OWN: [OWN]}

    def test_chain_or_none_of_a_file_absent_for_root(self):
        shell = FakeShell(absent_as_root={FOREIGN})
        provider = {"use_sudo": True}
        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=shell):
            session = _session(provider)
            assert session._chain_or_none(_ChainReads(provider),
                                          FOREIGN) == []

    def test_chain_or_none_unproved_names_why(self):
        shell = FakeShell()
        provider = {"use_sudo": True}
        with patch("boxman.providers.libvirt.commands._shell_run",
                   side_effect=shell):
            reads = _ChainReads(provider)
            assert _session(provider)._chain_or_none(reads, FOREIGN) is None
        assert "No such file or directory" in reads.why(FOREIGN)


@pytest.mark.skipif(shutil.which("qemu-img") is None, reason="needs qemu-img")
class TestOnRealFiles:
    """Real files, real ``qemu-img``, the real probes in a real shell; only
    ``virsh`` is stood in for. No sudo."""

    @staticmethod
    def _image(path, backing=None):
        cmd = ["qemu-img", "create", "-q", "-f", "qcow2"]
        if backing:
            cmd += ["-b", str(backing), "-F", "qcow2"]
        subprocess.run([*cmd, str(path), "1M"], check=True)
        return str(path)

    def _scan(self, domains):
        session = _session({})
        with patch("boxman.providers.libvirt.session.VirshCommand") as virsh:
            virsh.return_value.execute.side_effect = _virsh(domains)
            return session.disk_paths_in_use()

    @pytest.fixture
    def layout(self, tmp_path):
        ws, other = tmp_path / "ws", tmp_path / "other"
        ws.mkdir()
        other.mkdir()
        base = self._image(ws / "base.qcow2")
        own = self._image(ws / "node01.qcow2")
        overlay = self._image(other / "foreign01.qcow2", backing=base)
        return ws, other, base, own, overlay

    def test_a_clean_scan_names_nothing(self, layout, captured_logs):
        ws, other, base, own, overlay = layout
        answer = self._scan({"mine01": [own], "foreign01": [overlay]})
        assert answer is not None
        assert _warnings(captured_logs) == []
        assert set(answer.reasons([own])) == {own}

    def test_a_foreign_disk_built_on_a_workspace_file_protects_it(
            self, layout, captured_logs):
        """foreign01's overlay, in another directory, is built on a file
        in this workspace: the teardown must keep that file. Shut off, it
        counts all the same."""
        ws, other, base, own, overlay = layout
        answer = self._scan({"foreign01": [overlay]})
        assert answer is not None
        reasons = answer.reasons([base, own])
        assert set(reasons) == {base}
        assert "foreign01" in reasons[base]
        assert _warnings(captured_logs) == []

    def test_a_deleted_disk_in_a_searchable_directory_uses_nothing(
            self, layout, captured_logs):
        ws, other, base, own, overlay = layout
        gone = str(other / "deleted01.qcow2")
        answer = self._scan({"foreign01": [overlay], "stale01": [gone]})
        assert answer is not None
        assert base in answer.reasons([base])
        assert _warnings(captured_logs) == []

    def test_a_disk_in_a_directory_it_may_not_search_fails_named(
            self, layout, captured_logs):
        if os.geteuid() == 0:
            pytest.skip("root searches past mode 000")
        ws, other, base, own, overlay = layout
        locked = other / "home"
        locked.mkdir()
        hidden = str(locked / "deleted01.qcow2")
        locked.chmod(0)
        try:
            answer = self._scan({"foreign01": [overlay], "stale01": [hidden]})
        finally:
            locked.chmod(0o700)
        assert answer is None
        (warning,) = _warnings(captured_logs)
        assert "domain stale01" in warning and hidden in warning
        assert "Permission denied" in warning
        assert "use_sudo: true" in warning


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(),
                    reason="needs the fork start method")
def test_a_cause_a_forked_worker_named_is_not_named_again(captured_logs):
    """``deprovision`` and ``destroy`` tear each VM down in a forked worker
    (``_run_parallel``), then remove the workspace in the parent, each with
    its own scan: the cause one of them named is not warned about again."""
    message = "could not tell which files domain foreign01 uses: test"
    ctx = multiprocessing.get_context("fork")
    worker = ctx.Process(target=session_module._NAMED.first, args=(message,))
    worker.start()
    worker.join(10)
    assert worker.exitcode == 0
    _session({})._name_blocker(message)
    assert _warnings(captured_logs) == []
    assert any(r.getMessage() == message for r in captured_logs.records)
    _session({})._name_blocker(message + " (another cause)")
    assert len(_warnings(captured_logs)) == 1
