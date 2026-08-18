"""Network lifecycle (define/reconcile/destroy) for BoxmanManager."""





import os
import time
from typing import Any

from boxman.exceptions import ConfigError
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
                self.session_for_cluster(cluster_name).define_network(
                    name=_network_name,
                    info=network_info,
                    workdir=cluster['workdir']
                )
                self.logger.status(f"defined network {_network_name} in {cluster['workdir']}")

    def reconcile_networks(self,
                           dry_run: bool = False,
                           allow_recreate: bool = False,
                           auto_accept: bool = False) -> dict[str, str]:
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

        Args:
            dry_run: report the plan and change nothing
            allow_recreate: permit the disruptive path
            auto_accept: skip the confirmation prompt for a recreate

        Returns:
            A mapping of network name to the action taken: one of ``created``,
            ``updated``, ``recreated``, ``skipped`` or ``failed``.
        """
        if not hasattr(self.provider, 'plan_network'):
            self.logger.debug(
                "provider does not support network reconciliation, skipping")
            return {}

        results: dict[str, str] = {}

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

    def _vms_worth_waiting_for(self) -> list[str]:
        """
        Project VMs minus the ones a recreate could not reconnect.

        Waiting on a guest we already reported as unreachable only burns the
        whole timeout before saying what we already knew.
        """
        unreachable = getattr(self, '_reattach_failed_vms', set())
        return sorted(set(self._get_project_vm_names()) - unreachable)

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
            self.session_for_cluster(cluster_name).remove_network(
                name=_network_name,
                info=network_info
            )
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
