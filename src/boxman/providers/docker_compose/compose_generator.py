"""
ComposeGenerator — translate a boxman docker-compose *cluster* into a
``docker-compose.yml`` dict and write it to the cluster workdir.

Phase 3 scope: services (image / build / command / environment / ports /
depends_on / restart / healthcheck), cluster-internal bridge networks, and
the ``compose_extra:`` escape hatch (deep-merged verbatim, per-cluster and
per-box — design decision D7). Out-of-phase box features are warned about
and skipped:

- ``volumes:`` (structured named/bind/workdir mounts) → **Phase 5**.
- a box network that resolves to a ``shared_networks`` bridge (macvlan
  L2 to VMs) → **Phase 4**; only cluster-internal networks are emitted.

The generated file is a fidelity artifact — inspectable and hand-runnable
with ``docker compose -f <cluster_workdir>/docker-compose.yml ps`` (D5).
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable

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

#: every box key the Phase-3 generator understands. Anything else is warned
#: about and dropped (``volumes:`` → Phase 5 is handled separately, but is a
#: recognised key so it doesn't also trip the unknown-key warning).
_KNOWN_BOX_KEYS = frozenset(_PASSTHROUGH_KEYS) | {
    "build",
    "networks",
    "volumes",
    "compose_extra",
}


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
        shared_network_names: Iterable[str] = (),
    ) -> dict[str, Any]:
        """
        Build the compose dict for *cluster_name*.

        Args:
            cluster_name: The cluster name (for diagnostics).
            cluster_cfg: The cluster config (``boxes:``, ``networks:``,
                optional ``compose_extra:``).
            conf_dir: Directory of the project ``conf.yml`` — ``build.context``
                is resolved absolute against it (D4).
            shared_network_names: Names declared under the top-level
                ``shared_networks:`` — used only to phrase the Phase-4
                skip warning precisely.

        Returns:
            The compose dict (no top-level ``version:`` — the compose spec
            treats it as obsolete).
        """
        shared = frozenset(shared_network_names)
        cluster_networks = cluster_cfg.get("networks") or {}

        services: dict[str, Any] = {}
        for box_name, box in (cluster_cfg.get("boxes") or {}).items():
            services[box_name] = self._service(
                cluster_name, box_name, box or {}, conf_dir, cluster_networks, shared
            )

        compose: dict[str, Any] = {"services": services}
        networks = self._networks(cluster_networks)
        if networks:
            compose["networks"] = networks

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
        shared: frozenset[str],
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
            cluster_name, box_name, box.get("networks") or [], cluster_networks, shared
        )
        if nets:
            svc["networks"] = nets

        if box.get("volumes"):
            self.logger.warning(
                f"box '{cluster_name}.{box_name}': 'volumes:' is not supported "
                f"yet (lands in Phase 5) — skipping {len(box['volumes'])} "
                f"volume(s). Use 'compose_extra:' if you need them now."
            )

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

    def _service_networks(
        self,
        cluster_name: str,
        box_name: str,
        refs: list[str],
        cluster_networks: dict[str, Any],
        shared: frozenset[str],
    ) -> list[str]:
        out: list[str] = []
        for ref in refs:
            if ref in cluster_networks:
                out.append(ref)
            elif ref in shared:
                self.logger.warning(
                    f"box '{cluster_name}.{box_name}': network '{ref}' is a "
                    f"shared_networks bridge — L2 attach to VMs lands in "
                    f"Phase 4; skipping it for now."
                )
            else:
                self.logger.warning(
                    f"box '{cluster_name}.{box_name}': network '{ref}' is not a "
                    f"cluster-internal network — skipping (shared/macvlan "
                    f"networks land in Phase 4)."
                )
        return out

    def _networks(self, cluster_networks: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, net in cluster_networks.items():
            net = net or {}
            spec: dict[str, Any] = {"driver": net.get("driver", "bridge")}
            if net.get("subnet"):
                spec["ipam"] = {"config": [{"subnet": net["subnet"]}]}
            out[name] = self._deep_merge(spec, net.get("compose_extra") or {})
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
