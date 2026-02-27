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
        rt = create_runtime("docker")
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
        assert wrapped == "docker exec --user root ctr1 bash -c 'virsh list --all'"

    def test_wrap_command_escapes_single_quotes(self):
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})
        wrapped = rt.wrap_command("echo 'hello world'")
        assert "'\\''" in wrapped  # single quotes are escaped
        assert "--user root" in wrapped

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

    def test_collect_bind_mount_dirs(self):
        rt = DockerComposeRuntime()
        rt.workdirs = ["/home/user/libvirt-tiny", "/home/user/other-workdir"]
        dirs = rt._collect_bind_mount_dirs("/home/user/my-project")
        assert "/home/user/my-project" in dirs
        assert "/home/user/libvirt-tiny" in dirs
        assert "/home/user/other-workdir" in dirs

    def test_collect_bind_mount_dirs_deduplicates(self):
        rt = DockerComposeRuntime()
        rt.workdirs = ["/home/user/my-project"]
        dirs = rt._collect_bind_mount_dirs("/home/user/my-project")
        # /tmp is always added, so expect 2: the project dir + /tmp
        assert len(dirs) == 2

    def test_inject_bind_mounts_into_compose(self, tmp_path):
        """Bind-mount dirs are added as volume entries in docker-compose.yml."""
        import yaml
        compose_path = tmp_path / "docker-compose.yml"
        compose_data = {
            "services": {
                "boxman-libvirt": {
                    "image": "test",
                    "volumes": ["/sys/fs/cgroup:/sys/fs/cgroup:rw"],
                }
            }
        }
        with open(compose_path, "w") as f:
            yaml.dump(compose_data, f)

        rt = DockerComposeRuntime()
        rt._inject_bind_mounts_into_compose(
            str(compose_path),
            ["/home/user/project", "/home/user/workdir"]
        )

        with open(compose_path) as f:
            result = yaml.safe_load(f)

        vols = result["services"]["boxman-libvirt"]["volumes"]
        assert "/home/user/project:/home/user/project" in vols
        assert "/home/user/workdir:/home/user/workdir" in vols
        # original volume still present
        assert "/sys/fs/cgroup:/sys/fs/cgroup:rw" in vols

    def test_inject_bind_mounts_no_duplicates(self, tmp_path):
        """Already-present bind paths are not added again."""
        import yaml
        compose_path = tmp_path / "docker-compose.yml"
        compose_data = {
            "services": {
                "svc": {
                    "image": "test",
                    "volumes": ["/home/user/project:/home/user/project"],
                }
            }
        }
        with open(compose_path, "w") as f:
            yaml.dump(compose_data, f)

        rt = DockerComposeRuntime()
        rt._inject_bind_mounts_into_compose(
            str(compose_path), ["/home/user/project"]
        )

        with open(compose_path) as f:
            result = yaml.safe_load(f)

        vols = result["services"]["svc"]["volumes"]
        # should still be only 1 entry
        assert vols.count("/home/user/project:/home/user/project") == 1

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_ensure_ready_skips_compose_up_when_already_running(self, mock_run):
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})

        mock_result_running = MagicMock(ok=True, stdout="true\n")
        mock_result_virsh = MagicMock(ok=True)
        # First call: docker inspect (running check)
        # Second call: docker exec test -d (bind dir check) — one per bind dir
        # Last call: virsh version
        mock_run.side_effect = [mock_result_running, mock_result_running, mock_result_running, mock_result_virsh]

        with patch.object(rt, "get_compose_file_path", return_value="/tmp/docker-compose.yml"), \
             patch.object(rt, "_inject_bind_mounts_into_compose"), \
             patch.object(rt, "_write_env_file"), \
             patch.object(rt, "_log_compose_file"):
            rt.ensure_ready()

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert not any("compose" in c and "up" in c for c in calls)

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_ensure_ready_starts_compose_when_not_running(self, mock_run):
        rt = DockerComposeRuntime(config={
            "runtime_container": "ctr1",
            "compose_file": "/dev/null",
            "ready_timeout": 5,
        })

        with patch.object(rt, "get_compose_file_path", return_value="/tmp/docker-compose.yml"), \
             patch.object(rt, "_inject_bind_mounts_into_compose"), \
             patch.object(rt, "_log_compose_file"):
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
            "ready_timeout": 0,
        })

        with patch.object(rt, "get_compose_file_path", return_value="/tmp/docker-compose.yml"), \
             patch.object(rt, "_inject_bind_mounts_into_compose"), \
             patch.object(rt, "_log_compose_file"):
            mock_not_running = MagicMock(ok=True, stdout="false\n")
            mock_compose_up = MagicMock(ok=True)
            mock_run.side_effect = [mock_not_running, mock_compose_up]

            with pytest.raises(RuntimeError, match="did not start"):
                rt.ensure_ready()

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_ensure_ready_checks_libvirtd_after_container_is_running(self, mock_run):
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})

        mock_running = MagicMock(ok=True, stdout="true\n")
        mock_dir_ok = MagicMock(ok=True)
        mock_virsh = MagicMock(ok=True)
        mock_run.side_effect = [mock_running, mock_dir_ok, mock_dir_ok, mock_virsh]

        with patch.object(rt, "get_compose_file_path", return_value="/tmp/docker-compose.yml"), \
             patch.object(rt, "_inject_bind_mounts_into_compose"), \
             patch.object(rt, "_write_env_file"), \
             patch.object(rt, "_log_compose_file"):
            rt.ensure_ready()

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert any("virsh version" in c for c in calls)

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

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_ensure_ready_passes_project_dir_to_compose(self, mock_run):
        """ensure_ready must pass BOXMAN_PROJECT_DIR so the container
        bind-mounts the project directory."""
        rt = DockerComposeRuntime(config={
            "runtime_container": "ctr1",
            "compose_file": "/dev/null",
            "ready_timeout": 5,
        })
        rt.project_dir = "/home/user/my-project"

        with patch.object(rt, "get_compose_file_path", return_value="/tmp/docker-compose.yml"), \
             patch.object(rt, "_inject_bind_mounts_into_compose"), \
             patch.object(rt, "_log_compose_file"):
            mock_not_running = MagicMock(ok=True, stdout="false\n")
            mock_compose_up = MagicMock(ok=True)
            mock_running = MagicMock(ok=True, stdout="true\n")
            mock_virsh = MagicMock(ok=True)
            mock_run.side_effect = [
                mock_not_running, mock_compose_up,
                mock_running, mock_virsh,
            ]

            rt.ensure_ready()

            compose_call = mock_run.call_args_list[1]
            env_kwarg = compose_call.kwargs.get("env", {})
            assert env_kwarg.get("BOXMAN_PROJECT_DIR") == "/home/user/my-project"
            assert "BOXMAN_DATA_DIR" in env_kwarg
            assert "HOST_UID" in env_kwarg
            assert "HOST_GID" in env_kwarg

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_ensure_ready_injects_workdirs_into_compose(self, mock_run):
        """ensure_ready must inject workdirs as bind-mount volumes."""
        rt = DockerComposeRuntime(config={
            "runtime_container": "ctr1",
            "compose_file": "/dev/null",
            "ready_timeout": 5,
        })
        rt.project_dir = "/home/user/my-project"
        rt.workdirs = ["/home/user/libvirt-tiny"]

        inject_calls = []

        def mock_inject(compose_path, bind_dirs):
            inject_calls.append(bind_dirs)

        with patch.object(rt, "get_compose_file_path", return_value="/tmp/docker-compose.yml"), \
             patch.object(rt, "_inject_bind_mounts_into_compose", side_effect=mock_inject), \
             patch.object(rt, "_log_compose_file"):
            mock_not_running = MagicMock(ok=True, stdout="false\n")
            mock_compose_up = MagicMock(ok=True)
            mock_running = MagicMock(ok=True, stdout="true\n")
            mock_virsh = MagicMock(ok=True)
            mock_run.side_effect = [
                mock_not_running, mock_compose_up,
                mock_running, mock_virsh,
            ]

            rt.ensure_ready()

            assert len(inject_calls) == 1
            assert "/home/user/my-project" in inject_calls[0]
            assert "/home/user/libvirt-tiny" in inject_calls[0]


