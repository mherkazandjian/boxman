# Boxman prerequisites checker

A guided "doctor" that verifies a host is ready to run boxman and, for anything
that's missing, prints the exact fix for your distro and offers to run it.
Distro families covered: Arch, Debian/Ubuntu, Fedora/RHEL/Rocky, Gentoo, and the
declarative NixOS and Guix System (see the note on declarative distros below).

Boxman drives libvirt/KVM through the `virsh` / `virt-install` / `virt-clone` /
`qemu-img` command-line tools and uses `virt-sysprep` when available to reset
cloned Linux machine IDs. It also needs host-level dependencies
(a running `libvirtd`, group membership, a `default` NAT network, a cloud-init
seed-ISO tool, `sshpass`, sudo rights, …). Installing the Python package — e.g.
`pip install boxman` inside a conda env — does **not** set any of that up. This
script checks all of it in one pass.

On supported Debian and Ubuntu releases, the `guestfs-tools` package owns the
user-facing `virt-sysprep` and `virt-sparsify` executables. The similarly named
`libguestfs-tools` is a compatibility/meta package; checker remediation names
`guestfs-tools` directly so installing one optional tool also supplies the
other.

## Usage

```bash
# From the repo root (no boxman install required to run the checker):
python3 scripts/installer/check_prerequisites.py
```

The script is **standard-library only** and never imports boxman, so you can run
it before boxman's own dependencies are installed. It runs on Python 3.6+ (its
first check tells you if your Python is too old for boxman, which needs ≥ 3.10).

### Options

| Flag | Effect |
|------|--------|
| `--runtime {auto,local,docker}` | Which runtime's prerequisites to check. Default `auto` reads `runtime:` from `~/.config/boxman/boxman.yml`, falling back to `local`. |
| `--check-only` | Report only — never prompt, never change anything. Good for CI. |
| `--yes` / `-y` | Assume "yes" to every fix prompt (sudo may still ask for your password). |
| `--verbose` | Show extra detail. |

Exit code is `0` when no blocking `FAIL` remains, `1` otherwise.

## How the guided fixes work

For each problem it can fix, the checker:

1. explains what's wrong,
2. prints the exact command(s) for your detected distro (Arch / Debian-Ubuntu /
   Fedora-RHEL-Rocky / Gentoo), and
3. asks `Run this now? [y/N]`.

**Nothing on your system is changed unless you answer yes** (or pass `--yes`).
Commands that need root are run through `sudo`, which prompts for your password
as usual. Fixes that add you to a group (e.g. `libvirt`, `kvm`, `docker`) are
flagged as needing a logout/login before they take effect — re-run the checker
afterwards to confirm.

## Disruptive fixes

A few fixes restart running services — the docker/libvirt forwarding fix
restarts `docker` and `virtnetworkd`, briefly interrupting running containers
and guest connectivity. Those are marked **disruptive** and require a human at
a terminal:

- `--yes` does not cover them.
- Neither does a piped answer (`yes | check_prerequisites.py --yes`) — without
  a tty they are declined outright, not prompted.
- If one of their commands fails, the remaining commands are **not** run, so a
  failed config edit is never followed by the service restart it was meant to
  accompany.

Decline one and it is reported in the summary as a manual step. Disruptive
fixes write a `.boxman-bak` copy of every file they edit, and never overwrite
an existing backup.

**Declarative distros (NixOS, Guix System).** For the libvirt/QEMU stack, the
`libvirtd` service and group membership, the checker prints an *advisory*
snippet for your system config (`/etc/nixos/configuration.nix` +
`sudo nixos-rebuild switch`, or `/etc/config.scm` +
`sudo guix system reconfigure`) and never offers to auto-run it — those changes
belong in your declarative config, not an imperative `install`. Individual user
CLI tools (rsync, sshpass, a cloud-init seed-ISO tool, …) are still offered
imperatively via `nix profile install nixpkgs#<pkg>` / `guix install <pkg>`.

## What it checks

- **Environment** — Python ≥ 3.10, `boxman` on PATH (+ active conda/venv), and
  that boxman's Python deps import (`lxml` in particular needs system
  libxml2/libxslt).
- **Virtualization hardware** — CPU VT-x/AMD-V, `/dev/kvm` presence and access,
  and nested virt when running inside a VM.
- **Local runtime** — the `virsh`/`virt-install`/`virt-clone`/`qemu-img`/QEMU
  tools, optional `virt-sysprep` (`clone_machine_id: required` needs it),
  `libvirtd` running, `virsh -c qemu:///system` connectivity, `libvirt`
  and `kvm` group membership, the `default` NAT network, a cloud-init seed-ISO
  tool, `sshpass`, `rsync`, the OpenSSH client, and your **sudo rights**
  (including the footgun where cleanup silently no-ops if `sudo qemu-img`/`rm`
  aren't passwordless).
- **Host forwarding (docker/libvirt)** — whether docker has rebuilt the
  iptables `filter` table out from under libvirt (and boxman's own
  routed-network `FORWARD` rules), leaving guests that NAT but never forward.
  Catches both the acute case (rules already gone) and the latent one (still
  present, but the next `systemctl restart docker` takes them). The guided fix
  gives libvirt its own nftables table and stops docker forcing `FORWARD` to
  `DROP`. Background and diagrams: the **Advanced** section of the main README.
- **Docker runtime** (when selected) — Docker Engine + Compose v2 reachable and
  `/dev/kvm` on the host.
- **Optional features** — `ansible`, `zstd`, `virt-sparsify`, `oras`,
  `containerlab` (reported but never blocking).
- **Config & capacity** — `~/.config/boxman` and the image cache are writable,
  free disk space, and total RAM.
