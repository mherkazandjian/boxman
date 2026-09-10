#!/usr/bin/env python
"""
Utility for importing/initializing VM images from URLs.

This module provides the ImageImporter class that downloads a .tar.gz file
containing a VM package (manifest, XML, and disk image), extracts it, and
uses the manifest to set up the VM in libvirt.
"""

import json
import os
import shlex
import shutil
import tempfile
import traceback
import uuid
from collections.abc import Callable
from typing import Any

from lxml import etree

from boxman import log
from boxman.exceptions import ImageImportError
from boxman.utils.http_download import download_url
from boxman.utils.shell import run

SUPPORTED_PROVIDERS = ("libvirt",)
REQUIRED_MANIFEST_KEYS = ("xml_path", "image_path", "provider")


def normalise_provider_name(value: str) -> str:
    """
    Canonicalise a manifest-supplied provider name.

    Manifest validation and the caller that turns the name into a lookup
    key (against ``boxman.yml`` and the provider registry) must agree on
    exactly one spelling. They used to normalise independently -- one
    lowercased, the other did not -- so ``"LibVirt"`` passed validation
    and then raised ``KeyError`` (#164 F1). Both now call this.
    """
    return value.strip().lower()


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
        disk_dir: str | None = None,
        vm_name: str | None = None,
        force: bool = False,
        keep_uuid: bool = False,
        progress_callback: Callable[[str], None] | None = None
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

    @staticmethod
    def _validate_manifest(manifest: Any, source: str) -> None:
        """Validate a parsed manifest dict; raise ValueError on any issue.

        *source* is included in error messages (path or URI of the manifest).
        Required keys: xml_path, image_path, provider.
        provider must be one of SUPPORTED_PROVIDERS.
        xml_path / image_path must be non-empty strings.
        """
        if not isinstance(manifest, dict):
            raise ValueError(
                f"manifest at {source!r} must be a JSON object, "
                f"got {type(manifest).__name__}"
            )
        for key in REQUIRED_MANIFEST_KEYS:
            if key not in manifest:
                raise ValueError(
                    f"manifest at {source!r} missing required field {key!r}"
                )
        for key in ("xml_path", "image_path"):
            value = manifest[key]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"manifest at {source!r} field {key!r} must be a "
                    f"non-empty string, got {value!r}"
                )
        provider = manifest["provider"]
        if (not isinstance(provider, str)
                or normalise_provider_name(provider) not in SUPPORTED_PROVIDERS):
            raise ValueError(
                f"manifest at {source!r} provider {provider!r} not supported "
                f"(supported: {', '.join(SUPPORTED_PROVIDERS)})"
            )

    def load_manifest(self, manifest_path: str) -> dict[str, Any] | None:
        """
        Load and parse a json manifest file.

        Returns the parsed manifest dict, or None on any error (logs the
        error first). For a strict (raising) variant that also accepts
        http(s):// URIs, see :meth:`load_manifest_from_uri`.
        """
        try:
            if not os.path.exists(manifest_path):
                self._log_error(f"manifest file not found: {manifest_path}")
                return None

            self._log_info(f"reading the manifest from {manifest_path}...")

            with open(manifest_path) as fobj:
                manifest = json.load(fobj)

            try:
                self._validate_manifest(manifest, manifest_path)
            except ValueError as exc:
                self._log_error(str(exc))
                return None

            self._log_info("Manifest loaded successfully")
            self._log_info(f"  XML path: {manifest['xml_path']}")
            self._log_info(f"  Image path: {manifest['image_path']}")
            self._log_info(f"  Provider: {manifest['provider']}")

            return manifest

        except json.JSONDecodeError as exc:
            self._log_error(f"Failed to parse manifest JSON: {exc}")
            return None
        except Exception as exc:
            self._log_error(f"Error loading manifest: {exc}")
            self._log_debug(traceback.format_exc())
            return None

    @classmethod
    def load_manifest_from_uri(cls, uri: str) -> tuple[dict[str, Any], str]:
        """Load a manifest from a file://, http://, or https:// URI.

        Returns ``(manifest_dict, local_path)`` where ``local_path`` is the
        on-disk location of the manifest (the original path for ``file://``
        or a freshly-downloaded temp file for HTTP URIs). Callers use the
        local path to resolve manifest-relative ``xml_path`` / ``image_path``.

        Raises ``ValueError`` for unsupported schemes, download failures,
        bad JSON, or schema-validation failures.
        """
        if uri.startswith("file://"):
            local_path = os.path.expanduser(uri[len("file://"):])
            if not os.path.exists(local_path):
                raise ValueError(f"manifest file not found: {local_path}")
        elif uri.startswith("http://") or uri.startswith("https://"):
            tmp_dir = tempfile.mkdtemp(prefix="boxman-manifest-")
            local_path = os.path.join(tmp_dir, "manifest.json")
            log.info(f"fetching remote manifest {uri} -> {local_path}")
            if not download_url(uri, local_path):
                raise ValueError(f"failed to download manifest from {uri}")
        else:
            raise ValueError(
                f"unsupported manifest URI scheme: {uri!r} "
                "(supported: file://, http://, https://)"
            )

        try:
            with open(local_path) as fobj:
                manifest = json.load(fobj)
        except json.JSONDecodeError as exc:
            raise ValueError(f"failed to parse manifest JSON at {uri}: {exc}") from exc

        cls._validate_manifest(manifest, uri)
        return manifest, local_path

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
                f"virsh -c {shlex.quote(self.uri)} list --all --name",
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
                # The XML path is derived from the VM name, which comes
                # from the manifest's XML — not from boxman (#164 F1).
                f"virsh -c {shlex.quote(self.uri)} define "
                f"{shlex.quote(xml_path)}",
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

    def load_xml(self, fpath: str) -> etree._ElementTree | None:
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
                f'rsync --sparse --progress {shlex.quote(src_path)} '
                f'{shlex.quote(dst_path)}',
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

    def import_image(self, package_url: str = None, vm_name: str = None) -> None:
        """
        Import and initialize a VM from a .tar.gz package.

        Args:
            package_url: URL or file path to the .tar.gz package
            vm_name: Name for the new VM

        Returns:
            None on success.

        Raises:
            ImageImportError: on any failure. This deliberately does not
                report failure through a return value: callers used to
                discard the ``False`` and a failed import exited 0 (#164 F1).
        """
        self._log_info("vm image import utility")

        # read the manifest
        # .. todo:: this is redundant with what is done in the session and the manager and the app
        # implement a generic manfest reader and use it in all these places
        manifest = self.load_manifest(self.manifest_path)
        if manifest is None:
            raise ImageImportError(
                f"could not read the manifest at {self.manifest_path!r}"
            )

        # read the xml definition of the vm. Validate it here, before any
        # destination directory is created or any disk is copied -- an
        # unreadable XML used to surface much later, as a FileNotFoundError
        # from the copy step, after the disk had already been written.
        vm_xml_path = os.path.join(os.path.dirname(self.manifest_path), manifest['xml_path'])
        vm_xml = self.load_xml(vm_xml_path)
        if vm_xml is None:
            raise ImageImportError(
                f"could not read the vm definition xml at {vm_xml_path!r}"
            )

        # get the name of the vm
        # perform some basic validation of the inputs and get/set the vm name
        vm_name = vm_name if vm_name else self.vm_name
        if not vm_name:
            # use xpath to get the name of the vm from the xml at "domain/name"
            name_elements = vm_xml.xpath('/domain/name')
            if not name_elements or not (name_elements[0].text or '').strip():
                raise ImageImportError(
                    f"no vm name given and the xml at {vm_xml_path!r} has no "
                    f"usable /domain/name element -- pass --name explicitly"
                )
            vm_name = name_elements[0].text.strip()

        self._log_info(f"VM name: {vm_name}")

        # check that a vm with the same name already exists
        if self.check_vm_exists(vm_name):
            if not self.force:
                raise ImageImportError(
                    f"VM '{vm_name}' already exists. Use force=True to override."
                )
            self._log_warning(f"Warning: VM '{vm_name}' already exists but force was specified")
        else:
            self._log_info(f"VM '{vm_name}' does not already exist. Proceeding with import.")

        # check that the disk image exists and copy it to the disk directory
        src_image_path = manifest['image_path']
        if not os.path.isabs(src_image_path):
            src_image_path = os.path.join(os.path.dirname(self.manifest_path), src_image_path)
        if not os.path.exists(src_image_path):
            raise ImageImportError(f"disk image file not found: {src_image_path}")

        # copy the image to the disk directory, create the dir first if needed
        # exit if the vm dir already exists
        dst_image_dir = os.path.abspath(os.path.expanduser(self.disk_dir))
        dst_image_dir_path = os.path.join(dst_image_dir, vm_name)
        if os.path.exists(dst_image_dir_path):
            raise ImageImportError(
                f"vm directory already exists: {dst_image_dir_path} -- refusing "
                f"to import into it, move it aside or pick another --directory"
            )
        self._log_info(f"Creating disk image directory: {dst_image_dir_path}")
        os.makedirs(dst_image_dir_path, exist_ok=False)

        #
        # Use sparse-aware copy instead of shutil.copy2
        # .. todo:: add flags to control doing the checksum since it might be time consuming
        #           for large file or just do it via rsync
        src_image_base_name = os.path.basename(src_image_path)
        dst_image_path = os.path.join(dst_image_dir_path, src_image_base_name)
        self._log_info(f"Copying disk image to: {dst_image_path}")
        if not self.copy_disk_image_sparse(src_image_path, dst_image_path):
            raise ImageImportError(
                f"failed to copy the disk image {src_image_path} -> {dst_image_path}"
            )
        # compare the checksum of the source and destination files
        src_size = os.path.getsize(src_image_path)
        dst_size = os.path.getsize(dst_image_path)
        if src_size != dst_size:
            raise ImageImportError(
                f"disk image copy failed: size mismatch "
                f"({src_size} != {dst_size}) for {dst_image_path}"
            )
        self._log_info("Disk image size verified")
        # Single-quote concatenation was not quoting: a path containing an
        # apostrophe closes the quote and the rest is parsed as shell (#164 F1).
        src_checksum = run(
            f"sha256sum {shlex.quote(src_image_path)}",
            hide=True).stdout.split()[0]
        dst_checksum = run(
            f"sha256sum {shlex.quote(dst_image_path)}",
            hide=True).stdout.split()[0]
        if src_checksum != dst_checksum:
            raise ImageImportError(
                f"disk image copy failed: checksum mismatch for {dst_image_path}"
            )
        self._log_info("Disk image checksum verified")

        # make a copy of the xml file in the dst vm dir and update it
        dst_xml_path = os.path.join(dst_image_dir_path, f"{vm_name}.xml")
        self._log_info(f"Copying VM XML to: {dst_xml_path}")
        shutil.copy2(vm_xml_path, dst_xml_path)

        # update the xml file of the vm to be imported
        if not self.edit_vm_xml(
            dst_xml_path,
            new_vm_name=vm_name,
            disk_path=dst_image_path,
            change_uuid=not self.keep_uuid
        ):
            raise ImageImportError(f"failed to edit the vm xml at {dst_xml_path}")

        self._log_info("Defining VM in libvirt...")
        if not self.define_vm(dst_xml_path):
            raise ImageImportError(
                f"libvirt refused to define the vm '{vm_name}' from {dst_xml_path}"
            )

        self._log_info(f"Successfully imported VM '{vm_name}'")
        self._log_info(f"  Disk image: {dst_image_path}")
        self._log_info(f"  Connection URI: {self.uri}")
