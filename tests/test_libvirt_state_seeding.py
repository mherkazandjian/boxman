"""
Seeding of the bind-mounted libvirt state directories (#205).

``containers/docker/entrypoint.sh`` restores what ``/etc/libvirt`` and
``/var/lib/libvirt/qemu`` lack from the image's pristine copy, through
``seed-libvirt-state.sh``. It used to seed only a directory mounted empty,
so one holding just the ``nwfilter/`` and ``secrets/`` libvirtd creates for
itself counted as seeded; the missing ``qemu/networks/default.xml`` then
killed the entrypoint and the container restart-looped for good.

The script is plain bash over two directory arguments, so it runs here
against temp trees; the docker-level behaviour is covered by
``tests/test_docker_compose.py`` in the integration tier.

The review of the first fix added the rest: a copy cut short must never be
published, an existing path of the wrong kind must be reported rather than
accepted, a path appearing mid-restore must not be overwritten, and when
the entrypoint gives up, boxman's readiness wait must say why at once.
"""

from __future__ import annotations

import os
import re
import resource
import shutil
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

DOCKER_DIR = Path(__file__).resolve().parents[1] / "containers" / "docker"
SEED_SCRIPT = DOCKER_DIR / "seed-libvirt-state.sh"

if shutil.which("bash") is None:  # pragma: no cover - every CI host has it
    pytest.skip("the seeding script needs bash", allow_module_level=True)


#: mtime given to every pristine file, so a copy that drops it shows
PRISTINE_MTIME = 1_577_836_800  # 2020-01-01

#: File the script puts in each staging directory it makes, and without
#: which it never removes one
STAGING_MARKER = ".boxman-seed-staging"


def _make_pristine(root: Path) -> Path:
    """A small stand-in for the image's /etc/libvirt, with the shapes that
    matter: nested files, an empty 0700 directory, and the absolute
    autostart symlink the Dockerfile creates (dangling outside the image).
    The root is 0750 and the files carry an old mtime, so metadata that
    does not survive seeding shows up."""
    pristine = root / "pristine" / "etc-libvirt"
    (pristine / "nwfilter").mkdir(parents=True)
    pristine.chmod(0o750)
    (pristine / "secrets").mkdir()
    (pristine / "secrets").chmod(0o700)
    (pristine / "qemu" / "networks" / "autostart").mkdir(parents=True)
    (pristine / "libvirtd.conf").write_text('auth_unix_rw = "none"\n')
    (pristine / "libvirtd.conf").chmod(0o600)
    (pristine / "qemu.conf").write_text('user = "root"\n')
    (pristine / "nwfilter" / "clean-traffic.xml").write_text(
        "<filter name='clean-traffic'/>\n")
    (pristine / "qemu" / "networks" / "default.xml").write_text(
        "<network><name>default</name></network>\n")
    os.symlink("/etc/libvirt/qemu/networks/default.xml",
               pristine / "qemu" / "networks" / "autostart" / "default.xml")
    for path in pristine.rglob("*"):
        if path.is_file() and not path.is_symlink():
            os.utime(path, (PRISTINE_MTIME, PRISTINE_MTIME))
    return pristine


