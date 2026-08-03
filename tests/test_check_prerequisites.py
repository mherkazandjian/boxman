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


def test_parse_firewall_backend_ignores_the_bare_word_in_prose():
    """Support is inferred from a real declaration, not a mention.

    The word can appear in unrelated prose (a changelog note, a comment about
    another distro) and must not be read as "this build supports the option".
    """
    text = "# see the firewall_backend discussion upstream for context\n"
    assert checker.parse_firewall_backend(text) == ("", False)
    assert checker.parse_firewall_backend(
        '#firewall_backend = "iptables"\n') == ("", True)


# --------------------------------------------------------------------------- #
# iptables_forward_facts                                                      #
# --------------------------------------------------------------------------- #
def test_iptables_forward_facts_keeps_only_forward_rules():
    text = (
        "-P FORWARD DROP\n"
        "-A INPUT -i lo -j ACCEPT\n"
        "-A FORWARD -i virbr0 -o eth0 -j ACCEPT\n"
        "-A OUTPUT -o virbr9 -j ACCEPT\n"
    )
    rules, _drop_rules, policy_drop = checker.iptables_forward_facts(text)
    assert "virbr0" in rules
    # an OUTPUT rule must never vouch for a bridge's forwarding
    assert "virbr9" not in rules
    assert policy_drop is True


def test_iptables_forward_facts_ignores_orphaned_libvirt_chains():
    """A populated chain nothing jumps to is inert.

    Docker's rebuild removes the FORWARD jumps but leaves the LIBVIRT_FW*
    chains behind, so counting their contents would call a dead host healthy.
    """
    orphaned = (
        "-P FORWARD DROP\n"
        "-A FORWARD -j DOCKER-USER\n"
        "-A LIBVIRT_FWI -d 192.168.122.0/24 -o virbr0 -j ACCEPT\n"
    )
    rules, _, _ = checker.iptables_forward_facts(orphaned)
    assert "virbr0" not in rules

    wired = "-A FORWARD -j LIBVIRT_FWX\n" + orphaned
    rules, _, _ = checker.iptables_forward_facts(wired)
    assert "virbr0" in rules


def test_iptables_forward_facts_accept_policy():
    _, _, policy_drop = checker.iptables_forward_facts("-P FORWARD ACCEPT\n")
    assert policy_drop is False


# --------------------------------------------------------------------------- #
# nft_forward_facts                                                           #
# --------------------------------------------------------------------------- #
def _nft(*items):
    import json as _json
    return _json.dumps({"nftables": list(items)})


def test_nft_forward_facts_ignores_nat_and_mangle():
    """The regression that made the acute case undetectable.

    libvirt's mangle CHECKSUM rule names the bridge and survives a docker
    rebuild, so scanning the whole ruleset finds the bridge on a host whose
    forwarding is dead.
    """
    doc = _nft(
        {"table": {"family": "ip", "name": "mangle"}},
        {"chain": {"family": "ip", "table": "mangle", "name": "POSTROUTING",
                   "hook": "postrouting", "policy": "accept"}},
        {"rule": {"family": "ip", "table": "mangle", "chain": "POSTROUTING",
                  "expr": [{"match": {"right": "virbr0"}}]}},
    )
    rules, _drops, policy_drop, libvirt_table, parsed = checker.nft_forward_facts(doc)
    assert "virbr0" not in rules
    assert policy_drop is False
    assert libvirt_table is False
    assert parsed is True


def _libvirt_ruleset(bridge="virbr0", filter_policy="accept"):
    """A ruleset shaped like libvirt's actual nftables backend.

    libvirt's `forward` base chain only jumps; the rules naming the bridge sit
    in guest_cross / guest_input / guest_output one hop below it. A collector
    that stops at base chains sees nothing and calls a healthy host wiped.
    """
    return _nft(
        {"metainfo": {"version": "1.0.9"}},
        {"table": {"family": "ip", "name": "libvirt_network"}},
        {"chain": {"family": "ip", "table": "libvirt_network", "name": "forward",
                   "type": "filter", "hook": "forward", "policy": "accept"}},
        {"rule": {"family": "ip", "table": "libvirt_network", "chain": "forward",
                  "expr": [{"jump": {"target": "guest_cross"}}]}},
        {"rule": {"family": "ip", "table": "libvirt_network", "chain": "forward",
                  "expr": [{"jump": {"target": "guest_output"}}]}},
        {"chain": {"family": "ip", "table": "libvirt_network", "name": "guest_cross"}},
        {"chain": {"family": "ip", "table": "libvirt_network", "name": "guest_output"}},
        {"rule": {"family": "ip", "table": "libvirt_network", "chain": "guest_output",
                  "expr": [{"match": {"left": {"meta": {"key": "iifname"}},
                                      "right": bridge}},
                           {"accept": None}]}},
        {"table": {"family": "ip", "name": "filter"}},
        {"chain": {"family": "ip", "table": "filter", "name": "FORWARD",
                   "type": "filter", "hook": "forward", "policy": filter_policy}},
    )


