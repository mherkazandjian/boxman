# docker-compose provider — user guide

How to run containers from a boxman project, on their own or alongside VMs.

For the *why* behind the design see [design.md](design.md) and
[ADR-001](adr-001-per-cluster-provider.md); for every key and its exact
semantics see [config-schema.md](config-schema.md). This page is the practical
path.

---

## First, the name collision

`docker-compose` means two unrelated things in boxman:

| | What it is | Where it is set |
|---|---|---|
| **runtime** | *where* commands run — libvirt inside a container | `--runtime`, `runtime:` in `boxman.yml` |
| **provider** | *what* a cluster is made of — containers instead of VMs | `provider:` on a cluster |

They are independent axes. This guide is entirely about the **provider**.

The provider requires the **`local` runtime** (the default): it shells out to
`docker compose` on the host. Mixing the two is a config error with an
explanatory message, not a mysterious failure.

## Prerequisites

- Docker Engine with the **compose v2** plugin — check with
  `docker compose version`.
- Your user in the `docker` group, or set `use_sudo: true` under the provider
  config.
- For ansible against containers: `ansible-galaxy collection install
  community.docker` and `pip install docker` on the control host.

No KVM, no cloud images: a container-only project runs anywhere Docker does.

## A minimal project

```yaml
version: '2.0'                 # required — per-cluster providers are v2.0
project: shop

provider:
  docker-compose:
    project_name: shop         # compose project prefix (default: project name)

clusters:
  web:
    provider: docker-compose
    workdir: ./.boxman/web     # where the generated docker-compose.yml goes

    networks:
      appnet:
        driver: bridge
        subnet: 172.28.0.0/24

    boxes:
      cache:
        image: redis:7-alpine
        networks: [appnet]
```

```bash
boxman provision      # generate docker-compose.yml + docker compose up -d --wait
boxman ps             # containers listed alongside any VMs
boxman destroy        # down --volumes + remove the generated file
```

Containers are declared under **`boxes:`**, the provider-neutral key. Each box
becomes a compose *service*. boxman writes a real `docker-compose.yml` into the
cluster `workdir` — inspect it, or run `docker compose -f … <cmd>` against it
directly. It is a generated artifact: edit `conf.yml`, not the output.

### What boxman brings up

`provision` (and the idempotent `up`) run `docker compose up -d --wait`, so the
command returns only once every service is `running` — or `healthy`, when the
box declares a `healthcheck:`. Raise the ceiling per cluster with
`readiness_timeout: <seconds>` (default 120).

## Lifecycle

| Verb | Docker equivalent | Named volumes |
|---|---|---|
| `provision` / `up` | `up -d --wait` | created if missing |
| `down` | `stop` | kept |
| `deprovision` | `down` | **kept** |
| `destroy` | `down --volumes` | **removed** |
| `control suspend` / `resume` | `pause` / `unpause` | — |
| `control start` | `start` | — |
| `control save` | *(no equivalent)* | explains and skips |

`up` is idempotent — re-running reconciles rather than recreating.

## Volumes

```yaml
boxes:
  cache:
    volumes:
      - name: cache_data              # named: docker-managed
        container_path: /data
  frontend:
    volumes:
      - host_path: ./site             # bind: your directory
        container_path: /usr/share/nginx/html
        readonly: true                # -> :ro
```

- **Named** volumes survive `down`/`up` *and* `deprovision`; only `destroy`
  removes them.
- **Bind** mounts point at a host directory. A relative `host_path` resolves
  against the directory holding `conf.yml`, and boxman pre-creates it **as
  you** — left to docker, the daemon would create it root-owned.
- `workdir: <path>` on a box is shorthand for a bind of `.` at that path.
- `size:` on a named volume is **advisory** — docker's `local` driver cannot
  enforce a quota, so boxman emits the volume and warns rather than pretending
  to cap it.

## Networking

Two kinds, and the difference matters:

```yaml
clusters:
  web:
    networks:
      appnet:                     # cluster-internal bridge — isolated
        driver: bridge
        subnet: 172.28.0.0/24
```

```yaml
shared_networks:                  # project-level: an L2 domain VMs can join
  app_bridge:
    bridge: bx_app
    subnet: 10.10.0.0/24
```

