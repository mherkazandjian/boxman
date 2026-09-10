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
from urllib.parse import urljoin, urlsplit

from lxml import etree

from boxman import log
from boxman.exceptions import ImageImportError
from boxman.utils.http_download import download_url
from boxman.utils.shell import run

SUPPORTED_PROVIDERS = ("libvirt",)
REQUIRED_MANIFEST_KEYS = ("xml_path", "image_path", "provider")


#: schemes a manifest reference may resolve to. A remote manifest must not
#: be able to steer the importer at the local filesystem (file://) or at an
#: arbitrary protocol handler.
REMOTE_SCHEMES = ("http", "https")


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
        progress_callback: Callable[[str], None] | None = None,
        manifest_uri: str | None = None
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
            manifest_uri: the URI the manifest came from. Required for a
                remote (http/https) manifest so its ``xml_path`` /
                ``image_path`` siblings can be resolved against it -- they
                are relative to the manifest, and for a remote manifest the
                local copy sits alone in a temp dir (#164 F1).
        """
        self.manifest_path = manifest_path
        self.manifest_uri = manifest_uri
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

    @staticmethod
    def _validated_vm_name(name: str, source: str) -> str:
        """
        Return *name*, or raise if it is not usable as a directory component.

        The vm name becomes a directory under ``--directory`` and the name of
        a libvirt domain. It comes either from ``--name`` or from the
        ``/domain/name`` element of an XML that, for a remote import, is
        supplied by whoever published the manifest -- so a name like
        ``../../etc`` would place the import outside the requested directory
        entirely (#164 F1).
        """
        if not isinstance(name, str) or not name.strip():
            raise ImageImportError(f"the vm name from {source} is empty")
        name = name.strip()
        separators = [os.sep] + ([os.altsep] if os.altsep else [])
        if (name in (os.curdir, os.pardir)
                or os.path.isabs(name)
                or any(sep in name for sep in separators)
                or "\x00" in name):
            raise ImageImportError(
                f"the vm name from {source} is not usable as a directory "
                f"name: {name!r}"
            )
        return name

    def _manifest_is_remote(self) -> bool:
        """True when the manifest was fetched over http(s)."""
        if not self.manifest_uri:
            return False
        return urlsplit(self.manifest_uri).scheme in REMOTE_SCHEMES

    def _resolve_remote_reference(self, reference: str, key: str) -> str:
        """
        Resolve a manifest-relative *reference* against the manifest URI.

        Refuses anything that would leave the http(s) world or reach at the
        importing host's own filesystem: a remote manifest asking for
        ``/etc/shadow`` as its ``image_path`` used to resolve to exactly
        that local file (#164 F1).
        """
        if os.path.isabs(reference) or reference.startswith("\\"):
            raise ImageImportError(
                f"remote manifest {self.manifest_uri} declares an absolute "
                f"{key} ({reference!r}); references must be relative to the "
                f"manifest"
            )
        parts = urlsplit(reference)
        if parts.scheme and parts.scheme not in REMOTE_SCHEMES:
            raise ImageImportError(
                f"remote manifest {self.manifest_uri} declares {key} with an "
                f"unsupported scheme: {reference!r} "
                f"(supported: {', '.join(REMOTE_SCHEMES)})"
            )
        resolved = urljoin(self.manifest_uri, reference)
        if urlsplit(resolved).scheme not in REMOTE_SCHEMES:
            raise ImageImportError(
                f"remote manifest {self.manifest_uri} declares {key} that "
                f"resolves outside http(s): {resolved!r}"
            )
        return resolved

    def _fetch_reference(self, reference: str, key: str, dest_dir: str) -> str:
        """Download a manifest sibling into *dest_dir*; return its path."""
        url = self._resolve_remote_reference(reference, key)
        dest = os.path.join(dest_dir, os.path.basename(urlsplit(url).path) or key)
        self._log_info(f"fetching {key} {url}")
        if not download_url(url, dest):
            raise ImageImportError(f"failed to download {key} from {url}")
        return dest

    def _local_reference(self, reference: str, key: str) -> str:
        """Resolve a *local* manifest's sibling reference to a path."""
        path = reference
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(self.manifest_path), path)
        if not os.path.exists(path):
            raise ImageImportError(f"{key} not found: {path}")
        return path

    def import_image(self, package_url: str = None, vm_name: str = None) -> None:
        """
        Import and initialize a VM from a manifest and its siblings.

        The import is assembled in a staging directory alongside the target
        and moved into place with a single rename, so a failure never leaves
        a half-built vm directory behind to block the retry.

        Args:
            package_url: unused; kept for signature compatibility
            vm_name: Name for the new VM

        Returns:
            None on success.

        Raises:
            ImageImportError: on any failure. This deliberately does not
                report failure through a return value: callers used to
                discard the ``False`` and a failed import exited 0 (#164 F1).
        """
        self._log_info("vm image import utility")

        manifest = self.load_manifest(self.manifest_path)
        if manifest is None:
            raise ImageImportError(
                f"could not read the manifest at {self.manifest_path!r}"
            )

        remote = self._manifest_is_remote()
        dst_root = os.path.abspath(os.path.expanduser(self.disk_dir))
        os.makedirs(dst_root, exist_ok=True)
        # Two scratch directories with one job each. `staging` holds exactly
        # what the vm directory should end up containing, because it *is*
        # renamed into place -- so the raw fetched xml must not land in it,
        # or the imported vm keeps a second, unedited definition pointing at
        # the publisher's original disk path. `meta_dir` holds that raw
        # download; it is small and always discarded.
        staging = os.path.join(dst_root, f".boxman-import-{uuid.uuid4().hex}")
        os.makedirs(staging, exist_ok=False)
        meta_dir = tempfile.mkdtemp(prefix="boxman-import-meta-") if remote else None

        try:
            # The xml is fetched first and on its own: it is small, and the
            # vm name may come from it, which decides where everything else
            # goes.
            if remote:
                vm_xml_path = self._fetch_reference(
                    manifest['xml_path'], 'xml_path', meta_dir)
            else:
                vm_xml_path = self._local_reference(
                    manifest['xml_path'], 'xml_path')

            # Validate the xml before the destination is touched. An
            # unreadable xml used to surface as a FileNotFoundError from the
            # final copy -- after the whole disk image had been written.
            vm_xml = self.load_xml(vm_xml_path)
            if vm_xml is None:
                raise ImageImportError(
                    f"could not read the vm definition xml at {vm_xml_path!r}"
                )

            requested_name = vm_name if vm_name else self.vm_name
            if requested_name:
                vm_name = self._validated_vm_name(requested_name, "--name")
            else:
                name_elements = vm_xml.xpath('/domain/name')
                if not name_elements:
                    raise ImageImportError(
                        f"no vm name given and the xml at {vm_xml_path!r} has "
                        f"no /domain/name element -- pass --name explicitly"
                    )
                vm_name = self._validated_vm_name(
                    name_elements[0].text or '',
                    f"the /domain/name element of {vm_xml_path!r}")

            self._log_info(f"VM name: {vm_name}")

            if self.check_vm_exists(vm_name):
                if not self.force:
                    raise ImageImportError(
                        f"VM '{vm_name}' already exists. Use force=True to override."
                    )
                self._log_warning(
                    f"Warning: VM '{vm_name}' already exists but force was specified")
            else:
                self._log_info(
                    f"VM '{vm_name}' does not already exist. Proceeding with import.")

            dst_image_dir_path = os.path.join(dst_root, vm_name)
            if os.path.exists(dst_image_dir_path):
                raise ImageImportError(
                    f"vm directory already exists: {dst_image_dir_path} -- "
                    f"refusing to import into it, move it aside or pick "
                    f"another --directory"
                )

            # Now the disk image, into the same staging directory.
            if remote:
                staged_image_path = self._fetch_reference(
                    manifest['image_path'], 'image_path', staging)
            else:
                src_image_path = self._local_reference(
                    manifest['image_path'], 'image_path')
                staged_image_path = os.path.join(
                    staging, os.path.basename(src_image_path))
                self._log_info(f"Copying disk image to: {staged_image_path}")
                if not self.copy_disk_image_sparse(src_image_path, staged_image_path):
                    raise ImageImportError(
                        f"failed to copy the disk image {src_image_path} -> "
                        f"{staged_image_path}"
                    )
                self._verify_copy(src_image_path, staged_image_path)

            image_basename = os.path.basename(staged_image_path)
            final_image_path = os.path.join(dst_image_dir_path, image_basename)

            # The xml is edited in staging but must already point at where
            # the disk will live once the staging directory is renamed.
            staged_xml_path = os.path.join(staging, f"{vm_name}.xml")
            shutil.copy2(vm_xml_path, staged_xml_path)
            if not self.edit_vm_xml(
                staged_xml_path,
                new_vm_name=vm_name,
                disk_path=final_image_path,
                change_uuid=not self.keep_uuid
            ):
                raise ImageImportError(
                    f"failed to edit the vm xml at {staged_xml_path}")

            # Single rename into place: either the vm directory is complete
            # or it does not exist.
            try:
                os.replace(staging, dst_image_dir_path)
            except OSError as exc:
                raise ImageImportError(
                    f"could not move the imported vm into place at "
                    f"{dst_image_dir_path}: {exc}"
                ) from exc
            staging = None

            final_xml_path = os.path.join(dst_image_dir_path, f"{vm_name}.xml")
            self._log_info("Defining VM in libvirt...")
            if not self.define_vm(final_xml_path):
                # The files are imported and in place; only the domain
                # definition failed. Say so, rather than leaving the user to
                # guess whether a multi-gigabyte download has to happen again.
                raise ImageImportError(
                    f"libvirt refused to define the vm '{vm_name}' from "
                    f"{final_xml_path}. The imported files are in place at "
                    f"{dst_image_dir_path} -- fix the definition and define "
                    f"it directly, or remove that directory to re-import"
                )
        finally:
            # Only ever the directories this call created, and `staging`
            # only while it is still staging -- once renamed, it is None.
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            if meta_dir is not None:
                shutil.rmtree(meta_dir, ignore_errors=True)

        self._log_info(f"Successfully imported VM '{vm_name}'")
        self._log_info(f"  Disk image: {final_image_path}")
        self._log_info(f"  Connection URI: {self.uri}")

    def _verify_copy(self, src_path: str, dst_path: str) -> None:
        """Check a local disk-image copy by size and then by checksum."""
        src_size = os.path.getsize(src_path)
        dst_size = os.path.getsize(dst_path)
        if src_size != dst_size:
            raise ImageImportError(
                f"disk image copy failed: size mismatch "
                f"({src_size} != {dst_size}) for {dst_path}"
            )
        self._log_info("Disk image size verified")
        # Single-quote concatenation was not quoting: a path containing an
        # apostrophe closes the quote and the rest is parsed as shell (#164 F1).
        src_checksum = run(
            f"sha256sum {shlex.quote(src_path)}", hide=True).stdout.split()[0]
        dst_checksum = run(
            f"sha256sum {shlex.quote(dst_path)}", hide=True).stdout.split()[0]
        if src_checksum != dst_checksum:
            raise ImageImportError(
                f"disk image copy failed: checksum mismatch for {dst_path}"
            )
        self._log_info("Disk image checksum verified")
