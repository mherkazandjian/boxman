# PXE Boot Support for boxman — Implementation Plan

## Background

The `hpccluster` project uses a `cobbler_pxe` Ansible role to set up PXE
provisioning for HPC cluster nodes. Testing PXE boot end-to-end requires
a VM management tool that can:

1. Create a "bare" VM (empty disk, no OS) that boots from the network
2. Set a VM's boot order to network-first and restore it afterwards
3. Poll for SSH availability after an OS is installed via PXE/kickstart

Currently, `boxman` creates all VMs from pre-built cloud images via
`virt-install --import`. This document describes the concrete changes
required to add PXE boot support.

---

## Dependencies on hpccluster

- `ansible/roles/cobbler_pxe` must be deployed and reachable
- A Cobbler profile and system entry for the test VM must exist
- The libvirt network used for PXE must be the same network that Cobbler's DHCP/TFTP serves on

---

## Phase A — `boot_order` field in VM spec

### Files to change

**`data/conf.yml` (example/documentation)**
Add `boot_order` to a VM definition:
```yaml
vms:
  pxe-test01:
    boot_order: [network, hd]   # NEW: boot from network first, then disk
    memory: 2048
    vcpus: 2
    disks:
      - name: disk01
        size: 20
    networks:
      - name: mgmt
```

**No Python schema change required** — boxman uses duck-typed YAML; adding the
field to conf.yml is sufficient. The consumer code (Phase B) reads it directly.

---

## Phase B — virt-install PXE mode + `BareVM` class

### New file: `src/boxman/providers/libvirt/bare_vm.py`

```python
"""Create a bare VM (empty disk) for PXE network boot."""

import os
import subprocess
from typing import Any

from boxman import log
from .commands import VirshCommand, VirtInstallCommand


class BareVM:
    """
    Create a libvirt VM with an empty disk and boot order set to
    [network, hd].  The VM has no OS; it is intended to boot via PXE
    and have an OS installed by a provisioning server (e.g. Cobbler).
    """

    def __init__(self,
                 vm_name: str,
                 info: dict[str, Any],
                 provider_config: dict[str, Any],
                 workdir: str):
        self.vm_name = vm_name
        self.info = info
        self.provider_config = provider_config
        self.workdir = workdir
        self.logger = log
        self.virsh = VirshCommand(provider_config)
        self.virt_install = VirtInstallCommand(provider_config=provider_config)

    def create(self) -> bool:
        """Create the bare VM."""
        disk_path = os.path.join(self.workdir, f'{self.vm_name}.qcow2')
        disk_size = self._get_disk_size_gb()
        memory = self.info.get('memory', 2048)
        vcpus = self.info.get('vcpus', 2)
        network = self._get_network()

        # Create empty disk
        result = subprocess.run(
            ['qemu-img', 'create', '-f', 'qcow2', disk_path, f'{disk_size}G'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            self.logger.error(f"qemu-img create failed: {result.stderr}")
            return False

        # Build virt-install command for PXE boot
        parts = []
        if self.virt_install.use_sudo:
            parts.append('sudo')
        parts += [
            self.virt_install.command_path,
            f'--connect={self.virt_install.uri}',
            f'--name={self.vm_name}',
            f'--memory={memory}',
            f'--vcpus={vcpus}',
            f'--disk=path={disk_path},format=qcow2,bus=virtio',
            f'--network=network={network},model=virtio',
            '--boot=network,hd',
            '--os-variant=detect=on,require=off',
            '--graphics=vnc',
            '--noautoconsole',
            '--wait=0',
        ]
        cmd = ' '.join(parts)
        self.logger.info(f"creating bare PXE VM: {cmd}")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            self.logger.error(f"virt-install failed: {result.stderr}")
            return False

        self.logger.info(f"bare VM '{self.vm_name}' created for PXE boot")
        return True

    def _get_disk_size_gb(self) -> int:
        disks = self.info.get('disks', [{}])
        return disks[0].get('size', 20) if disks else 20

    def _get_network(self) -> str:
        networks = self.info.get('networks', [{}])
        return networks[0].get('name', 'default') if networks else 'default'
```

### Changes to `src/boxman/providers/libvirt/cloudinit.py`

In the `CloudInit` class (or wherever VM creation is orchestrated), detect
`boot_order[0] == 'network'` and delegate to `BareVM` instead:

```python
boot_order = vm_info.get('boot_order', ['hd'])
if boot_order[0] == 'network':
    from .bare_vm import BareVM
    bare = BareVM(vm_name, vm_info, provider_config, workdir)
    return bare.create()
# ... existing cloud-init path ...
```