def test_nft_forward_facts_follows_jumps_into_libvirt_guest_chains():
    """Regression: base chains alone find nothing on the nftables backend."""
    rules, _drops, policy_drop, libvirt_table, parsed = checker.nft_forward_facts(
        _libvirt_ruleset())
    assert parsed is True
    assert libvirt_table is True
    assert "virbr0" in rules
    assert policy_drop is False


def test_nft_forward_facts_does_not_follow_jumps_across_tables():
    """A same-named chain in another table must not be pulled in."""
    doc = _nft(
        {"table": {"family": "ip", "name": "filter"}},
        {"chain": {"family": "ip", "table": "filter", "name": "FORWARD",
                   "type": "filter", "hook": "forward", "policy": "drop"}},
        {"rule": {"family": "ip", "table": "filter", "chain": "FORWARD",
                  "expr": [{"jump": {"target": "shared"}}]}},
        {"rule": {"family": "ip", "table": "filter", "chain": "shared",
                  "expr": [{"match": {"right": "virbr0"}}]}},
        {"table": {"family": "ip", "name": "other"}},
        {"rule": {"family": "ip", "table": "other", "chain": "shared",
                  "expr": [{"match": {"right": "virbr9"}}]}},
    )
    rules, _, _, _, _ = checker.nft_forward_facts(doc)
    assert "virbr0" in rules
    assert "virbr9" not in rules


def test_nft_forward_facts_reads_a_base_chain_policy():
    rules, _drops, policy_drop, _, parsed = checker.nft_forward_facts(
        _libvirt_ruleset(filter_policy="drop"))
    assert parsed is True
    assert policy_drop is True


# --------------------------------------------------------------------------- #
# the two halves composed -- parser output fed to the verdict                 #
# --------------------------------------------------------------------------- #
def _verdict_for(ruleset, networks=None):
    rules, drops, policy_drop, libvirt_table, _ = checker.nft_forward_facts(ruleset)
    return checker.forwarding_verdict(
        networks or [("default", "virbr0", True)], rules, True,
        "nftables" if libvirt_table else "", policy_drop, drops)


def test_healthy_nftables_host_is_not_called_wiped():
    """The blocker, end to end: both halves done, nothing to report."""
    status, detail, needs_fix = _verdict_for(_libvirt_ruleset())
    assert status == checker.OK, detail
    assert needs_fix is False


def test_half_fixed_nftables_host_is_caught_end_to_end():
    """Backend migrated but the DROP policy never cleared."""
    status, detail, needs_fix = _verdict_for(_libvirt_ruleset(filter_policy="drop"))
    assert status == checker.WARN
    assert "cannot forward" in detail
    assert "only the policy is left" in detail
    assert needs_fix is True


def test_nft_forward_facts_survives_garbage():
    assert checker.nft_forward_facts("not json") == ("", [], False, False, False)
    assert checker.nft_forward_facts("") == ("", [], False, False, False)


def test_nft_forward_facts_reports_an_unrecognised_shape():
    """Schema drift must read as "I did not understand", not as "no libvirt".

    Silently returning empty facts would mark a healthy nftables host as
    sharing docker's table and offer it a disruptive fix.
    """
    assert checker.nft_forward_facts('{"something_else": []}')[4] is False
    assert checker.nft_forward_facts('{"nftables": []}')[4] is False
    assert checker.nft_forward_facts("[]")[4] is False


# --------------------------------------------------------------------------- #
# forwarding_verdict                                                          #
# --------------------------------------------------------------------------- #
_PRESENT = '-A FORWARD -i virbr0 -o eth0 -j ACCEPT'
_ABSENT = '-A FORWARD -j DOCKER-USER'
_NAT = [("default", "virbr0", True)]


def test_forwarding_verdict_skips_when_nothing_is_forwarded():
    status, _, needs_fix = checker.forwarding_verdict([], _ABSENT, True, "")
    assert status == checker.SKIP
    assert needs_fix is False


