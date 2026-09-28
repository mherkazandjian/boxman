import os
import tempfile
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from boxman import log
from boxman.exceptions import ProvisionError

from .commands import VirshCommand
from .virsh_parse import parse_domblklist, parse_domblklist_strict

#: Default CDROM targets on the IDE bus: two channels of two drives, the
#: whole bus. Offered only on a machine with a built-in IDE controller.
IDE_TARGETS = tuple(f'hd{suffix}' for suffix in 'abcd')

#: Default CDROM targets on the SATA bus, which i440fx and q35 both have.
SATA_TARGETS = tuple(f'sd{chr(ord("a") + i)}' for i in range(10))


def _xml_attr(value: str) -> str:
    """
    Escape a value for use inside a single-quoted XML attribute.

    Mirrors ``net_reconcile._attr``: a source path or target name holding an
    ``&`` or a quote would otherwise produce XML that libvirt cannot parse.
    """
    return escape(str(value), {"'": '&apos;', '"': '&quot;'})


def _machine_has_builtin_ide(machine: str) -> bool:
    """
    Whether a libvirt machine type comes with an IDE controller.

    Mirrors libvirt's own test (``qemuDomainHasBuiltinIDE``, through
    ``qemuDomainIsI440FX``) for x86: only the i440fx family — ``pc``,
    ``pc-i440fx-*`` and the older ``pc-0.*``, ``pc-1.*`` and ``rhel*``
    types — has one, the PIIX IDE controller. q35 (``q35``, ``pc-q35-*``)
    has none: libvirt defines a q35 domain holding an IDE drive without
    complaint, then refuses to start it ("IDE controllers are unsupported
    for this QEMU binary or machine type", #217).

    Args:
        machine: The ``machine`` attribute of a domain's ``<os><type>``,
            e.g. ``pc-i440fx-8.2``.

    Returns:
        True only for a machine type known to have an IDE controller.
    """
    return machine == 'pc' or machine.startswith(
        ('pc-i440fx-', 'pc-0.', 'pc-1.', 'rhel'))


