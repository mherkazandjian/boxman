# windows-server-2022-iso-boot

Proof of concept: an **unattended Windows Server 2022 (Server Core)
install** on libvirt, reachable over **WinRM and OpenSSH** and driven by
Ansible. It uses boxman's ISO-boot path, which gives virt-install every
`cdroms:` entry, so Setup sees the driver ISO on its first boot. The
findings, the gaps it works around and the proposed design are in
[doc/windows-guests.md](../../doc/windows-guests.md).

This is a spike box. It is not a template and it is not cloned (see
[Known gaps](#known-gaps)).

## What happens

1. `make-answer-iso.sh` renders `autounattend.xml.in` (password, computer
   name) and writes it, `boxman-setup.ps1` and, optionally, a public key
   onto a small ISO in the workspace.
2. `boxman up` downloads the evaluation ISO into the cache and checks its
   sha256. It creates an empty 40G virtio disk and runs `virt-install` with
   three CD-ROMs: the Windows ISO, the host's `virtio-win.iso`, and the
   answer ISO. All three are `install_only` (see step 5).
3. WinPE finds `autounattend.xml` on the answer ISO. It loads `viostor` and
   `NetKVM` from the virtio-win ISO, partitions the disk (MBR, SeaBIOS) and
   applies the Server Core image. It then reboots into the disk it just
   installed: the firmware order is `hd,cdrom`, and boxman runs
   virt-install without an install phase, so the reboot is a plain reboot.
4. After that reboot: specialize, OOBE, one auto-logon as Administrator,
   and `boxman-setup.ps1`. That installs all virtio drivers and the QEMU
   guest agent, enables WinRM (HTTP 5985, NTLM, Basic off) and installs
   OpenSSH Server from Windows Update. The last `boxman-setup:` line in
   `C:\boxman-setup.log` is `boxman-setup: done`, or `boxman-setup:
   FAILED: …` naming the steps that did not complete (the transcript's
   footer follows it). The script then turns auto-logon off, deletes the
   password Setup stored for it, and logs that session off.
5. The first `boxman up` or `boxman update` after that ejects the three
   CD-ROMs: the guest agent reports `C:\` on the boot disk, which is
   boxman's sign that the installed system is up. Until then, each `up`
   and `update` logs why the media stay and how to eject them by hand.
   The answer ISO holds the Administrator password in plain text, so this
   matters: until the eject, any process in the guest can read it from
   the drive. `boxman update` never compares the three: it neither puts
   them back after the eject nor detaches them mid-install.

Measured on the test host: about 85 s from `virt-install` to Setup's first reboot,
and about 2 min from that reboot to WinRM and SSH answering.

## Prerequisites

- The **local** runtime, libvirt/KVM, and `sudo` for `qemu:///system`.
- `dnf install virtio-win` (provides `/usr/share/virtio-win/virtio-win.iso`).
- `genisoimage`, `mkisofs` or `xorrisofs`. `xmllint` is optional and used
  if present.
- About 4.7 GiB for the ISO in `~/.cache/boxman/images`, and 7-8 GB for the
  installed disk. The VM has 2 vCPU and 4 GiB RAM.
- Internet access from the guest, for the OpenSSH Feature on Demand.

## Bring it up

```bash
cd boxes/windows-server-2022-iso-boot
export BOXMAN_WIN_ADMIN_PASS='...'                  # 8+ chars, 3 character classes
export BOXMAN_WIN_SSH_PUBKEY=~/.ssh/id_ed25519.pub  # optional
./make-answer-iso.sh
boxman up
```

Once `C:\boxman-setup.log` says `boxman-setup: done` (or
`boxman-setup: FAILED: …`, which names the steps left to do by hand),
run `boxman up` again: it ejects the install media (step 5) and rewrites
the ssh config.

```bash
boxman up
virsh -c qemu:///system domblklist \
  bprj__boxman_dev_windows-server-2022-iso-boot__bprj_win_win01   # sda-sdc: -
```

## Verify

```bash
IP=$(virsh -c qemu:///system domifaddr \
       bprj__boxman_dev_windows-server-2022-iso-boot__bprj_win_win01 --source lease \
     | awk '/ipv4/{split($4,a,"/");print a[1]}')

# OpenSSH (only if BOXMAN_WIN_SSH_PUBKEY was set)
ssh Administrator@$IP 'powershell -c "(Select-String -Path C:\boxman-setup.log -Pattern ^boxman-setup: | Select-Object -Last 1).Line"'

# Ansible over WinRM (needs pywinrm on the controller)
ansible all -i "$IP," -m ansible.windows.win_ping \
  -e ansible_connection=winrm -e ansible_winrm_transport=ntlm \
  -e ansible_winrm_scheme=http -e ansible_port=5985 \
  -e ansible_user=Administrator -e ansible_password="$BOXMAN_WIN_ADMIN_PASS"

# Ansible over SSH
ansible all -i "$IP," -m ansible.windows.win_ping \
  -e ansible_connection=ssh -e ansible_shell_type=powershell \
  -e ansible_user=Administrator
```

## Known gaps

All of these are covered in the doc.

- **`boxman ssh` / generated `ssh_config` do not work.** They use
  `User admin` and boxman's own key. Use `Administrator` and the key you
  baked into the answer ISO. The generated inventory has no
  `ansible_connection` or `ansible_shell_type`.
- **No `admin_pass` key push.** It runs `ssh-copy-id`, which writes
  `~/.ssh/authorized_keys`. Windows' sshd reads
  `C:\ProgramData\ssh\administrators_authorized_keys` for administrators.
- **A guest whose agent does not run keeps its install media.** boxman
  ejects them only once the guest agent reports the installed system, so
  if `boxman-setup.ps1` could not install the agent, eject them by hand:
  each `up` and `update` logs the `virsh change-media` command.
- **Licensing.** The ISO is a 180-day evaluation for test/demo use. Do not
  push the ISO or a disk installed from it to a registry.