---

## Phase C — Boot order set/restore in `session.py`

### Changes to `src/boxman/providers/libvirt/session.py`

Add two new methods:

```python
def set_boot_order(self, vm_name: str, order: list[str]) -> bool:
    """
    Set the boot device order for a VM.

    Args:
        vm_name: Name of the VM
        order: List of boot devices in priority order, e.g. ['network', 'hd']

    Returns:
        True if successful
    """
    editor = VirshEdit(self.provider_config)
    xml_content = editor.get_domain_xml(vm_name, inactive=True)

    from lxml import etree
    tree = etree.fromstring(xml_content.encode('utf-8'))
    os_elem = tree.find('os')
    if os_elem is None:
        os_elem = etree.SubElement(tree, 'os')
    for b in os_elem.findall('boot'):
        os_elem.remove(b)
    for dev in order:
        boot = etree.SubElement(os_elem, 'boot')
        boot.set('dev', dev)

    modified_xml = etree.tostring(tree).decode('utf-8')
    return editor.redefine_domain(vm_name, modified_xml)

def restore_boot_order(self, vm_name: str) -> bool:
    """Restore boot order to ['hd'] (local disk only)."""
    return self.set_boot_order(vm_name, ['hd'])
```

---

## Phase D — SSH polling in `session.py`

### Changes to `src/boxman/providers/libvirt/session.py`

Add:

```python
def wait_for_ssh(self,
                 ip: str,
                 port: int = 22,
                 timeout: int = 600,
                 interval: int = 10) -> bool:
    """
    Poll for SSH availability on a host.

    Args:
        ip: IP address to connect to
        port: SSH port (default: 22)
        timeout: Maximum seconds to wait
        interval: Seconds between retries

    Returns:
        True if SSH becomes available before timeout
    """
    import socket, time
    elapsed = 0
    while elapsed < timeout:
        try:
            with socket.create_connection((ip, port), timeout=5):
                self.logger.info(f"SSH available on {ip}:{port} after {elapsed}s")
                return True
        except (OSError, ConnectionRefusedError):
            self.logger.info(f"SSH not yet available on {ip}, retrying in {interval}s...")
            time.sleep(interval)
            elapsed += interval
    self.logger.error(f"SSH timeout after {timeout}s on {ip}:{port}")
    return False
```

---

## Phase E — `pxe-boot` CLI subcommand

### Changes to `src/boxman/scripts/app.py`

Add a `pxe-boot` subparser that orchestrates:
1. Create or configure VM for net-boot (BareVM or set_boot_order)
2. Start the VM
3. Wait for SSH
4. Report success/failure

```python
pxe_parser = subparsers.add_parser('pxe-boot',
    help='Create and test PXE boot of a VM via Cobbler')
pxe_parser.add_argument('--vm', required=True, help='VM name')
pxe_parser.add_argument('--profile', default='', help='Cobbler profile name')
pxe_parser.add_argument('--network', default='default', help='libvirt network name')
pxe_parser.add_argument('--expected-ip', help='IP to poll for SSH after boot')
pxe_parser.add_argument('--wait-timeout', type=int, default=600,
                         help='SSH poll timeout in seconds')
pxe_parser.add_argument('--restore-after', action='store_true',
                         help='Restore boot order to hd after provisioning')
```

Handler function:
```python
def cmd_pxe_boot(args, manager):
    session = manager.provider
    session.set_boot_order(args.vm, ['network', 'hd'])
    session.start_vm(args.vm)
    if args.expected_ip:
        ok = session.wait_for_ssh(args.expected_ip, timeout=args.wait_timeout)
        if ok and args.restore_after:
            session.restore_boot_order(args.vm)
        return ok
    return True
```

---

## Test coverage to add

- `tests/test_bare_vm.py` — unit test for `BareVM.create()` (mock virt-install)
- `tests/test_pxe_session.py` — unit tests for `set_boot_order`, `restore_boot_order`, `wait_for_ssh`
- Integration test (optional): provision a VM via PXE in a CI libvirt environment

---

## Summary of files to create/modify

| Action | File |
|---|---|
| CREATE | `src/boxman/providers/libvirt/bare_vm.py` |
| MODIFY | `src/boxman/providers/libvirt/cloudinit.py` |
| MODIFY | `src/boxman/providers/libvirt/session.py` |
| MODIFY | `src/boxman/scripts/app.py` |
| CREATE | `tests/test_bare_vm.py` |
| CREATE | `tests/test_pxe_session.py` |
| MODIFY | `data/conf.yml` (add `boot_order` example) |
