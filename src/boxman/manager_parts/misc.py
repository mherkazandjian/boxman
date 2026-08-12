"""Inspection and ad-hoc execution handlers (ps, show-conf, run-task, ...) for BoxmanManager."""

import json
import os

import yaml

from boxman import log
from boxman.exceptions import ConfigError
from boxman.task_runner import TaskRunner


class MiscMixin:

    def list_projects(self, cli_args) -> None:
        """
        List all registered projects.
        """
        projects = self.cache.list_projects()

        pretty = getattr(cli_args, 'pretty', None) if cli_args else None
        use_json = getattr(cli_args, 'json', False) if cli_args else False
        use_color = getattr(cli_args, 'color', 'yes') != 'no' if cli_args else True

        # --- JSON output ---
        if use_json:
            print(json.dumps(projects if projects else {}, indent=2, default=str))
            return

        # ANSI helpers
        if use_color and pretty:
            bold = "\033[1m"
            cyan = "\033[1;36m"
            green = "\033[1;32m"
            yellow = "\033[1;33m"
            dim = "\033[2m"
            reset = "\033[0m"
        else:
            bold = cyan = green = yellow = dim = reset = ""

        if not projects:
            if pretty:
                print(f"{yellow}No projects registered.{reset}")
            else:
                self.logger.info("No projects registered.")
            return

        if pretty == 'table':
            # Collect rows: [project, config, runtime, networks_summary]
            rows = []
            for proj_name, proj_info in projects.items():
                if isinstance(proj_info, dict):
                    conf = proj_info.get('conf', 'n/a')
                    runtime = proj_info.get('runtime', 'n/a')
                    networks = proj_info.get('networks', {})
                    net_parts = []
                    for net_name, net_info in networks.items():
                        if isinstance(net_info, dict):
                            ip = net_info.get('ip_address', 'n/a')
                            bridge = net_info.get('bridge_name', 'n/a')
                            net_parts.append(f"{net_name} (ip={ip}, br={bridge})")
                        else:
                            net_parts.append(net_name)
                    nets_str = "; ".join(net_parts) if net_parts else "-"
                else:
                    conf = str(proj_info)
                    runtime = "n/a"
                    nets_str = "-"
                rows.append((proj_name, conf, runtime, nets_str))

            headers = ("PROJECT", "CONFIG", "RUNTIME", "NETWORKS")
            # compute column widths
            col_widths = [len(h) for h in headers]
            for row in rows:
                for i, cell in enumerate(row):
                    col_widths[i] = max(col_widths[i], len(cell))

            def fmt_row(cells, bold=False):
                parts = []
                for i, cell in enumerate(cells):
                    parts.append(cell.ljust(col_widths[i]))
                line = "  ".join(parts)
                if bold:
                    return f"{bold}{line}{reset}"
                return line

            print()
            print(fmt_row(headers, bold=True))
            print("  ".join("-" * w for w in col_widths))
            for row in rows:
                print(fmt_row(row))
            print()

        elif pretty == 'plain':
            print()
            print(f"{bold}Registered projects:{reset}")
            print()
            for proj_name, proj_info in projects.items():
                print(f"  {cyan}{proj_name}{reset}")
                if isinstance(proj_info, dict):
                    conf = proj_info.get('conf', 'n/a')
                    runtime = proj_info.get('runtime', 'n/a')
                    print(f"    {dim}config:{reset}  {conf}")
                    print(f"    {dim}runtime:{reset} {runtime}")

                    networks = proj_info.get('networks', {})
                    if networks:
                        print(f"    {dim}networks:{reset}")
                        for net_name, net_info in networks.items():
                            ip = net_info.get('ip_address', 'n/a') if isinstance(net_info, dict) else 'n/a'
                            bridge = net_info.get('bridge_name', 'n/a') if isinstance(net_info, dict) else 'n/a'
                            print(f"      {green}-{reset} {net_name}")
                            print(f"          {dim}ip:{reset} {ip}  {dim}bridge:{reset} {bridge}")
                else:
                    print(f"    {proj_info}")
                print()

        else:
            # default logger-based output (no --pretty, no --json)
            self.logger.info("Registered projects:\n")
            for proj_name, proj_info in projects.items():
                self.logger.info(f"  project: {proj_name}")
                if isinstance(proj_info, dict):
                    conf = proj_info.get('conf', 'n/a')
                    runtime = proj_info.get('runtime', 'n/a')
                    self.logger.info(f"    config:  {conf}")
                    self.logger.info(f"    runtime: {runtime}")

                    networks = proj_info.get('networks', {})
                    if networks:
                        self.logger.info("    networks:")
                        for net_name, net_info in networks.items():
                            ip = net_info.get('ip_address', 'n/a') if isinstance(net_info, dict) else 'n/a'
                            bridge = net_info.get('bridge_name', 'n/a') if isinstance(net_info, dict) else 'n/a'
                            self.logger.info(f"      - {net_name}")
                            self.logger.info(f"          ip: {ip}  bridge: {bridge}")
                else:
                    self.logger.info(f"    {proj_info}")
                self.logger.info("")

    ### end control vm functions ####
    ### task runner functions ####
    def run_task(self, cli_args):
        """
        Run a named task or ad-hoc command with the workspace environment.
        """
        runner = TaskRunner(
            config=self.config,
            cluster_name=getattr(cli_args, "cluster", None),
        )

        if getattr(cli_args, "list_tasks", False):
            tasks = runner.list_tasks()
            if not tasks:
                print("No tasks defined in conf.yml")
                return
            max_name = max(len(t["name"]) for t in tasks)
            for task in tasks:
                desc = task["description"]
                print(f"  {task['name']:<{max_name}}  {desc}")
            return

        extra_args = getattr(cli_args, "extra_args", None) or []

        if getattr(cli_args, "cmd", None):
            exit_code = runner.run_command(
                cli_args.cmd,
                extra_args,
                ansible_flags=getattr(cli_args, "ansible_flags", None),
            )
        else:
            task_name = cli_args.task_name
            remaining = getattr(cli_args, "remaining_args", [])

            # Parse dynamic task flags from remaining CLI args based on
            # {placeholder} markers in the task command.
            task_flags = {}
            if task_name and task_name in runner.tasks:
                task_cmd = runner.tasks[task_name].get("command", "")
                placeholders = TaskRunner.extract_placeholders(task_cmd)

                if placeholders and remaining:
                    placeholder_set = set(placeholders)
                    # Python's argparse._parse_optional() returns None (positional)
                    # for argument strings that contain a space, so a flag value
                    # like '--limit node01' (a single bash-quoted arg) lands in
                    # extra_args instead of remaining.  We detect this by checking
                    # whether the next element in remaining is itself a recognised
                    # placeholder flag; if so, the current flag's value was
                    # consumed by argparse and is at the front of extra_args.
                    extra_args = list(extra_args)  # mutable copy; pops are visible at runner.run()
                    i = 0
                    while i < len(remaining):
                        arg = remaining[i]
                        if not arg.startswith("--"):
                            log.error(f"unrecognized argument: {arg}")
                            import sys
                            sys.exit(1)

                        name = arg[2:].replace("-", "_")
                        if name not in placeholder_set:
                            log.error(f"unrecognized argument: {arg}")
                            import sys
                            sys.exit(1)

                        # Determine the value for this flag.  Normal case:
                        # remaining[i + 1] is the value.  Exception: if that
                        # next arg is itself a recognised placeholder flag, the
                        # value for this flag was misclassified as a positional
                        # by argparse and sits at the front of extra_args.
                        value = None
                        if i + 1 < len(remaining):
                            next_arg = remaining[i + 1]
                            if next_arg.startswith("--"):
                                next_name = next_arg[2:].replace("-", "_")
                                if next_name not in placeholder_set:
                                    # next_arg is a value that starts with --
                                    value = next_arg
                                    i += 2
                                # else: next_arg is another flag → fall through
                            else:
                                value = next_arg
                                i += 2

                        if value is None:
                            # Value not in remaining; consume from extra_args.
                            if not extra_args:
                                log.error(f"argument {arg}: expected a value")
                                import sys
                                sys.exit(1)
                            value = extra_args.pop(0)
                            i += 1

                        task_flags[name] = value
                elif remaining:
                    log.error(
                        f"unrecognized arguments: {' '.join(remaining)}. "
                        f"Task '{task_name}' has no {{placeholder}} markers "
                        f"in its command."
                    )
                    import sys
                    sys.exit(1)

            exit_code = runner.run(task_name, extra_args, task_flags=task_flags)

        if exit_code != 0:
            import sys
            sys.exit(exit_code)

    def _get_vm_list(self) -> list[tuple[str, str, str]]:
        """
        Return the ordered list of VMs from the config.

        Returns:
            List of (cluster_name, vm_name, full_virsh_name) tuples.
            The list index is the boxman VM id (0-based).
        """
        project = self.config.get("project", "")
        prj_prefix = f"bprj__{project}__bprj_"
        vms = []
        for cluster_name, cluster in self.config.get("clusters", {}).items():
            for vm_name in cluster.get("vms", {}).keys():
                full_name = f"{prj_prefix}{cluster_name}_{vm_name}"
                vms.append((cluster_name, vm_name, full_name))
        return vms

    def resolve_vm_name(self, identifier: str) -> str:
        """
        Resolve a VM identifier to the short name used in the workspace
        (``{cluster}_{vm}``).

        The identifier can be:
        - A numeric boxman id (from ``boxman ps``)
        - A VM name (returned as-is)

        Raises:
            ValueError: If the numeric id is out of range.
        """
        if identifier.isdigit():
            vm_list = self._get_vm_list()
            idx = int(identifier)
            if idx < 0 or idx >= len(vm_list):
                raise ValueError(
                    f"VM id {idx} out of range (0-{len(vm_list) - 1})"
                )
            cluster_name, vm_name, _ = vm_list[idx]
            return f"{cluster_name}_{vm_name}"
        return identifier

    def show_conf(self, cli_args, merged_provider=None):
        """
        Display the effective merged configuration.

        Shows the merged provider config and the rendered project config.
        With ``--json``, outputs a single JSON object.
        """
        as_json = getattr(cli_args, 'json', False)

        # Read the rendered config file
        rendered_config = None
        if self.config_path:
            config_dir = os.path.dirname(os.path.abspath(self.config_path))
            config_basename = os.path.splitext(os.path.basename(self.config_path))[0]
            rendered_path = os.path.join(config_dir, f"{config_basename}.rendered.yml")
            if os.path.isfile(rendered_path):
                with open(rendered_path) as fobj:
                    rendered_config = fobj.read()

        if as_json:
            output = {
                "provider": merged_provider or {},
                "rendered_config": yaml.safe_load(rendered_config) if rendered_config else None,
            }
            print(json.dumps(output, indent=2, default=str))
            return

        # Plain text output
        print("Provider config")
        print("───────────────")
        if merged_provider:
            for key, value in merged_provider.items():
                print(f"  {key}: {value}")
        else:
            print("  (none)")

        print()
        print("Rendered config")
        print("───────────────")
        if rendered_config:
            print(rendered_config)
        else:
            print("  (conf.rendered.yml not found — run 'boxman provision' first)")

    def ps(self, cli_args):
        """
        Display the state of all project VMs in a table.

        With ``-p``, two extra columns are added showing the provider-specific
        virsh Id and virsh Name for each VM.
        """
        provider_info = getattr(cli_args, 'provider_info', False)
        as_json = getattr(cli_args, 'json', False)

        vm_list = self._get_vm_list()
        dc_clusters = self._compose_clusters

        if not vm_list and not dc_clusters:
            if as_json:
                print(json.dumps([], indent=2))
            else:
                print("No VMs or containers defined in configuration")
            return

        # Query virsh for VM states — only when the project has libvirt
        # clusters (a dc-only project has no libvirt to talk to).
        vm_info: dict[str, tuple[str, str]] = {}
        if vm_list and self._has_libvirt_clusters():
            result = self._virsh().execute("list", "--all", hide=True, warn=True)
            if result.ok:
                for line in result.stdout.strip().splitlines():
                    line = line.strip()
                    if not line or line.startswith("---") or line.startswith("Id"):
                        continue
                    parts = line.split(None, 2)
                    if len(parts) >= 3:
                        virsh_id, virsh_name, state = parts[0], parts[1], parts[2].strip()
                        vm_info[virsh_name] = (virsh_id, state)

        # Build records: libvirt VMs first (numeric ids), then dc containers.
        records = []
        for idx, (cluster_name, vm_name, full_name) in enumerate(vm_list):
            virsh_id, state = vm_info.get(full_name, ("-", "not created"))
            rec = {"id": idx, "cluster": cluster_name, "vm": vm_name,
                   "provider": self.provider_type_for_cluster(cluster_name),
                   "state": state}
            if provider_info:
                rec["virsh_id"] = virsh_id
                rec["virsh_name"] = full_name
            records.append(rec)

        for cluster_name, cluster in dc_clusters.items():
            try:
                status = {
                    r["service"]: r for r in
                    self._dc_session(cluster_name).container_status(
                        cluster_name, cluster)
                }
            except Exception as exc:  # a status probe must never break `ps`
                self.logger.warning(
                    f"could not query containers for '{cluster_name}': {exc}")
                status = {}
            for box_name in (cluster.get("boxes") or {}):
                row = status.get(box_name)
                state = "not created"
                if row:
                    state = row["state"] + (
                        f" ({row['health']})" if row.get("health") else "")
                rec = {"id": "-", "cluster": cluster_name, "vm": box_name,
                       "provider": "docker-compose", "state": state}
                if provider_info:
                    rec["virsh_id"] = "-"
                    rec["virsh_name"] = "-"
                records.append(rec)

        if as_json:
            print(json.dumps(records, indent=2))
            return

        if not records:
            print("No VMs or containers defined in configuration")
            return

        # Print table
        if provider_info:
            headers = ("Id", "Cluster", "Name", "Provider", "State",
                       "Virsh Id", "Virsh Name")
            rows = [(str(r["id"]), r["cluster"], r["vm"], r["provider"],
                     r["state"], r["virsh_id"], r["virsh_name"]) for r in records]
        else:
            headers = ("Id", "Cluster", "Name", "Provider", "State")
            rows = [(str(r["id"]), r["cluster"], r["vm"], r["provider"],
                     r["state"]) for r in records]

        col_count = len(headers)
        widths = [
            max(len(headers[i]), *(len(row[i]) for row in rows))
            for i in range(col_count)
        ]
        print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
        print("  ".join("-" * w for w in widths))
        for row in rows:
            print("  ".join(val.ljust(w) for val, w in zip(row, widths)))

    def ssh_session(self, cli_args):
        """
        Open an interactive SSH session to a VM.
        """
        vm_name = getattr(cli_args, "vm_name", None)

        # Resolve numeric id to VM name
        if vm_name:
            vm_name = self.resolve_vm_name(vm_name)

        runner = TaskRunner(
            config=self.config,
            cluster_name=getattr(cli_args, "cluster", None),
        )

        exit_code = runner.ssh_to_host(vm_name)

        if exit_code != 0:
            import sys
            sys.exit(exit_code)

    def _resolve_container_target(self, target: str) -> tuple[str, str]:
        """Resolve a ``boxman exec`` target to ``(cluster, box)``.

        ``<cluster>.<box>`` is split on the last dot; a bare ``<box>`` is
        allowed when exactly one docker-compose cluster defines it. Raises
        ``ConfigError`` for an unknown target or one that names a libvirt VM
        (which should use ``boxman ssh``).
        """
        dc_clusters = self._compose_clusters
        if '.' in target:
            cluster, box = target.rsplit('.', 1)
            if cluster not in (self.config.get('clusters') or {}):
                raise ConfigError(f"no cluster '{cluster}' in this project.")
            if not self._is_compose_cluster(cluster):
                raise ConfigError(
                    f"cluster '{cluster}' is a libvirt cluster — use "
                    f"'boxman ssh' for VMs, not 'boxman exec'."
                )
            return cluster, box
        matches = [
            cn for cn, cl in dc_clusters.items()
            if target in (cl.get('boxes') or {})
        ]
        if len(matches) == 1:
            return matches[0], target
        if not matches:
            raise ConfigError(
                f"no docker-compose container '{target}' — give the target as "
                f"'<cluster>.<box>' (dc clusters: "
                f"{', '.join(dc_clusters) or 'none'})."
            )
        raise ConfigError(
            f"container '{target}' is ambiguous across clusters "
            f"{', '.join(matches)} — use '<cluster>.<box>'."
        )

    def exec_container(self, cli_args):
        """Exec into a docker-compose container via ``docker compose exec``.

        Interactive shell when no command is given (``--shell`` picks the shell);
        a trailing command (after ``--`` when it has flags) runs
        non-interactively. Runs the argv list with ``shell=False`` and inherited
        stdio, so an interactive shell attaches to the real terminal.
        """
        import subprocess
        import sys

        cmd = list(getattr(cli_args, 'cmd', None) or [])
        shell = getattr(cli_args, 'shell', None) or 'sh'

        cluster, box = self._resolve_container_target(cli_args.target)
        cluster_cfg = self.config['clusters'][cluster]
        argv = self._dc_session(cluster).exec_command_for(
            cluster, cluster_cfg, box, cmd=cmd or None, shell=shell
        )
        self.logger.info(f"exec: {' '.join(argv)}")
        result = subprocess.run(argv)
        if result.returncode != 0:
            sys.exit(result.returncode)