def _tree(root: Path) -> dict[str, tuple]:
    """Everything under *root*: type, mode, and content or link target."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            st = path.lstat()
            if path.is_symlink():
                out[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                out[rel] = ("dir", st.st_mode & 0o7777)
            else:
                out[rel] = ("file", st.st_mode & 0o7777, path.read_bytes())
    return out


def _stamps(root: Path) -> dict[str, tuple]:
    """Identity of every entry, to prove nothing was replaced or rewritten.

    A directory's mtime is left out: restoring a file into it changes that,
    and should."""
    stamps = {}
    for rel in _tree(root):
        st = os.lstat(root / rel)
        is_dir = (root / rel).is_dir() and not (root / rel).is_symlink()
        stamps[rel] = (st.st_ino, None if is_dir else st.st_mtime_ns)
    return stamps


def _seed(target: Path, pristine: Path, *, env: dict | None = None,
          fsize_limit: int | None = None) -> subprocess.CompletedProcess:
    """Run the script. *fsize_limit* caps the size of any file it or its
    children write (RLIMIT_FSIZE), which cuts a copy short the way a full
    disk does."""
    def _limit():  # runs in the child, before exec
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_limit, fsize_limit))

    return subprocess.run(
        ["bash", str(SEED_SCRIPT), str(target), str(pristine)],
        capture_output=True, text=True, timeout=30, env=env,
        preexec_fn=_limit if fsize_limit is not None else None)


def _staging_left_in(root: Path) -> list[str]:
    """Anything the script stages under, left behind."""
    return sorted(str(p.relative_to(root))
                  for p in root.rglob(".boxman-seed.*"))


def _shim(tmp_path: Path, name: str, body: str) -> dict:
    """An environment whose PATH puts a wrapper around *name* first.

    The wrapper runs *body* and then execs the real *name*, so a test can
    make something happen at the exact moment the script calls it."""
    real = shutil.which(name)
    bindir = tmp_path / f"shim-{name}"   # one per command, so shims never mix
    bindir.mkdir(exist_ok=True)
    shim = bindir / name
    shim.write_text(f"#!/bin/bash\n{body}\nexec {real} \"$@\"\n")
    shim.chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}


class TestSeedLibvirtState:

    def test_the_partial_tree_from_the_issue_is_completed(self, tmp_path):
        """The state #205 was found in: libvirtd's own nwfilter/ and secrets/,
        and nothing else. Seeding must not read that as initialised."""
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        (target / "nwfilter").mkdir(parents=True)
        (target / "secrets").mkdir()
        mine = target / "nwfilter" / "site-filter.xml"
        mine.write_text("<filter name='site'/>\n")
        before = _stamps(target)

        result = _seed(target, pristine)

        assert result.returncode == 0, result.stderr
        assert (target / "qemu" / "networks" / "default.xml").read_text() == (
            "<network><name>default</name></network>\n")
        assert (target / "libvirtd.conf").is_file()
        assert (target / "nwfilter" / "clean-traffic.xml").is_file()
        # what the volume already held is exactly as it was
        assert mine.read_text() == "<filter name='site'/>\n"
        after = _stamps(target)
        assert {rel: after[rel] for rel in before} == before
        # and the log names what came back — a missing directory once, not
        # once per file inside it
        header, *restored = result.stdout.splitlines()
        assert header.startswith(f"Restored 4 path(s) missing from {target}")
        assert {line.strip() for line in restored} == {
            "libvirtd.conf", "qemu.conf", "qemu", "nwfilter/clean-traffic.xml"}

    def test_an_empty_directory_gets_the_whole_pristine_tree(self, tmp_path):
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        target.mkdir()

        result = _seed(target, pristine)

        assert result.returncode == 0, result.stderr
        assert _tree(target) == _tree(pristine)
        assert result.stdout.strip() == (
            f"Seeded {target} from the image's pristine copy")
        # file metadata survives staging and publication, not just modes
        for path in target.rglob("*"):
            if path.is_file() and not path.is_symlink():
                assert path.stat().st_mtime == PRISTINE_MTIME, path
        assert _staging_left_in(target) == []

    def test_a_missing_directory_is_created_and_seeded(self, tmp_path):
        """Including the root itself, which gets the pristine root's mode
        rather than whatever the umask gives."""
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "does" / "not" / "exist"

        result = _seed(target, pristine)

        assert result.returncode == 0, result.stderr
        assert _tree(target) == _tree(pristine)
        assert target.stat().st_mode & 0o7777 == 0o750

    def test_a_complete_directory_is_left_alone(self, tmp_path):
        """Every path present, several with local changes: nothing is
        copied, rewritten or reported."""
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        shutil.copytree(pristine, target, symlinks=True)
        (target / "qemu.conf").write_text('user = "someone-else"\n')
        (target / "qemu" / "networks" / "default.xml").write_text(
            "<network><name>default</name><uuid>x</uuid></network>\n")
        before_tree, before_stamps = _tree(target), _stamps(target)

        result = _seed(target, pristine)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "" and result.stderr == ""
        assert _tree(target) == before_tree
        assert _stamps(target) == before_stamps

    def test_a_single_missing_file_is_restored_beside_existing_ones(
            self, tmp_path):
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        shutil.copytree(pristine, target, symlinks=True)
        (target / "qemu" / "networks" / "default.xml").unlink()
        (target / "libvirtd.conf").write_text("# edited locally\n")

        result = _seed(target, pristine)

        assert result.returncode == 0, result.stderr
        assert (target / "qemu" / "networks" / "default.xml").is_file()
        assert (target / "libvirtd.conf").read_text() == "# edited locally\n"
        restored = [line.strip() for line in result.stdout.splitlines()[1:]]
        assert restored == ["qemu/networks/default.xml"]

    def test_symlinks_are_restored_as_symlinks(self, tmp_path):
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        target.mkdir()
        (target / "secrets").mkdir()

        assert _seed(target, pristine).returncode == 0
        link = target / "qemu" / "networks" / "autostart" / "default.xml"
        assert link.is_symlink()
        assert os.readlink(link) == "/etc/libvirt/qemu/networks/default.xml"

    def test_a_blocked_directory_is_reported_once_and_the_rest_restored(
            self, tmp_path):
        """A file where the image has a populated directory blocks everything
        below it. It is kept, reported once — not once per entry it blocks —
        with a non-zero exit, and everything else is still restored."""
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        (target / "qemu").mkdir(parents=True)
        (target / "qemu" / "networks").write_text("not a directory\n")

        result = _seed(target, pristine)

        assert result.returncode == 1
        assert (target / "qemu" / "networks").read_text() == (
            "not a directory\n")
        assert (target / "libvirtd.conf").is_file()
        assert (target / "secrets").is_dir()
        assert ("qemu/networks: a regular file where the image has a "
                "directory") in result.stderr
        assert "default.xml" not in result.stderr
        assert "autostart" not in result.stderr

    def test_a_missing_pristine_copy_fails_and_says_to_rebuild(self, tmp_path):
        target = tmp_path / "etc-libvirt"
        target.mkdir()

        result = _seed(target, tmp_path / "no-such-stash")

        assert result.returncode == 1
        assert "rebuilt" in result.stderr
        assert list(target.iterdir()) == []

    def test_wrong_usage_exits_2(self, tmp_path):
        result = subprocess.run(
            ["bash", str(SEED_SCRIPT), str(tmp_path)],
            capture_output=True, text=True, timeout=30)
        assert result.returncode == 2
        assert "usage:" in result.stderr


class TestAFailedCopyIsNeverAccepted:
    """Review finding 1: a copy written straight to its final path and cut
    short — a full disk, a container stopped mid-copy — left a truncated file
    that every later start took for the real thing."""

    @pytest.mark.parametrize("rel", [
        "libvirtd.conf",               # a file in a directory that exists
        "qemu/networks/default.xml",   # one inside a directory still missing
    ])
    def test_a_cut_short_copy_publishes_nothing_and_a_retry_restores_it(
            self, tmp_path, rel):
        pristine = _make_pristine(tmp_path)
        content = "x" * 8191 + "\n"
        (pristine / rel).write_text(content)
        target = tmp_path / "etc-libvirt"
        target.mkdir()

        first = _seed(target, pristine, fsize_limit=1024)

        assert first.returncode == 1
        assert rel in first.stderr
        assert not (target / rel).exists(), (
            "a partial copy was published at the final path")
        assert _staging_left_in(target) == []
        # everything that did fit is in place
        assert (target / "qemu.conf").is_file()

        second = _seed(target, pristine)

        assert second.returncode == 0, second.stderr
        assert (target / rel).read_text() == content
        assert _tree(target) == _tree(pristine)
        assert _staging_left_in(target) == []

    @staticmethod
    def _kill_mid_copy(tmp_path, pristine, target):
        """Run the script and SIGKILL it inside the copy of libvirtd.conf,
        as a container stopped mid-seed would; return the staging left."""
        env = _shim(tmp_path, "cp", (
            f'if [ "${{@: -2:1}}" = "{pristine / "libvirtd.conf"}" ]; then\n'
            f'    printf "auth_unix" > "${{@: -1}}"\n'
            f'    kill -KILL 0\n'
            f'fi'))

        killed = subprocess.run(
            ["bash", str(SEED_SCRIPT), str(target), str(pristine)],
            capture_output=True, text=True, timeout=30, env=env,
            start_new_session=True)   # so `kill 0` stops at the script

        assert killed.returncode == -9
        assert not (target / "libvirtd.conf").exists()
        leftover = _staging_left_in(target)
        assert len(leftover) == 1
        stale = target / leftover[0]
        assert sorted(p.name for p in stale.iterdir()) == [
            STAGING_MARKER, "entry"]
        return stale

    def test_what_a_killed_run_leaves_behind_is_reclaimed(self, tmp_path):
        """SIGKILL gives the script no chance to clean up. Kill it for real,
        mid-copy, and the next run must recognise the leftover as its own,
        remove it, and restore the file whole."""
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        target.mkdir()
        stale = self._kill_mid_copy(tmp_path, pristine, target)

        result = _seed(target, pristine)

        assert result.returncode == 0, result.stderr
        assert f"Removed {stale}" in result.stdout
        assert _staging_left_in(target) == []
        assert (target / "libvirtd.conf").read_text() == (
            'auth_unix_rw = "none"\n')

    def test_a_file_arriving_during_the_removal_is_kept(self, tmp_path):
        """Checking that a leftover is the script's own and removing it are
        two steps. A file written into it between them is not the script's
        to delete: it survives, and nothing claims the directory went."""
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        target.mkdir()
        stale = self._kill_mid_copy(tmp_path, pristine, target)
        ran = tmp_path / "shim-ran"
        env = _shim(tmp_path, "rm", (
            f'for arg in "$@"; do\n'
            f'    case "$arg" in "{stale}"|"{stale}"/*)\n'
            f'        if [ ! -e "{ran}" ]; then\n'
            f'            echo "<domain/>" > "{stale / "guest.xml"}"\n'
            f'            touch "{ran}"\n'
            f'        fi ;;\n'
            f'    esac\n'
            f'done'))

        result = _seed(target, pristine, env=env)

        assert ran.exists()
        assert result.returncode == 0, result.stderr
        assert (stale / "guest.xml").read_text() == "<domain/>\n"
        assert "Removed" not in result.stdout
        assert (target / "libvirtd.conf").read_text() == (
            'auth_unix_rw = "none"\n')

        # no longer staging at all, so later runs leave it alone too
        again = _seed(target, pristine)
        assert again.returncode == 0, again.stderr
        assert (stale / "guest.xml").read_text() == "<domain/>\n"
        assert "Removed" not in again.stdout

    @pytest.mark.parametrize("contents", [
        {STAGING_MARKER: "x"},                        # killed before copying
        {STAGING_MARKER: "x", "entry": "<filter"},    # killed mid-copy
    ])
    def test_staging_holding_its_marker_is_removed(self, tmp_path, contents):
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        shutil.copytree(pristine, target, symlinks=True)
        stale = target / "nwfilter" / ".boxman-seed.Ab12Cd"
        stale.mkdir()
        for name, text in contents.items():
            (stale / name).write_text(text)

        result = _seed(target, pristine)

        assert result.returncode == 0, result.stderr
        assert not stale.exists()
        assert f"Removed {stale}" in result.stdout

    @pytest.mark.parametrize("name, build", [
        # a user's directory with a six-character suffix, as the glob matches
        (".boxman-seed.backup",
         lambda d: (d.mkdir(), (d / "guest.xml").write_text("<domain/>"))),
        # the marker is there, but so is something staging never holds
        (".boxman-seed.Qx12ab",
         lambda d: (d.mkdir(), (d / STAGING_MARKER).write_text("x"),
                    (d / "entry").write_text("x"),
                    (d / "notes.txt").write_text("mine"))),
        # staging only ever holds a file or link as its entry
        (".boxman-seed.Rz34cd",
         lambda d: (d.mkdir(), (d / STAGING_MARKER).write_text("x"),
                    (d / "entry").mkdir(),
                    (d / "entry" / "guest.xml").write_text("<domain/>"))),
        # a marker that is not a regular file
        (".boxman-seed.Mk56ef",
         lambda d: (d.mkdir(), (d / STAGING_MARKER).mkdir())),
        # nor a link to one, which the removal would otherwise take for its
        # own marker and delete along with the directory
        (".boxman-seed.Ln78gh",
         lambda d: (d.mkdir(),
                    os.symlink(d.parent / "clean-traffic.xml",
                               d / STAGING_MARKER),
                    (d / "entry").write_text("x"))),
        (".boxman-seed.abcdef",
         lambda d: d.write_text("a file, not a directory\n")),
        (".boxman-seed.notes", lambda d: d.mkdir()),
    ])
    def test_a_look_alike_the_script_did_not_make_is_kept(
            self, tmp_path, name, build):
        """Nothing is removed on the strength of its name alone: only a
        directory holding the script's marker and at most its entry."""
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        shutil.copytree(pristine, target, symlinks=True)
        look_alike = target / "nwfilter" / name
        build(look_alike)
        before = _tree(target / "nwfilter")

        result = _seed(target, pristine)

        assert result.returncode == 0, result.stderr
        assert _tree(target / "nwfilter") == before
        assert "Removed" not in result.stdout


