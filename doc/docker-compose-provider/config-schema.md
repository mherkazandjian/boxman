# Boxman configuration schema — versioning (v1.0 & v2.0)

Boxman project configs (`conf.yml`) carry an optional top-level `version:`
key. It selects how boxman interprets the rest of the file. **v1.0 is
supported indefinitely and is unaffected by this document** — everything
below is additive.

This is the reference for the schema-version handling introduced in
Phase 2 of the docker-compose provider epic
([#50](https://github.com/mherkazandjian/boxman/issues/50)). It lives
alongside [`design.md`](./design.md) (see *Configuration Schema v2.0* and
*Breaking Changes & Versioning*).

## Version detection

`BoxmanManager.load_config` renders the Jinja template, parses the YAML,
then calls `_apply_config_version(conf)` before returning — so **every
downstream consumer sees the internal (v1.0-shaped) config**, regardless
of the on-disk schema version.

| `version:` value | Behaviour |
|---|---|
| absent | treated as **v1.0** — returned unchanged |
| `'1.0'` (or unquoted `1` / `1.0`) | **v1.0** — returned unchanged |
| `'2.0'` (or unquoted `2` / `2.0`) | **v2.0** — normalized (see below) |
| anything else | `ConfigError: unsupported config version: '<x>' (supported: '1.0', '2.0')` → CLI logs it and exits 2 |

The value is compared as a string, so unquoted YAML numerics are accepted
(`version: 2` and `version: 2.0` both select v2.0). **Quoting is still
recommended** — write `version: '2.0'` — to avoid surprises from YAML's
numeric coercion (e.g. a future `version: 2.10` would parse as the float
`2.1`).

## What v2.0 adds

1. **`boxes:` as the generic per-cluster key.** v1.0 uses `vms:`; v2.0
   uses `boxes:` (a VM *or* a container is a "box"). For **libvirt**
   clusters boxman renames `boxes:` → `vms:` at load time, so the entire
   existing libvirt code path is untouched.
2. **Per-cluster `provider:`.** Each cluster may declare its own
   `provider:` (`libvirt` or `docker-compose`). It defaults to the
   **top-level primary provider** (the first key under the top-level
   `provider:` mapping). This key is already honoured by the Phase 1
   provider registry (`provider_type_for_cluster`).

A v2.0 config that uses only libvirt behaves **exactly** like its v1.0
equivalent.

## Normalization rules (compatibility matrix)

For each cluster, the effective provider is
`cluster.get('provider') or <top-level primary provider>`. Then:

| cluster provider | `boxes:` | `vms:` | result |
|---|:---:|:---:|---|
| libvirt | ✓ | – | `boxes:` renamed to `vms:` |
| libvirt | – | ✓ | accepted with a **deprecation warning** (prefer `boxes:`) |
| libvirt | ✓ | ✓ | **`ConfigError`** — ambiguous, declare one |
| docker-compose | ✓ | – | `boxes:` kept as-is (consumed by the Phase 3 `DockerComposeSession`) |
| docker-compose | – | ✓ | **`ConfigError`** — docker-compose clusters must use `boxes:` |
| `virtualbox` | any | any | **`ConfigError`** — not supported under v2.0 yet; use `version: '1.0'` + `vms:` |
| unknown / typo | any | any | **`ConfigError`** — unknown provider (guards against silent empty clusters) |

Only `libvirt` and `docker-compose` are wired for v2.0. The legacy
`virtualbox` provider stays on v1.0 syntax for now, and an unknown value
(e.g. a typo `libvrit`) is rejected rather than left to silently provision
an empty cluster.

A **per-box `provider:`** key is also rejected (`ConfigError`) — providers
are a cluster-level concern (ADR-001), so `boxes: { web: { provider: … } }`
is a config error, not a silently-ignored field.

A v1.0 config that uses `boxes:` gets a one-line warning (nothing reads
`boxes:` in v1.0, so those boxes are silently ignored — including in a
partial migration that still has a sibling `vms:`) — provisioning behaviour
is unchanged.

### Running docker-compose clusters (Phase 3, #51)

A v2.0 config with a docker-compose cluster is **runnable**: `boxman
provision` / `up` generate a `docker-compose.yml` in the cluster `workdir`
and run `docker compose up -d --wait`, and `down` / `deprovision` / `destroy`
tear it down. Scope covers cluster-internal bridge networks,
`shared_networks` **macvlan L2 to libvirt VMs** (Phase 4, #52 — see
[Shared networks (macvlan L2 to VMs)](#shared-networks-macvlan-l2-to-vms)
below), and structured **`volumes:`** (Phase 5, #53 — see
[Volumes](#volumes) below). Still out of scope: ssh/exec/inventory (Phase 6),
snapshots (Phase 7). Box features outside this scope are warned about and
skipped (use `compose_extra:` as the escape hatch).

Two hard requirements, both enforced fail-fast (exit 2):

- **`version: '2.0'` is required.** A docker-compose cluster consumes
  `boxes:`, which v1.0 ignores (see [`_warn_on_v1_boxes`](#) above) — so a
  v1.0 / versionless config whose primary or per-cluster provider resolves to
  `docker-compose` is **rejected** (rather than silently provisioning services
  the v1.0 `boxes:` nudge — see above — says are ignored).
- **`runtime: local` is required** (see next section).

### Provider vs runtime — two independent axes

`provider:` (per-cluster, this document) selects *what* provisions a cluster
(`libvirt` vs `docker-compose`). `--runtime` / the app-config `runtime:` key
selects *where boxman's commands execute*: `local` (directly on the host) or
`docker` (a.k.a. `docker-compose` — **libvirt inside a container**, used to
run the libvirt provider without host libvirt). These are unrelated.

The **docker-compose provider requires `runtime: local`**: it shells out to
`docker compose` on the host directly. Pairing it with the `docker` runtime
(libvirt-in-a-container) is a configuration error and boxman exits 2 at
session build. Note this fail-fast fires for **every** subcommand, so a
project that adds a docker-compose cluster to an existing libvirt-in-docker
runtime can no longer run *anything* (including `destroy`) until the runtime
is switched to `local` — an intentional, if blunt, consequence.

### Why `boxman conf` still shows `boxes:`

Normalization (`boxes:` → `vms:`) happens on the in-memory config **after**
the `.rendered.yml` debug dump is written. `boxman conf` and `.rendered.yml`
therefore intentionally show the **pre-normalization** shape — a fidelity
view of what you wrote (`boxes:`), not boxman's internal `vms:` form. This
is by design; the internal normalization is what the provisioning flows act
on.

## Shared networks (macvlan L2 to VMs)

A top-level `shared_networks:` block declares host Linux bridges that both a
docker-compose container **and** a libvirt VM can attach to, putting them on
the same L2 domain. boxman creates each bridge on the host
(`shared_bridges.ensure`, idempotent, never torn down — bridges can be shared
across projects); the docker-compose generator attaches a referencing
container via a docker **macvlan** network whose `parent` is that bridge.

```yaml
version: '2.0'
project: hybrid_lab
shared_networks:
  app_bridge:
    bridge: br-app            # required: the host Linux bridge name
    subnet: 10.10.0.0/24      # required when a box attaches (macvlan IPAM pool)
    gateway: 10.10.0.1        # optional
    ip_range: 10.10.0.128/25  # optional — restrict docker's auto-assign pool
    stp: false                # optional (default false)
    # disable_netfilter: false  # default; see the Netfilter note below
    # compose_extra: {...}      # optional, deep-merged onto the macvlan net
clusters:
  services:
    provider: docker-compose
    workdir: ./.boxman/services
    boxes:
      web:
        image: nginx
        networks: [app_bridge]          # list form → auto-assigned address
      api:
        image: myapi
        networks:
          app_bridge:
            ipv4_address: 10.10.0.20     # mapping form → static address
```

A box's `networks:` accepts two forms:

| Form | Example | Effect |
|---|---|---|
| **list** | `networks: [app_bridge, backend]` | attach to each; docker auto-assigns an address |
| **mapping** | `networks: {app_bridge: {ipv4_address: 10.10.0.20}}` | pin a static address on that network |

Notes and requirements:

- A shared network a box attaches to **must** declare `bridge:` and `subnet:`
  — otherwise `provision` fails with a `ConfigError` (docker's macvlan IPAM
  needs an address pool). A `shared_networks` entry that no box references is
  fine and simply isn't emitted into the compose file.
- `ipv4_address` is **only** wired for `shared_networks` (macvlan) attachments.
  On a cluster-internal network it is warned about and dropped — use
  `compose_extra:` if you need a static IP on a cluster-internal network.
- **Netfilter (decision D8):** by default (`disable_netfilter: false`) boxman
  leaves the host-global `bridge-nf-call-iptables` untouched and installs an
  idempotent per-bridge scoped `iptables` accept rule (on `FORWARD`, and
  `DOCKER-USER` when that chain exists) so bridged lab frames aren't dropped by
  a docker `FORWARD`/`DOCKER-USER` DROP policy. Like the shared bridges
  themselves, these rules are **never removed by boxman** (they can be shared
  across projects) — tearing them down is an explicit user action (see the
  hybrid box's teardown note). Setting `disable_netfilter: true` instead
  disables netfilter on bridges **host-wide** (a discouraged opt-in, logged
  loudly, reverted by any reboot).

## Volumes

A box's `volumes:` is a list of **structured** mounts (not raw compose
strings — use `compose_extra:` for those). Each entry needs a `container_path`;
whether it has a `host_path` decides its kind:

```yaml
clusters:
  data:
    provider: docker-compose
    workdir: ./.boxman/data
    boxes:
      db:
        image: postgres:16
        volumes:
          - name: pg_data                 # named volume (docker-managed)
            container_path: /var/lib/postgresql/data
            size: 10G                      # advisory only — see below
          - host_path: ./initdb           # bind mount (relative → resolved vs conf.yml dir)
            container_path: /docker-entrypoint-initdb.d
            readonly: true
          - host_path: .                   # "workdir" mount — a bind at the project dir
            container_path: /workspace
```

| Kind | Trigger | Emitted | Top-level `volumes:` |
|---|---|---|---|
| **named** | has `name`, no `host_path` | `<name>:<container_path>[:ro]` | `{<name>: {driver: local}}` |
| **bind** | has `host_path` | `<abs host_path>:<container_path>[:ro]` | — |
| **workdir** | a bind with `host_path: .` | `<conf dir>:<container_path>[:ro]` | — |

- `container_path` must be an **absolute** path; `readonly: true` appends `:ro`.
- A relative `host_path` is resolved absolute against the `conf.yml` directory
  (same rule as `build.context`). boxman `mkdir -p`s a bind host dir before
  `up` (so docker doesn't create it as `root`); an existing path is left as-is.
  A **missing bind path is always created as a directory** — a single-file bind
  mount must already exist on the host (boxman, like docker, can't tell a file
  from a dir from the schema).
- **`size:` is advisory** — docker's `local` volume driver does not enforce
  quotas, so boxman emits the volume and logs a warning rather than pretending
  to cap it. On a bind mount `size:` is meaningless and warned/ignored.
- `name:` and `compose_extra:` apply to **named volumes only** — on a bind mount
  (`host_path:` present) they are warned and ignored. `compose_extra:` on a
  named entry is deep-merged onto its top-level `{driver: local}` spec (e.g.
  custom `driver_opts`); when two boxes share a `name:`, the first-seen spec
  wins and a later box's differing options are warned + ignored.
- Malformed entries fail fast with a `ConfigError` (non-mapping entry, missing
  or non-absolute `container_path`, a named entry missing `name`, or a `:` in a
  path/name that would re-split the emitted mount) rather than silently
  dropping a mount.

**Lifecycle:** `boxman down` keeps volumes (`docker compose down`); `boxman
destroy` removes **named** volumes (`down --volumes`) and the generated compose
file. Bind-mount host directories are **never** removed — they are your paths
(`./initdb`, `.`), so cleaning them up is your call.

## CLI/UX for docker-compose clusters

Every boxman verb treats a mixed (libvirt + docker-compose) project uniformly;
containers appear alongside VMs, and no verb tracebacks on an unsupported op.

- **`boxman ps`** lists VMs and containers with a `Provider` column and each
  container's `State` (+ health). `boxman connect_info` adds a per-container
  section with published ports and the `boxman exec` entry point.
- **`boxman exec <cluster>.<box>`** runs `docker compose exec` on a container —
  an interactive shell (default `sh`; `--shell bash`), or a one-shot command
  after `--` (`boxman exec services.cache -- redis-cli ping`). A bare
  `<box>` works when it's unique across dc clusters. **`boxman ssh` stays
  VM-only** — exec is the container equivalent (decision D2). There is no
  ssh_config entry for containers.
- **Ansible / `boxman run` / `tasks:`** reach containers through the generated
  inventory: each container is a host with
  `ansible_connection: community.docker.docker` and
  `ansible_host: <project>_<cluster>-<box>-1`, grouped under its cluster. So
  `boxman run --cmd '…'` and `tasks:` hit containers and VMs alike.
  **Prerequisite:** the `community.docker` Ansible collection
  (`ansible-galaxy collection install community.docker`) and the Docker SDK
  for Python on the control host.
- **`boxman control`** for containers: `suspend` → `docker compose pause`,
  `resume` → `docker compose unpause`, `start` → `docker compose start`.
  `save` has no docker equivalent (containers have no save-to-file state) — it
  logs an explanatory message and skips the dc clusters (use snapshots in a
  later phase, or `destroy`).

## Templating caveat for compose values (`environment:`, `command:`)

`load_config` pre-processes bare `{{ name }}` tokens into `{name}` task
placeholders before Jinja render (see the comment block in
`load_config`). This is a **whole-file** text substitution with no
awareness of YAML structure, so it also touches docker-compose cluster
blocks.

Safe / unsafe forms inside a compose `environment:` or `command:` value:

| Form | Matched by preprocessing? | Verdict |
|---|---|---|
| `${VAR}`, `$${VAR}` | no (`$`/braces differ) | **safe** — use these for compose-time interpolation |
| `{{ env("VAR") }}`, `{{ a.b }}`, `{{ x \| y }}` | no (contains non-word chars) | **safe** — rendered by Jinja as usual |
| `{{ word }}` (a single bare word) | **yes** → rewritten to `{word}` | **avoid** — becomes the literal string `{word}` |

**Rule of thumb:** in docker-compose clusters, use `${VAR}` /
`$${VAR}` for interpolation and never a bare single-word `{{ word }}`.

**Phase 3 status:** a full structure-aware exemption is still out of scope,
but the generator now **warns** when a compose `environment:` / `command:`
value carries the `{word}` corruption signature (a mangled bare
`{{ word }}`), so the silent-corruption trap surfaces at generate time
instead of reaching the container. `${VAR}` / `$${VAR}` are unaffected and
produce no warning.

## Examples

v1.0 (unchanged, supported indefinitely):

```yaml
project: my_lab
provider:
  libvirt:
    uri: qemu:///system
clusters:
  compute:
    vms:
      node01:
        hostname: node01
```

v2.0, libvirt-only (behaves identically to the v1.0 above):

```yaml
version: '2.0'
project: my_lab
provider:
  libvirt:
    uri: qemu:///system
clusters:
  compute:
    boxes:
      node01:
        hostname: node01
```

v2.0, mixed providers: see
[`example-conf.yml`](./example-conf.yml).
