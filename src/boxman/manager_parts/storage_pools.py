"""``boxman storage pools``: libvirt's storage pools, the ones left on
directories that are gone, and retiring those (#277)."""

from __future__ import annotations

import os
import shlex
from typing import Any

from boxman.exceptions import ConfigError, ProvisionError
from boxman.providers.libvirt.commands import LibVirtCommandBase, VirshCommand
from boxman.providers.libvirt.session import LibVirtSession
from boxman.providers.libvirt.storage_pools import (
    Pool,
    autostarted_pools,
    domain_pool_refs,
    read_pools,
    retire_pool,
)

#: what the probe in :meth:`StoragePoolsMixin._target_status` prints for a
#: target directory that is there
_THERE = "boxman-pool-target-there"


class StoragePoolsMixin:

    def storage_pools(self, cli_args) -> None:
        """
        ``boxman storage pools``: every storage pool of this libvirt, with
        its state, autostart and target; whether that target is ``there``,
        ``gone`` (positively absent, as ``destroy`` tells it:
        ``LibVirtSession.path_absent``) or ``unknown`` (neither can be told,
        e.g. a directory on the way cannot be searched); and a note: a
        workdir of this project, a ``bprj__`` name, the domains whose volume
        disks name it.

        ``--stale`` lists only the directory pools whose target is gone.
        ``--prune`` retires those (:func:`retire_pool`, never
        ``pool-delete``), or only the ones it names, after a prompt that
        lists them, unless ``-y``. ``--dry-run`` lists them and changes
        nothing. A pool is pruned only by the gates ``destroy`` retires one
        by: a ``dir`` pool, its target gone, and no domain naming it in a
        volume disk. One the domains name is skipped and said to be; a pool
        named that does not pass stops the whole prune. Without a conf.yml,
        boxman.yml's libvirt provider on the local runtime is asked (see
        ``scripts/app.py``).

        Raises:
            ConfigError: ``--dry-run`` or ``-y`` without ``--prune``.
            ProvisionError: The pools cannot be read; under ``--prune``,
                which domains use them cannot be read, a pool named does not
                pass the gates, or a pool could not be retired.
        """
        prune = getattr(cli_args, 'prune', None)
        if prune is None and (getattr(cli_args, 'dry_run', False)
                              or getattr(cli_args, 'yes', False)):
            raise ConfigError("--dry-run and -y only apply to --prune")
        provider_config = self._libvirt_provider_config()
        virsh = VirshCommand(provider_config=provider_config)
        pools = read_pools(virsh)
        if pools is None:
            raise ProvisionError(
                f"could not read the storage pools of {virsh.uri}")
        status = self._target_status(provider_config, pools)
        refs = domain_pool_refs(virsh)
        if prune is not None:
            self._prune_pools(cli_args, virsh, pools, status, refs, prune)
            return
        if getattr(cli_args, 'stale', False):
            pools = [pool for pool in pools if self._stale(pool, status)]
            if not pools:
                print("no storage pool is left on a directory that is gone")
                return
        self._print_pools(pools, status, refs, autostarted_pools(virsh))

    @staticmethod
    def _stale(pool: Pool, status: dict[str, str]) -> bool:
        """A directory pool whose target is confirmed gone."""
        return pool.type == "dir" and status[pool.name] == "gone"

    @staticmethod
    def _target_status(provider_config: dict[str, Any],
                       pools: list[Pool]) -> dict[str, str]:
        """
        ``there``, ``gone`` or ``unknown`` for each pool's target, by name
        (``-`` for a pool with none). ``gone`` is the absence probe
        ``destroy`` retires a pool by (``LibVirtSession.path_absent``);
        ``there`` a directory, asked through the same runtime wrapper and
        never with ``sudo``. Anything else proves neither: a directory on
        the way the probe cannot search, or a target that is not a
        directory, or not normalized.
        """
        session = LibVirtSession({'provider': {'libvirt': provider_config}})
        probe = LibVirtCommandBase(override_config_use_sudo=False,
                                   provider_config=provider_config)
        status = {}
        for pool in pools:
            if not pool.target:
                status[pool.name] = "-"
            elif session.path_absent(pool.target):
                status[pool.name] = "gone"
            else:
                quoted = shlex.quote(pool.target)
                there = probe.execute_shell(
                    f"if [ -d {quoted} ]; then echo {_THERE}; fi", warn=True)
                status[pool.name] = (
                    "there" if there.ok
                    and (there.stdout or "").strip() == _THERE else "unknown")
        return status

    def _project_dirs(self) -> set[str]:
        """The workspace and cluster workdirs of the project, if there is
        one, normalized."""
        config = self.config or {}
        dirs = [(config.get('workspace') or {}).get('path')]
        dirs += [(cluster or {}).get('workdir')
                 for cluster in (config.get('clusters') or {}).values()]
        return {os.path.normpath(os.path.abspath(os.path.expanduser(d)))
                for d in dirs if d}

    def _print_pools(self, pools: list[Pool], status: dict[str, str],
                     refs: dict[str, list[str]] | None,
                     autostart: set[str] | None) -> None:
        mine = self._project_dirs()
        rows = [("NAME", "STATE", "AUTOSTART", "TARGET", "STATUS", "NOTE")]
        for pool in sorted(pools, key=lambda p: p.name):
            notes = []
            if pool.target and os.path.normpath(pool.target) in mine:
                notes.append("workdir of this project")
            if pool.name.startswith("bprj__"):
                notes.append("bprj-named")
            if refs is None:
                notes.append("its users could not be read")
            elif pool.name in refs:
                notes.append(f"used by {', '.join(refs[pool.name])}")
            if not pool.persistent:
                notes.append("transient")
            rows.append((
                pool.name, "active" if pool.active else "inactive",
                "?" if autostart is None
                else ("yes" if pool.name in autostart else "no"),
                pool.target or "-", status[pool.name], "; ".join(notes)))
        # the last column, the note, is not padded
        widths = [max(len(row[i]) for row in rows) for i in range(5)]
        for row in rows:
            print("  ".join([row[i].ljust(widths[i]) for i in range(5)]
                            + [row[5]]).rstrip())

    def _prune_pools(self, cli_args, virsh, pools: list[Pool],
                     status: dict[str, str],
                     refs: dict[str, list[str]] | None,
                     names: list[str]) -> None:
        """``--prune``: see :meth:`storage_pools`."""
        if refs is None:
            raise ProvisionError(
                "could not read which domains use the storage pools, so "
                "none was pruned")
        by_name = {pool.name: pool for pool in pools}
        if names:
            refused = [f"{name}: no such storage pool" for name in names
                       if name not in by_name]
            chosen = [by_name[name] for name in dict.fromkeys(names)
                      if name in by_name]
            refused += [f"{pool.name}: {why}" for pool in chosen
                        if (why := self._not_prunable(pool, status, refs))]
            if refused:
                raise ProvisionError(
                    "nothing was pruned: " + "; ".join(refused))
        else:
            chosen = [pool for pool in pools if self._stale(pool, status)]
            for pool in [p for p in chosen if p.name in refs]:
                print(f"skipping storage pool {pool.name}: "
                      f"{self._not_prunable(pool, status, refs)}")
            chosen = [pool for pool in chosen if pool.name not in refs]
        if not chosen:
            print("no storage pool to prune")
            return
        print("\nThese storage pools will be stopped and undefined (their "
              "directories are already gone; nothing on disk is touched):\n")
        width = max(len(pool.name) for pool in chosen)
        for pool in sorted(chosen, key=lambda p: p.name):
            print(f"  {pool.name.ljust(width)}  {pool.target}")
        print()
        if getattr(cli_args, 'dry_run', False):
            print("dry run: nothing was changed")
            return
        if not getattr(cli_args, 'yes', False):
            try:
                answer = input("Proceed? [y/N] ").strip().lower()
            except EOFError:
                print("No input available, aborted.")
                return
            if answer not in ("y", "yes"):
                print("Aborted.")
                return
        failed = []
        for pool in sorted(chosen, key=lambda p: p.name):
            why = retire_pool(virsh, pool)
            if why is None:
                print(f"removed storage pool {pool.name}")
            else:
                failed.append(f"{pool.name}: {why}")
        if failed:
            raise ProvisionError(
                f"could not prune {len(failed)} storage pool(s): "
                + "; ".join(failed))

    @staticmethod
    def _not_prunable(pool: Pool, status: dict[str, str],
                      refs: dict[str, list[str]]) -> str | None:
        """Why *pool* may not be pruned, or ``None``."""
        if pool.type != "dir":
            return f"it is a {pool.type} pool, not a directory pool"
        state = status[pool.name]
        if state == "there":
            return f"its directory {pool.target} is still there"
        if state != "gone":
            return (f"whether its directory {pool.target} is gone cannot be "
                    f"told (a directory on the way cannot be searched, or "
                    f"the path is not normalized)")
        if pool.name in refs:
            return (f"{', '.join(refs[pool.name])} name(s) it in a volume "
                    f"disk")
        return None
