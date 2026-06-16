# Boxman API — docker-compose stack

Runs the boxman HTTP API (`boxman-api`), a Celery worker (`boxman-api-worker`),
and `redis`, layered on the existing boxman **libvirt** container so jobs can
provision real VMs through the docker-compose runtime.

## Bring it up

```bash
# 1) start the libvirt container (needs Docker + /dev/kvm)
cd containers/docker
docker compose up --build -d

# 2) start the API stack (sets an admin password so you can log in)
cd api
BOXMAN_API_ADMIN_PASSWORD=secret BOXMAN_API_JWT_SECRET=$(openssl rand -hex 32) \
  docker compose up --build -d
```

The API is on `http://127.0.0.1:8080` (OpenAPI docs at `/docs`). If you don't set
`BOXMAN_API_ADMIN_PASSWORD`, a random one is generated and printed in the
`boxman-api` logs on first start (`docker logs boxman-api`).

## Use it

```bash
BASE=http://127.0.0.1:8080
TOKEN=$(curl -s -d 'username=admin&password=secret' $BASE/auth/token | jq -r .access_token)
auth=(-H "Authorization: Bearer $TOKEN")

# register a project (conf path is on the API host / mounted HOME)
curl -s "${auth[@]}" -H 'content-type: application/json' \
  -d '{"name":"demo","conf":"'"$HOME"'/myproj/conf.yml","runtime":"docker-compose"}' \
  $BASE/projects

# provision (returns a job id), then poll it
JOB=$(curl -s "${auth[@]}" -H 'content-type: application/json' \
  -d '{"force":true}' $BASE/projects/demo/provision | jq -r .id)
curl -s "${auth[@]}" $BASE/jobs/$JOB
curl -s "${auth[@]}" $BASE/jobs/$JOB/log
```

## How it fits together

- **api + worker** share the boxman source (bind-mounted, importable via
  `PYTHONPATH`), a `api-data` volume (sqlite DB + job logs), your
  `~/.config/boxman` (boxman.yml + the project cache), and the docker socket.
- A long/mutating request creates a **Job**, enqueues a Celery task on redis;
  the worker runs `boxman <subcommand>` as a subprocess, which `docker exec`s
  into the libvirt container — so boxman's internal multiprocessing runs in a
  normal process, not the daemonic worker.
- Reads (`/status`, `/snapshots`, `/capabilities`, …) run the CLI synchronously.

## Host-libvirt variant

If libvirt runs on the host (not in a container), drop the `docker.sock` mount,
register projects with `"runtime":"local"`, and run the API/worker on the host:

```bash
pip install 'boxman[api]'
BOXMAN_API_ADMIN_PASSWORD=secret boxman-api &
boxman-worker &
redis-server &
```

## Security notes

- The API binds to `127.0.0.1` and requires JWT auth. Set a strong
  `BOXMAN_API_JWT_SECRET` and admin password; rotate as needed.
- Mounting `docker.sock` grants control of the host docker daemon to the
  containers — keep the stack on a trusted host and the API on localhost.
