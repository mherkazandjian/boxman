# windows-server-2022-iso-boot

Proof of concept: an **unattended Windows Server 2022 (Server Core)
install** on libvirt, reachable over **WinRM and OpenSSH** and driven by
Ansible. It uses boxman's existing ISO-boot path with no code change. The
findings, the gaps it works around and the proposed design are in
[doc/windows-guests.md](../../doc/windows-guests.md).

This is a spike box. It is not a template, it is not cloned, and it needs
one manual step (see [Known gaps](#known-gaps)).

## What happens

1. `make-answer-iso.sh` renders `autounattend.xml.in` (password, computer
   name) and writes it, `boxman-setup.ps1` and, optionally, a public key
   onto a small ISO in the workspace.
2. `boxman up` downloads the evaluation ISO into the cache and checks its
   sha256. It creates an empty 40G virtio disk and runs `virt-install` with
   three CD-ROMs: the Windows ISO, the host's `virtio-win.iso`, and the
   answer ISO.
3. WinPE finds `autounattend.xml` on the answer ISO. It loads `viostor` and
   `NetKVM` from the virtio-win ISO, partitions the disk (MBR, SeaBIOS) and
   applies the Server Core image. It then reboots; virt-install, kept
   waiting by `--wait=30` (at most 30 minutes), starts the VM again.
4. After the next start: specialize, OOBE, one auto-logon as Administrator,
   and `boxman-setup.ps1`. That installs all virtio drivers and the QEMU
   guest agent, enables WinRM (HTTP 5985, NTLM, Basic off) and installs
   OpenSSH Server from Windows Update. `C:\boxman-setup.log` ends with
   `boxman-setup: done`, or with `boxman-setup: FAILED: …` naming the
   steps that did not complete. The script then logs the auto-logon
   session off.

Measured on the test host: about 85 s from `virt-install` to Setup's first reboot,
and about 2 min from the second start to WinRM and SSH answering.

## Prerequisites

- The **local** runtime, libvirt/KVM, and `sudo` for `qemu:///system`.
- `dnf install virtio-win` (provides `/usr/share/virtio-win/virtio-win.iso`).
- `genisoimage`. `xmllint` is optional and used if present.
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

Once `C:\boxman-setup.log` ends with `boxman-setup: done` (or with
`boxman-setup: FAILED: …`, which names the steps left to do by hand),
**eject the answer ISO**. It holds the Administrator password in plain text, and
otherwise stays attached for the VM's whole life:

```bash
virsh -c qemu:///system change-media \
  bprj__boxman_dev_windows-server-2022-iso-boot__bprj_win_win01 sdc \
  --eject --live --config
```

## Verify

```bash
IP=$(virsh -c qemu:///system domifaddr \
       bprj__boxman_dev_windows-server-2022-iso-boot__bprj_win_win01 --source lease \
     | awk '/ipv4/{split($4,a,"/");print a[1]}')

# OpenSSH (only if BOXMAN_WIN_SSH_PUBKEY was set)
ssh Administrator@$IP 'powershell -c "Get-Content C:\boxman-setup.log -Tail 2"'

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

- **Powers off after the first Setup reboot without `--wait=30`.**
  virt-install's install-phase domain has `on_reboot=destroy`, and boxman
  runs it with `--wait=0`. The box overrides that with a later `--wait=30`;
  without it the VM stays off, and the first `boxman up` waits 600 s and
  exits 0. If Setup stalls, virt-install gives up after 30 minutes and
  `boxman up` fails, leaving the VM running so its console can be checked.
- **`boxman ssh` / generated `ssh_config` do not work.** They use
  `User admin` and boxman's own key. Use `Administrator` and the key you
  baked into the answer ISO. The generated inventory has no
  `ansible_connection` or `ansible_shell_type`.
- **No `admin_pass` key push.** It runs `ssh-copy-id`, which writes
  `~/.ssh/authorized_keys`. Windows' sshd reads
  `C:\ProgramData\ssh\administrators_authorized_keys` for administrators.
- **The answer ISO stays attached** until you eject it (above).
- **Licensing.** The ISO is a 180-day evaluation for test/demo use. Do not
  push the ISO or a disk installed from it to a registry.
