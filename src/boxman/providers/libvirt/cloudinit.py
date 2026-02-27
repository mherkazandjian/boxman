"""
Cloud-init support for libvirt provider.

Creates template VMs from cloud images by:
1. Building a NoCloud seed ISO (user-data + meta-data)
2. Copying the base cloud image
3. Running virt-install --import with both disks
"""

import os
import re
import shutil
import tempfile
import time
import json
import base64
import crypt
import secrets
import urllib.request
import urllib.error
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import invoke

from boxman import log
from .commands import VirshCommand, VirtInstallCommand


DEFAULT_META_DATA = """\
instance-id: {instance_id}
local-hostname: {hostname}
"""

DEFAULT_USER_DATA = """\
#cloud-config
hostname: {hostname}
manage_etc_hosts: true

ssh_pwauth: true
chpasswd:
  expire: false
  users:
    - name: ubuntu
      password: ubuntu
      type: text

package_update: false

write_files:
  - path: /etc/boxman-template-marker
    permissions: "0644"
    content: |
      created by boxman cloud-init template provisioning
  - path: /etc/systemd/system/systemd-networkd-wait-online.service.d/override.conf
    permissions: "0644"
    content: |
      [Service]
      ExecStart=
      ExecStart=/usr/lib/systemd/systemd-networkd-wait-online --any

# Remove any file that disables cloud-init networking, then bring up interfaces
runcmd:
  - rm -f /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg
  - rm -f /etc/cloud/cloud.cfg.d/subiquity-disable-cloudinit-networking.cfg
  - systemctl daemon-reload
  - netplan generate || true
  - netplan apply || true
  - dhclient -v || true
  - [ sh, -c, "echo template created at $(date -Is) >> /var/log/boxman-cloudinit.log" ]
"""

DEFAULT_NETWORK_CONFIG = """\
version: 2
ethernets:
  all-en:
    match:
      name: "en*"
    dhcp4: true
  all-eth:
    match:
      name: "eth*"
    dhcp4: true
"""


