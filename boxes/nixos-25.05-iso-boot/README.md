# nixos-25.05-iso-boot

Boots a single VM from the official, checksummed **NixOS 25.05 minimal
installer ISO**, using the same ISO-boot path as
[`ubuntu-24.04-live-iso-boot`](../ubuntu-24.04-live-iso-boot) — no template, no
clone, no cloud-init.

Verified end to end on 2026-08-17: `boxman up` exits 0 in ~70 s (after the
download), and the guest reaches the installer's `[nixos@nixos:~]$` auto-login
shell and takes a DHCP lease.

## Read this first: what this box does *not* do

It **boots** NixOS. It does not **install or provision** it. There is no admin
user and no injected SSH key, so `boxman ssh` will not reach the guest.

(boxman *does* still generate an `ssh_config` and an `id_ed25519_boxman`
keypair in the workspace, and `boxman up` prints an `ssh …` line for the VM.
Ignore both: the `admin` user they name does not exist in the guest. `boxman
up` also ends with `ERROR: failed to add ssh keys to some vms`, which is the
correct outcome here, not a fault.)

That is not an oversight, it is the current limit of the tooling:

- This box boots the **minimal installer ISO**, which is a live installer: it
  does not consume boxman's NoCloud seed and creates no user for boxman to
  inject a key into. That, not any limitation of NixOS, is why it is not
  SSH-able.
- NixOS itself supports cloud-init perfectly well
  ([`services.cloud-init.enable`](https://github.com/NixOS/nixpkgs/blob/nixos-25.05/nixos/modules/services/system/cloud-init.nix)),
  and the official
  [`proxmoxImage`](https://github.com/NixOS/nixpkgs/blob/nixos-25.05/nixos/release.nix)
  job builds a disk image with cloud-init, sshd and the qemu guest agent
  enabled. What is missing is a *generic* published cloud qcow2 that boxman
  could point `templates:` at — the installer ISO is the only artifact this
  box can pin without building one.
- boxman's `templates:` path **always** builds a NoCloud seed and then waits on
  cloud-init plus the qemu guest agent (`providers/libvirt/cloudinit.py`). A
  NixOS image pushed through it would time out and never receive a key, so
  using `templates:` here would produce a box that looks supported and isn't.

Getting from "boots" to "usable, SSH-able NixOS guest" does **not** require a
`nixos-rebuild` hook in boxman. The straightforward route is a
cloud-init-enabled NixOS disk image — built with
[`nixos-generators`](https://github.com/nix-community/nixos-generators) or
modelled on the upstream `proxmoxImage` job — used as an ordinary
`templates:` image. The boxman gap is therefore about **obtaining, building
or importing** such an image (and the guest-agent assumptions in the
verification phase), not about NixOS lacking the mechanism. See #149.

Until this box ships such an image, the two ways to finish the job by hand
are both **outside** boxman:

- interactively, in the guest console (`nixos-generate-config`, edit
  `/etc/nixos/configuration.nix`, `nixos-install`), or
- with [`nixos-anywhere`](https://github.com/nix-community/nixos-anywhere)
  driving the booted installer from your workstation.

## Prerequisites

- libvirt/KVM working (`virsh -c qemu:///system list`), and `sudo` for
  `qemu:///system` (the box uses `use_sudo: true`).
- `wget` or `curl` on `PATH` (boxman uses them to fetch the ISO).
- **~1.65 GB** of network + disk for the ISO, plus 20 GB for the VM disk.
- The **local** runtime — ISO boot is rejected under docker-compose, because
  the cached ISO is not visible inside the libvirt container.

## Bring it up

```bash
cd boxes/nixos-25.05-iso-boot
boxman up
```

> The first run downloads ~1.65 GB and boots a VM. Re-runs reuse the cached,
> checksum-verified ISO under `~/.cache/boxman/images`.

## How to test / verify

Let `V=bprj__boxman_dev_nixos-25.05-iso-boot__bprj_nixos_nixos01` (the
fully-qualified libvirt name; `virsh list` shows it).

**1. ISO was downloaded and checksum-verified** — the file exists:

```bash
ls -lh "$(boxman conf 2>/dev/null | grep -i cache_dir | awk '{print $2}')"/nixos-minimal-25.05-*.iso
# or just look in your configured cache dir, default ~/.cache/boxman/images
```

Note: at default verbosity boxman prints **no** `checksum ok` line — that
message logs at `INFO` while the default level is `STATUS`. Verification does
run (a mismatch raises `Checksum mismatch for ISO` and evicts the file), but if
you want to see it, run with `-v`. To check independently:

```bash
sha256sum <the cached iso>
# 38dee38fd5b5f2429c35aef7d9cc039a21cafbd93809adf061d29149e3583c94
```

**2. The VM is defined and running:**

```bash
virsh -c qemu:///system list --all | grep nixos01
```

**3. The ISO is attached as a CDROM:**

```bash
virsh -c qemu:///system dumpxml "$V" | grep -A2 '<boot\|cdrom\|\.iso'
# expect: <disk device='cdrom'> … <source file='…nixos-minimal-25.05-….iso'/>
#         <boot dev='cdrom'/> <boot dev='hd'/>
```

Note the boot order you actually get is **`cdrom` then `hd`**, even though
`IsoBootVM` asks virt-install for `hd,cdrom`: passing `--cdrom=` puts
virt-install into two-phase install mode, and boxman never reaches the
post-install reconfigure. For this box it makes no difference — it live-boots
and never installs — but do not rely on the `hd`-first ordering.

**4. It actually booted the installer** — watch the console:

```bash
virsh -c qemu:///system console "$V"       # serial; or use the graphical one:
virt-viewer -c qemu:///system "$V"
```

You should reach the NixOS installer's shell, auto-logged in as the `nixos`
user with a `[nixos@nixos:~]$` prompt.

**5. (Automated signal) the installer requests DHCP** — the ISO brings up
networking on boot and takes a lease from this box's NAT network:

```bash
virsh -c qemu:///system net-dhcp-leases \
  bprj__boxman_dev_nixos-25.05-iso-boot__bprj__clstr__nixos__clstr__nixos-net
```

A lease for the VM's MAC confirms it booted far enough to configure networking
from the ISO. This is the best non-interactive signal available, precisely
because there is no guest agent and no SSH.

## Going further: actually installing

Inside the guest console, the installer ISO is a normal NixOS live system:

```bash
sudo -i
passwd nixos                 # needed before sshd will accept a remote login
ip -brief addr               # note the address handed out above
```

From there either install by hand, or point `nixos-anywhere` at that address.
Anything you install lands on the empty 20 G disk.

Be aware the domain boots **cdrom first** (see above), so once you have
installed something you must detach the CDROM — or change the boot order — or
the VM will drop back into the installer on every reboot.

## Refreshing the ISO

The `uri` is deliberately the **release-pinned** `releases.nixos.org` path that
`channels.nixos.org` redirects to, not the `latest-nixos-minimal-…` alias. The
alias moves as the channel advances and would break the checksum. To move to a
newer release, take both the URL and its hash from the sibling `.iso.sha256`
file on `releases.nixos.org` and update them together.

## Tearing down

```bash
boxman destroy
```

The cached ISO stays under `~/.cache/boxman/images`; delete it there to force a
re-download.
