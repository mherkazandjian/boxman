"""Netlab/containerlab lifecycle for BoxmanManager."""

import json
import os

from boxman.netlab import shared_bridges


class NetlabMixin:

    ### end register the project in the cache
    ### networks define / remove / destroy
    def ensure_shared_bridges(self) -> None:
        """Create host Linux bridges declared under top-level ``shared_networks:``.

        Idempotent and cross-project safe. No-op when ``shared_networks`` is
        absent. Bridges are intentionally *not* removed on destroy — multiple
        boxman projects can share the same bridge.
        """
        shared = (self.config or {}).get('shared_networks')
        if not shared:
            return
        self.logger.info(f"ensuring {len(shared)} shared bridge(s) exist on host")
        shared_bridges.ensure(shared)

    def deploy_netlab(self) -> None:
        """Render and deploy the containerlab topology, if configured.

        No-op when there's no ``containerlab:`` block or ``enabled: false``.
        Runs preflight first so a missing ``containerlab`` / ``docker`` binary
        surfaces a clear error before any shell-out is attempted.
        """
        netlab = self.netlab
        if netlab is None:
            return
        netlab.preflight()
        # Resolve startup-config templates relative to the *config file's*
        # directory. abspath() is required: run the documented way
        # (`cd boxes/<box> && boxman up`), config_path is the bare relative
        # default "conf.yml", so os.path.dirname() would return "" — which
        # render_topology treats as "unset" and falls back to the workspace
        # dir, where configs/ does not exist (FileNotFoundError on sw1's
        # startup-config).
        source_root = (os.path.dirname(os.path.abspath(self.config_path))
                       if self.config_path else None)
        netlab.render_topology(source_root=source_root)
        netlab.deploy()

    def destroy_netlab(self) -> None:
        """Tear down the containerlab lab, if configured.

        Ordered before libvirt/network teardown in ``deprovision`` so lab
        container veths release their hold on shared bridges first.
        """
        netlab = self.netlab
        if netlab is None:
            return
        try:
            netlab.preflight()
        except Exception as exc:
            # Binary missing post-hoc is survivable — we still want to tear
            # down libvirt state even if containerlab is no longer on PATH.
            self.logger.warning(f"skipping containerlab destroy: {exc}")
            return
        netlab.destroy()

    def ensure_netlab_up(self) -> None:
        """Idempotent reconciliation of the containerlab lab state.

        Called from ``boxman up`` so `down`/`up` cycles (or a host reboot)
        bring the lab back alongside the VMs. Deploys fresh if absent,
        starts stopped nodes if some containers linger, no-ops if already
        running.
        """
        netlab = self.netlab
        if netlab is None:
            return
        netlab.preflight()
        # Render the topology so ensure_up has a .clab.yml to deploy from
        # if the lab is missing entirely. abspath() so a bare relative
        # --conf (the default "conf.yml") resolves to the box directory
        # rather than "" — see deploy_netlab() for the full rationale.
        source_root = (os.path.dirname(os.path.abspath(self.config_path))
                       if self.config_path else None)
        netlab.render_topology(source_root=source_root)
        netlab.ensure_up()

    ### end task runner functions ####
    ### netlab (containerlab) CLI handlers ####
    def netlab_deploy(self, cli_args):
        """``boxman netlab deploy`` — render topology and deploy the lab."""
        if self.netlab is None:
            self.logger.error(
                "no 'containerlab:' block in conf.yml (or enabled: false)")
            return
        self.deploy_netlab()

    def netlab_destroy(self, cli_args):
        """``boxman netlab destroy`` — destroy only the containerlab lab."""
        if self.netlab is None:
            self.logger.error(
                "no 'containerlab:' block in conf.yml (or enabled: false)")
            return
        self.destroy_netlab()

    def netlab_inspect(self, cli_args):
        """``boxman netlab inspect`` — print lab state as JSON."""
        if self.netlab is None:
            self.logger.error(
                "no 'containerlab:' block in conf.yml (or enabled: false)")
            return
        self.netlab.preflight()
        print(json.dumps(self.netlab.inspect(), indent=2))

    def netlab_ssh(self, cli_args):
        """``boxman netlab ssh <node>`` — print ssh command for a lab node."""
        if self.netlab is None:
            self.logger.error(
                "no 'containerlab:' block in conf.yml (or enabled: false)")
            return
        node_name = getattr(cli_args, "node", None)
        if not node_name:
            self.logger.error("missing required argument: node name")
            return
        user = getattr(cli_args, "user", None)
        print(self.netlab.ssh_command(node_name, user=user))
