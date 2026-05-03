"""Create a bare VM (empty disk) for PXE network boot."""

import os
from typing import Any

from boxman import log
from boxman.utils.shell import run as _shell_run

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

        # Create empty disk image
        qemu_img_cmd = f'qemu-img create -f qcow2 "{disk_path}" {disk_size}G'
        qemu_img_cmd = self.virsh._wrap_for_runtime(qemu_img_cmd)
        result = _shell_run(qemu_img_cmd, hide=True, warn=True)
        if not result.ok:
            self.logger.error(f"qemu-img create failed: {result.stderr}")
            return False

        # Build virt-install command for PXE boot (same pattern as cloudinit.py)
        parts = []
        if self.virt_install.use_sudo:
            parts.append("sudo")
        parts.append(self.virt_install.command_path)
        parts.append(f"--connect={self.virt_install.uri}")
        parts.append(f"--name={self.vm_name}")
        parts.append(f"--memory={memory}")
        parts.append(f"--vcpus={vcpus}")
        parts.append(f"--disk=path={disk_path},format=qcow2,bus=virtio")
        parts.append(f"--network=network={network},model=virtio")
        parts.append("--boot=network,hd")
        parts.append("--os-variant=detect=on,require=off")
        parts.append("--graphics=vnc")
        parts.append("--noautoconsole")
        parts.append("--wait=0")

        cmd = " ".join(parts)
        cmd = self.virt_install._wrap_for_runtime(cmd)
        self.logger.info(f"creating bare PXE VM: {cmd}")
        result = _shell_run(cmd, hide=True, warn=True)
        if not result.ok:
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
