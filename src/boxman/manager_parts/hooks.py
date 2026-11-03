"""Lifecycle hooks for ``boxman up``: named tasks run before and after it.

A project may declare, beside its ``tasks:``::

    hooks:
      pre_up: [check-inputs]       # before up changes anything
      post_up: [configure, smoke]  # after up brought everything up

Each entry names a task of the same config. ``boxman up`` runs the
``pre_up`` tasks, in order, before any side effect, and the ``post_up``
tasks, in order, after an ``up`` that succeeded: a fresh provision, a start
or resume of stopped VMs, or the "all VMs already running" path. A task that
exits non-zero stops the run and makes ``up`` exit non-zero; later tasks do
not run. With no ``hooks:`` key, ``up`` behaves exactly as before.

Before the ``post_up`` tasks run, boxman writes a record of this ``up`` to
``<workspace>/boxman-up-record.json`` (0600): the UUIDs of the project's
libvirt domains and networks that this ``up`` created, and of all of them
that are present afterwards. A hook can take ownership of what boxman
created from that record instead of trusting whatever it finds under the
project's names. The record carries this run's id, which the hooks receive
in ``BOXMAN_UP_RUN_ID``, so a record left by an earlier ``up`` is never
mistaken for this one's. See doc/hooks.md.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import uuid
from typing import Any

from boxman.exceptions import ConfigError, HookError, ProvisionError
from boxman.task_runner import TaskRunner

#: the phases ``hooks:`` may name, in the order ``up`` runs them
HOOK_PHASES = ('pre_up', 'post_up')

#: the file, in the workspace, that hands ``post_up`` hooks what ``up`` did
UP_RECORD_NAME = 'boxman-up-record.json'

#: bumped when the record's layout changes incompatibly
UP_RECORD_VERSION = 1


class HooksMixin:
    """``hooks:`` validation, hook execution and the ``up`` record."""

    def validate_hooks_config(self) -> None:
        """Check ``hooks:`` before anything runs.

        Raises:
            ConfigError: ``hooks`` is not a mapping, names a phase other than
                ``pre_up``/``post_up``, a phase is not a list of task names,
                or a name is not a task of this config with a ``command``.
        """
        hooks = (self.config or {}).get('hooks')
        if hooks is None:
            return
        if not isinstance(hooks, dict):
            raise ConfigError(
                "'hooks' must be a mapping of phase to a list of task names, "
                f"e.g. hooks: {{pre_up: [check]}}; got {type(hooks).__name__}")
        unknown = sorted(set(hooks) - set(HOOK_PHASES))
        if unknown:
            raise ConfigError(
                f"hooks: unknown phase(s) {', '.join(map(str, unknown))}; "
                f"supported: {', '.join(HOOK_PHASES)}")
        tasks = (self.config or {}).get('tasks') or {}
        for phase in HOOK_PHASES:
            names = hooks.get(phase)
            if names is None:
                continue
            if not isinstance(names, list) or not all(
                    isinstance(name, str) and name for name in names):
                raise ConfigError(
                    f"hooks.{phase} must be a list of task names")
            for name in names:
                task = tasks.get(name)
                if task is None:
                    available = ', '.join(tasks) or '(none)'
                    raise ConfigError(
                        f"hooks.{phase} names task '{name}', which is not "
                        f"defined under tasks: (available: {available})")
                if not isinstance(task, dict) or not str(task.get('command') or '').strip():
                    raise ConfigError(
                        f"hooks.{phase}: task '{name}' has no command")

    def _hook_tasks(self, phase: str) -> list[str]:
        """The task names configured for *phase* (validated), in order."""
        return list(((self.config or {}).get('hooks') or {}).get(phase) or [])

    def has_hooks(self) -> bool:
        """Whether any hook is configured."""
        return any(self._hook_tasks(phase) for phase in HOOK_PHASES)

    def _config_dir(self) -> str:
        """The directory of the project's conf.yml: where hooks run."""
        path = getattr(self, 'config_path', None)
        if path:
            return os.path.dirname(os.path.abspath(os.path.expanduser(path)))
        return os.environ.get('BOXMAN_CONF_DIR') or os.getcwd()

    def run_hooks(self, phase: str, env: dict[str, str]) -> None:
        """Run the *phase* hooks in order; stop at the first failure.

        Each runs through :class:`~boxman.task_runner.TaskRunner` in the
        task's own ``workdir`` if it sets one, else in the directory of
        conf.yml (the workspace may not exist yet). ``post_up`` hooks get the
        workspace environment as ``boxman run`` does; ``pre_up`` hooks run
        before anything exists, with the process environment only. Both get
        *env* and ``BOXMAN_HOOK=<phase>``.

        Raises:
            HookError: a hook exited non-zero.
        """
        names = self._hook_tasks(phase)
        if not names:
            return
        runner = TaskRunner(config=self.config)
        for name in names:
            self.logger.info(f"{phase} hook: running task '{name}'")
            rc = runner.run(
                name,
                extra_env=dict(env, BOXMAN_HOOK=phase),
                cwd=self._config_dir(),
                workspace_env=(phase != 'pre_up'),
            )
            if rc != 0:
                if phase == 'pre_up':
                    where = "before boxman up changed anything"
                else:
                    where = ("after boxman up brought the infrastructure up; "
                             "it stays as it is, and the next boxman up runs "
                             "the post_up hooks again")
                raise HookError(
                    f"{phase} hook '{name}' failed (exit {rc}); boxman up "
                    f"stopped {where}")
            self.logger.info(f"{phase} hook: task '{name}' succeeded")

    # ---- the up record ------------------------------------------------------
    def _project_network_names(self) -> list[str]:
        """Full libvirt names of the networks the project's libvirt
        clusters declare."""
        names = []
        for cluster_name, cluster in self._vm_clusters.items():
            if self.provider_type_for_cluster(cluster_name) != 'libvirt':
                continue
            for network_name in (cluster.get('networks') or {}):
                names.append(self.full_network_name(
                    project_config=self.config, cluster_name=cluster_name,
                    network_name=network_name))
        return names

    def project_inventory(self) -> dict[str, dict[str, str]]:
        """UUIDs of the project's libvirt domains and networks that exist.

        Only the names the config declares are looked at. Complete listings
        are read with their exit status checked: a failed query is an error,
        never an absent resource.

        Returns:
            ``{"domains": {name: uuid}, "networks": {name: uuid}}``.

        Raises:
            ProvisionError: libvirt could not be read.
        """
        inventory: dict[str, dict[str, str]] = {'domains': {}, 'networks': {}}
        if not self._has_libvirt_clusters():
            return inventory
        from boxman.providers.libvirt.session import _listed_domains
        virsh = self._virsh()
        listing = virsh.execute('list', '--all', '--uuid', '--name', warn=True)
        domains = _listed_domains(listing.stdout) if listing.ok else None
        if domains is None:
            raise ProvisionError(
                "cannot record what boxman up created: the libvirt domain "
                f"list could not be read ({(listing.stderr or '').strip()})")
        by_name = dict(domains)
        for name in self._get_project_vm_names():
            if name in by_name:
                inventory['domains'][name] = by_name[name]
        nets = virsh.execute('net-list', '--all', '--name', warn=True)
        if not nets.ok:
            raise ProvisionError(
                "cannot record what boxman up created: the libvirt network "
                f"list could not be read ({(nets.stderr or '').strip()})")
        listed = {line.strip() for line in nets.stdout.splitlines() if line.strip()}
        for name in self._project_network_names():
            if name not in listed:
                continue
            res = virsh.execute('net-uuid', name, warn=True)
            net_uuid = (res.stdout or '').strip() if res.ok else ''
            if not net_uuid:
                raise ProvisionError(
                    f"cannot record what boxman up created: the uuid of "
                    f"network {name} could not be read")
            inventory['networks'][name] = net_uuid
        return inventory

    @staticmethod
    def _created_between(before: dict, after: dict) -> dict[str, dict[str, str]]:
        """What *after* has that *before* did not, by name and uuid: a
        resource recreated under the same name counts as created."""
        return {kind: {name: uid for name, uid in after[kind].items()
                       if before[kind].get(name) != uid}
                for kind in ('domains', 'networks')}

    def up_record_path(self) -> str:
        """Where the up record goes: the workspace, else conf.yml's dir."""
        workspace = ((self.config or {}).get('workspace') or {}).get('path')
        if workspace:
            return os.path.join(os.path.expanduser(workspace), UP_RECORD_NAME)
        return os.path.join(self._config_dir(), UP_RECORD_NAME)

    def write_up_record(self, run_id: str, status: str, force: bool,
                        created: dict | None, present: dict | None) -> str | None:
        """Write the up record atomically, 0600; return its path.

        Returns None, writing nothing, when its directory does not exist
        (an ``up`` that failed before creating the workspace created
        nothing to record).
        """
        path = self.up_record_path()
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            return None
        record = {
            'boxman_up_record': UP_RECORD_VERSION,
            'project': (self.config or {}).get('project'),
            'run_id': run_id,
            'status': status,
            'force': force,
            'finished_at': datetime.datetime.now(datetime.timezone.utc)
                           .strftime('%Y-%m-%dT%H:%M:%SZ'),
            'created': created,
            'present': present,
        }
        fd, tmp = tempfile.mkstemp(prefix='.boxman-up-record.', dir=directory)
        try:
            with os.fdopen(fd, 'w') as fobj:
                os.fchmod(fobj.fileno(), 0o600)
                json.dump(record, fobj, indent=2, sort_keys=True)
                fobj.write('\n')
                fobj.flush()
                os.fsync(fobj.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    def up_with_hooks(self, cli_args: Any, up_body) -> None:
        """Run *up_body* (the hook-less ``up``) between the hooks.

        - no hooks configured: *up_body* alone, nothing else changes;
        - ``pre_up`` hooks, in order, before *up_body* changes anything; a
          failure stops here;
        - with ``post_up`` hooks, the project's domain and network UUIDs are
          read before and after *up_body*, and the difference -- what this
          ``up`` created -- is written to the up record, with what is present.
          If *up_body* fails, the record is written with status ``failed``
          (best effort, never hiding the original error) and no ``post_up``
          hook runs;
        - the ``post_up`` hooks, in order, with ``BOXMAN_UP_RECORD``.

        Every hook also gets ``BOXMAN_UP_RUN_ID`` (this run, also in the
        record) and ``BOXMAN_UP_FORCE`` (``1`` when ``--force`` was given).
        """
        self.validate_hooks_config()
        if not self.has_hooks():
            return up_body(cli_args)
        run_id = uuid.uuid4().hex
        force = bool(getattr(cli_args, 'force', False))
        env = {'BOXMAN_UP_RUN_ID': run_id, 'BOXMAN_UP_FORCE': '1' if force else '0'}
        self.run_hooks('pre_up', env)
        if not self._hook_tasks('post_up'):
            return up_body(cli_args)
        before = self.project_inventory()
        try:
            result = up_body(cli_args)
        except Exception:
            try:
                after = self.project_inventory()
                self.write_up_record(run_id, 'failed', force,
                                     self._created_between(before, after), after)
            except Exception as exc:   # never hide why up failed
                self.logger.warning(f"could not write the up record of a failed up: {exc}")
            raise
        after = self.project_inventory()
        path = self.write_up_record(
            run_id, 'succeeded', force, self._created_between(before, after), after)
        if path is None:
            raise HookError(
                f"cannot hand the post_up hooks a record: {os.path.dirname(self.up_record_path())} "
                "does not exist after boxman up")
        self.logger.info(f"up record written to {path}")
        self.run_hooks('post_up', dict(env, BOXMAN_UP_RECORD=path))
        return result
