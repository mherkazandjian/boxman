"""
Tests for central shell quoting in ``LibVirtCommandBase.build_command``
(#85 item 6).

Every positional argument and keyword VALUE that flows through
``build_command`` is quoted exactly once with ``shlex.quote``. Call sites
must therefore pass raw values (never pre-quoted), and anything needing
real shell constructs (pipes, redirects, ``&&``) must go through
``execute_shell``, which is intentionally not quoted.
"""

from __future__ import annotations

import shlex
from unittest.mock import MagicMock, patch

from boxman.providers.libvirt.commands import (
    LibVirtCommandBase,
    VirshCommand,
    VirtCloneCommand,
    VirtInstallCommand,
)

SHELL_RUN = "boxman.providers.libvirt.commands._shell_run"


def _result(stdout: str = "", ok: bool = True, stderr: str = "") -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = 0
    return r


class TestBuildCommandQuoting:

    def test_value_with_spaces_is_single_token(self):
        cmd = VirshCommand().build_command("snapshot-create-as", "--domain",
                                           "vm 01")
        tokens = shlex.split(cmd)
        assert "vm 01" in tokens
        # exactly one layer of quoting: the raw value survives a round-trip
        assert tokens == ["virsh", "-c", "qemu:///system",
                          "snapshot-create-as", "--domain", "vm 01"]

    def test_value_with_quotes_and_metachars_round_trips(self):
        nasty = 'a"b\'c;d|e&&f>g$(h)`i`'
        cmd = VirshCommand().build_command("dumpxml", nasty)
        assert shlex.split(cmd)[-1] == nasty
        # quoted exactly once — the quoted form appears verbatim
        assert shlex.quote(nasty) in cmd
        assert shlex.quote(shlex.quote(nasty)) not in cmd

    def test_kwarg_value_is_quoted(self):
        cmd = LibVirtCommandBase().build_command(file="/tmp/a b/c.qcow2")
        assert cmd == f"--file={shlex.quote('/tmp/a b/c.qcow2')}"
        # shlex.quote only quotes when needed, so flags stay untouched
        assert LibVirtCommandBase().build_command(size="50M") == "--size=50M"

    def test_kwarg_true_false_none_semantics_unchanged(self):
        cmd = LibVirtCommandBase().build_command(
            auto_clone=True, skip=False, missing=None)
        assert cmd == "--auto-clone"

    def test_safe_tokens_are_not_quoted(self):
        # quoting must be a no-op for ordinary values so existing command
        # strings (and their tests) stay byte-identical
        cmd = VirshCommand().build_command("domstate", "vm01", "--details")
        assert cmd == "virsh -c qemu:///system domstate vm01 --details"

    def test_uri_is_quoted(self):
        v = VirshCommand(provider_config={"uri": "qemu+ssh://user@host/system"})
        assert f"-c {shlex.quote(v.uri)}" in v.build_command("list")

    def test_sudo_prefix_unchanged(self):
        v = VirshCommand(provider_config={"use_sudo": True})
        assert v.build_command("list").startswith("sudo virsh -c ")

    def test_virt_install_uri_kwarg_quoted_once(self):
        v = VirtInstallCommand(provider_config={"uri": "qemu:///system"})
        assert v.build_command() == "virt-install --connect=qemu:///system"

    def test_virt_clone_kwarg_value_with_space(self):
        v = VirtCloneCommand()
        cmd = v.build_command(original="src vm", name="new vm")
        tokens = shlex.split(cmd)
        assert "--original=src vm" in tokens
        assert "--name=new vm" in tokens


class TestExecutePassesQuotedCommandToShell:

    def test_execute_quotes_before_shell_run(self):
        v = VirshCommand()
        with patch(SHELL_RUN, return_value=_result()) as run:
            v.execute("change-media", "vm01", "hd c", "/iso/a b.iso")
        command = run.call_args.args[0]
        tokens = shlex.split(command)
        assert tokens[:4] == ["virsh", "-c", "qemu:///system", "change-media"]
        assert "vm01" in tokens
        assert "hd c" in tokens
        assert "/iso/a b.iso" in tokens


class TestCallSiteAudit:
    """Pin the call-site fixes that central quoting required (#85 item 6)."""

    def test_agent_command_payload_not_double_quoted(self, tmp_path):
        # cloudinit._agent_command used to shlex.quote() the payload itself;
        # with central quoting that produced a double-quoted token
        from boxman.providers.libvirt.cloudinit import CloudInitTemplate
        t = CloudInitTemplate(
            template_name="tpl",
            image_path=str(tmp_path / "base.qcow2"),
            workdir=str(tmp_path / "workdir"),
            provider_config={"use_sudo": False, "uri": "qemu:///system"},
        )
        payload = '{"execute":"guest-exec","arguments":{"path":"/bin/cat"}}'
        with patch(SHELL_RUN, return_value=_result()) as run:
            t._agent_command(payload)
        command = run.call_args.args[0]
        assert shlex.split(command)[-1] == payload
        assert shlex.quote(shlex.quote(payload)) not in command

    def test_snapshot_create_as_description_round_trips(self, tmp_path):
        # snapshot.create_snapshot used to pre-quote the description and
        # pass "--domain <vm>" as one token; both break under central quoting
        from boxman.providers.libvirt.snapshot import SnapshotManager
        sm = SnapshotManager({"use_sudo": False, "uri": "qemu:///system"})
        description = "taken before 'big' upgrade; rm -rf /"
        with patch.object(sm, "_flatten_cdrom_overlays"), \
             patch.object(sm, "_cdrom_diskspec_args", return_value=[]), \
             patch(SHELL_RUN, return_value=_result()) as run:
            assert sm.create_snapshot(
                "vm01", str(tmp_path), "snap1", description) is True
        command = run.call_args.args[0]
        tokens = shlex.split(command)
        assert tokens[:4] == ["virsh", "-c", "qemu:///system",
                              "snapshot-create-as"]
        assert description in tokens

    def test_cdrom_diskspec_args_are_separate_tokens(self):
        from boxman.providers.libvirt.snapshot import SnapshotManager
        sm = SnapshotManager({"use_sudo": False})
        blklist = (
            "Type   Device  Target  Source\n"
            "file   cdrom   hdc     /iso/seed.iso\n"
            "file   disk    vda     /vms/vm.qcow2\n"
        )
        with patch.object(sm.virsh, "execute",
                          return_value=_result(stdout=blklist)):
            args = sm._cdrom_diskspec_args("vm01")
        assert args == ["--diskspec", "hdc,snapshot=no"]
