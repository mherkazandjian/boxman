# Migrating a project from config v1.0 to v2.0

**Short version: you do not have to.** v1.0 configs keep working exactly as
they did — same behaviour, byte for byte. Migrate only when you want something
v2.0 adds, most commonly a `docker-compose` cluster in the project.

---

## What v2.0 actually changes

Three things, all additive:

| | v1.0 | v2.0 |
|---|---|---|
| Version marker | none | `version: '2.0'` at the top level |
| Cluster members | `vms:` | `boxes:` (provider-neutral) — `vms:` still accepted |
| Provider | one, project-wide | `provider:` per cluster |

Everything else — `templates:`, `networks:`, `shared_networks:`, `workspace:`,
`tasks:`, cloud-init, snapshots, the CLI — is unchanged.

## Do I need to migrate?

| You want to… | Migrate? |
|---|---|
| Keep running your libvirt project | **No.** Leave it on v1.0. |
| Add a container cluster to it | **Yes** — per-cluster `provider:` is v2.0. |
| Use `boxes:` for readability | Optional. |

There is no deprecation deadline for v1.0 in this release.

## The migration, in three steps

### 1. Declare the version

```yaml
version: '2.0'      # add this line
project: myproject
```

`version: 2` and `version: '2.0'` both select v2.0; quoting is recommended so
YAML does not read it as a float. Anything else is refused outright
(`unsupported config version: '3.0' (supported: '1.0', '2.0')`).

Without the version line, a cluster that resolves to the docker-compose
provider is rejected with exactly that instruction:

```
cluster 's' uses the docker-compose provider, which requires version: '2.0'
```

That guard exists because v1.0 *ignores* `boxes:` — so without it your
containers would be silently skipped rather than provisioned. Note that a
per-cluster `provider: libvirt` is still accepted under v1.0; it is the
docker-compose provider specifically that requires v2.0.

### 2. Rename `vms:` → `boxes:` (optional)

```yaml
clusters:
  compute:
    vms:                    # v1.0
      node01: { memory: 2048 }
```

```yaml
clusters:
  compute:
    boxes:                  # v2.0 — same meaning
      node01: { memory: 2048 }
```

`boxes:` is the provider-neutral name; `vms:` remains accepted (with a
deprecation warning) so a mixed-vintage config keeps working. Declaring **both**
on one cluster is ambiguous and rejected:

```
cluster 'c' declares both 'boxes:' and 'vms:' — use one (prefer 'boxes:' in v2.0).
```

Under the hood a libvirt cluster's `boxes:` is renamed back to `vms:` during
normalization, so the existing libvirt code paths are untouched — the rename is
cosmetic for VM clusters and load-bearing only for container ones.

### 3. Set the provider where you want containers

Existing clusters need nothing — `libvirt` stays the default. Add
`provider: docker-compose` only on the new cluster:

```yaml
version: '2.0'
project: myproject

provider:
  libvirt: { uri: 'qemu:///system' }    # unchanged
  docker-compose:
    project_name: myproject             # new

clusters:
  compute:                              # untouched, still libvirt
    base_image: ubuntu-24.04-minimal-base-template-cloudinit
    boxes:
      node01: { memory: 2048 }

  services:                             # new container cluster
    provider: docker-compose
    workdir: ./.boxman/services
    boxes:
      cache:
        image: redis:7-alpine
```

That is the whole migration.

## Verifying

```bash
boxman conf            # render + validate without touching infrastructure
boxman ps              # both clusters listed, with a Provider column
```

Your existing VMs are not redefined by adding a container cluster — libvirt
resource names are unchanged, so `provision` will not recreate them.

## Things worth knowing before you add containers

- **The docker-compose provider needs the `local` runtime** (the default). The
  `docker` *runtime* is a different axis that happens to share the name — see
  the [user guide](user-guide.md#first-the-name-collision).
- **`boxman ssh` stays VM-only.** Containers use `boxman exec <cluster>.<box>`.
- **`--vms` is libvirt-only** and skips docker-compose clusters; `--cluster`
  scopes either kind.
- **Container snapshots do not capture named volumes** — see
  [Snapshots](user-guide.md#snapshots).
- Containers are ephemeral by design: anything that must survive `up` belongs
  in a volume.

## Rolling back

Delete the `provider:` line from the cluster (or the cluster itself) and drop
`version: '2.0'`. Nothing else in a v1.0 file has to change, since v2.0 only
adds keys. Run `boxman destroy` for the container cluster first so no orphaned
compose project is left behind.
