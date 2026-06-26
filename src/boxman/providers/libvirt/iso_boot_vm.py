"""Create a VM that boots directly from an ISO (e.g. Talos Linux)."""

import os
from typing import Any

from boxman import log
from boxman.utils.shell import run as _shell_run

from .commands import VirshCommand, VirtInstallCommand


class IsoBootVM:
    """
    Create a libvirt VM with an empty disk and a CDROM ISO attached,
    boot order set to [cdrom, hd].

    Intended for OSes that live-boot and self-install from an ISO
    (e.g. Talos Linux via Omni). The iso_path must already be resolved
    to a local file by the caller.
    """

    def __init__(
        self,
        vm_name: str,
        info: dict[str, Any],
        provider_config: dict[str, Any],
        workdir: str,
        iso_path: str,
    ):
        self.vm_name = vm_name
        self.info = info
        self.provider_config = provider_config
        self.workdir = workdir
        self.iso_path = iso_path
        self.logger = log
        self.virsh = VirshCommand(provider_config)
        self.virt_install = VirtInstallCommand(provider_config=provider_config)

    def create(self) -> bool:
        """Create the ISO-boot VM."""
        disk_path = os.path.join(self.workdir, f"{self.vm_name}.qcow2")
        disk_size = self._get_disk_size_gb()
        memory = self.info.get("memory", 2048)
        vcpus = self.info.get("vcpus", 2)
        network = self._get_network()

        qemu_img_cmd = f'qemu-img create -f qcow2 "{disk_path}" {disk_size}G'
        qemu_img_cmd = self.virsh._wrap_for_runtime(qemu_img_cmd)
        result = _shell_run(qemu_img_cmd, hide=True, warn=True)
        if not result.ok:
            self.logger.error(f"qemu-img create failed: {result.stderr}")
            return False

        parts = []
        if self.virt_install.use_sudo:
            parts.append("sudo")
        parts.append(self.virt_install.command_path)
        parts.append(f"--connect={self.virt_install.uri}")
        parts.append(f"--name={self.vm_name}")
        parts.append(f"--memory={memory}")
        parts.append(f"--vcpus={vcpus}")
        parts.append(f"--disk=path={disk_path},format=qcow2,bus=virtio,discard=unmap")
        parts.append(f"--network=network={network},model=virtio")
        parts.append(f"--cdrom={self.iso_path}")
        parts.append("--boot=cdrom,hd")
        parts.append("--os-variant=detect=on,require=off")
        parts.append("--graphics=vnc")
        parts.append("--noautoconsole")
        parts.append("--wait=0")

        cmd = " ".join(parts)
        cmd = self.virt_install._wrap_for_runtime(cmd)
        self.logger.info(f"creating ISO-boot VM '{self.vm_name}': {cmd}")
        result = _shell_run(cmd, hide=True, warn=True)
        if not result.ok:
            self.logger.error(f"virt-install failed: {result.stderr}")
            return False

        self.logger.info(f"ISO-boot VM '{self.vm_name}' created")
        return True

    def _get_disk_size_gb(self) -> int:
        disks = self.info.get("disks", [{}])
        return disks[0].get("size", 20) if disks else 20

    def _get_network(self) -> str:
        networks = self.info.get("networks", [{}])
        return networks[0].get("name", "default") if networks else "default"
