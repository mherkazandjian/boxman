"""
Docker-compose runtime – commands execute inside a boxman container.

The bundled ``docker-compose.yml`` (shipped inside the package at
``boxman/assets/docker/``) is used when no explicit compose file is provided.
When the bundled assets are used, they are copied to a ``.boxman/runtime/docker``
directory next to the project's ``conf.yml``.
"""

import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from typing import Any

import yaml as pyyaml

from boxman import log
from boxman.exceptions import ProvisionError
from boxman.runtime.base import RuntimeBase
from boxman.runtime.tar_stream import ArchiveError, extract_archive
from boxman.utils.compose_names import sanitize_project_name
from boxman.utils.shell import run as _shell_run


def docker_exec_wrap(command: str, container: str) -> str:
    """
    Wrap *command* in a ``docker exec --user root <container> bash -c '…'``
    invocation, escaping single quotes for the shell round-trip.

    This is the single shared implementation of the docker-exec wrapping —
    used by both :meth:`DockerComposeRuntime.wrap_command` and
    ``LibVirtCommandBase._wrap_for_runtime`` so the two paths cannot drift.
    """
    escaped = command.replace("'", "'\\''")
    return f"docker exec --user root {container} bash -c '{escaped}'"


class DockerComposeRuntime(RuntimeBase):

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.logger = log

        #: str | None: project name from conf.yml, used to scope Docker
        #: resources (container, volumes, network) per project.
        #: Set by the manager before calling ensure_ready().
        self._project_name: str | None = self.config.get("project_name")

        #: str | None: path to the compose file (None → use bundled)
        self.compose_file: str | None = self.config.get("compose_file")

        #: int: max seconds to wait for the container to become ready
        self.ready_timeout: int = self.config.get("ready_timeout", 60)

        #: str | None: the project directory where conf.yml lives;
        #: set by the manager before calling ensure_ready()
        self.project_dir: str | None = self.config.get("project_dir")

        #: list[str]: workdirs from conf.yml (one per cluster) that need
        #: to be accessible inside the container; set by the manager
        self.workdirs: list = self.config.get("workdirs", [])

        #: bool: whether destroying and recreating the runtime container is
        #: authorised even though guests may be running inside it. Default
        #: False; the manager raises it for verbs whose whole purpose is
        #: destruction, and ``--force`` raises it on provision/up.
        self.allow_recreate: bool = bool(
            self.config.get("allow_recreate", False))

    @property
    def project_name(self) -> str | None:
        """Return the project name used to scope Docker resources."""
        return self._project_name

    @project_name.setter
    def project_name(self, value: str | None) -> None:
        """Set the project name used to scope Docker resources."""
        self._project_name = value

    @staticmethod
    def _sanitize_project_name(name: str) -> str:
        """
        Sanitize a project name for use as a Docker Compose project name.

        Thin wrapper over :func:`boxman.utils.compose_names.sanitize_project_name`
        keeping this call site's rule set (no underscores, no fallback).
        """
        return sanitize_project_name(name)

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

        For a user-supplied compose file (anything outside the boxman-owned
        ``.boxman/runtime/docker`` directory), the bind-mount override and
        the ``.env`` file that :meth:`ensure_ready` wrote into ``.boxman``
        are merged in via a second ``-f`` and ``--env-file`` — the user's
        file and directory are never modified.
        """
        cmd = (
            f"docker compose "
            f"-p {self._compose_project_name} "
            f"-f {shlex.quote(compose_path)}"
        )
        if not self._is_boxman_owned_compose(compose_path):
            override = self._bind_mount_override_path()
            if os.path.isfile(override):
                cmd += f" -f {shlex.quote(override)}"
            env_file = os.path.join(self._get_local_runtime_dir(), ".env")
            if os.path.isfile(env_file):
                cmd += f" --env-file {shlex.quote(env_file)}"
        cmd += f" --project-directory {shlex.quote(compose_dir)}"
        return cmd

    def _is_boxman_owned_compose(self, compose_path: str) -> bool:
        """
        Whether *compose_path* is the boxman-deployed copy (inside
        ``.boxman/runtime/docker``) rather than a user-supplied file.
        Only the boxman-owned copy may be mutated in place.
        """
        runtime_dir = os.path.abspath(self._get_local_runtime_dir())
        return os.path.abspath(compose_path).startswith(runtime_dir + os.sep)

    def _bind_mount_override_path(self) -> str:
        """
        Path of the compose override file that carries the injected
        bind-mount volumes for user-supplied compose files. Lives in
        ``.boxman`` — never next to the user's compose file.
        """
        return os.path.join(
            self._get_local_runtime_dir(), "docker-compose.boxman.override.yml")

    @property
    def name(self) -> str:
        return "docker-compose"

    def wrap_command(self, command: str) -> str:
        """Wrap *command* in a ``docker exec`` invocation."""
        return docker_exec_wrap(command, self.container_name)

    def inject_into_provider_config(
        self, provider_config: dict[str, Any]
    ) -> dict[str, Any]:
        cfg = super().inject_into_provider_config(provider_config)
        cfg["runtime_container"] = self.container_name
        return cfg

    # ------------------------------------------------------------------
    # bind-mount injection
    # ------------------------------------------------------------------
    def _collect_bind_mount_dirs(self, abs_project_dir: str) -> list[str]:
        """
        Collect all unique absolute directories that must be bind-mounted
        into the container: the project directory plus every workdir, plus
        the temp directory, so host-side temp files (e.g. the XML written
        for ``virsh define``) are reachable inside the container.

        The temp directory is ``tempfile.gettempdir()``, not the literal
        ``/tmp``: every host-side temp file boxman writes goes through
        ``tempfile``, which honours ``TMPDIR``. With ``TMPDIR`` pointing
        somewhere outside the mounted project and workdirs, the container
        could not see the XML at all (#164 FBN-17). ``/tmp`` is kept as
        well, since it is cheap and some paths still name it directly.
        """
        dirs = set()
        dirs.add(abs_project_dir)
        dirs.add("/tmp")
        dirs.add(os.path.abspath(tempfile.gettempdir()))
        for wd in self.workdirs:
            dirs.add(os.path.abspath(wd))
        return sorted(dirs)

    @staticmethod
    def _declared_mount_pairs(volumes) -> set[tuple[str, str]]:
        """``(source, destination)`` pairs from compose ``volumes:`` entries.

        Only the short string form is understood; a long-form mapping or a
        named volume contributes nothing, which is the safe direction — an
        unrecognised entry means "not already present", so the mount is
        added rather than silently skipped.
        """
        pairs: set[tuple[str, str]] = set()
        for vol in volumes or []:
            if not isinstance(vol, str):
                continue
            parts = vol.split(":")
            if len(parts) >= 2:
                pairs.add((parts[0], parts[1]))
        return pairs

    def _container_mounts(self) -> list[dict] | None:
        """The container's live mount table, or None if it cannot be read.

        None means *unknown*, never *empty*: callers must not read a failed
        inspection as "nothing is mounted".
        """
        try:
            result = _shell_run(
                f"docker inspect -f '{{{{json .Mounts}}}}' "
                f"{self.container_name}",
                hide=True, warn=True,
            )
        except Exception:
            return None
        if not result.ok:
            return None
        try:
            mounts = json.loads(result.stdout.strip() or "null")
        except (ValueError, TypeError):
            return None
        return mounts if isinstance(mounts, list) else None

    @staticmethod
    def _mount_provides(mounts: list[dict],
                        host_path: str,
                        container_path: str,
                        require_writable: bool = True) -> bool:
        """Whether *host_path* is readable at *container_path* in *mounts*.

        A source-only check is not enough: `/var/tmp:/scratch` shares the
        source with the mount boxman needs while leaving the host's temp
        files unreachable at the container's `/var/tmp` (#164 FBN-17).

        A parent mount counts — `/var:/var` does provide `/var/tmp` — but
        only the *most specific* mount covering the path decides, since a
        narrower mount with a different source shadows the parent.
        """
        host_path = os.path.abspath(host_path)
        container_path = os.path.abspath(container_path)

        covering = None
        for mount in mounts:
            dest = mount.get("Destination")
            source = mount.get("Source")
            if not dest or not source:
                continue
            dest = os.path.abspath(dest)
            if container_path == dest or container_path.startswith(
                    dest.rstrip(os.sep) + os.sep):
                # keep the deepest destination — it shadows its parents
                if covering is None or len(dest) > len(covering[0]):
                    covering = (dest, source, mount)

        if covering is None:
            return False

        dest, source, mount = covering
        relative = os.path.relpath(container_path, dest)
        mapped = source if relative == "." else os.path.join(source, relative)
        if os.path.abspath(mapped) != host_path:
            return False
        if require_writable and mount.get("RW") is False:
            return False
        return True

    def _inject_bind_mounts_into_compose(
        self, compose_path: str, bind_dirs: list[str]
    ) -> None:
        """
        Read the docker-compose.yml, add ``path:path`` volume entries for
        each directory in *bind_dirs* (if not already present), and write
        the file back.
        """
        with open(compose_path) as fobj:
            compose = pyyaml.safe_load(fobj)

        # Find the first (and typically only) service
        services = compose.get("services", {})
        if not services:
            self.logger.warning("no services found in docker-compose.yml")
            return

        service_name = next(iter(services))
        service = services[service_name]
        volumes = service.setdefault("volumes", [])

        # Dedup on the (source, destination) pair, not the source alone.
        # boxman needs each host path visible at the *same* path inside the
        # container; an unrelated `/var/tmp:/scratch` shares the source but
        # does not provide that, and a source-only key would suppress the
        # very mount being added (#164 FBN-17).
        existing_pairs = self._declared_mount_pairs(volumes)

        added = []
        for d in bind_dirs:
            if (d, d) not in existing_pairs:
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

    def _write_bind_mount_override(
        self, compose_path: str, bind_dirs: list[str]
    ) -> str | None:
        """
        Write a compose override file carrying the bind-mount volumes for
        a user-supplied compose file, and return its path.

        The user's compose file is only READ (to find the first service
        name and to dedup volumes already present) — boxman never rewrites
        a user-owned file in place (the previous in-place injection
        destroyed comments/formatting via the pyyaml round-trip). The
        override is merged with a second ``-f`` (see
        :meth:`_compose_base_cmd`); compose appends the volumes of the
        same-named service.
        """
        with open(compose_path) as fobj:
            compose = pyyaml.safe_load(fobj)

        services = (compose or {}).get("services", {})
        if not services:
            self.logger.warning("no services found in docker-compose.yml")
            return None

        service_name = next(iter(services))

        # (source, destination) dedup — see _inject_bind_mounts_into_compose
        existing_pairs = self._declared_mount_pairs(
            services[service_name].get("volumes", []) or [])

        added = [f"{d}:{d}" for d in bind_dirs
                 if (d, d) not in existing_pairs]

        override_path = self._bind_mount_override_path()
        os.makedirs(os.path.dirname(override_path), exist_ok=True)
        with open(override_path, "w") as fobj:
            pyyaml.dump(
                {"services": {service_name: {"volumes": added}}},
                fobj, default_flow_style=False, sort_keys=False)

        self.logger.info(
            f"wrote {len(added)} bind-mount(s) for service "
            f"'{service_name}' to override file {override_path} "
            f"(user compose file {compose_path} left untouched)")
        return override_path

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # persisted libvirt state (#164 FB-2, FB-11)
    # ------------------------------------------------------------------

    #: ``(host subdirectory, container path)`` for the trees holding
    #: libvirt's own state: domain and network XML, snapshot metadata,
    #: NVRAM and saved state. ``docker-compose.yml`` bind-mounts both. A
    #: container created before those mounts existed keeps them in its
    #: writable layer instead, where the next recreate discards them.
    _PERSISTED_STATE: tuple[tuple[str, str], ...] = (
        ("etc-libvirt", "/etc/libvirt"),
        ("var-lib-libvirt-qemu", "/var/lib/libvirt/qemu"),
    )

    #: Written into the data dir only once *every* state tree has been
    #: renamed into place. Files at the destination are never the
    #: completion signal — an interrupted copy leaves plenty of them.
    _STATE_MARKER = ".libvirt-state-migrated"

    #: Suffix of the directory a tree is extracted into before its rename.
    _STAGING_SUFFIX = ".staging"

    #: Container states in which no guest can be running. Anything else —
    #: ``paused``, ``restarting``, ``removing``, or a state docker adds
    #: later — is treated as "may have guests", which refuses.
    _STATES_WITHOUT_GUESTS = frozenset({"created", "exited", "dead"})

    #: Marker the in-container probe prints. A count is trusted only when
    #: this line is present: ``docker exec`` overloads exit status 1 for
    #: its own failures, so no exit status may be read as "no guests".
    _GUEST_PROBE_MARKER = "BOXMAN_GUEST_PROBE:"

    #: Counts QEMU processes by **name**, never by command line: matching
    #: the command line would count the probe's own shell, since the
    #: pattern appears in it, and every empty runtime would report a guest.
    #: ``comm`` is truncated to 15 characters, so ``qemu-system-x86_64``
    #: appears as ``qemu-system-x86`` — hence a prefix anchor with no tail.
    #:
    #: ``pgrep`` exits 0 when it matched and 1 when it did not; anything
    #: else (127 for a missing pgrep, 2 usage, 3 fatal) is a probe that did
    #: not complete and exits 9 without printing, so the marker is only
    #: ever emitted after a probe that actually ran.
    _GUEST_PROBE_SCRIPT = (
        'n=$(pgrep -c "^qemu-(kvm|system-)" 2>/dev/null); st=$?; '
        '[ "$st" -eq 0 ] || [ "$st" -eq 1 ] || exit 9; '
        '[ "$st" -eq 1 ] && n=0; '
        'echo "BOXMAN_GUEST_PROBE:${n}"'
    )

    def _data_dir(self) -> str:
        """
        The one derivation of ``BOXMAN_DATA_DIR`` (#164 FB-11).

        Both the ``.env`` file and the environment ``docker compose`` is
        invoked with must name the same directory. They used to disagree
        for a user-supplied compose file — the process environment took it
        from the compose file's directory, ``.env`` from the runtime dir —
        so the container mounted one directory while every host-side
        consumer, the ProxyJump ``IdentityFile`` included, looked in the
        other.
        """
        return os.path.abspath(
            os.path.join(self._get_local_runtime_dir(), "data"))

    def _state_host_dir(self, subdir: str) -> str:
        """Host directory bind-mounted at one of the state trees."""
        return os.path.join(self._data_dir(), subdir)

    def _state_marker_path(self) -> str:
        """Path of the marker that makes a migrated destination trusted."""
        return os.path.join(self._data_dir(), self._STATE_MARKER)

    def _container_state(self) -> str | None:
        """
        The container's docker state, ``""`` when there is no such
        container, or ``None`` when the state could not be determined.

        ``None`` means *unknown* — never *absent*, never *stopped*. A
        caller deciding whether it is safe to destroy a container must not
        read a docker failure as "there is nothing there", which is why
        this exists alongside :meth:`_container_is_running` rather than
        reusing it: that one answers False to both questions.
        """
        try:
            result = _shell_run(
                f"docker ps -a --no-trunc "
                f"--filter name=^{self.container_name}$ "
                f"--format '{{{{.State}}}}'",
                hide=True, warn=True,
            )
        except Exception:
            return None
        if not result.ok:
            return None
        lines = [ln.strip() for ln in (result.stdout or "").splitlines()
                 if ln.strip()]
        return lines[0] if lines else ""

    def _running_guest_count(self) -> int | None:
        """
        Number of QEMU guests running inside the container, or ``None``
        when the probe could not answer.

        Acceptance needs both a clean ``docker exec`` **and** the marker
        line: a non-zero exec status, a missing line or an unparsable
        count are all unknown state, which callers refuse on exactly as
        they would on a positive count.
        """
        try:
            result = _shell_run(
                docker_exec_wrap(
                    self._GUEST_PROBE_SCRIPT, self.container_name),
                hide=True, warn=True,
            )
        except Exception as exc:
            self.logger.warning(f"guest probe could not run: {exc}")
            return None
        if not result.ok:
            return None
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith(self._GUEST_PROBE_MARKER):
                continue
            raw = line[len(self._GUEST_PROBE_MARKER):].strip()
            try:
                return int(raw)
            except ValueError:
                self.logger.warning(
                    f"guest probe returned an unparsable count: {raw!r}")
                return None
        return None

    def _assert_no_running_guests(self, action: str) -> None:
        """
        Refuse *action* while the container may be running guests.

        ``allow_recreate`` is the override, and it is read here rather than
        after anything has been stopped. Unknown state refuses like a
        positive count does: the question "are guests running?" has no safe
        default answer of "no".
        """
        state = self._container_state()
        if state == "":
            return
        if state is None:
            problem = ("boxman could not determine whether the runtime "
                       "container exists")
        elif state in self._STATES_WITHOUT_GUESTS:
            return
        elif state == "running":
            count = self._running_guest_count()
            if count == 0:
                return
            problem = (
                f"{count} QEMU guest(s) are running inside it"
                if count is not None else
                "boxman could not determine whether guests are running "
                "inside it")
        else:
            problem = (
                f"the container is {state!r}, a state in which guests may "
                f"still be running")

        if self.allow_recreate:
            self.logger.warning(
                f"{action} would destroy the runtime container and "
                f"{problem} — proceeding anyway because recreation was "
                f"explicitly authorised")
            return

        raise ProvisionError(
            f"refusing to {action}: it destroys the runtime container "
            f"'{self.container_name}' and {problem}. Shut the guests down "
            f"first (boxman control suspend, or boxman down), or re-run "
            f"with --force to recreate the container regardless.")

    def _state_is_persisted(self) -> bool:
        """
        Whether libvirt's state already lives outside the writable layer.

        A live container answers directly, from its own mount table: if
        both trees are bind-mounted, they survive a recreate and there is
        nothing to migrate. Only when no mount table is available does the
        marker decide.
        """
        mounts = self._container_mounts()
        if mounts is not None:
            return all(
                self._mount_provides(
                    mounts, self._state_host_dir(subdir), container_path)
                for subdir, container_path in self._PERSISTED_STATE
            )
        return os.path.isfile(self._state_marker_path())

    def _interrupted_migration_present(self) -> bool:
        """
        Whether a staged tree from an interrupted migration is lying around.

        This, not "the destination has files in it", is what says a
        migration did not finish. A populated destination is the *normal*
        state: entrypoint.sh seeds both directories on a first run, so any
        instance whose container has since been removed has a populated
        destination and has never migrated anything.

        A staging directory is unambiguous. And it is sufficient: every
        tree is extracted and validated before *any* of them is renamed, so
        once the last rename lands the destination is complete whether or
        not the marker was written. The marker is the belt to this braces.
        """
        return any(
            os.path.exists(self._state_host_dir(subdir)
                           + self._STAGING_SUFFIX)
            for subdir, _ in self._PERSISTED_STATE)

    def _mark_state_persisted(self, reason: str) -> None:
        """Record that the destination is authoritative."""
        os.makedirs(self._data_dir(), exist_ok=True)
        with open(self._state_marker_path(), "w") as fobj:
            fobj.write(f"{reason}\n")

    def _discard_untrusted_state(self) -> None:
        """
        Remove everything at the destination before a migration writes it.

        With the marker absent nothing there is trusted, and that has to
        include trees already renamed into place: the crash window runs
        from the first rename to the marker, so a retry can find one tree
        complete, one staged and no marker. Both are discarded and the
        migration restarts from the container, which is still there
        precisely because the marker does not exist yet.
        """
        for subdir, _ in self._PERSISTED_STATE:
            for path in (self._state_host_dir(subdir),
                         self._state_host_dir(subdir) + self._STAGING_SUFFIX):
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                elif os.path.lexists(path):
                    os.unlink(path)

    def _copy_state_out(self, container_path: str, archive_path: str) -> None:
        """
        Stream *container_path* out of the container as a tar archive.

        ``docker cp`` rather than ``docker exec``: the container is stopped
        by the time this runs, and ``exec`` needs a running one.
        """
        source = f"{self.container_name}:{container_path}"
        result = _shell_run(
            f"docker cp {shlex.quote(source)} - "
            f"> {shlex.quote(archive_path)}",
            hide=True, warn=True,
        )
        if not result.ok:
            raise ProvisionError(
                f"failed to copy {container_path} out of "
                f"'{self.container_name}': "
                f"{(result.stderr or '').strip() or 'docker cp failed'}")

    def _migrate_libvirt_state(self, compose_path: str,
                               compose_dir: str) -> None:
        """
        Move libvirt's state out of the container's writable layer and
        onto the host, so a recreate stops discarding it (#164 FB-2).

        Ordering matters and is the point of the method: authorise, probe,
        **stop** (never ``down`` — the writable layer has to survive until
        the copy is accepted), copy, validate, stage, rename both trees,
        and only then write the marker. Nothing is removed on any failure
        path: a refusal leaves the stopped container holding the only copy.
        """
        self._assert_no_running_guests("migrate libvirt state")

        self.logger.info(
            f"migrating libvirt state out of '{self.container_name}' so it "
            f"survives a container recreate")

        # stop, never down: down removes the container, and with it the
        # writable layer this copy reads from.
        _shell_run(
            f"{self._compose_base_cmd(compose_path, compose_dir)} stop",
            hide=False, warn=True,
        )

        os.makedirs(self._data_dir(), exist_ok=True)
        self._discard_untrusted_state()

        staged: list[tuple[str, str, str]] = []
        with tempfile.TemporaryDirectory(prefix="boxman-libvirt-state-") as tmp:
            for subdir, container_path in self._PERSISTED_STATE:
                archive = os.path.join(tmp, f"{subdir}.tar")
                self._copy_state_out(container_path, archive)
                staging = self._state_host_dir(subdir) + self._STAGING_SUFFIX
                try:
                    extract_archive(archive, staging)
                except ArchiveError as exc:
                    raise ProvisionError(
                        f"the copy of {container_path} out of "
                        f"'{self.container_name}' did not complete: {exc}. "
                        f"The container was left in place and still holds "
                        f"the only copy — re-run the same command to retry."
                    ) from exc
                # docker cp roots the archive at the source's basename
                inner = os.path.join(
                    staging, os.path.basename(container_path))
                if not os.path.isdir(inner):
                    raise ProvisionError(
                        f"the archive of {container_path} does not contain "
                        f"the expected {os.path.basename(container_path)}/ "
                        f"directory; refusing to migrate")
                staged.append((subdir, staging, inner))

        # Every tree is complete on disk before any of them is renamed, so
        # a failure in the second copy cannot leave the first one accepted.
        for subdir, staging, inner in staged:
            os.rename(inner, self._state_host_dir(subdir))
            shutil.rmtree(staging, ignore_errors=True)

        self._mark_state_persisted(
            f"migrated out of container {self.container_name}: "
            f"{', '.join(path for _, path in self._PERSISTED_STATE)}")

        self.logger.info(
            "libvirt state migrated; it now lives on the host and "
            "survives a container recreate")

    def _ensure_state_persisted(self, compose_path: str,
                                compose_dir: str) -> None:
        """
        Bring libvirt's state under the persistence mounts, migrating it
        out of an existing container when it is still in the writable layer.
        """
        if self._state_is_persisted():
            # The live container is mounting the host trees, so they are
            # the authority. Record that, so a later run finding the
            # container gone can tell a seeded install from an interrupted
            # migration. Best-effort only: the mounts already answered the
            # question, and a read-only data dir must not fail the verb.
            if not os.path.isfile(self._state_marker_path()):
                try:
                    self._mark_state_persisted(
                        f"persisted by the mounts of container "
                        f"{self.container_name}")
                except OSError as exc:
                    self.logger.debug(
                        f"could not write {self._STATE_MARKER}: {exc}")
            return

        state = self._container_state()

        if state is None:
            raise ProvisionError(
                f"cannot tell whether the runtime container "
                f"'{self.container_name}' exists, so boxman cannot tell "
                f"whether it holds libvirt state that a recreate would "
                f"destroy. Check that the docker daemon is reachable.")

        if state == "":
            # No container, so nothing to migrate. Files at the destination
            # are the normal case here — entrypoint.sh seeds both trees on
            # a first run — so their presence alone decides nothing.
            if self._interrupted_migration_present():
                raise ProvisionError(
                    f"{self._data_dir()} holds a half-finished migration "
                    f"({self._STAGING_SUFFIX} directories are still there) "
                    f"and no container remains to migrate from, so the "
                    f"state cannot be completed or verified. Inspect it, "
                    f"then either remove the {self._STAGING_SUFFIX} "
                    f"directories to accept what is already in place, or "
                    f"remove the etc-libvirt and var-lib-libvirt-qemu "
                    f"directories to start clean.")
            return

        if state == "running" and self._running_guest_count() != 0:
            # Migrating means stopping the container, which stops the
            # guests. That is the user's call to schedule, not boxman's to
            # force in the middle of an unrelated command.
            self.logger.warning(
                f"libvirt state in '{self.container_name}' is still in the "
                f"container's writable layer and a recreate would discard "
                f"every domain, network and snapshot. Migrating it needs "
                f"the container stopped, and guests are running (or their "
                f"state is unknown). Run 'boxman down', then any boxman "
                f"command, to migrate — or 'boxman up --force' to migrate "
                f"now and recreate regardless.")
            return

        self._migrate_libvirt_state(compose_path, compose_dir)

    def _assert_no_stranded_data_dir(self, compose_dir: str) -> None:
        """
        Refuse to start against an empty data dir while a populated one
        sits where boxman used to derive it (#164 FB-11).

        The two derivations have been unified onto :meth:`_data_dir`, which
        moves the directory for anyone using a compose file outside
        ``.boxman``. Their images and generated SSH identity are still in
        the old one. Moving it is not boxman's call — ``images`` can be
        tens of gigabytes and the destination may be on another filesystem
        — so name the directory instead of silently starting empty.
        """
        legacy = os.path.abspath(os.path.join(compose_dir, "data"))
        current = self._data_dir()
        if legacy == current:
            return
        if not os.path.isdir(legacy) or not os.listdir(legacy):
            return
        if os.path.isdir(current) and os.listdir(current):
            return
        raise ProvisionError(
            f"boxman now keeps its runtime data in {current}, but the "
            f"previous location {legacy} still holds this instance's "
            f"images and SSH identity while the new one is empty. Move it "
            f"before continuing:\n"
            f"    mv {legacy} {current}\n"
            f"(the two disagreed before: the container mounted the old "
            f"path while host-side lookups used the new one — #164 FB-11)")

    def ensure_ready(self) -> None:
        """
        Make sure the docker-compose environment is up and healthy.
        """
        compose_path = self.get_compose_file_path()
        compose_dir = os.path.dirname(compose_path)
        abs_project_dir = os.path.abspath(self.project_dir or os.getcwd())

        # Collect directories to bind-mount and make them visible inside
        # the container before starting.
        # A pure precondition, so it comes before anything is written:
        # refuse to start against an empty data dir while a populated one
        # sits where boxman used to derive it.
        self._assert_no_stranded_data_dir(compose_dir)

        bind_dirs = self._collect_bind_mount_dirs(abs_project_dir)
        if self._is_boxman_owned_compose(compose_path):
            # boxman owns this copy — mutate it in place and drop the
            # .env next to it
            self._inject_bind_mounts_into_compose(compose_path, bind_dirs)
            self._write_env_file(compose_dir, abs_project_dir)
        else:
            # user-supplied compose file — never rewrite it (or drop a
            # .env) in the user's directory; merge via override + env
            # files kept in .boxman
            self._write_bind_mount_override(compose_path, bind_dirs)
            self._write_env_file(
                self._get_local_runtime_dir(), abs_project_dir)

        # Log the compose file so the user can see what will be started
        self._log_compose_file(compose_path)

        # Move libvirt's own state out of the writable layer while the
        # container holding it is still there. This runs after the override
        # and .env files are written, because it issues a compose command
        # and _compose_base_cmd merges both in for a user-supplied compose
        # file — and before anything below, all of which can destroy the
        # container.
        self._ensure_state_persisted(compose_path, compose_dir)

        if self._container_is_running():
            # Check that every bind dir is actually *mounted* at the same
            # path inside the container. `test -d` was not enough: the
            # container has its own /tmp and /var/tmp, so the probe passed
            # for a temp dir that was never bind-mounted and ensure_ready()
            # returned without ever applying the mount (#164 FBN-17).
            mounts = self._container_mounts()
            if mounts is None:
                # The mount table is unreadable — fall back to the weaker
                # existence probe rather than forcing a recreate on a
                # transient `docker inspect` failure.
                self.logger.warning(
                    "could not read the container's mount table; falling "
                    "back to an existence check for the bind dirs")
                all_accessible = all(
                    self._project_dir_accessible(d) for d in bind_dirs
                )
            else:
                all_accessible = all(
                    self._mount_provides(mounts, d, d) for d in bind_dirs
                )
            if all_accessible:
                self.logger.info(
                    f"runtime container '{self.container_name}' is already "
                    f"running and all bind-mount dirs are accessible")
                # A container can be "running" yet have libvirtd holding
                # sockets in a mount namespace whose bind-mount source was
                # unlinked on the host (e.g. after an aborted destroy or
                # `make boxes-clean`). In that state `virsh` inside the
                # container sees an empty /run/libvirt and hangs. Detect
                # the condition with a bounded wait and fall through to
                # stop+recreate instead of propagating the timeout.
                try:
                    self._wait_for_libvirtd()
                    return
                except RuntimeError as exc:
                    self.logger.warning(
                        f"container is running but libvirtd is not "
                        f"responsive ({exc}) — recreating...")
                    self._assert_no_running_guests(
                        "recreate the container because libvirtd is "
                        "unresponsive")
                    self._stop_compose(compose_path, compose_dir)
            else:
                self.logger.info(
                    "some bind-mount dirs are NOT accessible "
                    "inside container — recreating...")
                self._assert_no_running_guests(
                    "recreate the container to apply missing bind mounts")
                self._stop_compose(compose_path, compose_dir)

        self.logger.info(
            f"starting docker-compose environment "
            f"(compose file: {compose_path})")

        # `up -d --build` recreates the container whenever the compose
        # configuration differs from the running one — and adding the
        # libvirt state mounts gave every pre-existing container such a
        # difference. Reaching here with a container that is actually
        # running means _container_is_running() answered False without
        # knowing, since it reports False for a docker failure as readily
        # as for a stopped container. The guard is a no-op for a stopped or
        # absent container, which is the path that normally gets here.
        self._assert_no_running_guests(
            "recreate the container to apply the compose configuration")

        try:
            abs_data_dir = self._data_dir()
            host_uid = os.getuid()
            host_gid = os.getgid()

            env_vars = {
                "BOXMAN_DATA_DIR": abs_data_dir,
                "HOST_UID": str(host_uid),
                "HOST_GID": str(host_gid),
                "BOXMAN_PROJECT_DIR": abs_project_dir,
            }

            self.logger.debug("docker compose environment variables:")
            for k, v in env_vars.items():
                self.logger.debug(f"  {k}={v}")

            compose_env = os.environ.copy()
            compose_env.update(env_vars)

            _shell_run(
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

        # Refuse to proceed if any bind-mount dir is not visible and
        # writable inside the container. Without this check, downstream
        # commands (rsync, qemu-img) would run against directories that
        # only exist in the container's overlay filesystem, producing
        # confusing "No such file or directory" errors after a host-side
        # copy appeared to succeed.
        self.verify_workdirs_accessible(bind_dirs)

        self.logger.info(
            f"runtime container '{self.container_name}' is ready")

    def verify_workdirs_accessible(
        self, bind_dirs: list[str] | None = None
    ) -> None:
        """
        Verify that every bind-mount directory is reachable and writable
        inside the runtime container.

        Raises ``RuntimeError`` listing every path that fails the check so
        the user can correct the docker-compose volume configuration
        before rsync or qemu-img is invoked.
        """
        if bind_dirs is None:
            abs_project_dir = os.path.abspath(
                self.project_dir or os.getcwd())
            bind_dirs = self._collect_bind_mount_dirs(abs_project_dir)

        failures: list[str] = []
        for path in bind_dirs:
            check = _shell_run(
                f"docker exec --user root {self.container_name} "
                f"test -d '{path}' -a -w '{path}'",
                hide=True, warn=True,
            )
            if not check.ok:
                failures.append(path)

        if failures:
            lines = "\n".join(f"  - {p}" for p in failures)
            raise RuntimeError(
                "the following workdirs are not accessible inside "
                f"container '{self.container_name}':\n{lines}\n"
                "each path must exist on the host and be bind-mounted "
                "into the container at the same absolute path. "
                f"check the volumes section of {self.get_compose_file_path()} "
                "and that the host directories exist and are writable."
            )

        self.logger.info(
            f"verified {len(bind_dirs)} workdir(s) accessible inside "
            f"container '{self.container_name}'")

    def _log_compose_file(self, compose_path: str) -> None:
        """Log the contents of the docker-compose.yml before starting."""
        try:
            with open(compose_path) as fobj:
                contents = fobj.read()
            self.logger.info(
                f"docker-compose.yml ({compose_path}):\n{contents}")
        except Exception as exc:
            self.logger.warning(f"could not read compose file for logging: {exc}")

    def _container_is_running(self) -> bool:
        """Return True if the container is in 'running' state."""
        try:
            result = _shell_run(
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
            result = _shell_run(check_cmd, hide=True, warn=True)
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
            result = _shell_run(
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
            _shell_run(
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
            # Done as root inside the container because the host user
            # cannot necessarily remove all of it: libvirtd creates
            # directories of its own while running, and those are root's
            # until the next container start repairs them.
            clean_cmd = (
                f"docker exec --user root {self.container_name} "
                f"bash -c 'rm -rf /var/run/libvirt/* "
                f"/var/lib/libvirt/images/* /etc/boxman/ssh/* "
                f"/etc/libvirt/* /var/lib/libvirt/qemu/*'")
            plan["commands"].append(clean_cmd)

        down_cmd = (
            f"{self._compose_base_cmd(compose_path, compose_dir)} "
            f"down --volumes --remove-orphans")
        plan["actions"].append(
            "tear down docker-compose environment "
            "(stop container, remove volumes & networks)")
        plan["commands"].append(down_cmd)

        base = self.project_dir or os.getcwd()
        boxman_dir = os.path.join(base, ".boxman")
        plan["boxman_dir"] = boxman_dir
        if os.path.isdir(boxman_dir):
            # .boxman now holds libvirt's own state as well as images and
            # keys (#164 FB-2), so removing it destroys every domain,
            # network and snapshot. The confirmation prompt has to say so.
            detail = ""
            if any(os.path.isdir(self._state_host_dir(subdir))
                   for subdir, _ in self._PERSISTED_STATE):
                detail = (" — this includes libvirt's own state, so every "
                          "domain, network and snapshot definition goes "
                          "with it")
            plan["actions"].append(
                f"remove directory tree {boxman_dir}{detail}")
            plan["paths_to_delete"].append(boxman_dir)

        return plan

    def destroy_runtime(self) -> str | None:
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
                _shell_run(
                    f"docker exec --user root {self.container_name} "
                    f"bash -c 'rm -rf /var/run/libvirt/* "
                    f"/var/lib/libvirt/images/* /etc/boxman/ssh/*'",
                    hide=False,
                    warn=True,
                )
            except Exception as exc:
                self.logger.warning(
                    f"in-container cleanup failed: {exc}")

        # Unlike the in-container cleanup above, this one is not best-effort:
        # if the compose project is still up, its containers and volumes
        # survive. Returning the .boxman path anyway told the caller the
        # runtime was gone, and it went on to delete the workspace, the
        # generated files and the cache entry on top of live state.
        try:
            result = _shell_run(
                f"{self._compose_base_cmd(compose_path, compose_dir)} "
                f"down --volumes --remove-orphans",
                hide=False,
                warn=True,
            )
        except Exception as exc:
            raise ProvisionError(
                f"docker compose down failed: {exc}") from exc

        if not getattr(result, "ok", False):
            stderr = (getattr(result, "stderr", "") or "").strip()
            raise ProvisionError(
                f"docker compose down --volumes --remove-orphans failed "
                f"({stderr or 'no stderr'}) — the runtime container, its "
                f"networks and its volumes may still exist")

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

    #: Items in the bundled asset source dir that are never deployed:
    #: ``data`` holds runtime state, ``.env`` holds local overrides.
    _ASSET_DEPLOY_EXCLUDES = ("data", ".env")

    #: Name of the fingerprint file written next to the deployed assets.
    #: Used to detect when the package's bundled assets have changed
    #: (e.g. after a boxman upgrade) so stale copies are redeployed.
    _ASSET_FINGERPRINT_FILE = ".assets-fingerprint"

    @classmethod
    def _assets_fingerprint(cls, source_dir: str) -> str:
        """
        Content fingerprint (sha256) of every deployable file under
        *source_dir*, covering relative paths and file contents.

        Excludes ``_ASSET_DEPLOY_EXCLUDES`` at any depth — the same
        semantics as ``shutil.copytree(ignore=ignore_patterns(...))``
        used by the deploy step, so both always agree on the file set.
        """
        digest = hashlib.sha256()
        for root, dirs, files in os.walk(source_dir):
            dirs[:] = sorted(
                d for d in dirs if d not in cls._ASSET_DEPLOY_EXCLUDES)
            for name in sorted(files):
                if name in cls._ASSET_DEPLOY_EXCLUDES:
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, source_dir)
                # NUL separators keep (path, content) pairs unambiguous
                digest.update(rel.encode() + b"\0")
                with open(path, "rb") as fh:
                    digest.update(fh.read())
                digest.update(b"\0")
        return digest.hexdigest()

    def _deploy_bundled_assets(self) -> str | None:
        """
        Copy the bundled docker assets from the package into
        ``.boxman/runtime/docker/`` next to the project's conf.yml.

        Already-deployed assets are reused only while their fingerprint
        matches the package's bundled assets; a mismatch (e.g. a boxman
        upgrade shipping a fixed Dockerfile) triggers a redeploy so
        fixes actually reach existing projects.

        Returns:
            Absolute path to the deployed docker-compose.yml, or None
            if the bundled source assets cannot be found.
        """
        local_dir = self._get_local_runtime_dir()
        local_compose = os.path.join(local_dir, "docker-compose.yml")

        # find the source assets inside the package
        source_dir = self._find_asset_source_dir()
        if source_dir is None:
            # without a source we cannot verify staleness — fall back to
            # reusing whatever was deployed before
            if os.path.isfile(local_compose):
                self.logger.info(
                    f"using existing runtime assets in {local_dir}")
                return local_compose
            return None

        source_compose = os.path.join(source_dir, "docker-compose.yml")
        if not os.path.isfile(source_compose):
            return None

        # reuse the deployed copy only if it matches the bundled source
        fingerprint_path = os.path.join(
            local_dir, self._ASSET_FINGERPRINT_FILE)
        if os.path.isfile(local_compose) and os.path.isfile(fingerprint_path):
            with open(fingerprint_path) as fh:
                deployed_fingerprint = fh.read().strip()
            if deployed_fingerprint == self._assets_fingerprint(source_dir):
                self.logger.info(
                    f"using existing runtime assets in {local_dir}")
                return local_compose
            self.logger.info(
                f"bundled runtime assets changed; redeploying to {local_dir}")

        # copy all files from the source to the local runtime dir.
        # Deployed entries that no longer exist in the source are
        # removed first (except runtime state) so stale files cannot
        # linger while the fingerprint reports "in sync".
        self.logger.info(
            f"copying bundled docker assets from {source_dir} → {local_dir}")
        os.makedirs(local_dir, exist_ok=True)

        keep = set(self._ASSET_DEPLOY_EXCLUDES) | {self._ASSET_FINGERPRINT_FILE}
        for item in os.listdir(local_dir):
            if item in keep:
                continue
            stale = os.path.join(local_dir, item)
            if os.path.isdir(stale) and not os.path.islink(stale):
                shutil.rmtree(stale)
            else:
                os.remove(stale)

        shutil.copytree(
            source_dir, local_dir,
            ignore=shutil.ignore_patterns(*self._ASSET_DEPLOY_EXCLUDES),
            dirs_exist_ok=True)

        with open(fingerprint_path, "w") as fh:
            fh.write(self._assets_fingerprint(source_dir))

        self.logger.info(f"deployed runtime assets to {local_dir}")
        return local_compose

    @staticmethod
    def _derive_port_offset(instance_name: str) -> int:
        """
        Return a deterministic 0–999 offset derived from *instance_name*.

        Without this, every boxman project on the same host would try to
        bind the same SSH / libvirt TCP / TLS host ports and collide.
        The ``default`` instance always maps to offset 0 so legacy
        single-project setups keep their 2222 / 16509 / 16514 ports.
        """
        if not instance_name or instance_name == "default":
            return 0
        # usedforsecurity=False: this is a name-to-port hash, not a
        # security primitive. Without it a FIPS-enabled host (fips=1 on
        # RHEL/Rocky) raises ValueError from the OpenSSL backend and every
        # docker-runtime command dies with a traceback (#164 FB-13).
        digest = hashlib.md5(
            instance_name.encode(), usedforsecurity=False).hexdigest()
        return int(digest[:4], 16) % 1000

    @staticmethod
    def _ports_for_offset(offset: int) -> tuple[int, int, int]:
        """Return ``(ssh, libvirt_tcp, libvirt_tls)`` for a port *offset*.

        The two libvirt families are strided by 2 so they can never land on
        the same port. With a plain ``base + offset`` the TCP and TLS bases
        differ by 5, so two instances whose offsets differ by 5 collided —
        one instance's TLS port was another's TCP port (#164 FBN-15).
        Striding makes a collision require ``2a - 2b == 5``, which has no
        integer solution.

        Offset 0 still yields 2222 / 16509 / 16514, so the ``default``
        instance is unchanged.
        """
        return 2222 + offset, 16509 + offset * 2, 16514 + offset * 2

    def _instance_name(self) -> str:
        """Return the sanitised instance name used for port derivation
        and the compose/container naming scheme."""
        if self._project_name:
            return self._sanitize_project_name(self._project_name)
        return "default"

    @property
    def ssh_port(self) -> int:
        """The host port on which the libvirt container's sshd is
        published (``127.0.0.1:<ssh_port> → container:22``)."""
        return self._ports_for_offset(
            self._derive_port_offset(self._instance_name()))[0]

    @property
    def ssh_identity_path(self) -> str:
        """Absolute host path to the container-generated SSH private
        key (bind-mounted from ``/etc/boxman/ssh/id_ed25519`` and chowned
        to the current host user by ``containers/docker/entrypoint.sh``).
        Used as the ``IdentityFile`` of the ProxyJump stanza that
        ``write_ssh_config`` emits for the docker runtime."""
        return os.path.join(
            self._get_local_runtime_dir(), "data", "ssh", "id_ed25519"
        )

    def _write_env_file(self, runtime_dir: str,
                        abs_project_dir: str = None) -> str:
        """
        Write a ``.env`` file in *runtime_dir* with absolute paths and
        return its path.
        """
        os.makedirs(runtime_dir, exist_ok=True)
        abs_data_dir = self._data_dir()
        if abs_project_dir is None:
            abs_project_dir = os.path.abspath(self.project_dir or os.getcwd())
        env_path = os.path.join(runtime_dir, ".env")
        host_uid = os.getuid()
        host_gid = os.getgid()

        instance_name = self._instance_name()
        offset = self._derive_port_offset(instance_name)
        ssh_port, tcp_port, tls_port = self._ports_for_offset(offset)

        with open(env_path, "w") as fobj:
            fobj.write(f"BOXMAN_INSTANCE_NAME={instance_name}\n")
            fobj.write(f"BOXMAN_DATA_DIR={abs_data_dir}\n")
            fobj.write(f"BOXMAN_PROJECT_DIR={abs_project_dir}\n")
            fobj.write(f"BOXMAN_SSH_PORT={ssh_port}\n")
            fobj.write(f"BOXMAN_LIBVIRT_TCP_PORT={tcp_port}\n")
            fobj.write(f"BOXMAN_LIBVIRT_TLS_PORT={tls_port}\n")
            fobj.write(f"HOST_UID={host_uid}\n")
            fobj.write(f"HOST_GID={host_gid}\n")

        self.logger.info(
            f"wrote .env: BOXMAN_PROJECT_DIR={abs_project_dir}, "
            f"BOXMAN_DATA_DIR={abs_data_dir}, "
            f"ports={ssh_port}/{tcp_port}/{tls_port}")
        return env_path

    @staticmethod
    def _find_asset_source_dir() -> str | None:
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
