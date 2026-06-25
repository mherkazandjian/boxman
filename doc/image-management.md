# Image Management

Boxman has three related but distinct image features. They are introduced
here in the order most users meet them, with cross-references to the
runtime and provider machinery they sit on top of.

## At a glance

| Feature | Purpose | Entry point |
|---|---|---|
| **Image cache** | Download a base cloud image once; reuse across projects | Automatic — used by templates |
| **Template creation** | Build a cloud-init-customised template VM from a base cloud image | `boxman create-templates` |
| **Image import** | Define a libvirt VM from a pre-built `(disk + XML)` package described by a JSON manifest | `boxman import-image` |
| **Image push (OCI)** | Publish a qcow2 + optional metadata to an OCI registry via `oras` | `boxman image push` |
| **Image inspect (OCI)** | Inspect an OCI reference's manifest + `vmimage.json` metadata | `boxman image inspect` |
| **OCI base image** | Use an OCI image as a template / VM base, pulled on `boxman up` | `image.uri: oci://…` or `base_image: oci://…` |

The first two cover the common "spin up VMs from a public cloud image"
flow. The third is for moving an already-built VM (an exported lab VM,
a vendor appliance, etc.) onto a libvirt host. The OCI features distribute
and consume VM images through the same registries that hold container images.

---

## Image cache

The cache lives on disk and dedupes downloads of cloud base images:

```yaml
# ~/.config/boxman/boxman.yml
cache:
  enabled: true                        # default
  cache_dir: ~/.cache/boxman/images    # default
```

Images are keyed by the basename of their URL. A cache hit skips the
download; a cache miss downloads to `cache_dir` and reuses for every
subsequent project that references the same URL.

When a checksum is given the cache verifies it on every read and aborts
on mismatch — a corrupted file is re-downloaded on the next run rather
than silently used. See the README "Image Caching" section for the
checksum-spec format and the full hit/miss matrix.

The same cache is reused by manifest URIs in `boxman import-image` (see
below) so that a remote `manifest.json` is fetched once.

---

## Template creation

`boxman create-templates` builds template VMs from cloud base images
using cloud-init. See the README "Cloud-init template creation" section
for a worked example. The image cache above is engaged automatically
whenever the template's `image:` field is a URL.

---

## `boxman import-image`

Define a libvirt VM from a pre-built package consisting of:

- a libvirt domain XML
- a qcow2/raw disk image
- a JSON `manifest.json` describing the two

### Usage

```bash
boxman import-image \
  --uri    file:///path/to/manifest.json \
  --name   my-imported-vm \
  --directory ~/myvms \
  --provider libvirt
```

### Flags

| Flag | Required | Notes |
|---|---|---|
| `--uri` | yes | `file://`, `http://`, or `https://` URI of the manifest |
| `--name` | no | Override the VM name; defaults to the `<name>` element in the XML |
| `--directory` | no | Where on the libvirt host to place the new VM's disk + XML; defaults to the current directory |
| `--provider` | no | Skips parsing the manifest to discover the provider; only `libvirt` is currently supported |

### Manifest schema

```json
{
  "xml_path":   "vm/vm-definition.xml",
  "image_path": "vm/disk-image.qcow2",
  "provider":   "libvirt"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `xml_path` | string | yes | Path to the libvirt domain XML, relative to the manifest |
| `image_path` | string | yes | Path to the disk image, relative to the manifest |
| `provider` | string | yes | Must be `libvirt` |

Unknown fields are ignored. Validation runs before any disk I/O — bad
manifests fail fast with a `ValueError` describing the offending field.

### Manifest URI schemes

| Scheme | Status | Behaviour |
|---|---|---|
| `file://` | supported | Local manifest; `xml_path` and `image_path` are resolved relative to the manifest's directory |
| `http://`, `https://` | supported | Manifest is downloaded (wget → curl → urllib fallback) to a temp file; relative `xml_path` / `image_path` are resolved alongside the downloaded copy |

