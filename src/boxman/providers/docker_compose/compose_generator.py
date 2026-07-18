"""
ComposeGenerator — translate a boxman docker-compose *cluster* into a
``docker-compose.yml`` dict and write it to the cluster workdir.

Scope: services (image / build / command / environment / ports /
depends_on / restart / healthcheck), cluster-internal bridge networks,
``shared_networks`` bridges attached as docker **macvlan** networks (Phase 4
— L2 to libvirt VMs on the same host bridge), structured ``volumes:`` (Phase
5 — named / bind / workdir mounts), and the ``compose_extra:`` escape hatch
(deep-merged verbatim, per-cluster and per-box — design decision D7).

A box's ``volumes:`` is a list of structured mounts:

- **named** (``{name: pg_data, container_path: /var/lib/postgresql/data}``) —
  emitted as ``pg_data:/var/lib/postgresql/data`` plus a top-level
  ``volumes: {pg_data: {driver: local}}``. An optional ``size:`` is
  **advisory** (warned) — docker's local driver does not enforce quotas.
- **bind** (``{host_path: ./configs, container_path: /etc/app,
  readonly: true}``) — emitted as ``<abs host>:/etc/app:ro``; a relative
  ``host_path`` is resolved against the project ``conf.yml`` dir (D4). A
  "workdir mount" is just a bind mount with ``host_path: .``.

A box's ``networks:`` may be a plain list (``[app_bridge, backend]`` — the
container gets an auto-assigned address on each) or a mapping that pins a
static address on a shared bridge
(``{app_bridge: {ipv4_address: 10.10.0.5}}``). A shared bridge referenced by
a box must declare a ``subnet:`` under ``shared_networks:`` so docker's
macvlan IPAM has an address pool.

The generated file is a fidelity artifact — inspectable and hand-runnable
with ``docker compose -f <cluster_workdir>/docker-compose.yml ps`` (D5).
"""

from __future__ import annotations

import os
import re
from typing import Any

import yaml

from boxman import log
from boxman.exceptions import ConfigError

#: compose service keys copied through verbatim from a box definition
_PASSTHROUGH_KEYS = (
    "image",
    "command",
    "environment",
    "ports",
    "depends_on",
    "restart",
    "healthcheck",
)

#: the ``{word}`` signature left by config preprocessing when it mangles a
#: bare Jinja ``{{ word }}`` in a compose value. Excludes ``${word}`` (a `$`
#: before the brace), which is a safe compose interpolation, not corruption.
_CORRUPTED_TEMPLATE_RE = re.compile(r"(?<!\$)\{[a-zA-Z_]\w*\}")

#: every box key the generator understands. Anything else is warned about and
#: dropped.
_KNOWN_BOX_KEYS = frozenset(_PASSTHROUGH_KEYS) | {
    "build",
    "networks",
    "volumes",
    "compose_extra",
}

#: keys understood inside a structured ``volumes:`` entry. Others warn.
_KNOWN_VOLUME_KEYS = frozenset({
    "name",
    "container_path",
    "host_path",
    "readonly",
    "size",
    "compose_extra",
})