A box that joins a `shared_networks` entry is attached with a **macvlan**
network whose parent is that host bridge, so it is directly L2-adjacent to any
VM on the same bridge — they can ping and ARP for each other. Cluster-internal
networks stay isolated from the VMs.

boxman installs a **scoped per-bridge netfilter accept rule** rather than
disabling netfilter host-wide (decision D8). `disable_netfilter: true` forces
the global switch; it is discouraged and logged loudly.

See [`boxes/hybrid-libvirt-docker-compose`](../../boxes/hybrid-libvirt-docker-compose)
for the full worked example.


### Every network a box names must exist

A `networks:` entry that matches neither a cluster-internal network nor a
`shared_networks` bridge is a **config error**: the cluster refuses to
deploy and nothing is written.

> **Behaviour change.** This used to warn and drop the reference. The
> service was then left with no explicit attachment at all — and Compose
> places such a service on the project's *default* network. So a typo
> quietly changed which L2 a container joined:
>
> ```yaml
> boxes:
>   db:  {image: postgres, networks: [app_bridge]}
>   web: {image: nginx,    networks: [app_brige]}   # typo
> ```
>
> `db` was isolated on `app_bridge` as asked; `web` was on the default
> network with whatever else had no explicit attachment, and the single
> warning scrolled past.

**If an upgrade starts refusing a config that used to deploy**, boxman is
naming a reference it cannot resolve. One of these applies:

| you meant | do this |
|---|---|
| a typo | correct the name |
| a network you never declared | add it under the cluster's `networks:` or project `shared_networks:` |
| a leftover you no longer need | delete the reference |
| a network boxman does not manage | declare **and** attach it through `compose_extra:` (below) |

#### What boxman decides, and what Compose decides

boxman refuses only a reference it can *prove* is unresolvable — one it
emitted itself from a box's `networks:`, checked against the finished file
after every `compose_extra:` has merged. Everything else is left to
Compose, which resolves `include:`, `extends:` and `${VAR}` first and then
refuses any reference to an undeclared network on its own.

So these are **not** refused by boxman, and are not silently dropped either
— they are emitted as written and Compose rules on them:

| reference | why boxman defers |
|---|---|
| `${NET:-corp}` | only Compose interpolates it |
| a network defined in an `include:`d file | boxman cannot see the other file |
| a service using `extends:` | the effective service is not visible here |
| anything added by `compose_extra:` | you are deliberately reaching past boxman |

Two references resolve without any declaration:

- **`default`** — Compose's implicit per-project network. When the cluster
  does not declare one, `networks: [default]` is emitted as *no* `networks:`
  key: the same thing to Compose, and it cannot collide with a
  `network_mode:` override. A `default` the cluster **declares** under
  `networks:` is a real network and is always emitted — under `extends:` an
  omitted key means "inherit the parent's attachments", so the explicit
  entry is not redundant there.
- **anything `compose_extra:` supplies** — an override that declares a
  network satisfies a reference to it, and one that removes a reference
  takes it out of the question.

#### `network_mode:` and `networks:`

Compose refuses a service that declares both, so a `compose_extra:`
override setting `network_mode:` drops the attachments boxman generated.
Only a mode actually *in effect* does this: `network_mode: ""` (or an
expression interpolating to empty) leaves the service on its declared
networks rather than silently detaching it.



### One alias, one L2

A name may not appear in both a cluster's `networks:` and the project's
`shared_networks:`. They are different broadcast domains, boxman resolves
the cluster-internal one, and a box asking for the shared bridge would be
quietly isolated instead. Rename one of the two declarations.

The refusal is on the *attachment*, not the declaration: a colliding name
nothing attaches to, or one an override removes, deploys fine.

### Sharing one bridge between clusters

Docker's IPAM knows only its own pools — it never looks at the wire. Two
consequences, and they need different handling:

**Between Docker networks**, Docker polices itself. Two clusters referencing
one `shared_networks` alias each emit their own macvlan network over the same
bridge, requesting the *same* pool, and the Engine refuses the second:

```
failed to create network <proj>_<alias>:
  invalid pool request: Pool overlaps with other one on this address space
```

`ip_range` alone does not fix that, because one global `ip_range` is emitted
unchanged into every referencing cluster. Supported arrangements:

| you want | do this |
|---|---|
| two clusters on one bridge | declare **separate aliases** over the same `bridge:`, each with a disjoint `ip_range` |
| ditto, one config | give each cluster a `compose_extra:` that overrides `networks.<alias>.ipam.config` with its own range |
| full control | manage the Docker network externally and attach it through `compose_extra:` |

**Between Docker and everything else on that bridge**, nothing polices
anything. A container can be handed an address a VM already holds, and `up`
will succeed. Declare an `ip_range` that nothing else allocates from —
boxman warns when a shared network a cluster uses has none, because Docker
then owns the whole subnet and starts at the first free address:

```yaml
shared_networks:
  app_bridge:
    bridge: bx_app
    subnet: 10.10.0.0/24
    ip_range: 10.10.0.128/25   # docker gets .128-.255; VMs keep the rest
```

Reserving that range against your VM addresses **and any DHCP pool on the
bridge** is the operator's job: boxman creates no DHCP service for these
bridges, but it reuses existing ones and excludes nothing served by libvirt,
a guest, or another machine. A declared `gateway:` must be inside `subnet:`,
and `ip_range:` must be inside it too — both are checked.

## How the compose file is published

Every generated file is validated with `docker compose config` **before the
working `docker-compose.yml` is touched at all**: boxman writes a candidate
under a unique temporary name, validates that, and publishes it with a
single atomic rename. A file that does not resolve fails the run with
Compose's own message and leaves the previous file exactly as it was.

That matters because teardown reuses the on-disk file: a file Compose cannot
read makes `down` fail while the containers keep running. Nothing is rolled
back, because nothing is moved until a validated candidate exists — a
rejected candidate, an I/O error, a crash, or another run failing
concurrently all leave the working file untouched. If `docker compose`
cannot be run at all, boxman publishes **nothing**.

One case this cannot catch: a file whose `compose_extra.include:` reaches
the `docker-compose.yml` boxman generates for that cluster. While staged the
name still refers to the *previous* file, so Compose accepts it, and it
becomes an include cycle once published. `up` then fails with Compose's own
"include cycle detected" — boxman does not try to predict it, because doing
so means re-implementing Compose's resolution of interpolated paths,
`extends.file`, `project_directory` and YAML tags.

> boxman sets `COMPOSE_PROJECT_NAME` to the **empty string** for every
> `docker compose` call, and passes the real name with `-p`. Compose falls
> back to that variable *silently* when it cannot load a file, so an
> unreadable file plus an inherited value could point a teardown at a
> different project entirely. Clearing it is not enough — Compose then reads
> `COMPOSE_*` from the project directory's `.env` (or `COMPOSE_ENV_FILES`)
> and the value comes back; the shell environment outranks both, so it has
> to be *set*. Empty rather than the project name, so the fallback has
> nothing to fall back to and a file Compose cannot load fails the command
> instead of quietly proceeding without it. A label-only call additionally
> passes `--env-file /dev/null`, since it has no `-f` to outrank a `.env`
> that names one.

## If the compose file cannot be read

boxman does not try to predict whether the file is usable — every such check
asked a different question than teardown does. It simply **attempts the
operation with the file first**, because the file is authoritative when it
works. A file that fails `docker compose config` may still tear down
perfectly: delete a service's `env_file:` after deploying and `config` exits
1 while `down --volumes` succeeds and still honours `external: true`. That
case is not degraded.

| operation | behaviour when the attempt fails |
|---|---|
| `ps` | retried by project label, with a warning. Read-only, so nothing the file defines is lost. |
| `stop`, `start`, `pause`, `exec` | **not retried.** A failed `stop`/`start` may be a failed `pre_stop`/`post_start` hook, and stepping over it would report success while your hook never ran. |
| `deprovision` (`down`), `destroy` (`down --volumes`) | **not retried.** The file is kept so a retry can use it. If there was no file to run them with, the containers are stopped; if the removal itself failed, they are left exactly as Compose left them — the failure may be a `pre_stop` hook, and stopping anyway would perform the step Compose withheld. |

Resource removal is never retried by label because Compose reconstructs a
project from container labels, and that reconstruction does not carry
`external: true`. A label-only `down --volumes` would delete resources the
config says to keep. **A missing compose file is refused for the same
reason** — losing the file does not establish what the project owns.