> **Known limitation**: when the manifest is fetched over HTTP, the
> `xml_path` and `image_path` referenced by it must still be locally
> resolvable from the downloaded manifest's directory. Fully-remote
> packages (HTTP `xml_path` / `image_path`) are tracked as a follow-up
> — see `data/templates/libvirt_image_manifest_web.json` for the
> intended shape.

### Example workflow — local package

```bash
# 1. Extract or copy a vendor-supplied package
tar xf vendor-vm.tar.gz -C /srv/vm-packages/vendor-vm
ls /srv/vm-packages/vendor-vm
# manifest.json
# vm/vm-definition.xml
# vm/disk-image.qcow2

# 2. Import
boxman import-image \
  --uri file:///srv/vm-packages/vendor-vm/manifest.json \
  --name lab-appliance-01 \
  --directory ~/myvms

# 3. Boxman copies the disk into ~/myvms/lab-appliance-01/, edits the
#    domain XML to point at the new disk path and a fresh UUID, and
#    runs `virsh define` so the VM is ready to start.
virsh start lab-appliance-01
```

### Example workflow — remote manifest

```bash
boxman import-image \
  --uri https://internal.example.com/vms/lab-appliance-01/manifest.json \
  --name lab-appliance-01 \
  --directory ~/myvms
```

### Templates for new packages

Two starter manifests live under `data/templates/`:

- `libvirt_image_manifest_localdir.json` — local package layout
- `libvirt_image_manifest_web.json` — HTTP-served package (note the
  limitation above)

---

## `boxman image push` (OCI registry)

