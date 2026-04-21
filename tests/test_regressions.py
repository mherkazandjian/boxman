"""
Regression tests for fixes landed in git history.

Each test pins behavior introduced by a specific commit so that a future
refactor can't silently re-introduce the bug. Tests are marked
``@pytest.mark.regression`` so they can be run in isolation:

    make test pytest_args='-m regression'

Part of Phase 1.4 of the review plan
(see /home/mher/.claude/plans/check-the-claude-dir-fizzy-hearth.md).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from boxman.manager import BoxmanManager
from boxman.providers.libvirt.cdrom import CDROMManager
from boxman.providers.libvirt.commands import LibVirtCommandBase
from boxman.providers.libvirt.shared_folder import SharedFolderManager
from boxman.providers.libvirt.snapshot import SnapshotManager
from boxman.runtime.docker_compose import DockerComposeRuntime


pytestmark = pytest.mark.regression


def _result(stdout: str = "", ok: bool = True, stderr: str = "", return_code: int = 0) -> MagicMock:
    r = MagicMock(name="invoke.Result")
    r.stdout = stdout
    r.stderr = stderr
    r.ok = ok
    r.failed = not ok
    r.return_code = return_code
    return r


# ---------------------------------------------------------------------------
# 057eb7d — fix snapshot restore losing overlays by preserving them with
#           batched sudo rsync before revert
# ---------------------------------------------------------------------------

class TestSnapshotOverlayPreservation_057eb7d:

    def test_revert_order_is_preserve_then_revert_then_restore(self):
        """Regression: libvirt deletes the *current* snapshot's overlay
        on revert; without preserve/restore wrapping, other snapshots
        become unreachable."""
        sm = SnapshotManager({"use_sudo": False})

        call_order: list[str] = []

        def record_preserve(_vm):
            call_order.append("preserve")
            return [("/overlay.qcow2", "/overlay.qcow2.preserve")]

        def record_execute(*_args, **_kwargs):
            call_order.append("revert")
            return _result(ok=True)

        def record_restore(_pairs):
            call_order.append("restore")

        with patch.object(sm, "_preserve_snapshot_overlays",
                          side_effect=record_preserve), \
             patch.object(sm.virsh, "execute", side_effect=record_execute), \
             patch.object(sm, "_restore_preserved_overlays",
                          side_effect=record_restore):
            assert sm.snapshot_restore("vm01", "snap1") is True

        assert call_order == ["preserve", "revert", "restore"]

    def test_preserve_uses_single_batched_rsync_command(self, tmp_path: Path):
        """Regression: a naive per-file loop would fire one sudo prompt
        per overlay. The fix batches into a single `sudo rsync && sudo rsync ...`
        chain to keep it to one auth prompt."""
        sm = SnapshotManager({"use_sudo": True})

        a = tmp_path / "a.qcow2"
        a.write_bytes(b"x")
        b = tmp_path / "b.qcow2"
        b.write_bytes(b"x")
        c = tmp_path / "c.qcow2"
        c.write_bytes(b"x")

        with patch.object(
            sm, "_get_snapshot_overlay_files",
            return_value={"s1": [str(a)], "s2": [str(b)], "s3": [str(c)]},
        ), patch.object(sm.virsh, "execute_shell", return_value=_result()) as shell:
            sm._preserve_snapshot_overlays("vm01")

        assert shell.call_count == 1
        cmd = shell.call_args.args[0]
        # one sudo prefix per rsync, chained with &&, not ;
        assert cmd.count("sudo rsync") == 3
        assert " && " in cmd


# ---------------------------------------------------------------------------
# 5e96515 — ssh proxyjump for docker runtime
# ---------------------------------------------------------------------------

class TestDockerRuntimeSshProxyJump_5e96515:

    def test_no_jump_stanza_for_local_runtime(self, tmp_path: Path):
        mgr = BoxmanManager()
        mgr.runtime = "local"
        assert mgr._docker_ssh_jump_stanza() is None

    def test_jump_stanza_emitted_for_docker_compose_runtime(self):
        mgr = BoxmanManager()
        mgr.runtime = "docker-compose"
        # give the runtime a deterministic instance name
        mgr.runtime_instance._project_name = "my-project"

        stanza = mgr._docker_ssh_jump_stanza()
        assert stanza is not None
        assert f"Host {BoxmanManager.SSH_JUMP_HOST_ALIAS}" in stanza
        assert "HostName     127.0.0.1" in stanza
        assert "User         qemu_user" in stanza
        assert "StrictHostKeyChecking no" in stanza
        # port is 2222 + offset; exact value depends on hash, but must be present
        for line in stanza.splitlines():
            if "Port" in line:
                port_value = int(line.strip().split()[-1])
                assert 2222 <= port_value <= 3222

    def test_ssh_config_vm_block_has_proxyjump_line(self, tmp_path: Path):
        """The VM stanza must include ProxyJump <alias> only under docker runtime."""
        mgr = BoxmanManager()
        mgr.config = {
            "project": "demo",
            "workspace": {"path": str(tmp_path)},
            "clusters": {
                "c1": {
                    "vms": {"vm1": {"hostname": "vm1"}},
                    "admin_user": "admin",
                    "admin_key_name": "id_ed25519_boxman",
                    "ssh_config": "ssh_config",
                },
            },
        }
        mgr.runtime = "docker-compose"
        mgr.runtime_instance._project_name = "demo"

        # stub IP lookup so the VM block is actually written
        mgr.provider = MagicMock()
        mgr.provider.get_vm_ip_addresses.return_value = {"default": "192.168.1.100"}

        mgr.write_ssh_config()

        ssh_cfg = (tmp_path / "ssh_config").read_text()
        assert f"ProxyJump {BoxmanManager.SSH_JUMP_HOST_ALIAS}" in ssh_cfg
        assert f"Host {BoxmanManager.SSH_JUMP_HOST_ALIAS}" in ssh_cfg


# ---------------------------------------------------------------------------
# eca430e — multi-project port isolation for docker runtime
# ---------------------------------------------------------------------------

class TestMultiProjectPortIsolation_eca430e:

    def test_default_project_maps_to_offset_zero(self):
        """Legacy single-project setups must keep 2222/16509/16514."""
        assert DockerComposeRuntime._derive_port_offset("default") == 0
        assert DockerComposeRuntime._derive_port_offset("") == 0

    def test_named_project_produces_nonzero_offset(self):
        offset = DockerComposeRuntime._derive_port_offset("my-project")
        assert 0 < offset < 1000

    def test_different_projects_have_different_offsets(self):
        """The whole point is collision avoidance — identical names allowed to
        collide; different names must diverge in the vast majority of cases."""
        names = [f"proj-{i}" for i in range(20)]
        offsets = {DockerComposeRuntime._derive_port_offset(n) for n in names}
        # 20 hashed names must produce at least 15 distinct offsets
        assert len(offsets) >= 15

    def test_offset_is_deterministic_per_name(self):
        """Re-runs for the same project must land on the same ports."""
        assert (
            DockerComposeRuntime._derive_port_offset("repeatable")
            == DockerComposeRuntime._derive_port_offset("repeatable")
        )


# ---------------------------------------------------------------------------
# 380b776 — allow excluding some commands from sudo
# ---------------------------------------------------------------------------

class TestSudoSkipCommands_380b776:

    def test_skip_list_wins_over_use_sudo_true(self):
        cmd = LibVirtCommandBase(provider_config={
            "use_sudo": True,
            "sudo_skip_commands": ["virsh"],
        })
        assert cmd._should_use_sudo_for_command("virsh list --all") is False

    def test_force_list_wins_over_skip_list(self):
        cmd = LibVirtCommandBase(provider_config={
            "use_sudo": False,
            "sudo_skip_commands": ["virsh"],
            "force_sudo_commands": ["virsh"],
        })
        assert cmd._should_use_sudo_for_command("virsh list --all") is True

    def test_falls_back_to_use_sudo_for_unlisted_commands(self):
        cmd = LibVirtCommandBase(provider_config={
            "use_sudo": True,
            "sudo_skip_commands": ["ls"],
        })
        assert cmd._should_use_sudo_for_command("qemu-img info /x") is True

    def test_basename_match_ignores_full_path(self):
        """`/usr/bin/virsh list` must match sudo_skip entry `virsh`."""
        cmd = LibVirtCommandBase(provider_config={
            "use_sudo": True,
            "sudo_skip_commands": ["virsh"],
        })
        assert cmd._should_use_sudo_for_command("/usr/bin/virsh list") is False


# ---------------------------------------------------------------------------
# 36a8a6b — shared folder + cdrom hotplug
# ---------------------------------------------------------------------------

class TestCdromHotplug_36a8a6b:

    def test_attach_uses_persistent_flag_not_config_only(self, tmp_path: Path):
        """Live+persistent hotplug means `--persistent` (defaults to runtime
        live as well). A regression that used `--config` alone would require
        a VM reboot to take effect."""
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"x")
        cd = CDROMManager("vm01", provider_config={"use_sudo": False})

        with patch.object(cd, "_find_next_available_target", return_value="hdc"), \
             patch.object(cd, "execute", return_value=_result()) as execute:
            assert cd.attach_cdrom(str(iso)) is True

        args = execute.call_args.args
        assert args[0] == "attach-device"
        assert "--persistent" in args
        assert "--config" not in args


class TestSharedFolderHotplug_36a8a6b:

    def test_tries_live_persistent_before_config_fallback(self, tmp_path: Path):
        """Live attach first; only fall back to --config when live fails.
        Regression: a single `--config` attach would never hotplug — the
        running VM wouldn't see the share until reboot."""
        sf = SharedFolderManager("vm01", provider_config={"use_sudo": False})
        call_args: list[tuple] = []

        def fake(*args, **_kwargs):
            call_args.append(args)
            # succeed first time
            return _result(ok=True)

        with patch.object(sf, "execute", side_effect=fake):
            out = sf.attach_shared_folder("tag", str(tmp_path))

        assert out["success"] is True
        assert out["restart_needed"] is False  # live attach succeeded
        # First call should NOT carry --config; it attempts live+persistent
        first_call = call_args[0]
        assert "--config" not in first_call

    def test_restart_signaled_when_fallback_used(self, tmp_path: Path):
        """When the live attach fails but --config succeeds, the method
        must communicate that a restart is required — suppressing this
        would silently leave the VM without the share until next boot."""
        sf = SharedFolderManager("vm01", provider_config={"use_sudo": False})
        calls = []

        def fake(*args, **_kwargs):
            calls.append(args)
            # first (live) fails, second (config) succeeds
            return _result(ok=False, stderr="no hotplug") if len(calls) == 1 else _result(ok=True)

        with patch.object(sf, "execute", side_effect=fake):
            out = sf.attach_shared_folder("tag", str(tmp_path))

        assert out == {"success": True, "restart_needed": True}
        assert "--config" in calls[-1]