class TestIncompatibleExistingPaths:
    """Review finding 2: an existing path is kept whatever it is, but one
    that cannot serve as what the image has there is reported, not
    silently accepted."""

    def test_a_file_where_the_image_has_an_empty_directory(self, tmp_path):
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        target.mkdir()
        (target / "secrets").write_text("not a directory\n")

        result = _seed(target, pristine)

        assert result.returncode == 1
        assert ("secrets: a regular file where the image has a directory"
                in result.stderr)
        assert (target / "secrets").read_text() == "not a directory\n"
        assert (target / "libvirtd.conf").is_file()   # the rest still came

    def test_a_directory_where_the_image_has_a_config_file(self, tmp_path):
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        (target / "libvirtd.conf").mkdir(parents=True)

        result = _seed(target, pristine)

        assert result.returncode == 1
        assert ("libvirtd.conf: a directory where the image has a regular "
                "file" in result.stderr)
        assert (target / "libvirtd.conf").is_dir()

    def test_a_dangling_symlink_where_the_image_has_a_file(self, tmp_path):
        """Kept — the script never removes what the volume holds — but it
        does not make the required file usable, so it is reported."""
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        (target / "qemu" / "networks").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere.xml"
        os.symlink(elsewhere, target / "qemu" / "networks" / "default.xml")

        result = _seed(target, pristine)

        assert result.returncode == 1
        assert ("qemu/networks/default.xml: a dangling symlink where the "
                "image has a regular file") in result.stderr
        assert os.readlink(target / "qemu" / "networks" / "default.xml") == (
            str(elsewhere))
        assert not elsewhere.exists()

    def test_symlinks_are_judged_by_what_they_point_at(self, tmp_path):
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        target.mkdir()
        real_conf = tmp_path / "site-libvirtd.conf"
        real_conf.write_text("# managed elsewhere\n")
        os.symlink(real_conf, target / "libvirtd.conf")
        real_secrets = tmp_path / "site-secrets"
        real_secrets.mkdir()
        os.symlink(real_secrets, target / "secrets")

        result = _seed(target, pristine)

        assert result.returncode == 0, result.stderr
        assert real_conf.read_text() == "# managed elsewhere\n"
        assert os.readlink(target / "secrets") == str(real_secrets)

    def test_where_the_image_has_a_symlink_any_non_directory_will_do(
            self, tmp_path):
        """The image's autostart link points into the container's own
        /etc/libvirt; what the volume holds there is libvirt's business,
        dangling or not — only a directory cannot stand in for a link."""
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        shutil.copytree(pristine, target, symlinks=True)
        link = target / "qemu" / "networks" / "autostart" / "default.xml"
        link.unlink()
        os.symlink(tmp_path / "gone.xml", link)

        assert _seed(target, pristine).returncode == 0

        link.unlink()
        link.mkdir()
        result = _seed(target, pristine)
        assert result.returncode == 1
        assert ("qemu/networks/autostart/default.xml: a directory where the "
                "image has a symlink") in result.stderr