class ComposeGenerator:
    """Render a docker-compose cluster to a ``docker-compose.yml`` dict/file."""

    def __init__(self, logger=None) -> None:
        self.logger = logger or log

    # -- public API --------------------------------------------------------

    def generate(
        self,
        cluster_name: str,
        cluster_cfg: dict[str, Any],
        conf_dir: str,
        shared_networks: dict[str, Any] | None = None,
        project_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Build the compose dict for *cluster_name*.

        Args:
            cluster_name: The cluster name (for diagnostics).
            cluster_cfg: The cluster config (``boxes:``, ``networks:``,
                optional ``compose_extra:``).
            conf_dir: Directory of the project ``conf.yml`` — ``build.context``
                is resolved absolute against it (D4).
            shared_networks: The top-level ``shared_networks:`` block (name →
                ``{bridge, subnet, gateway?, ip_range?, ...}``). A box network
                ref that matches a key here is emitted as a docker **macvlan**
                network over that host bridge (Phase 4).
            project_name: The compose project name boxman runs the stack under.
                When given, it is emitted as a top-level ``name:`` so the file
                is hand-runnable (``docker compose -f <file> ps``) under the
                same project boxman uses — the D5 fidelity claim. The runner
                still passes ``-p`` explicitly, which overrides ``name:``, so
                its behaviour is unchanged.

        Returns:
            The compose dict (no top-level ``version:`` — the compose spec
            treats it as obsolete).
        """
        shared_networks = shared_networks or {}
        cluster_networks = cluster_cfg.get("networks") or {}
        #: shared_networks keys actually attached by a box in this cluster —
        #: only these become top-level macvlan networks (insertion order via
        #: shared_networks below keeps the output deterministic).
        referenced_shared: set[str] = set()
        #: named volumes defined by any box → the top-level ``volumes:`` block,
        #: in first-seen order (a name shared by two boxes is defined once).
        named_volumes: dict[str, Any] = {}

        services: dict[str, Any] = {}
        for box_name, box in (cluster_cfg.get("boxes") or {}).items():
            services[box_name] = self._service(
                cluster_name, box_name, box or {}, conf_dir,
                cluster_networks, shared_networks, referenced_shared,
                named_volumes,
            )

        compose: dict[str, Any] = {}
        if project_name:
            compose["name"] = project_name
        compose["services"] = services
        networks = self._networks(cluster_networks)
        networks.update(
            self._shared_macvlan_networks(referenced_shared, shared_networks)
        )
        if networks:
            compose["networks"] = networks
        if named_volumes:
            compose["volumes"] = named_volumes

        # D7: per-cluster escape hatch, deep-merged verbatim.
        return self._deep_merge(compose, cluster_cfg.get("compose_extra") or {})

    def write(self, compose_dict: dict[str, Any], workdir: str) -> str:
        """Write *compose_dict* to ``<workdir>/docker-compose.yml`` (D5)."""
        workdir = os.path.expanduser(workdir)
        os.makedirs(workdir, exist_ok=True)
        path = os.path.join(workdir, "docker-compose.yml")
        with open(path, "w") as fobj:
            yaml.safe_dump(compose_dict, fobj, sort_keys=False, default_flow_style=False)
        return path

    # -- internals ---------------------------------------------------------

    def _service(
        self,
        cluster_name: str,
        box_name: str,
        box: dict[str, Any],
        conf_dir: str,
        cluster_networks: dict[str, Any],
        shared_networks: dict[str, Any],
        referenced_shared: set[str],
        named_volumes: dict[str, Any],
    ) -> dict[str, Any]:
        svc: dict[str, Any] = {}

        self._warn_corrupted_templating(cluster_name, box_name, box)

        unknown = [k for k in box if k not in _KNOWN_BOX_KEYS]
        if unknown:
            self.logger.warning(
                f"box '{cluster_name}.{box_name}': ignoring unknown key(s) "
                f"{', '.join(repr(k) for k in unknown)} — not part of the "
                f"Phase-3 docker-compose box schema; use 'compose_extra:' to "
                f"pass them through to the service."
            )

        for key in _PASSTHROUGH_KEYS:
            if key in box:
                svc[key] = box[key]

        if "build" in box:
            svc["build"] = self._build(box["build"], conf_dir)

        nets = self._service_networks(
            cluster_name, box_name, box.get("networks"),
            cluster_networks, shared_networks, referenced_shared,
        )
        if nets:
            svc["networks"] = nets

        vols = self._service_volumes(
            cluster_name, box_name, box.get("volumes"), conf_dir, named_volumes
        )
        if vols:
            svc["volumes"] = vols

        # D7: per-box escape hatch, deep-merged verbatim (last, so it can
        # override anything boxman generated).
        svc = self._deep_merge(svc, box.get("compose_extra") or {})

        if not (svc.get("image") or svc.get("build")):
            raise ConfigError(
                f"box '{cluster_name}.{box_name}' must define 'image:' or "
                f"'build:'."
            )
        return svc

    def _warn_corrupted_templating(
        self, cluster_name: str, box_name: str, box: dict[str, Any]
    ) -> None:
        """Warn when a compose ``environment:``/``command:`` value carries the
        ``{word}`` corruption signature.

        ``load_config`` does a whole-file preprocessing pass that rewrites a
        bare Jinja ``{{ word }}`` into ``{word}`` (a task placeholder) with no
        YAML-structure awareness, so it also mangles docker-compose values. The
        Phase-2 caveat deferred a structure-aware exemption to "Phase 3, where
        these values are consumed" (config-schema.md); this is that consumer,
        so at minimum we flag the corruption instead of shipping it silently.
        ``${VAR}`` / ``$${VAR}`` are unaffected and remain the safe forms.
        """
        values: list[str] = []
        cmd = box.get("command")
        if isinstance(cmd, str):
            values.append(cmd)
        elif isinstance(cmd, (list, tuple)):
            values.extend(str(x) for x in cmd)
        env = box.get("environment")
        if isinstance(env, (list, tuple)):
            values.extend(str(x) for x in env)
        elif isinstance(env, dict):
            values.extend(str(v) for v in env.values())

        for val in values:
            if _CORRUPTED_TEMPLATE_RE.search(val):
                self.logger.warning(
                    f"box '{cluster_name}.{box_name}': value {val!r} contains a "
                    f"'{{word}}' token — a bare Jinja '{{{{ word }}}}' in a "
                    f"compose environment:/command: is rewritten to '{{word}}' "
                    f"by config preprocessing. Use ${{VAR}} or $${{VAR}} for "
                    f"compose-time interpolation (see "
                    f"doc/docker-compose-provider/config-schema.md)."
                )
                break

    def _build(self, build: Any, conf_dir: str) -> Any:
        """Resolve a **local** ``build.context`` to an absolute path vs
        *conf_dir* (D4).

        Compose also accepts remote contexts — Git URLs (``https://…​.git#ref``,
        ``git@…``), scheme URLs, and host shorthands (``github.com/…``). Those
        are passed through verbatim: joining them onto *conf_dir* would mangle
        e.g. ``https://github.com/x/y.git#main`` into ``/proj/https:/github…``.
        """
        if isinstance(build, str):
            return build if _is_remote_build_context(build) \
                else self._abs_context(build, conf_dir)
        out = dict(build)
        ctx = str(out.get("context", "."))
        if not _is_remote_build_context(ctx):
            out["context"] = self._abs_context(ctx, conf_dir)
        return out

    @staticmethod
    def _abs_context(ctx: str, conf_dir: str) -> str:
        return os.path.abspath(os.path.join(conf_dir, os.path.expanduser(ctx)))

    def _service_volumes(
        self,
        cluster_name: str,
        box_name: str,
        volumes: Any,
        conf_dir: str,
        named_volumes: dict[str, Any],
    ) -> list[str]:
        """Translate a box's structured ``volumes:`` to compose mount strings.

        Each entry is a mapping. A ``host_path`` makes it a **bind** mount
        (relative paths resolved absolute vs *conf_dir*, D4); otherwise it is a
        **named** volume (needs ``name``) that is also recorded in
        *named_volumes* for the top-level ``volumes:`` block. ``readonly: true``
        appends ``:ro``. ``size:`` is advisory on a named volume (docker's
        local driver does not enforce quotas) and meaningless on a bind mount —
        both are warned, never enforced. Malformed input raises ``ConfigError``
        rather than silently dropping a mount.
        """
        if not volumes:
            return []
        if not isinstance(volumes, (list, tuple)):
            raise ConfigError(
                f"box '{cluster_name}.{box_name}': 'volumes:' must be a list of "
                f"mounts (got {type(volumes).__name__})."
            )
        mounts: list[str] = []
        for entry in volumes:
            if not isinstance(entry, dict):
                raise ConfigError(
                    f"box '{cluster_name}.{box_name}': each 'volumes:' entry must "
                    f"be a mapping with 'container_path' (+ 'name' for a named "
                    f"volume or 'host_path' for a bind mount) — got {entry!r}."
                )
            unknown = [k for k in entry if k not in _KNOWN_VOLUME_KEYS]
            if unknown:
                self.logger.warning(
                    f"box '{cluster_name}.{box_name}': ignoring unknown "
                    f"'volumes:' key(s) {', '.join(repr(k) for k in unknown)} — "
                    f"use 'compose_extra:' to pass extra mount options."
                )
            container_path = entry.get("container_path")
            if not container_path:
                raise ConfigError(
                    f"box '{cluster_name}.{box_name}': a 'volumes:' entry is "
                    f"missing 'container_path' ({entry!r})."
                )
            ro = ":ro" if entry.get("readonly") else ""
            host_path = entry.get("host_path")
            if host_path:
                if entry.get("size"):
                    self.logger.warning(
                        f"box '{cluster_name}.{box_name}': 'size:' is ignored on "
                        f"the bind mount for '{container_path}' — it is only "
                        f"meaningful on a named volume."
                    )
                abs_host = self._abs_context(str(host_path), conf_dir)
                mounts.append(f"{abs_host}:{container_path}{ro}")
            else:
                name = entry.get("name")
                if not name:
                    raise ConfigError(
                        f"box '{cluster_name}.{box_name}': a named 'volumes:' "
                        f"entry needs a 'name' (or a 'host_path' for a bind "
                        f"mount) — {entry!r}."
                    )
                if entry.get("size"):
                    self.logger.warning(
                        f"box '{cluster_name}.{box_name}': size: "
                        f"{entry['size']!r} on named volume '{name}' is advisory "
                        f"— docker's local driver does not enforce quotas."
                    )
                mounts.append(f"{name}:{container_path}{ro}")
                if name not in named_volumes:
                    named_volumes[name] = self._deep_merge(
                        {"driver": "local"}, entry.get("compose_extra") or {}
                    )
        return mounts

    def _service_networks(
        self,
        cluster_name: str,
        box_name: str,
        networks: Any,
        cluster_networks: dict[str, Any],
        shared_networks: dict[str, Any],
        referenced_shared: set[str],
    ) -> list[str] | dict[str, Any]:
        """Resolve a box's ``networks:`` to the service's compose ``networks``.

        *networks* is either a list of names (auto address on each) or a
        mapping ``name -> {ipv4_address: …}`` pinning a static address (only
        meaningful on a shared/macvlan bridge). Cluster-internal and shared
        refs are attached; a shared ref is recorded in *referenced_shared* so
        :meth:`_shared_macvlan_networks` emits the top-level macvlan network.
        Unknown refs are warned about and dropped.

        Returns the mapping form (``{name: {ipv4_address: …}}``) when any ref
        carries per-network options, else the plain list form.
        """
        entries = self._normalize_box_networks(cluster_name, box_name, networks)
        attached: dict[str, dict[str, Any]] = {}
        any_opts = False
        for ref, opts in entries:
            if ref in cluster_networks:
                if opts.get("ipv4_address"):
                    self.logger.warning(
                        f"box '{cluster_name}.{box_name}': ignoring "
                        f"'ipv4_address' on cluster-internal network '{ref}' — "
                        f"static addresses are only wired for shared_networks "
                        f"(macvlan) bridges; use 'compose_extra:' for a static "
                        f"IP on a cluster-internal network."
                    )
                    opts = {k: v for k, v in opts.items() if k != "ipv4_address"}
            elif ref in shared_networks:
                self._require_macvlan_ipam(cluster_name, box_name, ref,
                                           shared_networks[ref] or {})
                referenced_shared.add(ref)
            else:
                self.logger.warning(
                    f"box '{cluster_name}.{box_name}': network '{ref}' is "
                    f"neither a cluster-internal network nor a shared_networks "
                    f"bridge — skipping."
                )
                continue
            svc_opts = {"ipv4_address": opts["ipv4_address"]} \
                if opts.get("ipv4_address") else {}
            if svc_opts:
                any_opts = True
            attached[ref] = svc_opts

        if not attached:
            return []
        return attached if any_opts else list(attached)

    @staticmethod
    def _normalize_box_networks(
        cluster_name: str, box_name: str, networks: Any
    ) -> list[tuple[str, dict[str, Any]]]:
        """Normalise a box ``networks:`` (list or mapping) to ``[(ref, opts)]``.

        List form yields empty opts per ref; mapping form carries each ref's
        option dict (``{ipv4_address: …}``, or ``None`` → empty). Malformed
        input fails fast with a ``ConfigError`` rather than silently dropping
        the attachment — a bare string (``networks: app_bridge``, a forgotten
        ``[…]``) would otherwise emit a service with no ``networks:`` key at
        all, so compose attaches it to the project-default bridge and the
        macvlan L2 attach silently never happens.
        """
        if not networks:
            return []
        if isinstance(networks, str):
            # A forgotten list: treat the single name as a one-element list
            # rather than dropping it silently.
            return [(networks, {})]
        if isinstance(networks, dict):
            for ref, opts in networks.items():
                if opts is not None and not isinstance(opts, dict):
                    raise ConfigError(
                        f"box '{cluster_name}.{box_name}': networks['{ref}'] "
                        f"must be a mapping like {{ipv4_address: …}} or null "
                        f"(got {opts!r})."
                    )
            return [(ref, dict(opts or {})) for ref, opts in networks.items()]
        if isinstance(networks, (list, tuple)):
            return [(str(ref), {}) for ref in networks]
        raise ConfigError(
            f"box '{cluster_name}.{box_name}': 'networks:' must be a list or "
            f"mapping (got {type(networks).__name__})."
        )

    def _require_macvlan_ipam(
        self, cluster_name: str, box_name: str, ref: str, entry: dict[str, Any]
    ) -> None:
        """A shared bridge attached by a box needs a ``subnet`` (macvlan IPAM)
        and an underlying ``bridge`` — raise ``ConfigError`` otherwise."""
        if not entry.get("bridge"):
            raise ConfigError(
                f"box '{cluster_name}.{box_name}': shared network '{ref}' has "
                f"no 'bridge:' — add it under shared_networks['{ref}']."
            )
        if not entry.get("subnet"):
            raise ConfigError(
                f"box '{cluster_name}.{box_name}': shared network '{ref}' needs "
                f"a 'subnet:' for the docker macvlan IPAM pool — add it under "
                f"shared_networks['{ref}']."
            )

    def _networks(self, cluster_networks: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, net in cluster_networks.items():
            net = net or {}
            spec: dict[str, Any] = {"driver": net.get("driver", "bridge")}
            if net.get("subnet"):
                spec["ipam"] = {"config": [{"subnet": net["subnet"]}]}
            out[name] = self._deep_merge(spec, net.get("compose_extra") or {})
        return out

    def _shared_macvlan_networks(
        self, referenced: set[str], shared_networks: dict[str, Any]
    ) -> dict[str, Any]:
        """Emit a top-level docker **macvlan** network for each referenced
        shared bridge, so containers land on the same L2 as libvirt VMs
        cabled into that host bridge (Phase 4, design D8).

        Each becomes ``driver: macvlan`` + ``driver_opts.parent: <bridge>`` +
        an ``ipam`` config carrying the bridge's ``subnet`` (required) and
        optional ``gateway`` / ``ip_range``. Iterated in *shared_networks*
        declaration order (filtered by *referenced*) for deterministic output.
        Presence of ``bridge``/``subnet`` is already enforced upstream in
        :meth:`_require_macvlan_ipam`.
        """
        out: dict[str, Any] = {}
        for name, entry in shared_networks.items():
            if name not in referenced:
                continue
            entry = entry or {}
            ipam_cfg: dict[str, Any] = {"subnet": entry["subnet"]}
            if entry.get("gateway"):
                ipam_cfg["gateway"] = entry["gateway"]
            if entry.get("ip_range"):
                ipam_cfg["ip_range"] = entry["ip_range"]
            spec: dict[str, Any] = {
                "driver": "macvlan",
                "driver_opts": {"parent": entry["bridge"]},
                "ipam": {"config": [ipam_cfg]},
            }
            out[name] = self._deep_merge(spec, entry.get("compose_extra") or {})
        return out

    @staticmethod
    def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
        """Recursively merge *extra* onto *base* (extra wins); returns a copy."""
        result = dict(base)
        for key, value in (extra or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = ComposeGenerator._deep_merge(result[key], value)
            else:
                result[key] = value
        return result


#: git host shorthands compose accepts as a remote build context
_REMOTE_CONTEXT_HOSTS = ("github.com/", "bitbucket.org/", "gitlab.com/")


def _is_remote_build_context(ctx: str) -> bool:
    """True if *ctx* is a remote build context (Git/URL) rather than a local
    filesystem path.

    Compose treats these as remote and must **not** have them resolved against
    the conf.yml dir: scheme URLs (``https://``, ``git://``, ``ssh://``),
    scp-like Git (``git@host:path``), and the ``github.com/…`` /
    ``bitbucket.org/…`` / ``gitlab.com/…`` shorthands (with an optional
    ``#ref`` fragment).
    """
    ctx = ctx.strip()
    if "://" in ctx or ctx.startswith("git@"):
        return True
    head = ctx.split("#", 1)[0]
    return head.startswith(_REMOTE_CONTEXT_HOSTS)
