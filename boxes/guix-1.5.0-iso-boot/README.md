# guix-1.5.0-iso-boot

Boots a single VM from the official **Guix System 1.5.0 installer ISO**, using
the same ISO-boot path as
[`ubuntu-24.04-live-iso-boot`](../ubuntu-24.04-live-iso-boot) and
[`nixos-25.05-iso-boot`](../nixos-25.05-iso-boot) — no template, no clone, no
cloud-init.

Verified end to end on 2026-08-17: `boxman up` exits 0 in ~2 min including the
download, the pinned checksum matches, and the guest reaches the Guix guided
installer's locale screen and takes a DHCP lease (hostname `gnu`).

## Read this first: what this box does *not* do

It **boots** Guix System's installer. It does not **install or provision** it.
There is no admin user and no injected SSH key, so `boxman ssh` will not reach
the guest.

(boxman *does* still generate an `ssh_config` and an `id_ed25519_boxman`
keypair in the workspace, and `boxman up` prints an `ssh …` line for the VM.
Ignore both: the `admin` user they name does not exist in the guest. `boxman
up` also ends with `ERROR: failed to add ssh keys to some vms`, which is the
correct outcome here, not a fault.)

As with the NixOS box, that is the current limit of the tooling rather than an
oversight:

- Guix System has **no native cloud-init**, so boxman's NoCloud seed — the
  mechanism it uses to inject a key at provision time — has nothing to
  consume it. GNU does publish a bootable
  `guix-system-vm-image-1.5.0.x86_64-linux.qcow2`, but it is a GNOME desktop
  *demo* image with a fixed user.
- That does **not** mean an SSH-able Guix image is impossible. `guix system
  image --image-type=qcow2` builds one from an `operating-system`
  declaration, and that declaration can include `openssh-service-type` with
  authorized keys baked in ahead of time.
- boxman's `templates:` path **always** builds a NoCloud seed and then waits on
  cloud-init plus the qemu guest agent
  (`providers/libvirt/cloudinit.py`). Either Guix artifact pushed through it
  would time out and never receive a key.

So the real boxman gaps for Guix are narrower than "no cloud-init": **dynamic
key injection** (boxman generates a keypair at provision time, which a
prebuilt image cannot know in advance), **image build/import** (boxman has no
way to build a `guix system image` or consume one as a template), and the
**guest-agent assumptions** in the template verification phase. A
declaratively prebuilt image with your own key already in it sidesteps the
first of those, at the cost of the image no longer being a signed upstream
artifact. See #149.

Until this box ships such an image, finishing the install is done in the guest
console.

## About the checksum

This is the one box here whose checksum did **not** come from upstream. GNU
publishes only a detached OpenPGP signature for this ISO, no `.sha256`. The
hash pinned in [`conf.yml`](conf.yml) was computed locally *after* verifying
that signature, and you can reproduce the whole chain:

```bash
curl -O https://ftp.gnu.org/gnu/guix/guix-system-install-1.5.0.x86_64-linux.iso
curl -O https://ftp.gnu.org/gnu/guix/guix-system-install-1.5.0.x86_64-linux.iso.sig
gpg --recv-keys A28BF40C3E551372662D14F741AAE7DCCA3D8351
gpg --verify guix-system-install-1.5.0.x86_64-linux.iso.sig
sha256sum guix-system-install-1.5.0.x86_64-linux.iso
# 107e0a8082f03a10b15c1fb9383d2d752c1cdeda41b8db575a15550e1c2d8b4a
```

The signature is from **Efraim Flashner**, a Guix maintainer, made 2026-01-22.
The OpenPGP signature is the real authority here; the `sha256:` in `conf.yml`
only pins what boxman downloads so a corrupted or swapped file fails loudly.

## Prerequisites

- libvirt/KVM working (`virsh -c qemu:///system list`), and `sudo` for
  `qemu:///system` (the box uses `use_sudo: true`).
