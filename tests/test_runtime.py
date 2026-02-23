"""
Tests for boxman.runtime – runtime factory, wrapping, and config injection.
"""

import pytest
from unittest.mock import patch, MagicMock
from boxman.runtime import create_runtime
from boxman.runtime.local import LocalRuntime
from boxman.runtime.docker_compose import DockerComposeRuntime


class TestCreateRuntime:

    def test_local(self):
        rt = create_runtime("local")
        assert isinstance(rt, LocalRuntime)
        assert rt.name == "local"

    def test_docker_compose(self):
        rt = create_runtime("docker-compose")
        assert isinstance(rt, DockerComposeRuntime)
        assert rt.name == "docker-compose"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown runtime"):
            create_runtime("aws-magic")


class TestLocalRuntime:

    def test_wrap_command_is_noop(self):
        rt = LocalRuntime()
        cmd = "virsh list --all"
        assert rt.wrap_command(cmd) == cmd

    def test_inject_sets_runtime_key(self):
        rt = LocalRuntime()
        cfg = rt.inject_into_provider_config({"uri": "qemu:///system"})
        assert cfg["runtime"] == "local"
        assert cfg["uri"] == "qemu:///system"

    def test_ensure_ready_is_noop(self):
        rt = LocalRuntime()
        rt.ensure_ready()  # should not raise