class TestConcurrentWriters:
    """Review finding 3: a path that appears after the script found it
    missing must not be overwritten. Ordinary startup has no concurrent
    writer, so a shim around cp/mkdir makes one appear at the worst moment:
    after the check, before the script publishes."""

    def test_a_file_appearing_during_the_copy_is_not_overwritten(
            self, tmp_path):
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        target.mkdir()
        dst = target / "libvirtd.conf"
        env = _shim(tmp_path, "cp", (
            f'if [ "${{@: -2:1}}" = "{pristine / "libvirtd.conf"}" ]; then\n'
            f'    echo "# written by someone else" > "{dst}"\n'
            f'    touch "{tmp_path / "shim-ran"}"\n'
            f'fi'))

        result = _seed(target, pristine, env=env)

        assert (tmp_path / "shim-ran").exists()
        assert result.returncode == 0, result.stderr
        assert dst.read_text() == "# written by someone else\n"
        assert _staging_left_in(target) == []
        assert (target / "qemu.conf").is_file()

    def test_a_directory_appearing_before_mkdir_is_kept(self, tmp_path):
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        target.mkdir()
        dst = target / "nwfilter"
        env = _shim(tmp_path, "mkdir", (
            f'if [ "${{@: -1}}" = "{dst}" ]; then\n'
            f'    echo "not a directory" > "{dst}"\n'
            f'    touch "{tmp_path / "shim-ran"}"\n'
            f'fi'))

        result = _seed(target, pristine, env=env)

        assert (tmp_path / "shim-ran").exists()
        assert result.returncode == 1
        assert dst.read_text() == "not a directory\n"
        assert ("nwfilter: a regular file where the image has a directory"
                in result.stderr)