class TestDockerComposeDestroyRuntime:

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_destroy_runtime_cleans_data_dirs_inside_container(self, mock_run):
        """When the container is running, destroy_runtime should exec rm -rf
        inside it before running docker compose down."""
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})
        rt.project_dir = "/tmp/test-project"

        mock_inspect_running = MagicMock(ok=True, stdout="true\n")
        mock_rm = MagicMock(ok=True)
        mock_down = MagicMock(ok=True)
        mock_run.side_effect = [mock_inspect_running, mock_rm, mock_down]

        with patch.object(rt, "get_compose_file_path",
                          return_value="/tmp/test-project/.boxman/runtime/docker/docker-compose.yml"):
            result = rt.destroy_runtime()

        calls = [c.args[0] for c in mock_run.call_args_list]
        # Second call should be the in-container cleanup
        assert "docker exec --user root" in calls[1]
        assert "rm -rf" in calls[1]
        assert "/var/run/libvirt/*" in calls[1]
        assert "/var/lib/libvirt/images/*" in calls[1]
        assert "/etc/boxman/ssh/*" in calls[1]
        # Third call should be docker compose down
        assert "down --volumes --remove-orphans" in calls[2]
        assert result == "/tmp/test-project/.boxman"

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_destroy_runtime_skips_cleanup_when_container_not_running(self, mock_run):
        """When the container is not running, destroy_runtime should skip
        the in-container cleanup and go straight to docker compose down."""
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})
        rt.project_dir = "/tmp/test-project"

        mock_inspect_not_running = MagicMock(ok=True, stdout="false\n")
        mock_down = MagicMock(ok=True)
        mock_run.side_effect = [mock_inspect_not_running, mock_down]

        with patch.object(rt, "get_compose_file_path",
                          return_value="/tmp/test-project/.boxman/runtime/docker/docker-compose.yml"):
            result = rt.destroy_runtime()

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert len(calls) == 2
        # First call is the inspect, second is compose down
        assert "docker inspect" in calls[0]
        assert "down --volumes --remove-orphans" in calls[1]
        assert result == "/tmp/test-project/.boxman"

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_destroy_runtime_continues_if_cleanup_fails(self, mock_run):
        """If the in-container cleanup fails, destroy_runtime should still
        proceed with docker compose down."""
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})
        rt.project_dir = "/tmp/test-project"

        mock_inspect_running = MagicMock(ok=True, stdout="true\n")
        mock_run.side_effect = [
            mock_inspect_running,
            Exception("container exec failed"),
            MagicMock(ok=True),  # compose down
        ]

        with patch.object(rt, "get_compose_file_path",
                          return_value="/tmp/test-project/.boxman/runtime/docker/docker-compose.yml"):
            result = rt.destroy_runtime()

        # Should still return the .boxman path despite cleanup failure
        assert result == "/tmp/test-project/.boxman"

    def test_destroy_runtime_returns_none_when_no_compose_file(self, monkeypatch):
        """When no compose file exists, destroy_runtime returns None."""
        monkeypatch.delenv("BOXMAN_COMPOSE_FILE", raising=False)
        rt = DockerComposeRuntime(config={
            "compose_file": "/nonexistent/docker-compose.yml"
        })
        result = rt.destroy_runtime()
        assert result is None

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_plan_destroy_runtime_with_running_container(self, mock_run, tmp_path):
        """plan_destroy_runtime should list cleanup, compose down, and dir removal."""
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})
        rt.project_dir = str(tmp_path)

        # Create the .boxman dir so the plan detects it
        boxman_dir = tmp_path / ".boxman"
        boxman_dir.mkdir()

        # _container_is_running returns True
        mock_run.return_value = MagicMock(ok=True, stdout="true\n")

        with patch.object(rt, "get_compose_file_path",
                          return_value=str(tmp_path / ".boxman/runtime/docker/docker-compose.yml")):
            plan = rt.plan_destroy_runtime()

        assert len(plan["actions"]) == 3
        assert "clean up" in plan["actions"][0]
        assert "tear down" in plan["actions"][1]
        assert "remove directory" in plan["actions"][2]
        assert len(plan["commands"]) == 2
        assert "docker exec" in plan["commands"][0]
        assert "down --volumes" in plan["commands"][1]
        assert str(boxman_dir) in plan["paths_to_delete"]
        assert plan["container_running"] is True

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_plan_destroy_runtime_container_not_running(self, mock_run, tmp_path):
        """When container is not running, plan should omit the exec cleanup."""
        rt = DockerComposeRuntime(config={"runtime_container": "ctr1"})
        rt.project_dir = str(tmp_path)

        boxman_dir = tmp_path / ".boxman"
        boxman_dir.mkdir()

        mock_run.return_value = MagicMock(ok=True, stdout="false\n")

        with patch.object(rt, "get_compose_file_path",
                          return_value=str(tmp_path / ".boxman/runtime/docker/docker-compose.yml")):
            plan = rt.plan_destroy_runtime()

        assert len(plan["actions"]) == 2
        assert "tear down" in plan["actions"][0]
        assert "remove directory" in plan["actions"][1]
        assert len(plan["commands"]) == 1
        assert "down --volumes" in plan["commands"][0]
        assert plan["container_running"] is False

    def test_plan_destroy_runtime_no_compose_file(self, monkeypatch):
        """When no compose file exists, plan should indicate nothing to do."""
        monkeypatch.delenv("BOXMAN_COMPOSE_FILE", raising=False)
        rt = DockerComposeRuntime(config={
            "compose_file": "/nonexistent/docker-compose.yml"
        })
        plan = rt.plan_destroy_runtime()
        assert "nothing to tear down" in plan["actions"][0]
        assert plan["commands"] == []
        assert plan["paths_to_delete"] == []