def test_forwarding_verdict_flags_wiped_rules():
    status, detail, needs_fix = checker.forwarding_verdict(
        _NAT, _ABSENT, True, "", policy_drop=True, drop_evidence=_ABSENT)
    assert status == checker.WARN
    assert needs_fix is True
    assert "default" in detail
    assert "NAT but never forward" in detail
    assert "docker shares this table" in detail


def test_forwarding_verdict_flags_the_latent_case():
    """Rules are present, but they live in the table docker rebuilds."""
    status, detail, needs_fix = checker.forwarding_verdict(
        _NAT, _PRESENT, True, "iptables")
    assert status == checker.WARN
    assert needs_fix is True
    assert "wipes these rules" in detail
    assert "FORWARD policy is DROP" not in detail


def test_forwarding_verdict_notes_a_drop_policy():
    _, detail, _ = checker.forwarding_verdict(
        _NAT, _PRESENT, True, "iptables", policy_drop=True,
        drop_evidence=_PRESENT)
    assert "FORWARD policy is DROP" in detail


def test_forwarding_verdict_catches_the_half_fixed_host():
    """libvirt in its own table is not enough while a foreign chain drops.

    This is the state the README calls out as "the step everyone misses":
    firewall_backend switched, `iptables -P FORWARD ACCEPT` never run. The
    rules are pristine and the guest is still dead.
    """
    status, detail, needs_fix = checker.forwarding_verdict(
        _NAT, _PRESENT, True, "nftables", policy_drop=True, drop_evidence="")
    assert status == checker.WARN
    assert needs_fix is True
    assert "cannot forward" in detail
    assert "only the policy is left" in detail


def test_forwarding_verdict_ok_once_both_halves_are_done():
    status, _, needs_fix = checker.forwarding_verdict(
        _NAT, _PRESENT, True, "nftables", policy_drop=False)
    assert status == checker.OK
    assert needs_fix is False


def test_forwarding_verdict_ok_without_docker():
    status, _, needs_fix = checker.forwarding_verdict(
        _NAT, _PRESENT, False, "iptables")
    assert status == checker.OK
    assert needs_fix is False


def test_forwarding_verdict_will_not_cry_wolf_on_a_partial_view():
    """Unreadable ruleset => "unknown", never "wiped"."""
    status, detail, needs_fix = checker.forwarding_verdict(
        _NAT, _ABSENT, True, "", complete_view=False)
    assert status == checker.INFO
    assert needs_fix is False
    assert "could not be confirmed" in detail


def test_forwarding_verdict_reports_an_undetermined_backend():
    status, detail, needs_fix = checker.forwarding_verdict(
        _NAT, _PRESENT, False, "", complete_view=False)
    assert status == checker.INFO
    assert needs_fix is False
    assert "backend could not be determined" in detail


def test_forwarding_verdict_open_mode_is_never_called_wiped():
    """libvirt writes no rules for mode='open'; absence proves nothing."""
    status, _, needs_fix = checker.forwarding_verdict(
        [("lab", "virbr7", False)], _ABSENT, False, "nftables")
    assert status == checker.OK
    assert needs_fix is False


def test_forwarding_verdict_bridge_match_is_not_a_substring():
    """virbr10's rules must not vouch for virbr1."""
    evidence = "-A FORWARD -i virbr10 -o virbr10 -j ACCEPT"
    status, detail, needs_fix = checker.forwarding_verdict(
        [("lab", "virbr1", True)], evidence, True, "")
    assert status == checker.WARN
    assert needs_fix is True
    assert "lab" in detail


# --------------------------------------------------------------------------- #
# disruptive fixes are never applied unattended                               #
# --------------------------------------------------------------------------- #
class _Opts(object):
    def __init__(self, yes=True, check_only=False):
        self.yes = yes
        self.check_only = check_only
        self.runtime = "local"
        self.verbose = False


def _doctor(monkeypatch, stdin_isatty):
    monkeypatch.setattr(checker.sys.stdin, "isatty", lambda: stdin_isatty,
                        raising=False)
    doctor = checker.Doctor.__new__(checker.Doctor)
    doctor.opts = _Opts()
    doctor.results = []
    doctor.manual_steps = []
    doctor.relogin_needed = False
    doctor.use_color = False
    doctor.interactive = True          # --yes implies this
    return doctor


