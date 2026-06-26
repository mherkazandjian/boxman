# ubuntu-24.04-live-iso-boot

The simplest **end-to-end test of boxman's ISO-boot support**. It boots a single
VM straight from the public, checksummed **Ubuntu 24.04 desktop ("Try Ubuntu")
live ISO** — no template, no clone, no cloud-init, no registration service
(unlike [`talos-iso-boot`](../talos-iso-boot), which needs an Omni instance).

What boxman does on `boxman up`:

1. downloads `ubuntu-24.04.4-desktop-amd64.iso` and verifies its SHA256
   (cached under `~/.cache/boxman/images`, so it is fetched only once),
2. creates an empty 16G boot disk and runs `virt-install` with the ISO attached
   as a CDROM and firmware boot order `hd,cdrom`,
3. the empty disk is not bootable, so the VM **falls through to the CDROM** and
   live-boots the Ubuntu **"Try Ubuntu"** desktop — a usable live GNOME session,
   no install. (For a lighter ~3.4 GB test that boots the server installer
   instead, swap the ISO in [`conf.yml`](conf.yml) to the live-server one noted
   there.)

It intentionally does **not** install or log into an OS — the point is to verify
the ISO-boot path itself.

## Prerequisites

- libvirt/KVM working (`virsh -c qemu:///system list`), and `sudo` for
  `qemu:///system` (the box uses `use_sudo: true`).
- `wget` or `curl` on `PATH` (boxman uses them to fetch the ISO).
- ~6.2 GB of network + disk for the ISO, plus a few GB for the VM disk. The VM
  gets 4 GB RAM (the Ubuntu desktop live minimum).
- The **local** runtime (ISO boot is not supported under docker-compose yet).

## Bring it up

```bash
cd boxes/ubuntu-24.04-live-iso-boot
boxman up
```

> First run downloads ~6.2 GB and boots a VM — give it a few minutes. Re-runs
> reuse the cached, checksum-verified ISO.

## How to test / verify

Let `V=bprj__boxman_dev_ubuntu-24.04-live-iso-boot__bprj_live_ubuntu-live01` (the
fully-qualified libvirt name; `virsh list` shows it).

**1. ISO was downloaded and checksum-verified** — the `boxman up` log shows
`checksum ok (sha256: …)`, and the file exists:

```bash
ls -lh ~/.cache/boxman/images/ubuntu-noble-live-*.iso
```

**2. The VM is defined and running:**

```bash
virsh -c qemu:///system list --all | grep ubuntu-live01
```

**3. The ISO is attached and the boot order is `hd,cdrom`** (the core of the
feature):

```bash
virsh -c qemu:///system dumpxml "$V" | grep -A2 '<boot\|cdrom\|\.iso'
# expect: <disk device='cdrom'> … <source file='…ubuntu-noble-live-….iso'/>
#         <boot dev='hd'/> <boot dev='cdrom'/>
```

**4. It actually booted the live environment** — watch the console (graphical):

```bash
virsh -c qemu:///system vncdisplay "$V"     # e.g. :0
virt-viewer -c qemu:///system "$V"          # or: remote-viewer vnc://127.0.0.1:5900
```

You should see the Ubuntu boot menu, then the **"Try Ubuntu" welcome screen /
GNOME live desktop**.

**5. (Automated signal) the live environment requests DHCP** — once it boots,
the installer brings up networking and gets a lease from the box's NAT network:

```bash
virsh -c qemu:///system net-dhcp-leases \
  bprj__boxman_dev_ubuntu-24.04-live-iso-boot__bprj__clstr__live__clstr__live-net
```

A lease for the VM's MAC confirms it booted far enough to configure the network
from the ISO.

## Tearing down

```bash
boxman destroy
```

The cached ISO stays under `~/.cache/boxman/images`; delete it there to force a
re-download.