class TestDockerComposeRuntime:

    def test_default_container_name(self):
        rt = DockerComposeRuntime()
        assert rt.container_name == "boxman-libvirt-default"

    def test_custom_container_name(self):
        rt = DockerComposeRuntime(config={"runtime_container": "my-ctr"})
        assert rt.container_name == "my-ctr"

    def test_wrap_command(self):
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})
        wrapped = rt.wrap_command("virsh list --all")
        assert wrapped == "docker exec ctr1 bash -c 'virsh list --all'"

    def test_wrap_command_escapes_single_quotes(self):
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})
        wrapped = rt.wrap_command("echo 'hello world'")
        assert "'\\''" in wrapped  # single quotes are escaped

    def test_inject_sets_runtime_and_container(self):
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})
        cfg = rt.inject_into_provider_config({"uri": "qemu:///system"})
        assert cfg["runtime"] == "docker-compose"
        assert cfg["runtime_container"] == "ctr1"
        assert cfg["uri"] == "qemu:///system"

    def test_inject_does_not_mutate_original(self):
        """inject_into_provider_config must return a copy, not modify the input."""
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})
        original = {"uri": "qemu:///system"}
        enriched = rt.inject_into_provider_config(original)
        assert "runtime" not in original
        assert enriched is not original

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_ensure_ready_skips_compose_up_when_already_running(self, mock_run):
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})

        # first call: docker inspect → running
        # second call: virsh version → ok
        mock_result_running = MagicMock(ok=True, stdout="true\n")
        mock_result_virsh = MagicMock(ok=True)
        mock_run.side_effect = [mock_result_running, mock_result_virsh]

        rt.ensure_ready()

        # should NOT have called docker compose up
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert not any("compose" in c and "up" in c for c in calls)

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_ensure_ready_starts_compose_when_not_running(self, mock_run):
        rt = DockerComposeRuntime(config={
            "runtime_container": "ctr1",
            "compose_file": "/dev/null",  # won't actually be used
            "ready_timeout": 5,
        })

        # Patch get_compose_file_path to avoid file lookup
        with patch.object(rt, "get_compose_file_path", return_value="/tmp/docker-compose.yml"):
            # call sequence:
            # 1. docker inspect → not running
            # 2. docker compose up → ok
            # 3. docker inspect → running (from _wait_for_container_running)
            # 4. virsh version → ok (from _wait_for_libvirtd)
            mock_not_running = MagicMock(ok=True, stdout="false\n")
            mock_compose_up = MagicMock(ok=True)
            mock_running = MagicMock(ok=True, stdout="true\n")
            mock_virsh = MagicMock(ok=True)
            mock_run.side_effect = [
                mock_not_running, mock_compose_up,
                mock_running, mock_virsh,
            ]

            rt.ensure_ready()

            calls = [c.args[0] for c in mock_run.call_args_list]
            assert any("compose" in c and "up" in c for c in calls)

    @patch("boxman.runtime.docker_compose.invoke.run")
    @patch("boxman.runtime.docker_compose.time.sleep")
    def test_ensure_ready_raises_on_timeout(self, mock_sleep, mock_run):
        rt = DockerComposeRuntime(config={
            "runtime_container": "ctr1",
            "ready_timeout": 0,  # immediate timeout
        })

        with patch.object(rt, "get_compose_file_path", return_value="/tmp/docker-compose.yml"):
            mock_not_running = MagicMock(ok=True, stdout="false\n")
            mock_compose_up = MagicMock(ok=True)
            mock_run.side_effect = [mock_not_running, mock_compose_up]

            with pytest.raises(RuntimeError, match="did not start"):
                rt.ensure_ready()

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_ensure_ready_checks_libvirtd_after_container_is_running(self, mock_run):
        """After the container is confirmed running, virsh version must be
        checked before ensure_ready returns successfully."""
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})

        mock_running = MagicMock(ok=True, stdout="true\n")
        mock_virsh = MagicMock(ok=True)
        mock_run.side_effect = [mock_running, mock_virsh]

        rt.ensure_ready()

        calls = [c.args[0] for c in mock_run.call_args_list]
        # the second call must be the virsh health check wrapped in docker exec
        assert "virsh version" in calls[1]
        assert "docker exec" in calls[1]

    def test_compose_file_from_env_var(self, tmp_path, monkeypatch):
        """BOXMAN_COMPOSE_FILE env var should be used when no explicit
        compose_file is configured."""
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("version: '3'\n")
        monkeypatch.setenv("BOXMAN_COMPOSE_FILE", str(compose))

        rt = DockerComposeRuntime()
        # disable bundled asset lookup so env var is tested in isolation
        with patch.object(rt, "_deploy_bundled_assets", return_value=None):
            assert rt.get_compose_file_path() == str(compose)

    def test_compose_file_explicit_config_takes_precedence(self, tmp_path, monkeypatch):
        """Explicit compose_file in config takes precedence over env var."""
        explicit = tmp_path / "explicit.yml"
        explicit.write_text("version: '3'\n")
        env_file = tmp_path / "env.yml"
        env_file.write_text("version: '3'\n")
        monkeypatch.setenv("BOXMAN_COMPOSE_FILE", str(env_file))

        rt = DockerComposeRuntime(config={"compose_file": str(explicit)})
        assert rt.get_compose_file_path() == str(explicit)

    def test_compose_file_missing_raises(self, monkeypatch):
        """FileNotFoundError when no compose file can be found anywhere."""
        # ensure env var doesn't interfere
        monkeypatch.delenv("BOXMAN_COMPOSE_FILE", raising=False)
        rt = DockerComposeRuntime(config={
            "compose_file": "/nonexistent/path/docker-compose.yml"
        })
        with pytest.raises(FileNotFoundError):
            rt.get_compose_file_path()

    def test_compose_file_from_bundled_assets(self, tmp_path):
        """Bundled assets in boxman/assets/docker/ are used as last resort."""
        # Create a fake asset directory
        asset_dir = tmp_path / "assets" / "docker"
        asset_dir.mkdir(parents=True)
        (asset_dir / "docker-compose.yml").write_text("version: '3'\n")
        (asset_dir / "Dockerfile").write_text("FROM scratch\n")
        (asset_dir / "entrypoint.sh").write_text("#!/bin/bash\n")

        # Use a temp project dir so _deploy_bundled_assets doesn't find
        # existing files from a real .boxman/ directory
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        rt = DockerComposeRuntime()
        rt.project_dir = str(project_dir)
        with patch.object(
            rt, "_find_asset_source_dir", return_value=str(asset_dir)
        ):
            result = rt.get_compose_file_path()
            expected = str(project_dir / ".boxman" / "runtime" / "docker" / "docker-compose.yml")
            assert result == expected

    def test_bundled_assets_not_found_falls_through(self, tmp_path, monkeypatch):
        """When bundled assets don't exist and no other source is configured,
        FileNotFoundError is raised."""
        monkeypatch.delenv("BOXMAN_COMPOSE_FILE", raising=False)

        # Use a temp project dir so no existing .boxman/ is found
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        rt = DockerComposeRuntime()
        rt.project_dir = str(project_dir)
        with patch.object(rt, "_find_asset_source_dir", return_value=None):
            with pytest.raises(FileNotFoundError, match="cannot locate"):
                rt.get_compose_file_path()


