#!/usr/bin/env python
"""
Utility for importing/initializing VM images from URLs.

This module provides the ImageImporter class that downloads a .tar.gz file
containing a VM package (manifest, XML, and disk image), extracts it, and
uses the manifest to set up the VM in libvirt.
"""

import os
import uuid
import json
import shutil
import traceback
from typing import Optional, Dict, Any, Callable
from urllib.parse import urlparse

from lxml import etree
from invoke import run

from boxman import log


class ImageDownloaderUtils:
    """
    Class that provides utility functions for the ImageImporter such as downloading
    the package from various sources. move this to a separate module.
    For the time being just put the .zip on google drive, and add instructions on how to
    download and extract it using one liners and then point to the extracted location.
    for the command line to import it.
    """
    pass


class ImageImporter:
    """
    A class to import and initialize VM images from URLs or local files.

    Supports:
    - HTTP/HTTPS URLs
    - Google Drive URLs (drive.google.com)
    - Local file paths (file:// URLs)
    """

    def __init__(
        self,
        manifest_path: str = None,
        uri: str = "qemu:///system",
        disk_dir: Optional[str] = None,
        vm_name: Optional[str] = None,
        force: bool = False,
        keep_uuid: bool = False,
        progress_callback: Optional[Callable[[str], None]] = None
    ):
        """
        Initialize the ImageImporter.

        Args:
            manifest: manifest dictionary
            uri: libvirt connection URI (default: qemu:///system)
            disk_dir: directory to save disk images (default: current directory)
            vm_name: optional name of the VM
            manifest:
            force: force import even if VM with same name exists
            keep_uuid: keep the original UUID instead of generating a new one
            progress_callback: optional callback function for progress messages
        """
        self.manifest_path = manifest_path
        self.uri = uri
        self.disk_dir = disk_dir if disk_dir else os.getcwd()
        self.vm_name = vm_name
        self.force = force
        self.keep_uuid = keep_uuid
        self.progress_callback = progress_callback
        self.logger = log

    def _log_info(self, message: str):
        """Log an info message and optionally call the progress callback."""
        self.logger.info(message)
        if self.progress_callback:
            self.progress_callback(message)

    def _log_error(self, message: str):
        """Log an error message and optionally call the progress callback."""
        self.logger.error(message)
        if self.progress_callback:
            self.progress_callback(message)

    def _log_warning(self, message: str):
        """Log a warning message and optionally call the progress callback."""
        self.logger.warning(message)
        if self.progress_callback:
            self.progress_callback(message)

    def _log_debug(self, message: str):
        """Log a debug message."""
        self.logger.debug(message)

    def load_manifest(self, manifest_path: str) -> Optional[Dict[str, Any]]:
        """
        Load and parse a json manifest file.

        Args:
            manifest_path: the path to the manifest file

        Returns:
            dictionary containing manifest data, or None if failed
        """
        try:
            if not os.path.exists(manifest_path):
                self._log_error(f"manifest file not found: {manifest_path}")
                return None

            self._log_info(f"reading the manifest from {manifest_path}...")

            with open(manifest_path, 'r') as fobj:
                manifest = json.load(fobj)

            if 'xml_path' not in manifest:
                self._log_error("Manifest missing required field: 'xml_path'")
                return None

            if 'image_path' not in manifest:
                self._log_error("Manifest missing required field: 'image_path'")
                return None

            if 'provider' in manifest:
                if manifest['provider'].lower() != 'libvirt':
                    self._log_error(
                        f"Manifest provider '{manifest['provider']}' is not supported.")
                    return None

            self._log_info("Manifest loaded successfully")
            self._log_info(f"  XML path: {manifest['xml_path']}")
            self._log_info(f"  Image path: {manifest['image_path']}")
            self._log_info(f"  Provider: {manifest.get('provider', 'libvirt')}")

            return manifest

        except json.JSONDecodeError as exc:
            self._log_error(f"Failed to parse manifest JSON: {exc}")
            return None
        except Exception as exc:
            self._log_error(f"Error loading manifest: {exc}")
            self._log_debug(traceback.format_exc())
            return None

    def edit_vm_xml(self,
                    xml_path: str,
                    new_vm_name: str,
                    disk_path: str,
                    change_uuid: bool = True) -> bool:
        """
        Edit the VM XML definition to change the name, uuid, and disk path.

        Args:
            xml_path: path to the XML file to edit
            new_vm_name: the new name for the VM
            disk_path: the path to the disk image
            change_uuid: whether to generate a new uuid (default: True)

        Returns:
            True if successful, False otherwise
        """
        self._log_info(f"Editing VM XML: {xml_path}")
        tree = self.load_xml(xml_path)
        root = tree.getroot()

        name_elements = root.xpath('/domain/name')
        if name_elements:
            name_elements[0].text = new_vm_name
            self._log_info(f"  changed VM name to: {new_vm_name}")
        else:
            self._log_error("Could not find name element in XML")
            return False

        if change_uuid:
            uuid_elements = root.xpath('/domain/uuid')
            if uuid_elements:
                new_uuid = str(uuid.uuid4())
                uuid_elements[0].text = new_uuid
                self._log_info(f"  Changed UUID to: {new_uuid}")
            else:
                self._log_warning("Warning: Could not find UUID element in XML")

        disk_source_elements = root.xpath("/domain/devices/disk[@type='file'][@device='disk']/source[@file]")
        if disk_source_elements:
            disk_source_elements[0].set('file', disk_path)
            self._log_info(f"  Changed disk source to: {disk_path}")
        else:
            self._log_error("Could not find disk source element in XML")
            return False

        tree.write(xml_path, encoding='utf-8', xml_declaration=True, pretty_print=True)
        self._log_info("XML file updated successfully")

        return True

    def check_vm_exists(self, vm_name: str) -> bool:
        """
        Check if a vm with the given name already exists.

        Args:
            vm_name: The name of the VM to check

        Returns:
            True if the VM exists, False otherwise
        """
        try:
            result = run(
                f"virsh -c {self.uri} list --all --name",
                hide=True,
                warn=True
            )

            if result.ok:
                vm_list = [vm for vm in result.stdout.strip().split('\n') if vm]
                return vm_name in vm_list

            return False

        except Exception as exc:
            self._log_warning(f"Warning: Could not check if VM exists: {exc}")
            return False

    def define_vm(self, xml_path: str) -> bool:
        """
        Define a VM from an XML file using virsh.

        Args:
            xml_path: Path to the XML file

        Returns:
            True if successful, False otherwise
        """
        try:
            self._log_info(f"Defining VM from {xml_path}...")

            result = run(
                f"virsh -c {self.uri} define {xml_path}",
                hide=True,
                warn=True
            )

            if result.ok:
                self._log_info("VM defined successfully")
                return True
            else:
                self._log_error(f"Failed to define VM: {result.stderr}")
                return False

        except Exception as exc:
            self._log_error(f"Error defining VM: {exc}")
            return False

    def load_xml(self, fpath: str) -> Optional[etree._ElementTree]:
        """
        Load and parse an XML file.

        Args:
            fpath: Path to the XML file
        Returns:
            Parsed XML tree, or None if failed
        """
        try:
            tree = etree.parse(fpath)
            return tree
        except Exception as exc:
            log.error(f"Error loading XML file {fpath}: {exc}")
            return None

    def copy_disk_image_sparse(self, src_path: str, dst_path: str) -> bool:
        """
        Copy a disk image while preserving sparsity using rsync.

        Args:
            src_path: Source disk image path
            dst_path: Destination disk image path

        Returns:
            True if successful, False otherwise
        """
        try:
            self._log_info(f"Copying disk image (sparse-aware) from {src_path} to {dst_path}")

            # Use rsync with --sparse flag to preserve sparsity
            result = run(
                f'rsync --sparse --progress "{src_path}" "{dst_path}"',
                hide=False,
                warn=True
            )

            if result.ok:
                self._log_info("Disk image copied successfully using rsync (sparse-aware)")
                return True
            else:
                self._log_error(f"rsync failed: {result.stderr}")
                return False

        except Exception as exc:
            self._log_error(f"Failed to copy disk image: {exc}")
            return False

    def import_image(self, package_url: str = None, vm_name: str = None) -> bool:
        """
        Import and initialize a VM from a .tar.gz package.

        Args:
            package_url: URL or file path to the .tar.gz package
            vm_name: Name for the new VM

        Returns:
            True if successful, False otherwise
        """
        self._log_info("=" * 70)
        self._log_info("vm image import utility")
        self._log_info("=" * 70)

        # read the manifest
        # .. todo:: this is redundant with what is done in the session and the manager and the app
        # implement a generic manfest reader and use it in all these places
        manifest = self.load_manifest(self.manifest_path)

        # read the xml definition of the vm
        vm_xml_path = os.path.join(os.path.dirname(self.manifest_path), manifest['xml_path'])
        vm_xml = self.load_xml(vm_xml_path)

        # get the name of the vm
        # perform some basic validation of the inputs and get/set the vm name
        vm_name = vm_name if vm_name else self.vm_name
        if not vm_name:
            # use xpath to get the name of the vm from the xml at "domain/name"
            vm_name = vm_xml.xpath('/domain/name')[0].text.strip()

        if not vm_name:
            self._log_error("vm name must be specified")
            return False
        else:
            self._log_info(f"VM name: {vm_name}")

        # check that a vm with the same name already exists
        if self.check_vm_exists(vm_name):
            if not self.force:
                self._log_error(f"VM '{vm_name}' already exists. Use force=True to override.")
                return False
            else:
                self._log_warning(f"Warning: VM '{vm_name}' already exists but force was specified")
        else:
            self._log_info(f"VM '{vm_name}' does not already exist. Proceeding with import.")

        # check that the disk image exists and copy it to the disk directory
        src_image_path = manifest['image_path']
        if not os.path.isabs(src_image_path):
            src_image_path = os.path.join(os.path.dirname(self.manifest_path), src_image_path)
        if not os.path.exists(src_image_path):
            self._log_error(f"Disk image file not found: {src_image_path}")
            return False

        # copy the image to the disk directory, create the dir first if needed
        # exit if the vm dir already exists
        dst_image_dir = os.path.abspath(os.path.expanduser(self.disk_dir))
        dst_image_dir_path = os.path.join(dst_image_dir, vm_name)
        self._log_info(f"Creating disk image directory: {dst_image_dir_path}")
        if not os.path.exists(dst_image_dir_path):
            os.makedirs(dst_image_dir_path, exist_ok=False)
        else:
            self._log_info(f"vm directory already exists: {dst_image_dir_path}, exiting...")
            return False

        #
        # Use sparse-aware copy instead of shutil.copy2
        # .. todo:: add flags to control doing the checksum since it might be time consuming
        #           for large file or just do it via rsync
        src_image_base_name = os.path.basename(src_image_path)
        dst_image_path = os.path.join(dst_image_dir_path, src_image_base_name)
        self._log_info(f"Copying disk image to: {dst_image_path}")
        if not self.copy_disk_image_sparse(src_image_path, dst_image_path):
            self._log_error("Failed to copy disk image")
            return False
        # compare the checksum of the source and destination files
        src_size = os.path.getsize(src_image_path)
        dst_size = os.path.getsize(dst_image_path)
        if src_size != dst_size:
            self._log_error("Disk image copy failed: size mismatch")
            return False
        else:
            self._log_info("Disk image size verified")
        src_checksum = run(f"sha256sum '{src_image_path}'", hide=True).stdout.split()[0]
        dst_checksum = run(f"sha256sum '{dst_image_path}'", hide=True).stdout.split()[0]
        if src_checksum != dst_checksum:
            self._log_error("Disk image copy failed: checksum mismatch")
            return False
        else:
            self._log_info("Disk image checksum verified")

        # make a copy of the xml file in the dst vm dir and update it
        dst_xml_path = os.path.join(dst_image_dir_path, f"{vm_name}.xml")
        self._log_info(f"Copying VM XML to: {dst_xml_path}")
        shutil.copy2(vm_xml_path, dst_xml_path)

        # update the xml file of the vm to be imported
        status = self.edit_vm_xml(
            dst_xml_path,
            new_vm_name=vm_name,
            disk_path=dst_image_path,
            change_uuid=not self.keep_uuid
        )
        if not status:
            self._log_error("Failed to edit VM XML")
            return False

        self._log_info("Defining VM in libvirt...")
        if not self.define_vm(dst_xml_path):
            self._log_error("Failed to define VM")
            return False

        self._log_info("=" * 70)
        self._log_info(f"Successfully imported VM '{vm_name}'")
        self._log_info(f"  Disk image: {dst_image_path}")
        self._log_info(f"  Connection URI: {self.uri}")
        self._log_info("=" * 70)

        return True