class CloudInitTemplate:
    """
    Create a libvirt VM template from a cloud image + cloud-init config.
    """

    def __init__(
        self,
        template_name: str,
        image_path: str,
        cloudinit_userdata: Optional[str] = None,
        cloudinit_metadata: Optional[str] = None,
        cloudinit_network_config: Optional[str] = None,
        workdir: Optional[str] = None,
        provider_config: Optional[Dict[str, Any]] = None,
        memory: int = 2048,
        vcpus: int = 2,
        os_variant: str = "generic",
        disk_format: str = "qcow2",
        disk_size: Optional[str] = None,
        network: str = "default",
        bridge: Optional[str] = None,
    ):
        self.template_name = template_name
        self.image_path = self._resolve_image_path(image_path)
        self.cloudinit_userdata = cloudinit_userdata
        self.cloudinit_metadata = cloudinit_metadata
        self.cloudinit_network_config = cloudinit_network_config
        self.workdir = os.path.expanduser(workdir) if workdir else tempfile.mkdtemp(prefix="boxman-cloudinit-")
        self.provider_config = provider_config or {}
        self.memory = memory
        self.vcpus = vcpus
        self.os_variant = os_variant
        self.disk_format = disk_format
        self.disk_size = disk_size
        self.network = network
        self.bridge = bridge
        self.logger = log

        self.logger.debug(f"CloudInitTemplate provider_config: {provider_config}")

        self.virsh = VirshCommand(provider_config=provider_config)
        self.virt_install = VirtInstallCommand(provider_config=provider_config)
        self.logger.info(f"using virt-install command: {self.virt_install.command_path}")
        self.logger.info(f"using virsh command: {self.virsh.command_path}")

    @staticmethod
    def _resolve_image_path(image_path: str) -> str:
        if image_path.startswith("file://"):
            image_path = image_path[len("file://"):]
        if image_path.startswith(("http://", "https://")):
            return image_path
        return os.path.expanduser(image_path)

    def _check_vm_exists(self) -> bool:
        result = self.virsh.execute("list", "--all", "--name", hide=True, warn=True)
        if result.ok:
            names = [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]
            return self.template_name in names
        return False

    def build_seed_iso(self, nocloud_dir: str, seed_iso_path: str) -> bool:
        self.logger.info(f"building cloud-init seed ISO: {seed_iso_path}")

        network_config_path = os.path.join(nocloud_dir, "network-config")
        network_flag = ""
        if os.path.exists(network_config_path):
            network_flag = f' --network-config="{network_config_path}"'

        result = self.virsh.execute_shell(
            f'cloud-localds{network_flag} "{seed_iso_path}" "{nocloud_dir}/user-data" "{nocloud_dir}/meta-data"',
            hide=False, warn=True,
        )
        if result.ok:
            self.logger.info("seed ISO created with cloud-localds")
            return True

        # Fallback: include network-config in the ISO if present
        extra_files = ""
        if os.path.exists(network_config_path):
            extra_files = f' "{network_config_path}"'

        for tool in ("genisoimage", "mkisofs", "xorrisofs"):
            result = self.virsh.execute_shell(
                f'{tool} -output "{seed_iso_path}" -volid cidata -joliet -rock '
                f'"{nocloud_dir}/user-data" "{nocloud_dir}/meta-data"{extra_files}',
                hide=False, warn=True,
            )
            if result.ok:
                self.logger.info(f"seed ISO created with {tool}")
                return True

        self.logger.error(
            "failed to create seed ISO. Install one of: "
            "cloud-image-utils, genisoimage, mkisofs, or xorrisofs"
        )
        return False

    def _resolve_bridge(self) -> Optional[str]:
        """
        Resolve the bridge device name to use for the VM network.

        Priority:
          1. Explicit ``bridge`` parameter (e.g. 'virbr0')
          2. Look up the bridge device from the libvirt network name

        Returns:
            The bridge device name (e.g. 'virbr0') or None if unresolvable.
        """
        if self.bridge:
            self.logger.info(f"using explicit bridge device: {self.bridge}")
            return self.bridge

        # Try to discover the bridge from the libvirt network
        net_name = self.network
        self.logger.info(
            f"resolving bridge device from libvirt network '{net_name}'...")

        # Ensure the network is active first
        self._ensure_network_active()

        # Use virsh net-info to get the bridge name
        result = self.virsh.execute(
            "net-info", net_name, hide=True, warn=True)
        if result.ok:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.lower().startswith("bridge:"):
                    bridge_name = line.split(":", 1)[1].strip()
                    if bridge_name:
                        self.logger.info(
                            f"resolved bridge '{bridge_name}' from "
                            f"network '{net_name}'")
                        return bridge_name

        self.logger.warning(
            f"could not resolve bridge from network '{net_name}', "
            f"falling back to network-based connection")
        return None

    def _ensure_network_active(self) -> bool:
        """
        Ensure the libvirt network used by this template is active.

        Returns:
            True if the network is active, False otherwise.
        """
        net_name = self.network
        self.logger.info(f"checking if libvirt network '{net_name}' is active...")

        result = self.virsh.execute(
            "net-list", "--all", "--name", hide=True, warn=True)
        if not result.ok:
            self.logger.warning("could not list libvirt networks")
            return False

        existing = [n.strip() for n in result.stdout.strip().split("\n")
                    if n.strip()]
        if net_name not in existing:
            self.logger.error(
                f"libvirt network '{net_name}' does not exist. "
                f"Create it first or specify a bridge device directly.")
            return False

        # Check if active
        result = self.virsh.execute(
            "net-list", "--name", hide=True, warn=True)
        if result.ok:
            active = [n.strip() for n in result.stdout.strip().split("\n")
                      if n.strip()]
            if net_name in active:
                self.logger.info(f"libvirt network '{net_name}' is active")
                return True

        # Start it
        self.logger.info(
            f"libvirt network '{net_name}' is inactive, starting...")
        result = self.virsh.execute("net-start", net_name, warn=True)
        if result.ok:
            self.logger.info(f"libvirt network '{net_name}' started")
            return True

        self.logger.error(
            f"failed to start network '{net_name}': {result.stderr}")
        return False

    def prepare_nocloud_dir(self, base_dir: str) -> str:
        nocloud_dir = os.path.join(base_dir, "nocloud")
        os.makedirs(nocloud_dir, exist_ok=True)

        userdata = self.cloudinit_userdata
        if not userdata:
            userdata = DEFAULT_USER_DATA.format(hostname=self.template_name)

        # Replace ${env:VAR} placeholders with actual environment variables
        # (run first so ${hash:${env:VAR}} resolves the env var before hashing)
        userdata = re.sub(
            r'\$\{env:([A-Za-z0-9_]+)\}',
            lambda m: os.environ.get(m.group(1), ''),
            userdata
        )

        # Replace ${hash:plaintext} placeholders with SHA-512 hashed passwords
        def _hash_repl(m):
            hashed = self.hash_password(m.group(1))
            self.logger.info(f"hashed password for cloud-init user (placeholder replaced)")
            # Wrap in single quotes so YAML doesn't interpret $ in the hash
            return f"'{hashed}'"

        userdata = re.sub(
            r'\$\{hash:([^}]+)\}',
            _hash_repl,
            userdata
        )

        userdata_path = os.path.join(nocloud_dir, "user-data")
        with open(userdata_path, "w") as fobj:
            fobj.write(userdata)
        self.logger.info(f"wrote user-data to {userdata_path}")
        self.logger.debug(f"user-data content:\n{userdata}")

        metadata = self.cloudinit_metadata
        if not metadata:
            metadata = DEFAULT_META_DATA.format(
                instance_id=f"{self.template_name}-001",
                hostname=self.template_name,
            )
        metadata_path = os.path.join(nocloud_dir, "meta-data")
        with open(metadata_path, "w") as fobj:
            fobj.write(metadata)
        self.logger.info(f"wrote meta-data to {metadata_path}")

        # Use custom network config if provided, otherwise use default DHCP config
        network_config = self.cloudinit_network_config
        if not network_config:
            network_config = DEFAULT_NETWORK_CONFIG
        network_config_path = os.path.join(nocloud_dir, "network-config")
        with open(network_config_path, "w") as fobj:
            fobj.write(network_config)
        self.logger.info(f"wrote network-config to {network_config_path}")

        return nocloud_dir

    def copy_base_image(self, dst_path: str) -> bool:
        # Check if it's a URL and needs downloading
        if self.image_path.startswith(("http://", "https://")):
            return self._download_image(self.image_path, dst_path)

        # Local file copy
        if not self.image_path:
            self.logger.error(
                "base cloud image path is empty. "
                "Check that 'image:' (not 'file:') is set in the template config.")
            return False

        if not os.path.exists(self.image_path):
            self.logger.error(
                f"base cloud image not found: '{self.image_path}' "
                f"(resolved from template config 'image' field)")
            return False

        self.logger.info(f"copying base image {self.image_path} -> {dst_path}")

        result = self.virsh.execute_shell(
            f'rsync --sparse --progress "{self.image_path}" "{dst_path}"',
            hide=False, warn=True,
        )
        if result.ok:
            self.logger.info("base image copied (sparse-aware via rsync)")
            return True

        try:
            shutil.copy2(self.image_path, dst_path)
            self.logger.info("base image copied via shutil.copy2")
            return True
        except Exception as exc:
            self.logger.error(f"failed to copy base image: {exc}")
            return False

    def _resize_disk_image(self, image_path: str, size: str) -> bool:
        """
        Resize a qcow2 disk image to the given size using qemu-img.

        Args:
            image_path: Path to the disk image.
            size: Target size (e.g. '20G', '50G'). If the value is smaller
                  than the current image size, this is a no-op (shrinking
                  is not supported by qemu-img resize without --shrink).

        Returns:
            True if successful, False otherwise.
        """
        self.logger.info(f"resizing disk image {image_path} to {size}")
        result = self.virsh.execute_shell(
            f'qemu-img resize "{image_path}" {size}',
            hide=False, warn=True,
        )
        if result.ok:
            self.logger.info(f"disk image resized to {size}")
            return True

        self.logger.error(f"failed to resize disk image: {result.stderr}")
        return False

    def _download_image(self, url: str, dst_path: str) -> bool:
        """Download a cloud image from a URL with progress and fallbacks."""
        self.logger.info(f"downloading base image {url} -> {dst_path}")

        # Try wget first (handles redirects, proxies, SSL better)
        result = invoke.run(
            f'wget --progress=dot:mega -O "{dst_path}" "{url}"',
            hide=False, warn=True,
        )
        if result.ok and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
            self.logger.info("download complete (wget)")
            return True

        # Try curl as second fallback
        result = invoke.run(
            f'curl -L --progress-bar -o "{dst_path}" "{url}"',
            hide=False, warn=True,
        )
        if result.ok and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
            self.logger.info("download complete (curl)")
            return True

        # Last resort: urllib with timeout
        try:
            self.logger.info("falling back to urllib download (timeout=120s)...")
            req = urllib.request.Request(url, headers={"User-Agent": "boxman/1.0"})
            with urllib.request.urlopen(req, timeout=120) as response:
                total = int(response.headers.get("Content-Length", 0))
                downloaded = 0
                with open(dst_path, "wb") as out_file:
                    while True:
                        chunk = response.read(1024 * 1024)  # 1 MB chunks
                        if not chunk:
                            break
                        out_file.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded * 100 // total
                            self.logger.info(
                                f"  downloaded {downloaded // (1024*1024)} MB "
                                f"/ {total // (1024*1024)} MB ({pct}%)")
            self.logger.info("download complete (urllib)")
            return True
        except Exception as e:
            self.logger.error(f"failed to download image: {e}")
            # Clean up partial download
            if os.path.exists(dst_path):
                os.remove(dst_path)
            return False

    def verify_and_shutdown(self) -> bool:
        self.logger.info("verifying VM health: waiting for QEMU guest agent (this may take a few minutes while cloud-init installs it)...")
        agent_up = False

        # Wait up to 300 seconds (5 minutes) for the guest agent
        for i in range(150):
            result = self.virsh.execute_shell(
                f"virsh qemu-agent-command {self.template_name} '{{\"execute\":\"guest-ping\"}}'",
                hide=True, warn=True)
            if result.ok:
                agent_up = True
                break

            if i > 0 and i % 15 == 0:
                self.logger.info(f"still waiting for QEMU guest agent... ({i * 2}s elapsed)")

            time.sleep(2)

        if not agent_up:
            self.logger.warning("QEMU guest agent did not respond in time. OS might not be healthy or agent is not installed.")
        else:
            self.logger.info("QEMU guest agent is responding. OS is healthy.")
            self.logger.info("fetching cloud-init logs via guest agent (waiting for cloud-init to finish)...")

            exec_cmd = '{"execute":"guest-exec","arguments":{"path":"/bin/sh","arg":["-c","cloud-init status --wait && cat /var/log/cloud-init-output.log"],"capture-output":true}}'

            res = self.virsh.execute_shell(
                f"virsh qemu-agent-command {self.template_name} '{exec_cmd}'",
                hide=True, warn=True)
            if res.ok:
                try:
                    pid_info = json.loads(res.stdout.strip())
                    pid = pid_info.get("return", {}).get("pid")

                    if pid:
                        status_cmd = f'{{"execute":"guest-exec-status","arguments":{{"pid":{pid}}}}}'

                        # Poll for completion (up to 5 minutes)
                        for _ in range(150):
                            status_res = self.virsh.execute_shell(
                                f"virsh qemu-agent-command {self.template_name} '{status_cmd}'",
                                hide=True, warn=True)
                            if status_res.ok:
                                status_info = json.loads(status_res.stdout.strip())
                                ret = status_info.get("return", {})
                                if ret.get("exited"):
                                    out_data = ret.get("out-data", "")
                                    if out_data:
                                        decoded_out = base64.b64decode(out_data).decode('utf-8', errors='replace')
                                        self.logger.info("=== Cloud-Init Output Log ===")
                                        for line in decoded_out.splitlines():
                                            self.logger.info(f"  {line}")
                                        self.logger.info("=============================")

                                    err_data = ret.get("err-data", "")
                                    if err_data:
                                        decoded_err = base64.b64decode(err_data).decode('utf-8', errors='replace')
                                        if decoded_err.strip():
                                            self.logger.debug(f"Guest exec stderr: {decoded_err}")
                                    break
                            time.sleep(2)
                except Exception as e:
                    self.logger.warning(f"failed to parse guest-exec output: {e}")
            else:
                self.logger.warning("failed to execute command via guest agent.")

        self.logger.info("shutting down the template VM...")
        self.virsh.execute("shutdown", self.template_name, hide=True, warn=True)

        self.logger.info("waiting for VM to shut off...")
        for _ in range(30):
            result = self.virsh.execute("domstate", self.template_name, hide=True, warn=True)
            if result.ok and "shut off" in result.stdout.strip():
                self.logger.info("VM is successfully shut off.")
                return True
            time.sleep(2)

        self.logger.warning("VM did not shut off gracefully. Forcing destroy...")
        self.virsh.execute("destroy", self.template_name, hide=True, warn=True)
        return True

    def _verify_dhcp_on_network(self) -> bool:
        """
        Verify that DHCP is enabled on the libvirt network backing this template.

        Parses the network XML and checks for a <dhcp> element with a <range>.

        Returns:
            True if DHCP is configured, False otherwise.
        """
        net_name = self.network
        result = self.virsh.execute(
            "net-dumpxml", net_name, hide=True, warn=True)
        if not result.ok:
            self.logger.warning(
                f"could not dump XML for network '{net_name}'")
            return False

        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(result.stdout)
            dhcp_elem = root.find(".//dhcp/range")
            if dhcp_elem is not None:
                start = dhcp_elem.get("start", "?")
                end = dhcp_elem.get("end", "?")
                self.logger.info(
                    f"DHCP is enabled on network '{net_name}' "
                    f"(range {start} - {end})")
                return True
            else:
                self.logger.error(
                    f"DHCP is NOT configured on network '{net_name}'. "
                    f"The guest will not get an IP address. "
                    f"Add a <dhcp><range .../></dhcp> block to the network, "
                    f"or use a static IP in cloudinit_network_config.")
                return False
        except ET.ParseError as exc:
            self.logger.warning(f"failed to parse network XML: {exc}")
            return False

    def create_template(self, force: bool = False) -> bool:
        self.logger.info("=" * 70)
        self.logger.info(f"creating cloud-init template: {self.template_name}")
        self.logger.info("=" * 70)

        if self._check_vm_exists():
            if not force:
                self.logger.error(
                    f"template VM '{self.template_name}' already exists. "
                    f"Use --force to delete and recreate it."
                )
                return False
            else:
                self.logger.warning(
                    f"template VM '{self.template_name}' exists, "
                    f"destroying first (--force was specified)")
                self.virsh.execute(
                    "destroy", self.template_name, hide=True, warn=True)
                self.virsh.execute(
                    "undefine", self.template_name,
                    "--remove-all-storage", hide=True, warn=True)
                self.logger.info(
                    f"template VM '{self.template_name}' has been removed")

        # Resolve bridge device (auto-starts the network if needed)
        bridge_device = self._resolve_bridge()

        # Verify DHCP is available (warn early rather than debug a silent failure)
        if not self.bridge:
            # only check when using a libvirt-managed network
            self._verify_dhcp_on_network()

        template_dir = os.path.join(self.workdir, self.template_name)
        os.makedirs(template_dir, exist_ok=True)

        image_ext = os.path.splitext(self.image_path)[1] or f".{self.disk_format}"
        dst_image_path = os.path.join(template_dir, f"{self.template_name}{image_ext}")
        if not self.copy_base_image(dst_image_path):
            return False

        # Resize the disk image if a target size was specified
        if self.disk_size:
            if not self._resize_disk_image(dst_image_path, self.disk_size):
                return False

        nocloud_dir = self.prepare_nocloud_dir(template_dir)

        seed_iso_path = os.path.join(template_dir, "seed.iso")
        if not self.build_seed_iso(nocloud_dir, seed_iso_path):
            return False

        self.logger.info("running virt-install to create template VM...")

        try:
            # Build the command using VirtInstallCommand which handles
            # command_path, sudo, URI, and runtime wrapping.
            # We use build_command + _wrap_for_runtime manually because
            # virt-install needs two --disk flags (not supported by kwargs).
            parts = []
            if self.virt_install.use_sudo:
                parts.append("sudo")
            parts.append(self.virt_install.command_path)
            parts.append(f"--connect={self.virt_install.uri}")
            parts.append(f"--name={self.template_name}")
            parts.append(f"--memory={self.memory}")
            parts.append(f"--vcpus={self.vcpus}")
            parts.append(f"--os-variant={self.os_variant}")
            parts.append("--import")
            parts.append(f"--disk=path={dst_image_path},format={self.disk_format},bus=virtio")
            parts.append(f"--disk=path={seed_iso_path},device=cdrom")

            # Use bridge device directly if resolved, otherwise fall back to network name
            if bridge_device:
                parts.append(f"--network=bridge={bridge_device},model=virtio")
            else:
                parts.append(f"--network=network={self.network},model=virtio")

            parts.append("--graphics=vnc")
            parts.append("--video=virtio")
            parts.append("--channel=unix,target_type=virtio,name=org.qemu.guest_agent.0")
            parts.append("--noautoconsole")

            cmd = " ".join(parts)
            cmd = self.virt_install._wrap_for_runtime(cmd)

            self.logger.info(f"executing: {cmd}")
            result = invoke.run(cmd, hide=True, warn=True)
            if not result.ok:
                self.logger.error(f"virt-install failed: {result.stderr}")
                return False
        except Exception as exc:
            self.logger.error(f"virt-install error: {exc}")
            return False

        # Verify the VM is up and healthy, then shut it down
        self.verify_and_shutdown()

        self.logger.info("=" * 70)
        self.logger.info(f"template VM '{self.template_name}' created successfully")
        self.logger.info(f"  disk image: {dst_image_path}")
        self.logger.info(f"  seed ISO:   {seed_iso_path}")
        self.logger.info("=" * 70)
        return True

    @staticmethod
    def hash_password(plain_password: str) -> str:
        """Hash a plain-text password using SHA-512 for use in cloud-init passwd field."""
        salt = crypt.mksalt(crypt.METHOD_SHA512)
        return crypt.crypt(plain_password, salt)