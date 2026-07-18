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

## Notes

- The generated `docker-compose.yml` lives under `.boxman/` (git-ignored). It
  is a fidelity artifact — you can run `docker compose -f ... <cmd>` against it
  directly.
- Need a compose feature boxman does not model yet? Add it under
  `compose_extra:` (per-box or per-cluster) and it is deep-merged verbatim.
