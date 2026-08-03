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


def test_fix_is_not_disruptive_by_default():
    assert checker.Fix("install a package").disruptive is False
    assert checker.Fix("restart docker", disruptive=True).disruptive is True


# --------------------------------------------------------------------------- #
# parse_firewall_backend                                                      #
# --------------------------------------------------------------------------- #
def test_parse_firewall_backend_reads_the_setting():
    assert checker.parse_firewall_backend(
        'firewall_backend = "nftables"\n') == ("nftables", True)
    assert checker.parse_firewall_backend(
        "firewall_backend = iptables\n") == ("iptables", True)


def test_parse_firewall_backend_commented_out_still_means_supported():
    """The shipped network.conf documents the option even when unset.

    That is the signal we use to tell "libvirt supports this but nobody set
    it" (fixable) apart from "this build has never heard of it" (not).
    """
    text = ('# firewall_backend = "nftables"\n'
            "#\n"
            "# Firewall backend to use...\n")
    assert checker.parse_firewall_backend(text) == ("", True)


def test_parse_firewall_backend_absent_means_unsupported():
    assert checker.parse_firewall_backend("# some other option\n") == ("", False)


# --------------------------------------------------------------------------- #
# forwarding_verdict                                                          #
# --------------------------------------------------------------------------- #
_HEALTHY = (
    "-P FORWARD ACCEPT\n"
    "-A FORWARD -i virbr0 -o virbr0 -j ACCEPT\n"
)
_WIPED = (
    "-P FORWARD DROP\n"
    "-A FORWARD -j DOCKER-USER\n"
    "-A FORWARD -j DOCKER-FORWARD\n"
)


def test_forwarding_verdict_skips_when_nothing_is_forwarded():
    status, _, needs_fix = checker.forwarding_verdict([], _WIPED, True, "")
    assert status == checker.SKIP
    assert needs_fix is False


def test_forwarding_verdict_flags_wiped_rules():
    status, detail, needs_fix = checker.forwarding_verdict(
        [("default", "virbr0")], _WIPED, True, "")
    assert status == checker.WARN
    assert needs_fix is True
    assert "default" in detail
    assert "NAT but never forward" in detail
    assert "docker shares this table" in detail


def test_forwarding_verdict_flags_the_latent_case():
    """Rules are present, but they live in the table docker rebuilds."""
    status, detail, needs_fix = checker.forwarding_verdict(
        [("default", "virbr0")], _HEALTHY, True, "iptables")
    assert status == checker.WARN
    assert needs_fix is True
    assert "wipes these rules" in detail
    # policy is ACCEPT here, so the extra DROP warning must not appear
    assert "FORWARD policy is DROP" not in detail


def test_forwarding_verdict_notes_a_drop_policy():
    rules = _HEALTHY.replace("-P FORWARD ACCEPT", "-P FORWARD DROP")
    _, detail, _ = checker.forwarding_verdict(
        [("default", "virbr0")], rules, True, "iptables")
    assert "FORWARD policy is DROP" in detail


def test_forwarding_verdict_ok_once_libvirt_owns_its_table():
    status, _, needs_fix = checker.forwarding_verdict(
        [("default", "virbr0")], _HEALTHY, True, "nftables")
    assert status == checker.OK
    assert needs_fix is False


def test_forwarding_verdict_ok_without_docker():
    status, _, needs_fix = checker.forwarding_verdict(
        [("default", "virbr0")], _HEALTHY, False, "")
    assert status == checker.OK
    assert needs_fix is False


def test_forwarding_verdict_will_not_cry_wolf_on_a_partial_view():
    """Unreadable ruleset => "unknown", never "wiped".

    libvirt's nftables backend keeps its rules in a private table. If we could
    not read it, their absence from `iptables -S` proves nothing.
    """
    status, detail, needs_fix = checker.forwarding_verdict(
        [("default", "virbr0")], _WIPED, True, "", complete_view=False)
    assert status == checker.INFO
    assert needs_fix is False
    assert "could not be confirmed" in detail


def test_forwarding_verdict_accepts_rules_from_the_nft_table():
    """Rules found in libvirt's own nft table count as present."""
    rules = (
        "-P FORWARD DROP\n"
        "-A FORWARD -j DOCKER-USER\n"
        'table ip libvirt_network {\n'
        '  chain forward {\n'
        '    iifname "virbr0" accept\n'
        "  }\n"
        "}\n"
    )
    status, _, needs_fix = checker.forwarding_verdict(
        [("default", "virbr0")], rules, True, "nftables")
    assert status == checker.OK
    assert needs_fix is False


def test_forwarding_verdict_bridge_match_is_not_a_substring():
    """virbr10's rules must not vouch for virbr1.

    Plain containment would report virbr1 as healthy here and hide a network
    with no rules at all.
    """
    rules = "-P FORWARD DROP\n-A FORWARD -i virbr10 -o virbr10 -j ACCEPT\n"
    status, detail, needs_fix = checker.forwarding_verdict(
        [("lab", "virbr1")], rules, True, "")
    assert status == checker.WARN
    assert needs_fix is True
    assert "lab" in detail