class TestManagerDestroyRuntimeCleanup:
    """Tests for the manager-level docker fallback when shutil.rmtree fails
    on root-owned leftover directories."""

    @staticmethod
    def _make_cli_args(auto_accept=True):
        args = MagicMock()
        args.auto_accept = auto_accept
        return args

    @patch("boxman.manager.subprocess.run")
    @patch("boxman.manager.shutil.rmtree")
    @patch("boxman.manager.os.path.isdir")
    def test_docker_fallback_when_boxman_dir_survives_rmtree(
        self, mock_isdir, mock_rmtree, mock_subprocess_run
    ):
        """When shutil.rmtree leaves root-owned dirs behind, the manager
        should use a throwaway docker container to clean them up."""
        from boxman.manager import BoxmanManager

        mgr = BoxmanManager()
        mgr.runtime = "docker-compose"

        mock_runtime = MagicMock(spec=DockerComposeRuntime)
        mock_runtime.name = "docker-compose"
        mock_runtime.destroy_runtime.return_value = "/tmp/proj/.boxman"
        mock_runtime.plan_destroy_runtime.return_value = {
            "actions": ["tear down docker-compose environment"],
            "commands": ["docker compose down --volumes"],
            "paths_to_delete": ["/tmp/proj/.boxman"],
        }
        mgr._runtime_instance = mock_runtime

        # First isdir: True (dir exists), second: True (still exists after
        # rmtree)
        mock_isdir.side_effect = [True, True]

        BoxmanManager.destroy_runtime(mgr, self._make_cli_args())

        # shutil.rmtree called twice: initial attempt + after docker cleanup
        assert mock_rmtree.call_count == 2
        # docker run called with alpine rm -rf
        mock_subprocess_run.assert_called_once()
        docker_cmd = mock_subprocess_run.call_args[0][0]
        assert "docker" == docker_cmd[0]
        assert "alpine" in docker_cmd
        assert "/cleanup/*" in docker_cmd[-1]

    @patch("boxman.manager.subprocess.run")
    @patch("boxman.manager.shutil.rmtree")
    @patch("boxman.manager.os.path.isdir")
    def test_no_docker_fallback_when_rmtree_succeeds(
        self, mock_isdir, mock_rmtree, mock_subprocess_run
    ):
        """When shutil.rmtree fully removes .boxman, no docker fallback."""
        from boxman.manager import BoxmanManager

        mgr = BoxmanManager()
        mgr.runtime = "docker-compose"

        mock_runtime = MagicMock(spec=DockerComposeRuntime)
        mock_runtime.name = "docker-compose"
        mock_runtime.destroy_runtime.return_value = "/tmp/proj/.boxman"
        mock_runtime.plan_destroy_runtime.return_value = {
            "actions": ["tear down docker-compose environment"],
            "commands": ["docker compose down --volumes"],
            "paths_to_delete": ["/tmp/proj/.boxman"],
        }
        mgr._runtime_instance = mock_runtime

        # First isdir: True (dir exists), second: False (rmtree succeeded)
        mock_isdir.side_effect = [True, False]

        BoxmanManager.destroy_runtime(mgr, self._make_cli_args())

        assert mock_rmtree.call_count == 1
        mock_subprocess_run.assert_not_called()

    @patch("builtins.input", return_value="n")
    def test_prompt_aborts_on_no(self, mock_input):
        """When the user answers 'n', destroy_runtime should abort."""
        from boxman.manager import BoxmanManager

        mgr = BoxmanManager()
        mgr.runtime = "docker-compose"

        mock_runtime = MagicMock(spec=DockerComposeRuntime)
        mock_runtime.name = "docker-compose"
        mock_runtime.plan_destroy_runtime.return_value = {
            "actions": ["tear down docker-compose environment"],
            "commands": ["docker compose down --volumes"],
            "paths_to_delete": ["/tmp/proj/.boxman"],
        }
        mgr._runtime_instance = mock_runtime

        BoxmanManager.destroy_runtime(mgr, self._make_cli_args(auto_accept=False))

        mock_input.assert_called_once()
        mock_runtime.destroy_runtime.assert_not_called()

    @patch("boxman.manager.shutil.rmtree")
    @patch("boxman.manager.os.path.isdir", return_value=False)
    @patch("builtins.input", return_value="y")
    def test_prompt_proceeds_on_yes(self, mock_input, mock_isdir, mock_rmtree):
        """When the user answers 'y', destroy_runtime should proceed."""
        from boxman.manager import BoxmanManager

        mgr = BoxmanManager()
        mgr.runtime = "docker-compose"

        mock_runtime = MagicMock(spec=DockerComposeRuntime)
        mock_runtime.name = "docker-compose"
        mock_runtime.destroy_runtime.return_value = "/tmp/proj/.boxman"
        mock_runtime.plan_destroy_runtime.return_value = {
            "actions": ["tear down docker-compose environment"],
            "commands": ["docker compose down --volumes"],
            "paths_to_delete": ["/tmp/proj/.boxman"],
        }
        mgr._runtime_instance = mock_runtime

        BoxmanManager.destroy_runtime(mgr, self._make_cli_args(auto_accept=False))

        mock_input.assert_called_once()
        mock_runtime.destroy_runtime.assert_called_once()

    @patch("boxman.manager.shutil.rmtree")
    @patch("boxman.manager.os.path.isdir", return_value=False)
    def test_auto_accept_skips_prompt(self, mock_isdir, mock_rmtree):
        """With --auto-accept, no input prompt should be shown."""
        from boxman.manager import BoxmanManager

        mgr = BoxmanManager()
        mgr.runtime = "docker-compose"

        mock_runtime = MagicMock(spec=DockerComposeRuntime)
        mock_runtime.name = "docker-compose"
        mock_runtime.destroy_runtime.return_value = "/tmp/proj/.boxman"
        mock_runtime.plan_destroy_runtime.return_value = {
            "actions": ["tear down docker-compose environment"],
            "commands": ["docker compose down --volumes"],
            "paths_to_delete": ["/tmp/proj/.boxman"],
        }
        mgr._runtime_instance = mock_runtime

        with patch("builtins.input") as mock_input:
            BoxmanManager.destroy_runtime(mgr, self._make_cli_args(auto_accept=True))
            mock_input.assert_not_called()

        mock_runtime.destroy_runtime.assert_called_once()


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

    The fix uses ``virsh net-list`` + ``virsh net-dumpxml`` to discover bridges
    managed by libvirt in the **current runtime**, and filters the boxman cache
    by runtime scope. Host bridges visible via ``brctl show`` are no longer
    used for bridge allocation.
    """

    @patch("boxman.runtime.docker_compose.invoke.run")
    def test_brctl_show_sees_host_bridges_inside_container(self, mock_run):
        """Demonstrates that brctl show inside the container returns host bridges,
        which is why virsh net-list is used instead."""
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

    def test_cache_filtering_by_runtime(self):
        """Projects registered under a different runtime should be ignored
        during conflict checks."""
        from boxman.manager import BoxmanManager
        from boxman.providers.libvirt.net import Network
        from unittest.mock import MagicMock, patch

        mgr = BoxmanManager()
        mgr._runtime_name = 'docker-compose'
        mgr.config = {'project': 'test_project'}

        # Simulate cache with a host-runtime project using virbr1
        mgr.cache = MagicMock()
        mgr.cache.projects = {
            'surfvpn': {
                'conf': '/some/path',
                'runtime': 'local',
                'networks': {
                    'surfvpn_nat': {
                        'ip_address': '192.168.12.1',
                        'bridge_name': 'virbr1',
                    }
                }
            }
        }

        info = {'mode': 'nat', 'ip': {'address': '192.168.123.1', 'netmask': '255.255.255.0'}}
        with patch.object(Network, 'find_available_bridge_name', return_value='virbr1'):
            network = Network(
                name='test_net',
                info=info,
                provider_config={},
                manager=mgr,
            )
            network.bridge_name = 'virbr1'

            # Should NOT raise because surfvpn is in 'local' runtime
            # and we are in 'docker-compose' runtime
            network.check_network_exists()  # must not raise

    def test_cache_conflict_same_runtime(self):
        """Projects in the same runtime SHOULD trigger conflicts."""
        from boxman.manager import BoxmanManager
        from boxman.providers.libvirt.net import Network
        from unittest.mock import MagicMock, patch

        mgr = BoxmanManager()
        mgr._runtime_name = 'docker-compose'
        mgr.config = {'project': 'test_project'}

        mgr.cache = MagicMock()
        mgr.cache.projects = {
            'other_project': {
                'conf': '/some/path',
                'runtime': 'docker-compose',
                'networks': {
                    'other_nat': {
                        'ip_address': '192.168.123.1',
                        'bridge_name': 'virbr1',
                    }
                }
            }
        }

        info = {'mode': 'nat', 'ip': {'address': '192.168.123.1', 'netmask': '255.255.255.0'}}
        with patch.object(Network, 'find_available_bridge_name', return_value='virbr1'):
            network = Network(
                name='test_net',
                info=info,
                provider_config={},
                manager=mgr,
            )
            network.bridge_name = 'virbr1'

            # SHOULD raise because other_project is in same runtime
            import pytest
            with pytest.raises(RuntimeError, match="conflicts"):
                network.check_network_exists()

    def test_virsh_net_list_should_be_used_for_bridge_discovery(self):
        """Document that virsh net-list is the correct way to discover
        bridges in the current runtime scope.

        Inside a fresh docker-compose container, ``virsh net-list --all``
        returns only the ``default`` network (virbr0). The host bridges
        (virbr1, virbr2, etc.) are NOT libvirt networks in this runtime
        and should be ignored by the bridge allocator.
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