class CDROMManager:
    """
    Class for managing CDROM/ISO device operations in libvirt.

    Handles attaching ISO images as CDROM devices to VMs, detaching them,
    and swapping media in existing CDROM slots.
    """

    def __init__(self, vm_name: str, provider_config: dict[str, Any] | None = None):
        #: VirshCommand: Command executor for virsh
        self.virsh = VirshCommand(provider_config=provider_config)

        #: Dict[str, Any]: Configuration for the libvirt provider
        self.provider_config = provider_config or {}

        #: logging.Logger: Logger instance
        self.logger = log

        self.vm_name = vm_name

    def attach_cdrom(self,
                     source_path: str,
                     target_dev: str | None = None,
                     persistent: bool = True) -> bool:
        """
        Attach an ISO image as a CDROM device to the VM.

        Args:
            source_path: Absolute path to the ISO image
            target_dev: Target device name (e.g., 'sdb'), used as given. If
                None, the first free target on a bus the domain's machine
                has (see :meth:`_find_next_available_target`).
            persistent: Whether to make the attachment persistent

        Returns:
            True if successful, False otherwise
        """
        try:
            source_path = os.path.abspath(os.path.expanduser(source_path))
            if not os.path.isfile(source_path):
                self.logger.error(f"ISO file does not exist: {source_path}")
                return False

            if target_dev is None:
                target_dev = self._find_next_available_target()
                if target_dev is None:
                    self.logger.error(
                        f"could not find available CDROM target for VM {self.vm_name}")
                    return False

            xml_content = self._generate_cdrom_xml(source_path, target_dev)

            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            attachment_args = ["--persistent"] if persistent else []
            # warn=True so a failed attach reaches the error branch below
            # instead of raising out of execute (matches change_media)
            result = self.virsh.execute("attach-device", self.vm_name, temp_path,
                                  *attachment_args, warn=True)

            os.unlink(temp_path)

            if not result.ok:
                self.logger.error(f"failed to attach CDROM: {result.stderr}")
                return False

            self.logger.info(
                f"attached CDROM {source_path} as {target_dev} on VM {self.vm_name}")
            return True
        except Exception as e:
            self.logger.error(f"error attaching CDROM: {e}")
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
            return False

    def detach_cdrom(self, target_dev: str, persistent: bool = True) -> bool:
        """
        Detach a CDROM device from the VM.

        Args:
            target_dev: Target device name to detach (e.g., 'hdc')
            persistent: Whether to make the detachment persistent

        Returns:
            True if successful, False otherwise
        """
        try:
            # Generate XML for the device to detach (source not needed for detach)
            bus = self._bus_for_target(target_dev)
            xml_content = f"""<disk type='file' device='cdrom'>
  <target dev='{_xml_attr(target_dev)}' bus='{bus}'/>
  <readonly/>
</disk>"""

            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as temp:
                temp.write(xml_content)
                temp_path = temp.name

            detach_args = ["--persistent"] if persistent else []
            # warn=True so a failed detach reaches the error branch below
            # instead of raising out of execute (matches change_media)
            result = self.virsh.execute("detach-device", self.vm_name, temp_path,
                                  *detach_args, warn=True)

            os.unlink(temp_path)

            if not result.ok:
                self.logger.error(
                    f"failed to detach CDROM {target_dev} from {self.vm_name}: {result.stderr}")
                return False

            self.logger.info(f"detached CDROM {target_dev} from VM {self.vm_name}")
            return True
        except Exception as e:
            self.logger.error(f"error detaching CDROM: {e}")
            if 'temp_path' in locals() and os.path.exists(temp_path):
                os.unlink(temp_path)
            return False

    def change_media(self, target_dev: str, source_path: str,
                     live: bool = True) -> bool:
        """
        Swap ISO media in an existing CDROM slot.

        Args:
            target_dev: Target device name (e.g., 'hdc')
            source_path: Path to the new ISO image
            live: Whether the domain is active. ``--live`` against an
                inactive domain is rejected by libvirt, and omitting
                ``--live`` for an active one changes only the persistent
                configuration — the guest keeps the old media until it is
                next booted, while the caller is told the swap worked
                (#164 FB-5).

        Returns:
            True if successful, False otherwise
        """
        try:
            source_path = os.path.abspath(os.path.expanduser(source_path))
            if not os.path.isfile(source_path):
                self.logger.error(f"ISO file does not exist: {source_path}")
                return False

            scope = ["--live", "--config"] if live else ["--config"]
            result = self.virsh.execute(
                "change-media", self.vm_name, target_dev,
                source_path, *scope,
                warn=True)

            if not result.ok:
                self.logger.error(
                    f"failed to change media on {target_dev}: {result.stderr}")
                return False

            self.logger.info(
                f"changed CDROM media on {target_dev} to {source_path} "
                f"on VM {self.vm_name}")
            return True
        except Exception as e:
            self.logger.error(f"error changing CDROM media: {e}")
            return False

    def configure_from_config(self, cdrom_config: dict[str, Any]) -> bool:
        """
        Configure a CDROM device from a configuration dictionary.

        Args:
            cdrom_config: Dictionary with 'name', 'source', and optional 'target' keys

        Returns:
            True if successful, False otherwise
        """
        source = cdrom_config.get('source')
        if not source:
            self.logger.error("CDROM config missing 'source' field")
            return False

        target = cdrom_config.get('target')
        return self.attach_cdrom(source_path=source, target_dev=target)

    def get_attached_cdroms(self) -> list[dict[str, Any]]:
        """
        Every CDROM device on the VM, empty drives included.

        Returns a list of dicts with 'target' and 'source' keys; ``source``
        is ``None`` for a drive with no media. Seed ISOs (cloud-init) are
        excluded.

        Empty drives are reported rather than skipped because they are part
        of the domain's topology: a drive that exists but holds nothing is
        where media gets *inserted*, not a place to add a second device.
        Dropping them made a media request on an existing empty target look
        like a device addition (#164 FB-5).

        Raises:
            ProvisionError: if the device list cannot be read. Returning an
                empty list made a failed query indistinguishable from a
                domain with no CDROMs at all, and the caller then treats
                every declared cdrom as new (#164 FB-5).
        """
        result = self.virsh.execute("domblklist", self.vm_name, "--details", warn=True)
        if not result.ok:
            raise ProvisionError(
                f"could not list the block devices of {self.vm_name} (exit "
                f"{result.return_code}): "
                f"{(result.stderr or '').strip() or 'no error output'}")

        cdroms = []
        for row in parse_domblklist(result.stdout):
            if row.device != 'cdrom':
                continue
            source = row.source or '-'
            if source == '-':
                cdroms.append({'target': row.target, 'source': None})
                continue
            # exclude seed ISOs (cloud-init)
            if os.path.basename(source).startswith('seed'):
                continue

            cdroms.append({
                'target': row.target,
                'source': source,
            })

        return cdroms

    def _generate_cdrom_xml(self, source_path: str, target_dev: str) -> str:
        """
        Generate XML for CDROM device attachment.

        Args:
            source_path: Absolute path to ISO image
            target_dev: Target device name

        Returns:
            XML string for CDROM attachment
        """
        bus = self._bus_for_target(target_dev)
        return f"""<disk type='file' device='cdrom'>
  <driver name='qemu' type='raw'/>
  <source file='{_xml_attr(source_path)}'/>
  <target dev='{_xml_attr(target_dev)}' bus='{bus}'/>
  <readonly/>
</disk>"""

    @staticmethod
    def _bus_for_target(target_dev: str) -> str:
        """
        Derive the libvirt bus from a target device name.

        ``_find_next_available_target`` hands out sd* names — the only ones
        on a machine without IDE (#217), and on i440fx once the four IDE
        slots are taken — so the bus cannot be hardcoded to ide.

        Returns:
            'sata' for sd* targets, 'ide' otherwise (hd*)
        """
        return 'sata' if target_dev.startswith('sd') else 'ide'

    def _find_next_available_target(self) -> str | None:
        """
        Find the first free target for a CDROM declared without one.

        The bus follows from the target's name (see :meth:`_bus_for_target`),
        so the name must be one the domain's machine can host. IDE
        ``hda``-``hdd`` is tried first only on a machine with a built-in IDE
        controller — the i440fx family, where this keeps the old default and
        existing VMs are unchanged — and then SATA ``sda``-``sdj``. Any other
        machine gets SATA only: q35 has no IDE controller, and a q35 clone
        given an ``hdX`` drive was defined but could not start (#217).

        A target in use in either the live or the persistent definition is
        not handed out (see :meth:`_used_targets`).

        Returns:
            Device name (e.g., 'sdb' or 'hdc') or None if no slot available

        Raises:
            ProvisionError: if the domain's block devices cannot be read
        """
        used_targets = self._used_targets()

        machine = self._machine_type()
        if machine is None:
            # The machine type cannot be read: prefer SATA, which i440fx and
            # q35 both have, over IDE, which breaks a q35 domain's next
            # start (#217).
            self.logger.warning(
                f"could not read the machine type of {self.vm_name}; giving "
                f"its CDROM a SATA target, which i440fx and q35 both support")
            candidates = SATA_TARGETS
        elif _machine_has_builtin_ide(machine):
            # i440fx: IDE first, as before, so existing VMs are unchanged
            candidates = IDE_TARGETS + SATA_TARGETS
        else:
            # q35, or any other machine without an IDE controller
            candidates = SATA_TARGETS

        for candidate in candidates:
            if candidate not in used_targets:
                return candidate

        return None

    def _used_targets(self) -> set[str]:
        """
        Every device target in the domain's live and persistent definition.

        Plain ``domblklist`` of a running domain reports only the live
        definition, while a drive attached with ``--config`` alone is only
        in the persistent one — what the domain starts with next — and one
        hot-plugged without it only in the live one. A target taken in
        either is not free. For a shut-off or transient domain both queries
        report its one definition (libvirt 10.0).

        Raises:
            ProvisionError: if either definition cannot be listed or reads
                as incomplete (see :func:`parse_domblklist_strict`). An empty
                set would make a failed query look like a domain with no
                devices, and the first target would be handed out whether or
                not it is taken (the same trap as #164 FB-5).
        """
        used_targets = set()
        for inactive in ((), ("--inactive",)):
            definition = 'persistent' if inactive else 'live'
            result = self.virsh.execute(
                "domblklist", self.vm_name, "--details", *inactive, warn=True)
            if not result.ok:
                raise ProvisionError(
                    f"could not list the block devices of {self.vm_name} "
                    f"({definition} definition, exit {result.return_code}): "
                    f"{(result.stderr or '').strip() or 'no error output'}")
            rows = parse_domblklist_strict(result.stdout)
            if rows is None:
                raise ProvisionError(
                    f"could not read the block devices of {self.vm_name} "
                    f"({definition} definition): virsh domblklist printed a "
                    f"line that is not a device, so which targets are taken "
                    f"cannot be told")
            used_targets.update(row.target for row in rows)
        return used_targets

    def _machine_type(self) -> str | None:
        """
        The machine type of the domain's persistent definition, such as
        ``pc-q35-8.2`` or ``pc-i440fx-8.2``, or None if it cannot be read.

        The persistent definition is the one the domain starts with next,
        and a start is where a drive its machine cannot host fails (#217);
        for a transient domain ``--inactive`` reports its one definition.
        """
        result = self.virsh.execute(
            "dumpxml", self.vm_name, "--inactive", warn=True)
        # a failed query is not read, whatever it printed
        if not result.ok:
            return None
        try:
            root = ET.fromstring(result.stdout or '')
        except ET.ParseError:
            return None
        os_type = root.find('./os/type')
        if os_type is None:
            return None
        return os_type.get('machine')
