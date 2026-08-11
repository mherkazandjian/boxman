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
