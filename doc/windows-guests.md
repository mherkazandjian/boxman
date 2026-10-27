# Windows guests on libvirt

Findings of a spike: can boxman provision Windows guests on libvirt the way
it does Linux ones? This document holds what was measured, a design
proposal, and the open questions. The proof-of-concept box is
[`boxes/windows-server-2022-iso-boot`](../boxes/windows-server-2022-iso-boot).

## 1. Answer

**Yes for a single installed guest, today, with one small boxman fix (G1,
in this branch). Not yet as a template that boxman clones.**

The PoC box installs Windows Server 2022 (Server Core, evaluation) unattended
through boxman's existing ISO-boot path (`boot_order: [cdrom, hd]` →
`IsoBootVM` → `virt-install`). The guest comes up with all virtio drivers
and the QEMU guest agent, and is reachable by Ansible over **both** WinRM and
OpenSSH (`ansible.windows.win_ping` → `pong` on both). The install takes 3 to
3.5 minutes of active time on the test host (§2).

It needs three workarounds in `virt_install_extra_args`, down from five once
G1 and G3 were fixed. Each points at a small, specific gap in boxman (§4). None of
them are architectural.
Clone-based Windows boxes, which would match how every Linux box works, need
more: a sysprep'd local template plus cloudbase-init, and boxman's
Linux-only clone-identity pass switched off (§5).

## 2. What was tested, and where

| | |
|---|---|
| Host | one libvirt/KVM host, shared with other users' VMs |
| Host CPU / RAM / disk | AMD x86-64, 16 threads, 1 NUMA node; 124 GiB RAM, of which ≈46 GiB was free alongside the VMs already running; several hundred GiB free on `/` |
| Nested virt | `kvm_amd nested=1` (on) |
| Stack | Rocky 9.7, libvirt 10.10.0, qemu-kvm 9.1.0, virt-install with osinfo-db 20250606, edk2-ovmf 20241117, swtpm 0.8.0, **virtio-win 1.9.49 (RPM, already installed; the host has since moved to 1.9.61, same layout)** |
| qemu process user | `root` (`/etc/libvirt/qemu.conf`): disks under a 0700 home still work. That is a property of this host, not a requirement. |
| Guest | Windows Server 2022 Standard Evaluation (Server Core), build 20348, en-US |
| Media | the Evaluation Center ISO (§3.5), downloaded once |
| boxman | this branch (`f06c8fe` + the PoC box), local runtime, run from the worktree |

What was verified, and how:

