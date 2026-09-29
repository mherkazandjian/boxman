import os
import secrets
import shlex
import stat
import tempfile
from collections.abc import Callable
from typing import Any

from boxman import log
from boxman.exceptions import BoxmanError, DiskPathOccupiedError, ProvisionError

from .commands import LibVirtCommandBase, VirshCommand
from .disk_ownership import (
    DEFAULT_DISK_TARGET,
    ROLE_ADOPTED,
    ROLE_DATA,
    disk_logical_name,
    record_attached_disk,
)


def libvirt_disk_source(disk_path: str) -> str:
    """
    The exact ``<source file=...>`` string written into a disk's XML.

    Ownership records store this, not the path they were handed. The two
    used to be computed independently -- the XML expanded and absolutised,
    the record did not -- so a project with a relative or ``~`` workdir
    recorded a source that could never equal what libvirt reports back,
    and every removal on it was refused for a mismatch that was not real
    (#164 F2 review).
    """
    return os.path.abspath(os.path.expanduser(disk_path))


def disk_path_for(workdir: str,
                  disk_name: str,
                  driver_type: str = 'qcow2',
                  disk_prefix: str | None = None) -> str:
    """
    Return the canonical path of a disk image under *workdir*.

    Naming: ``<workdir>/<disk_prefix>_<disk_name>.<driver_type>`` when
    *disk_prefix* is given, ``<workdir>/<disk_name>.<driver_type>``
    otherwise; ``~`` is expanded.
    """
    if disk_prefix:
        disk_path = os.path.join(
            workdir, f"{disk_prefix}_{disk_name}.{driver_type}")
    else:
        disk_path = os.path.join(workdir, f"{disk_name}.{driver_type}")
    return os.path.expanduser(disk_path)


#: What a refused disk tells the operator to do, for a disk declared under
#: a VM's ``disks:`` (:func:`create_image_exclusive`).
DATA_DISK_REMEDY = (
    "If it is stale, remove or rename it and run the command again. If it "
    "is the disk this VM should use, set `attach_only: true` on the disk so "
    "that boxman attaches the existing file instead of creating one.")


def _entry_kind(mode: int) -> str:
    """What an ``lstat`` mode says is at a path, for an error message."""
    if stat.S_ISLNK(mode):
        return "a symlink"
    if stat.S_ISDIR(mode):
        return "a directory"
    if stat.S_ISREG(mode):
        return "a file"
    return "a special file"


def _occupied(path: str, kind: str, remedy: str) -> str:
    return (
        f"refusing to create the disk image {path}: {kind} is already "
        f"there, and `qemu-img create` would write over it (or through it). "
        f"It may be a disk a teardown kept -- a teardown keeps what it "
        f"cannot prove was the VM's own -- another VM's disk, or the base of "
        f"a snapshot chain; nothing was changed. {remedy}")