- `wget` or `curl` on `PATH` (boxman uses them to fetch the ISO).
- **~1.13 GB** of network + disk for the ISO, plus 30 GB for the VM disk.
- The **local** runtime — ISO boot is rejected under docker-compose, because
  the cached ISO is not visible inside the libvirt container.

## Bring it up

```bash
cd boxes/guix-1.5.0-iso-boot
boxman up
```

> The first run downloads ~1.13 GB and boots a VM. Re-runs reuse the cached,
> checksum-verified ISO under `~/.cache/boxman/images`.

## How to test / verify

Let `V=bprj__boxman_dev_guix-1.5.0-iso-boot__bprj_guix_guix01` (the
fully-qualified libvirt name; `virsh list` shows it).

**1. ISO was downloaded and checksum-verified** — the file exists in your
configured cache dir (default `~/.cache/boxman/images`):

```bash
ls -lh <cache_dir>/guix-system-install-1.5.0-*.iso
```

Note: at default verbosity boxman prints **no** `checksum ok` line — that
message logs at `INFO` while the default level is `STATUS`. Verification does
run (a mismatch raises `Checksum mismatch for ISO` and evicts the file). Given
this box's hand-computed hash, checking it yourself is worthwhile:

```bash
sha256sum <the cached iso>
# 107e0a8082f03a10b15c1fb9383d2d752c1cdeda41b8db575a15550e1c2d8b4a
```

**2. The VM is defined and running:**

```bash
virsh -c qemu:///system list --all | grep guix01
```

**3. The ISO is attached as a CDROM:**

```bash
virsh -c qemu:///system dumpxml "$V" | grep -A2 '<boot\|cdrom\|\.iso'
# expect: <disk device='cdrom'> … <source file='…guix-system-install-….iso'/>
#         <boot dev='cdrom'/> <boot dev='hd'/>
```

Note the boot order you actually get is **`cdrom` then `hd`**, even though
`IsoBootVM` asks virt-install for `hd,cdrom`: passing `--cdrom=` puts
virt-install into two-phase install mode, and boxman never reaches the
post-install reconfigure. For this box it makes no difference — it boots the
installer and never completes one — but do not rely on the `hd`-first ordering.

**4. It actually booted the installer** — watch the console:

```bash
virt-viewer -c qemu:///system "$V"          # or: virsh console "$V"
```

You should reach the GNU Guix **guided installer** (a text-mode/ncurses
wizard), starting at its language-selection screen. Switching to another tty
(`Ctrl-Alt-F3` in virt-viewer) gets you a root shell in the live system
instead.

**5. (Automated signal) the installer requests DHCP** — it brings up networking
and takes a lease from this box's NAT network:

```bash
virsh -c qemu:///system net-dhcp-leases \
  bprj__boxman_dev_guix-1.5.0-iso-boot__bprj__clstr__guix__clstr__guix-net
```

A lease for the VM's MAC confirms it booted far enough to configure networking
from the ISO. As with the NixOS box, this is the best non-interactive signal
available — there is no guest agent and no SSH.

## Going further: actually installing

Run the guided installer on the console, or drop to a shell on another tty and
write an `operating-system` declaration by hand and `guix system init` it onto
the 30 G disk.

Be aware the domain boots **cdrom first** (see above), so once you have
installed something you must detach the CDROM — or change the boot order — or
the VM will drop back into the installer on every reboot.

If you install, remember Guix pulls a large number of substitutes on first
build; the VM needs working outbound networking through `guix-net`.

## Refreshing the ISO

Newer releases appear at <https://ftp.gnu.org/gnu/guix/>. Because upstream
still publishes no `.sha256`, refreshing means repeating the verify-then-hash
procedure above and updating `uri` and `checksum` together.

## Tearing down

```bash
boxman destroy
```

The cached ISO stays under `~/.cache/boxman/images`; delete it there to force a
re-download.
