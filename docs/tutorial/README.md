# Getting Started with Boxman (v0.10)

> **Local VM clusters on libvirt/QEMU — reproducible, YAML-driven, and scriptable.**  
> This guide targets users who want to go beyond a quick demo and understand the operational model and CLI.

Boxman is an infrastructure-as-code CLI for building and managing *local* VM clusters on top of **libvirt/QEMU**. You describe clusters in YAML (`conf.yml`), and Boxman provisions (or reuses) supporting infra, manages VM lifecycle, snapshots, exports/imports, and provides convenient “run tasks with environment loaded” workflows.

---

## Table of Contents

- [Getting Started with Boxman (v0.10)](#getting-started-with-boxman-v010)
  - [Table of Contents](#table-of-contents)
  - [1. Prerequisites](#1-prerequisites)
    - [Permissions: qemu:///system vs session](#permissions-qemusystem-vs-session)
  - [2. Install libvirt/QEMU (APT \& DNF)](#2-install-libvirtqemu-apt--dnf)
    - [Ubuntu / Debian (APT)](#ubuntu--debian-apt)
    - [Fedora / RHEL / Rocky / Alma (DNF)](#fedora--rhel--rocky--alma-dnf)
  - [3. Install Python tooling (pip + venv)](#3-install-python-tooling-pip--venv)
    - [Ubuntu / Debian (APT)](#ubuntu--debian-apt-1)
    - [Fedora / RHEL family (DNF)](#fedora--rhel-family-dnf)
  - [4. Clone \& Install Boxman (venv)](#4-clone--install-boxman-venv)
  - [5. Use Tested Configurations (boxes/)](#5-use-tested-configurations-boxes)
  - [6. First Run (Provision → Up → SSH)](#6-first-run-provision--up--ssh)
  - [7. CLI Overview (v0.10)](#7-cli-overview-v010)
  - [8. Lifecycle Operations](#8-lifecycle-operations)
    - [8.1 provision](#81-provision)
    - [8.2 up](#82-up)
    - [8.3 down](#83-down)
    - [8.4 deprovision](#84-deprovision)
  - [9. Snapshots](#9-snapshots)
  - [10. Control (Start/Save/Suspend/Resume)](#10-control-startsavesuspendresume)
  - [11. Run: Tasks \& Ad-hoc Commands](#11-run-tasks--ad-hoc-commands)
  - [12. SSH Convenience](#12-ssh-convenience)
  - [13. Import/Export](#13-importexport)
    - [13.1 import-image](#131-import-image)
    - [13.2 export / import (VMs)](#132-export--import-vms)
  - [14. Troubleshooting](#14-troubleshooting)
    - [14.1 libvirt connection errors](#141-libvirt-connection-errors)
    - [14.2 "works in virsh but Boxman fails"](#142-works-in-virsh-but-boxman-fails)
    - [14.3 Resetting docker runtime artifacts](#143-resetting-docker-runtime-artifacts)
  - [15. Quick Reference](#15-quick-reference)
    - [Most common flow](#most-common-flow)
    - [Stop vs save vs suspend](#stop-vs-save-vs-suspend)
    - [Snapshots](#snapshots)
    - [Tasks and ad-hoc commands](#tasks-and-ad-hoc-commands)
    - [Export/Import](#exportimport)
    - [Notes for advanced users](#notes-for-advanced-users)

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| **Linux host** | Any distro with libvirt support |
| **CPU virtualization** | VT-x/AMD-V enabled in BIOS/UEFI |
| **RAM/Disk** | Depends on VM count; 8GB+ recommended |
| **libvirt/QEMU** | `libvirtd`, `qemu-kvm`, `virsh`, `virt-install` |
| **Python** | Python 3 + `pip` + `venv` |
| **Networking** | Ability to create bridges/NAT networks |

### Permissions: qemu:///system vs session
Most setups use the system libvirt daemon: `qemu:///system`.
That typically means:
- `libvirtd` is enabled and running
- your user is in `libvirt` (and sometimes `kvm`)
- you **log out/in** after group changes

Verify you can talk to libvirt:

```bash
virsh -c qemu:///system list --all
```

If this fails, fix libvirt first before attempting Boxman.

## 2. Install libvirt/QEMU (APT & DNF)

### Ubuntu / Debian (APT)

```bash
sudo apt update
sudo apt install -y \
  qemu-kvm \
  libvirt-daemon-system \
  libvirt-clients \
  virtinst \
  bridge-utils
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt,kvm "$USER"
```

Verify:

```bash
virsh -c qemu:///system list --all
```

### Fedora / RHEL / Rocky / Alma (DNF)

```bash
sudo dnf install -y \
  @virtualization \
  libvirt \
  virt-install
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt "$USER"
```

Verify:

```bash
virsh -c qemu:///system list --all
```

⚠️ After adding yourself to groups: log out and log back in (or reboot).

## 3. Install Python tooling (pip + venv)

### Ubuntu / Debian (APT)

```bash
sudo apt install -y python3 python3-pip python3-venv
```

### Fedora / RHEL family (DNF)

```bash
sudo dnf install -y python3 python3-pip
# If your distro packages venv separately, install it too.
```

---

## 4. Clone & Install Boxman (venv)

Clone:

```bash
git clone https://github.com/mherkazandjian/boxman
cd boxman
```

Create and activate venv:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install:

```bash
pip install -r requirements.txt
pip install .
```

Verify install:

```bash
boxman --version
```

---

## 5. Use Tested Configurations (boxes/)

Instead of writing configs from scratch, start from known-good examples:

`boxes/` directory at the root of the repository

**Recommended workflow:**

- Pick a `boxes/<something>/` that matches your scenario
- Copy it into your workspace (or work in-place for experimentation)
- Run Boxman from that directory (where `conf.yml` lives), or pass `--conf`

This is the fastest way to avoid "config archaeology" and focus on the Boxman lifecycle.

---

## 6. First Run (Provision → Up → SSH)

From a directory containing a `conf.yml` (e.g., one under `boxes/`):

**Provision:**

```bash
boxman provision
```

**Bring everything up** (provisions if missing, otherwise starts VMs):

```bash
boxman up
```

**Show VM state** for this project:

```bash
boxman ps
```

**SSH into a VM:**

```bash
# If you omit the name, Boxman defaults to the gateway host (first VM)
boxman ssh

# Or specify a VM name
boxman ssh node02
```

These SSH behaviors are defined in the CLI help.

---

## 7. CLI Overview (v0.10)

Top-level commands (from `boxman --help` output) include:

- `provision`, `up`, `down`, `deprovision`
- `snapshot {take,list,restore,delete}`, `restore`
- `control {suspend,resume,save,start}`
- `run`, `ps`, `ssh`
- `import-image`, `create-templates`
- `export`, `import`
- `destroy-runtime`, `list`

## 8. Lifecycle Operations

### 8.1 provision

Creates whatever is defined in `conf.yml`.

```bash
boxman provision
```

**Useful flags:**

- `--force`: if VMs already exist, deprovision first, then provision
- `--rebuild-templates`: destroy/recreate templates before provisioning
- `--docker-compose`: use the docker-compose setup (legacy-style runtime switch at the command level)

```bash
boxman provision --force
boxman provision --rebuild-templates
boxman provision --docker-compose
```

Flags confirmed by `boxman provision --help`.

### 8.2 up

Ensures the infra is present and running.

```bash
boxman up
```

Supports same flags as provision:

```bash
boxman up --force
boxman up --rebuild-templates
boxman up --docker-compose
```

### 8.3 down

Brings down the infra by saving or suspending state.

```bash
boxman down
```

If you prefer suspend/pause instead of saving VM state to disk:

```bash
boxman down --suspend
```

### 8.4 deprovision

Tears down infra defined in config.

```bash
boxman deprovision
```

If your project was created with the docker-compose setup:

```bash
boxman deprovision --docker-compose
```

---

## 9. Snapshots

Snapshots are under the `snapshot` subcommand:

```bash
boxman snapshot list
```

Take a snapshot:

```bash
boxman snapshot take
boxman snapshot take --name mystate1
boxman snapshot take --vm vm1
boxman snapshot take --vm vm1,vm2
```

Restore:

```bash
boxman snapshot restore --name mystate1
boxman snapshot restore --vm vm1
boxman snapshot restore --vm vm1,vm2
```

Delete:

```bash
boxman snapshot delete
```

---

## 10. Control (Start/Save/Suspend/Resume)

`control` is explicitly structured as subcommands:

- `control start`
- `control save`
- `control suspend`
- `control resume`

Example patterns:

```bash
boxman control start
boxman control save
boxman control suspend
boxman control resume
```

These exact subcommands come from `boxman control --help`.

**Tip:** Use `down` for the "project lifecycle" intent, and `control` when you want an explicit VM state action without implying teardown or full project lifecycle semantics.

---

---

## 11. Run: Tasks & Ad-hoc Commands

`boxman run` is your "task runner" with the workspace environment loaded (from an env file like `env.sh`).

List available tasks:

```bash
boxman run --list
```

Run a named task (task names come from the tasks section of your config):

```bash
boxman run ping
```

Pass extra args through to the task command:

```bash
boxman run site -- --limit foo --tags=bar
```

Run an ad-hoc command with the workspace env loaded:

```bash
boxman run --cmd 'ansible all -m ping'
```

Scope run to a specific cluster:

```bash
boxman run --cluster cluster_1 ping
boxman run --cluster cluster_1 --cmd 'ansible all -m ping'
```

`--ansible-flags` exists specifically for passing flags for `--cmd`.

---

---

## 12. SSH Convenience

Open an interactive SSH session:

```bash
# default: gateway host (first VM) if no name is provided
boxman ssh
```

SSH by VM name (supports "full name" or shorthand depending on config naming):

```bash
boxman ssh cluster_1_node02
boxman ssh node02
```

Scope to a cluster:

```bash
boxman ssh --cluster cluster_1
boxman ssh --cluster cluster_1 node02
```

These behaviors are documented in `boxman ssh --help`.

---

## 13. Import/Export

### 13.1 import-image

Imports an image based on a manifest URI:

```bash
boxman import-image --uri <MANIFEST_URI>
```

Optional flags:

```bash
boxman import-image --uri <MANIFEST_URI> --provider libvirt
boxman import-image --uri <MANIFEST_URI> --name myvm
boxman import-image --uri <MANIFEST_URI> --directory /tmp/boxman_images
```

Provider values are `{virtualbox,libvirt}`.

### 13.2 export / import (VMs)

Export:

```bash
boxman export --vms vm1,vm2 --path /path/to/exports
```

Import:

```bash
boxman import --vms vm1,vm2 --path /path/to/exports
```

Flags confirmed by `boxman export --help` and `boxman import --help`.

---

## 14. Troubleshooting

### 14.1 libvirt connection errors

Check daemon:

```bash
systemctl status libvirtd
```

Check direct connectivity:

```bash
virsh -c qemu:///system list --all
```

Check group membership:

```bash
groups "$USER"
```

If you just added groups, re-login.

### 14.2 "works in virsh but Boxman fails"

Common causes:

- running from a directory without the intended `conf.yml`
- using a different `--conf` than you think
- environment mismatch when using docker-compose mode (see `--docker-compose` flags)

### 14.3 Resetting docker runtime artifacts

If you used the docker runtime environment and want to clean it:

```bash
boxman destroy-runtime
boxman destroy-runtime -y
```

`-y`/`--auto-accept` confirmed by help.

---

## 15. Quick Reference

### Most common flow

```bash
boxman provision
boxman up
boxman ps
boxman ssh
```

### Stop vs save vs suspend

```bash
boxman down
boxman down --suspend
boxman control save
boxman control suspend
boxman control resume
```

### Snapshots

```bash
boxman snapshot take --name before-change
boxman snapshot list
boxman snapshot restore --name before-change
boxman snapshot delete
boxman restore
```

### Tasks and ad-hoc commands

```bash
boxman run --list
boxman run ping
boxman run --cmd 'ansible all -m ping'
boxman run --cluster cluster_1 --cmd 'ansible all -m shell -a "uname -a"'
```

### Export/Import

```bash
boxman export --vms vm1,vm2 --path ./exports
boxman import --vms vm1,vm2 --path ./exports
```

### Notes for advanced users

- Treat `boxes/` as canonical tested baselines. Iterate from there.
- Prefer snapshot checkpoints before risky operations.
- Use `run --cmd` for quick ops; graduate to full Ansible playbooks when you need idempotence and state enforcement.
- Keep one Python venv per Boxman checkout to avoid dependency drift.