def create_image_exclusive(disk_path: str,
                           size: str,
                           fmt: str,
                           run: Callable[[str], Any],
                           remedy: str = DATA_DISK_REMEDY
                           ) -> tuple[int, int] | None:
    """
    Create a new, empty *fmt* image of *size* at *disk_path* with
    ``qemu-img create`` -- never over anything already there (#215).

    ``qemu-img create`` writes through whatever holds its path: it truncates
    a file, follows a symlink to truncate what it points at, and creates the
    missing target of a dangling one; only a directory makes it fail. That
    file may be a disk a teardown kept, another VM's disk, or the base of a
    snapshot chain, and the loss is silent. So:

    1. The path is looked up first, and only "no such file" counts as
       absent. Any entry -- a file, a symlink (dangling or not), a directory
       -- is refused; a lookup that fails for another reason (a directory on
       the way that cannot be searched) fails closed.
    2. The image is made under a private name in the same directory and
       then hard-linked to the path. ``link(2)`` fails with ``EEXIST`` if
       anything holds the name, and decides that in the same step that
       creates it, so an entry that appears after the lookup is refused
       too. ``os.link`` is exactly that call; ``ln`` without ``-T`` would
       link *into* a directory holding the name and report success.
    3. The private name is removed whatever happens, so a failure at any
       step leaves nothing behind.

    The private file is created here, by the boxman process, and
    ``qemu-img`` writes the image into it; it never creates the file
    itself. ``qemu-img`` runs through the caller's command wrapper, so it
    may run as root -- inside the docker-compose runtime's container, or
    under a ``force_sudo_commands`` entry -- and with
    ``fs.protected_hardlinks`` only a file's owner may hard-link a file it
    cannot write. The link, the lookup and the removal are directory
    operations on a cluster workdir, which boxman creates on the host and
    bind-mounts at the same path into the runtime container, so they are
    made here, by the owner, the same whatever runtime or ``use_sudo``
    setting ``qemu-img`` runs under.

    Args:
        disk_path: where the image goes. ``~`` is expanded and the path made
            absolute: the path the domain XML names
            (:func:`libvirt_disk_source`).
        size: ``qemu-img`` size argument, e.g. ``1024M`` or ``20G``.
        fmt: image format, e.g. ``qcow2``.
        run: runs one shell command string where ``qemu-img`` runs and
            returns its result (``.ok``, ``.stderr``); called with
            ``warn`` semantics, so a failed command is a result, not an
            exception.
        remedy: what the refusal tells the operator to do.

    Returns:
        ``(st_dev, st_ino)`` of the new image, now at *disk_path*; ``None``
        when ``qemu-img`` failed, with nothing left behind.

    Raises:
        DiskPathOccupiedError: *disk_path* already has an entry, or one
            appeared while the image was being made. It is left untouched.
        ProvisionError: whether *disk_path* has an entry cannot be told, or
            the image could not be made or put in place.
    """
    path = libvirt_disk_source(disk_path)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise ProvisionError(
            f"could not create the directory {directory} for the disk image "
            f"{path} ({exc.strerror})") from exc

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ProvisionError(
            f"refusing to create the disk image {path}: whether something is "
            f"already there cannot be told ({exc.strerror}). Make it "
            f"accessible, then run the command again.") from exc
    else:
        raise DiskPathOccupiedError(
            _occupied(path, _entry_kind(st.st_mode), remedy))

    prefix = f".boxman-new.{os.getpid()}.{secrets.token_hex(4)}."
    base = os.path.basename(path)
    scaffold = os.path.join(directory, prefix + base[:255 - len(prefix)])
    try:
        # 0644 before the umask: the mode `qemu-img create` gives a new file
        fd = os.open(scaffold, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError as exc:
        raise ProvisionError(
            f"could not create {scaffold} to make the disk image {path} in "
            f"({exc.strerror})") from exc
    try:
        try:
            info = os.fstat(fd)
        finally:
            os.close(fd)

        cmd = (f"qemu-img create -f {shlex.quote(fmt)} "
               f"{shlex.quote(scaffold)} {shlex.quote(size)}")
        log.info(f"creating disk image {path}: {cmd}")
        result = run(cmd)
        if not result.ok:
            log.error(f"failed to create disk image {path}: {result.stderr}")
            return None

        try:
            os.link(scaffold, path)
        except FileExistsError as exc:
            kind = "something"
            try:
                kind = _entry_kind(os.lstat(path).st_mode)
            except OSError:
                pass
            raise DiskPathOccupiedError(
                _occupied(path, f"{kind} that appeared while it was being "
                                f"made", remedy)) from exc
        except OSError as exc:
            raise ProvisionError(
                f"could not put the new disk image at {path} "
                f"({exc.strerror})") from exc
        return (info.st_dev, info.st_ino)
    finally:
        try:
            os.unlink(scaffold)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning(
                f"could not remove {scaffold}, left from making the disk "
                f"image {path} ({exc.strerror}); remove it by hand")


class DiskManager:
    """
    Class for managing VM disk operations in libvirt.

    This class handles creating disk images with qemu-img and attaching them to VMs.
    """

    def __init__(self, vm_name: str, provider_config: dict[str, Any] | None = None):
        """
        Initialize the disk manager.

        Args:
            vm_name: Name of the VM to manage disks for
            provider_config: Configuration for the libvirt provider
        """
        #: VirshCommand: Command executor for virsh
        self.virsh = VirshCommand(provider_config=provider_config)

        #: Dict[str, Any]: Configuration for the libvirt provider
        self.provider_config = provider_config or {}

        #: logging.Logger: Logger instance
        self.logger = log

        #: str: the name of the VM
        self.vm_name = vm_name

    def create_disk(self, disk_path: str, size: int, format: str = 'qcow2') -> bool:
        """
        Create a new, empty disk image using qemu-img -- never over an
        existing file, symlink or directory
        (:func:`create_image_exclusive`, #215).

        Args:
            disk_path: Path where the disk image will be created
            size: Size of the disk in MiB
            format: Disk format (default: qcow2)

        Returns:
            True if the image was created, False if qemu-img failed

        Raises:
            DiskPathOccupiedError: *disk_path* is already taken; it is left
                as it was.
            ProvisionError: whether it is taken cannot be told, or the image
                could not be put in place.
        """
        try:
            cmd_executor = LibVirtCommandBase(
                provider_config=self.provider_config,
                override_config_use_sudo=False)
            created = create_image_exclusive(
                disk_path, f"{size}M", format,
                run=lambda cmd: cmd_executor.execute_shell(cmd, warn=True))
        except BoxmanError:
            raise
        except Exception as exc:
            self.logger.error(f"error creating disk image: {exc}")
            return False

        if created is None:
            return False
        self.logger.info(f"successfully created disk image at {disk_path}")
        return True

    def attach_disk(self,
                   disk_path: str,
                   target_dev: str,
                   driver_name: str = 'qemu',
                   driver_type: str = 'qcow2',
                   bus: str = 'virtio',
                   persistent: bool = True) -> bool:
        """
        Attach a disk to the VM.

        Args:
            disk_path: Path to the disk image
            target_dev: Target device name (e.g., 'vdb')
            driver_name: Name of the disk driver (default: qemu)
            driver_type: Type of the disk driver (default: qcow2)
            bus: Disk bus type (default: virtio)
            persistent: Whether to make the attachment persistent

        Returns:
            True if successful, False otherwise
        """
        try:
            # Generate an XML file for the disk attachment
            xml_content = self._generate_disk_xml(
                disk_path=disk_path,
                target_dev=target_dev,
                driver_name=driver_name,
                driver_type=driver_type,
                bus=bus
            )

            # Create a temporary file for the XML
            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            # Attach the disk
            attachment_args = ["--persistent"] if persistent else []
            result = self.virsh.execute("attach-device", self.vm_name, temp_path,
                                  *attachment_args, warn=True)

            # Clean up the temporary file
            os.unlink(temp_path)

            if not result.ok:
                self.logger.error(f"Failed to attach disk: {result.stderr}")
                return False

            self.logger.info(f"successfully attached disk {disk_path} to VM {self.vm_name}")
            return True
        except Exception as e:
            self.logger.error(f"Error attaching disk: {e}")
            # Clean up temp file if it exists
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
            return False

    def _generate_disk_xml(self,
                          disk_path: str,
                          target_dev: str,
                          driver_name: str,
                          driver_type: str,
                          bus: str = 'virtio') -> str:
        """
        Generate XML for disk attachment.

        Args:
            disk_path: Path to the disk image
            target_dev: Target device name
            driver_name: Name of the disk driver
            driver_type: Type of the disk driver
            bus: Disk bus type (default: virtio)

        Returns:
            XML string for disk attachment
        """
        return f"""<disk type='file' device='disk'>
  <driver name='{driver_name}' type='{driver_type}' discard='unmap'/>
  <source file='{libvirt_disk_source(disk_path)}'/>
  <target dev='{target_dev}' bus='{bus}'/>
</disk>"""

    def configure_from_disk_config(self,
                                  disk_config: dict[str, Any],
                                  workdir: str,
                                  disk_prefix: str = "") -> bool:
        """
        Configure a disk from configuration.

        Args:
            disk_config: Dictionary with disk configuration. When it
                contains ``attach_only: True`` the image file is expected
                to already exist and creation is skipped (attach only).
                Otherwise a file (or anything else) already at the disk's
                path is refused, never written over (#215).
            workdir: Working directory for disk images
            disk_prefix: Prefix to add to disk image filename

        Returns:
            True if successful, False otherwise; a refusal is logged as an
            error naming the file.
        """
        try:
            # extract configuration
            disk_name = disk_logical_name(disk_config)
            disk_size = disk_config.get("size", 1024)  # default 1GB

            # get driver info
            driver = disk_config.get("driver", {})
            driver_name = driver.get("name", "qemu")
            driver_type = driver.get("type", "qcow2")

            # get target device and bus
            target_dev = disk_config.get("target", DEFAULT_DISK_TARGET)
            bus = disk_config.get("bus", "virtio")

            # create disk path
            disk_path = disk_path_for(workdir, disk_name,
                                      driver_type=driver_type,
                                      disk_prefix=disk_prefix)

            # 1. create the disk — unless the caller flagged the config
            # attach_only (image file already exists, e.g. a leftover
            # from a failed earlier run): recreating it would wipe data.
            if disk_config.get("attach_only"):
                self.logger.info(
                    f"using existing disk image {disk_path} (attach only)")
            elif not self.create_disk(disk_path, disk_size, format=driver_type):
                self.logger.error(f"failed to create disk {disk_path}")
                return False

            # 2. attach the disk to the VM
            if not self.attach_disk(
                disk_path=disk_path,
                target_dev=target_dev,
                driver_name=driver_name,
                driver_type=driver_type,
                bus=bus
            ):
                self.logger.error(f"Failed to attach disk {disk_path} to VM {self.vm_name}")
                return False

            # Record what was attached, so a later `update` that no longer
            # declares this disk can tell it apart from the root disk, from
            # one attached by hand, and from a different disk that has since
            # taken the same target (#164 F2).
            try:
                record_attached_disk(
                    self.virsh, self.vm_name,
                    name=disk_name, target=target_dev,
                    source=libvirt_disk_source(disk_path),
                    # attach_only means the image already existed, which is
                    # not proof boxman created it (#164 F2 review)
                    role=(ROLE_ADOPTED if disk_config.get("attach_only")
                          else ROLE_DATA))
            except ProvisionError as exc:
                # The disk is attached and working; only the bookkeeping
                # failed. Do not fail the attach over it -- but say so
                # clearly, because without the record boxman will refuse to
                # detach this disk later rather than guess.
                self.logger.warning(
                    f"disk {disk_name} is attached to {self.vm_name}, but "
                    f"boxman could not record that it owns it ({exc}). It "
                    f"will not be detached automatically if it is later "
                    f"removed from the config.")

            self.logger.info(f"successfully configured disk {disk_name} for VM {self.vm_name}")
            return True
        except BoxmanError as exc:
            # boxman's own errors carry their cause: a refusal to create the
            # image over something already there names the file and what to
            # do about it (#215)
            self.logger.error(f"VM {self.vm_name}: {exc}")
            return False
        except Exception as exc:
            self.logger.error(f"error configuring disk from config: {exc}")
            return False

    def resize_disk_offline(self, disk_path: str, new_size_mb: int) -> bool:
        """
        Resize a disk image using qemu-img resize. Only grows.

        Args:
            disk_path: Path to the disk image
            new_size_mb: New size in MiB (must be larger than current)

        Returns:
            True if successful, False otherwise
        """
        try:
            cmd = (f"qemu-img resize {shlex.quote(disk_path)} "
                   f"{shlex.quote(f'{new_size_mb}M')}")
            self.logger.info(f"resizing disk image: {cmd}")

            cmd_executor = LibVirtCommandBase(
                provider_config=self.provider_config,
                override_config_use_sudo=False)
            result = cmd_executor.execute_shell(cmd, warn=True)

            if not result.ok:
                self.logger.error(f"failed to resize disk image: {result.stderr}")
                return False

            self.logger.info(f"successfully resized disk image {disk_path} to {new_size_mb}M")
            return True
        except Exception as exc:
            self.logger.error(f"error resizing disk image: {exc}")
            return False

    def resize_disk_online(self, target_dev: str, new_size_mb: int) -> bool:
        """
        Resize a block device on a running VM using virsh blockresize.

        Args:
            target_dev: Target device name (e.g., 'vdb')
            new_size_mb: New size in MiB

        Returns:
            True if successful, False otherwise
        """
        try:
            result = self.virsh.execute(
                'blockresize', self.vm_name,
                target_dev, f"--size={new_size_mb}M", warn=True)

            if not result.ok:
                self.logger.error(
                    f"failed to online resize {target_dev} on {self.vm_name}: "
                    f"{result.stderr}")
                return False

            self.logger.info(
                f"online resized {target_dev} on {self.vm_name} to {new_size_mb}M")
            return True
        except Exception as exc:
            self.logger.error(f"error in online disk resize: {exc}")
            return False

    def resize_disk(self,
                    disk_path: str,
                    target_dev: str,
                    new_size_mb: int,
                    vm_running: bool) -> bool:
        """
        Resize a disk: offline resize always, plus online resize if VM is running.

        Args:
            disk_path: Path to the disk image
            target_dev: Target device name (e.g., 'vdb')
            new_size_mb: New size in MiB
            vm_running: Whether the VM is currently running

        Returns:
            True if successful, False otherwise
        """
        if vm_running:
            # For running VMs, use virsh blockresize which handles both
            # the image and the hypervisor-level resize.
            return self.resize_disk_online(target_dev, new_size_mb)
        else:
            return self.resize_disk_offline(disk_path, new_size_mb)