class TestBoxmanManagerRuntimeIntegration:

    def test_manager_default_runtime_is_local(self):
        from boxman.manager import BoxmanManager
        mgr = BoxmanManager()
        assert mgr.runtime == "local"

    def test_manager_runtime_setter(self):
        from boxman.manager import BoxmanManager
        mgr = BoxmanManager()
        mgr.runtime = "docker-compose"
        assert mgr.runtime == "docker-compose"
        assert mgr.runtime_instance.name == "docker-compose"

    def test_manager_runtime_resets_on_change(self):
        from boxman.manager import BoxmanManager
        mgr = BoxmanManager()
        mgr.runtime = "docker-compose"
        inst1 = mgr.runtime_instance
        mgr.runtime = "local"
        inst2 = mgr.runtime_instance
        assert inst1 is not inst2
        assert inst2.name == "local"

    def test_get_provider_config_with_runtime(self):
        from boxman.manager import BoxmanManager
        mgr = BoxmanManager()
        mgr.runtime = "docker-compose"
        enriched = mgr.get_provider_config_with_runtime({"uri": "qemu:///system"})
        assert enriched["runtime"] == "docker-compose"
        assert enriched["runtime_container"] == "boxman-libvirt-default"
        assert enriched["uri"] == "qemu:///system"

    def test_get_provider_config_local_runtime(self):
        from boxman.manager import BoxmanManager
        mgr = BoxmanManager()
        mgr.runtime = "local"
        enriched = mgr.get_provider_config_with_runtime({"uri": "qemu:///system"})
        assert enriched["runtime"] == "local"
        assert "runtime_container" not in enriched

    def test_ensure_ready_called_on_local_is_noop(self):
        """Local runtime ensure_ready should be a safe no-op."""
        from boxman.manager import BoxmanManager
        mgr = BoxmanManager()
        mgr.runtime = "local"
        mgr.runtime_instance.ensure_ready()  # must not raise


class TestDockerComposeRuntimeBridgeConflict:
    """Tests documenting the bridge name conflict when runtime=docker-compose.

    Root cause (known issue — fix belongs in net.py):
      When the container runs with --privileged and shares the host network
      namespace, ``brctl show`` inside the container returns the **host's**
      bridges. The bridge name allocator (``find_available_bridge_name``)
      picks the next sequential name (e.g. ``virbr1``), but
      ``check_network_exists`` then finds that bridge in ``brctl show``
      output, looks it up in the boxman cache, and sees it belongs to
      another project (e.g. ``surfvpn``), raising a RuntimeError.

    The fix in net.py should:
      1. Use ``virsh net-list --all`` + ``virsh net-dumpxml <net>`` to
         discover bridges managed by libvirt in the **current** runtime,
         rather than relying solely on ``brctl show``.
      2. When runtime=docker-compose, the container's libvirt is a fresh
         instance — it has no networks other than ``default``. The host
         bridges (virbr1..N) are irrelevant.
      3. Alternatively, filter ``brctl show`` output against ``virsh
         net-list`` to only consider bridges that belong to libvirt
         networks in the current runtime scope.
    """

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_brctl_show_sees_host_bridges_inside_container(self, mock_run):
        """Demonstrates that brctl show inside the container returns host bridges."""
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})

        # Simulate brctl show output that includes host bridges
        host_brctl_output = (
            "bridge name\tbridge id\t\tSTP enabled\tinterfaces\n"
            "virbr0\t\t8000.525400d2b101\tyes\n"
            "virbr1\t\t8000.525400001185\tyes\n"  # surfvpn on host
            "virbr2\t\t8000.525400001185\tyes\n"  # ubuntu desktop on host
            "docker0\t\t8000.7e6cfa9db8e5\tno\n"
        )
        mock_result = MagicMock(ok=True, stdout=host_brctl_output)
        mock_run.return_value = mock_result

        wrapped_cmd = rt.wrap_command("brctl show")
        result = mock_run(wrapped_cmd, hide=True, warn=True)

        # The output contains host bridges that don't belong to this runtime
        assert "virbr1" in result.stdout
        assert "virbr2" in result.stdout

    def test_virsh_net_list_should_be_used_for_bridge_discovery(self):
        """Document that virsh net-list is the correct way to discover
        bridges in the current runtime scope.

        Inside a fresh docker-compose container, ``virsh net-list --all``
        returns only the ``default`` network (virbr0). The host bridges
        (virbr1, virbr2, etc.) are NOT libvirt networks in this runtime
        and should be ignored by the bridge allocator.

        This test serves as documentation. The actual fix belongs in
        ``net.py:find_available_bridge_name()`` and
        ``net.py:check_network_exists()``.
        """
        # Expected virsh net-list output inside a fresh container:
        expected_virsh_output = (
            " Name      State    Autostart   Persistent\n"
            "----------------------------------------------\n"
            " default   active   yes         yes\n"
        )
        # Only virbr0 should be considered "taken" — virbr1 should be
        # available for the new project's network.
        assert "default" in expected_virsh_output
