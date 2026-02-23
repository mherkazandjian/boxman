"""
Template manager for creating base VM templates from cloud images
with cloud-init configuration.
"""

import os
import shutil
from typing import Optional, Dict, Any

from invoke import run

from boxman import log
from boxman.providers.libvirt.cloudinit import CloudInit
from boxman.providers.libvirt.commands import VirtInstallCommand, VirshCommand


class TemplateManager:
    """
    Creates base VM templates by:
    1. Copying/downloading the base cloud image
    2. Generating a cloud-init seed ISO
    3. Running virt-install --import to create the template VM
    """

    def __init__(self, provider_config: Optional[Dict[str, Any]] = None):
        """
        Args:
            provider_config: Provider configuration dict (uri, use_sudo, etc.)
        """
        self.provider_config = provider_config or {}
        self.logger = log
        self.virt_install = VirtInstallCommand(provider_config)
        self.virsh = VirshCommand(provider_config)

    def _resolve_image_path(self, image_uri: str, dest_dir: str) -> str:
        """
        Resolve an image URI to a local file path.

        Supports:
          - file:///path/to/image
          - /absolute/path
          - ~/relative/path

        Args:
            image_uri: The image URI or path.
            dest_dir: Directory to copy the image into.

        Returns:
            Absolute path to the image in dest_dir.
        """
        if image_uri.startswith("file://"):
            src_path = os.path.expanduser(image_uri[len("file://"):])
        else:
            src_path = os.path.expanduser(image_uri)

        if not os.path.exists(src_path):
            raise FileNotFoundError(f"base image not found: {src_path}")

        # copy to dest_dir preserving the filename
        basename = os.path.basename(src_path)
        # change extension to .qcow2 if it's .img
        name_root, ext = os.path.splitext(basename)
        if ext in (".img",):
            basename = name_root + ".qcow2"

        dest_path = os.path.join(dest_dir, basename)

        if os.path.abspath(src_path) != os.path.abspath(dest_path):
            self.logger.info(f"copying base image {src_path} -> {dest_path}")
            # use sparse-aware copy
            result = run(
                f"rsync --sparse --progress '{src_path}' '{dest_path}'",
                hide=False, warn=True
            )
            if not result.ok:
                # fallback to regular copy
                shutil.copy2(src_path, dest_path)
        else:
            self.logger.info(f"base image already at {dest_path}")

        return dest_path

    def template_exists(self, name: str) -> bool:
        """Check if a VM/template with the given name already exists."""
        result = self.virsh.execute("list", "--all", "--name", warn=True)
        if result.ok:
            vm_list = [v.strip() for v in result.stdout.strip().split("\n") if v.strip()]
            return name in vm_list
        return False

    def create_template(self,
                        template_name: str,
                        template_config: Dict[str, Any],
                        workdir: str,
                        force: bool = False,
                        wait_for_cloudinit: bool = True) -> bool:
        """
        Create a single template VM from a cloud image + cloud-init config.

        Args:
            template_name: The key from the ``templates`` config section.
            template_config: Dict with keys: name, image, cloudinit,
                             and optionally: meta_data, network_config,
                             memory, vcpus, os_variant, network, etc.
            workdir: Working directory for intermediate files.
            force: If True, destroy existing VM with same name first.
            wait_for_cloudinit: If True, wait for cloud-init to finish
                                before shutting down the template VM.

        Returns:
            True if successful, False otherwise.
        """
        vm_name = template_config.get("name", template_name)

        self.logger.info("=" * 70)
        self.logger.info(f"creating template: {vm_name}")
        self.logger.info("=" * 70)

        # check if template already exists
        if self.template_exists(vm_name):
            if not force:
                self.logger.info(
                    f"template '{vm_name}' already exists, skipping "
                    "(use --force to recreate)")
                return True
            else:
                self.logger.warning(
                    f"template '{vm_name}' exists, destroying first (force=True)")
                self.virsh.execute("destroy", vm_name, warn=True)
                self.virsh.execute("undefine", vm_name,
                                   "--remove-all-storage", warn=True)

        # prepare workdir
        template_workdir = os.path.join(
            os.path.expanduser(workdir), ".boxman", "templates", vm_name)
        os.makedirs(template_workdir, exist_ok=True)

        # resolve base image
        image_uri = template_config.get("image", "")
        if not image_uri:
            self.logger.error(f"no 'image' specified for template '{template_name}'")
            return False

        try:
            disk_path = self._resolve_image_path(image_uri, template_workdir)
        except FileNotFoundError as exc:
            self.logger.error(str(exc))
            return False

        # build cloud-init seed ISO
        cloudinit_data = template_config.get("cloudinit", "")
        if not cloudinit_data:
            self.logger.error(
                f"no 'cloudinit' user-data specified for template '{template_name}'")
            return False

        ci = CloudInit(
            user_data=cloudinit_data,
            meta_data=template_config.get("meta_data"),
            network_config=template_config.get("network_config"),
            workdir=template_workdir,
        )

        try:
            seed_iso_path = ci.build_seed_iso()
        except RuntimeError as exc:
            self.logger.error(f"failed to build seed ISO: {exc}")
            return False

        # build virt-install command
        memory = template_config.get("memory", 2048)
        vcpus = template_config.get("vcpus", 2)
        os_variant = template_config.get("os_variant", "generic")
        network = template_config.get("network", "network=default,model=virtio")

        # build disk arguments — virt-install accepts multiple --disk flags
        # so we construct the raw command string
        disk_main = f"path={disk_path},format=qcow2,bus=virtio"
        disk_seed = f"path={seed_iso_path},device=cdrom"

        cmd_parts = []
        if self.provider_config.get("use_sudo", False):
            cmd_parts.append("sudo")

        virt_install_cmd = self.provider_config.get(
            "virt_install_cmd", "virt-install")
        uri = self.provider_config.get("uri", "qemu:///system")

        cmd_parts.extend([
            virt_install_cmd,
            f"--connect {uri}",
            f"--name {vm_name}",
            f"--memory {memory}",
            f"--vcpus {vcpus}",
            f"--os-variant {os_variant}",
            "--import",
            f"--disk {disk_main}",
            f"--disk {disk_seed}",
            f"--network {network}",
            "--graphics spice",
            "--video virtio",
            "--noautoconsole",
        ])

        # add any extra virt-install flags from config
        extra_args = template_config.get("virt_install_extra_args", [])
        cmd_parts.extend(extra_args)

        cmd = " ".join(cmd_parts)

        # wrap for runtime if needed
        runtime = self.provider_config.get("runtime", "local")
        if runtime == "docker-compose":
            container = self.provider_config.get(
                "runtime_container", "boxman-libvirt-default")
            escaped = cmd.replace("'", "'\\''")
            cmd = f"docker exec --user root {container} bash -c '{escaped}'"

        self.logger.info(f"executing: {cmd}")
        result = run(cmd, hide=False, warn=True)

        if not result.ok:
            self.logger.error(f"virt-install failed: {result.stderr}")
            return False

        self.logger.info(f"template VM '{vm_name}' created and booting")

        # optionally wait for cloud-init to complete then shut down
        if wait_for_cloudinit:
            self._wait_and_shutdown(vm_name, template_config)

        # clean up the seed ISO nocloud directory
        ci.cleanup()

        self.logger.info("=" * 70)
        self.logger.info(f"template '{vm_name}' is ready")
        self.logger.info("=" * 70)

        return True

    def _wait_and_shutdown(self, vm_name: str,
                           template_config: Dict[str, Any]) -> None:
        """
        Wait for cloud-init to finish inside the VM, then shut it down
        so it can be used as a template for cloning.
        """
        import time

        timeout = template_config.get("cloudinit_timeout", 300)
        poll_interval = template_config.get("cloudinit_poll_interval", 10)
        elapsed = 0

        self.logger.info(
            f"waiting up to {timeout}s for cloud-init to complete in '{vm_name}'...")

        while elapsed < timeout:
            time.sleep(poll_interval)
            elapsed += poll_interval

            # check if VM is still running
            result = self.virsh.execute(
                "domstate", vm_name, warn=True)
            if result.ok:
                state = result.stdout.strip()
                if state == "shut off":
                    self.logger.info(
                        f"VM '{vm_name}' has shut off (cloud-init may have "
                        f"triggered poweroff)")
                    return
            else:
                self.logger.warning(f"could not query state of '{vm_name}'")

            self.logger.info(
                f"  waiting... ({elapsed}/{timeout}s)")

        # timeout reached — shut down gracefully
        self.logger.info(
            f"cloud-init timeout reached, shutting down '{vm_name}'")
        self.virsh.execute("shutdown", vm_name, warn=True)

        # wait a bit for graceful shutdown
        for _ in range(6):
            time.sleep(5)
            result = self.virsh.execute("domstate", vm_name, warn=True)
            if result.ok and result.stdout.strip() == "shut off":
                self.logger.info(f"VM '{vm_name}' shut down successfully")
                return

        # force destroy if still running
        self.logger.warning(f"force-destroying '{vm_name}'")
        self.virsh.execute("destroy", vm_name, warn=True)
