"""
Docker-compose runtime – commands execute inside a boxman container.

The bundled ``docker-compose.yml`` (shipped inside the package at
``boxman/assets/docker/``) is used when no explicit compose file is provided.
When the bundled assets are used, they are copied to a ``.boxman/runtime/docker``
directory next to the project's ``conf.yml``.
"""

import os
import sys
import time
import shutil
from typing import Dict, Any, Optional

import invoke

from boxman.runtime.base import RuntimeBase
from boxman import log


class DockerComposeRuntime(RuntimeBase):

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.logger = log

        #: str: container name used by ``docker exec``
        self.container_name: str = self.config.get(
            "runtime_container", "boxman-libvirt-default"
        )

        #: str | None: path to the compose file (None → use bundled)
        self.compose_file: Optional[str] = self.config.get("compose_file")

        #: int: max seconds to wait for the container to become ready
        self.ready_timeout: int = self.config.get("ready_timeout", 60)

        #: str | None: the project directory where conf.yml lives;
        #: set by the manager before calling ensure_ready()
        self.project_dir: Optional[str] = self.config.get("project_dir")

    @property
    def name(self) -> str:
        return "docker-compose"

    def wrap_command(self, command: str) -> str:
        """Wrap *command* in a ``docker exec`` invocation."""
        escaped = command.replace("'", "'\\''")
        return f"docker exec {self.container_name} bash -c '{escaped}'"

    def inject_into_provider_config(
        self, provider_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        cfg = super().inject_into_provider_config(provider_config)
        cfg["runtime_container"] = self.container_name
        return cfg

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def ensure_ready(self) -> None:
        """
        Make sure the docker-compose environment is up and healthy.
        """
        if self._container_is_running():
            self.logger.info(
                f"runtime container '{self.container_name}' is already running")
            self._wait_for_libvirtd()
            return

        compose_path = self.get_compose_file_path()
        compose_dir = os.path.dirname(compose_path)

        self.logger.info(
            f"starting docker-compose environment "
            f"(compose file: {compose_path})")

        try:
            # Resolve absolute BOXMAN_DATA_DIR so the entrypoint generates
            # an absolute IdentityFile path in the SSH config.
            # Also pass HOST_UID/HOST_GID so the entrypoint can chown
            # generated SSH keys to be readable by the host user.
            data_dir = os.path.join(compose_dir, "data")
            abs_data_dir = os.path.abspath(data_dir)
            host_uid = os.getuid()
            host_gid = os.getgid()

            invoke.run(
                f"BOXMAN_DATA_DIR={abs_data_dir} "
                f"HOST_UID={host_uid} "
                f"HOST_GID={host_gid} "
                f"docker compose "
                f"-f {compose_path} "
                f"--project-directory {compose_dir} "
                f"up -d --build",
                hide=False,
                warn=False,
            )
        except Exception as exc:
            raise RuntimeError(
                f"failed to start docker-compose environment: {exc}"
            ) from exc

        self._wait_for_container_running()
        self._wait_for_libvirtd()

        self.logger.info(
            f"runtime container '{self.container_name}' is ready")

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
            # always rewrite .env with absolute paths to avoid stale relative refs
            self._write_env_file(local_dir)
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
            # skip the data/ directory — it is created at runtime
            if item == "data":
                continue
            # skip .env — we generate it with absolute paths below
            if item == ".env":
                continue
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

        # write .env with absolute paths
        self._write_env_file(local_dir)

        self.logger.info(f"deployed runtime assets to {local_dir}")
        return local_compose

    def _write_env_file(self, runtime_dir: str) -> None:
        """
        Write a ``.env`` file in *runtime_dir* with absolute paths.

        Docker Compose reads ``.env`` from the ``--project-directory``.
        Using absolute paths ensures volume mounts and entrypoint
        variables resolve correctly regardless of the caller's cwd.
        """
        abs_data_dir = os.path.abspath(os.path.join(runtime_dir, "data"))
        env_path = os.path.join(runtime_dir, ".env")
        host_uid = os.getuid()
        host_gid = os.getgid()

        with open(env_path, "w") as fobj:
            fobj.write(f"BOXMAN_INSTANCE_NAME=default\n")
            fobj.write(f"BOXMAN_DATA_DIR={abs_data_dir}\n")
            fobj.write(f"BOXMAN_SSH_PORT=2222\n")
            fobj.write(f"BOXMAN_LIBVIRT_TCP_PORT=16509\n")
            fobj.write(f"BOXMAN_LIBVIRT_TLS_PORT=16514\n")
            fobj.write(f"HOST_UID={host_uid}\n")
            fobj.write(f"HOST_GID={host_gid}\n")

        self.logger.debug(f"wrote .env with BOXMAN_DATA_DIR={abs_data_dir}")

    @staticmethod
    def _find_asset_source_dir() -> Optional[str]:
        """
        Locate the bundled docker assets directory on disk.

        Checks (in order):
          1. ``boxman/assets/docker/`` via importlib.resources
          2. ``boxman/assets/docker/`` relative to boxman.__file__
          3. ``containers/docker/`` relative to the distribution root
             (poetry includes these files at the top level of the wheel/sdist)
        """
        def _has_compose(d: str) -> bool:
            return os.path.isdir(d) and os.path.isfile(
                os.path.join(d, "docker-compose.yml"))

        try:
            import boxman as _pkg
            pkg_dir = os.path.dirname(os.path.abspath(_pkg.__file__))
            log.debug(f"_find_asset_source_dir: pkg_dir = {pkg_dir}")

            # 1. importlib.resources (Python 3.9+)
            if sys.version_info >= (3, 9):
                from importlib.resources import files
                asset_path = str(files("boxman").joinpath("assets", "docker"))
                log.debug(f"_find_asset_source_dir: importlib candidate = {asset_path}, has_compose = {_has_compose(asset_path)}")
                if _has_compose(asset_path):
                    return asset_path

            # 2. relative to boxman.__file__ → assets/docker/
            candidate = os.path.join(pkg_dir, "assets", "docker")
            log.debug(f"_find_asset_source_dir: assets candidate = {candidate}, has_compose = {_has_compose(candidate)}")
            if _has_compose(candidate):
                return candidate

            # 3. containers/docker/ included by poetry at the distribution root
            #    In an installed package, pkg_dir is <site-packages>/boxman/
            #    and the included files land at <site-packages>/containers/docker/
            #    In an editable install, pkg_dir is <project>/src/boxman/
            #    and the files are at <project>/containers/docker/
            site_root = os.path.dirname(pkg_dir)  # <site-packages> or <project>/src
            for base in [site_root, os.path.dirname(site_root)]:
                candidate = os.path.join(base, "containers", "docker")
                log.debug(f"_find_asset_source_dir: containers candidate = {candidate}, has_compose = {_has_compose(candidate)}")
                if _has_compose(candidate):
                    return candidate

        except Exception as exc:
            log.debug(f"_find_asset_source_dir: exception: {exc}")

        log.debug("_find_asset_source_dir: no asset directory found")
        return None
