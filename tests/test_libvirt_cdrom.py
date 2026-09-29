"""
Unit tests for boxman.providers.libvirt.cdrom.CDROMManager.

Part of Phase 1.2 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.exceptions import ProvisionError
from boxman.providers.libvirt.cdrom import CDROMManager

pytestmark = pytest.mark.unit


def _result(stdout: str = "", ok: bool = True, stderr: str = "", return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


@pytest.fixture
def cd() -> CDROMManager:
    return CDROMManager("vm01", provider_config={"use_sudo": False})


class TestGenerateXml:

    def test_xml_contains_expected_parts(self, cd: CDROMManager):
        xml = cd._generate_cdrom_xml("/tmp/foo.iso", "hdc")
        assert "device='cdrom'" in xml
        assert "file='/tmp/foo.iso'" in xml
        assert "dev='hdc'" in xml
        assert "bus='ide'" in xml
        assert "<readonly/>" in xml


class TestBusForTarget:
    # _find_next_available_target hands out sd* targets: the only ones on a
    # machine without IDE (#217), and on i440fx once the IDE slots are taken;
    # the device XML bus must match the target prefix (#85 item 26)

    def test_hd_targets_are_ide(self, cd: CDROMManager):
        assert cd._bus_for_target("hdc") == "ide"

    def test_sd_targets_are_sata(self, cd: CDROMManager):
        assert cd._bus_for_target("sda") == "sata"

    def test_generated_xml_matches_sata_target(self, cd: CDROMManager):
        xml = cd._generate_cdrom_xml("/tmp/foo.iso", "sda")
        assert "dev='sda' bus='sata'" in xml

    def test_detach_xml_matches_sata_target(self, cd: CDROMManager):
        written = {}

        def capture(*args, **kwargs):
            # the temp XML still exists while detach-device runs
            written["xml"] = Path(args[2]).read_text()
            return _result()

        with patch.object(cd.virsh, "execute", side_effect=capture):
            assert cd.detach_cdrom("sda") is True
        assert "dev='sda' bus='sata'" in written["xml"]


# domblklist --details has 4 columns: Type Device Target Source
BLK_HEADER = (
    "Type  Device  Target  Source\n"
    "------------------------------------------------\n"
)
BOOT_DISK = "file  disk    vda     /var/lib/libvirt/vm01.qcow2\n"
# The cloud-init templates' seed drive: its media is ejected, the drive
# stays. On q35 virt-install puts it on SATA, so every clone has it at sda.
EMPTY_SEED_DRIVE = "file  cdrom   sda     -\n"

Q35 = "pc-q35-8.2"
I440FX = "pc-i440fx-8.2"


def _domain_xml(machine: str | None) -> str:
    """A domain definition whose ``<os><type>`` names *machine*."""
    machine_attr = f" machine='{machine}'" if machine else ""
    return ("<domain type='kvm'><name>vm01</name>"
            f"<os><type arch='x86_64'{machine_attr}>hvm</type></os>"
            "<devices/></domain>")


def _target_of(xml: str) -> str:
    """The ``<target dev=...>`` of a device XML."""
    return re.search(r"<target dev='([^']+)'", xml).group(1)


class FakeVirsh:
    """
    ``virsh`` as a CDROMManager sees it: ``domblklist --details`` and
    ``dumpxml`` of the live definition, or with ``--inactive`` of the
    persistent one. Every call is recorded, and an ``attach-device`` adds
    its drive to both definitions, as it does for a shut-off domain — or,
    at a target either definition already holds, is refused the way
    libvirt 10.0 refuses it, and recorded in ``refused``.

    ``fail`` maps ``(command, 'live' | 'persistent')`` to the result that
    query returns instead.
    """

    def __init__(self, machine: str | None = Q35, live_machine: str | None = None,
                 live: tuple[str, ...] = (BOOT_DISK,),
                 persistent: tuple[str, ...] | None = None,
                 fail: dict | None = None):
        self.machine = machine
        self.live_machine = machine if live_machine is None else live_machine
        self.live = list(live)
        self.persistent = list(live if persistent is None else persistent)
        self.fail = fail or {}
        self.calls: list[tuple] = []
        self.attached: list[str] = []
        self.refused: list[str] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        scope = "persistent" if "--inactive" in args else "live"
        if (args[0], scope) in self.fail:
            return self.fail[(args[0], scope)]
        if args[0] == "domblklist":
            rows = self.persistent if scope == "persistent" else self.live
            return _result(stdout=BLK_HEADER + "".join(rows))
        if args[0] == "dumpxml":
            machine = self.machine if scope == "persistent" else self.live_machine
            return _result(stdout=_domain_xml(machine))
        if args[0] == "attach-device":
            # the temp XML still exists while attach-device runs
            xml = Path(args[2]).read_text()
            target = _target_of(xml)
            if any(row.split()[2] == target for row in self.live + self.persistent):
                self.refused.append(target)
                return _result(ok=False, return_code=1, stderr=(
                    f"error: Failed to attach device from {args[2]}\n"
                    f"error: Requested operation is not valid: target "
                    f"{target} already exists"))
            self.attached.append(xml)
            row = f"file  cdrom   {target}     /isos/attached.iso\n"
            self.live.append(row)
            self.persistent.append(row)
            return _result()
        raise AssertionError(f"unexpected virsh call: {args!r}")

    def queried(self, command: str) -> bool:
        return any(call[0] == command for call in self.calls)


class TestFindNextAvailableTarget:
    """
    The target a cdrom declared without one gets. Its name fixes the bus
    (see ``_bus_for_target``), so it has to be one the domain's machine
    has: a q35 domain given an IDE ``hdX`` drive is defined without
    complaint and then refuses to start (#217).
    """

    def _pick(self, cd: CDROMManager, virsh: FakeVirsh) -> str | None:
        with patch.object(cd.virsh, "execute", side_effect=virsh):
            return cd._find_next_available_target()

    # i440fx: the old default, unchanged

    def test_returns_hda_when_slots_free(self, cd: CDROMManager):
        # vda is used, so hda is free
        assert self._pick(cd, FakeVirsh(machine=I440FX)) == "hda"

    def test_skips_used_targets(self, cd: CDROMManager):
        virsh = FakeVirsh(machine=I440FX, live=(
            "file  disk    hda     /tmp/x\n",
            "file  disk    hdb     /tmp/y\n"))
        assert self._pick(cd, virsh) == "hdc"

    def test_falls_back_to_sd_when_all_ide_used(self, cd: CDROMManager):
        virsh = FakeVirsh(machine=I440FX, live=tuple(
            f"file  disk    hd{s}     /tmp/{s}\n" for s in "abcd"))
        assert self._pick(cd, virsh) == "sda"

    # q35, and any machine without a built-in IDE controller: SATA only

    def test_q35_gets_a_sata_target(self, cd: CDROMManager):
        target = self._pick(cd, FakeVirsh(machine=Q35))
        assert target == "sda"
        assert cd._bus_for_target(target) == "sata"

    def test_q35_skips_the_templates_empty_seed_drive(self, cd: CDROMManager):
        virsh = FakeVirsh(machine=Q35, live=(BOOT_DISK, EMPTY_SEED_DRIVE))
        assert self._pick(cd, virsh) == "sdb"

    def test_q35_never_falls_back_to_ide(self, cd: CDROMManager):
        """With every SATA target taken nothing is free: an hdX target is
        no slot at all on a machine without an IDE controller."""
        virsh = FakeVirsh(machine=Q35, live=tuple(
            f"file  cdrom   sd{chr(ord('a') + i)}     -\n" for i in range(10)))
        assert self._pick(cd, virsh) is None

    @pytest.mark.parametrize("machine,expected", [
        # the i440fx family, libvirt's test for a built-in IDE controller
        # (qemuDomainHasBuiltinIDE -> qemuDomainIsI440FX)
        ("pc", "hda"),
        ("pc-i440fx-noble", "hda"),
        ("pc-i440fx-rhel7.6.0", "hda"),
        ("pc-1.3", "hda"),
        ("pc-0.15", "hda"),
        ("rhel6.6.0", "hda"),
        # no IDE controller
        ("q35", "sda"),
        ("pc-q35-noble", "sda"),
        ("pc-q35-rhel9.4.0", "sda"),
        ("microvm", "sda"),
    ])
    def test_machine_types(self, cd: CDROMManager, machine, expected):
        assert self._pick(cd, FakeVirsh(machine=machine)) == expected

    def test_the_persistent_machine_type_decides(self, cd: CDROMManager):
        """The persistent definition is what the domain starts with next,
        and a start is where an IDE drive on q35 fails."""
        virsh = FakeVirsh(machine=Q35, live_machine=I440FX)
        assert self._pick(cd, virsh) == "sda"

    # a machine type that cannot be read: SATA, which both machines have

    @pytest.mark.parametrize("dumpxml", [
        _result(ok=False, stderr="error: failed to get domain 'vm01'", return_code=1),
        _result(stdout="not xml"),
        _result(stdout="<domain><devices/></domain>"),
        # a failed query is not read, whatever it printed
        _result(stdout=_domain_xml(I440FX), ok=False, return_code=1),
    ], ids=["query-fails", "not-xml", "no-os-element", "failed-query-output"])
    def test_an_unreadable_machine_type_gets_sata(self, cd: CDROMManager, dumpxml):
        virsh = FakeVirsh(machine=I440FX, fail={("dumpxml", "persistent"): dumpxml})
        assert self._pick(cd, virsh) == "sda"

    def test_a_definition_naming_no_machine_type_gets_sata(self, cd: CDROMManager):
        assert self._pick(cd, FakeVirsh(machine=None)) == "sda"

    # collisions: any device, either definition

    def test_sd_targets_in_use_by_any_device_are_skipped(self, cd: CDROMManager):
        virsh = FakeVirsh(machine=Q35, live=(
            BOOT_DISK, EMPTY_SEED_DRIVE,
            "file  disk    sdb     /data/scsi-disk.qcow2\n"))
        assert self._pick(cd, virsh) == "sdc"

    def test_a_target_only_in_the_persistent_definition_is_taken(
            self, cd: CDROMManager):
        """Plain domblklist of a running domain reports only the live
        definition; a drive attached with --config alone is only in the
        persistent one, and handing its target out again breaks the next
        start."""
        virsh = FakeVirsh(
            machine=I440FX,
            live=(BOOT_DISK, "file  cdrom   hda     -\n"),
            persistent=(BOOT_DISK, "file  cdrom   hda     -\n",
                        "file  cdrom   hdb     /isos/next-boot.iso\n"))
        assert self._pick(cd, virsh) == "hdc"

    def test_a_target_only_in_the_live_definition_is_taken(
            self, cd: CDROMManager):
        virsh = FakeVirsh(
            machine=Q35,
            live=(BOOT_DISK, EMPTY_SEED_DRIVE,
                  "file  cdrom   sdb     /isos/hotplugged.iso\n"),
            persistent=(BOOT_DISK, EMPTY_SEED_DRIVE))
        assert self._pick(cd, virsh) == "sdc"

    # no guessing when the devices in use cannot be read

    @pytest.mark.parametrize("scope", ["live", "persistent"])
    def test_unreadable_block_devices_raise(self, cd: CDROMManager, scope):
        """An empty set would make a failed query look like a domain with
        no devices, and the first target would be handed out whether or
        not it is taken."""
        failed = _result(ok=False, stderr="error: no domain", return_code=1)
        virsh = FakeVirsh(fail={("domblklist", scope): failed})
        with pytest.raises(ProvisionError, match="could not list the block devices"):
            self._pick(cd, virsh)

    def test_a_garbled_device_list_raises(self, cd: CDROMManager):
        garbled = _result(stdout=BLK_HEADER + BOOT_DISK + "file  cdrom\n")
        virsh = FakeVirsh(fail={("domblklist", "persistent"): garbled})
        with pytest.raises(ProvisionError, match="could not read the block devices"):
            self._pick(cd, virsh)


class TestAttachDefaultTarget:
    """attach_cdrom without a target, down to the XML handed to virsh (#217)."""

    def _attach(self, cd: CDROMManager, virsh: FakeVirsh, tmp_path: Path, **kwargs):
        iso = tmp_path / "extra.iso"
        iso.write_bytes(b"iso")
        with patch.object(cd.virsh, "execute", side_effect=virsh):
            return cd.attach_cdrom(str(iso), **kwargs)

    def test_q35_clone_gets_sdb_on_sata(self, cd: CDROMManager, tmp_path: Path):
        virsh = FakeVirsh(machine=Q35, live=(BOOT_DISK, EMPTY_SEED_DRIVE))
        assert self._attach(cd, virsh, tmp_path) is True
        [xml] = virsh.attached
        assert "dev='sdb' bus='sata'" in xml
        assert "bus='ide'" not in xml

    def test_i440fx_still_gets_hda_on_ide(self, cd: CDROMManager, tmp_path: Path):
        virsh = FakeVirsh(machine=I440FX)
        assert self._attach(cd, virsh, tmp_path) is True
        [xml] = virsh.attached
        assert "dev='hda' bus='ide'" in xml

    def test_an_explicit_target_is_honoured_as_given(self, cd: CDROMManager, tmp_path: Path):
        """Even an IDE one on q35: the config named it, and the domain is
        not queried for a default."""
        virsh = FakeVirsh(machine=Q35)
        assert self._attach(cd, virsh, tmp_path, target_dev="hdc") is True
        [xml] = virsh.attached
        assert "dev='hdc' bus='ide'" in xml
        assert not virsh.queried("domblklist")
        assert not virsh.queried("dumpxml")

    def test_nothing_is_attached_when_the_devices_cannot_be_read(
            self, cd: CDROMManager, tmp_path: Path):
        failed = _result(ok=False, stderr="error: no domain", return_code=1)
        virsh = FakeVirsh(fail={("domblklist", "persistent"): failed})
        assert self._attach(cd, virsh, tmp_path) is False
        assert virsh.attached == []


class TestDefaultTargetThroughTheSession:
    """
    The provision and update paths from LibVirtSession down to the XML
    handed to virsh; only virsh itself is faked (#217).
    """

    def _session(self):
        from boxman.providers.libvirt.session import LibVirtSession
        session = LibVirtSession(config={"provider": {"libvirt": {}}})
        session.logger = MagicMock()
        return session

    def _iso(self, tmp_path: Path, name: str) -> str:
        iso = tmp_path / name
        iso.write_bytes(b"iso")
        return str(iso)

    def test_provision_puts_targetless_cdroms_on_sata_on_q35(self, tmp_path: Path):
        virsh = FakeVirsh(machine=Q35, live=(BOOT_DISK, EMPTY_SEED_DRIVE))
        cdroms = [{"name": "tools", "source": self._iso(tmp_path, "tools.iso")},
                  {"name": "data", "source": self._iso(tmp_path, "data.iso")}]
        with patch("boxman.providers.libvirt.cdrom.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.side_effect = virsh
            assert self._session().configure_vm_cdroms("vm01", cdroms) is True
        assert len(virsh.attached) == 2
        assert "dev='sdb' bus='sata'" in virsh.attached[0]
        assert "dev='sdc' bus='sata'" in virsh.attached[1]

    def test_provision_on_i440fx_is_unchanged(self, tmp_path: Path):
        virsh = FakeVirsh(machine=I440FX, live=(BOOT_DISK, "file  cdrom   hda     -\n"))
        cdroms = [{"name": "tools", "source": self._iso(tmp_path, "tools.iso")}]
        with patch("boxman.providers.libvirt.cdrom.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.side_effect = virsh
            assert self._session().configure_vm_cdroms("vm01", cdroms) is True
        [xml] = virsh.attached
        assert "dev='hdb' bus='ide'" in xml

    def test_update_attaches_a_new_targetless_cdrom_on_sata_on_q35(self, tmp_path: Path):
        virsh = FakeVirsh(machine=Q35, live=(BOOT_DISK, EMPTY_SEED_DRIVE))
        new = [{"name": "tools", "source": self._iso(tmp_path, "tools.iso")}]
        with patch("boxman.providers.libvirt.cdrom.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.side_effect = virsh
            assert self._session().update_vm_cdroms(
                vm_name="vm01", new_cdroms=new, removed_cdroms=[],
                changed_cdroms=[], vm_active=False) is True
        [xml] = virsh.attached
        assert "dev='sdb' bus='sata'" in xml


def _source_name_of(xml: str) -> str:
    """The file name of a device XML's ``<source file=...>``."""
    return Path(re.search(r"<source file='([^']+)'", xml).group(1)).name


class TestExplicitTargetsAreReserved:
    """
    A target the same list names explicitly is never handed to a targetless
    entry, whatever the declaration order. Declared first, the targetless
    entry took it, and the explicit attach after it was refused ("target
    sdb already exists"), so the list could not be applied (#217). The
    attach-time counterpart of VMStateDiffer's claimed_targets (#164 FB-5).
    """

    def _session(self):
        from boxman.providers.libvirt.session import LibVirtSession
        session = LibVirtSession(config={"provider": {"libvirt": {}}})
        session.logger = MagicMock()
        return session

    def _cdroms(self, tmp_path: Path, explicit: str) -> list[dict]:
        """A targetless entry declared before one naming *explicit*."""
        for name in ("tools.iso", "data.iso"):
            (tmp_path / name).write_bytes(b"iso")
        return [{"name": "tools", "source": str(tmp_path / "tools.iso")},
                {"name": "data", "source": str(tmp_path / "data.iso"),
                 "target": explicit}]

    def _apply(self, session, path: str, cdroms: list[dict], virsh: FakeVirsh) -> bool:
        with patch("boxman.providers.libvirt.cdrom.VirshCommand") as virsh_cls:
            virsh_cls.return_value.execute.side_effect = virsh
            if path == "provision":
                return session.configure_vm_cdroms("vm01", cdroms)
            return session.update_vm_cdroms(
                vm_name="vm01", new_cdroms=cdroms, removed_cdroms=[],
                changed_cdroms=[], vm_active=False)

    @pytest.mark.parametrize("path", ["provision", "update"])
    @pytest.mark.parametrize("machine,drives,explicit,default", [
        # the template's empty seed drive holds sda
        (Q35, (BOOT_DISK, EMPTY_SEED_DRIVE), "sdb", "sdc"),
        (I440FX, (BOOT_DISK,), "hda", "hdb"),
    ], ids=["q35", "i440fx"])
    def test_a_targetless_cdrom_leaves_a_later_explicit_target_alone(
            self, tmp_path: Path, path, machine, drives, explicit, default):
        virsh = FakeVirsh(machine=machine, live=drives)

        ok = self._apply(self._session(), path, self._cdroms(tmp_path, explicit), virsh)

        assert virsh.refused == []
        assert ok is True
        # in declared order: the targetless entry first, at the first target
        # nobody named, then the explicit one where it asked to be
        assert [(_target_of(xml), _source_name_of(xml)) for xml in virsh.attached] == [
            (default, "tools.iso"), (explicit, "data.iso")]

    def test_provision_keeps_the_declared_order_and_numbering(self, tmp_path: Path):
        virsh = FakeVirsh(machine=Q35, live=(BOOT_DISK, EMPTY_SEED_DRIVE))
        session = self._session()

        assert self._apply(session, "provision", self._cdroms(tmp_path, "sdb"), virsh) is True

        messages = [c.args[0] for c in session.logger.info.call_args_list]
        assert [m for m in messages if m.startswith("configuring CDROM")] == [
            "configuring CDROM 1 ('tools') for VM vm01",
            "configuring CDROM 2 ('data') for VM vm01"]


class TestReservedTargetsInTheManager:
    """CDROMManager's side of the reservation (#217)."""

    def _iso(self, tmp_path: Path) -> str:
        iso = tmp_path / "tools.iso"
        iso.write_bytes(b"iso")
        return str(iso)

    def test_a_reserved_target_is_not_free(self, cd: CDROMManager):
        virsh = FakeVirsh(machine=Q35, live=(BOOT_DISK, EMPTY_SEED_DRIVE))
        with patch.object(cd.virsh, "execute", side_effect=virsh):
            assert cd._find_next_available_target(
                reserved=frozenset({"sdb", "sdc"})) == "sdd"

    def test_attach_cdrom_passes_the_reservation_on(self, cd: CDROMManager, tmp_path: Path):
        virsh = FakeVirsh(machine=Q35, live=(BOOT_DISK, EMPTY_SEED_DRIVE))
        with patch.object(cd.virsh, "execute", side_effect=virsh):
            assert cd.attach_cdrom(self._iso(tmp_path), reserved=frozenset({"sdb"})) is True
        [xml] = virsh.attached
        assert _target_of(xml) == "sdc"

    def test_configure_from_config_passes_the_reservation_on(
            self, cd: CDROMManager, tmp_path: Path):
        virsh = FakeVirsh(machine=Q35, live=(BOOT_DISK, EMPTY_SEED_DRIVE))
        with patch.object(cd.virsh, "execute", side_effect=virsh):
            assert cd.configure_from_config(
                {"name": "tools", "source": self._iso(tmp_path)},
                reserved=frozenset({"sdb"})) is True
        [xml] = virsh.attached
        assert _target_of(xml) == "sdc"

    def test_an_explicit_target_is_used_as_given_though_reserved(
            self, cd: CDROMManager, tmp_path: Path):
        """The reservation holds the list's explicit targets, this entry's
        own among them; it only keeps defaults off them."""
        virsh = FakeVirsh(machine=Q35, live=(BOOT_DISK, EMPTY_SEED_DRIVE))
        with patch.object(cd.virsh, "execute", side_effect=virsh):
            assert cd.configure_from_config(
                {"name": "data", "source": self._iso(tmp_path), "target": "sdb"},
                reserved=frozenset({"sdb"})) is True
        [xml] = virsh.attached
        assert _target_of(xml) == "sdb"
        assert not virsh.queried("domblklist")

    def test_explicit_cdrom_targets_are_the_named_ones(self):
        from boxman.providers.libvirt.cdrom import explicit_cdrom_targets
        assert explicit_cdrom_targets([
            {"name": "tools", "source": "/isos/tools.iso"},
            {"name": "data", "source": "/isos/data.iso", "target": "sdb"},
            {"name": "docs", "source": "/isos/docs.iso", "target": "sdd"},
        ]) == frozenset({"sdb", "sdd"})


class TestAttachCDROM:

    def test_missing_iso_returns_false(self, cd: CDROMManager, captured_logs):
        assert cd.attach_cdrom("/nonexistent.iso") is False
        assert any("ISO file does not exist" in rec.message for rec in captured_logs.records)

    def test_attaches_with_persistent_flag_by_default(self, cd: CDROMManager, tmp_path: Path):
        iso = tmp_path / "ubuntu.iso"
        iso.write_bytes(b"fake iso")
        with patch.object(cd, "_find_next_available_target", return_value="hdc"), \
             patch.object(cd.virsh, "execute", return_value=_result()) as execute:
            assert cd.attach_cdrom(str(iso)) is True
        args, _kwargs = execute.call_args
        assert args[0] == "attach-device"
        assert args[1] == "vm01"
        assert "--persistent" in args

    def test_non_persistent_omits_flag(self, cd: CDROMManager, tmp_path: Path):
        iso = tmp_path / "ubuntu.iso"
        iso.write_bytes(b"fake iso")
        with patch.object(cd, "_find_next_available_target", return_value="hdc"), \
             patch.object(cd.virsh, "execute", return_value=_result()) as execute:
            cd.attach_cdrom(str(iso), persistent=False)
        args, _kwargs = execute.call_args
        assert "--persistent" not in args

    def test_no_available_target_returns_false(self, cd: CDROMManager, tmp_path: Path):
        iso = tmp_path / "u.iso"
        iso.write_bytes(b"x")
        with patch.object(cd, "_find_next_available_target", return_value=None):
            assert cd.attach_cdrom(str(iso)) is False

    def test_command_failure_returns_false(self, cd: CDROMManager, tmp_path: Path):
        iso = tmp_path / "u.iso"
        iso.write_bytes(b"x")
        with patch.object(cd, "_find_next_available_target", return_value="hdc"), \
             patch.object(cd.virsh, "execute", return_value=_result(ok=False, stderr="nope")) as execute:
            assert cd.attach_cdrom(str(iso)) is False
        # warn=True keeps the error branch live — without it execute raises
        # and the `if not result.ok` check is dead code (#85 item 38)
        assert execute.call_args.kwargs.get("warn") is True

    def test_cleans_up_temp_xml_on_exception(self, cd: CDROMManager, tmp_path: Path):
        """If execute raises, the temp XML file must still be removed."""
        iso = tmp_path / "u.iso"
        iso.write_bytes(b"x")
        recorded_path = {}

        orig_nt = __import__("tempfile").NamedTemporaryFile

        def tracker(*a, **kw):
            handle = orig_nt(*a, **kw)
            recorded_path["path"] = handle.name
            return handle

        with patch.object(cd, "_find_next_available_target", return_value="hdc"), \
             patch("boxman.providers.libvirt.cdrom.tempfile.NamedTemporaryFile", side_effect=tracker), \
             patch.object(cd.virsh, "execute", side_effect=ValueError("boom")):
            assert cd.attach_cdrom(str(iso)) is False
        # temp file should have been unlinked
        assert recorded_path["path"] is not None
        assert not Path(recorded_path["path"]).exists()


class TestDetachCDROM:

    def test_success_includes_readonly_xml(self, cd: CDROMManager):
        with patch.object(cd.virsh, "execute", return_value=_result()) as execute:
            assert cd.detach_cdrom("hdc") is True
        # first call is detach-device
        args, _kwargs = execute.call_args
        assert args[0] == "detach-device"

    def test_failure_returns_false(self, cd: CDROMManager):
        with patch.object(cd.virsh, "execute", return_value=_result(ok=False, stderr="x")) as execute:
            assert cd.detach_cdrom("hdc") is False
        # warn=True keeps the error branch live — without it execute raises
        # and the `if not result.ok` check is dead code (#85 item 38)
        assert execute.call_args.kwargs.get("warn") is True


class TestChangeMedia:

    def test_missing_file_returns_false(self, cd: CDROMManager):
        assert cd.change_media("hdc", "/missing.iso") is False

    def test_change_media_happy_path(self, cd: CDROMManager, tmp_path: Path):
        iso = tmp_path / "new.iso"
        iso.write_bytes(b"x")
        with patch.object(cd.virsh, "execute", return_value=_result()) as execute:
            assert cd.change_media("hdc", str(iso)) is True
        args, _kwargs = execute.call_args
        assert args[0] == "change-media"
        assert args[1] == "vm01"
        assert args[2] == "hdc"
        assert "--live" in args
        assert "--config" in args


class TestConfigureFromConfig:

    def test_missing_source_returns_false(self, cd: CDROMManager):
        assert cd.configure_from_config({"name": "iso1"}) is False

    def test_delegates_to_attach_cdrom(self, cd: CDROMManager):
        with patch.object(cd, "attach_cdrom", return_value=True) as attach:
            cd.configure_from_config({"source": "/x.iso", "target": "hdd"})
        attach.assert_called_once_with(
            source_path="/x.iso", target_dev="hdd", reserved=(),
            domain_active=False)


class TestGetAttachedCDROMs:

    def test_parses_domblklist_output(self, cd: CDROMManager):
        out = (
            "Type  Device  Target  Source\n"
            "---------------------------------------------\n"
            "file  cdrom   hdc     /isos/ubuntu.iso\n"
            "file  disk    vda     /disks/vm01.qcow2\n"
            "file  cdrom   hdd     /isos/seed.iso\n"  # seed ISO filtered out
            "file  cdrom   hde     -\n"                # empty drive, reported
        )
        with patch.object(cd.virsh, "execute", return_value=_result(stdout=out)):
            found = cd.get_attached_cdroms()
        assert found == [
            {"target": "hdc", "source": "/isos/ubuntu.iso"},
            {"target": "hde", "source": None},
        ]

    def test_empty_drives_are_reported_not_skipped(self, cd: CDROMManager):
        """
        An empty drive is topology: it is where media gets inserted, not a
        free slot to add a second device to (#164 FB-5).
        """
        out = (
            "Type  Device  Target  Source\n"
            "---------------------------------------------\n"
            "file  cdrom   hdc     -\n"
        )
        with patch.object(cd.virsh, "execute", return_value=_result(stdout=out)):
            assert cd.get_attached_cdroms() == [
                {"target": "hdc", "source": None}]

    def test_query_failure_raises_rather_than_reporting_no_cdroms(
            self, cd: CDROMManager):
        """
        Returning [] made a failed query look like a domain with no CDROMs,
        and the caller then treats every declared cdrom as new (#164 FB-5).
        """
        from boxman.exceptions import ProvisionError

        with patch.object(cd.virsh, "execute", return_value=_result(ok=False)):
            with pytest.raises(ProvisionError, match="could not list"):
                cd.get_attached_cdroms()


class TestXmlEscaping:
    # device XML interpolates paths/names into attributes — a value holding
    # & or a quote must be escaped or libvirt cannot parse it (#85 item 6)

    def test_source_path_is_escaped(self, cd: CDROMManager):
        xml = cd._generate_cdrom_xml("/tmp/a&b's.iso", "hdc")
        assert "file='/tmp/a&amp;b&apos;s.iso'" in xml

    def test_target_is_escaped(self, cd: CDROMManager):
        xml = cd._generate_cdrom_xml("/tmp/foo.iso", 'hd"c')
        assert "dev='hd&quot;c'" in xml

    def test_detach_target_is_escaped(self, cd: CDROMManager):
        written = {}

        def capture(*args, **kwargs):
            written["xml"] = Path(args[2]).read_text()
            return _result()

        with patch.object(cd.virsh, "execute", side_effect=capture):
            assert cd.detach_cdrom("hd&c") is True
        assert "dev='hd&amp;c'" in written["xml"]

    def test_plain_values_are_left_untouched(self, cd: CDROMManager):
        xml = cd._generate_cdrom_xml("/tmp/foo.iso", "hdc")
        assert "file='/tmp/foo.iso'" in xml
        assert "dev='hdc'" in xml