def _entrypoint_function(name: str) -> str:
    """The text of one shell function from entrypoint.sh, to run on its own."""
    text = (DOCKER_DIR / "entrypoint.sh").read_text()
    match = re.search(rf"^{name}\(\) {{\n.*?^}}\n", text, re.M | re.S)
    assert match, f"{name}() not found in entrypoint.sh"
    return match.group(0)


class TestTheRuntimeSurfacesAStartupFailure:
    """Review nit: when the entrypoint gives up it idles, and boxman used to
    wait out its readiness timeout for a libvirtd that was never coming,
    report nothing specific, and recreate the container on the next run —
    which then stopped at the same problem."""

    DIAGNOSIS = ("/etc/libvirt is incomplete or damaged and could not be "
                 "repaired from the image.\n    secrets: a regular file where "
                 "the image has a directory")

    @staticmethod
    def _runtime(tmp_path):
        from boxman.runtime.docker_compose import DockerComposeRuntime
        return DockerComposeRuntime(config={"project_dir": str(tmp_path)})

    @staticmethod
    def _docker(marker_text):
        """A stand-in for the shell: libvirtd never answers, and the marker
        file holds *marker_text* (None: there is no marker)."""
        def run(cmd, **_kwargs):
            if "/run/boxman/startup-failure" in cmd and marker_text:
                return MagicMock(ok=True, stdout=marker_text + "\n")
            return MagicMock(ok=False, stdout="")
        return run

    def test_the_wait_ends_at_once_with_the_diagnosis(self, tmp_path):
        from boxman.exceptions import RuntimeUnavailable
        rt = self._runtime(tmp_path)
        rt.ready_timeout = 60
        clock = iter(range(1000))
        with patch("boxman.runtime.docker_compose._shell_run",
                   side_effect=self._docker(self.DIAGNOSIS)), \
                patch("time.sleep") as sleep, \
                patch("time.monotonic", side_effect=lambda: next(clock)):
            with pytest.raises(RuntimeUnavailable) as err:
                rt._wait_for_libvirtd()

        assert sleep.call_count == 0
        message = str(err.value)
        assert self.DIAGNOSIS in message
        assert rt.container_name in message
        assert "docker restart" in message

    def test_without_a_marker_the_wait_times_out_as_before(self, tmp_path):
        rt = self._runtime(tmp_path)
        rt.ready_timeout = 1
        clock = iter(x * 0.3 for x in range(100))
        with patch("boxman.runtime.docker_compose._shell_run",
                   side_effect=self._docker(None)), \
                patch("time.sleep"), \
                patch("time.monotonic", side_effect=lambda: next(clock)):
            with pytest.raises(RuntimeError, match="did not become responsive"):
                rt._wait_for_libvirtd()

    def test_ensure_ready_does_not_recreate_a_container_that_gave_up(
            self, tmp_path):
        """Recreating is the answer to a wedged libvirtd, not to a state
        directory only the user can fix."""
        from boxman.exceptions import RuntimeUnavailable
        rt = self._runtime(tmp_path)
        with patch.object(rt, "_container_is_running", return_value=True), \
                patch.object(rt, "_container_mounts", return_value=None), \
                patch.object(rt, "_project_dir_accessible", return_value=True), \
                patch.object(rt, "_ensure_state_persisted"), \
                patch.object(rt, "_log_compose_file"), \
                patch.object(rt, "_wait_for_libvirtd",
                             side_effect=RuntimeUnavailable(self.DIAGNOSIS)), \
                patch.object(rt, "_recreate_container") as recreate:
            with pytest.raises(RuntimeUnavailable):
                rt.ensure_ready()
        recreate.assert_not_called()

    def test_the_entrypoint_writes_where_the_runtime_looks(self, tmp_path):
        """Runs the entrypoint's own fail_without_restarting, pointed at a
        temp marker: it writes the diagnosis, idles, and exits on TERM."""
        from boxman.runtime.docker_compose import DockerComposeRuntime
        entrypoint = (DOCKER_DIR / "entrypoint.sh").read_text()
        assert (f"STARTUP_FAILURE={DockerComposeRuntime._STARTUP_FAILURE_MARKER}\n"
                in entrypoint)
        # the container's writable layer survives `docker restart`, so a
        # marker from a failed start has to go before the next one begins
        assert entrypoint.index('rm -f "$STARTUP_FAILURE"') < entrypoint.index(
            "seed_state_dir /etc/libvirt")

        marker = tmp_path / "run" / "boxman" / "startup-failure"
        proc = subprocess.Popen(
            ["bash", "-c",
             f"set -e\nSTARTUP_FAILURE={marker}\n"
             f"{_entrypoint_function('fail_without_restarting')}"
             f'fail_without_restarting "$1"', "_", self.DIAGNOSIS],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert marker.read_text() == self.DIAGNOSIS + "\n"
            time.sleep(0.2)
            assert proc.poll() is None, "it exited instead of idling"
            # like `docker stop`: TERM to the entrypoint alone
            proc.terminate()
            proc.wait(timeout=10)
        finally:
            # its sleep outlives it here, as it cannot in a container whose
            # PID 1 has exited, and holds the pipes open
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 1
        assert stderr.count("ERROR:") == 1


class TestSeedingIsWiredIntoTheImage:
    """The script is only useful if the image ships it where the
    entrypoint calls it, for both trees the compose file bind-mounts."""

    def test_the_entrypoint_calls_the_script_the_dockerfile_installs(self):
        dockerfile = (DOCKER_DIR / "Dockerfile").read_text()
        entrypoint = (DOCKER_DIR / "entrypoint.sh").read_text()

        installed = re.search(
            r"^COPY\s+seed-libvirt-state\.sh\s+(\S+)", dockerfile, re.M)
        assert installed, "the Dockerfile does not install the seeding script"
        assert installed.group(1) in entrypoint

    @pytest.mark.parametrize("container_path, subdir", [
        ("/etc/libvirt", "etc-libvirt"),
        ("/var/lib/libvirt/qemu", "var-lib-libvirt-qemu"),
    ])
    def test_both_state_trees_are_seeded(self, container_path, subdir):
        dockerfile = (DOCKER_DIR / "Dockerfile").read_text()
        entrypoint = (DOCKER_DIR / "entrypoint.sh").read_text()
        compose = (DOCKER_DIR / "docker-compose.yml").read_text()

        assert f"/{subdir}:{container_path}\n" in compose
        assert f"/opt/boxman/pristine/{subdir}" in dockerfile
        assert re.search(
            rf"^seed_state_dir {re.escape(container_path)} {subdir}$",
            entrypoint, re.M)
