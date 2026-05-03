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

The first two cover the common "spin up VMs from a public cloud image"
flow. The third is for moving an already-built VM (an exported lab VM,
a vendor appliance, etc.) onto a libvirt host.

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

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `manifest at <uri> missing required field 'xml_path'` | Manifest schema invalid | Check the manifest matches the schema above |
| `manifest at <uri> provider 'foo' not supported` | Wrong `provider` value | Set `provider` to `libvirt` |
| `failed to download manifest from <url>` | Network / TLS / 404 | `curl -I <url>` to check; ensure the host can reach it |
| `Disk image file not found: <path>` | `image_path` resolved against the wrong base directory | When using `file://`, the path is relative to the manifest; check the manifest's directory layout |
| `VM '<name>' already exists` | Re-running an import without a unique name | Pass a different `--name` or destroy the existing VM |