def test_disruptive_fix_is_not_run_by_yes_without_a_terminal(monkeypatch):
    """`yes | check_prerequisites.py --yes` must not bounce a container host."""
    doctor = _doctor(monkeypatch, stdin_isatty=False)
    ran = []
    monkeypatch.setattr(doctor, "_run_fix", lambda fix: ran.append(fix) or True)
    monkeypatch.setattr(doctor, "_ask", lambda prompt: pytest.fail(
        "must not even ask without a terminal"))

    fix = checker.Fix("restart docker", ["sudo systemctl restart docker"],
                      disruptive=True)
    result = checker.Result("Host forwarding", checker.WARN, "", fix)
    doctor._handle_fix(result, lambda: (checker.WARN, "", fix))

    assert ran == []
    assert any("needs a terminal" in step for step in doctor.manual_steps)


def test_non_disruptive_fix_still_honours_yes(monkeypatch):
    doctor = _doctor(monkeypatch, stdin_isatty=False)
    ran = []
    monkeypatch.setattr(doctor, "_run_fix", lambda fix: ran.append(fix) or True)

    fix = checker.Fix("install sshpass", ["sudo pacman -S sshpass"])
    result = checker.Result("sshpass", checker.FAIL, "", fix)
    doctor._handle_fix(result, lambda: (checker.OK, "installed", None))

    assert len(ran) == 1


# --------------------------------------------------------------------------- #
# _forwarding_fix command construction                                        #
# --------------------------------------------------------------------------- #
def _fix_doctor(monkeypatch, configured="", supported=True):
    doctor = checker.Doctor.__new__(checker.Doctor)
    monkeypatch.setattr(doctor, "_libvirt_fw_backend",
                        lambda: (configured, supported), raising=False)
    monkeypatch.setattr(doctor, "_libvirt_net_unit",
                        lambda: "virtnetworkd", raising=False)
    return doctor


def test_fix_migrates_libvirt_before_touching_docker(monkeypatch):
    """Ordering is a safety property, not a style choice.

    The docker half ends in a docker restart -- the event that wipes the
    shared table. Running it before libvirt has moved out means an abort at
    any later step leaves a merely at-risk host actually broken.
    """
    fix = _fix_doctor(monkeypatch)._forwarding_fix("iptables", True)
    joined = " | ".join(fix.commands)
    assert joined.index("network.conf") < joined.index("daemon.json"), joined
    assert joined.index("restart virtnetworkd") < joined.index("restart docker")
    assert fix.disruptive is True


def test_fix_reapplies_libvirt_rules_when_nothing_else_would(monkeypatch):
    """Acute case with the backend already migrated.

    Without a restart the guided fix "succeeds" and the re-probe still
    reports the network as wiped.
    """
    doctor = _fix_doctor(monkeypatch, configured="nftables")
    fix = doctor._forwarding_fix("nftables", True, wiped=True)
    assert fix.commands[-1] == "sudo systemctl restart virtnetworkd"


def test_fix_does_not_double_restart_when_migrating(monkeypatch):
    fix = _fix_doctor(monkeypatch)._forwarding_fix("iptables", True, wiped=True)
    assert fix.commands.count("sudo systemctl restart virtnetworkd") == 1


def test_fix_offers_nothing_runnable_when_docker_is_not_the_cause(monkeypatch):
    """A DROP policy with no docker means some other owner set it."""
    doctor = _fix_doctor(monkeypatch, configured="nftables")
    fix = doctor._forwarding_fix("nftables", False)
    assert fix.commands == []
    assert fix.disruptive is False
    assert "find what set" in fix.description


def test_fix_leaves_daemon_json_alone_without_a_docker_daemon(monkeypatch):
    fix = _fix_doctor(monkeypatch)._forwarding_fix("iptables", False)
    assert not any("daemon.json" in cmd for cmd in fix.commands)
    assert any("network.conf" in cmd for cmd in fix.commands)


def test_fix_appends_the_backend_with_a_leading_newline(monkeypatch):
    """A network.conf with no trailing newline must not have lines fused."""
    fix = _fix_doctor(monkeypatch)._forwarding_fix("iptables", False)
    sed = next(cmd for cmd in fix.commands if "firewall_backend" in cmd)
    assert '\\nfirewall_backend' in sed


# --------------------------------------------------------------------------- #
# open-mode networks                                                          #
# --------------------------------------------------------------------------- #
def test_open_mode_network_is_flagged_when_the_policy_drops():
    """libvirt writes no rules for mode='open', so a DROP kills it outright."""
    status, detail, needs_fix = checker.forwarding_verdict(
        [("lab", "virbr7", False)], _ABSENT, False, "nftables",
        policy_drop=True, drop_evidence=_ABSENT)
    assert status == checker.WARN
    assert "lab" in detail
    assert "drops by default" in detail
    assert needs_fix is True


