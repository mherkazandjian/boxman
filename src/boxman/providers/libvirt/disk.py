import contextlib
import os
import secrets
import shlex
import stat
import tempfile
from collections.abc import Callable
from typing import Any

from boxman import log
from boxman.exceptions import BoxmanError, DiskPathOccupiedError, ProvisionError
from boxman.utils.shell import run as _shell_run

from .commands import LibVirtCommandBase, VirshCommand
from .disk_ownership import (
    DEFAULT_DISK_TARGET,
    ROLE_ADOPTED,
    ROLE_DATA,
    disk_logical_name,
    read_disk_records,
    record_attached_disk,
)

#: the extended attribute a new image carries its creation token in: the
#: token its ownership record holds, which tells that very file from any
#: other put at the same path since (#215)
IMAGE_MARK_XATTR = "user.boxman.disk"


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


#: exit status of the image command when the directory it runs in is not the
#: private directory boxman made: not qemu-img's own failure (1), a shell's
#: (2, 126, 127) or docker's (125-127)
PRIVATE_DIR_MOVED_EXIT = 97

#: the image's name inside its private directory
_PRIVATE_IMAGE = "image"


def create_image_exclusive(disk_path: str,
                           size: str,
                           fmt: str,
                           run: Callable[[str], Any],
                           remedy: str = DATA_DISK_REMEDY,
                           token: str = "",
                           sudo: str = "",
                           ) -> tuple[int, int] | None:
    """
    Create a new, empty *fmt* image of *size* at *disk_path* with
    ``qemu-img create`` -- never over anything already there, and never
    through anything put in its way while it is made (#215).

    ``qemu-img create`` writes through whatever holds its path: it truncates
    a file, follows a symlink to truncate what it points at, and creates the
    missing target of a dangling one; only a directory makes it fail. That
    file may be a disk a teardown kept, another VM's disk, or the base of a
    snapshot chain, and the loss is silent. So:

    1. The disk's directory is opened, and everything below is done through
       that descriptor rather than its path. The disk's name is looked up in
       it first, and only "no such file" counts as absent: any entry -- a
       file, a symlink (dangling or not), a directory -- is refused, and a
       lookup that fails for another reason fails closed.
    2. A private directory is made in it, and refused unless it is a
       directory of the boxman user that no one else may change (mode 0700).
       The image file is created in it, exclusively, under a fixed name, and
       marked with *token*.
    3. One shell command enters the private directory by its path, checks
       that what it entered is the very directory made in 2 (device and
       inode), and only then runs ``qemu-img`` on ``/proc/$$/cwd/image``.
       The shell's working directory is a reference to that directory
       itself, so whatever happens to its name, or to any directory above
       it, after the check, the image is written in it. The path is
       absolute, so a ``sudo`` that starts ``qemu-img`` somewhere else (a
       sudoers ``runcwd``) still names it. A directory found moved or
       replaced at the check stops the command before ``qemu-img`` runs.
    4. The file is published from the pinned directories: checked to still
       be the file made in 2, then hard-linked to the disk's name.
       ``link(2)`` fails with ``EEXIST`` if anything holds the name, in the
       step that creates it, so an entry that appeared after the lookup is
       refused too.
    5. The image name and the private directory are removed whatever
       happens -- the directory only while its name still names it.

    What this guards against: someone other than the boxman user and root
    who can change entries in the disk's directory or in a directory above
    it. The boxman user and root can do anything boxman can, so they are out
    of scope.

    ``qemu-img`` runs through the caller's command wrapper, maybe as root:
    in the docker-compose runtime's container, or with *sudo*. No descriptor
    can be handed to it there, so the path it gets names the directory by
    the shell's own reference instead. Everything else -- the lookups, the
    private directory, the file, the link, the removal -- is done here, by
    the boxman user, in the cluster workdir, which boxman creates on the
    host and bind-mounts at the same path into the runtime container. A
    runtime whose bind mounts report another device or inode than the host
    (a VM-based Docker Desktop) fails the check in 3 every time, with the
    error that says so.

    Args:
        disk_path: where the image goes. ``~`` is expanded and the path made
            absolute: the path the domain XML names
            (:func:`libvirt_disk_source`).
        size: ``qemu-img`` size argument, e.g. ``1024M`` or ``20G``.
        fmt: image format, e.g. ``qcow2``.
        run: runs one shell command string where ``qemu-img`` runs, exactly
            as given, and returns its result (``.ok``, ``.return_code``,
            ``.stderr``); called with ``warn`` semantics, so a failed
            command is a result, not an exception.
        remedy: what the refusal tells the operator to do.
        token: marks the image (:data:`IMAGE_MARK_XATTR`) before ``qemu-img``
            writes into it, so it carries it from its first moment. A
            filesystem that takes no user extended attributes leaves it
            unmarked, which only means a later run cannot prove the image is
            boxman's own.
        sudo: the prefix ``qemu-img`` runs with -- ``"sudo "`` or ``""``.
            Nothing else in the command is given it.

    Returns:
        ``(st_dev, st_ino)`` of the new image, now at *disk_path*; ``None``
        when ``qemu-img`` failed, with nothing left behind.

    Raises:
        DiskPathOccupiedError: *disk_path* already has an entry, or one
            appeared while the image was being made. It is left untouched.
        ProvisionError: whether *disk_path* has an entry cannot be told, the
            private directory is not boxman's or was moved or replaced, or
            the image could not be made or put in place.
    """
    path = libvirt_disk_source(disk_path)
    directory, base = os.path.split(path)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise ProvisionError(
            f"could not create the directory {directory} for the disk image "
            f"{path} ({exc.strerror})") from exc
    try:
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise ProvisionError(
            f"could not open the directory {directory} to create the disk "
            f"image {path} in ({exc.strerror})") from exc
    try:
        try:
            st = os.stat(base, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ProvisionError(
                f"refusing to create the disk image {path}: whether something "
                f"is already there cannot be told ({exc.strerror}). Make it "
                f"accessible, then run the command again.") from exc
        else:
            raise DiskPathOccupiedError(
                _occupied(path, _entry_kind(st.st_mode), remedy))

        prefix = f".boxman-new.{os.getpid()}.{secrets.token_hex(4)}."
        private = prefix + base[:255 - len(prefix)]
        private_path = os.path.join(directory, private)
        try:
            os.mkdir(private, 0o700, dir_fd=dfd)
        except OSError as exc:
            raise ProvisionError(
                f"could not create {private_path} to make the disk image "
                f"{path} in ({exc.strerror})") from exc

        pfd = None
        # the private directory's stat, once it is known to be boxman's
        pinned = None
        # whether the image's name in it was made here
        image_made = False
        try:
            try:
                pfd = os.open(private, os.O_RDONLY | os.O_DIRECTORY
                              | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
            except OSError as exc:
                raise ProvisionError(
                    f"refusing to create the disk image {path}: "
                    f"{private_path}, the private directory boxman made for "
                    f"it, could not be opened ({exc.strerror}); nothing was "
                    f"written") from exc
            pst = os.fstat(pfd)
            if stat.S_ISDIR(pst.st_mode) and pst.st_uid == os.geteuid():
                pinned = pst
            if pinned is None or pst.st_mode & 0o077:
                raise ProvisionError(
                    f"refusing to create the disk image {path}: "
                    f"{private_path} is not a directory only the boxman user "
                    f"may change ({stat.filemode(pst.st_mode)}, owner uid "
                    f"{pst.st_uid}); nothing was written")

            try:
                ifd = os.open(_PRIVATE_IMAGE, os.O_WRONLY | os.O_CREAT
                              | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                              # 0644 before the umask: the mode `qemu-img
                              # create` gives a new file
                              0o644, dir_fd=pfd)
            except OSError as exc:
                raise ProvisionError(
                    f"could not create the disk image {path} in "
                    f"{private_path} ({exc.strerror})") from exc
            image_made = True
            try:
                info = os.fstat(ifd)
                if token:
                    try:
                        os.setxattr(ifd, IMAGE_MARK_XATTR, token.encode())
                    except OSError as exc:
                        log.debug(f"could not mark the image made in "
                                  f"{private_path} as boxman's "
                                  f"({exc.strerror})")
            finally:
                os.close(ifd)

            identity = f"{pinned.st_dev}:{pinned.st_ino}"
            cmd = (f"cd -P -- {shlex.quote(private_path)} && "
                   f"[ \"$(stat -c %d:%i .)\" = {shlex.quote(identity)} ] "
                   f"|| exit {PRIVATE_DIR_MOVED_EXIT}; "
                   f"{sudo}qemu-img create -f {shlex.quote(fmt)} "
                   f"\"/proc/$$/cwd/{_PRIVATE_IMAGE}\" {shlex.quote(size)}")
            log.info(f"creating disk image {path}: {cmd}")
            result = run(cmd)
            if not result.ok:
                if getattr(result, "return_code", None) == \
                        PRIVATE_DIR_MOVED_EXIT:
                    raise ProvisionError(
                        f"refusing to create the disk image {path}: the "
                        f"private directory boxman made to create it in is "
                        f"not the one found at {private_path} -- it was moved "
                        f"or replaced while the image was being made (or the "
                        f"runtime reports another device and inode for it "
                        f"than this host, as a VM-based Docker Desktop "
                        f"does); nothing was written")
                log.error(
                    f"failed to create disk image {path}: {result.stderr}")
                return None

            try:
                made = os.stat(_PRIVATE_IMAGE, dir_fd=pfd,
                               follow_symlinks=False)
            except OSError as exc:
                raise ProvisionError(
                    f"refusing to put the new disk image at {path}: the "
                    f"image made in {private_path} cannot be looked up "
                    f"({exc.strerror})") from exc
            if not (stat.S_ISREG(made.st_mode)
                    and (made.st_dev, made.st_ino)
                    == (info.st_dev, info.st_ino)):
                raise ProvisionError(
                    f"refusing to put the new disk image at {path}: what is "
                    f"in {private_path} is not the image boxman made there; "
                    f"nothing was put at {path}")
            try:
                os.link(_PRIVATE_IMAGE, base, src_dir_fd=pfd, dst_dir_fd=dfd,
                        follow_symlinks=False)
            except FileExistsError as exc:
                kind = "something"
                try:
                    kind = _entry_kind(os.stat(
                        base, dir_fd=dfd, follow_symlinks=False).st_mode)
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
            _remove_private(dfd, pfd, private, private_path, pinned,
                            image_made, path)
    finally:
        os.close(dfd)


def _remove_private(dfd: int, pfd: int | None, private: str,
                    private_path: str, pinned: os.stat_result | None,
                    image_made: bool, path: str) -> None:
    """
    Remove what :func:`create_image_exclusive` made in its private directory
    -- the image's name, if it made it, through the directory's descriptor
    -- and then the directory, but only while its name in the disk's
    directory still names it. Whatever else is found there is left as it
    is, and named.
    """
    if pfd is None:
        log.warning(
            f"left {private_path} as it is: it could not be opened, so it "
            f"cannot be told to be the directory boxman made for the disk "
            f"image {path}; remove it by hand if it is")
        return
    try:
        try:
            if image_made:
                os.unlink(_PRIVATE_IMAGE, dir_fd=pfd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning(
                f"could not remove {os.path.join(private_path, _PRIVATE_IMAGE)}"
                f", left from making the disk image {path} ({exc.strerror}); "
                f"remove it by hand")
        if pinned is None:
            log.warning(
                f"left {private_path} as it is: it is not a directory the "
                f"boxman user made; remove it by hand if it is stale")
            return
        try:
            there = os.stat(private, dir_fd=dfd, follow_symlinks=False)
        except OSError:
            there = None
        if there is None or (there.st_dev, there.st_ino) != \
                (pinned.st_dev, pinned.st_ino):
            log.warning(
                f"left {private_path} as it is: it is not the private "
                f"directory boxman made for the disk image {path} any more "
                f"(it was moved or replaced); remove it by hand if it is "
                f"stale")
            return
        # The one window left is between that check and this rmdir: a
        # directory swapped in at the name in that instant is removed only
        # if it is empty, which is all rmdir ever removes.
        try:
            os.rmdir(private, dir_fd=dfd)
        except OSError as exc:
            log.warning(
                f"could not remove {private_path}, left from making the disk "
                f"image {path} ({exc.strerror}); remove it by hand")
    finally:
        os.close(pfd)


class DiskManager:
    """
    Class for managing VM disk operations in libvirt.

    This class handles creating disk images with qemu-img and attaching them to VMs.
    """

    #: held around each ownership-record write, which reads the domain's
    #: records and writes them back with one added: callers that configure
    #: disks of one VM in parallel processes replace it with a lock they
    #: share, or two writes at once keep only one record (#215)
    records_lock: Any = contextlib.nullcontext()

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

    def create_disk(self, disk_path: str, size: int, format: str = 'qcow2',
                    token: str = "") -> tuple[int, int] | None:
        """
        Create a new, empty disk image using qemu-img -- never over an
        existing file, symlink or directory, nor through one put in its way
        (:func:`create_image_exclusive`, #215).

        Args:
            disk_path: Path where the disk image will be created
            size: Size of the disk in MiB
            format: Disk format (default: qcow2)
            token: mark the new image with it (:data:`IMAGE_MARK_XATTR`)

        Returns:
            ``(st_dev, st_ino)`` of the new image, or ``None`` if qemu-img
            failed

        Raises:
            DiskPathOccupiedError: *disk_path* is already taken; it is left
                as it was.
            ProvisionError: whether it is taken cannot be told, the image
                was put in danger while being made, or it could not be put
                in place.
        """
        try:
            cmd_executor = LibVirtCommandBase(
                provider_config=self.provider_config,
                override_config_use_sudo=False)
            created = create_image_exclusive(
                disk_path, f"{size}M", format,
                # the command as it is, wrapped for the runtime only:
                # execute_shell would decide a sudo for its first word, `cd`,
                # while the one qemu-img has always had goes before qemu-img
                run=lambda cmd: _shell_run(cmd_executor._wrap_for_runtime(cmd),
                                           hide=True, warn=True),
                token=token,
                sudo=cmd_executor.sudo_prefix("qemu-img create"))
        except BoxmanError:
            raise
        except Exception as exc:
            self.logger.error(f"error creating disk image: {exc}")
            return None

        if created is None:
            return None
        self.logger.info(f"successfully created disk image at {disk_path}")
        return created

    def _own_unattached_image(self, name: str, target: str,
                              source: str) -> str:
        """
        Whether the image at *source* is one boxman created for this VM as
        disk *name* at *target* and recorded, now not attached -- the state
        a run that stopped between creating and attaching it leaves, or one
        detached by hand (#215).

        It is, only when all of these hold, checked on one open descriptor
        of the file so they are all about the same file:

        - *source* is a regular file (never a symlink or a directory);
        - this VM's own ownership records hold one for exactly this disk:
          same *name*, *target* and *source*, role ``data`` (created by
          boxman, not adopted), with the image's inode number and creation
          token;
        - the file has that inode number;
        - the file carries that token (:data:`IMAGE_MARK_XATTR`), which
          boxman put on the image it created, before anything else could
          open it.

        The path alone is not enough: a record outlives its file, and a file
        put at the same path since is not the one the record was written
        for. The mark alone is not enough either: a copy that keeps
        extended attributes (``cp -a``, ``rsync -X``, ``shutil.copy2``)
        carries it, but is another inode. Anything that cannot be read (the
        path, the records, the mark) proves nothing, nor does a record
        without an inode number.

        One case is beyond it: a copy of the image made with its mark after
        the image was deleted can be given the same inode number back (ext4
        reuses a freed one at once). Only a deliberate copy of boxman's own
        image does that, and telling it apart would take the file's birth
        time or inode generation, which Python's standard library does not
        expose; anyone who can put it there could as well replace a disk
        that is attached. The device number is not compared: a btrfs
        subvolume's (Fedora's default ``/home``) can change across a
        reboot, and the record already pins the path.

        Returns:
            The token, or ``""`` when the image is not provably boxman's own.
        """
        try:
            # never through a symlink; and without blocking, should a FIFO
            # be there, which is refused below like anything but a file
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | os.O_CLOEXEC)
        except OSError:
            # nothing there, or it cannot be told: create_disk decides, and
            # refuses the latter
            return ""
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode):
                return ""
            try:
                records = read_disk_records(self.virsh, self.vm_name)
            except ProvisionError as exc:
                self.logger.warning(
                    f"VM {self.vm_name}: cannot tell whether boxman created "
                    f"{source} ({exc})")
                return ""
            for record in records or ():
                # at most one record has this name and target; one without
                # an inode number or a token proves nothing
                if (record.name == name and record.target == target
                        and record.role == ROLE_DATA
                        and record.source == source):
                    if record.ino != str(fst.st_ino):
                        return ""
                    try:
                        mark = os.getxattr(fd, IMAGE_MARK_XATTR)
                    except OSError:
                        return ""
                    if mark == record.token.encode():
                        return record.token
            return ""
        finally:
            os.close(fd)

    def _record(self, name: str, target: str, source: str, role: str,
                token: str, ino: str = "") -> bool:
        """
        Record disk *name* on this VM (:func:`record_attached_disk`).

        A failure is not the disk's: it is reported, and the configuring
        goes on -- but without the record boxman will refuse to detach the
        disk later rather than guess, and a rerun cannot prove the image is
        its own.

        Returns:
            Whether the record was written.
        """
        try:
            with self.records_lock:
                record_attached_disk(self.virsh, self.vm_name, name=name,
                                     target=target, source=source,
                                     role=role, token=token, ino=ino)
        except ProvisionError as exc:
            self.logger.warning(
                f"boxman could not record that disk {name} ({source}) of "
                f"{self.vm_name} is its own ({exc}). It will not be detached "
                f"automatically if it is later removed from the config.")
            return False
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

            source = libvirt_disk_source(disk_path)

            # 1. the image: an existing one only when the config says to
            # adopt it (attach_only), or when boxman's own record proves it
            # made that very file for this VM (a run that stopped between
            # creating and attaching it); otherwise a new one, which is never
            # created over anything already there (#215)
            recorded = False
            ino = ""
            if disk_config.get("attach_only"):
                self.logger.info(
                    f"using existing disk image {disk_path} (attach only)")
                # it already existed, which is not proof boxman created it
                # (#164 F2 review)
                role, token = ROLE_ADOPTED, ""
            else:
                role = ROLE_DATA
                token = self._own_unattached_image(disk_name, target_dev,
                                                   source)
                if token:
                    self.logger.info(
                        f"attaching {source}: boxman created it for "
                        f"{self.vm_name} and recorded it, and it is not "
                        f"attached")
                    recorded = True
                else:
                    token = secrets.token_hex(16)
                    created = self.create_disk(disk_path, disk_size,
                                               format=driver_type,
                                               token=token)
                    if not created:
                        self.logger.error(
                            f"failed to create disk {disk_path}")
                        return False
                    # the inode of the file made, not of whatever is at the
                    # path now
                    ino = str(created[1])
                    # Recorded before the attach: a run that stops between
                    # the two leaves an image whose record lets the rerun
                    # attach it rather than refuse it (#215).
                    recorded = self._record(disk_name, target_dev, source,
                                            role, token, ino)

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
            if not recorded:
                self._record(disk_name, target_dev, source, role, token, ino)

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
