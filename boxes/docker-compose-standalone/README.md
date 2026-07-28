# docker-compose-standalone

The simplest **docker-compose provider** example — no libvirt, no VMs. Two
containers on one cluster-internal bridge network:

```
frontend (nginx, :8080)  --depends_on: service_healthy-->  cache (redis)
```

On `boxman provision` boxman translates the `boxes:` into a
`docker-compose.yml` (written to the cluster `workdir`, `./.boxman/web/`) and
runs `docker compose up -d --wait`. It shows the core docker-compose surface:
`image`, `ports`, a `healthcheck`, a `depends_on` with a
`condition: service_healthy` (passed through verbatim), and a cluster-internal
bridge network with an explicit `subnet:`.

## Prerequisites

- Docker Engine + the **compose v2** plugin (`docker compose version`).
- boxman on the **`local` runtime** (the default). The docker-compose provider
  shells out to `docker compose` on the host; it is a config error under the
  `docker` runtime.
- Free TCP port **8080** on the host.

## Bring it up

```bash
cd boxes/docker-compose-standalone
boxman provision
```

## Verify

```bash
# nginx answers on the published port
curl -sI localhost:8080 | head -1              # HTTP/1.1 200 OK

# inspect the stack — boxman runs it under project 'dc_standalone_web'
docker compose -p dc_standalone_web ps
cat .boxman/web/docker-compose.yml     # emits a top-level 'name:', so `-f <file> ps` also works

# redis is healthy behind the frontend
docker exec dc_standalone_web-cache-1 redis-cli ping   # PONG
```

`boxman up` is idempotent — re-running reconciles (starts anything stopped and
re-asserts health).

## Tear down

```bash
boxman down       # docker compose stop (keeps containers/volumes)
boxman up         # bring them back
boxman destroy    # docker compose down --volumes + remove the generated file
```

## Volumes

Two kinds, one of each:

| Box | Kind | Mount | Survives |
|-----|------|-------|----------|
| `cache` | named (`cache_data`) | `/data` | `down`/`up` and `deprovision` — removed by `destroy` |
| `frontend` | bind (`./site`, read-only) | `/usr/share/nginx/html` | it is your directory; boxman never deletes it |

The bind mount is live — edit and reload, no rebuild:

```bash
curl -s localhost:8080 | grep -o '<h1>.*</h1>'   # <h1>boxman</h1>
sed -i 's|<h1>boxman</h1>|<h1>edited</h1>|' site/index.html
curl -s localhost:8080 | grep -o '<h1>.*</h1>'   # <h1>edited</h1>
```

It is mounted `:ro`, so the container cannot write to it:

```bash
boxman exec web.frontend -- sh -c 'echo x > /usr/share/nginx/html/x' 2>&1
# sh: can't create /usr/share/nginx/html/x: Read-only file system
```

The named volume persists across a stop/start cycle:

```bash
boxman exec web.cache -- redis-cli set greeting hello
boxman exec web.cache -- redis-cli save          # flush to /data (the volume)
boxman down && boxman up
boxman exec web.cache -- redis-cli get greeting  # "hello" — survived
```

`boxman destroy` removes named volumes (`docker compose down --volumes`); the
bind directory `./site` is left alone.

## Exec into a container

`boxman ssh` is for VMs. Containers get **`boxman exec`** (decision D2) — no
sshd sidecar, no keys baked into images:

```bash
boxman exec web.cache                     # interactive shell (default: sh)
boxman exec web.frontend --shell bash     # pick the shell
boxman exec web.cache -- redis-cli ping   # one-shot: PONG
```

Put a command after `--` when it has its own flags, so they reach the container
instead of boxman.

## Ansible / `boxman run`

Containers land in the generated inventory as ordinary hosts, reached with the
`community.docker` connection plugin — no SSH, no keys in the image:

```bash
cat .boxman/web/inventory/01-hosts.yml
```

```yaml
web_cache:
  ansible_connection: "community.docker.docker"
  ansible_host: "dc_standalone_web-cache-1"     # the real container name
web_frontend:
  ansible_connection: "community.docker.docker"
  ansible_host: "dc_standalone_web-frontend-1"
```

Prerequisites on the control host:

```bash
ansible-galaxy collection install community.docker
pip install docker            # the Docker SDK for Python
```

> **Ansible modules need a Python interpreter *inside* the container.** The
> images here (`nginx:alpine`, `redis:alpine`) deliberately ship without one,
> so module-based tasks such as `-m ping` or `ansible.builtin.shell` fail with
> *"No python interpreters found"*. That is a property of minimal images, not
> of boxman. Two ways forward:
>
> ```bash
> # 1. the raw module needs no interpreter — good for minimal images
> ansible all -m raw -a 'nginx -v'
>
> # 2. or use an image that has python, and the full module set works
> ```
>
> `boxman run --cmd '<cmd>'` wraps `ansible.builtin.shell`, so it needs option
> 2. For a quick command against a minimal image, `boxman exec` is the direct
> route.

## Snapshots

Backed by `docker commit` (decision D3) — with one caveat worth reading twice.

```bash
boxman exec web.cache -- redis-cli set marker before-snapshot
boxman snapshot take --name v1 -m "known good"
boxman snapshot list

# change something, then roll back
boxman exec web.cache -- sh -c 'echo scratch > /tmp/scratch'
boxman snapshot restore --name v1
boxman exec web.cache -- ls /tmp/scratch     # gone — filesystem rolled back

boxman snapshot delete --name v1
```

> **Named volumes are not part of a snapshot.** `docker commit` captures a
> container's writable layer only, never the data in a mounted volume. So the
> redis key above lives in `/data` (the `cache_data` volume) and is **not**
> rolled back by a restore — only the container filesystem is. This is the key
> divergence from libvirt snapshots, which capture the disk. Back volumes up
> separately. boxman warns about this on every `take`.

Snapshot names must be unused — `boxman snapshot delete --name v1` first, or
pick a new name. A restore is a point-in-time recreate, not a permanent pin: a
later `boxman up` regenerates from `conf.yml` and returns to the declared
images.

## Notes

- The generated `docker-compose.yml` lives under `.boxman/` (git-ignored). It
  is a fidelity artifact — you can run `docker compose -f ... <cmd>` against it
  directly.
- Need a compose feature boxman does not model yet? Add it under
  `compose_extra:` (per-box or per-cluster) and it is deep-merged verbatim.
- In a **mixed** project, `--cluster <name>` scopes a command to one cluster of
  either provider. `--vms` names libvirt VMs only, so passing it skips
  docker-compose clusters entirely.
