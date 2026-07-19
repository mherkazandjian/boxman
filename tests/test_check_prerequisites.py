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
    ("nixos", "", "nixos"),
    ("guix", "", "guix"),
    ("gentoo", "", "gentoo"),
    ("unknowndistro", "", "unknown"),
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


def test_install_cmd_gentoo_uses_emerge_ask():
    assert checker.install_cmd("gentoo", "net-misc/rsync") == "sudo emerge --ask net-misc/rsync"


def test_install_cmd_nixos_prefixes_each_pkg_with_nixpkgs():
    # Per-user imperative install is legitimate for individual CLI tools.
    assert checker.install_cmd("nixos", "rsync") == "nix profile install nixpkgs#rsync"
    assert (checker.install_cmd("nixos", "rsync sshpass")
            == "nix profile install nixpkgs#rsync nixpkgs#sshpass")


def test_install_cmd_guix_uses_guix_install():
    assert checker.install_cmd("guix", "rsync") == "guix install rsync"


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


# --------------------------------------------------------------------------- #
# Per-distro table completeness                                               #
# --------------------------------------------------------------------------- #
# Imperative distros drive a package manager; NixOS and Guix System are
# declarative (their libvirt/QEMU stack + service are set in the system config
# and applied with a rebuild), so they are deliberately absent from _CORE_STACK.
IMPERATIVE_FAMILIES = ["arch", "debian", "rhel", "gentoo"]
DECLARATIVE_FAMILIES = ["nixos", "guix"]
ALL_FAMILIES = IMPERATIVE_FAMILIES + DECLARATIVE_FAMILIES


def test_core_stack_covers_exactly_the_imperative_families():
    assert set(checker._CORE_STACK) == set(IMPERATIVE_FAMILIES)


@pytest.mark.parametrize("family", ALL_FAMILIES)
def test_seed_pkg_has_a_column_for_every_family(family):
    assert family in checker._SEED_PKG, "missing _SEED_PKG column: %s" % family
    assert checker._SEED_PKG[family], "empty _SEED_PKG entry: %s" % family


@pytest.mark.parametrize("tool", sorted(checker._TOOL_PKG))
@pytest.mark.parametrize("family", ALL_FAMILIES)
def test_tool_pkg_has_a_column_for_every_family(tool, family):
    assert family in checker._TOOL_PKG[tool], \
        "missing _TOOL_PKG[%r] column: %s" % (tool, family)
    assert checker._TOOL_PKG[tool][family], \
        "empty _TOOL_PKG[%r] entry: %s" % (tool, family)


# --------------------------------------------------------------------------- #
# Declarative-distro advisory text                                            #
# --------------------------------------------------------------------------- #
def test_declarative_families_have_non_empty_advisory_constants():
    for const in (checker._NIXOS_CORE_ADVICE, checker._GUIX_CORE_ADVICE,
                  checker._NIXOS_LIBVIRTD_ADVICE):
        assert isinstance(const, str) and const.strip()
    assert "nixos-rebuild switch" in checker._NIXOS_CORE_ADVICE
    assert "virtualisation.libvirtd.enable" in checker._NIXOS_CORE_ADVICE
    assert "guix system reconfigure" in checker._GUIX_CORE_ADVICE
    assert "libvirt-service-type" in checker._GUIX_CORE_ADVICE


# --------------------------------------------------------------------------- #
# Doctor per-distro dispatch (methods only touch .family / .os -- no host I/O) #
# --------------------------------------------------------------------------- #
def _doctor_with_family(family, osid="x"):
    """A Doctor with just the attributes the dispatch methods read.

    Built via ``__new__`` so ``Doctor.__init__`` (which inspects the host) is
    skipped -- these tests stay side-effect free like the rest of the file.
    """
    doc = object.__new__(checker.Doctor)
    doc.family = family
    doc.os = {"id": osid}
    return doc


@pytest.mark.parametrize("family", IMPERATIVE_FAMILIES)
def test_core_stack_fix_is_runnable_for_imperative_families(family):
    fix = _doctor_with_family(family)._core_stack_fix()
    assert fix.runnable is True
    assert fix.commands == [checker._CORE_STACK[family]]


@pytest.mark.parametrize("family", DECLARATIVE_FAMILIES)
def test_core_stack_fix_is_advisory_for_declarative_families(family):
    fix = _doctor_with_family(family)._core_stack_fix()
    assert fix.runnable is False
    assert fix.commands == []
    assert fix.description.strip()


def test_core_stack_fix_gentoo_uses_emerge():
    fix = _doctor_with_family("gentoo")._core_stack_fix()
    assert "emerge" in fix.commands[0]
    assert "app-emulation/libvirt" in fix.commands[0]


def test_core_stack_fix_unknown_family_falls_back_to_manual_advice():
    fix = _doctor_with_family("unknown", osid="weird")._core_stack_fix()
    assert fix.runnable is False
    assert "unknown distro 'weird'" in fix.description


def test_install_fix_new_families_are_not_flagged_unrecognized():
    # New families take the per-distro imperative user-tool path, so the
    # generic "unrecognized distro" advisory must NOT be appended.
    nixos = _doctor_with_family("nixos")._install_fix("install rsync", "rsync")
    assert nixos.commands == ["nix profile install nixpkgs#rsync"]
    assert "unrecognized distro" not in nixos.description

    guix = _doctor_with_family("guix")._install_fix("install rsync", "rsync")
    assert guix.commands == ["guix install rsync"]
    assert "unrecognized distro" not in guix.description

    gentoo = _doctor_with_family("gentoo")._install_fix("install rsync", "net-misc/rsync")
    assert gentoo.commands == ["sudo emerge --ask net-misc/rsync"]
    assert "unrecognized distro" not in gentoo.description


def test_install_fix_unknown_family_is_flagged_unrecognized():
    fix = _doctor_with_family("unknown", osid="weird")._install_fix("install rsync", "rsync")
    assert fix.commands == []
    assert "unrecognized distro 'weird'" in fix.description