def test_open_mode_network_is_fine_when_something_accepts_it():
    # the accept lives in the dropping chain itself, so the policy is moot --
    # this is what boxman's own routed-network FORWARD rules look like
    evidence = "-A FORWARD -i virbr7 -j ACCEPT"
    status, detail, needs_fix = checker.forwarding_verdict(
        [("lab", "virbr7", False)], evidence, False, "nftables",
        policy_drop=True, drop_evidence=evidence)
    assert status == checker.OK, detail
    assert needs_fix is False


def test_wiped_networks_ignores_networks_that_expect_no_rules():
    nets = [("lab", "virbr7", False), ("default", "virbr0", True)]
    assert checker.wiped_networks(nets, "") == ["default"]


# --------------------------------------------------------------------------- #
# round-3 regressions                                                         #
# --------------------------------------------------------------------------- #
def test_each_dropping_chain_must_accept_the_bridge():
    """An accept in one dropping chain cannot vouch past a second one.

    Two independent default-drop forward chains (a hardened host plus docker):
    the packet dies in whichever one has no accept for the bridge.
    """
    accepts = '-A FORWARD -i virbr0 -j ACCEPT'
    status, detail, needs_fix = checker.forwarding_verdict(
        _NAT, accepts, True, "nftables", policy_drop=True,
        drop_evidence=[accepts, "-A FORWARD -j DOCKER-USER"])
    assert status == checker.WARN, detail
    assert needs_fix is True

    # accepted in both -> genuinely safe
    status, _, needs_fix = checker.forwarding_verdict(
        _NAT, accepts, True, "nftables", policy_drop=True,
        drop_evidence=[accepts, accepts])
    assert status == checker.OK
    assert needs_fix is False


def test_nft_forward_facts_keeps_dropping_chains_separate():
    doc = _nft(
        {"table": {"family": "ip", "name": "filter"}},
        {"chain": {"family": "ip", "table": "filter", "name": "FORWARD",
                   "type": "filter", "hook": "forward", "policy": "drop"}},
        {"rule": {"family": "ip", "table": "filter", "chain": "FORWARD",
                  "expr": [{"match": {"right": "virbr0"}}]}},
        {"table": {"family": "ip", "name": "hardened"}},
        {"chain": {"family": "ip", "table": "hardened", "name": "fwd",
                   "type": "filter", "hook": "forward", "policy": "drop"}},
    )
    _, drop_closures, policy_drop, _, _ = checker.nft_forward_facts(doc)
    assert policy_drop is True
    assert len(drop_closures) == 2
    # one chain names the bridge, the other is empty -> not safe
    assert any("virbr0" in closure for closure in drop_closures)
    assert any("virbr0" not in closure for closure in drop_closures)


def test_latent_host_on_old_libvirt_still_reapplies_rules(monkeypatch):
    """A docker restart against the shared table wipes rules even when the
    host was only *at risk* at the time the fix was built."""
    doctor = _fix_doctor(monkeypatch, supported=False)
    monkeypatch.setattr(doctor, "_docker_supports_no_drop", lambda: True,
                        raising=False)
    fix = doctor._forwarding_fix("iptables", docker_manages=True, wiped=False)
    assert any("restart docker" in cmd for cmd in fix.commands)
    assert fix.commands[-1] == "sudo systemctl restart virtnetworkd"


def test_old_docker_engine_is_not_offered_the_daemon_json_edit(monkeypatch):
    """dockerd refuses to start on an unknown directive -- writing it would
    leave docker down, which is worse than the problem."""
    doctor = _fix_doctor(monkeypatch, supported=False)
    monkeypatch.setattr(doctor, "_docker_supports_no_drop", lambda: False,
                        raising=False)
    fix = doctor._forwarding_fix("iptables", docker_manages=True)
    assert not any("daemon.json" in cmd for cmd in fix.commands)
    assert not any("restart docker" in cmd for cmd in fix.commands)
    assert "sudo iptables -P FORWARD ACCEPT" in fix.commands
    assert "predates" in fix.description


def test_fix_describes_no_docker_host_without_mentioning_docker(monkeypatch):
    doctor = _fix_doctor(monkeypatch, supported=False)
    fix = doctor._forwarding_fix("iptables", docker_manages=False, wiped=True)
    assert fix.commands == ["sudo systemctl restart virtnetworkd"]
    assert "docker" not in fix.description
