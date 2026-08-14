#!/usr/bin/env python3
"""Boxman host prerequisites checker (guided doctor).

Inspects the host a boxman user is about to run on, reports OK/WARN/FAIL for
each prerequisite, and -- for fixable problems -- prints the exact OS-specific
remediation and offers to run it after a ``[y/N]`` confirmation.

Design constraints (deliberate):
  * Standard library only.  This script is meant to run *before* boxman's own
    dependencies are guaranteed to be installed (e.g. straight after
    ``pip install boxman`` in a fresh conda env), so it never imports ``boxman``
    or any third-party module.
  * Runs on old Python too (3.6+).  Its very first check is "is Python new
    enough?", which is pointless if the script itself fails to import on an old
    interpreter -- so no dataclasses / walrus / match / PEP 604 unions here.
  * Read-only by default.  Nothing on the host is changed unless you explicitly
    answer "yes" to a fix prompt (or pass --yes).  --check-only disables prompts
    entirely, which is handy for CI.

Usage:
    python3 scripts/installer/check_prerequisites.py [--runtime auto|local|docker]
                                                     [--check-only] [--yes]
                                                     [--verbose]

Exit code: 0 when no blocking FAIL remains, 1 otherwise.
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys

try:
    import grp
    import pwd
    _HAVE_UNIX_IDS = True
except ImportError:  # non-unix; boxman is linux-only but stay graceful
    _HAVE_UNIX_IDS = False


# --------------------------------------------------------------------------- #
# Status constants                                                            #
# --------------------------------------------------------------------------- #
OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"
INFO = "INFO"

_STATUS_TAG = {
    OK: (" OK ", "green"),
    WARN: ("WARN", "yellow"),
    FAIL: ("FAIL", "red"),
    SKIP: ("SKIP", "dim"),
    INFO: ("INFO", "cyan"),
}

_ANSI = {
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "dim": "2", "bold": "1",
}


# --------------------------------------------------------------------------- #
# Pure helpers (no side effects -- unit tested)                               #
# --------------------------------------------------------------------------- #
def parse_os_release(text):
    """Parse the KEY=VALUE body of /etc/os-release into a dict."""
    data = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        data[key.strip()] = val.strip().strip('"').strip("'")
    return data


def classify_family(osid, id_like):
    """Map an os-release ID / ID_LIKE to a package-manager family."""
    osid = (osid or "").lower()
    like = (id_like or "").lower()
    tokens = set([osid] + like.split())
    if tokens & {"arch", "manjaro", "endeavouros", "arcolinux", "artix"}:
        return "arch"
    if tokens & {"debian", "ubuntu", "linuxmint", "pop", "raspbian", "elementary", "kali"}:
        return "debian"
    if tokens & {"rhel", "fedora", "centos", "rocky", "almalinux", "ol", "oracle"}:
        return "rhel"
    if tokens & {"nixos"}:
        return "nixos"
    if tokens & {"guix"}:  # ID=guix is always Guix System
        return "guix"
    if tokens & {"gentoo"}:
        return "gentoo"
    return "unknown"


def configured_libvirt_use_sudo(text, default=False):
    """Read ``providers.libvirt.use_sudo`` from simple YAML without PyYAML.

    The prerequisite checker deliberately has no third-party dependencies.
    This small indentation-aware scalar reader ignores same-named keys in
    project/provider blocks and falls back safely when the app config is
    absent, templated, or malformed.
    """
    parents = []
    for raw_line in text.splitlines():
        code = raw_line.split("#", 1)[0].rstrip()
        if not code.strip():
            continue
        indent = len(code) - len(code.lstrip(" "))
        match = re.match(
            r"^([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$", code.lstrip(" "))
        if not match:
            continue
        while parents and parents[-1][0] >= indent:
            parents.pop()
        key, value = match.groups()
        path = [parent_key for _, parent_key in parents]
        if path == ["providers", "libvirt"] and key == "use_sudo":
            value = value.strip().strip("\"'").lower()
            if value in ("true", "yes", "on", "1"):
                return True
            if value in ("false", "no", "off", "0"):
                return False
            return default
        if not value:
            parents.append((indent, key))
    return default


# Verbatim per-distro core stack lines (from doc/tutorial/README.md), minus the
# systemctl/usermod steps which the checker handles as their own fixes.
#
# Only *imperative* package-manager distros belong here. NixOS and Guix System
# are declarative: their libvirt/QEMU stack and the libvirtd service are enabled
# by editing the system config and rebuilding, never by an imperative "install"
# command -- see _NIXOS_CORE_ADVICE / _GUIX_CORE_ADVICE, which the core-tools
# check emits as advisory (printed, never auto-run) fixes instead.
#
# Gentoo USE-flag caveats: app-emulation/qemu needs QEMU_SOFTMMU_TARGETS to
# include x86_64 (e.g. QEMU_SOFTMMU_TARGETS="x86_64") to build qemu-system-x86_64;
# app-emulation/libvirt needs USE="virt-network qemu" for the QEMU/KVM driver and
# the default NAT network. virt-install / virt-clone ship in
# app-emulation/virt-manager; virt-sysprep ships in app-emulation/guestfs-tools.
# Services are systemd or OpenRC depending on the profile (OpenRC:
# `rc-update add libvirtd default && rc-service libvirtd start`).
_CORE_STACK = {
    "arch": ("sudo pacman -S --needed libvirt qemu-full virt-install "
             "guestfs-tools sshpass dnsmasq"),
    "debian": ("sudo apt update && sudo apt install -y libvirt-daemon-system "
               "libvirt-clients qemu-kvm virtinst guestfs-tools sshpass "
               "bridge-utils cloud-image-utils"),
    "rhel": ("sudo dnf install -y libvirt qemu-kvm virt-install "
             "guestfs-tools sshpass genisoimage"),
    "gentoo": ("sudo emerge --ask app-emulation/libvirt app-emulation/qemu "
               "app-emulation/virt-manager app-emulation/guestfs-tools "
               "net-dns/dnsmasq app-admin/sudo"),
}

# Declarative core-stack remediation for NixOS: printed as advisory text, never
# offered to auto-run (a Fix with no runnable commands).
_NIXOS_CORE_ADVICE = (
    "NixOS is declarative -- enable the libvirt/QEMU stack in "
    "/etc/nixos/configuration.nix:\n"
    "virtualisation.libvirtd.enable = true;\n"
    "programs.virt-manager.enable = true;\n"
    "environment.systemPackages = with pkgs; [ virt-manager qemu guestfs-tools cloud-utils ];\n"
    "users.users.<you>.extraGroups = [ \"libvirtd\" \"kvm\" ];\n"
    "then apply with: sudo nixos-rebuild switch"
)

# Declarative core-stack remediation for Guix System (advisory, never auto-run).
_GUIX_CORE_ADVICE = (
    "Guix System is declarative -- add libvirt to your operating-system in "
    "/etc/config.scm:\n"
    "(use-service-modules virtualization)  ; provides libvirt-service-type\n"
    "in (services ...): (service libvirt-service-type)\n"
    "in your (user-account ...): "
    "(supplementary-groups '(\"libvirt\" \"kvm\" \"wheel\"))\n"
    "then apply with: sudo guix system reconfigure /etc/config.scm\n"
    "user CLI tools can also be added with: guix install qemu virt-manager libguestfs"
)

# Declarative libvirtd-service remediation (advisory) for NixOS.
_NIXOS_LIBVIRTD_ADVICE = (
    "enable libvirtd declaratively: set virtualisation.libvirtd.enable = true; "
    "in /etc/nixos/configuration.nix, then sudo nixos-rebuild switch"
)

# Package name for a cloud-init seed-ISO tool, per family. On NixOS/Guix these
# are individual user CLI tools, so an imperative install is legitimate.
_SEED_PKG = {
    "arch": "libisoburn",
    "debian": "cloud-image-utils",
    "rhel": "genisoimage",
    "gentoo": "dev-libs/libisoburn",
    "nixos": "cloud-utils",
    "guix": "xorriso",
}

# Single-package names for individually-missing tools, per family.
_TOOL_PKG = {
    "rsync": {"arch": "rsync", "debian": "rsync", "rhel": "rsync",
              "gentoo": "net-misc/rsync", "nixos": "rsync", "guix": "rsync"},
    "sshpass": {"arch": "sshpass", "debian": "sshpass", "rhel": "sshpass",
                "gentoo": "net-misc/sshpass", "nixos": "sshpass", "guix": "sshpass"},
    "ansible": {"arch": "ansible", "debian": "ansible", "rhel": "ansible",
                "gentoo": "app-admin/ansible", "nixos": "ansible", "guix": "ansible"},
    "zstd": {"arch": "zstd", "debian": "zstd", "rhel": "zstd",
             "gentoo": "app-arch/zstd", "nixos": "zstd", "guix": "zstd"},
    # Debian-family releases supported by Boxman, including Ubuntu 22.04 and
    # Debian 12, ship the standalone virt-* applications in guestfs-tools.
    # libguestfs-tools is a compatibility/meta package there; keep sysprep and
    # sparsify guidance aligned on the package that owns both executables.
    "virt-sparsify": {"arch": "guestfs-tools", "debian": "guestfs-tools", "rhel": "guestfs-tools",
                      "gentoo": "app-emulation/libguestfs", "nixos": "guestfs-tools", "guix": "libguestfs"},
    "virt-sysprep": {"arch": "guestfs-tools", "debian": "guestfs-tools", "rhel": "guestfs-tools",
                     "gentoo": "app-emulation/guestfs-tools", "nixos": "guestfs-tools", "guix": "libguestfs"},
    "openssh": {"arch": "openssh", "debian": "openssh-client", "rhel": "openssh-clients",
                "gentoo": "net-misc/openssh", "nixos": "openssh", "guix": "openssh"},
}


def install_cmd(family, pkgs):
    """Build the package-manager install command for `pkgs` (a space string).

    For NixOS and Guix this is only ever used for *individual user CLI tools*
    (rsync, sshpass, a seed-ISO tool, ...), which may legitimately be installed
    imperatively into the user profile. The declarative-only bits (the libvirtd
    service, the core stack, group membership) never go through here -- they are
    emitted as advisory fixes instead.
    """
    if family == "arch":
        return "sudo pacman -S --needed " + pkgs
    if family == "debian":
        return "sudo apt update && sudo apt install -y " + pkgs
    if family == "rhel":
        return "sudo dnf install -y " + pkgs
    if family == "gentoo":
        return "sudo emerge --ask " + pkgs
    if family == "nixos":
        atoms = " ".join("nixpkgs#" + pkg for pkg in pkgs.split())
        return "nix profile install " + atoms
    if family == "guix":
        return "guix install " + pkgs
    return None


def sudo_nopasswd_covers(sudo_l_output, binary):
    """Best-effort: does `sudo -n -l` output grant passwordless `binary`?

    Returns True if a NOPASSWD rule appears to cover the binary (either an
    explicit `NOPASSWD: ALL`, or a NOPASSWD line naming the binary's basename).
    """
    base = os.path.basename(binary)
    for line in sudo_l_output.splitlines():
        if "NOPASSWD" not in line:
            continue
        rhs = line.split("NOPASSWD", 1)[1]
        if re.search(r":\s*ALL\b", rhs):
            return True
        if re.search(rf"[/\s]{re.escape(base)}(\b|,|$)", rhs):
            return True
    return False


def worst_status(statuses):
    """Return the most severe status in an iterable (for exit-code decisions)."""
    order = [FAIL, WARN, INFO, SKIP, OK]
    present = set(statuses)
    for status in order:
        if status in present:
            return status
    return OK


def parse_firewall_backend(text):
    """Return ``(configured_backend, option_supported)`` from network.conf text.

    libvirt's shipped network.conf documents ``firewall_backend`` even when the
    setting itself is commented out, so the option's mere presence in the file
    is a reliable signal that this build understands it.
    """
    match = re.search(r'^\s*firewall_backend\s*=\s*"?([a-z]+)"?', text, re.M)
    # anchored on a real declaration, set or commented out -- the bare word
    # could appear in unrelated prose and would then claim false support
    supported = re.search(r"^\s*#?\s*firewall_backend\s*=", text, re.M) is not None
    return (match.group(1) if match else ""), supported


def iptables_forward_facts(text):
    """Forward-relevant facts from ``iptables -S`` (filter table) output.

    Returns ``(rules_text, drop_closures, policy_drop)``.  Only ``-A FORWARD``
    lines count as
    evidence, plus libvirt's own ``LIBVIRT_FW*`` chains -- and those only when
    FORWARD still jumps into them, because a populated chain nothing jumps to
    is inert.  Docker's rebuild removes the jumps, which is exactly the state
    we must not mistake for a healthy one.
    """
    lines = [line.strip() for line in text.splitlines()]
    jumped = any(re.match(r"-A FORWARD .*-j LIBVIRT_", line) for line in lines)
    keep = []
    for line in lines:
        if re.match(r"-A FORWARD\b", line):
            keep.append(line)
        elif jumped and re.match(r"-A LIBVIRT_FW", line):
            keep.append(line)
    policy_drop = any(re.match(r"-P FORWARD DROP\b", line) for line in lines)
    rules = "\n".join(keep)
    # When FORWARD drops by default, its own rules are what can still rescue a
    # packet before the policy applies -- so they decide which bridges are
    # actually at risk.
    return rules, ([rules] if policy_drop else []), policy_drop


def _jump_targets(rule):
    """Chain names a rule hands control to (``jump`` / ``goto``)."""
    targets = []
    for expr in rule.get("expr") or []:
        if not isinstance(expr, dict):
            continue
        for verb in ("jump", "goto"):
            spec = expr.get(verb)
            if isinstance(spec, dict) and spec.get("target"):
                targets.append(spec["target"])
    return targets


def nft_forward_facts(json_text):
    """Forward-hook facts from ``nft -j list ruleset`` output.

    Returns ``(rules_text, drop_closures, policy_drop, libvirt_table,
    parsed)``: the serialised rules reachable from the forward hook, one
    closure per chain that *drops by default*, whether any such chain exists, whether libvirt owns a table (proof that it is using the
    nftables backend instead of sharing docker's), and whether the output was
    understood at all.

    The drop subset matters because a default drop only kills what its own
    chain did not already accept -- so it is per-bridge, not per-host.

    ``parsed`` guards against schema drift.  If a future nft speaks a shape
    this function does not recognise, every fact above comes back empty and
    silently absent -- which would read as "libvirt is not using nftables" and
    warn a perfectly healthy host.  Saying "I did not understand this" instead
    keeps that failure honest.

    The JSON form is used rather than the human-readable listing because the
    latter cannot be scanned safely: it also contains nat and mangle, which
    survive the very wipe we are looking for.  libvirt's mangle CHECKSUM rule
    names the bridge, so a naive scan finds it and reports a dead network as
    healthy.
    """
    try:
        doc = json.loads(json_text)
    except (ValueError, TypeError):
        return "", [], False, False, False

    items = doc.get("nftables", []) if isinstance(doc, dict) else []
    if not isinstance(items, list) or not items:
        return "", [], False, False, False

    reachable, dropping, policy_drop, libvirt_table = set(), set(), False, False
    for item in items:
        if not isinstance(item, dict):
            continue
        table = item.get("table")
        if isinstance(table, dict) and str(table.get("name", "")).startswith("libvirt"):
            libvirt_table = True
        chain = item.get("chain")
        if isinstance(chain, dict) and chain.get("hook") == "forward":
            key = (chain.get("family"), chain.get("table"), chain.get("name"))
            reachable.add(key)
            if chain.get("policy") == "drop":
                policy_drop = True
                dropping.add(key)

    by_chain = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        rule = item.get("rule")
        if isinstance(rule, dict):
            key = (rule.get("family"), rule.get("table"), rule.get("chain"))
            by_chain.setdefault(key, []).append(rule)

    # Base chains are only the entry point. libvirt's nftables backend keeps
    # its `forward` base chain nearly empty and jumps to guest_cross /
    # guest_input / guest_output, which is where the bridge is actually named
    # -- so stopping at base chains would find no rules for a perfectly
    # healthy network. Follow jump/goto within the same table until the
    # reachable set stops growing.
    def close_over(seed):
        found, pending = set(seed), list(seed)
        while pending:
            family, table_name, chain_name = pending.pop()
            for rule in by_chain.get((family, table_name, chain_name), []):
                for target in _jump_targets(rule):
                    nxt = (family, table_name, target)
                    if nxt not in found:
                        found.add(nxt)
                        pending.append(nxt)
        return found

    def serialise(keys):
        out = []
        for key in sorted(keys, key=lambda k: [str(part) for part in k]):
            for rule in by_chain.get(key, []):
                out.append(json.dumps(rule, sort_keys=True))
        return "\n".join(out)

    # one closure per dropping chain, not a merged blob: each such chain kills
    # independently, so a bridge must be accepted in every one of them
    drop_closures = [serialise(close_over([key])) for key in
                     sorted(dropping, key=lambda k: [str(part) for part in k])]
    return (serialise(close_over(reachable)), drop_closures,
            policy_drop, libvirt_table, True)


def wiped_networks(networks, evidence):
    """Networks that should carry forwarding rules but have none.

    Word-anchored, not a substring test: plain containment would let virbr10's
    rules vouch for virbr1 and hide a genuinely wiped network.
    """
    return [name for name, bridge, expects in networks
            if expects and not re.search(rf"\b{re.escape(bridge)}\b", evidence)]


def forwarding_verdict(networks, evidence, docker_present, backend,
                       policy_drop=False, drop_evidence="", complete_view=True):
    """Grade the host forward path from facts already gathered.

    ``networks`` is ``[(name, bridge, expects_rules), ...]`` for the active
    libvirt networks that care about forwarding; ``expects_rules`` is False for
    modes libvirt deliberately writes no rules for (``open``), which must not
    be reported as wiped.  ``evidence`` is the forward-scoped rule text --
    never a whole ruleset.  ``complete_view`` is False when part of the ruleset
    could not be read, in which case a bridge we fail to find is reported as
    unconfirmed rather than wiped.  Returns ``(status, detail, needs_fix)``.

    Docker and libvirt both write the ``filter`` table.  Docker rebuilds it on
    every restart and leaves the FORWARD policy at DROP; libvirt's rules -- and
    boxman's own routed-network FORWARD rules -- are collateral damage, and
    nothing re-applies them.  Every base chain on the forward hook is evaluated
    and a drop in any of them is final, so libvirt's ACCEPTs cannot rescue the
    packet.  The nat table usually survives, so guests keep NATing while
    forwarding is dead: it reads as a slow network, not a firewall fault.
    """
    if not networks:
        return SKIP, "no active forwarding libvirt networks", False

    # Word-anchored, not a substring test: plain containment would let
    # virbr10's rules vouch for virbr1 and hide a genuinely wiped network.
    # One entry per chain that drops by default, holding the rules that chain
    # can still rescue a packet with. Merging them would let an accept in one
    # dropping chain vouch for a bridge that a second dropping chain kills.
    if isinstance(drop_evidence, str):
        drop_evidence = [drop_evidence] if drop_evidence else []
    closures = list(drop_evidence) or ([""] if policy_drop else [])

    wiped = wiped_networks(networks, evidence)
    # A default drop only kills what its own chain did not already accept, so
    # this is decided per bridge. It catches the half-fixed host (libvirt moved
    # to its own table, policy never cleared) and 'open' networks alike -- for
    # those libvirt writes nothing by design, which is exactly why a drop is
    # fatal to them.
    at_risk = [name for name, bridge, _ in networks
               if any(not re.search(rf"\b{re.escape(bridge)}\b", closure)
                      for closure in closures)]

    if wiped and not complete_view:
        return INFO, ("part of the ruleset was unreadable, so the rules for "
                      f"{', '.join(wiped)} could not be confirmed either way"), False

    if wiped:
        detail = (f"no forwarding rules for active network(s): {', '.join(wiped)}\n"
                  "guests will NAT but never forward")
        if docker_present:
            detail += "\ndocker shares this table and rebuilds it on restart"
        return WARN, detail, True

    if at_risk and complete_view:
        detail = ("a chain at the forward hook drops by default and nothing "
                  f"in it accepts: {', '.join(at_risk)}\n"
                  "a drop in any table at the hook is final, so these guests "
                  "cannot forward")
        if backend == "nftables":
            # the half-fixed host: rules protected, policy never cleared
            detail += "\nlibvirt already has its own table -- only the policy is left"
        return WARN, detail, True

    if docker_present and backend != "nftables":
        detail = ("forwarding works now, but libvirt shares the filter table "
                  "with docker -- the next `systemctl restart docker` wipes "
                  "these rules")
        if policy_drop:
            detail += "\nFORWARD policy is DROP, so nothing survives the wipe"
        return WARN, detail, True

    if not backend and not complete_view:
        return INFO, ("rules are in place, but libvirt's firewall backend "
                      "could not be determined from here"), False

    return OK, "forwarding rules present for: {}".format(
        ", ".join(name for name, _, _ in networks)), False


# --------------------------------------------------------------------------- #
# Small result containers                                                      #
# --------------------------------------------------------------------------- #
class Fix:
    def __init__(self, description, commands=None, needs_sudo=False,
                 needs_relogin=False, disruptive=False):
        self.description = description
        self.commands = list(commands or [])
        self.needs_sudo = needs_sudo
        self.needs_relogin = needs_relogin
        # Restarts a running service. Always confirmed interactively, even
        # under --yes, so an unattended run can never bounce a container host.
        self.disruptive = disruptive

    @property
    def runnable(self):
        return bool(self.commands)


class Result:
    def __init__(self, name, status, detail="", fix=None):
        self.name = name
        self.status = status
        self.detail = detail
        self.fix = fix


# --------------------------------------------------------------------------- #
# Process helpers                                                             #
# --------------------------------------------------------------------------- #
def have(cmd):
    return shutil.which(cmd) is not None


def run_capture(args, timeout=20):
    """Run a command, capturing merged stdout/stderr. Never raises.

    Returns (returncode, text).  rc 127 => not found, 124 => timed out.
    """
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or ""
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except Exception as exc:  # pragma: no cover - defensive
        return 1, str(exc)


def user_group_names():
    """Return (set_of_group_names, username) for the current process."""
    if not _HAVE_UNIX_IDS:
        return set(), ""
    names = set()
    try:
        username = pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        username = ""
    for gid in list(os.getgroups()) + ([os.getgid()] if hasattr(os, "getgid") else []):
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except (KeyError, OverflowError):
            pass
    return names, username


# --------------------------------------------------------------------------- #
# The doctor                                                                  #
# --------------------------------------------------------------------------- #
class Doctor:
    def __init__(self, opts):
        self.opts = opts
        self.os = self._detect_os()
        self.family = self.os["family"]
        self.runtime = self._detect_runtime(opts.runtime)
        self.use_sudo = self._detect_libvirt_use_sudo()
        self.results = []
        self.manual_steps = []
        self.relogin_needed = False
        self.use_color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        self.interactive = (
            sys.stdin.isatty() and not opts.check_only
        ) or bool(opts.yes)

    # -- presentation ------------------------------------------------------- #
    def c(self, text, name):
        if not self.use_color:
            return text
        return "\033[{}m{}\033[0m".format(_ANSI.get(name, "0"), text)

    def section(self, title):
        print("\n" + self.c(f"== {title} ==", "bold"))

    def _print_result(self, result, prefix=""):
        tag, color = _STATUS_TAG[result.status]
        label = self.c(f"[{tag}]", color)
        print(f"{prefix}{label} {result.name}")
        if result.detail:
            for line in result.detail.splitlines():
                print("       " + line)

    # -- check driver ------------------------------------------------------- #
    def check(self, name, probe):
        """Run a probe -> (status, detail, fix), print it, offer guided fix."""
        status, detail, fix = probe()
        result = Result(name, status, detail, fix)
        self.results.append(result)
        self._print_result(result)
        if fix and status in (FAIL, WARN):
            self._handle_fix(result, probe)
        return result

    def _show_fix(self, fix):
        # Descriptions are usually one line; advisory (declarative-distro) fixes
        # carry a multi-line config snippet, so indent continuation lines.
        desc_lines = fix.description.splitlines() or [""]
        print("       " + self.c("fix: " + desc_lines[0], "cyan"))
        for extra in desc_lines[1:]:
            print("       " + self.c("     " + extra, "cyan"))
        for cmd in fix.commands:
            print("         " + self.c("$ " + cmd, "dim"))
        if fix.needs_relogin:
            print("       " + self.c("(requires you to log out and back in)", "yellow"))

    def _handle_fix(self, result, probe):
        fix = result.fix
        self._show_fix(fix)
        if fix.disruptive:
            print("       " + self.c(
                "(disruptive: restarts services -- running containers and guest "
                "connectivity are interrupted)", "yellow"))

        # Advisory-only situations: read-only mode, no runnable commands, or a
        # non-interactive session the user hasn't pre-approved with --yes.
        if not fix.runnable or self.opts.check_only or not self.interactive:
            self._remember_manual(result)
            return

        # A disruptive fix needs a human at a terminal: --yes does not cover
        # it, and neither does a piped "y" (`yes | check_prerequisites.py`).
        # Bouncing a container host is not something an unattended run may
        # decide for itself.
        if fix.disruptive and not sys.stdin.isatty():
            self._remember_manual(
                result, note="needs a terminal to confirm; not run")
            return

        auto = self.opts.yes and not fix.disruptive
        if not (auto or self._ask("       -> run the above now?")):
            self._remember_manual(result)
            return

        ok = self._run_fix(fix)
        if fix.needs_relogin:
            self.relogin_needed = True
            self._remember_manual(result, note="applied; re-run this checker after logging back in")
            return
        if not ok:
            self._remember_manual(result)
            return

        # Re-probe once to reflect the new state.
        status, detail, new_fix = probe()
        result.status, result.detail, result.fix = status, detail, new_fix
        self._print_result(result, prefix="       -> ")
        if status in (FAIL, WARN):
            self._remember_manual(result)

    def _remember_manual(self, result, note=None):
        entry = result.name
        if note:
            entry += f" ({note})"
        elif result.fix and result.fix.commands:
            entry += ": " + " ; ".join(result.fix.commands)
        elif result.fix:
            entry += ": " + result.fix.description
        if entry not in self.manual_steps:
            self.manual_steps.append(entry)

    def _ask(self, prompt):
        try:
            answer = input(f"{prompt} [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt, OSError):
            # OSError: the terminal went away mid-prompt (EIO on hangup).
            # Declining is the only safe reading of "no answer".
            print()
            return False
        return answer in ("y", "yes")

    def _run_fix(self, fix):
        ok = True
        for cmd in fix.commands:
            print("       " + self.c("$ " + cmd, "dim"))
            try:
                rc = subprocess.call(cmd, shell=True)
            except KeyboardInterrupt:
                print()
                return False
            if rc != 0:
                ok = False
                print("       " + self.c(f"command exited with status {rc}", "yellow"))
                if fix.disruptive:
                    # Later commands restart services. Carrying on past a
                    # failed edit would apply the disruption without the
                    # change it was meant to accompany.
                    print("       " + self.c(
                        "stopping here; the remaining commands were not run",
                        "yellow"))
                    return False
        return ok

    # -- environment discovery --------------------------------------------- #
    def _detect_os(self):
        text = ""
        if os.path.exists("/etc/os-release"):
            try:
                with open("/etc/os-release") as handle:
                    text = handle.read()
            except OSError:
                text = ""
        data = parse_os_release(text)
        osid = data.get("ID", "")
        like = data.get("ID_LIKE", "")
        return {
            "id": osid,
            "like": like,
            "pretty": data.get("PRETTY_NAME") or platform.platform(),
            "family": classify_family(osid, like),
        }

    def _detect_runtime(self, explicit):
        if explicit and explicit != "auto":
            return explicit
        path = os.path.expanduser("~/.config/boxman/boxman.yml")
        if os.path.exists(path):
            try:
                with open(path) as handle:
                    for line in handle:
                        match = re.match(r"^\s*runtime\s*:\s*([A-Za-z0-9_-]+)", line)
                        if match:
                            value = match.group(1).strip().strip("\"'")
                            if value in ("local", "docker"):
                                return value
            except OSError:
                pass
        return "local"

    def _detect_libvirt_use_sudo(self):
        path = os.path.expanduser("~/.config/boxman/boxman.yml")
        try:
            with open(path) as handle:
                return configured_libvirt_use_sudo(handle.read(), default=False)
        except OSError:
            return False

    def is_virtualized(self):
        rc, out = run_capture(["systemd-detect-virt"])
        if rc == 0:
            return out.strip() not in ("", "none")
        return None  # unknown

    def _install_fix(self, description, pkgs, extra_cmds=None, relogin=False):
        cmd = install_cmd(self.family, pkgs)
        commands = []
        if cmd:
            commands.append(cmd)
        commands.extend(extra_cmds or [])
        if not commands:
            description += (" (unrecognized distro '{}' -- install {} with your "
                            "package manager)".format(self.os["id"] or "?", pkgs))
        return Fix(description, commands, needs_sudo=True, needs_relogin=relogin)

    def _core_stack_fix(self):
        """Remediation for a missing libvirt/QEMU core stack, per distro.

        Imperative (a runnable install command) on package-manager distros
        (arch/debian/rhel/gentoo). Advisory-only (a Fix with no commands, so it
        is printed but never offered to auto-run) on the declarative distros
        NixOS and Guix System, whose stack + service are enabled by editing the
        system config and rebuilding.
        """
        stack = _CORE_STACK.get(self.family)
        if stack:
            return Fix("install the libvirt/QEMU stack for your distro",
                       [stack], needs_sudo=True)
        if self.family == "nixos":
            return Fix(_NIXOS_CORE_ADVICE, commands=[])
        if self.family == "guix":
            return Fix(_GUIX_CORE_ADVICE, commands=[])
        fix = Fix("install the libvirt/QEMU stack for your distro", [],
                  needs_sudo=True)
        fix.description += (" -- unknown distro '%s'; install libvirt, "
                            "qemu-kvm, virt-install, virt-clone" % (self.os["id"] or "?"))
        return fix

    # ===================================================================== #
    # Check groups                                                          #
    # ===================================================================== #
    def run(self):
        self._header()
        self.check_env()
        self.check_virt_hw()
        if self.runtime == "docker":
            self.check_docker_runtime()
        else:
            self.check_local_runtime()
        self.check_config_and_capacity()
        return self._summary()

    def _header(self):
        print(self.c("Boxman prerequisites checker", "bold"))
        print(f"  host    : {self.os['pretty']}")
        print(f"  family  : {self.family}   runtime: {self.runtime}")
        mode = "check-only (read-only)" if self.opts.check_only else (
            "auto-fix (--yes)" if self.opts.yes else "guided (asks before any change)")
        print(f"  mode    : {mode}")
        if not self.opts.check_only:
            print(self.c("  Nothing is changed unless you confirm each fix.", "dim"))

    # -- env basics -------------------------------------------------------- #
    def check_env(self):
        self.section("Environment")

        def python_version():
            ver = ".".join(str(p) for p in sys.version_info[:3])
            if sys.version_info[:2] >= (3, 10):  # noqa: UP036 - standalone installer must parse on end-user Python < 3.10
                return OK, f"Python {ver} (boxman needs >= 3.10)", None
            fix = Fix(
                "install Python >= 3.10 (e.g. `conda create -n boxman python=3.12` "
                "or a system python3.10+), then reinstall boxman into it",
                commands=[],
            )
            return FAIL, f"Python {ver} is too old; boxman needs >= 3.10", fix

        self.check("Python version", python_version)

        def boxman_installed():
            env = os.environ.get("CONDA_DEFAULT_ENV") or os.environ.get("VIRTUAL_ENV")
            env_note = (f"active env: {env}") if env else "no conda/venv detected"
            if have("boxman"):
                rc, out = run_capture(["boxman", "--version"])
                ver = out.strip().splitlines()[0] if (rc == 0 and out.strip()) else "installed"
                return OK, f"{ver} ({shutil.which('boxman')}); {env_note}", None
            fix = Fix("install boxman into your active environment",
                      commands=["pip install boxman"])
            return WARN, f"`boxman` not on PATH; {env_note}", fix

        self.check("boxman on PATH", boxman_installed)

        def python_deps():
            mods = ["yaml", "invoke", "jinja2", "lxml", "passlib"]
            missing = []
            for mod in mods:
                rc, _ = run_capture([sys.executable, "-c", f"import {mod}"])
                if rc != 0:
                    missing.append(mod)
            if not missing:
                return OK, "yaml, invoke, jinja2, lxml, passlib import cleanly", None
            hint = ("lxml needs system libxml2/libxslt; the rest come with "
                    "`pip install boxman`") if "lxml" in missing else \
                   "reinstall boxman to pull these"
            fix = Fix(f"install boxman's Python deps into {sys.executable} ({hint})",
                      commands=[f"{sys.executable} -m pip install {' '.join(missing)}"])
            return WARN, f"missing: {', '.join(missing)}", fix

        self.check("boxman Python deps", python_deps)

    # -- virtualization hardware ------------------------------------------- #
    def check_virt_hw(self):
        self.section("Virtualization hardware")

        def cpu_virt():
            flags = ""
            try:
                with open("/proc/cpuinfo") as handle:
                    for line in handle:
                        if line.startswith("flags") or line.startswith("Features"):
                            flags = line
                            break
            except OSError:
                return SKIP, "cannot read /proc/cpuinfo", None
            if " vmx" in flags or " svm" in flags:
                return OK, "hardware virtualization (VT-x/AMD-V) supported", None
            fix = Fix("enable Intel VT-x / AMD-V (SVM) in your BIOS/UEFI firmware",
                      commands=[])
            return FAIL, "no vmx/svm CPU flag -- virtualization off in BIOS or unsupported", fix

        self.check("CPU virtualization", cpu_virt)

        def dev_kvm():
            if os.path.exists("/dev/kvm"):
                return OK, "/dev/kvm present", None
            vendor_mod = "kvm_intel"
            try:
                with open("/proc/cpuinfo") as handle:
                    if "AuthenticAMD" in handle.read():
                        vendor_mod = "kvm_amd"
            except OSError:
                pass
            fix = Fix("load the KVM kernel module (needs VT-x/AMD-V enabled in BIOS)",
                      commands=[f"sudo modprobe kvm {vendor_mod}"], needs_sudo=True)
            return FAIL, "/dev/kvm missing -- KVM module not loaded", fix

        kvm = self.check("/dev/kvm device", dev_kvm)

        if kvm.status == OK:
            def kvm_access():
                if os.access("/dev/kvm", os.R_OK | os.W_OK):
                    return OK, "current user can read/write /dev/kvm", None
                fix = Fix("add yourself to the `kvm` group",
                          commands=["sudo usermod -aG kvm $USER"],
                          needs_sudo=True, needs_relogin=True)
                return FAIL, "no read/write access to /dev/kvm (group membership)", fix

            self.check("/dev/kvm access", kvm_access)

        self._check_nested_virt()

    def _check_nested_virt(self):
        virtualized = self.is_virtualized()
        if virtualized is False:
            return  # bare metal: nested virt is irrelevant
        if virtualized is None:

            def unknown():
                return SKIP, "systemd-detect-virt unavailable; skipping nested-virt check", None
            self.check("Nested virtualization", unknown)
            return

        def nested():
            for mod in ("kvm_intel", "kvm_amd"):
                path = f"/sys/module/{mod}/parameters/nested"
                if os.path.exists(path):
                    try:
                        with open(path) as handle:
                            val = handle.read().strip()
                    except OSError:
                        continue
                    if val in ("Y", "1"):
                        return OK, f"nested virtualization enabled ({mod})", None
                    fix = Fix(
                        "enable nested virt on the *host*, e.g. "
                        f"`echo 'options {mod} nested=1' | sudo tee /etc/modprobe.d/kvm.conf` "
                        "then reload the module",
                        commands=[])
                    return WARN, f"running in a VM and nested virt is disabled ({mod}={val})", fix
            return SKIP, "no kvm_intel/kvm_amd module parameter found", None

        self.check("Nested virtualization", nested)

    def _check_clone_sanitizer(self):
        """Report virt-sysprep as recommended, not a core runtime gate."""
        def clone_sanitizer():
            if have("virt-sysprep"):
                return OK, "virt-sysprep present for clone machine-ID reset", None
            package = _TOOL_PKG["virt-sysprep"].get(
                self.family, "guestfs-tools")
            return WARN, (
                "virt-sysprep missing; clone_machine_id=auto will warn and "
                "continue, while clone_machine_id=required will fail closed"
            ), self._install_fix(
                "install virt-sysprep for automatic clone machine-ID resets",
                package)

        return self.check("Clone machine-ID sanitizer", clone_sanitizer)

    # -- LOCAL runtime ----------------------------------------------------- #
    def check_local_runtime(self):
        self.section("libvirt / QEMU (local runtime)")

        def core_tools():
            required = ["virsh", "virt-install", "virt-clone", "qemu-img"]
            missing = [b for b in required if not have(b)]
            qemu_sys = [b for b in ("qemu-system-x86_64", "qemu-kvm", "kvm") if have(b)]
            if not qemu_sys:
                missing.append("qemu-system-x86_64")
            if not missing:
                return OK, ("virsh, virt-install, virt-clone, qemu-img, "
                            "qemu-system present"), None
            return FAIL, f"missing: {', '.join(missing)}", self._core_stack_fix()

        tools = self.check("Core libvirt/QEMU tools", core_tools)

        self._check_clone_sanitizer()

        def libvirtd_service():
            active = run_capture(["systemctl", "is-active", "libvirtd"])[1].strip()
            if active == "active":
                return OK, "libvirtd.service is active", None
            # modular libvirt daemons (newer distros)
            alt = run_capture(["systemctl", "is-active", "virtqemud"])[1].strip()
            if alt == "active":
                return OK, "virtqemud.service is active (modular libvirt)", None
            if self.family == "nixos":
                fix = Fix(_NIXOS_LIBVIRTD_ADVICE, commands=[])  # declarative: no auto-run
            elif self.family == "gentoo":
                # systemd profile: systemctl works. OpenRC profile: use rc-service.
                fix = Fix("enable and start libvirtd (systemd profile; on an OpenRC "
                          "profile use `sudo rc-update add libvirtd default && "
                          "sudo rc-service libvirtd start`)",
                          commands=["sudo systemctl enable --now libvirtd"], needs_sudo=True)
            else:
                fix = Fix("enable and start libvirtd",
                          commands=["sudo systemctl enable --now libvirtd"], needs_sudo=True)
            return FAIL, "libvirtd not active (state: %s)" % (active or "unknown"), fix

        if have("systemctl"):
            self.check("libvirtd service", libvirtd_service)

        if tools.status == OK or have("virsh"):
            def libvirt_conn():
                rc, out = run_capture(["virsh", "-c", "qemu:///system", "list", "--all"])
                if rc == 0:
                    return OK, "virsh can reach qemu:///system", None
                reason = out.strip().splitlines()[-1] if out.strip() else "connection failed"
                detail = f"cannot reach qemu:///system: {reason}"
                if "permission" in out.lower() or "authentication" in out.lower():
                    detail += "\n(usually a group-membership issue -- see the groups check below)"
                return FAIL, detail, None  # fix handled by service/groups checks

            self.check("libvirt connectivity", libvirt_conn)

        def groups():
            names, user = user_group_names()
            # NixOS names the libvirt group `libvirtd`; everyone else `libvirt`.
            libvirt_group = "libvirtd" if self.family == "nixos" else "libvirt"
            want = [libvirt_group, "kvm"]
            missing = [g for g in want if g not in names]
            if not missing:
                return OK, f"user '{user}' is in: {', '.join(want)}", None
            if self.family == "nixos":
                fix = Fix("add your user to the libvirtd/kvm groups declaratively: "
                          "users.users.<you>.extraGroups = [ \"libvirtd\" \"kvm\" ]; "
                          "in /etc/nixos/configuration.nix, then sudo nixos-rebuild switch",
                          commands=[], needs_relogin=True)
            elif self.family == "guix":
                fix = Fix("add your user to the libvirt/kvm groups declaratively: put "
                          "(supplementary-groups '(\"libvirt\" \"kvm\")) in your "
                          "user-account in /etc/config.scm, then sudo guix system "
                          "reconfigure /etc/config.scm",
                          commands=[], needs_relogin=True)
            else:
                fix = Fix("add yourself to the libvirt and kvm groups",
                          commands=["sudo usermod -aG libvirt,kvm $USER"],
                          needs_sudo=True, needs_relogin=True)
            # WARN, not FAIL: group membership is only the standard *means* to
            # reach /dev/kvm and the libvirt socket, and those ends are checked
            # directly ("/dev/kvm access", "libvirt connectivity"). If access
            # already works (world-readable device, polkit, sudo), missing group
            # membership isn't blocking -- the functional checks decide that.
            return WARN, f"user '{user}' not in group(s): {', '.join(missing)}", fix

        if _HAVE_UNIX_IDS:
            self.check("User groups (libvirt, kvm)", groups)

        if have("virsh"):
            def default_net():
                rc, out = run_capture(["virsh", "-c", "qemu:///system", "net-info", "default"])
                if rc != 0:
                    fix = Fix("define, start and autostart the default NAT network",
                              commands=[
                                  "sudo virsh net-define /usr/share/libvirt/networks/default.xml",
                                  "sudo virsh net-start default",
                                  "sudo virsh net-autostart default",
                              ], needs_sudo=True)
                    return WARN, "libvirt 'default' network is not defined", fix
                active = re.search(r"Active:\s*(\w+)", out)
                if active and active.group(1).lower() == "yes":
                    return OK, "'default' NAT network is active", None
                fix = Fix("start and autostart the default network",
                          commands=["sudo virsh net-start default",
                                    "sudo virsh net-autostart default"], needs_sudo=True)
                return WARN, "'default' network exists but is inactive", fix

            self.check("Default libvirt network", default_net)
            self._check_host_forwarding()

        def seed_tool():
            tools = ["cloud-localds", "genisoimage", "mkisofs", "xorrisofs", "xorriso"]
            found = [t for t in tools if have(t)]
            if found:
                return OK, f"cloud-init seed tool available ({found[0]})", None
            fix = self._install_fix(
                "install a cloud-init seed-ISO tool", _SEED_PKG.get(self.family, "genisoimage"))
            return FAIL, f"none of: {', '.join(tools)} (needed to build cloud-init seed ISOs)", fix

        self.check("cloud-init seed tool", seed_tool)

        self._check_simple_bin("sshpass", "sshpass", FAIL, "password-based SSH key injection")
        self._check_simple_bin("rsync", "rsync", FAIL, "image/template copy")

        def ssh_client():
            missing = [b for b in ("ssh", "ssh-keygen") if not have(b)]
            if not missing:
                return OK, "ssh and ssh-keygen present", None
            fix = self._install_fix("install the OpenSSH client",
                                    _TOOL_PKG["openssh"].get(self.family, "openssh-client"))
            return WARN, f"missing: {', '.join(missing)}", fix

        self.check("SSH client tools", ssh_client)

        self._check_sudo_rights()
        self._check_optional_tools()

    def _check_simple_bin(self, label, binary, severity, feature):
        def probe():
            if have(binary):
                return OK, f"{binary} present", None
            pkg = _TOOL_PKG.get(binary, {}).get(self.family, binary)
            fix = self._install_fix(f"install {binary}", pkg)
            return severity, f"{binary} not found (needed for {feature})", fix

        self.check(label, probe)

    def _check_sudo_rights(self):
        def sudo_rights():
            sysprep_uses_sudo = getattr(self, "use_sudo", False)
            if not have("sudo"):
                fix = self._install_fix("install sudo", "sudo")
                return WARN, "sudo not found; libvirt network/cleanup steps need it", fix
            rc, out = run_capture(["sudo", "-n", "-l"])
            if rc != 0:
                fix = Fix(
                    "grant passwordless sudo for the commands boxman runs, e.g. a "
                    "/etc/sudoers.d/boxman line: "
                    "`%s ALL=(root) NOPASSWD: /usr/bin/virsh, "
                    "/usr/bin/virt-sysprep, /usr/bin/qemu-img, /usr/sbin/iptables, "
                    "/usr/sbin/ip, /usr/bin/rsync, /bin/rm`" % (
                        user_group_names()[1] or "$USER"),
                    commands=[])
                return WARN, ("passwordless sudo not available; boxman's automatic "
                              "iptables/NAT and cleanup steps fail when run "
                              "non-interactively"), fix
            # passwordless sudo exists -- check the scope that bites people.
            iptables_ok = sudo_nopasswd_covers(out, "iptables")
            sysprep_ok = (
                not sysprep_uses_sudo
                or sudo_nopasswd_covers(out, "virt-sysprep")
            )
            qemu_ok = sudo_nopasswd_covers(out, "qemu-img")
            rm_ok = sudo_nopasswd_covers(out, "rm")
            gaps = []
            if not iptables_ok:
                gaps.append("iptables/ip (NAT & isolated networks, netlab bridges)")
            if not sysprep_ok:
                gaps.append(
                    "virt-sysprep (clone_machine_id=required needs NOPASSWD)")
            if not (qemu_ok and rm_ok):
                gaps.append("qemu-img/rm (destroy/cleanup silently no-ops without these)")
            if not gaps:
                covered = "virsh/qemu-img/iptables/rm"
                if sysprep_uses_sudo:
                    covered = "virsh/virt-sysprep/qemu-img/iptables/rm"
                return OK, "passwordless sudo covers " + covered, None
            fix = Fix(
                "widen NOPASSWD sudo scope in /etc/sudoers.d/boxman to include: "
                "virsh, virt-sysprep, qemu-img, iptables, ip, rsync, rm",
                commands=[])
            return WARN, "passwordless sudo present but missing: " + "; ".join(gaps), fix

        self.check("sudo rights", sudo_rights)

    # -- host packet forwarding (docker vs libvirt) ------------------------- #
    def _root_capture(self, args):
        """Run a root-only inspection command without ever prompting.

        Returns ``(rc, text)``.  Any non-zero rc means "we did not get a
        reliable answer" -- the caller must treat that as reduced visibility
        rather than as evidence of absence.  No attempt is made to classify
        sudo's refusal message: it is localised, so matching English text would
        silently misbehave under any other locale.
        """
        if getattr(os, "geteuid", lambda: 1)() == 0:
            return run_capture(args)
        if not have("sudo"):
            return 1, ""
        return run_capture(["sudo", "-n"] + args)

    def _libvirt_net_unit(self):
        """The systemd unit that owns libvirt's network rules on this host."""
        for unit in ("virtnetworkd", "libvirtd"):
            if run_capture(["systemctl", "is-active", unit])[1].strip() == "active":
                return unit
        # a socket-activated virtnetworkd is idle, not absent; restarting
        # libvirtd instead would fail outright on a modular install
        if run_capture(["systemctl", "is-active",
                        "virtnetworkd.socket"])[1].strip() == "active":
            return "virtnetworkd"
        return "libvirtd"

    def _libvirt_fw_backend(self):
        """``(configured_backend, option_supported)`` for this libvirt."""
        try:
            with open("/etc/libvirt/network.conf") as handle:
                return parse_firewall_backend(handle.read())
        except OSError:
            return "", False

    def _docker_manages_firewall(self, evidence):
        """Is a docker daemon actually writing firewall rules here?

        The ``docker`` binary alone proves nothing -- it is also present as a
        remote-only client and as podman's compatibility shim, neither of which
        touches this host's tables.  Warning those hosts about "the next
        `systemctl restart docker`" and offering to edit /etc/docker/daemon.json
        is pure noise.
        """
        if "DOCKER-USER" in evidence or "DOCKER-FORWARD" in evidence:
            return True
        if not have("docker"):
            return False
        for query in ("is-active", "is-enabled"):
            if run_capture(["systemctl", query, "docker"])[1].strip() in (
                    "active", "enabled"):
                return True
        return False

    def _docker_supports_no_drop(self):
        """Does this engine understand ``ip-forward-no-drop``?

        Distro engines from the 24.x/26.x era do not, and dockerd refuses to
        start on an unknown daemon.json directive -- so writing the key and
        restarting would leave docker down. That is the worst outcome this
        script can produce, so anything short of proof counts as "no".
        """
        if not have("dockerd"):
            return False
        rc, out = run_capture(["dockerd", "--help"])
        return rc == 0 and "ip-forward-no-drop" in out

    def _forwarding_fix(self, backend, docker_manages, wiped=False):
        """Commands that take libvirt out of the table docker rebuilds.

        ``backend`` is the *effective* backend, so a host that already keeps
        libvirt in its own table is only offered the docker half.
        ``docker_manages`` is the same signal the verdict used: offering to
        edit /etc/docker/daemon.json and restart docker just because the
        binary exists would contradict the reason we stopped trusting it.
        """
        configured, supported = self._libvirt_fw_backend()
        unit = self._libvirt_net_unit()

        # Migrate libvirt FIRST. The docker half ends in a docker restart --
        # the very event that wipes the shared table -- so running it before
        # libvirt has moved out means an abort at any later step leaves a host
        # that was merely at risk actually broken.
        libvirt_cmds = []
        if supported and backend != "nftables":
            libvirt_cmds = [
                "sudo sh -c '[ ! -f /etc/libvirt/network.conf ] || "
                "cp -an /etc/libvirt/network.conf "
                "/etc/libvirt/network.conf.boxman-bak'",
                # the leading newline guards against a file with no trailing
                # one, where a bare append would fuse onto the last setting
                "sudo sh -c 'sed -i "
                "\"/^[[:space:]]*firewall_backend[[:space:]]*=/d\" "
                "/etc/libvirt/network.conf && printf "
                "\"\\nfirewall_backend = \\\"nftables\\\"\\n\" "
                ">> /etc/libvirt/network.conf'",
                f"sudo systemctl restart {unit}",
            ]

        # the flag stops docker *setting* the policy; it does not clear one
        # docker already set, so this one-off reset is required. Both families:
        # docker manages ip6tables too, and leaving the v6 policy at DROP makes
        # the check warn forever after an otherwise successful fix.
        policy_reset = ["sudo iptables -P FORWARD ACCEPT"]
        if have("ip6tables"):
            policy_reset.append("sudo ip6tables -P FORWARD ACCEPT")

        docker_cmds, stale_engine = [], False
        if docker_manages and self._docker_supports_no_drop():
            docker_cmds = [
                "sudo sh -c '[ ! -f /etc/docker/daemon.json ] || "
                "cp -an /etc/docker/daemon.json "
                "/etc/docker/daemon.json.boxman-bak'",
                # merge and restart are chained: if the merge fails, restarting
                # docker would perform the very wipe this fix exists to prevent
                "sudo python3 -c \"import json,pathlib;"
                "p=pathlib.Path('/etc/docker/daemon.json');"
                "d=json.loads(p.read_text().strip() or '{}') if p.exists() else {};"
                "d['ip-forward-no-drop']=True;"
                "p.parent.mkdir(parents=True,exist_ok=True);"
                "p.write_text(json.dumps(d,indent=2))\""
                " && sudo systemctl restart docker",
            ] + policy_reset
        elif docker_manages:
            # Engine too old for the directive: clearing the policy still
            # restores connectivity now, but docker will set it again on its
            # next restart. Editing daemon.json here would stop dockerd dead.
            docker_cmds, stale_engine = list(policy_reset), True

        commands = libvirt_cmds + docker_cmds

        # Re-apply libvirt's rules when nothing above will and they are either
        # already gone or about to be: a docker restart against the shared
        # table takes them with it. Gating this on `wiped` alone left a merely
        # at-risk host actually broken, with the fix reporting success.
        restarts_docker = any("restart docker" in cmd for cmd in docker_cmds)
        if not libvirt_cmds and (
                wiped or (restarts_docker and backend != "nftables")):
            commands.append(f"sudo systemctl restart {unit}")
        if backend == "nftables" and docker_manages:
            description = ("stop docker forcing FORWARD to DROP -- libvirt "
                           "already has its own table")
        elif backend == "nftables":
            # nothing here to automate: docker is not the one dropping, so
            # whatever set the policy (firewalld, a hand-rolled ruleset, an
            # admin) has to be found before anything can be recommended
            description = ("libvirt already has its own table; find what set "
                           "the FORWARD policy to DROP -- `iptables -S FORWARD "
                           "| head -1` and `nft list ruleset | grep -B5 'hook "
                           "forward'` -- and let that owner accept the bridge")
        elif libvirt_cmds:
            description = ("give libvirt its own nftables table and stop "
                           "docker forcing FORWARD to DROP (a .boxman-bak "
                           "backup is written next to each file)")
        elif docker_manages:
            description = ("clear the FORWARD policy and re-apply libvirt's "
                           "rules; this libvirt build has no firewall_backend "
                           "option, so it cannot be moved out of docker's "
                           "table")
        else:
            description = "re-apply libvirt's network rules"
        if stale_engine:
            description += ("; this docker engine predates "
                            "`ip-forward-no-drop`, so daemon.json is left "
                            "alone -- upgrade docker or the policy returns on "
                            "its next restart")
        return Fix(description, commands, needs_sudo=True,
                   disruptive=bool(commands))

    def _forward_evidence(self):
        """Gather forward-scoped rules from both firewall views.

        Returns ``(evidence, drop_closures, policy_drop, libvirt_table,
        complete_view)``.  Only rules on the forward hook are collected:
        scanning a whole ruleset would let nat and mangle -- which survive a
        docker rebuild -- vouch for filter rules that are long gone.
        """
        evidence, drops = [], []
        policy_drop, libvirt_table, complete_view = False, False, True

        if have("iptables"):
            rc, out = self._root_capture(["iptables", "-S"])
            if rc == 0:
                rules, drop_rules, drop = iptables_forward_facts(out)
                evidence.append(rules)
                drops.extend(drop_rules)
                policy_drop = policy_drop or drop
            else:
                complete_view = False
        else:
            complete_view = False

        if have("nft"):
            rc, out = self._root_capture(["nft", "-j", "list", "ruleset"])
            rules, drop_rules, drop, libvirt_table, parsed = \
                nft_forward_facts(out) if rc == 0 else ("", [], False, False, False)
            if parsed:
                evidence.append(rules)
                drops.extend(drop_rules)
                policy_drop = policy_drop or drop
            else:
                # ran but told us nothing we understood -- same standing as
                # not having been allowed to look
                complete_view = False
        else:
            complete_view = False

        return ("\n".join(evidence), drops, policy_drop,
                libvirt_table, complete_view)

    def _check_host_forwarding(self):
        """Report whether libvirt's forwarding rules are intact -- see
        :func:`forwarding_verdict` for why docker keeps removing them."""
        def forwarding():
            if not have("virsh"):
                return SKIP, "needs virsh to inspect", None

            rc, out = run_capture(
                ["virsh", "-c", "qemu:///system", "net-list", "--name"])
            if rc != 0:
                return SKIP, "cannot list libvirt networks", None

            networks = []
            for name in [n.strip() for n in out.splitlines() if n.strip()]:
                rc, xml = run_capture(
                    ["virsh", "-c", "qemu:///system", "net-dumpxml", name])
                if rc != 0:
                    continue
                forward = re.search(r"<forward\b[^>]*>", xml)
                if not forward:
                    continue
                # libvirt defaults a mode-less <forward/> to nat
                mode = re.search(r"mode=['\"]([a-z]+)", forward.group(0))
                mode = mode.group(1) if mode else "nat"
                if mode not in ("nat", "route", "open"):
                    continue
                bridge = re.search(r"<bridge[^>]*name=['\"]([^'\"]+)", xml)
                if bridge:
                    # 'open' means libvirt deliberately writes no rules, so its
                    # bridge must not be judged against the ruleset -- but the
                    # host's forward policy still decides whether it works
                    networks.append((name, bridge.group(1), mode != "open"))
            if not networks:
                return SKIP, "no active forwarding libvirt networks", None

            evidence, drop_evidence, policy_drop, libvirt_table, complete_view = \
                self._forward_evidence()
            if not evidence and not complete_view:
                return INFO, ("cannot read the firewall ruleset without "
                              "passwordless sudo; re-run as root to include "
                              "this check"), None

            # A live libvirt table is proof of the backend; the config file is
            # only a fallback, since libvirt >= 11 defaults to nftables with
            # the setting left unset.
            backend = "nftables" if libvirt_table else self._libvirt_fw_backend()[0]
            docker_manages = self._docker_manages_firewall(evidence)
            status, detail, needs_fix = forwarding_verdict(
                networks, evidence, docker_manages, backend, policy_drop,
                drop_evidence, complete_view)
            wiped = bool(wiped_networks(networks, evidence))
            return status, detail, (
                self._forwarding_fix(backend, docker_manages, wiped)
                if needs_fix else None)

        self.check("Host forwarding (docker/libvirt)", forwarding)

    def _check_optional_tools(self):
        self.section("Optional features (not required for basic `boxman up`)")
        optional = [
            ("ansible", "ansible", "`boxman run` / Ansible tasks"),
            ("zstd", "zstd", "compressed snapshot memory state"),
            ("virt-sparsify", "virt-sparsify", "`boxman storage compact`"),
            ("oras", None, "OCI registry push/pull"),
            ("containerlab", None, "netlab / containerlab hybrid topologies"),
        ]
        for binary, pkgkey, feature in optional:
            self._optional_line(binary, pkgkey, feature)

    def _optional_line(self, binary, pkgkey, feature):
        def probe():
            if have(binary):
                return OK, f"{binary} present", None
            if pkgkey:
                pkg = _TOOL_PKG.get(pkgkey, {}).get(self.family, pkgkey)
                fix = self._install_fix(f"install {binary}", pkg)
            else:
                url = {"oras": "https://oras.land/docs/installation",
                       "containerlab": "https://containerlab.dev/install/"}.get(binary, "")
                fix = Fix(f"install {binary} -- see {url}", commands=[])
            return WARN, f"{binary} not installed (only needed for {feature})", fix

        self.check(binary, probe)

    # -- DOCKER runtime ---------------------------------------------------- #
    def check_docker_runtime(self):
        self.section("Docker runtime")

        def docker_engine():
            if not have("docker"):
                fix = Fix("install Docker Engine -- see https://docs.docker.com/engine/install/",
                          commands=[])
                return FAIL, "docker not found", fix
            rc, out = run_capture(["docker", "info"])
            if rc == 0:
                return OK, "docker daemon reachable", None
            low = out.lower()
            if "permission denied" in low or "dial unix" in low and "permission" in low:
                fix = Fix("add yourself to the docker group",
                          commands=["sudo usermod -aG docker $USER"],
                          needs_sudo=True, needs_relogin=True)
                return FAIL, "docker daemon not reachable (permission denied)", fix
            fix = Fix("start the Docker daemon",
                      commands=["sudo systemctl enable --now docker"], needs_sudo=True)
            return FAIL, "docker installed but daemon not reachable", fix

        self.check("Docker engine", docker_engine)

        def compose_v2():
            rc, out = run_capture(["docker", "compose", "version"])
            if rc == 0:
                return OK, out.strip().splitlines()[0] if out.strip() else "compose v2 present", None
            fix = Fix("install the Docker Compose v2 plugin -- see "
                      "https://docs.docker.com/compose/install/", commands=[])
            return FAIL, "`docker compose` (v2) not available", fix

        if have("docker"):
            self.check("Docker Compose v2", compose_v2)

        def dev_kvm_host():
            if os.path.exists("/dev/kvm"):
                return OK, "/dev/kvm present on host (passed through to the container)", None
            fix = Fix("enable KVM on the host (BIOS VT-x/AMD-V + `sudo modprobe kvm`)",
                      commands=[])
            return FAIL, "/dev/kvm missing on host; container cannot accelerate VMs", fix

        self.check("/dev/kvm on host", dev_kvm_host)
        self._check_nested_virt()

    # -- config / capacity ------------------------------------------------- #
    def check_config_and_capacity(self):
        self.section("Config & capacity")

        for label, path in (
            ("~/.config/boxman", "~/.config/boxman"),
            ("image cache ~/.cache/boxman/images", "~/.cache/boxman/images"),
        ):
            self._check_writable(label, path)

        def disk_space():
            target = os.path.expanduser("~/.cache/boxman/images")
            probe_dir = target
            while probe_dir and not os.path.isdir(probe_dir):
                probe_dir = os.path.dirname(probe_dir)
            if not probe_dir:
                probe_dir = os.path.expanduser("~")
            try:
                free_gb = shutil.disk_usage(probe_dir).free / (1024.0 ** 3)
            except OSError:
                return SKIP, f"cannot stat {probe_dir}", None
            detail = f"{free_gb:.0f} GB free on {probe_dir}"
            if free_gb < 20:
                return WARN, detail + " (VM images are large; ~20-50 GB recommended)", None
            return OK, detail, None

        self.check("Free disk space", disk_space)

        def memory():
            try:
                with open("/proc/meminfo") as handle:
                    first = handle.readline()
            except OSError:
                return SKIP, "cannot read /proc/meminfo", None
            match = re.search(r"(\d+)", first)
            if not match:
                return SKIP, "cannot parse MemTotal", None
            total_gb = int(match.group(1)) / (1024.0 ** 2)
            detail = f"{total_gb:.1f} GB RAM"
            if total_gb < 8:
                return WARN, detail + " (8+ GB recommended for comfortable VM clusters)", None
            return OK, detail, None

        self.check("System memory", memory)

    def _check_writable(self, label, path):
        def probe():
            full = os.path.expanduser(path)
            existing = full
            while existing and not os.path.exists(existing):
                existing = os.path.dirname(existing)
            if existing and os.access(existing, os.W_OK):
                return OK, f"{full} is writable", None
            fix = Fix("create the directory", commands=[f"mkdir -p {full}"])
            return WARN, f"{full} not writable (boxman auto-creates it; check permissions)", fix

        self.check(label, probe)

    # -- summary ----------------------------------------------------------- #
    def _summary(self):
        counts = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0, INFO: 0}
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        self.section("Summary")
        print("  {}   {}   {}   {}   {}".format(
            self.c(f"OK: {counts[OK]}", "green"),
            self.c(f"WARN: {counts[WARN]}", "yellow"),
            self.c(f"FAIL: {counts[FAIL]}", "red"),
            self.c(f"SKIP: {counts[SKIP]}", "dim"),
            self.c(f"INFO: {counts[INFO]}", "cyan"),
        ))

        if self.manual_steps:
            print("\n  " + self.c("Remaining / suggested steps:", "bold"))
            for step in self.manual_steps:
                lines = step.splitlines() or [step]
                print("   - " + lines[0])
                for extra in lines[1:]:
                    print("     " + extra)
        if self.relogin_needed:
            print("\n  " + self.c("* Log out and back in for group changes to take effect, "
                                  "then re-run this checker.", "yellow"))

        blocking = counts[FAIL] > 0
        print()
        if blocking:
            print(self.c("Result: blocking prerequisites are missing (see FAIL items).", "red"))
        elif counts[WARN] > 0:
            print(self.c("Result: ready for basic use; review WARN items for full functionality.",
                         "yellow"))
        else:
            print(self.c("Result: all prerequisites satisfied. You're good to go.", "green"))
        return 1 if blocking else 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Guided checker for boxman host prerequisites.")
    parser.add_argument("--runtime", choices=["auto", "local", "docker"], default="auto",
                        help="which runtime's prerequisites to check "
                             "(default: auto-detect from ~/.config/boxman/boxman.yml)")
    parser.add_argument("--check-only", action="store_true",
                        help="report only; never prompt or change anything (CI-friendly)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="assume 'yes' to every fix prompt (sudo may still ask for a password)")
    parser.add_argument("--verbose", action="store_true",
                        help="show extra detail")
    return parser


def main(argv=None):
    opts = build_parser().parse_args(argv)
    try:
        return Doctor(opts).run()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