Restore or repair the file and re-run. If you accept that every resource
carrying the project's labels will be removed, external ones included, run
that explicitly yourself:

```bash
docker compose -p <project> down --volumes
```

## Getting into a container

`boxman ssh` stays **VM-only** — SSH into a container would mean an sshd
sidecar and keys baked into the image. Containers get `boxman exec`:

```bash
boxman exec web.cache                     # interactive shell (default sh)
boxman exec web.frontend --shell bash     # choose the shell
boxman exec web.cache -- redis-cli ping   # one-shot command
```

Put the command after `--` when it carries its own flags, so they reach the
container rather than boxman. A bare box name works when it is unique across
your docker-compose clusters.

## Ansible and `boxman run`

Containers are rendered into the generated inventory as ordinary hosts, using
the `community.docker` connection plugin:

```yaml
web_cache:
  ansible_connection: "community.docker.docker"
  ansible_host: "shop_web-cache-1"      # the real container name
```

so `boxman run` and `tasks:` reach containers and VMs alike.

> **Ansible modules need a Python interpreter inside the container.** Minimal
> images (`alpine`-based ones especially) do not ship one, and module-based
> tasks fail with *"No python interpreters found"*. Use `ansible … -m raw`,
> which needs no interpreter, or pick an image that has Python. `boxman run
> --cmd` wraps `ansible.builtin.shell`, so it needs the latter; for a quick
> command against a minimal image, `boxman exec` is the direct route.

## Snapshots

Backed by `docker commit` (decision D3):

```bash
boxman snapshot take --name v1 -m "known good"
boxman snapshot list
boxman snapshot restore --name v1
boxman snapshot delete --name v1
```

Each container is committed to `boxman/<project>_<cluster>_<box>:<name>` — the
repository carries the compose project, so same-named boxes in different
clusters never collide — and recorded in `snapshots.json` in the cluster
workdir.

> ### Named volumes are not part of a snapshot
>
> `docker commit` captures a container's writable layer only, never the data in
> a mounted volume. A restore rolls the **container filesystem** back and
> leaves **volume data exactly as it is** — databases, uploads and anything
> else you deliberately persisted are untouched. This is the key divergence
> from libvirt's external snapshots, which capture the disk. Back volumes up
> separately. boxman warns on every `take`.

Practical rules:

- A snapshot name must be unused — `delete` it first, or pick another. (libvirt
  rejects duplicate snapshot names too.) Names that differ only by punctuation
  are rejected as well, since they would sanitize to the same docker tag.
- A restore is a **point-in-time recreate, not a permanent pin**: a later
  `boxman up` regenerates from `conf.yml` and returns to the declared images.
- Restore pre-validates that every recorded image still exists, so a snapshot
  whose images were pruned outside boxman fails cleanly instead of part-way
  through the recreate.
- Deleting a snapshot you are *currently restored onto* untags the image but
  leaves it dangling until those containers are replaced (`docker image prune`
  reclaims it).

## Mixed projects: scoping

In a project with both kinds of cluster:

- **`--cluster <name>`** scopes to one cluster, of either provider.
- **`--vms <names>`** names libvirt VMs, so it **skips docker-compose clusters
  entirely**. `boxman snapshot restore --vms node01 --name X` will not
  force-recreate your containers.

## Escape hatch

Anything boxman does not model yet goes through `compose_extra:`, per box or
per cluster, deep-merged into the generated file verbatim:

```yaml
boxes:
  cache:
    image: redis:7-alpine
    compose_extra:
      deploy:
        resources:
          limits: { cpus: '0.50' }
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| `requires runtime 'local'` | The provider was used under the `docker` runtime — the two axes were mixed up. |
| `'docker compose' … not available` | Compose v2 plugin missing, or docker needs `use_sudo: true`. |
| `snapshot 'x' already exists` | Names are single-use; `snapshot delete --name x` first. |
| `No python interpreters found` | Ansible module against a minimal image — use `-m raw` or `boxman exec`. |
| `no containers to snapshot` | The cluster is not up. |
| Container state lost after `up` | Expected: the container filesystem is rebuilt from the declared image. Persist data in a **volume**. |
