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

import ipaddress
import os
import re
from typing import Any

import yaml

from boxman import log
from boxman.exceptions import ConfigError

#: Compose's implicit per-project network. An explicit ``networks:
#: [default]`` needs no top-level declaration and attaches to it, so the
#: resolver accepts the literal name (#164 NET-C1).
IMPLICIT_DEFAULT_NETWORK = "default"

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


def resolve_local_path(path: str, base_dir: str) -> str:
    """Resolve *path* absolute against *base_dir* (``~`` expanded); an already
    absolute *path* ignores *base_dir*.

    The single source of truth for build-context, bind-mount and bind-dir
    resolution — the docs promise bind resolution follows "the same rule as
    ``build.context``", so both the generator (emitting the mount) and the
    session (``mkdir``-ing the host dir) call this, and can never drift apart.
    """
    return os.path.abspath(os.path.join(base_dir, os.path.expanduser(path)))


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
        #: per-box refs that came from the box's own ``networks:`` key and
        #: matched no cluster-internal or shared network — the only refs
        #: boxman is entitled to judge (#164 NET-C1).
        unmatched_native: dict[str, set[str]] = {}
        #: per-box refs naming BOTH a cluster-internal network and a shared
        #: bridge. Resolution picks the cluster-internal one silently, so the
        #: box lands on an isolated bridge instead of the shared L2 it asked
        #: for (#164 NET-C3).
        ambiguous_native: dict[str, set[str]] = {}

        services: dict[str, Any] = {}
        for box_name, box in (cluster_cfg.get("boxes") or {}).items():
            box_unmatched: set[str] = set()
            box_ambiguous: set[str] = set()
            services[box_name] = self._service(
                cluster_name, box_name, box or {}, conf_dir,
                cluster_networks, shared_networks, referenced_shared,
                named_volumes, box_unmatched, box_ambiguous,
            )
            if box_unmatched:
                unmatched_native[box_name] = box_unmatched
            if box_ambiguous:
                ambiguous_native[box_name] = box_ambiguous

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
        compose = self._deep_merge(
            compose, cluster_cfg.get("compose_extra") or {})
        # order matters: a mode in effect removes the attachments, so there
        # is then no reference left to judge.
        self._apply_network_mode(compose)
        self._assert_networks_resolve(cluster_name, compose, unmatched_native)
        self._assert_no_ambiguous_alias(
            cluster_name, compose, ambiguous_native, cluster_networks,
            shared_networks)
        self._assert_macvlan_ipam_sane(cluster_name, compose, referenced_shared)
        return compose

    def write(self, compose_dict: dict[str, Any], workdir: str,
              filename: str = "docker-compose.yml") -> str:
        """Write *compose_dict* to ``<workdir>/<filename>`` (D5).

        *filename* exists so a caller can stage a **candidate** next to the
        working file, validate it, and only then move it into place -- an
        invalid file written over a working one strands the running stack,
        because teardown reuses the on-disk file and Compose refuses to read
        a project it cannot resolve (#164 NET-C1).
        """
        workdir = os.path.expanduser(workdir)
        os.makedirs(workdir, exist_ok=True)
        path = os.path.join(workdir, filename)
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
        unmatched_native: set[str],
        ambiguous_native: set[str],
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
            unmatched_native, ambiguous_native,
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
        return resolve_local_path(ctx, conf_dir)

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
            container_path = str(container_path)
            # Fail fast on paths that would only error cryptically at
            # ``docker compose up``, or a ``:`` that would silently re-split the
            # short-syntax mount string boxman emits.
            if not os.path.isabs(container_path):
                raise ConfigError(
                    f"box '{cluster_name}.{box_name}': volume container_path "
                    f"{container_path!r} must be an absolute path."
                )
            if ":" in container_path:
                raise ConfigError(
                    f"box '{cluster_name}.{box_name}': volume container_path "
                    f"{container_path!r} must not contain ':'."
                )
            ro = ":ro" if entry.get("readonly") else ""
            host_path = entry.get("host_path")
            if host_path:
                # host_path decides the kind; a named-volume-only key here is a
                # no-op — warn rather than drop it silently (as `size:` does).
                for noop_key in ("name", "size", "compose_extra"):
                    if entry.get(noop_key):
                        self.logger.warning(
                            f"box '{cluster_name}.{box_name}': '{noop_key}:' is "
                            f"ignored on the bind mount for '{container_path}' — "
                            f"it applies only to named volumes ('host_path' makes "
                            f"this a bind mount)."
                        )
                if ":" in str(host_path):
                    raise ConfigError(
                        f"box '{cluster_name}.{box_name}': volume host_path "
                        f"{host_path!r} must not contain ':'."
                    )
                abs_host = resolve_local_path(str(host_path), conf_dir)
                mounts.append(f"{abs_host}:{container_path}{ro}")
            else:
                name = entry.get("name")
                if not name:
                    raise ConfigError(
                        f"box '{cluster_name}.{box_name}': a named 'volumes:' "
                        f"entry needs a 'name' (or a 'host_path' for a bind "
                        f"mount) — {entry!r}."
                    )
                if ":" in str(name):
                    raise ConfigError(
                        f"box '{cluster_name}.{box_name}': volume name {name!r} "
                        f"must not contain ':'."
                    )
                if entry.get("size"):
                    self.logger.warning(
                        f"box '{cluster_name}.{box_name}': size: "
                        f"{entry['size']!r} on named volume '{name}' is advisory "
                        f"— docker's local driver does not enforce quotas."
                    )
                mounts.append(f"{name}:{container_path}{ro}")
                spec = self._deep_merge(
                    {"driver": "local"}, entry.get("compose_extra") or {}
                )
                if name not in named_volumes:
                    named_volumes[name] = spec
                elif named_volumes[name] != spec:
                    self.logger.warning(
                        f"box '{cluster_name}.{box_name}': named volume '{name}' "
                        f"is already defined by an earlier box (first-seen wins) "
                        f"— this box's differing volume options are ignored."
                    )
        return mounts

    @staticmethod
    def _effective_network_mode(svc: dict[str, Any]) -> str | None:
        """The ``network_mode`` actually in effect, or ``None``.

        A *present* key is not a mode in effect. ``network_mode: ""`` (or an
        expression interpolating to empty) leaves the service on its declared
        networks, and an interpolated value cannot be resolved here at all --
        only Compose knows what it becomes. Treating "key present" as "mode
        set" silently erased declared attachments (#164 NET-C1).
        """
        mode = svc.get("network_mode")
        if not isinstance(mode, str) or not mode.strip():
            return None
        if "$" in mode:
            # Only Compose can resolve this, and it may interpolate to empty.
            # boxman no longer emits a redundant `networks: [default]`, so
            # leaving both keys alone cannot manufacture a conflict that a
            # plain box config would not already have.
            return None
        return mode

    def _apply_network_mode(self, compose: dict[str, Any]) -> None:
        """Drop ``networks:`` from any service with a mode actually in effect.

        Compose refuses a service declaring both, so an override asking for
        ``network_mode:`` takes the attachments with it. This runs over the
        **assembled** dict, after the cluster-level ``compose_extra:`` has
        merged: a mode set (or cancelled) at cluster level, or inherited
        through ``extends:``, is only visible at that point (#164 NET-C1).
        """
        for svc in (compose.get("services") or {}).values():
            if not isinstance(svc, dict):
                continue
            if self._effective_network_mode(svc) is not None:
                svc.pop("networks", None)

    def _assert_networks_resolve(
        self,
        cluster_name: str,
        compose: dict[str, Any],
        unmatched_native: dict[str, set[str]],
    ) -> None:
        """Refuse a reference boxman can *prove* Compose will reject.

        Compose is the authority here, and a good one: it refuses any service
        reference to an undeclared network -- including one that only appears
        after ``include:`` is merged or ``${VAR}`` is interpolated -- with
        ``service "x" refers to undefined network y``. boxman's own check
        exists to say the same thing earlier and better (naming the cluster,
        the box and the available names), never to be the only thing that
        catches it.

        So this refuses only where refusal is certain, and stays quiet
        everywhere Compose knows something boxman does not:

        - only refs boxman itself emitted from a box's ``networks:`` key are
          judged; anything arriving through ``compose_extra:`` is the user
          deliberately reaching past boxman;
        - checked against the **final** top-level ``networks:``, so an
          override that declares the network satisfies the reference;
        - ``default`` is Compose's implicit network and needs no declaration;
        - a ref containing ``$`` is an interpolation boxman cannot resolve;
        - a top-level ``include:`` may define networks in another file;
        - a service with ``extends:`` is not fully visible here.

        Everything deferred still fails -- in Compose, by its own message --
        so deferring costs a less specific diagnostic, never a silent
        misattachment (#164 NET-C1).
        """
        if not unmatched_native:
            return
        if compose.get("include"):
            return
        declared = set(compose.get("networks") or {})
        services = compose.get("services") or {}
        for box_name, refs in unmatched_native.items():
            svc = services.get(box_name) or {}
            if svc.get("extends"):
                continue
            attached = svc.get("networks")
            if not attached:
                # an override removed the attachment, or a mode took it
                continue
            attached_names = set(
                attached if isinstance(attached, list) else list(attached))
            for ref in sorted(refs):
                if ref not in attached_names:
                    continue
                if ref in declared or ref == IMPLICIT_DEFAULT_NETWORK:
                    continue
                if "$" in ref:
                    continue
                known = sorted(declared | {IMPLICIT_DEFAULT_NETWORK})
                raise ConfigError(
                    f"box '{cluster_name}.{box_name}': network '{ref}' is "
                    f"not defined by this cluster "
                    f"(available: {', '.join(known) or 'none'}). "
                    f"Compose would refuse this file with "
                    f"'service \"{box_name}\" refers to undefined network "
                    f"{ref}'. Correct the name, declare the network under "
                    f"'networks:', or attach it through 'compose_extra:' if "
                    f"it is one boxman does not manage."
                )

    def _assert_no_ambiguous_alias(
        self,
        cluster_name: str,
        compose: dict[str, Any],
        ambiguous_native: dict[str, set[str]],
        cluster_networks: dict[str, Any],
        shared_networks: dict[str, Any],
    ) -> None:
        """Refuse a name that means both a cluster network and a shared bridge.

        :meth:`_service_networks` resolves cluster-internal names first, so a
        box asking for a shared L2 by a name the cluster also declares lands
        on an isolated bridge instead -- silently, and the two are different
        broadcast domains. The precedence is unguessable and either choice is
        wrong for somebody, so the operator has to say which they meant.

        Judged on the **surviving** attachment, after ``compose_extra:`` and
        network-mode handling, for the same reason NET-C1 is: the colliding
        alias may be unused, or an override may remove the attachment, and
        neither of those deploys onto the wrong L2.

        Unlike NET-C1 this has **no** ``include:``/``extends:`` deferral.
        Those deferrals are safe there because Compose is a complete oracle
        for an undeclared network -- it refuses one itself. It is not an
        oracle here: Compose knows nothing about boxman's
        ``shared_networks:``, so an unrelated ``include:`` would simply let
        the ambiguous attachment resolve to the internal bridge (#164
        NET-C3).
        """
        if not ambiguous_native:
            return
        services = compose.get("services") or {}
        for box_name, refs in ambiguous_native.items():
            svc = services.get(box_name) or {}
            attached = svc.get("networks")
            if not attached:
                continue
            attached_names = set(
                attached if isinstance(attached, list) else list(attached))
            for ref in sorted(refs):
                if ref not in attached_names:
                    continue
                bridge = (shared_networks.get(ref) or {}).get("bridge", "?")
                raise ConfigError(
                    f"box '{cluster_name}.{box_name}': network '{ref}' names "
                    f"both a cluster-internal network (under this cluster's "
                    f"'networks:') and a shared bridge (under the project's "
                    f"'shared_networks:', bridge '{bridge}'). Those are "
                    f"different L2 domains, and boxman resolves the "
                    f"cluster-internal one -- so this box would be isolated "
                    f"rather than on the shared bridge with the VMs. Rename "
                    f"one of the two declarations so the attachment says "
                    f"which you meant."
                )

    @staticmethod
    def _unresolved(*values: Any) -> bool:
        """Whether any operand still contains a Compose expression.

        A comparison is deferred when **either** side is unresolved, not just
        the one being parsed: ``subnet: ${SUBNET}`` with a literal gateway
        and range is valid, and skipping only the subnet must not turn its
        dependent comparisons into errors (#164 FBN-16).
        """
        return any(isinstance(v, str) and "$" in v for v in values)

    @staticmethod
    def _effectively_attached(compose: dict[str, Any]) -> set[str]:
        """Networks some service in the assembled file actually attaches to.

        ``referenced_shared`` records what was referenced *before* overrides
        merged, so validating against it rejects configurations that deploy:
        a `network_mode:` override, or one clearing the attachment, leaves the
        network unused and Compose drops it from the resolved model entirely.

        A service gated behind ``profiles:`` is not counted. Whether its
        profile is active is not decidable here, and refusing on a network
        only such a service uses would reject a working deployment -- the
        same fail-only-when-certain rule the rest of this module follows
        (#164 NET-C2/FBN-16).
        """
        used: set[str] = set()
        for svc in (compose.get("services") or {}).values():
            if not isinstance(svc, dict) or svc.get("profiles"):
                continue
            attached = svc.get("networks")
            if not attached:
                continue
            used.update(
                attached if isinstance(attached, list) else list(attached))
        return used

    def _assert_macvlan_ipam_sane(
        self, cluster_name: str, compose: dict[str, Any],
        referenced: set[str],
    ) -> None:
        """Check the shared-bridge IPAM boxman emitted actually holds together.

        ``_require_macvlan_ipam`` only checks that ``bridge:`` and ``subnet:``
        are *present*. Presence is not membership: a gateway outside the
        subnet, or an ``ip_range`` that is not inside it, is reported by
        Docker obscurely and late (#164 FBN-16).

        Run on the **assembled** file and only for networks still in
        effective use, because ``compose_extra:`` may replace the IPAM block
        wholesale or remove the attachment altogether.
        """
        in_use = self._effectively_attached(compose)
        for name in sorted(referenced & in_use):
            spec = (compose.get("networks") or {}).get(name) or {}
            configs = ((spec.get("ipam") or {}).get("config") or [])
            for cfg in configs:
                if not isinstance(cfg, dict):
                    continue
                self._check_ipam_entry(name, cfg)

    def _check_ipam_entry(self, name: str, cfg: dict[str, Any]) -> None:
        """One ``ipam.config`` element of a shared macvlan network."""
        subnet = cfg.get("subnet")
        gateway = cfg.get("gateway")
        ip_range = cfg.get("ip_range")

        # The missing-range warning does not depend on parsing anything, and
        # must not be skipped along with a deferred membership comparison --
        # it is the whole NET-C2 safeguard (#164 NET-C2).
        if subnet and not ip_range:
            self.logger.warning(
                f"shared network '{name}' declares no 'ip_range', so docker "
                f"allocates from the whole of {subnet}, starting at the "
                f"first free address. This bridge is a shared L2 -- docker's "
                f"IPAM sees only its own pools, never the wire, so it will "
                f"hand a container an address a VM or a DHCP server on that "
                f"bridge is already using. Reserve docker a range nothing "
                f"else allocates from: 'ip_range:' under "
                f"shared_networks['{name}']."
            )
        if not subnet:
            # An override asking for automatic IPAM: docker selects a
            # predefined pool. Nothing declared here to check.
            return
        if self._unresolved(subnet):
            return
        try:
            net = ipaddress.ip_network(str(subnet), strict=False)
        except ValueError as exc:
            raise ConfigError(
                f"shared network '{name}': 'subnet: {subnet}' is not a valid "
                f"network ({exc})."
            ) from exc

        if gateway and not self._unresolved(gateway):
            try:
                addr = ipaddress.ip_address(str(gateway))
            except ValueError as exc:
                raise ConfigError(
                    f"shared network '{name}': 'gateway: {gateway}' is not a "
                    f"valid address ({exc})."
                ) from exc
            if addr.version != net.version:
                raise ConfigError(
                    f"shared network '{name}': 'gateway: {gateway}' is IPv"
                    f"{addr.version} but 'subnet: {subnet}' is IPv"
                    f"{net.version}."
                )
            if addr not in net:
                raise ConfigError(
                    f"shared network '{name}': 'gateway: {gateway}' is "
                    f"outside 'subnet: {subnet}'."
                )

        if ip_range and not self._unresolved(ip_range):
            try:
                rng = ipaddress.ip_network(str(ip_range), strict=False)
            except ValueError as exc:
                raise ConfigError(
                    f"shared network '{name}': 'ip_range: {ip_range}' is not "
                    f"a valid network ({exc})."
                ) from exc
            if rng.version != net.version:
                raise ConfigError(
                    f"shared network '{name}': 'ip_range: {ip_range}' is IPv"
                    f"{rng.version} but 'subnet: {subnet}' is IPv"
                    f"{net.version}."
                )
            if not rng.subnet_of(net):
                raise ConfigError(
                    f"shared network '{name}': 'ip_range: {ip_range}' is not "
                    f"inside 'subnet: {subnet}'."
                )

    def _service_networks(
        self,
        cluster_name: str,
        box_name: str,
        networks: Any,
        cluster_networks: dict[str, Any],
        shared_networks: dict[str, Any],
        referenced_shared: set[str],
        unmatched_native: set[str],
        ambiguous_native: set[str],
    ) -> list[str] | dict[str, Any]:
        """Resolve a box's ``networks:`` to the service's compose ``networks``.

        *networks* is either a list of names (auto address on each) or a
        mapping ``name -> {ipv4_address: …}`` pinning a static address (only
        meaningful on a shared/macvlan bridge). Cluster-internal and shared
        refs are attached; a shared ref is recorded in *referenced_shared* so
        :meth:`_shared_macvlan_networks` emits the top-level macvlan network.
        A ref matching neither is **still attached**, and recorded in
        *unmatched_native* for :meth:`_assert_networks_resolve` to judge once
        every override has merged. It used to be warned about and dropped,
        which left the service with no explicit attachment at all -- so
        Compose placed it on the project's default network rather than
        refusing it (#164 NET-C1). The literal ``default`` resolves to that
        same implicit network; names boxman does not manage can be attached
        through ``compose_extra:``, which bypasses this resolver.

        Returns the mapping form (``{name: {ipv4_address: …}}``) when any ref
        carries per-network options, else the plain list form.
        """
        entries = self._normalize_box_networks(cluster_name, box_name, networks)
        attached: dict[str, dict[str, Any]] = {}
        any_opts = False
        for ref, opts in entries:
            if ref in cluster_networks and ref in shared_networks:
                # One alias, two different L2s. Resolution below would pick
                # the cluster-internal one silently (#164 NET-C3).
                ambiguous_native.add(ref)
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
                # Not a name boxman manages -- which is not yet an error. It
                # may be `default` (Compose provides it), or something an
                # override supplies. Attach it as declared and remember it:
                # the final pass decides, once every override has merged
                # (#164 NET-C1). Crucially the ref is never dropped -- that
                # was the original defect, because a dropped ref left the
                # service with no attachment and Compose silently placed it
                # on the project default instead of refusing it.
                unmatched_native.add(ref)
                if (ref == IMPLICIT_DEFAULT_NETWORK
                        and opts.get("ipv4_address")):
                    self.logger.warning(
                        f"box '{cluster_name}.{box_name}': ignoring "
                        f"'ipv4_address' on the implicit 'default' network — "
                        f"boxman declares no IPAM pool for it. Declare a "
                        f"'default' network under the cluster's 'networks:', "
                        f"or use 'compose_extra:'."
                    )
                    opts = {k: v for k, v in opts.items()
                            if k != "ipv4_address"}
            svc_opts = {"ipv4_address": opts["ipv4_address"]} \
                if opts.get("ipv4_address") else {}
            if svc_opts:
                any_opts = True
            attached[ref] = svc_opts

        if not attached:
            return []
        if (not any_opts
                and list(attached) == [IMPLICIT_DEFAULT_NETWORK]
                and IMPLICIT_DEFAULT_NETWORK in unmatched_native):
            # Emitting the *implicit* `networks: [default]` is redundant --
            # a service with no `networks:` key lands on the project default
            # anyway -- and it is the sole reason a `network_mode:` override
            # becomes Compose's "mutually exclusive" error. That includes
            # modes boxman cannot see: one inherited through `extends:` or
            # written `${VAR:-none}` is invisible here, so the safe move is
            # not to emit the redundant attachment at all.
            #
            # A `default` that boxman *resolved* -- declared by the cluster
            # or by `shared_networks:` -- is a different thing: it is a real
            # network, and under `extends:` an omitted `networks:` means
            # "inherit the parent's attachments", so dropping the explicit
            # entry would silently change where the service lands. Keying
            # off `unmatched_native` is exactly "matched nothing", which is
            # what makes it the implicit one (#164 NET-C1).
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
