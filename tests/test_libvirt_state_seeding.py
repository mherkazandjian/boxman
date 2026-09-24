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
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

DOCKER_DIR = Path(__file__).resolve().parents[1] / "containers" / "docker"
SEED_SCRIPT = DOCKER_DIR / "seed-libvirt-state.sh"

if shutil.which("bash") is None:  # pragma: no cover - every CI host has it
    pytest.skip("the seeding script needs bash", allow_module_level=True)


def _make_pristine(root: Path) -> Path:
    """A small stand-in for the image's /etc/libvirt, with the shapes that
    matter: nested files, an empty 0700 directory, and the absolute
    autostart symlink the Dockerfile creates (dangling outside the image)."""
    pristine = root / "pristine" / "etc-libvirt"
    (pristine / "nwfilter").mkdir(parents=True)
    (pristine / "secrets").mkdir()
    (pristine / "secrets").chmod(0o700)
    (pristine / "qemu" / "networks" / "autostart").mkdir(parents=True)
    (pristine / "libvirtd.conf").write_text('auth_unix_rw = "none"\n')
    (pristine / "qemu.conf").write_text('user = "root"\n')
    (pristine / "nwfilter" / "clean-traffic.xml").write_text(
        "<filter name='clean-traffic'/>\n")
    (pristine / "qemu" / "networks" / "default.xml").write_text(
        "<network><name>default</name></network>\n")
    os.symlink("/etc/libvirt/qemu/networks/default.xml",
               pristine / "qemu" / "networks" / "autostart" / "default.xml")
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


def _seed(target: Path, pristine: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SEED_SCRIPT), str(target), str(pristine)],
        capture_output=True, text=True, timeout=30)


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

    def test_a_missing_directory_is_created_and_seeded(self, tmp_path):
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "does" / "not" / "exist"

        result = _seed(target, pristine)

        assert result.returncode == 0, result.stderr
        assert _tree(target) == _tree(pristine)

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

    def test_a_dangling_symlink_counts_as_present(self, tmp_path):
        """``-e`` alone calls a dangling link missing; ``cp`` then refuses to
        write through it, and a link the volume deliberately holds would be
        reported as damage the container cannot start with."""
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        (target / "qemu" / "networks").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere.xml"
        os.symlink(elsewhere, target / "qemu" / "networks" / "default.xml")

        result = _seed(target, pristine)

        assert result.returncode == 0, result.stderr
        assert os.readlink(target / "qemu" / "networks" / "default.xml") == (
            str(elsewhere))
        assert not elsewhere.exists()

    def test_what_cannot_be_restored_fails_naming_each_path(self, tmp_path):
        """A file where the image has a directory blocks everything below it.
        That is reported, per path, with a non-zero exit — and everything
        else is still restored."""
        pristine = _make_pristine(tmp_path)
        target = tmp_path / "etc-libvirt"
        (target / "qemu").mkdir(parents=True)
        (target / "qemu" / "networks").write_text("not a directory\n")

        result = _seed(target, pristine)

        assert result.returncode == 1
        assert (target / "qemu" / "networks").read_text() == (
            "not a directory\n")
        assert (target / "libvirtd.conf").is_file()
        assert "Could not restore 2 path(s)" in result.stderr
        assert "qemu/networks/default.xml:" in result.stderr
        assert "qemu/networks/autostart:" in result.stderr
        # the directory that failed is reported once, not once per entry
        assert "autostart/default.xml" not in result.stderr

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
