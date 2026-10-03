"""Network lifecycle (define/reconcile/destroy) for BoxmanManager."""





import os
import time
from typing import Any

from boxman.exceptions import ConfigError, NetworkError
from boxman.providers.libvirt import net_reconcile


class NetworksMixin:

    def define_networks(self) -> None:
        """
        Define the networks specified in the cluster configuration (sequential).

        Networks must be defined one at a time because bridge name assignment
        (virbrX) is a shared resource: each definition must be committed to
        libvirt and the cache before the next network picks its bridge name,
        otherwise two concurrent processes can both select the same bridge index
        and the second net-define fails with "bridge name already in use".
        """
        for cluster_name, cluster in self._vm_clusters.items():
            for network_name, network_info in (cluster.get('networks') or {}).items():
                _network_name = self.full_network_name(
                    project_config=self.config,
                    cluster_name=cluster_name,
                    network_name=network_name
                )
                defined = self.session_for_cluster(cluster_name).define_network(
                    name=_network_name,
                    info=network_info,
                    workdir=cluster['workdir']
                )
                if not defined:
                    # Dropping this status is how `provision` -- and therefore
                    # every project's *first* `up`, which routes through it --
                    # reported "defined network …" for a network libvirt had
                    # rolled back, then went on to clone VMs and attach them
                    # to something that does not exist. Only the second `up`
                    # onward gets the guarded reconcile path.
                    raise NetworkError(
                        f"network {_network_name} could not be defined in "
                        f"{cluster['workdir']}; see the preceding libvirt "
                        f"error. Not continuing: the VMs about to be cloned "
                        f"would be attached to a network that does not exist.")
                self.logger.status(f"defined network {_network_name} in {cluster['workdir']}")

    def reconcile_networks(self,
                           dry_run: bool = False,
                           allow_recreate: bool = False,
                           auto_accept: bool = False,
                           prune: bool = False) -> dict[str, str]:
        """
        Bring the libvirt networks in line with the configuration.

        Three outcomes per network, decided by comparing the configuration
        against ``virsh net-dumpxml``:

        - **create** -- the network is not defined yet. Defined and started.
        - **live** -- only the dhcp reservations or the dhcp range differ.
          Applied with ``virsh net-update ... --live --config``: dnsmasq is
          reloaded, the bridge stays up, no guest is disturbed.
        - **recreate** -- the forward mode, address, netmask, bridge or mac
          differ. libvirt cannot change these in place, so the network has to
          be destroyed and defined again. That deletes the bridge and leaves
          attached guests with a dead nic, so it only happens with
          *allow_recreate*, and the guests are re-attached afterwards.

        A network the project provisioned but no longer declares is
        reported every run, and removed only with *prune* — see
        :meth:`_prune_orphaned_networks`.

        Args:
            dry_run: report the plan and change nothing
            allow_recreate: permit the disruptive path
            auto_accept: skip the confirmation prompt for a recreate
            prune: remove networks dropped from the config, rather than
                only reporting them

        Returns:
            A mapping of network name to the action taken: one of ``created``,
            ``updated``, ``recreated``, ``skipped``, ``failed`` or
            ``removed``.
        """
        if not hasattr(self.provider, 'plan_network'):
            self.logger.debug(
                "provider does not support network reconciliation, skipping")
            return {}

        results: dict[str, str] = self._prune_orphaned_networks(
            dry_run=dry_run, prune=prune)

        for cluster_name, cluster in self.config['clusters'].items():
            for network_name, network_info in (cluster.get('networks') or {}).items():
                full_name = self.full_network_name(
                    project_config=self.config,
                    cluster_name=cluster_name,
                    network_name=network_name)

                try:
                    plan = self.provider.plan_network(
                        name=full_name, info=network_info)
                except (ConfigError, ValueError) as exc:
                    # the network block itself does not validate (a bad dhcp
                    # reservation, say). Report it against the network it came
                    # from and carry on: the other networks, and the VMs, are
                    # not necessarily affected
                    self.logger.error(
                        f"network {network_name}: {exc}")
                    results[full_name] = 'failed'
                    continue

                if plan['action'] == 'none':
                    continue

                # two clusters may each hold a network called 'mgmt', so the
                # logs carry the cluster as well
                label = f"{cluster_name}/{network_name}"

                if plan['action'] == 'error':
                    results[full_name] = 'failed'
                    continue

                for line in net_reconcile.describe_plan(label, plan):
                    self.logger.info(line)

                if plan['action'] == 'create':
                    self.logger.info(f"network {label}: not defined yet")
                    if dry_run:
                        results[full_name] = 'skipped'
                        continue
                    results[full_name] = self._define_network(
                        label=label, full_name=full_name,
                        network_info=network_info, workdir=cluster['workdir'])

                elif plan['action'] == 'live':
                    if dry_run:
                        if plan.get('inactive'):
                            self.logger.info(
                                f"[dry-run] network {label}: is defined but "
                                f"not running, would start it")
                        results[full_name] = 'skipped'
                        continue

                    ok = True
                    if plan.get('inactive'):
                        self.logger.info(
                            f"network {label}: is defined but not running, "
                            f"starting it")
                        ok = self.provider.start_network(
                            name=full_name, info=network_info)

                    if ok and (plan['host_ops'] or plan['range_ops']):
                        ok = self.provider.apply_network_live_plan(
                            name=full_name, info=network_info, plan=plan)
                    results[full_name] = 'updated' if ok else 'failed'

                elif plan['action'] == 'recreate':
                    results[full_name] = self._recreate_network(
                        cluster=cluster,
                        network_name=label,
                        full_name=full_name,
                        network_info=network_info,
                        plan=plan,
                        dry_run=dry_run,
                        allow_recreate=allow_recreate,
                        auto_accept=auto_accept)

        # Isolation rules are host iptables state, not libvirt state, so they
        # do not survive a reboot or a manual flush while the network itself
        # autostarts happily without them. Re-assert them on every reconcile
        # rather than only at define time.
        for full_name, outcome in self._reconcile_network_isolation(
                dry_run=dry_run).items():
            # an unisolated routed network is a failed network: without this
            # `up` exits 0 while the guests can reach the host
            if outcome == 'failed':
                results[full_name] = 'failed'

        return results

    def _reconcile_network_isolation(
            self, dry_run: bool = False) -> dict[str, str]:
        """
        Re-apply the host isolation of every routed network in the config.

        Reported rather than done silently: a routed network that lost its
        rules has been reachable from the host, and from the guests' point of
        view unisolated, for however long it was up. That is worth a warning
        even though it is being fixed here and now.
        """
        outcomes: dict[str, str] = {}
        if not hasattr(self.provider, 'reconcile_network_isolation'):
            return outcomes

        for cluster_name, cluster in self.config['clusters'].items():
            for network_name, network_info in (cluster.get('networks') or {}).items():
                if (network_info or {}).get('mode') != 'route':
                    continue
                label = f"{cluster_name}/{network_name}"
                full_name = self.full_network_name(
                    project_config=self.config,
                    cluster_name=cluster_name,
                    network_name=network_name)
                try:
                    outcome = self.provider.reconcile_network_isolation(
                        name=full_name, info=network_info,
                        check_only=dry_run)
                except Exception as exc:
                    self.logger.error(
                        f"network {label}: could not check isolation rules: {exc}")
                    outcomes[full_name] = 'failed'
                    continue
                outcomes[full_name] = outcome

                if outcome == 'absent':
                    # the network is not defined; its own failure was already
                    # reported by the define path
                    self.logger.debug(
                        f"network {label}: not defined, isolation not applicable")
                elif outcome == 'repaired':
                    self.logger.warning(
                        f"network {label}: isolation rules were missing and "
                        f"have been re-applied. A routed network is left "
                        f"unprotected between a host reboot and the next "
                        f"boxman run.")
                elif outcome == 'drifted':
                    self.logger.warning(
                        f"[dry-run] network {label}: isolation rules are "
                        f"missing; they would be re-applied.")
                elif outcome == 'failed':
                    self.logger.error(
                        f"network {label}: could not apply isolation rules")

        return outcomes

    def _declared_network_names(self) -> set:
        """
        The full names of every network this project's config declares.

        The same construction the lifecycle loops use, so "declared" here
        means exactly what it means to :meth:`define_networks` and
        :meth:`destroy_networks`.

        Returns:
            set: fully qualified network names.
        """
        return {
            self.full_network_name(project_config=self.config,
                                   cluster_name=cluster_name,
                                   network_name=network_name)
            for cluster_name, cluster in self._vm_clusters.items()
            for network_name in (cluster.get('networks') or {})
        }

    def _orphaned_networks(self) -> dict:
        """
        Networks this project provisioned and no longer declares.

        Every lifecycle loop iterates the *declared* networks, so one
        dropped from ``conf.yml`` is by construction absent from all of
        them: it stays defined and active, holding its bridge and subnet,
        and ``check_network_exists`` counts its cache record as a conflict
        against any later network of the same name (#189).

        The cache is the record of what was provisioned. Only this
        project's own entries are considered, and only those whose name
        carries this project's prefix — a ``project::cluster::net``
        reference resolves to *another* project's network, which this
        project may use but does not own.

        Returns:
            dict: ``{full_name: cached_info}`` for each orphan, empty when
            the declared set accounts for everything provisioned.
        """
        project = (self.config or {}).get('project')
        if not project:
            return {}

        try:
            self.cache.read_projects_cache()
        except (OSError, ValueError) as exc:
            # an unreadable cache is not an empty one; saying "nothing was
            # provisioned" here would hide every orphan there is
            self.logger.warning(
                f"could not read the projects cache ({exc}); not checking "
                f"for networks dropped from the config")
            return {}

        entry = (self.cache.projects or {}).get(project) or {}
        provisioned = entry.get('networks') or {}
        if not provisioned:
            return {}

        declared = self._declared_network_names()
        owned_prefix = f'bprj__{project}__bprj__'
        return {
            name: info for name, info in provisioned.items()
            if name not in declared and name.startswith(owned_prefix)
        }

    def _prune_orphaned_networks(self,
                                 dry_run: bool = False,
                                 prune: bool = False) -> dict[str, str]:
        """
        Report — and with *prune*, remove — networks dropped from the config.

        Removing a network from ``conf.yml`` used to leak it: every
        lifecycle loop iterates the declared networks, so the dropped one
        was destroyed by nothing, kept its bridge and subnet, and its cache
        record then counted as a conflict against any later network of the
        same name (#189).

        Reporting is unconditional because the leak is otherwise invisible;
        removal is not, because a network is infrastructure and guests may
        still be attached to it. A network with attached domains is never
        removed even under *prune* — the guests would be left with a dead
        nic — and the refusal names them. Nor is one whose attachments
        could not be determined: under *prune* that is a failure, since the
        removal that was asked for could not be done safely.

        Args:
            dry_run: say what would happen and change nothing.
            prune: actually remove the orphans.

        Returns:
            dict: ``{full_name: outcome}`` for each orphan — ``removed``,
            ``failed``, or ``skipped`` when it was only reported.
        """
        orphans = self._orphaned_networks()
        if not orphans:
            return {}

        results: dict[str, str] = {}
        for full_name, cached in sorted(orphans.items()):
            attached, unknown = self._attached_domains(full_name)
            if unknown:
                removing = prune and not dry_run
                (self.logger.error if removing else self.logger.warning)(
                    f"network {full_name} is no longer in the config, but "
                    f"whether guests are still attached to it could not be "
                    f"determined ({unknown}); not removing it.")
                results[full_name] = 'failed' if removing else 'skipped'
                continue

            if attached:
                self.logger.warning(
                    f"network {full_name} is no longer in the config but "
                    f"{len(attached)} guest(s) are still attached to it "
                    f"({', '.join(sorted(attached))}); leaving it alone. "
                    f"Detach or destroy them first.")
                results[full_name] = 'skipped'
                continue

            if not prune:
                self.logger.warning(
                    f"network {full_name} is no longer in the config but is "
                    f"still defined (bridge "
                    f"{cached.get('bridge_name') or 'unknown'}). It is "
                    f"holding its bridge and subnet, and will collide with a "
                    f"later network of the same name. Remove it with "
                    f"`boxman up --prune-networks`.")
                results[full_name] = 'skipped'
                continue

            if dry_run:
                self.logger.info(f"[dry-run] would remove network {full_name}")
                results[full_name] = 'skipped'
                continue

            results[full_name] = self._remove_orphaned_network(
                full_name, cached)

        return results

    def _attached_domains(self, full_name: str) -> tuple[list, str | None]:
        """
        The guests attached to *full_name*, as the provider reports them.

        An empty list from a provider that cannot answer is the dangerous
        direction here — it reads as "nothing is attached" and lets the
        removal proceed — so a provider without the capability, or one that
        fails to answer, is reported as an unknown rather than as an empty
        list.

        Args:
            full_name: the fully qualified network name.

        Returns:
            tuple: ``(domain names, None)``, or ``([], reason)`` when the
            question could not be answered.
        """
        session = self.provider
        if not hasattr(session, 'network_attached_domains'):
            return [], 'the provider does not report attachments'
        try:
            return list(session.network_attached_domains(full_name)), None
        except Exception as exc:
            return [], f'{type(exc).__name__}: {exc}'

    def _record_orphan_state(self, full_name: str, mode: str,
                             bridge_name: str | None) -> None:
        """
        Write a network's live forward mode and bridge into its cache entry.

        ``remove_network`` undefines a network before it withdraws a routed
        network's isolation rules, so a removal that fails between the two
        leaves no definition to read the mode from on the next run. The
        record is what lets that run finish the teardown rather than guess.

        Raises:
            KeyError: when the entry is no longer in the cache.
            OSError, ValueError: when the cache cannot be read or written.
        """
        self.cache.read_projects_cache()
        record = self.cache.projects[self.config['project']]['networks'][full_name]
        if (record.get('mode') == mode
                and (not bridge_name or record.get('bridge_name') == bridge_name)):
            return
        record['mode'] = mode
        if bridge_name:
            record['bridge_name'] = bridge_name
        self.cache.write_projects_cache()

    def _orphan_teardown_info(self, full_name: str,
                              cached: dict) -> dict | None:
        """
        Describe an orphaned network for ``remove_network``, or refuse to.

        The forward mode decides whether removal also withdraws iptables
        rules -- only ``route`` has boxman-owned ones -- and ``Network``
        takes a missing mode for ``nat``, which skips exactly that teardown.
        So the mode is never left out and never guessed:

        - a defined network is described from its *live* definition, and
          that mode and bridge are recorded in the cache entry before
          anything is torn down;
        - a network libvirt no longer has is described from that record --
          left by an earlier removal that stopped half way -- and a routed
          one only while no defined network holds its bridge, whose rules
          would otherwise be the ones withdrawn;
        - anything that cannot be read, or was never recorded, is a refusal.

        Args:
            full_name: the fully qualified network name.
            cached: its cache record.

        Returns:
            dict: the ``info`` for ``remove_network``, or None when it must
            not be removed; the reason has been logged.
        """
        def refuse(reason: str) -> None:
            self.logger.error(
                f"not removing the orphaned network {full_name}: {reason}. "
                f"Its cache entry is kept, so it is reported again on the "
                f"next run.")

        session = self.provider
        if not hasattr(session, 'live_network_state'):
            refuse("the provider cannot report its live definition, so its "
                   "forward mode is unknown")
            return None
        try:
            actual = session.live_network_state(full_name)
        except Exception as exc:
            refuse(f"its live definition could not be read "
                   f"({type(exc).__name__}: {exc}), and its forward mode "
                   f"decides whether isolation rules have to be removed too")
            return None

        if actual is not None:
            mode, bridge = actual.get('mode'), actual.get('bridge_name')
            if not mode:
                refuse("its live definition names no forward mode")
                return None
            if mode == 'route' and not bridge:
                refuse("it is a routed network with no bridge in its live "
                       "definition, so its isolation rules cannot be found")
                return None
            try:
                self._record_orphan_state(full_name, mode, bridge)
            except (KeyError, OSError, ValueError) as exc:
                refuse(f"its forward mode could not be recorded in the "
                       f"projects cache first ({type(exc).__name__}: {exc})")
                return None
            return self._teardown_info({'actual': actual}, cached)

        mode, bridge = cached.get('mode'), cached.get('bridge_name')
        if not mode:
            refuse(f"libvirt no longer has it and its forward mode was never "
                   f"recorded, so whether it left isolation rules on bridge "
                   f"{bridge or '(unknown)'} cannot be told. Check for "
                   f"BXM_ISO_* chains on that bridge and remove the entry "
                   f"from the projects cache by hand")
            return None

        info: dict[str, Any] = {'mode': mode}
        if mode == 'route':
            if not bridge:
                refuse("it was a routed network and no bridge is recorded "
                       "for it, so its isolation rules cannot be found")
                return None
            if not hasattr(session, 'network_bridges'):
                refuse(f"the provider cannot say whether another network "
                       f"now holds bridge {bridge}")
                return None
            try:
                holders = sorted(name for name, held in
                                 session.network_bridges().items()
                                 if held == bridge)
            except Exception as exc:
                refuse(f"whether another network now holds bridge {bridge} "
                       f"could not be determined ({type(exc).__name__}: "
                       f"{exc})")
                return None
            if holders:
                refuse(f"bridge {bridge} now belongs to "
                       f"{', '.join(holders)}, and withdrawing the isolation "
                       f"rules on it would withdraw that network's")
                return None
        if bridge:
            info['bridge'] = {'name': bridge}
        return info

    def _remove_orphaned_network(self, full_name: str, cached: dict) -> str:
        """
        Remove one orphaned network and forget its cache entry.

        See :meth:`_orphan_teardown_info` for how the network is described
        to the removal, and when it is not removed at all.

        Args:
            full_name: the fully qualified network name.
            cached: its cache record, for the address and bridge.

        Returns:
            str: ``removed`` or ``failed``.
        """
        info = self._orphan_teardown_info(full_name, cached)
        if info is None:
            return 'failed'

        try:
            removed = self.provider.remove_network(name=full_name, info=info)
        except Exception as exc:
            self.logger.error(
                f"failed to remove the orphaned network {full_name}: "
                f"{type(exc).__name__}: {exc}. Its cache entry is kept so the "
                f"next run tries again.")
            return 'failed'

        if not removed:
            self.logger.error(
                f"failed to remove the orphaned network {full_name} "
                f"completely. Its cache entry is kept so the next run tries "
                f"again.")
            return 'failed'

        self.logger.info(
            f"removed network {full_name}, which is no longer in the config")
        self._forget_cached_network(full_name)
        return 'removed'

    def report_network_results(self, results: dict[str, str]) -> None:
        """
        Log the outcome of a reconcile, loudly for the ones that went wrong.

        ``failed`` and ``partial`` would otherwise be buried: the caller
        carries on either way, so this is the only place a user learns that a
        network did not come back or that a guest is still disconnected.
        """
        if not results:
            return

        for full_name, outcome in sorted(results.items()):
            if outcome == 'failed':
                self.logger.error(f"network {full_name}: {outcome}")
            elif outcome == 'partial':
                self.logger.warning(
                    f"network {full_name}: recreated, but at least one VM "
                    f"could not be reconnected")
            else:
                self.logger.info(f"network {full_name}: {outcome}")

    def raise_on_network_failures(self, results: dict[str, str]) -> None:
        """
        Turn failed networks into a failed run.

        Reporting alone is not enough: a routed network that could not be
        isolated, or one that could not be defined at all, leaves guests
        without the connectivity -- or the containment -- the configuration
        asked for. Continuing to exit 0 makes that look like success.
        """
        failed = sorted(name for name, outcome in results.items()
                        if outcome == 'failed')
        if failed:
            raise NetworkError(
                f"{len(failed)} network(s) could not be brought to the "
                f"configured state: {', '.join(failed)}")

    def _vms_worth_waiting_for(self) -> list[str]:
        """
        Project VMs minus the ones a recreate could not reconnect, and minus
        the ones known not to be running.

        Waiting on a guest we already reported as unreachable only burns the
        whole timeout before saying what we already knew. The same goes for
        a guest that is not running: a recreate leaves a shut-off domain
        shut off, so it has no lease coming (#223). When the states cannot
        be read, every VM is waited for, as before.
        """
        unreachable = getattr(self, '_reattach_failed_vms', set())
        names = set(self._get_project_vm_names()) - unreachable
        states = self._vm_states_if_known()
        if states is not None:
            names = {name for name in names
                     if states.get(name, 'not defined')
                     not in self._NOT_RUNNING_STATES}
        return sorted(names)

    def wait_for_vm_ips(self, vm_names: list[str], max_wait: int = 300) -> bool:
        """
        Wait until every named VM reports an address, or *max_wait* passes.

        Returns:
            True if they all got one.
        """
        if not vm_names:
            return True

        self.logger.info("waiting for VMs to get IP addresses...")
        wait_time = 1
        total_waited = 0
        while total_waited < max_wait:
            if all(self.provider.get_vm_ip_addresses(name) for name in vm_names):
                self.logger.info(
                    f"all VMs have IP addresses (waited {total_waited}s)")
                return True
            time.sleep(wait_time)
            total_waited += wait_time
            wait_time = min(wait_time * 2, 60)

        self.logger.warning(
            f"not every VM had an IP address after {max_wait}s; the ssh "
            f"config may be incomplete")
        return False

    def _define_network(self,
                        label: str,
                        full_name: str,
                        network_info: dict[str, Any],
                        workdir: str) -> str:
        """
        Define a network, turning a conflict into a result instead of a crash.

        ``define_network`` starts with ``check_network_exists()``, which raises
        when the cache already holds an entry with this name, bridge or
        address. That is the right guard when two projects collide, but it
        raises before the try inside ``define_network``, so left alone it
        reaches the CLI as a traceback.
        """
        try:
            ok = self.provider.define_network(
                name=full_name, info=network_info, workdir=workdir)
        except (ConfigError, RuntimeError) as exc:
            self.logger.error(f"network {label}: could not be defined: {exc}")
            return 'failed'
        return 'created' if ok else 'failed'

    def _forget_cached_network(self, full_name: str) -> None:
        """
        Drop a network's entry from the projects cache.

        Needed before redefining it: ``check_network_exists()`` walks every
        cached project *including this one*, so a network's own leftover entry
        counts as a conflict with itself -- same name, same address -- and the
        redefine raises instead of running.
        """
        try:
            if self.cache.unregister_network(self.config['project'], full_name):
                self.logger.debug(f"removed {full_name} from the projects cache")
        except (KeyError, OSError, ValueError) as exc:
            # not fatal on its own, but the redefine that follows will fail on
            # the stale entry, so say why
            self.logger.warning(
                f"could not drop {full_name} from the projects cache: {exc}")

    @staticmethod
    def _teardown_info(plan: dict[str, Any],
                       network_info: dict[str, Any]) -> dict[str, Any]:
        """
        Describe the network **as it is now**, for the removal step.

        The iptables rules to withdraw are the ones that were installed for the
        current definition, so they follow its forward mode, bridge and subnet
        -- not the ones being defined in its place. Handing ``remove_network``
        the new configuration would, on a ``nat`` -> ``route`` change, try to
        withdraw route rules that were never added and leave the nat rules
        behind.

        No dhcp block is carried over: nothing in the teardown reads it, and
        reservations validated against the new subnet would be rejected when
        paired with the old address.
        """
        actual = plan.get('actual') or {}
        if not actual.get('mode'):
            return network_info

        info: dict[str, Any] = {'mode': actual['mode']}

        if actual.get('bridge_name'):
            info['bridge'] = {'name': actual['bridge_name']}
        if actual.get('ip_address'):
            info['ip'] = {'address': actual['ip_address']}
            if actual.get('netmask'):
                info['ip']['netmask'] = actual['netmask']

        return info

    def _recreate_network(self,
                          cluster: dict[str, Any],
                          network_name: str,
                          full_name: str,
                          network_info: dict[str, Any],
                          plan: dict[str, Any],
                          dry_run: bool,
                          allow_recreate: bool,
                          auto_accept: bool) -> str:
        """
        Destroy and redefine one network, then reconnect its guests.

        Split out of :meth:`reconcile_networks` because the disruptive path is
        where all the caveats live and it deserves to be read on its own.
        """
        attached = plan.get('attached_vms', [])
        attached_text = ', '.join(attached) if attached else 'none'

        if not allow_recreate:
            self.logger.warning(
                f"network {network_name}: the changes above need the network "
                f"to be destroyed and defined again, which libvirt cannot do "
                f"in place. Re-run with --recreate-networks to apply them "
                f"(attached VMs that would be restarted: {attached_text})")
            return 'skipped'

        if dry_run:
            self.logger.info(
                f"[dry-run] would recreate network {network_name} and "
                f"reconnect: {attached_text}")
            return 'skipped'

        if not auto_accept:
            print(f"\nNetwork '{network_name}' has to be destroyed and "
                  f"redefined to apply:")
            for change in plan['structural']:
                print(f"  - {change}")
            print("\nThe libvirt network will be deleted and recreated. These "
                  f"VMs lose their network link and will be reconnected, by "
                  f"a reboot if their machine type cannot hot-plug: "
                  f"{attached_text}\n")
            try:
                answer = input(
                    f"Type '{network_name}' to proceed: ").strip()
            except EOFError:
                # nothing is attached to stdin (a cron run, a pipeline): treat
                # that as a no rather than a traceback
                print("No input available, aborted.")
                return 'skipped'
            if answer != network_name:
                print("Aborted.")
                return 'skipped'

        self.logger.info(f"network {network_name}: removing")
        removed = True
        try:
            removed = self.provider.remove_network(
                name=full_name, info=self._teardown_info(plan, network_info))
        except RuntimeError as exc:
            # remove_network destroys and undefines before it touches iptables,
            # so the network is already gone: say what was left behind rather
            # than aborting half way
            self.logger.warning(
                f"network {network_name}: removed, but its firewall rules "
                f"could not be cleaned up: {exc}")

        if not removed:
            # destroy or undefine failed, so the network is still there.
            # Redefining on top of it would fail confusingly
            self.logger.error(
                f"network {network_name}: could not be removed, leaving it "
                f"as it is rather than defining on top of it")
            return 'failed'

        # the cache still lists the network we just removed, and
        # check_network_exists() would count that as a conflict with itself
        self._forget_cached_network(full_name)

        self.logger.info(f"network {network_name}: defining again")
        definition_info = network_info
        replacement_bridge = plan.get('replacement_bridge_name')
        if replacement_bridge:
            # This is an execution-time reservation, not a config pin. The
            # next reconcile still treats the bridge as auto-assigned and reads
            # its actual name from libvirt.
            definition_info = dict(network_info)
            configured_bridge = network_info.get('bridge')
            definition_info['bridge'] = {
                **(configured_bridge if isinstance(configured_bridge, dict)
                   else {}),
                'name': replacement_bridge,
            }
        if self._define_network(
                label=network_name, full_name=full_name,
                network_info=definition_info,
                workdir=cluster['workdir']) == 'failed':
            self.logger.error(
                f"network {network_name}: could not be defined again. The "
                f"attached VMs are left disconnected: {attached_text}")
            return 'failed'

        reattach_failed = []
        for domain in attached:
            outcome = self.provider.reattach_domain_network(domain, full_name)
            if outcome == 'failed':
                reattach_failed.append(domain)
                # remembered so the post-recreate IP wait does not sit on a
                # guest we already know is not coming back on its own
                if not hasattr(self, '_reattach_failed_vms'):
                    self._reattach_failed_vms = set()
                self._reattach_failed_vms.add(domain)
                self.logger.error(
                    f"{domain}: could not be reconnected to {network_name}, "
                    f"start it by hand")

        if reattach_failed:
            # the network is back but not every guest is, and saying
            # 'recreated' would paper over a VM that is still down
            return 'partial'

        return 'recreated'

    def destroy_networks(self) -> dict:
        """
        Destroy the networks specified in the cluster configuration (parallel).

        Returns:
            The ``failures`` dict from :meth:`_run_parallel`, empty when
            every network was removed.
        """
        def _destroy(cluster_name, cluster, network_name, network_info):
            _network_name = self.full_network_name(
                project_config=self.config,
                cluster_name=cluster_name,
                network_name=network_name
            )
            removed = self.session_for_cluster(cluster_name).remove_network(
                name=_network_name,
                info=network_info
            )
            if not removed:
                # Swallowing this used to log "removed network …" and delete
                # the XML while the network was still defined, so an orphaned
                # libvirt network looked like a clean teardown. Raising lands
                # it in _run_parallel's failure dict, which now gates the
                # cache unregistration and the workspace removal.
                raise NetworkError(
                    f"failed to remove network {_network_name}: it is still "
                    f"defined. Keeping {_network_name}_net_define.xml so the "
                    f"teardown can be retried.")
            self.logger.info(f"removed network {_network_name} in {cluster['workdir']}")
            xml_path = os.path.expanduser(
                os.path.join(cluster['workdir'], f'{_network_name}_net_define.xml'))
            if os.path.isfile(xml_path):
                os.remove(xml_path)
                self.logger.info(f"removed network XML {xml_path}")

        processes = [
            (f"{cluster_name}/{network_name}", _destroy,
             (cluster_name, cluster, network_name, network_info))
            for cluster_name, cluster in self._vm_clusters.items()
            for network_name, network_info in (cluster.get('networks') or {}).items()
        ]
        _results, failures = self._run_parallel(
            processes, op_label='destroy network')
        return failures
