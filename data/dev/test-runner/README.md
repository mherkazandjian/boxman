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
- Two host-kernel quirks are handled via cloud-init (see `conf.yml`
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

## Validate Docker SSH without the legacy AppArmor override

A runner provisioned before issue #84 was fixed may still have the old
`unix_chkpwd` local include from cloud-init. Temporarily empty it and reload
the enforcing profile before rebuilding the image, otherwise the stale
capability grants can hide a regression:

```bash
# Save and disable the old local include in the existing disposable runner.
ssh -F ~/workspaces/boxmandev/test-runner/ssh_config cluster_1_runner01 \
  'sudo cp -a /etc/apparmor.d/local/unix-chkpwd /tmp/unix-chkpwd.boxman-84 && \
   sudo truncate -s 0 /etc/apparmor.d/local/unix-chkpwd && \
   sudo apparmor_parser -r /etc/apparmor.d/unix-chkpwd'

make test-vm-sync
ssh -F ~/workspaces/boxmandev/test-runner/ssh_config cluster_1_runner01 \
  'cd ~/boxman/containers/docker && docker compose down && \
   docker compose build --no-cache'
make test-vm-test tier=integration pytest_args="-k test_ssh_into_container"

# Restore the runner's previous local include after the validation.
ssh -F ~/workspaces/boxmandev/test-runner/ssh_config cluster_1_runner01 \
  'sudo cp -a /tmp/unix-chkpwd.boxman-84 /etc/apparmor.d/local/unix-chkpwd && \
   sudo apparmor_parser -r /etc/apparmor.d/unix-chkpwd'
```

Freshly provisioned runners do not install that include and need no special
handling.
