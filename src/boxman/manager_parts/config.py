"""Config loading, schema versioning, and inventory rendering for BoxmanManager."""





import os
import re
from typing import Any

import yaml

from boxman.exceptions import ConfigError
from boxman.providers import PROVIDERS, primary_provider_type
from boxman.utils.jinja_env import create_jinja_env


class ConfigMixin:

    def load_app_config(self, config: dict[str, Any]) -> None:
        """
        Load the boxman application-level configuration (from boxman.yml).

        Args:
            config: The parsed boxman.yml configuration dictionary
        """
        self.app_config = config

    def load_config(self, config_path: str) -> dict[str, Any]:
        """
        Load configuration from a YAML file.

        The file is first treated as a Jinja template and rendered,
        then parsed as YAML.

        Args:
            config_path: Path to the configuration file

        Returns:
            Dict containing the configuration
        """
        # get the directory and filename for jinja template loading
        config_dir = os.path.dirname(os.path.abspath(config_path))
        config_filename = os.path.basename(config_path)

        # expose the config file location as env vars so templates can use
        # {{ env("BOXMAN_CONF_FILE") }} and {{ env("BOXMAN_CONF_DIR") }}.
        # setdefault lets a user-defined value take precedence.
        os.environ.setdefault('BOXMAN_CONF_FILE', os.path.abspath(config_path))
        os.environ.setdefault('BOXMAN_CONF_DIR', config_dir)

        # create jinja environment with boxman helpers (env(), env_required(), etc.)
        env = create_jinja_env(config_dir)

        # Read raw content and convert bare {{ name }} placeholders to
        # {name} markers before Jinja2 rendering.  Bare names (no parens,
        # dots, pipes, etc.) are task-command placeholders, not real Jinja2
        # expressions — real ones are function calls like {{ env("VAR") }}.
        #
        # IMPORTANT: variables defined by Jinja2 control flow ({% for %},
        # {% set %}) must be excluded from this substitution so that Jinja2
        # can render them correctly.  Without this exclusion, a loop like
        #   {% for suffix in 'abc' %} ... disk{{ suffix }} ... {% endfor %}
        # would have {{ suffix }} converted to {suffix} before Jinja2 runs,
        # leaving the literal string "disk{suffix}" in the output.
        raw_path = os.path.join(config_dir, config_filename)
        with open(raw_path) as fobj:
            raw_content = fobj.read()

        # collect all variable names introduced by jinja2 control flow
        jinja_ctrl_vars: set = set()
        jinja_ctrl_vars.update(re.findall(r'\{%-?\s*for\s+(\w+)\s+in\b', raw_content))
        jinja_ctrl_vars.update(re.findall(r'\{%-?\s*set\s+(\w+)\s*=', raw_content))

        def _preserve_jinja_vars(m: re.Match) -> str:
            name = m.group(1)
            if name in jinja_ctrl_vars:
                return m.group(0)   # keep {{ name }} for Jinja2 to render
            return '{' + name + '}'  # task placeholder → {name}

        preserved = re.sub(r"\{\{\s*(\w+)\s*\}\}", _preserve_jinja_vars, raw_content)

        # load and render the template from the pre-processed string
        template = env.from_string(preserved)

        # render the template
        # NOTE: pass os.environ as 'environ' (not 'env') to avoid shadowing
        # the env() helper function registered in the Jinja globals.
        rendered_yaml = template.render(
            environ=os.environ,
        )

        # parse the rendered yaml
        conf = yaml.safe_load(rendered_yaml)

        # dump the rendered yaml file for debugging/inspection
        rendered_filename = f"{os.path.splitext(config_filename)[0]}.rendered.yml"
        rendered_path = os.path.join(config_dir, rendered_filename)
        with open(rendered_path, 'w') as fobj:
            fobj.write(rendered_yaml)
            self.logger.info(f"rendered YAML template written to {rendered_path}")

        # apply schema-version handling (v2.0 boxes:→vms: normalization etc.)
        # before returning, so every downstream consumer sees the internal
        # (v1.0-shaped) config regardless of the on-disk schema version.
        conf = self._apply_config_version(conf)

        return conf

    def _apply_config_version(self, conf: dict[str, Any] | None) -> dict[str, Any] | None:
        """
        Dispatch on the config's ``version:`` key and return the config in
        boxman's internal (v1.0) shape.

        - No ``version:`` key, ``'1.0'`` (or the unquoted YAML numeric
          ``1`` / ``1.0``) → returned unchanged (v1.0 is supported
          indefinitely and stays byte-identical).
        - ``'2.0'`` (or unquoted ``2`` / ``2.0``) → :meth:`normalize_v2_config`
          (per-cluster ``boxes:``→``vms:`` for libvirt clusters, schema
          validation).
        - anything else → :class:`~boxman.exceptions.ConfigError`.

        The value is compared as a string so unquoted YAML numerics work:
        ``version: 2`` and ``version: 2.0`` both select v2.0. Quoting
        (``version: '2.0'``) is still recommended — see
        ``doc/docker-compose-provider/config-schema.md``.

        Args:
            conf: The parsed project configuration. A non-mapping root
                (``None``, list, scalar — e.g. an empty file) is returned
                unchanged rather than raising, so malformed input surfaces
                later through the normal path, not as an ``AttributeError``
                here.

        Returns:
            The version-normalized configuration.

        Raises:
            ConfigError: If ``version:`` is set to an unsupported value.
        """
        if not isinstance(conf, dict):
            return conf

        version = str(conf.get('version', '1.0')).strip()
        if version in ('1', '1.0'):
            self._reject_v1_docker_compose(conf)
            self._warn_on_v1_boxes(conf)
            return conf
        if version in ('2', '2.0'):
            return self.normalize_v2_config(conf)

        raise ConfigError(
            f"unsupported config version: '{version}' "
            f"(supported: '1.0', '2.0')"
        )

    def _reject_v1_docker_compose(self, conf: dict[str, Any]) -> None:
        """
        Reject a v1.0 / versionless config that resolves any cluster to the
        docker-compose provider.

        The docker-compose provider consumes ``boxes:``, which v1.0 ignores
        (see :meth:`_warn_on_v1_boxes`). Without this guard the cluster would
        be picked up by ``_compose_clusters`` and provisioned into live
        services *despite* the v1 nudge telling the user those boxes are
        ignored — the warning and the behaviour would contradict. The
        provider is new in this epic, so no legitimate v1 config uses it;
        failing fast with a clear message is safe.

        Raises:
            ConfigError: If a cluster's effective provider (per-cluster
                ``provider:`` or the primary provider) is ``docker-compose``.
        """
        for cluster_name, cluster in (conf.get('clusters') or {}).items():
            if not isinstance(cluster, dict):
                continue
            provider = cluster.get('provider') or primary_provider_type(conf)
            if provider == 'docker-compose':
                raise ConfigError(
                    f"cluster '{cluster_name}' uses the docker-compose "
                    f"provider, which requires version: '2.0' (docker-compose "
                    f"clusters consume 'boxes:', ignored under v1.0). Add "
                    f"\"version: '2.0'\" to the config."
                )

    def _warn_on_v1_boxes(self, conf: dict[str, Any]) -> None:
        """
        Warn when a v1.0 config uses the v2.0-only ``boxes:`` key.

        In v1.0 nothing reads ``boxes:``, so such boxes are silently
        ignored — including in a partial migration where a cluster carries
        both ``vms:`` (provisioned) and ``boxes:`` (dropped). The warning
        fires whenever ``boxes:`` is present, regardless of ``vms:``. It is
        a log-only nudge toward ``version: '2.0'`` and does not change v1.0
        provisioning behaviour.
        """
        for cluster_name, cluster in (conf.get('clusters') or {}).items():
            if isinstance(cluster, dict) and 'boxes' in cluster:
                self.logger.warning(
                    f"cluster '{cluster_name}' uses 'boxes:' but the config "
                    f"is v1.0 — 'boxes:' is only recognised under "
                    f"version: '2.0', so these boxes are ignored. Add "
                    f"\"version: '2.0'\" or rename 'boxes:' to 'vms:'."
                )

    def normalize_v2_config(self, conf: dict[str, Any]) -> dict[str, Any]:
        """
        Normalize a v2.0 config into boxman's internal (v1.0) shape.

        For each cluster the effective provider is
        ``cluster.get('provider') or primary_provider_type(conf)`` (the
        same resolution :meth:`provider_type_for_cluster` uses). Then, per
        the design compatibility matrix:

        - **libvirt** clusters: ``boxes:`` is renamed to ``vms:`` so the
          ~35 existing libvirt call sites are untouched; a cluster still
          using ``vms:`` is accepted with a deprecation warning; declaring
          both ``boxes:`` and ``vms:`` is ambiguous and rejected.
        - **docker-compose** clusters: ``boxes:`` is kept as-is (consumed
          by the Phase 3 ``DockerComposeSession``); ``vms:`` is rejected.
        - any **other** provider (the legacy ``virtualbox``, or an unknown
          value / typo) is rejected — v2.0 supports only ``libvirt`` and
          ``docker-compose`` today, and silently leaving ``boxes:``
          untouched would provision an empty cluster.

        A per-box ``provider:`` key is a config error (ADR-001): providers
        are declared at the cluster level only.

        The config is **mutated in place** and also returned; it is meant
        to run once over a freshly parsed config (``load_config`` re-parses
        each call). Re-running over an already-normalized dict — whose
        ``boxes:`` were popped but whose ``version:`` stays ``'2.0'`` —
        would treat the renamed ``vms:`` as a legacy key and emit a
        spurious deprecation warning.

        Args:
            conf: The parsed v2.0 project configuration.

        Returns:
            The normalized configuration (same object).

        Raises:
            ConfigError: On an ambiguous, provider-incompatible, or
                unknown-provider cluster, or a per-box ``provider:``.
        """
        for cluster_name, cluster in (conf.get('clusters') or {}).items():
            if not isinstance(cluster, dict):
                continue

            provider = cluster.get('provider') or primary_provider_type(conf)
            has_boxes = 'boxes' in cluster
            has_vms = 'vms' in cluster

            # ADR-001: a `provider:` on an individual box is a config error;
            # inspect both the boxes: mapping and the legacy vms: mapping.
            self._reject_per_box_provider(cluster_name, cluster.get('boxes'))
            self._reject_per_box_provider(cluster_name, cluster.get('vms'))

            if provider == 'libvirt':
                if has_boxes and has_vms:
                    raise ConfigError(
                        f"cluster '{cluster_name}' declares both 'boxes:' "
                        f"and 'vms:' — use one (prefer 'boxes:' in v2.0)."
                    )
                if has_boxes:
                    cluster['vms'] = cluster.pop('boxes')
                elif has_vms:
                    self.logger.warning(
                        f"cluster '{cluster_name}' uses the legacy 'vms:' "
                        f"key under version: '2.0' — 'boxes:' is the "
                        f"preferred generic key. 'vms:' remains accepted."
                    )
            elif provider == 'docker-compose':
                if has_vms:
                    raise ConfigError(
                        f"cluster '{cluster_name}' declares 'vms:'; "
                        f"docker-compose clusters only support 'boxes:'."
                    )
                # 'boxes:' is kept verbatim for the docker-compose provider.
            elif provider in PROVIDERS:
                # a registered provider not yet wired for v2.0 (virtualbox)
                raise ConfigError(
                    f"cluster '{cluster_name}': provider '{provider}' is "
                    f"not supported under config version '2.0' yet — use "
                    f"version: '1.0' with 'vms:' for '{provider}' clusters."
                )
            else:
                raise ConfigError(
                    f"cluster '{cluster_name}': unknown provider "
                    f"'{provider}' (known: {', '.join(sorted(PROVIDERS))})."
                )

        return conf

    @staticmethod
    def _reject_per_box_provider(cluster_name: str, boxes: Any) -> None:
        """
        Raise if any box in *boxes* declares a ``provider:`` key.

        ADR-001 assigns per-box-provider validation to Phase 2: a provider
        is a cluster-level concern, so a ``provider:`` inside an individual
        box (which ``provider_type_for_cluster`` would silently ignore) is
        rejected rather than accepted and dropped.
        """
        for box_name, box in (boxes or {}).items():
            if isinstance(box, dict) and 'provider' in box:
                raise ConfigError(
                    f"box '{box_name}' in cluster '{cluster_name}' declares "
                    f"a 'provider:' key — per-box providers are not "
                    f"supported (ADR-001); declare 'provider:' on the "
                    f"cluster instead."
                )

    @staticmethod
    def _render_inventory(host_aliases, cluster_groups, host_extra_vars=None) -> str:
        """
        Render an Ansible ``01-hosts.yml`` body.

        Args:
            host_aliases: iterable of ``(host_key, boxman_alias)`` placed under
                ``all.hosts``.
            cluster_groups: mapping of group name → list of host keys, rendered
                as ``all.children.<group>.hosts``.
            host_extra_vars: optional ``{host_key: {var: value}}`` of extra host
                vars (e.g. ``ansible_connection``/``ansible_host`` for
                docker-compose containers reached via ``community.docker``).
                VM hosts pass nothing and render exactly as before.

        Returns:
            The YAML text (identical in shape to what boxman has always
            generated for the combined workspace inventory).
        """
        host_extra_vars = host_extra_vars or {}
        host_blocks: list[str] = []
        for host, alias in host_aliases:
            lines = [f'        {host}:', f'          boxman_alias: "{alias}"']
            for var, value in (host_extra_vars.get(host) or {}).items():
                lines.append(f'          {var}: "{value}"')
            host_blocks.append('\n'.join(lines))
        host_lines = '\n'.join(host_blocks)
        children_lines: list[str] = []
        for group, hosts in cluster_groups.items():
            children_lines.append(f'    {group}:')
            children_lines.append('      hosts:')
            for host in hosts:
                children_lines.append(f'        {host}:')
        children_section = '\n'.join(children_lines)
        return (
            f"---\n"
            f"all:\n"
            f"  hosts:\n"
            f"{host_lines}\n"
            f"  children:\n"
            f"{children_section}\n"
        )

    @staticmethod
    def _cluster_inventory_key(cluster: dict) -> str:
        """
        Resolve the ``files`` key for a cluster's own ``01-hosts.yml``.

        Honors a per-cluster ``inventory:`` override (relative to the cluster
        workdir, or absolute); otherwise defaults to ``inventory/01-hosts.yml``
        under the cluster workdir. The key is consumed by ``provision_files``,
        which writes cluster files with ``rootdir=cluster['workdir']`` — so an
        absolute override naturally bypasses the workdir, mirroring how the
        workspace-level custom inventory path is handled.
        """
        inv = cluster.get('inventory')
        if inv:
            inv = os.path.normpath(os.path.expanduser(str(inv)))
            return os.path.join(inv, '01-hosts.yml')
        return os.path.join('inventory', '01-hosts.yml')
