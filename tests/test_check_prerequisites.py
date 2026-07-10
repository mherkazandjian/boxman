"""Unit tests for the pure helpers in scripts/installer/check_prerequisites.py.

The checker is a standalone stdlib-only script (not part of the importable
``boxman`` package), so we load it by path via importlib. Only the
side-effect-free helpers are exercised here -- no libvirt/subprocess/host calls.
"""

import importlib.util
import os

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "installer", "check_prerequisites.py",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("boxman_prereq_checker", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_module()


# --------------------------------------------------------------------------- #
# parse_os_release                                                            #
# --------------------------------------------------------------------------- #
def test_parse_os_release_basic():
    text = (
        'NAME="Ubuntu"\n'
        'ID=ubuntu\n'
        'ID_LIKE=debian\n'
        'PRETTY_NAME="Ubuntu 24.04 LTS"\n'
    )
    data = checker.parse_os_release(text)
    assert data["ID"] == "ubuntu"
    assert data["ID_LIKE"] == "debian"
    assert data["PRETTY_NAME"] == "Ubuntu 24.04 LTS"


def test_parse_os_release_ignores_comments_and_blanks():
    text = "# a comment\n\nID=arch\nBROKEN_LINE_NO_EQUALS\n"
    data = checker.parse_os_release(text)
    assert data == {"ID": "arch"}


def test_parse_os_release_strips_single_quotes():
    data = checker.parse_os_release("ID='fedora'\n")
    assert data["ID"] == "fedora"


# --------------------------------------------------------------------------- #
# classify_family                                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("osid,like,expected", [
    ("arch", "", "arch"),
    ("manjaro", "arch", "arch"),
    ("ubuntu", "debian", "debian"),
    ("debian", "", "debian"),
    ("linuxmint", "ubuntu debian", "debian"),
    ("fedora", "", "rhel"),
    ("rocky", "rhel centos fedora", "rhel"),
    ("almalinux", "rhel", "rhel"),
    ("gentoo", "", "unknown"),
    ("", "", "unknown"),
])
def test_classify_family(osid, like, expected):
    assert checker.classify_family(osid, like) == expected


def test_classify_family_uses_id_like_when_id_unknown():
    # A derivative whose ID we don't hardcode but whose ID_LIKE is debian.
    assert checker.classify_family("somederiv", "ubuntu debian") == "debian"


# --------------------------------------------------------------------------- #
# install_cmd                                                                 #
# --------------------------------------------------------------------------- #
def test_install_cmd_per_family():
    assert checker.install_cmd("arch", "sshpass").startswith("sudo pacman -S --needed ")
    assert "apt install -y" in checker.install_cmd("debian", "rsync")
    assert checker.install_cmd("rhel", "zstd") == "sudo dnf install -y zstd"


def test_install_cmd_unknown_family_returns_none():
    assert checker.install_cmd("unknown", "rsync") is None


# --------------------------------------------------------------------------- #
# sudo_nopasswd_covers                                                        #
# --------------------------------------------------------------------------- #
def test_sudo_nopasswd_covers_all():
    out = "User me may run the following commands:\n    (ALL) NOPASSWD: ALL\n"
    assert checker.sudo_nopasswd_covers(out, "qemu-img") is True
    assert checker.sudo_nopasswd_covers(out, "iptables") is True


def test_sudo_nopasswd_covers_specific_binary():
    out = "    (root) NOPASSWD: /usr/bin/virsh, /usr/sbin/iptables\n"
    assert checker.sudo_nopasswd_covers(out, "iptables") is True
    assert checker.sudo_nopasswd_covers(out, "/usr/bin/virsh") is True
    # qemu-img / rm are NOT in the NOPASSWD list -> the documented footgun.
    assert checker.sudo_nopasswd_covers(out, "qemu-img") is False
    assert checker.sudo_nopasswd_covers(out, "rm") is False


def test_sudo_nopasswd_covers_ignores_password_required_lines():
    out = "    (ALL : ALL) ALL\n"  # sudo allowed but with a password
    assert checker.sudo_nopasswd_covers(out, "qemu-img") is False


# --------------------------------------------------------------------------- #
# worst_status                                                                #
# --------------------------------------------------------------------------- #
def test_worst_status_severity_order():
    assert checker.worst_status([checker.OK, checker.WARN, checker.FAIL]) == checker.FAIL
    assert checker.worst_status([checker.OK, checker.WARN]) == checker.WARN
    assert checker.worst_status([checker.OK, checker.SKIP]) == checker.SKIP
    assert checker.worst_status([checker.OK]) == checker.OK
    assert checker.worst_status([]) == checker.OK


# --------------------------------------------------------------------------- #
# Fix container                                                               #
# --------------------------------------------------------------------------- #
def test_fix_runnable_flag():
    assert checker.Fix("do a thing", commands=["echo hi"]).runnable is True
    assert checker.Fix("read a doc", commands=[]).runnable is False