| Claim | Evidence |
|---|---|
| Unattended install completes with no console interaction | answer file on a third CD-ROM. Two runs, the second from scratch with the committed config: install phase 84 s / 85 s (libvirt log); in the clean run, second `boxman up` 15:12:18 → DHCP address 17 s later → `boxman-setup: done` at 15:13:58 |
| Edition | `InstallationType: Server Core` (registry), *Windows Server 2022 Standard Evaluation* |
| virtio-blk boot disk works through Setup | WinPE loaded `viostor` from the virtio-win ISO via `PnpCustomizationsWinPE`; the image was applied to `vda` |
| virtio-net, balloon, guest agent | `Get-PnpDevice`: *Red Hat VirtIO Ethernet Adapter*, *VirtIO Balloon Driver*, *Red Hat VirtIO SCSI Disk Device* all `OK`; `BalloonService` and `QEMU-GA` running |
| Host-side agent features | `virsh domfsinfo`, `domfstrim` (rc 0, what `boxman storage trim` uses), `guestinfo --os`, and `qemu-agent-command guest-ping` 9 s after boot. This works only after the channel fix in §4 G3. |
| Graceful shutdown | `virsh shutdown` → `shut off` in ≈9 s (ACPI) |
| WinRM | HTTP 5985, NTLM with message encryption, Basic disabled: `win_ping` → `pong` |
| OpenSSH | `ssh Administrator@…` with a baked-in key; Ansible `ansible_connection=ssh`, `ansible_shell_type=powershell`: `win_ping` → `pong` |
| Licensing state | `slmgr /dli`: *ServerStandardEval, TIMEBASED_EVAL channel, 180 days*; `slmgr /dlv`: rearm count 6 |
| Footprint | Server Core: 7.4 GB used on C:, qcow2 6.2 GB after install |
| `boxman up` idempotence | a second `up` on the running VM found its DHCP address and wrote `ssh_config`/inventory |
| Secrets after install | Windows scrubbed the password from every file under `C:\Windows\Panther` (checked for the plain-text value). The attached answer ISO still exposed it (`F:\autounattend.xml`) until `virsh change-media … --eject --live --config`; after that the guest could no longer read it (G7) |
| `boxman up` exit status during G2 | the first `up` exited **0** after its 600 s IP wait with the only VM shut off (rc captured from boxman itself, not a pipe) |
| Teardown and shared media | **Not safe on older boxman.** A boxman without 248235f (#208) undefines with `--remove-all-storage --wipe-storage`, which zero-filled both the cached eval ISO and the RPM-owned `/usr/share/virtio-win` ISO on 2026-10-15. With 248235f, undefine passes no storage flags and boxman removes only the storage it owns. Until then, eject or detach the CD-ROMs before `boxman destroy` |

The committed answer-file template differs from the one used in the clean
run in one way: after review it no longer contains the RDP setting
(`Microsoft-Windows-TerminalServices-LocalSessionManager`, which was dead
without a firewall rule). That block was removed, nothing was added, and the
file still validates with `xmllint`.

Review changed the box further after those runs: `boxman-setup.ps1` reports
failed steps and logs its session off, the install wait is bounded
(`--wait=30` instead of `-1`), and virtio-win is a `cdroms:` entry that
boxman hands to virt-install (G1, fixed in this branch) instead of a raw
`--disk`, and the agent channel comes from boxman (G3) instead of a raw
`--channel`. A fresh install with the box at `2e52d33`, on a second host
(Rocky 9.6, osinfo-db 20250124, virtio-win 1.9.61), confirmed them: all
three CD-ROMs were in the definition virt-install created (`sda`-`sdc`),
the install phase took 87 s, virt-install restarted the guest 0.3 s after
Setup's reboot, `boxman-setup: done` came 77 s later with no session left
logged on (`quser`), `guest-ping` answered over the channel boxman added,
the eject by path worked, and `boxman update --dry-run` then found no
changes.

Not tested: Windows 11 / Server 2025, UEFI + Secure Boot + TPM, Desktop
Experience, cloudbase-init, sysprep + clone, snapshots of a Windows guest,
the docker runtime (boxman refuses ISO boot there anyway).

## 3. Findings by topic

### 3.1 Unattended install vs golden image

The two are not alternatives. **The answer file is how the golden image gets
built**, and the golden image is what makes N guests cheap.

| | `autounattend.xml` from the ISO, per VM | Local golden image (sysprep'd template), cloned |
|---|---|---|
| Time per VM | 3 to 3.5 min active (Core); Desktop Experience is slower (not measured) | seconds to clone, plus first-boot specialize/OOBE (≈1 min, estimated) |
| Fits boxman today | yes: ISO-boot path, this PoC | partly: clone path exists, but its identity pass is Linux-only (§3.6) |
| Per-VM identity (SID, name, password) | native: each install is unique | needs `sysprep /generalize` in the template plus per-clone data (cloudbase-init) |
| What must be redistributed | nothing: answer file and scripts only | nothing, **if the image is built on each host** (§3.5) |
| Reproducibility | answer file + ISO checksum | same, plus the build recipe |

Recommendation: keep both, layered. Phase 1 is the per-VM unattended install
(this PoC, hardened). Phase 2 uses the same answer file, plus a final
`sysprep /generalize /oobe /shutdown`, to build a **local** template through
boxman's `templates:` section. Clusters then clone from it.

Things learned about the answer file:

- **Firmware.** virt-install picks **SeaBIOS + q35** for `win2k22`, so the
  answer file partitions MBR (a 500 MB active System partition plus C:). For
  UEFI (needed for Win11/Secure Boot), the partition layout changes to
  EFI/MSR/Windows. The Windows ISO's UEFI loader also shows *"Press any key
  to boot from CD"*. With `boot_order: [cdrom, hd]` (firmware order
  `hd,cdrom`) and an empty disk, nobody presses it and the boot falls
  through. The usual fixes are a remastered ISO using `efisys_noprompt.bin`,
  or `virsh send-key` timed against the console. Neither was tried.
- **Edition** is chosen by `/IMAGE/INDEX`. On the 2022 eval ISO, index 1 is
  Standard Core and index 2 is Standard with Desktop Experience.
  `install.wim` was not listed (no `wimlib` on the host). Index 1 was
  confirmed by the result: `InstallationType` is `Server Core`.
- **No product key** is needed for evaluation media.
- **A malformed answer file fails only at boot.** The first draft had `--`
  inside an XML comment, which is invalid XML (caught by `xmllint`, not by
  booting). The render script now runs `xmllint` first.
- **Secrets.** `AdministratorPassword` and `AutoLogon` are plain text in the
  answer file (`<PlainText>false</PlainText>` is only base64). The file is
  rendered from a template at build time, the ISO is written mode 0600, and
  nothing rendered is committed. On this host libvirt then chowns it to root
  while attached. In the guest, Setup keeps the `AutoLogon` password as the
  LSA secret `DefaultPassword` (not the registry value of that name), and it
  was still there after the one logon and two reboots, so `boxman-setup.ps1`
  deletes it. Without that, every snapshot or copy of the disk carries it.

### 3.2 virtio drivers: disk, net, balloon

Source: the **virtio-win** package (`dnf install virtio-win`; the Fedora
build lives at fedorapeople.org). It was already on the test host as
`/usr/share/virtio-win/virtio-win.iso`, a symlink to the versioned ISO
(1.9.49, 622 MB, during the spike; 1.9.61, 615 MB, since 2026-10-15). The PoC
uses the symlink, so it downloads nothing and survives package upgrades. Its licence file is BSD-3-Clause; the RPM is tagged
Apache-2.0 AND BSD-3-Clause AND GPL-2.0. The drivers are signed: Server 2022
loaded them with signature enforcement on and no test-signing. Whether this
build is WHQL-certified (RHEL's is; the Fedora build is not) does not matter
for a lab. The licences allow redistribution. 1.9.49 ships `2k22`, `2k25`,
`w10` and `w11` directories for every driver; 1.9.61 keeps the same
`viostor`/`NetKVM`/`Balloon` `2k22\amd64` paths and MSI names.

Injection, as done in the PoC:

| Phase | Mechanism | Drivers |
|---|---|---|
| WinPE (Setup) | `Microsoft-Windows-PnpCustomizationsWinPE` → `DriverPaths`, one entry per likely CD letter (`D:` to `F:`); Setup ignores the paths that do not exist | `viostor` (needed: the boot disk is virtio-blk), `NetKVM` |
| First logon | `msiexec /i virtio-win-gt-x64.msi /qn`, located by probing drive letters | everything else: Balloon, vioserial, viorng, fwcfg, pvpanic, … |
| First logon | `msiexec /i guest-agent\qemu-ga-x86_64.msi /qn` | QEMU guest agent |

Alternatives considered and not needed: `vioscsi` (a virtio-scsi controller
gives TRIM/discard and hot-plug). boxman uses virtio-blk, which supports
discard on Windows too, and `domfstrim` worked. Injecting drivers offline into
`boot.wim`/`install.wim` (DISM) avoids the second CD-ROM, but it means
remastering Microsoft's ISO, so we would be redistributing a modified ISO.
Avoid it.

**Balloon.** The driver and service run, but `virsh dommemstat` shows only
`actual`/`rss`. libvirt's balloon stats period is not set, so it reports
nothing from inside the guest. boxman's existing memballoon setting
(`stats_period`) should cover it; that was not tried.

### 3.3 cloudbase-init vs cloud-init

cloud-init does not run on Windows. **cloudbase-init** (Apache-2.0, MSI from
cloudbase.it) is the Windows equivalent, and it reads boxman's existing seed
format. According to its documentation
([services](https://cloudbase-init.readthedocs.io/en/latest/services.html),
[user data](https://cloudbase-init.readthedocs.io/en/latest/userdata.html);
not tested here):

- `NoCloudConfigDriveService` reads a config drive labelled `cidata`/`CIDATA`
  with `meta-data`, `user-data` and `network-config`. That is the NoCloud
  seed boxman already builds for Linux templates.
- It uses `instance-id`, `local-hostname` and `public-keys`, and runs
  user-data.
- **network-config: v1 and Debian ENI formats only.** boxman's default and
  documented template `cloudinit_network_config` is **v2**, so a Windows
  template needs a v1 document or none (DHCP).
- **cloud-config subset**: `write_files`, `set_timezone`, `set_hostname`,
  `groups`, `users`, `ntp`, `runcmd`. Not `chpasswd`, `ssh_pwauth`,
  `package_update` or `manage_etc_hosts`, all of which boxman's default
  Linux cloud-init uses. It also takes `#ps1_sysnative` PowerShell user-data.

So cloudbase-init is the right per-clone personalization layer for
Windows: hostname, Administrator password or a new user, SSH key, first-boot
script. It is **not needed** for the per-VM unattended install, where the
answer file does all of that. Recommendation: install it in the template
(Phase 2), not in Phase 1.

### 3.4 WinRM vs OpenSSH for Ansible

Both were verified. Recommendation: **OpenSSH as the default, WinRM as an
opt-in.**

| | OpenSSH | WinRM |
|---|---|---|
| Fits boxman | `ssh_config`, `boxman ssh`, ProxyJump under the docker runtime, key-based auth: all already ssh | a second transport boxman has never had; the docker-runtime jump host does not forward 5985 |
| Ansible | `ansible_connection=ssh`, `ansible_shell_type=powershell`; supported by `ansible.windows` | `ansible_connection=winrm`; needs **pywinrm** on the controller (not installed on the test host, so the PoC used a scratch venv) |
| Availability | Server 2022: a Feature on Demand, **downloaded from Windows Update** (needs guest internet, or an FoD ISO offline). Server 2025 / Win11: in the box | built in, enabled by `Enable-PSRemoting` |
| Auth in the PoC | key in `C:\ProgramData\ssh\administrators_authorized_keys` (ACL: Administrators + SYSTEM only) | NTLM over HTTP with message encryption; **Basic and AllowUnencrypted left off** |
| Weakness | | NTLM over HTTP is message-encrypted but NTLM itself is weak (relay, offline cracking of captured exchanges); acceptable on a host-only NAT, not on a shared network. Stronger: HTTPS listener (5986) with a self-signed cert boxman would generate, or Kerberos, which needs a domain. CredSSP only for double-hop |
| Gotcha | for members of Administrators, sshd ignores `~\.ssh\authorized_keys`, so boxman's `ssh-copy-id` push (§4 G5) would "succeed" and still not work | HTTPS (5986) needs a certificate; Kerberos needs a domain |

The PoC enables both, so either can be dropped later without rebuilding.
RDP is left off: Windows' default, and nothing here needs a desktop
session. The console is reachable over VNC (`virsh vncdisplay`); it shows
the logon screen, since the setup script logs its auto-logon session off.

**Offline labs.** The PoC guest had internet through its NAT network, and
two things depended on it: the OpenSSH Feature on Demand (Windows Update),
and possibly the evaluation's activation. The eval reported `Licensed`; its
behaviour with no internet was not tested. An isolated lab needs the FoD ISO
or Server 2025 (Q6).

### 3.5 Evaluation ISOs and licensing: what may live in a box

Media used (resolved from Microsoft's Evaluation Center links on 2026-10-08):

| | Server 2022 eval (used) | Server 2025 eval (not used) |
|---|---|---|
| Link | `https://go.microsoft.com/fwlink/?linkid=2195280&clcid=0x409&culture=en-us&country=us` | `…linkid=2293312…` |
| Resolves to | `https://software-static.download.prss.microsoft.com/sg/download/888969d5-f34g-4e03-ac9d-1f9786c66749/SERVER_EVAL_x64FRE_en-us.iso` | `…/dbazure/888969d5-…/26100.1742.240906-0331.ge_release_svc_refresh_SERVER_EVAL_x64FRE_en-us.iso` |
| Size | 5,044,094,976 B (4.7 GiB) | 6,014,152,704 B (5.6 GiB) |
| Last-Modified | Wed, 16 Mar 2022 13:16:34 GMT | Thu, 19 Sep 2024 17:43:12 GMT |
| sha256 | `3e4fa6d8507b554856fc9ca6079cc402df11a8b79344871669f0251535255325` | not downloaded |
| Volume ID | `SSS_X64FREE_EN-US_DV9` | |

**Microsoft publishes no checksum** next to the eval downloads, and a search
for this hash found no independent copy. So the pinned value is **trust on
first use**: it proves later downloads match ours, not that ours is
Microsoft's. The fwlink can be repointed silently, which is why the box pins
the resolved URL rather than the fwlink. The basename
`SERVER_EVAL_x64FRE_en-us.iso` is the same across releases, but boxman
caches ISOs as `<iso name>-<sha256(uri)[:8]>.iso` (`images.py`
`_iso_cache_filename`), so a 2025 download cannot collide with it.

What the evaluation terms mean for boxman. This is not legal advice; Q4
asks for an owner.

- The evaluation is for **testing and demonstration, 180 days**, not
  production ([Evaluation Center](https://www.microsoft.com/en-us/evalcenter/);
  the binding text is the licence terms shown at install, which the answer
  file accepts with `AcceptEula`). Accepting a EULA unattended on someone's
  behalf is itself a policy point (Q4). When it expires the guest warns and
  periodically shuts itself down.
- **May go in a box (git):** answer-file templates, setup scripts, the
  download URL, size and checksum. The user's own boxman downloads the ISO
  from Microsoft.
- **Must not go anywhere shared:** the ISO itself, or any disk **installed
  from it**, sysprep'd or not. That means no `boxman image push` of a Windows
  qcow2 to a shared OCI registry, and no import packages on a web server. A
  locally built template on the host that built it is fine; that is the
  Phase 2 design.
- **Rearm:** `slmgr /dlv` reports a rearm count of 6. Do not design around
  rearm. Re-installing from the answer file takes about 3 min, and short-lived
  lab guests should simply be rebuilt.
- **Non-evaluation use** (anything long-lived, or a guest other people
  depend on) needs real licences: Windows Server Standard/Datacenter
  per-core licensing, possibly through volume licensing or KMS. Out of
  scope for boxman; Q4.
- virtio-win and cloudbase-init are open-source and redistributable. They
  are not the constraint.

### 3.6 boxman's clone-identity pass does not fit Windows

Every cloned VM goes through one offline `virt-sysprep` pass
([clone-identity-plan.md](clone-identity-plan.md)): `machine-id`,
`ssh-hostkeys`, `--hostname`, and a POSIX `sh` script run with `--run` to fix
`/etc/hosts` and cloud-init. All of it is Linux-specific. On a Windows clone,
`machine-id` and `ssh-hostkeys` have nothing to act on, `--hostname` is a
Linux customization, and `--run` executes a shell script in the guest.
**Not tested**; the behaviour under `auto` (degrade notice) versus `required`
(fail) needs checking. Windows identity is a different mechanism entirely:
`sysprep /generalize` in the template (new SID on first boot), then the
computer name and password from cloudbase-init or a specialize-pass answer
file. A Windows template therefore needs `clone_machine_id`,
`clone_ssh_host_keys` and `clone_hostname` all **off**. That is a
config-time rule boxman should apply by itself for `os: windows` (§5).

### 3.7 CPU, RAM, disk, nested virt

| | Minimum (Microsoft) | PoC | Suggested lab default |
|---|---|---|---|
| vCPU | 1 (1.4 GHz x64) | 2 | 2 |
| RAM | 512 MB Core / 2 GB Desktop | 4 GiB | 4 GiB Core, 6-8 GiB Desktop |
| Disk | 32 GB | 40 GB thin (6.2 GB used after install) | 40 GB Core, 60 GB Desktop |

On the test host, about 46 GiB of RAM was free alongside the VMs already
running there, so roughly **6 to 8 Windows Core guests** would fit. Disk was
not a constraint. RAM is the limit, and that number moves with whatever else
runs. KSM ([ksm.md](ksm.md)) should merge identical Windows pages well
across guests of one build. Not measured.

**Nested virtualization** is on (`kvm_amd nested=1`), and virt-install gives
the guest `host-passthrough` CPU and Hyper-V enlightenments
(`<hyperv mode='custom'>`, `hypervclock`). A Windows guest needs nested virt
only for Hyper-V inside it: the Hyper-V role, WSL2, Docker Desktop, or
VBS/Credential Guard. Nothing here needs it. Nested Hyper-V on
AMD/KVM is known to be fragile and was not tried.

## 4. Gaps found in boxman (G1 and G3 fixed in this branch, the rest worked around in the PoC)

| # | Gap | Where | Effect | PoC workaround | Proper fix |
|---|---|---|---|---|---|
| G1 | **Extra `cdroms:` reach the guest only after the install has started.** virt-install starts the installer at once (`--wait=0`); additional CD-ROMs are attached afterwards, to the persistent definition only, because SATA/IDE drives cannot be hot-plugged | `manager_parts/vms.py` (configure step, `extra_cdroms`), `cdrom.py` `cdroms_awaiting_boot` | Setup never sees the virtio-win or answer ISO. Any ISO-boot OS that needs a second medium at install time has the same problem | none any more: virtio-win is a `cdroms:` entry (`source:`, used in place). The answer ISO is still a raw `--disk` in `virt_install_extra_args`, because `boxman update` would put a declared one back after its eject (G7) | **fixed in this branch:** `IsoBootVM` passes every `cdroms:` entry to virt-install (`--disk …,device=cdrom` per entry), and the configure step attaches none for ISO-boot VMs |
| G2 | **The VM stays off after the installer's first reboot.** The install-phase domain has `on_reboot=destroy`; with `--wait=0` nothing restarts it into the final definition | `direct_vm.py` (`--wait=0`) | Windows (and any installer that reboots) stops after phase 1. The first `boxman up` then waits 600 s for an IP and **exits 0** with a warning | `'--wait=30'` last in `virt_install_extra_args` (virt-install waits up to 30 min and restarts the guest; `boxman up` blocks for the install phase) | planned, in a follow-up: no install phase. `IsoBootVM` keeps `--cdrom` and adds `--install no_install=yes`, so virt-install writes one definition (boot `hd,cdrom`, a normal reboot) and the installer's reboot boots the installed disk. Unlike a plain CD-ROM `--disk`, this keeps virt-install's OS detection from the ISO (virt-install dry runs: the `--disk` form loses the OS id, and with it Windows' `localtime` clock). Real boots of every CD-boot box before it merges. Also: "VM shut off while waiting for IP" should fail fast, not wait 600 s, and a virt-install that timed out after defining the domain should not be retried (the retries fail with "name already in use" and hide the cause) |
| G3 | **No guest-agent channel.** virt-install adds `org.qemu.guest_agent.0` only for OS variants it knows have an agent (Linux) | `direct_vm.py` | `boxman storage trim`, agent IP lookup, fsinfo and fsfreeze fail ("QEMU guest agent is not configured") | none any more | **fixed in this branch:** `DirectInstallVM` always adds the channel, unless `virt_install_extra_args` already add one (harmless when no agent runs) |
| G4 | **OS detection picks the wrong version.** With osinfo-db 20250606, `--os-variant=detect=on` identifies the 2022 eval ISO as `win2k25` (20250124 gets `win2k22` right), so the result depends on the host | `direct_vm.py` | wrong libosinfo defaults; it happens to be harmless for 2022 vs 2025 | a later `--os-variant=win2k22` in extra args (the last one wins) | an `os_variant:` key for direct-boot VMs, as templates already have |
| G5 | **ssh access assumes a Linux guest.** `ssh_config` writes `User admin`; the `admin_pass` key push is `sshpass ssh-copy-id` (to `~/.ssh/authorized_keys`); the generated inventory has no `ansible_connection` or `ansible_shell_type` | `manager_parts/ssh.py`, `manager_parts/workspace.py` | `boxman ssh` and `boxman run` do not reach the guest | the key is baked into the answer ISO; the user is passed by hand | `os: windows` → `User Administrator` (or `admin_user`), the key pushed into `administrators_authorized_keys` by the guest setup (not ssh-copy-id), inventory vars `ansible_shell_type: powershell` (or the WinRM set) |
| G6 | the post-provision "eject `seed.iso`" step ran over the ISO-boot VM | `manager_parts/flows.py:194` | **none here.** All three CD-ROMs were still attached afterwards; listed only so that a Windows template using a `cidata` seed checks it | n/a | n/a |
| G7 | **The answer ISO stays attached for the VM's whole life**, live and persistent, and it holds the Administrator password in plain text. Any process in the guest can read it from the CD | `direct_vm.py` (media given to virt-install stay in the definition) | password exposure inside the guest. And since the answer ISO is not in `cdroms:`, `boxman update` takes it for a removed drive and detaches it, on every run, until it is ejected | eject it once setup is done (README step: `virsh change-media <domain> <answer.iso path> --eject --force --live --config`); `update` leaves an empty drive alone | install-only media: boxman attaches them at virt-install time, ejects them once the install is done (as it already ejects `seed.iso`), and never compares them in `update`. Phase 2 avoids the password part, because the per-clone secret comes from the cloudbase-init seed |

Two things showed up that are **not** Windows-specific:

- `boxman destroy -y` undefined the VM, but kept its disk and the workspace,
  and left the project registered. Its host-wide "is this file used by
  another domain" scan could not stat a disk that **another user's** VM uses
  in that user's 0700 home directory (EACCES), and boxman keeps everything when it cannot
  tell. That is fail-safe and correct (README, Teardown safety). It did not
  complete here, and likely will not on any shared host where the caller
  cannot read some other VM's disk; that generalisation is for a separate
  issue to confirm (Q8). `deprovision` then finished the unregistration.
- The first `boxman up` reports exit 0 although its only VM was shut off for
  the whole IP wait (G2).

## 5. Design proposal

### 5.1 Today: the PoC box

```yaml
isos:
  winsrv2022-eval:
    uri: https://software-static.download.prss.microsoft.com/.../SERVER_EVAL_x64FRE_en-us.iso
    checksum: sha256:3e4fa6d8…
vms:
  win01:
    boot_order: [cdrom, hd]
    disk_size: 40G
    memory: 4096
    cdroms:
      - name: winsrv2022-eval
      - name: virtio-win              # host RPM, used in place
        source: /usr/share/virtio-win/virtio-win.iso
    virt_install_extra_args:          # G4, G7
      - '--os-variant=win2k22'
      - '--disk=path={{ env("HOME") }}/workspaces/…/answer.iso,device=cdrom,readonly=on'
      - '--wait=30'                   # G2
```

Plus `make-answer-iso.sh` to render the answer file.

### 5.2 Phase 1: first-class unattended install (small, and fixes Linux installers too)

1. **G1**: done in this branch: `IsoBootVM` attaches all `cdroms:` at
   virt-install time.
2. **G2**: no install phase. `--cdrom` with `--install no_install=yes`
   gives one definition, so the installer's reboot boots the installed disk;
   the IP wait fails fast on a VM that is shut off, and a virt-install that
   timed out is not retried.
3. **G3**: done in this branch: the guest-agent channel is always added.
   **G4**: an `os_variant:` key for direct-boot VMs, so a box does not
   depend on the host's osinfo-db. **G7**: install-only media. A medium needed only for
   the install (the answer ISO, virtio-win) is attached at virt-install
   time, ejected by boxman once the install is done, and never compared by
   `boxman update`. A golden image or template built this way carries no
   install media and no password ISO.
4. A new **`unattend:`** block. boxman renders the answer file and builds
   the answer ISO itself, as it already builds NoCloud seeds (with the
   seed-ISO helper, generalised to take a label and a list of files):

   ```yaml
   vms:
     win01:
       os: windows                       # new; default linux
       os_variant: win2k22
       boot_order: [cdrom, hd]
       cdroms:
         - name: winsrv2022-eval
         - name: virtio-win              # local file, used in place
           source: /usr/share/virtio-win/virtio-win.iso
       unattend:
         template: ./autounattend.xml.j2 # Jinja, same env() helpers as conf.yml
         files: [./boxman-setup.ps1]     # copied onto the answer ISO
         # rendered with: admin_pass, admin_user, hostname, the cluster's
         # boxman public key, and the image index
   ```

   boxman would ship a default `autounattend.xml.j2` and `boxman-setup.ps1`
   (this PoC's, parameterised), so a minimal Windows VM is `os: windows`
   plus an ISO. virt-install's own `--unattended` was evaluated and
   rejected: with osinfo-db 20250124 it refuses `win2k22` (no Windows Server
   2022 template), and its Windows templates need a product key and inject
   no virtio drivers.
5. **`os: windows` switches** (G5): ssh user `Administrator` (or
   `admin_user`); the boxman key goes in through the answer ISO, not
   `ssh-copy-id`; inventory gets `ansible_shell_type: powershell` (or the
   WinRM vars when `transport: winrm`); clone identity is forced `off`; no
   `cloudinit:` allowed.

### 5.3 Phase 2: Windows templates (golden image built locally)

1. `templates:` gets an install mode: build the template VM from an ISO with
   the Phase 1 machinery instead of from a cloud image. The answer file's
   last first-logon step installs cloudbase-init and runs
   `sysprep /generalize /oobe /shutdown /unattend:<specialize file>`. The
   template build waits for that shutdown, as it waits for cloud-init today.
2. Clones get a per-clone `cidata` seed (boxman already generates these).
   cloudbase-init's `NoCloudConfigDriveService` applies hostname, password,
   key and `#ps1_sysnative` user-data. Network-config is **v1 only**, or
   omitted for DHCP.
3. Templates are never pushed anywhere while they come from evaluation
   media (§3.5); `boxman image push` could refuse an `os: windows` image
   without an explicit flag.

### 5.4 Phase 3 (only if wanted): Windows 11 / Server 2025 desktop

UEFI (`--boot uefi`), Secure Boot, `swtpm` TPM 2.0 (already installed),
GPT layout in the answer file, and a fix for the press-any-key-to-boot-from-CD
prompt (§3.1). Win11 client licensing on VMs is its own question.

## 6. Open questions

1. **Sizing target.** All host numbers here come from one test host. Which
   hosts would run Windows guests, and how many per host?
2. **What is Windows for?** Ansible role testing against Windows targets?
   AD/domain-controller labs? User desktops? That decides Core vs Desktop,
   2022 vs 2025, and whether Phase 3 matters at all.
3. **`os:` key vs inference.** Is a new per-VM `os: windows` (default
   `linux`) acceptable in the config schema? The alternative, inferring from
   `os_variant`, is implicit.
4. **Licensing owner.** Who decides what is acceptable beyond the 180-day
   evaluation: volume licensing or KMS for Server, and whether any
   Windows image may ever be shared between hosts or users? Until someone
   decides, the rule in §3.5 stands: nothing installed from eval media
   leaves the host that built it.
5. **WinRM in scope?** Proposal: OpenSSH by default; WinRM opt-in, needing
   pywinrm on the controller. Or drop WinRM entirely?
6. **Server 2022 + OpenSSH offline.** The OpenSSH FoD comes from Windows
   Update. Do lab networks always have internet, or should boxman support an
   FoD ISO, or target Server 2025, where OpenSSH is in-box?
7. **G2 would change how every CD-boot box boots** (talos, nixos, guix,
   ubuntu-live, proxmox-nested-ceph-cluster): no install phase, so the
   installer's reboot is a normal reboot rather than a power-off (OS
   detection from the ISO is kept). OK as a general change, or gate it on
   `os: windows`? (G1 and G3 are general already: each
   of those boxes declares a single CD-ROM, so G1 changes none of them, and
   G3 only adds an agent channel where virt-install added none.)
8. **The shared-host teardown issue** (§4): file separately?

## 7. Reproducing

See the box [README](../boxes/windows-server-2022-iso-boot/README.md). In
short: `dnf install virtio-win`; `BOXMAN_WIN_ADMIN_PASS=… ./make-answer-iso.sh`;
`boxman up`; once `boxman-setup: done`, eject the answer ISO (G7); then
`win_ping`.
