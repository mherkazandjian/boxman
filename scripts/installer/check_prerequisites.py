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
    return "unknown"


# Verbatim per-distro core stack lines (from doc/tutorial/README.md), minus the
# systemctl/usermod steps which the checker handles as their own fixes.
_CORE_STACK = {
    "arch": "sudo pacman -S --needed libvirt qemu-full virt-install sshpass dnsmasq",
    "debian": ("sudo apt update && sudo apt install -y libvirt-daemon-system "
               "libvirt-clients qemu-kvm virtinst sshpass bridge-utils cloud-image-utils"),
    "rhel": "sudo dnf install -y libvirt qemu-kvm virt-install sshpass genisoimage",
}

# Package name for a cloud-init seed-ISO tool, per family.
_SEED_PKG = {"arch": "libisoburn", "debian": "cloud-image-utils", "rhel": "genisoimage"}

# Single-package names for individually-missing tools, per family.
_TOOL_PKG = {
    "rsync": {"arch": "rsync", "debian": "rsync", "rhel": "rsync"},
    "sshpass": {"arch": "sshpass", "debian": "sshpass", "rhel": "sshpass"},
    "ansible": {"arch": "ansible", "debian": "ansible", "rhel": "ansible"},
    "zstd": {"arch": "zstd", "debian": "zstd", "rhel": "zstd"},
    "virt-sparsify": {"arch": "guestfs-tools", "debian": "libguestfs-tools", "rhel": "guestfs-tools"},
    "openssh": {"arch": "openssh", "debian": "openssh-client", "rhel": "openssh-clients"},
}


def install_cmd(family, pkgs):
    """Build the package-manager install command for `pkgs` (a space string)."""
    if family == "arch":
        return "sudo pacman -S --needed " + pkgs
    if family == "debian":
        return "sudo apt update && sudo apt install -y " + pkgs
    if family == "rhel":
        return "sudo dnf install -y " + pkgs
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
        if re.search(r"[/\s]%s(\b|,|$)" % re.escape(base), rhs):
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
    return (match.group(1) if match else ""), ("firewall_backend" in text)


def forwarding_verdict(networks, rules, docker_present, backend,
                       complete_view=True):
    """Grade the host forward path from facts already gathered.

    ``networks`` is ``[(network_name, bridge_name), ...]`` for the active
    NAT/routed libvirt networks; ``rules`` is the concatenated text of every
    ruleset we managed to read (``iptables -S`` *and* ``nft list ruleset``).
    ``complete_view`` is False when one of those could not be read -- a bridge
    we then fail to find is reported as unconfirmed rather than wiped, because
    libvirt's nftables backend keeps its rules in a private table and a checker
    that could not look there must not claim they are gone.
    Returns ``(status, detail, needs_fix)``.

    Docker and libvirt both write the ``filter`` table.  Docker rebuilds it on
    every restart and leaves the FORWARD policy at DROP; libvirt's rules -- and
    boxman's own routed-network FORWARD rules -- are collateral damage, and
    nothing re-applies them.  Every base chain on the forward hook is evaluated
    and a drop in any of them is final, so libvirt's ACCEPTs cannot rescue the
    packet.  The nat table usually survives, so guests keep NATing while
    forwarding is dead: it reads as a slow network, not a firewall fault.
    """
    if not networks:
        return SKIP, "no active NAT/routed libvirt networks", False

    # Word-anchored, not a substring test: plain containment would let
    # virbr10's rules vouch for virbr1 and hide a genuinely wiped network.
    wiped = [name for name, bridge in networks
             if not re.search(r"\b%s\b" % re.escape(bridge), rules)]

    if wiped and not complete_view:
        return INFO, ("part of the ruleset was unreadable, so the rules for %s "
                      "could not be confirmed either way" % ", ".join(wiped)), False

    if wiped:
        detail = ("no forwarding rules for active network(s): %s\n"
                  "guests will NAT but never forward" % ", ".join(wiped))
        if docker_present:
            detail += "\ndocker shares this table and rebuilds it on restart"
        return WARN, detail, True

    if docker_present and backend != "nftables":
        detail = ("forwarding works now, but libvirt shares the filter table "
                  "with docker -- the next `systemctl restart docker` wipes "
                  "these rules")
        if re.search(r"^-P FORWARD DROP", rules, re.M):
            detail += "\nFORWARD policy is DROP, so nothing survives the wipe"
        return WARN, detail, True

    return OK, "forwarding rules present for: %s" % ", ".join(
        name for name, _ in networks), False


