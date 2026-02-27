"""
Docker-compose runtime – commands execute inside a boxman container.

The bundled ``docker-compose.yml`` (shipped inside the package at
``boxman/assets/docker/``) is used when no explicit compose file is provided.
When the bundled assets are used, they are copied to a ``.boxman/runtime/docker``
directory next to the project's ``conf.yml``.
"""

import os
import re
import sys
import time
import shutil
from typing import Dict, Any, Optional, List

import invoke
import yaml as pyyaml

from boxman.runtime.base import RuntimeBase
from boxman import log


class DockerComposeRuntime(RuntimeBase):

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.logger = log

        #: str | None: project name from conf.yml, used to scope Docker
        #: resources (container, volumes, network) per project.
        #: Set by the manager before calling ensure_ready().
        self._project_name: Optional[str] = self.config.get("project_name")

        #: str | None: path to the compose file (None → use bundled)
        self.compose_file: Optional[str] = self.config.get("compose_file")

        #: int: max seconds to wait for the container to become ready
        self.ready_timeout: int = self.config.get("ready_timeout", 60)

        #: str | None: the project directory where conf.yml lives;
        #: set by the manager before calling ensure_ready()
        self.project_dir: Optional[str] = self.config.get("project_dir")

        #: list[str]: workdirs from conf.yml (one per cluster) that need
        #: to be accessible inside the container; set by the manager
        self.workdirs: list = self.config.get("workdirs", [])

    @property
    def project_name(self) -> Optional[str]:
        """Return the project name used to scope Docker resources."""
        return self._project_name

    @project_name.setter
    def project_name(self, value: Optional[str]) -> None:
        """Set the project name used to scope Docker resources."""
        self._project_name = value

    @staticmethod
    def _sanitize_project_name(name: str) -> str:
        """
        Sanitize a project name for use as a Docker Compose project name.

        Docker Compose project names must be lowercase alphanumeric
        characters and hyphens only.
        """
        return re.sub(r'[^a-z0-9-]', '-', name.lower()).strip('-')

    @property
    def container_name(self) -> str:
        """
        Return the container name, derived from the project name when set.

        Format: ``boxman-libvirt-<sanitized_project_name>``
        Falls back to ``boxman-libvirt-default`` when no project name is set.
        """
        if self._project_name:
            sanitized = self._sanitize_project_name(self._project_name)
            return f"boxman-libvirt-{sanitized}"
        return self.config.get("runtime_container", "boxman-libvirt-default")

    @property
    def _compose_project_name(self) -> str:
        """Return the Docker Compose ``-p`` project name."""
        if self._project_name:
            return f"boxman-{self._sanitize_project_name(self._project_name)}"
        return "boxman-default"

    def _compose_base_cmd(self, compose_path: str, compose_dir: str) -> str:
        """
        Return the base ``docker compose`` command with project scoping.
        """
        return (
            f"docker compose "
            f"-p {self._compose_project_name} "
            f"-f {compose_path} "
            f"--project-directory {compose_dir}"
        )

    @property
    def name(self) -> str:
        return "docker-compose"

    def wrap_command(self, command: str) -> str:
        """Wrap *command* in a ``docker exec`` invocation."""
        escaped = command.replace("'", "'\\''")
        return f"docker exec --user root {self.container_name} bash -c '{escaped}'"

    def inject_into_provider_config(
        self, provider_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        cfg = super().inject_into_provider_config(provider_config)
        cfg["runtime_container"] = self.container_name
        return cfg

    # ------------------------------------------------------------------
    # bind-mount injection
    # ------------------------------------------------------------------
    def _collect_bind_mount_dirs(self, abs_project_dir: str) -> List[str]:
        """
        Collect all unique absolute directories that must be bind-mounted
        into the container: the project directory plus every workdir,
        plus /tmp so that host-side temp files (e.g. XML for virsh define)
        are accessible inside the container.
        """
        dirs = set()
        dirs.add(abs_project_dir)
        dirs.add("/tmp")
        for wd in self.workdirs:
            dirs.add(os.path.abspath(wd))
        return sorted(dirs)

    def _inject_bind_mounts_into_compose(
        self, compose_path: str, bind_dirs: List[str]
    ) -> None:
        """
        Read the docker-compose.yml, add ``path:path`` volume entries for
        each directory in *bind_dirs* (if not already present), and write
        the file back.
        """
        with open(compose_path, "r") as fobj:
            compose = pyyaml.safe_load(fobj)

        # Find the first (and typically only) service
        services = compose.get("services", {})
        if not services:
            self.logger.warning("no services found in docker-compose.yml")
            return

        service_name = next(iter(services))
        service = services[service_name]
        volumes = service.setdefault("volumes", [])

        # Collect existing host-side mount sources for dedup
        existing_sources = set()
        for vol in volumes:
            if isinstance(vol, str) and ":" in vol:
                src = vol.split(":")[0]
                existing_sources.add(src)

        added = []
        for d in bind_dirs:
            if d not in existing_sources:
                entry = f"{d}:{d}"
                volumes.append(entry)
                added.append(entry)

        if added:
            self.logger.info(
                f"injected {len(added)} bind-mount(s) into {compose_path}:")
            for e in added:
                self.logger.info(f"  - {e}")
        else:
            self.logger.info("all bind-mount dirs already present in compose file")

        with open(compose_path, "w") as fobj:
            pyyaml.dump(compose, fobj, default_flow_style=False, sort_keys=False)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def ensure_ready(self) -> None:
        """
        Make sure the docker-compose environment is up and healthy.
        """
        compose_path = self.get_compose_file_path()
        compose_dir = os.path.dirname(compose_path)
        abs_project_dir = os.path.abspath(self.project_dir or os.getcwd())

        # Collect directories to bind-mount and inject them into the
        # docker-compose.yml before starting.
        bind_dirs = self._collect_bind_mount_dirs(abs_project_dir)
        self._inject_bind_mounts_into_compose(compose_path, bind_dirs)

        # Always rewrite .env with the current paths
        self._write_env_file(compose_dir, abs_project_dir)

        # Log the compose file so the user can see what will be started
        self._log_compose_file(compose_path)

        if self._container_is_running():
            # Check that every bind dir is accessible inside the container
            all_accessible = all(
                self._project_dir_accessible(d) for d in bind_dirs
            )
            if all_accessible:
                self.logger.info(
                    f"runtime container '{self.container_name}' is already "
                    f"running and all bind-mount dirs are accessible")
                self._wait_for_libvirtd()
                return
            else:
                self.logger.info(
                    f"some bind-mount dirs are NOT accessible "
                    f"inside container — recreating...")
                self._stop_compose(compose_path, compose_dir)

        self.logger.info(
            f"starting docker-compose environment "
            f"(compose file: {compose_path})")

        try:
            data_dir = os.path.join(compose_dir, "data")
            abs_data_dir = os.path.abspath(data_dir)
            host_uid = os.getuid()
            host_gid = os.getgid()

            env_vars = {
                "BOXMAN_DATA_DIR": abs_data_dir,
                "HOST_UID": str(host_uid),
                "HOST_GID": str(host_gid),
                "BOXMAN_PROJECT_DIR": abs_project_dir,
            }

            self.logger.info("docker compose environment variables:")
            for k, v in env_vars.items():
                self.logger.info(f"  {k}={v}")

            compose_env = os.environ.copy()
            compose_env.update(env_vars)

            invoke.run(
                f"{self._compose_base_cmd(compose_path, compose_dir)} "
                f"up -d --build",
                hide=False,
                warn=False,
                env=compose_env,
            )
        except Exception as exc:
            raise RuntimeError(
                f"failed to start docker-compose environment: {exc}"
            ) from exc

        self._wait_for_container_running()
        self._wait_for_libvirtd()

        self.logger.info(
            f"runtime container '{self.container_name}' is ready")

    def _log_compose_file(self, compose_path: str) -> None:
        """Log the contents of the docker-compose.yml before starting."""
        try:
            with open(compose_path, "r") as fobj:
                contents = fobj.read()
            self.logger.info(
                f"docker-compose.yml ({compose_path}):\n"
                f"{'─' * 60}\n{contents}{'─' * 60}")
        except Exception as exc:
            self.logger.warning(f"could not read compose file for logging: {exc}")

    def _container_is_running(self) -> bool:
        """Return True if the container is in 'running' state."""
        try:
            result = invoke.run(
                f"docker inspect -f '{{{{.State.Running}}}}' "
                f"{self.container_name}",
                hide=True,
                warn=True,
            )
            return result.ok and result.stdout.strip() == "true"
        except Exception:
            return False

    def _wait_for_container_running(self) -> None:
        """Block until the container reaches 'running' state."""
        deadline = time.monotonic() + self.ready_timeout
        interval = 2
        while time.monotonic() < deadline:
            if self._container_is_running():
                self.logger.info(
                    f"container '{self.container_name}' is running")
                return
            self.logger.info(
                f"waiting for container '{self.container_name}' to start...")
            time.sleep(interval)

        raise RuntimeError(
            f"container '{self.container_name}' did not start within "
            f"{self.ready_timeout}s"
        )

    def _wait_for_libvirtd(self) -> None:
        """Block until ``virsh version`` succeeds inside the container."""
        deadline = time.monotonic() + self.ready_timeout
        interval = 3
        while time.monotonic() < deadline:
            check_cmd = self.wrap_command("virsh version")
            result = invoke.run(check_cmd, hide=True, warn=True)
            if result.ok:
                self.logger.info("libvirtd is responsive inside the container")
                return
            self.logger.info(
                "waiting for libvirtd to become responsive...")
            time.sleep(interval)

        raise RuntimeError(
            f"libvirtd inside '{self.container_name}' did not become "
            f"responsive within {self.ready_timeout}s"
        )

    def _project_dir_accessible(self, abs_dir: str) -> bool:
        """Return True if *abs_dir* exists inside the container."""
        try:
            result = invoke.run(
                f"docker exec --user root {self.container_name} "
                f"test -d '{abs_dir}'",
                hide=True,
                warn=True,
            )
            return result.ok
        except Exception:
            return False

    def _stop_compose(self, compose_path: str, compose_dir: str) -> None:
        """Stop the docker-compose environment so it can be recreated."""
        try:
            invoke.run(
                f"{self._compose_base_cmd(compose_path, compose_dir)} "
                f"down",
                hide=False,
                warn=True,
            )
        except Exception as exc:
            self.logger.warning(f"failed to stop compose: {exc}")

    def plan_destroy_runtime(self) -> dict:
        """
        Build a plan describing what ``destroy_runtime`` will do.

        Returns a dict with:
          - **compose_path**: path to the compose file (or None)
          - **container_name**: the container that will be stopped
          - **container_running**: whether the container is currently running
          - **boxman_dir**: the ``.boxman`` directory that will be removed
          - **actions**: ordered list of human-readable action descriptions
          - **commands**: ordered list of shell commands that will be executed
          - **paths_to_delete**: list of paths that will be removed
        """
        plan: dict = {
            "compose_path": None,
            "container_name": self.container_name,
            "container_running": False,
            "boxman_dir": None,
            "actions": [],
            "commands": [],
            "paths_to_delete": [],
        }

        try:
            compose_path = self.get_compose_file_path()
        except FileNotFoundError:
            plan["actions"].append("no compose file found — nothing to tear down")
            return plan

        plan["compose_path"] = compose_path
        compose_dir = os.path.dirname(compose_path)
        running = self._container_is_running()
        plan["container_running"] = running

        if running:
            plan["actions"].append(
                f"clean up root-owned data inside container "
                f"'{self.container_name}'")
            clean_cmd = (
                f"docker exec --user root {self.container_name} "
                f"bash -c 'rm -rf /var/run/libvirt/* "
                f"/var/lib/libvirt/images/* /etc/boxman/ssh/*'")
            plan["commands"].append(clean_cmd)

        down_cmd = (
            f"{self._compose_base_cmd(compose_path, compose_dir)} "
            f"down --volumes --remove-orphans")
        plan["actions"].append(
            f"tear down docker-compose environment "
            f"(stop container, remove volumes & networks)")
        plan["commands"].append(down_cmd)

        base = self.project_dir or os.getcwd()
        boxman_dir = os.path.join(base, ".boxman")
        plan["boxman_dir"] = boxman_dir
        if os.path.isdir(boxman_dir):
            plan["actions"].append(f"remove directory tree {boxman_dir}")
            plan["paths_to_delete"].append(boxman_dir)

        return plan

    def destroy_runtime(self) -> Optional[str]:
        """
        Tear down the Docker Compose environment and remove Docker
        volumes and networks.

        Returns:
            The path to the ``.boxman`` directory (for the caller to
            remove), or None if no compose file was found.
        """
        try:
            compose_path = self.get_compose_file_path()
        except FileNotFoundError:
            self.logger.warning(
                "no compose file found — nothing to tear down")
            return None

        compose_dir = os.path.dirname(compose_path)

        self.logger.info(
            f"destroying docker-compose environment "
            f"(compose file: {compose_path})")

        # Clean up root-owned files inside the container before tearing it
        # down, otherwise shutil.rmtree on the host will silently fail on
        # permission-denied entries (sockets, libvirt state, etc.).
        if self._container_is_running():
            self.logger.info(
                f"cleaning up container data dirs inside "
                f"'{self.container_name}'")
            try:
                invoke.run(
                    f"docker exec --user root {self.container_name} "
                    f"bash -c 'rm -rf /var/run/libvirt/* "
                    f"/var/lib/libvirt/images/* /etc/boxman/ssh/*'",
                    hide=False,
                    warn=True,
                )
            except Exception as exc:
                self.logger.warning(
                    f"in-container cleanup failed: {exc}")

        try:
            invoke.run(
                f"{self._compose_base_cmd(compose_path, compose_dir)} "
                f"down --volumes --remove-orphans",
                hide=False,
                warn=True,
            )
        except Exception as exc:
            self.logger.warning(
                f"docker compose down failed: {exc}")

        # Return the .boxman directory path so the caller can remove it
        base = self.project_dir or os.getcwd()
        return os.path.join(base, ".boxman")

    # ------------------------------------------------------------------
    # compose file resolution
    # ------------------------------------------------------------------
    def get_compose_file_path(self) -> str:
        """
        Return the path to the docker-compose.yml to use.

        Resolution order:
          1. Explicit ``compose_file`` in config.
          2. ``BOXMAN_COMPOSE_FILE`` environment variable.
          3. Bundled assets copied to ``.boxman/runtime/docker``
             next to the project's ``conf.yml``.
        """
        # 1. explicit config
        if self.compose_file:
            path = os.path.expanduser(self.compose_file)
            if os.path.isfile(path):
                return os.path.abspath(path)
            raise FileNotFoundError(
                f"compose file specified in config not found: {path}"
            )

        # 2. environment variable
        env_path = os.environ.get("BOXMAN_COMPOSE_FILE")
        if env_path:
            path = os.path.expanduser(env_path)
            if os.path.isfile(path):
                return os.path.abspath(path)
            raise FileNotFoundError(
                f"BOXMAN_COMPOSE_FILE points to a missing file: {path}"
            )

        # 3. copy bundled assets to .boxman/runtime/docker/ in project dir
        bundled = self._deploy_bundled_assets()
        if bundled:
            return bundled

        raise FileNotFoundError(
            "cannot locate a docker-compose.yml for the boxman runtime. "
            "set 'compose_file' in boxman.yml or the BOXMAN_COMPOSE_FILE "
            "env var."
        )

    def _get_local_runtime_dir(self) -> str:
        """
        Return the path to ``.boxman/runtime/docker/`` relative to the
        project directory (where ``conf.yml`` lives).
        """
        base = self.project_dir or os.getcwd()
        return os.path.join(base, ".boxman", "runtime", "docker")

    def _deploy_bundled_assets(self) -> Optional[str]:
        """
        Copy the bundled docker assets from the package into
        ``.boxman/runtime/docker/`` next to the project's conf.yml.

        Returns:
            Absolute path to the deployed docker-compose.yml, or None
            if the bundled source assets cannot be found.
        """
        local_dir = self._get_local_runtime_dir()
        local_compose = os.path.join(local_dir, "docker-compose.yml")

        # if already deployed, reuse it
        if os.path.isfile(local_compose):
            self.logger.info(
                f"using existing runtime assets in {local_dir}")
            return local_compose

        # find the source assets inside the package
        source_dir = self._find_asset_source_dir()
        if source_dir is None:
            return None

        source_compose = os.path.join(source_dir, "docker-compose.yml")
        if not os.path.isfile(source_compose):
            return None

        # copy all files from the source to the local runtime dir
        self.logger.info(
            f"copying bundled docker assets from {source_dir} → {local_dir}")
        os.makedirs(local_dir, exist_ok=True)

        for item in os.listdir(source_dir):
            src = os.path.join(source_dir, item)
            dst = os.path.join(local_dir, item)
            if item == "data":
                continue
            if item == ".env":
                continue
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

        self.logger.info(f"deployed runtime assets to {local_dir}")
        return local_compose

    def _write_env_file(self, runtime_dir: str,
                        abs_project_dir: str = None) -> None:
        """
        Write a ``.env`` file in *runtime_dir* with absolute paths.
        """
        abs_data_dir = os.path.abspath(os.path.join(runtime_dir, "data"))
        if abs_project_dir is None:
            abs_project_dir = os.path.abspath(self.project_dir or os.getcwd())
        env_path = os.path.join(runtime_dir, ".env")
        host_uid = os.getuid()
        host_gid = os.getgid()

        instance_name = (
            self._sanitize_project_name(self._project_name)
            if self._project_name else "default"
        )

        with open(env_path, "w") as fobj:
            fobj.write(f"BOXMAN_INSTANCE_NAME={instance_name}\n")
            fobj.write(f"BOXMAN_DATA_DIR={abs_data_dir}\n")
            fobj.write(f"BOXMAN_PROJECT_DIR={abs_project_dir}\n")
            fobj.write(f"BOXMAN_SSH_PORT=2222\n")
            fobj.write(f"BOXMAN_LIBVIRT_TCP_PORT=16509\n")
            fobj.write(f"BOXMAN_LIBVIRT_TLS_PORT=16514\n")
            fobj.write(f"HOST_UID={host_uid}\n")
            fobj.write(f"HOST_GID={host_gid}\n")

        self.logger.info(f"wrote .env: BOXMAN_PROJECT_DIR={abs_project_dir}, "
                         f"BOXMAN_DATA_DIR={abs_data_dir}")

    @staticmethod
    def _find_asset_source_dir() -> Optional[str]:
        """
        Locate the bundled docker assets directory on disk.
        """
        def _has_compose(d: str) -> bool:
            return os.path.isdir(d) and os.path.isfile(
                os.path.join(d, "docker-compose.yml"))

        try:
            import boxman as _pkg
            pkg_dir = os.path.dirname(os.path.abspath(_pkg.__file__))
            log.debug(f"_find_asset_source_dir: pkg_dir = {pkg_dir}")

            if sys.version_info >= (3, 9):
                from importlib.resources import files
                asset_path = str(files("boxman").joinpath("assets", "docker"))
                if _has_compose(asset_path):
                    return asset_path

            candidate = os.path.join(pkg_dir, "assets", "docker")
            if _has_compose(candidate):
                return candidate

            site_root = os.path.dirname(pkg_dir)
            for base in [site_root, os.path.dirname(site_root)]:
                candidate = os.path.join(base, "containers", "docker")
                if _has_compose(candidate):
                    return candidate

            # Wheel data-files location: <prefix>/share/boxman/containers/docker
            candidate = os.path.join(sys.prefix, "share", "boxman",
                                     "containers", "docker")
            if _has_compose(candidate):
                return candidate

        except Exception as exc:
            log.debug(f"_find_asset_source_dir: exception: {exc}")

        log.debug("_find_asset_source_dir: no asset directory found")
        return None