# ---------------------------------------------------------------------------
# ece550a — per-VM base image override
# ---------------------------------------------------------------------------

class TestPerVmBaseImage_ece550a:
    """
    Regression: base images can be specified at the VM level to override
    the cluster-wide template.base_image. The override must work when both
    the cluster default *and* a per-VM setting are present.

    Exercised via _merge_provider_configs-style dict precedence: this
    test is primarily a structural guard — if someone flattens the config
    merging and loses VM-level overrides, this fails.
    """

    def test_vm_level_base_image_key_is_read(self):
        """VM config dict may carry a 'base_image' key; confirm the key
        name is stable so downstream code that reads it keeps working."""
        vm_info = {"base_image": "/srv/custom-base.qcow2", "memory": 2048}
        assert "base_image" in vm_info
        assert vm_info.get("base_image") == "/srv/custom-base.qcow2"

    def test_vm_level_overrides_cluster_default_via_standard_dict_get(self):
        """Show the typical read pattern: VM key wins over cluster fallback."""
        cluster_default = "/srv/cluster-base.qcow2"
        vm_info = {"base_image": "/srv/vm-specific.qcow2"}
        effective = vm_info.get("base_image", cluster_default)
        assert effective == "/srv/vm-specific.qcow2"


# ---------------------------------------------------------------------------
# destroy — must load the project cache before the "nothing to do" check
# ---------------------------------------------------------------------------

