"""
Tests for boxman.runtime.tar_stream — validating the archives ``docker cp``
produces before their contents are accepted as a migration (#164 FB-2).

The behaviour under test is deliberately stronger than "does it extract":
:mod:`tarfile` accepts an archive cut between two complete members, and
several of these tests assert that it does, so the reason the extra
validation exists stays visible in the suite.
"""

import io
import os
import tarfile

import pytest

from boxman.runtime.tar_stream import (
    ArchiveError,
    extract_archive,
    read_archive,
)

BLOCK = 512


def _build(path, members, terminate=True):
    """Write a tar at *path* from ``(name, kind, payload/linkname)`` triples."""
    with tarfile.open(path, "w") as tar:
        for name, kind, extra in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.DIRTYPE:
                info.mode = 0o755
                tar.addfile(info)
            elif kind == tarfile.SYMTYPE:
                info.linkname = extra
                tar.addfile(info)
            else:
                info.mode = 0o644
                info.size = len(extra)
                tar.addfile(info, io.BytesIO(extra))
    if not terminate:
        # drop the end-of-archive records and the record padding
        with tarfile.open(path) as tar:
            last = tar.getmembers()[-1]
        end = last.offset_data + ((last.size + BLOCK - 1) // BLOCK) * BLOCK
        data = open(path, "rb").read()[:end]
        with open(path, "wb") as fobj:
            fobj.write(data)


TREE = [
    ("libvirt", tarfile.DIRTYPE, None),
    ("libvirt/libvirtd.conf", tarfile.REGTYPE, b"listen_tls = 0\n"),
    ("libvirt/qemu", tarfile.DIRTYPE, None),
    ("libvirt/qemu/vm1.xml", tarfile.REGTYPE, b"<domain/>\n"),
    ("libvirt/qemu/networks", tarfile.DIRTYPE, None),
    ("libvirt/qemu/networks/autostart", tarfile.DIRTYPE, None),
    ("libvirt/qemu/networks/autostart/default.xml", tarfile.SYMTYPE,
     "/etc/libvirt/qemu/networks/default.xml"),
]


class TestReadArchive:

    def test_clean_archive_is_accepted(self, tmp_path):
        path = str(tmp_path / "a.tar")
        _build(path, TREE)
        assert len(read_archive(path)) == len(TREE)

    def test_empty_archive_is_accepted(self, tmp_path):
        path = str(tmp_path / "a.tar")
        with tarfile.open(path, "w"):
            pass
        assert read_archive(path) == []

    def test_truncated_mid_member_is_refused(self, tmp_path):
        path = str(tmp_path / "a.tar")
        _build(path, TREE)
        with tarfile.open(path) as tar:
            cut = tar.getmembers()[1].offset_data + 3
        data = open(path, "rb").read()[:cut]
        (tmp_path / "cut.tar").write_bytes(data)
        with pytest.raises(ArchiveError, match="not a readable tar archive"):
            read_archive(str(tmp_path / "cut.tar"))

    def test_truncated_between_members_is_refused(self, tmp_path):
        """The case a tail check cannot see.

        A member whose payload ends in zeros supplies 1,024 zero bytes of
        its own. Cut the archive right after it and the tail is
        indistinguishable from an end-of-archive marker — while every
        member after the cut is gone.
        """
        members = [
            ("libvirt", tarfile.DIRTYPE, None),
            ("libvirt/nvram.fd", tarfile.REGTYPE, b"x" * 64 + b"\0" * 2048),
            ("libvirt/vm2.xml", tarfile.REGTYPE, b"<domain/>\n"),
        ]
        path = str(tmp_path / "a.tar")
        _build(path, members)
        with tarfile.open(path) as tar:
            second = tar.getmembers()[1]
        cut = (second.offset_data
               + ((second.size + BLOCK - 1) // BLOCK) * BLOCK)
        data = open(path, "rb").read()[:cut]
        cut_path = tmp_path / "cut.tar"
        cut_path.write_bytes(data)

        # the two things that make this the interesting case
        assert data[-1024:] == b"\0" * 1024
        with tarfile.open(str(cut_path)) as tar:
            assert len(tar.getmembers()) == 2  # tarfile is happy, one is lost

        with pytest.raises(ArchiveError, match="truncated"):
            read_archive(str(cut_path))

    def test_missing_terminator_is_refused(self, tmp_path):
        path = str(tmp_path / "a.tar")
        _build(path, TREE, terminate=False)
        with pytest.raises(ArchiveError, match="truncated"):
            read_archive(path)

    def test_absolute_member_is_refused(self, tmp_path):
        path = str(tmp_path / "a.tar")
        _build(path, [("/etc/shadow", tarfile.REGTYPE, b"root::0:\n")])
        with pytest.raises(ArchiveError, match="absolute path"):
            read_archive(path)

    @pytest.mark.parametrize("name", [
        "../escape",
        "libvirt/../../escape",
        "a/../../../etc/passwd",
    ])
    def test_escaping_member_is_refused(self, tmp_path, name):
        path = str(tmp_path / "a.tar")
        _build(path, [(name, tarfile.REGTYPE, b"x")])
        with pytest.raises(ArchiveError, match="escapes its root"):
            read_archive(path)

    def test_traversal_inside_the_root_is_fine(self, tmp_path):
        """``libvirt/../f`` resolves to ``f``, which does not escape."""
        path = str(tmp_path / "a.tar")
        _build(path, [("libvirt/../f", tarfile.REGTYPE, b"x")])
        assert len(read_archive(path)) == 1

    def test_sparse_member_is_refused(self, tmp_path):
        path = str(tmp_path / "a.tar")
        _build(path, TREE)
        real_open = tarfile.open

        class _Sparse(tarfile.TarInfo):
            def issparse(self):
                return True

        def _open(*args, **kwargs):
            tar = real_open(*args, **kwargs)
            tar.tarinfo = _Sparse
            return tar

        import unittest.mock as _mock
        with _mock.patch.object(tarfile, "open", _open):
            with pytest.raises(ArchiveError, match="sparse"):
                read_archive(path)

    def test_missing_file_is_refused(self, tmp_path):
        with pytest.raises(ArchiveError):
            read_archive(str(tmp_path / "nope.tar"))


class TestExtractArchive:

    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "a.tar")
        _build(path, TREE)
        dest = str(tmp_path / "out")
        extracted = extract_archive(path, dest)

        assert "libvirt/qemu/vm1.xml" in extracted
        assert (tmp_path / "out/libvirt/qemu/vm1.xml").read_bytes() == \
            b"<domain/>\n"

    def test_absolute_symlink_survives(self, tmp_path):
        """The ``data`` and ``tar`` extraction filters reject these.

        ``/etc/libvirt/qemu/networks/autostart/default.xml`` is an absolute
        symlink in the image, so rejecting it would refuse a valid
        migration rather than protect one.
        """
        path = str(tmp_path / "a.tar")
        _build(path, TREE)
        dest = tmp_path / "out"
        extract_archive(path, str(dest))

        link = dest / "libvirt/qemu/networks/autostart/default.xml"
        assert os.path.islink(link)
        assert os.readlink(link) == \
            "/etc/libvirt/qemu/networks/default.xml"

    def test_existing_destination_is_refused(self, tmp_path):
        path = str(tmp_path / "a.tar")
        _build(path, TREE)
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(ArchiveError, match="already exists"):
            extract_archive(path, str(dest))

    def test_truncation_is_refused_before_anything_extracts(self, tmp_path):
        path = str(tmp_path / "a.tar")
        _build(path, TREE, terminate=False)
        dest = tmp_path / "out"
        with pytest.raises(ArchiveError):
            extract_archive(path, str(dest))
        assert not dest.exists()

    def test_member_that_does_not_reach_disk_is_detected(self, tmp_path):
        """A write that silently does not happen must not pass as success."""
        path = str(tmp_path / "a.tar")
        _build(path, TREE)
        dest = str(tmp_path / "out")

        real_extractall = tarfile.TarFile.extractall

        def _drop_one(self, dest_path, members=None, **kwargs):
            kept = [m for m in members if m.name != "libvirt/qemu/vm1.xml"]
            return real_extractall(self, dest_path, members=kept, **kwargs)

        import unittest.mock as _mock
        with _mock.patch.object(tarfile.TarFile, "extractall", _drop_one):
            with pytest.raises(ArchiveError, match="did not reach"):
                extract_archive(path, dest)

    def test_undeclared_file_at_the_destination_is_refused(self, tmp_path):
        path = str(tmp_path / "a.tar")
        _build(path, TREE)
        dest = str(tmp_path / "out")

        real_extractall = tarfile.TarFile.extractall

        def _add_one(self, dest_path, members=None, **kwargs):
            result = real_extractall(self, dest_path, members=members,
                                     **kwargs)
            with open(os.path.join(dest_path, "libvirt/stowaway"), "w") as f:
                f.write("x")
            return result

        import unittest.mock as _mock
        with _mock.patch.object(tarfile.TarFile, "extractall", _add_one):
            with pytest.raises(ArchiveError, match="does not declare"):
                extract_archive(path, dest)

    def test_implicit_parent_directories_are_allowed(self, tmp_path):
        """tarfile creates a member's parents when the archive omits them.

        Those are legitimate extras; refusing them would refuse a valid
        archive, which is the failure mode this check must not have.
        """
        path = str(tmp_path / "a.tar")
        _build(path, [("libvirt/qemu/nvram/vm1.fd", tarfile.REGTYPE, b"\0" * 8)])
        dest = str(tmp_path / "out")
        extracted = extract_archive(path, dest)
        assert "libvirt/qemu/nvram/vm1.fd" in extracted
        assert "libvirt/qemu" in extracted
