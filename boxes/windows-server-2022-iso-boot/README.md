# windows-server-2022-iso-boot

An **unattended Windows Server 2022 (Server Core, evaluation) install** on
libvirt, reachable over OpenSSH with `boxman ssh` and Ansible. The box is a
`conf.yml` only: `os: windows` on an ISO-boot VM makes boxman build the
answer ISO itself, from its own answer file and setup script
(`src/boxman/assets/windows/`). The findings and the design are in
[doc/windows-guests.md](../../doc/windows-guests.md).

It is not a template and it is not cloned (see [Known gaps](#known-gaps)).

## What happens

1. `boxman up` checks the Administrator password (`admin_pass`, from
   `BOXMAN_WIN_ADMIN_PASS`) against Windows' default policy, and the
   computer name, before it builds anything: OOBE would otherwise stop at
   a prompt nobody answers.
2. It downloads the evaluation ISO into the cache and checks its sha256,
   generates the cluster's ssh key pair, and builds the answer ISO in the
   workspace: the rendered `autounattend.xml`, `boxman-setup.ps1`, and
   `authorized_keys` (the cluster's public key, plus `ssh.authorized_keys`
   from `boxman.yml`). The ISO is written 0600 from a private staging
   directory: it holds the password in plain text.
3. It creates an empty 40G virtio disk and runs `virt-install` with three
   CD-ROMs, all `install_only`: the Windows ISO, the host's
   `virtio-win.iso`, and the answer ISO. The domain gets a UTC real-time
   clock (`--clock offset=utc`).
4. WinPE finds `autounattend.xml` on the answer ISO, loads `viostor` and
   `NetKVM` from the virtio-win ISO, partitions the disk (MBR, SeaBIOS) and
   applies the Server Core image. It then reboots into the installed disk:
   the firmware order is `hd,cdrom`, and there is no virt-install install
   phase, so the reboot is a plain reboot.
5. Specialize (computer name, time zone, the RTC read as UTC), OOBE, one
   auto-logon as Administrator, and `boxman-setup.ps1`: it reads the
   public keys off the answer ISO, installs the virtio drivers and then
   the QEMU guest agent, sets the network profile private, installs
   OpenSSH Server from Windows Update with PowerShell as its shell, and
   writes the keys to `administrators_authorized_keys`. The last
   `boxman-setup:` line in `C:\boxman-setup.log` is `boxman-setup: done`,
   or `boxman-setup: FAILED: …` naming the steps that did not complete. It
   then turns auto-logon off, deletes the password Setup stored for it,
   and logs that session off.
6. The first `boxman up` or `boxman update` that finds the guest agent
   reporting `C:\` on the boot disk ejects the three CD-ROMs and deletes
   the answer ISO. That can be the first `up` itself, when the agent is up
   by the end of its IP wait. Until then each run logs why the media stay;
   `boxman control finish-install --vms win_win01` ejects them on your
   word (and deletes the answer ISO).
   `boxman update` never compares them: it neither puts them back after
   the eject nor detaches them mid-install.

Measured on the test host: about 2 min from `virt-install` to the
installed system's DHCP lease, and 3 to 3.5 min to `boxman-setup: done`.

## Prerequisites

- The **local** runtime, libvirt/KVM, and `sudo` for `qemu:///system`.
  libvirt's `dynamic_ownership` (on by default): it gives the 0600 answer
  ISO to qemu while the VM runs.
- `dnf install virtio-win` (provides `/usr/share/virtio-win/virtio-win.iso`).
- `genisoimage`, `mkisofs` or `xorrisofs`.
- About 4.7 GiB for the ISO in boxman's image cache, and 7-8 GB for the
  installed disk. The VM has 2 vCPU and 4 GiB RAM.
- Internet access from the guest, for the OpenSSH Feature on Demand.

## Bring it up

```bash
cd boxes/windows-server-2022-iso-boot
export BOXMAN_WIN_ADMIN_PASS='...'   # 8+ chars, 3 of upper/lower/digit/symbol
boxman up                            # the install runs on after it returns
# once C:\boxman-setup.log says `boxman-setup: done`, a few minutes later:
boxman up                            # ejects the install media, if the first did not
```

Only the `up` that creates the VM needs the password. To set more than the
defaults, give the VM an `unattend:` block (commented out in `conf.yml`):
your own template or files, `image_index: 2` for Desktop Experience, or
`vars:` such as `timezone`. An `admin_user` other than Administrator is
created in Administrators with the same password, and becomes the ssh user.

## Verify

```bash
boxman ssh win_win01      # PowerShell, as Administrator, with the cluster key

virsh -c qemu:///system domblklist \
  bprj__boxman_dev_windows-server-2022-iso-boot__bprj_win_win01   # sda-sdc: -

# Ansible over SSH (needs the ansible.windows collection); the generated
# inventory sets ansible_shell_type: powershell
cd ~/workspaces/windows-server-2022-iso-boot && . ./env.sh
ansible all -m ansible.windows.win_ping
```

## Known gaps

All of these are covered in the doc.

- **OpenSSH only.** boxman sets up and uses OpenSSH; it does not configure
  WinRM (Windows Server's own listener on 5985 is left as Windows has it).
- **A guest whose agent does not run keeps its install media.** boxman
  ejects them only once the guest agent reports the installed system. If
  `boxman-setup.ps1` could not install the agent, declare the install
  done: `boxman control finish-install --vms win_win01`. The answer ISO
  is deleted with the VM in any case.
- **Not a template.** Each VM is a 3-minute install; cloning a sysprep'd
  local template is Phase 2.
- **Licensing.** The ISO is a 180-day evaluation for test/demo use. Do not
  push the ISO or a disk installed from it to a registry.