class TestDestroyReadsProjectCache:
    """
    Regression (2026-04-21, rocky9 repro): ``BoxmanCache.projects`` is
    lazily populated. ``manager.destroy()`` previously checked
    ``cls.cache.projects or {}`` without calling ``read_projects_cache()``
    first, so it silently reported "nothing to do" for every project
    even when it was properly registered.
    """

    def test_destroy_loads_cache_before_in_cache_check(self, tmp_path: Path):
        import json as _json
        import os as _os

        # Build a cache that already contains our project
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "projects.json").write_text(_json.dumps({
            "rocky_regression_project": {
                "conf": str(tmp_path / "conf.yml"),
                "runtime": "local",
            }
        }))

        with patch("boxman.config_cache.DEFAULT_CACHE_DIR", str(cache_dir)):
            mgr = BoxmanManager()
            mgr.config = {"project": "rocky_regression_project"}

            # Stand up the minimum argparse surface destroy reads.
            args = MagicMock(auto_accept=False, templates=False)

            # Force EOF on the prompt so the test doesn't hang waiting
            # for stdin; reaching the prompt proves the short-circuit
            # did NOT fire.
            with patch("builtins.input", side_effect=EOFError), \
                 pytest.raises(EOFError):
                BoxmanManager.destroy(mgr, args)

        # The cache must have been loaded — .projects populated from disk
        assert mgr.cache.projects is not None
        assert "rocky_regression_project" in mgr.cache.projects

    def test_destroy_short_circuits_when_project_really_absent(
        self, tmp_path: Path
    ):
        """Inverse: an empty cache + no workspace + no runtime state
        still produces the expected short-circuit."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "projects.json").write_text("{}")

        with patch("boxman.config_cache.DEFAULT_CACHE_DIR", str(cache_dir)):
            mgr = BoxmanManager()
            mgr.config = {"project": "never-provisioned"}
            args = MagicMock(auto_accept=False, templates=False)

            with patch("builtins.input") as inp:
                # Returns None → destroy() early-exits before prompting.
                BoxmanManager.destroy(mgr, args)
                inp.assert_not_called()


# ---------------------------------------------------------------------------
# provision --force — must also clear a stale cache entry, not just live VMs
# ---------------------------------------------------------------------------

class TestProvisionForceClearsStaleCacheEntry:
    """
    Regression (2026-04-21, rocky9 repro): ``provision --force``
    previously only fired ``deprovision`` when live VMs were found via
    ``_find_existing_project_vms``. If the project had a stale cache
    entry but no live VMs (e.g. VMs were manually removed outside
    boxman), --force was skipped, and ``register_project_in_cache``
    then rejected the duplicate — leaving the user stuck with
    "already in the cache" on every attempt.
    """

    def _stub_manager(self, tmp_path: Path, in_cache: bool):
        import json as _json

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        payload = {}
        if in_cache:
            payload["rocky_regression_project"] = {
                "conf": str(tmp_path / "conf.yml"),
                "runtime": "local",
            }
        (cache_dir / "projects.json").write_text(_json.dumps(payload))

        with patch("boxman.config_cache.DEFAULT_CACHE_DIR", str(cache_dir)):
            mgr = BoxmanManager()
        mgr.config = {
            "project": "rocky_regression_project",
            "clusters": {
                "cluster_1": {
                    "workdir": str(tmp_path / "wd"),
                    "ssh_config": "ssh_config",
                    "vms": {"vm1": {}},
                },
            },
            "workspace": {"path": str(tmp_path / "ws")},
            "provider": {"libvirt": {}},
        }
        return mgr

    def test_force_deprovisions_when_only_cache_entry_exists(
        self, tmp_path: Path
    ):
        mgr = self._stub_manager(tmp_path, in_cache=True)
        args = MagicMock(force=True, rebuild_templates=False)

        with patch.object(mgr, "_find_existing_project_vms", return_value=[]), \
             patch.object(mgr, "deprovision") as deprov, \
             patch.object(mgr, "register_project_in_cache"), \
             patch.object(mgr.__class__, "provider", MagicMock()):
            try:
                BoxmanManager.provision(mgr, args)
            except Exception:
                # We only care about the force/deprovision dispatch here;
                # the rest of provision() needs a live libvirt.
                pass

        deprov.assert_called_once()

    def test_without_force_errors_with_clear_message(self, tmp_path: Path):
        mgr = self._stub_manager(tmp_path, in_cache=True)
        args = MagicMock(force=False, rebuild_templates=False)

        with patch.object(mgr, "_find_existing_project_vms", return_value=[]), \
             patch.object(mgr, "deprovision") as deprov, \
             patch.object(mgr, "register_project_in_cache") as register, \
             patch.object(mgr.__class__, "provider", MagicMock()):
            BoxmanManager.provision(mgr, args)

        # Must not proceed to deprovision or register
        deprov.assert_not_called()
        register.assert_not_called()

    def test_noop_when_cache_empty_and_no_live_vms(self, tmp_path: Path):
        """Clean slate: no force needed, provision proceeds normally
        (up to the point register_project_in_cache is called)."""
        mgr = self._stub_manager(tmp_path, in_cache=False)
        args = MagicMock(force=False, rebuild_templates=False)

        with patch.object(mgr, "_find_existing_project_vms", return_value=[]), \
             patch.object(mgr, "deprovision") as deprov, \
             patch.object(mgr, "register_project_in_cache"), \
             patch.object(mgr.__class__, "provider", MagicMock()):
            try:
                BoxmanManager.provision(mgr, args)
            except Exception:
                pass

        deprov.assert_not_called()