Push a qcow2 image (and optional `vmimage.json` metadata) to an OCI
registry using [oras](https://oras.land/). Useful for distributing
pre-built VM images via the same registries that hold container images.

### Usage

```bash
boxman image push registry.example.com/my-vms/ubuntu:latest \
  --qcow2    /path/to/disk.qcow2 \
  --metadata /path/to/vmimage.json   # optional
```

### Flags

| Flag | Required | Notes |
|---|---|---|
| `image_ref` (positional) | yes | OCI reference, e.g. `registry.example.com/repo:tag` |
| `--qcow2` | yes | Path to the qcow2 disk image file |
| `--metadata` | no | Path to a `vmimage.json` metadata file |

### Authentication

Authentication is delegated to oras and follows oras-supported methods:

- Environment variables: `ORAS_USERNAME`, `ORAS_PASSWORD`
- Config file: `~/.oras/config.json` (e.g. populated by `oras login`)
- Interactive prompt at push time if no credentials are configured

### Prerequisites

- The [`oras`](https://oras.land/docs/installation) CLI must be on `PATH`.
- A reachable OCI registry that accepts artifact pushes.

### Errors

| Error | Cause |
|---|---|
| `image_ref must be a non-empty string` | Positional ref was empty |
| `qcow2 file not found: <path>` | `--qcow2` path does not exist |
| `metadata file not found: <path>` | `--metadata` path does not exist |
| `oras CLI not found` | Install `oras` and ensure it is on `PATH` |
| `oras push failed for '<ref>'.` | Registry rejected the push (auth, permissions, or quota); inspect the included stderr in the error message |

---

## `boxman image inspect` (OCI registry)

Inspect an OCI image reference **without downloading the full qcow2**. Fetches
the manifest with `oras manifest fetch` and, when a `vmimage.json` layer is
present, fetches just that small blob to surface its metadata. The `kind` line
classifies the reference: `artifact` (a boxman/oras titled-qcow2 artifact),
`image` / `image-index` (a container image — a candidate KubeVirt containerDisk
whose embedded `/disk/*.qcow2` is extracted on pull), or `unknown`.

### Usage

```bash
boxman image inspect oci://registry.example.com/my-vms/ubuntu:latest
```

```
image_ref: registry.example.com/my-vms/ubuntu:latest
kind: artifact
media_type: application/vnd.oci.image.manifest.v1+json
layers: 2
  - disk.qcow2 (application/octet-stream, 678331904 bytes)
  - vmimage.json (application/json, 96 bytes)
metadata:
  firmware: uefi
  machine: None
  disk_bus: virtio
  net_model: virtio
```

A KubeVirt containerDisk instead reports it is a container image:

```bash
boxman image inspect oci://quay.io/containerdisks/ubuntu:24.04
```

```
image_ref: quay.io/containerdisks/ubuntu:24.04
kind: image-index
  (container image — if it is a KubeVirt-style containerDisk, boxman extracts
   the embedded /disk/*.qcow2 on pull)
manifests (image index): 2
  - linux/amd64 (application/vnd.oci.image.manifest.v1+json, 525 bytes)
  - linux/arm64 (application/vnd.oci.image.manifest.v1+json, 525 bytes)
```

The `oci://` scheme is optional (`registry.example.com/repo:tag` also works).
Authentication and prerequisites are the same as `boxman image push` above.

---

## OCI registry base images

An OCI image can serve as the base for boxman-built VMs in two ways (template
`image.uri` or direct `base_image`). Both reuse the image cache above, so the
download happens once. A worked example lives in
`boxes/tiny-libvirt-ubuntu-24.04-oci`.

Two registry source layouts are supported, detected automatically from the
manifest on pull:

- **boxman / oras artifact** — a qcow2 stored as a titled OCI layer (what
  `boxman image push` produces). Expected to contain a `disk.qcow2` (any
  `*.qcow2` is accepted as a fallback).
- **container image / KubeVirt containerDisk** — a qcow2 embedded in a container
  image's filesystem at `/disk/*.qcow2`, e.g.
  `oci://quay.io/containerdisks/ubuntu:24.04`. This lets boxman launch VMs from
  the large existing ecosystem of containerDisk images. Multi-arch references
  are resolved to the host architecture, then the carrying layer is fetched and
  the qcow2 extracted. (Layers compressed with zstd are not yet supported.)

### As a template `image.uri` (recommended)

Point a template's `image.uri` at an `oci://` reference. The image is pulled and
cached on the first `boxman up` / `boxman create-templates`, then a template VM
is built from it — with cloud-init, exactly like an http(s) base image:

```yaml
templates:
  template1:
    name: ubuntu-24.04-oci-base-template-cloudinit
    image:
      uri: oci://registry.example.com/boxman/ubuntu-24.04:latest
    os_variant: ubuntu24.04
    cloudinit: | ...
clusters:
  cluster_1:
    base_image: ubuntu-24.04-oci-base-template-cloudinit
```

### Directly as `base_image`

Reference the registry straight from a cluster- or VM-level `base_image`. Boxman
synthesizes an implicit template (named `boxman-oci-<…>`), pulls the image and
clones from it. The synthesized template carries no explicit cloud-init, so the
template build applies boxman's **default** cloud-init (creates a default user,
reconfigures networking) — point it at a cloud-init-enabled cloud image. Use the
template `image.uri` form above when you need custom cloud-init:

```yaml
clusters:
  cluster_1:
    base_image: oci://registry.example.com/boxman/ubuntu-24.04:latest
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `manifest at <uri> missing required field 'xml_path'` | Manifest schema invalid | Check the manifest matches the schema above |
| `manifest at <uri> provider 'foo' not supported` | Wrong `provider` value | Set `provider` to `libvirt` |
| `failed to download manifest from <url>` | Network / TLS / 404 | `curl -I <url>` to check; ensure the host can reach it |
| `Disk image file not found: <path>` | `image_path` resolved against the wrong base directory | When using `file://`, the path is relative to the manifest; check the manifest's directory layout |
| `VM '<name>' already exists` | Re-running an import without a unique name | Pass a different `--name` or destroy the existing VM |
