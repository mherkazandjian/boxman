# test-runner — disposable boxman test VM

All boxman test tiers (unit, smoke, integration) run inside this VM so test
side effects — docker networks, libvirt domains, downloaded cloud images —
never touch the host.

- 2 vCPU / 16 GB RAM / 60 GB system disk + 20 GB data disk
- Ubuntu 24.04 with `docker.io`, `python3-venv`, `git`, `rsync`
- NAT network on `192.168.15.0/24` (chosen to not collide with the other
  projects on the dev host — check `virsh net-dumpxml` before changing it)
- Integration tier needs `/dev/kvm` *inside* the VM, i.e. nested
  virtualization on the host (`cat /sys/module/kvm_intel/parameters/nested`
  → `Y`). boxman already emits `cpu mode='host-passthrough'` in the domain
  XML, so no post-provisioning CPU change is needed; `make test-vm-up`
  verifies `/dev/kvm` is visible inside the guest.
- Ubuntu 24.04's enforcing AppArmor profile for `unix_chkpwd` breaks sshd
  inside the privileged libvirt test container (the Rocky image ships
  `/etc/shadow` mode 0000, and the profile drops the DAC capabilities the
  helper needs to read it). cloud-init installs an override in
  `/etc/apparmor.d/local/unix-chkpwd` granting `dac_override` +
  `dac_read_search`; without it every SSH login into the container fails
  with "Access denied by PAM account configuration". See issue #84.
- Two more host-kernel quirks are handled via cloud-init (see `conf.yml`
  comments): the libvirtd AppArmor profile only allows the Debian
  `libvirt_iohelper` path (Rocky uses `/usr/libexec/...`), which breaks
  `snapshot-create-as`; and `vm.overcommit_memory=1` is needed because a
  box with a `max_memory` ceiling makes qemu pre-size its memory backend
  at the maximum (16 GB), which a 16 GB VM cannot mmap with the default
  heuristic.

## Usage (from the repo root)

```bash
make test-vm-up        # provision (one-time; downloads + builds the template)
make test-vm-sync      # rsync the repo into the VM, (re)install the venv
make test-vm-test      # default tiers (unit + smoke)
make test-vm-test tier=integration     # Docker + nested-KVM integration tier
make test-vm-test pytest_args="-k test_name"   # select specific tests
make test-vm-destroy   # full teardown (VM, network, workspace)
```

After changing code on the host, re-run `make test-vm-sync` before
`make test-vm-test` — the VM has its own copy of the tree.