# --------------------------------------------------------------------------- #
# Small result containers                                                      #
# --------------------------------------------------------------------------- #
class Fix(object):
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


class Result(object):
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
            universal_newlines=True,
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
class Doctor(object):
    def __init__(self, opts):
        self.opts = opts
        self.os = self._detect_os()
        self.family = self.os["family"]
        self.runtime = self._detect_runtime(opts.runtime)
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
        return "\033[%sm%s\033[0m" % (_ANSI.get(name, "0"), text)

    def section(self, title):
        print("\n" + self.c("== %s ==" % title, "bold"))

    def _print_result(self, result, prefix=""):
        tag, color = _STATUS_TAG[result.status]
        label = self.c("[%s]" % tag, color)
        print("%s%s %s" % (prefix, label, result.name))
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
        print("       " + self.c("fix: " + fix.description, "cyan"))
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

        # --yes deliberately does not cover disruptive fixes: bouncing a
        # container host is not something an unattended run may decide.
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
            entry += " (%s)" % note
        elif result.fix and result.fix.commands:
            entry += ": " + " ; ".join(result.fix.commands)
        elif result.fix:
            entry += ": " + result.fix.description
        if entry not in self.manual_steps:
            self.manual_steps.append(entry)

    def _ask(self, prompt):
        try:
            answer = input("%s [y/N] " % prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
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
                print("       " + self.c("command exited with status %d" % rc, "yellow"))
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
            description += (" (unrecognized distro '%s' -- install %s with your "
                            "package manager)" % (self.os["id"] or "?", pkgs))
        return Fix(description, commands, needs_sudo=True, needs_relogin=relogin)

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
        print("  host    : %s" % self.os["pretty"])
        print("  family  : %s   runtime: %s" % (self.family, self.runtime))
        mode = "check-only (read-only)" if self.opts.check_only else (
            "auto-fix (--yes)" if self.opts.yes else "guided (asks before any change)")
        print("  mode    : %s" % mode)
        if not self.opts.check_only:
            print(self.c("  Nothing is changed unless you confirm each fix.", "dim"))

    # -- env basics -------------------------------------------------------- #
    def check_env(self):
        self.section("Environment")

        def python_version():
            ver = "%d.%d.%d" % sys.version_info[:3]
            if sys.version_info[:2] >= (3, 10):
                return OK, "Python %s (boxman needs >= 3.10)" % ver, None
            fix = Fix(
                "install Python >= 3.10 (e.g. `conda create -n boxman python=3.12` "
                "or a system python3.10+), then reinstall boxman into it",
                commands=[],
            )
            return FAIL, "Python %s is too old; boxman needs >= 3.10" % ver, fix

        self.check("Python version", python_version)

        def boxman_installed():
            env = os.environ.get("CONDA_DEFAULT_ENV") or os.environ.get("VIRTUAL_ENV")
            env_note = ("active env: %s" % env) if env else "no conda/venv detected"
            if have("boxman"):
                rc, out = run_capture(["boxman", "--version"])
                ver = out.strip().splitlines()[0] if (rc == 0 and out.strip()) else "installed"
                return OK, "%s (%s); %s" % (ver, shutil.which("boxman"), env_note), None
            fix = Fix("install boxman into your active environment",
                      commands=["pip install boxman"])
            return WARN, "`boxman` not on PATH; %s" % env_note, fix

        self.check("boxman on PATH", boxman_installed)

        def python_deps():
            mods = ["yaml", "invoke", "jinja2", "lxml", "passlib"]
            missing = []
            for mod in mods:
                rc, _ = run_capture([sys.executable, "-c", "import %s" % mod])
                if rc != 0:
                    missing.append(mod)
            if not missing:
                return OK, "yaml, invoke, jinja2, lxml, passlib import cleanly", None
            hint = ("lxml needs system libxml2/libxslt; the rest come with "
                    "`pip install boxman`") if "lxml" in missing else \
                   "reinstall boxman to pull these"
            fix = Fix("install boxman's Python deps into %s (%s)" % (sys.executable, hint),
                      commands=["%s -m pip install %s" % (sys.executable, " ".join(missing))])
            return WARN, "missing: %s" % ", ".join(missing), fix

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
                      commands=["sudo modprobe kvm %s" % vendor_mod], needs_sudo=True)
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
                path = "/sys/module/%s/parameters/nested" % mod
                if os.path.exists(path):
                    try:
                        with open(path) as handle:
                            val = handle.read().strip()
                    except OSError:
                        continue
                    if val in ("Y", "1"):
                        return OK, "nested virtualization enabled (%s)" % mod, None
                    fix = Fix(
                        "enable nested virt on the *host*, e.g. "
                        "`echo 'options %s nested=1' | sudo tee /etc/modprobe.d/kvm.conf` "
                        "then reload the module" % mod,
                        commands=[])
                    return WARN, "running in a VM and nested virt is disabled (%s=%s)" % (mod, val), fix
            return SKIP, "no kvm_intel/kvm_amd module parameter found", None

        self.check("Nested virtualization", nested)

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
                return OK, "virsh, virt-install, virt-clone, qemu-img, qemu-system present", None
            stack = _CORE_STACK.get(self.family)
            cmds = [stack] if stack else []
            fix = Fix("install the libvirt/QEMU stack for your distro", cmds,
                      needs_sudo=True)
            if not stack:
                fix.description += (" -- unknown distro '%s'; install libvirt, "
                                    "qemu-kvm, virt-install, virt-clone" % (self.os["id"] or "?"))
            return FAIL, "missing: %s" % ", ".join(missing), fix

        tools = self.check("Core libvirt/QEMU tools", core_tools)

        def libvirtd_service():
            active = run_capture(["systemctl", "is-active", "libvirtd"])[1].strip()
            if active == "active":
                return OK, "libvirtd.service is active", None
            # modular libvirt daemons (newer distros)
            alt = run_capture(["systemctl", "is-active", "virtqemud"])[1].strip()
            if alt == "active":
                return OK, "virtqemud.service is active (modular libvirt)", None
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
                detail = "cannot reach qemu:///system: %s" % reason
                if "permission" in out.lower() or "authentication" in out.lower():
                    detail += "\n(usually a group-membership issue -- see the groups check below)"
                return FAIL, detail, None  # fix handled by service/groups checks

            self.check("libvirt connectivity", libvirt_conn)

        def groups():
            names, user = user_group_names()
            want = ["libvirt", "kvm"]
            missing = [g for g in want if g not in names]
            if not missing:
                return OK, "user '%s' is in: %s" % (user, ", ".join(want)), None
            fix = Fix("add yourself to the libvirt and kvm groups",
                      commands=["sudo usermod -aG libvirt,kvm $USER"],
                      needs_sudo=True, needs_relogin=True)
            # WARN, not FAIL: group membership is only the standard *means* to
            # reach /dev/kvm and the libvirt socket, and those ends are checked
            # directly ("/dev/kvm access", "libvirt connectivity"). If access
            # already works (world-readable device, polkit, sudo), missing group
            # membership isn't blocking -- the functional checks decide that.
            return WARN, "user '%s' not in group(s): %s" % (user, ", ".join(missing)), fix

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
                return OK, "cloud-init seed tool available (%s)" % found[0], None
            fix = self._install_fix(
                "install a cloud-init seed-ISO tool", _SEED_PKG.get(self.family, "genisoimage"))
            return FAIL, "none of: %s (needed to build cloud-init seed ISOs)" % ", ".join(tools), fix

        self.check("cloud-init seed tool", seed_tool)

        self._check_simple_bin("sshpass", "sshpass", FAIL, "password-based SSH key injection")
        self._check_simple_bin("rsync", "rsync", FAIL, "image/template copy")

        def ssh_client():
            missing = [b for b in ("ssh", "ssh-keygen") if not have(b)]
            if not missing:
                return OK, "ssh and ssh-keygen present", None
            fix = self._install_fix("install the OpenSSH client",
                                    _TOOL_PKG["openssh"].get(self.family, "openssh-client"))
            return WARN, "missing: %s" % ", ".join(missing), fix

        self.check("SSH client tools", ssh_client)

        self._check_sudo_rights()
        self._check_optional_tools()

    def _check_simple_bin(self, label, binary, severity, feature):
        def probe():
            if have(binary):
                return OK, "%s present" % binary, None
            pkg = _TOOL_PKG.get(binary, {}).get(self.family, binary)
            fix = self._install_fix("install %s" % binary, pkg)
            return severity, "%s not found (needed for %s)" % (binary, feature), fix

        self.check(label, probe)

    def _check_sudo_rights(self):
        def sudo_rights():
            if not have("sudo"):
                fix = self._install_fix("install sudo", "sudo")
                return WARN, "sudo not found; libvirt network/cleanup steps need it", fix
            rc, out = run_capture(["sudo", "-n", "-l"])
            if rc != 0:
                fix = Fix(
                    "grant passwordless sudo for the commands boxman runs, e.g. a "
                    "/etc/sudoers.d/boxman line: "
                    "`%s ALL=(root) NOPASSWD: /usr/bin/virsh, /usr/bin/qemu-img, "
                    "/usr/sbin/iptables, /usr/sbin/ip, /usr/bin/rsync, /bin/rm`" % (
                        (user_group_names()[1] or "$USER")),
                    commands=[])
                return WARN, ("passwordless sudo not available; boxman's automatic "
                              "iptables/NAT and cleanup steps fail when run "
                              "non-interactively"), fix
            # passwordless sudo exists -- check the scope that bites people.
            iptables_ok = sudo_nopasswd_covers(out, "iptables")
            qemu_ok = sudo_nopasswd_covers(out, "qemu-img")
            rm_ok = sudo_nopasswd_covers(out, "rm")
            gaps = []
            if not iptables_ok:
                gaps.append("iptables/ip (NAT & isolated networks, netlab bridges)")
            if not (qemu_ok and rm_ok):
                gaps.append("qemu-img/rm (destroy/cleanup silently no-ops without these)")
            if not gaps:
                return OK, "passwordless sudo covers virsh/qemu-img/iptables/rm", None
            fix = Fix(
                "widen NOPASSWD sudo scope in /etc/sudoers.d/boxman to include: "
                "virsh, qemu-img, iptables, ip, rsync, rm", commands=[])
            return WARN, "passwordless sudo present but missing: " + "; ".join(gaps), fix

        self.check("sudo rights", sudo_rights)

    # -- host packet forwarding (docker vs libvirt) ------------------------- #
    def _root_capture(self, args):
        """Run a root-only inspection command without ever prompting.

        Returns ``(rc, text)``; rc 126 means "we were not allowed to look",
        which callers surface as INFO -- a checker that cannot see must say so
        rather than guess.
        """
        if getattr(os, "geteuid", lambda: 1)() == 0:
            return run_capture(args)
        if not have("sudo"):
            return 126, ""
        rc, out = run_capture(["sudo", "-n"] + args)
        if rc != 0 and re.search(r"password is required|terminal is required",
                                 out, re.I):
            return 126, ""
        return rc, out

    def _libvirt_net_unit(self):
        """The systemd unit that owns libvirt's network rules on this host."""
        for unit in ("virtnetworkd", "libvirtd"):
            if run_capture(["systemctl", "is-active", unit])[1].strip() == "active":
                return unit
        return "libvirtd"

    def _libvirt_fw_backend(self):
        """``(configured_backend, option_supported)`` for this libvirt."""
        try:
            with open("/etc/libvirt/network.conf") as handle:
                return parse_firewall_backend(handle.read())
        except OSError:
            return "", False

    def _effective_fw_backend(self, rules):
        """What libvirt is actually doing, not merely what is configured.

        A libvirt-owned nft table is proof.  The config file is only a
        fallback: libvirt >= 11 defaults to the nftables backend with the
        setting left unset, so an empty ``firewall_backend`` means nothing on
        its own.
        """
        if re.search(r"^table (?:ip|ip6|inet) libvirt", rules, re.M):
            return "nftables"
        return self._libvirt_fw_backend()[0]

    def _forwarding_fix(self):
        """Commands that take libvirt out of the table docker rebuilds."""
        backend, supported = self._libvirt_fw_backend()
        commands = []
        if have("docker"):
            commands += [
                "sudo cp -a /etc/docker/daemon.json "
                "/etc/docker/daemon.json.boxman-bak 2>/dev/null || true",
                "sudo python3 -c \"import json,pathlib;"
                "p=pathlib.Path('/etc/docker/daemon.json');"
                "d=json.loads(p.read_text() or '{}') if p.exists() else {};"
                "d['ip-forward-no-drop']=True;"
                "p.parent.mkdir(parents=True,exist_ok=True);"
                "p.write_text(json.dumps(d,indent=2))\"",
                "sudo systemctl restart docker",
                # the flag stops docker *setting* the policy; it does not clear
                # one docker already set, so this one-off reset is required
                "sudo iptables -P FORWARD ACCEPT",
            ]
        if supported and backend != "nftables":
            commands += [
                "sudo cp -a /etc/libvirt/network.conf "
                "/etc/libvirt/network.conf.boxman-bak 2>/dev/null || true",
                "sudo sh -c 'sed -i "
                "\"/^[[:space:]]*firewall_backend[[:space:]]*=/d\" "
                "/etc/libvirt/network.conf && printf "
                "\"firewall_backend = \\\"nftables\\\"\\n\" "
                ">> /etc/libvirt/network.conf'",
                "sudo systemctl restart %s" % self._libvirt_net_unit(),
            ]
        description = ("give libvirt its own nftables table and stop docker "
                       "forcing FORWARD to DROP (a .boxman-bak backup is "
                       "written next to each file)")
        if not supported:
            description += ("; this libvirt build has no firewall_backend "
                            "option, so only the docker half can be automated")
        return Fix(description, commands, needs_sudo=True,
                   disruptive=bool(commands))

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
                if rc != 0 or not re.search(
                        r"<forward[^>]*mode=['\"](nat|route)", xml):
                    continue
                bridge = re.search(r"<bridge[^>]*name=['\"]([^'\"]+)", xml)
                if bridge:
                    networks.append((name, bridge.group(1)))
            if not networks:
                return SKIP, "no active NAT/routed libvirt networks", None

            # Both must be consulted: docker and boxman write the iptables
            # filter table, while libvirt's nftables backend keeps its rules in
            # a private table that `iptables -S` cannot see.
            texts, complete_view = [], True
            for args in (["iptables", "-S"], ["nft", "list", "ruleset"]):
                if not have(args[0]):
                    complete_view = False
                    continue
                rc, out = self._root_capture(args)
                if rc == 0:
                    texts.append(out)
                else:
                    complete_view = False
            if not texts:
                return INFO, ("cannot read the firewall ruleset without "
                              "passwordless sudo; re-run as root to include "
                              "this check"), None

            rules = "\n".join(texts)
            status, detail, needs_fix = forwarding_verdict(
                networks, rules, "DOCKER-USER" in rules or have("docker"),
                self._effective_fw_backend(rules), complete_view)
            return status, detail, (self._forwarding_fix() if needs_fix else None)

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
                return OK, "%s present" % binary, None
            if pkgkey:
                pkg = _TOOL_PKG.get(pkgkey, {}).get(self.family, pkgkey)
                fix = self._install_fix("install %s" % binary, pkg)
            else:
                url = {"oras": "https://oras.land/docs/installation",
                       "containerlab": "https://containerlab.dev/install/"}.get(binary, "")
                fix = Fix("install %s -- see %s" % (binary, url), commands=[])
            return WARN, "%s not installed (only needed for %s)" % (binary, feature), fix

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
                return SKIP, "cannot stat %s" % probe_dir, None
            detail = "%.0f GB free on %s" % (free_gb, probe_dir)
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
            detail = "%.1f GB RAM" % total_gb
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
                return OK, "%s is writable" % full, None
            fix = Fix("create the directory", commands=["mkdir -p %s" % full])
            return WARN, "%s not writable (boxman auto-creates it; check permissions)" % full, fix

        self.check(label, probe)

    # -- summary ----------------------------------------------------------- #
    def _summary(self):
        counts = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0, INFO: 0}
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        self.section("Summary")
        print("  %s   %s   %s   %s   %s" % (
            self.c("OK: %d" % counts[OK], "green"),
            self.c("WARN: %d" % counts[WARN], "yellow"),
            self.c("FAIL: %d" % counts[FAIL], "red"),
            self.c("SKIP: %d" % counts[SKIP], "dim"),
            self.c("INFO: %d" % counts[INFO], "cyan"),
        ))

        if self.manual_steps:
            print("\n  " + self.c("Remaining / suggested steps:", "bold"))
            for step in self.manual_steps:
                print("   - " + step)
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
