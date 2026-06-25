# tiny-libvirt-ubuntu-24.04-oci

A single Ubuntu 24.04 VM whose template **base image is pulled from an OCI
registry** with [`oras`](https://oras.land), instead of being downloaded over
http(s). It is otherwise identical to
[`tiny-libvirt-ubuntu-24.04-cloudinit`](../tiny-libvirt-ubuntu-24.04-cloudinit):
same cloud-init, networks (`nat1` + `isolated1`) and single VM `boxman01`.

This demonstrates boxman's OCI image support:

- `boxman image push` — publish a qcow2 (optionally with metadata) to a registry.
- `boxman image inspect` — view a reference's manifest + `vmimage.json` metadata.
- `image.uri: oci://…` on a template — pull + cache the base image on `boxman up`.
- `base_image: oci://…` directly on a cluster/VM — sugar for pre-baked images.

## Prerequisites

- libvirt/KVM working (`virsh -c qemu:///system list`).
- The `oras` CLI installed and on `PATH`.
- Network access + credentials for an OCI registry you can push to. The
  `registry.example.com/boxman/ubuntu-24.04:latest` reference in
  [`conf.yml`](conf.yml) is a **placeholder** — replace it with your registry.
- Registry auth is delegated to oras (`oras login`, `ORAS_USERNAME` /
  `ORAS_PASSWORD`, or `~/.oras/config.json`).

## Publishing the base image (one-time)

Grab an Ubuntu 24.04 cloud image and push it as an OCI artifact named
`disk.qcow2` (boxman looks for `disk.qcow2`, falling back to any `*.qcow2`):

```bash
# 1. obtain a qcow2 (e.g. the upstream cloud image used by the cloudinit box)
curl -L -o disk.qcow2 \
  http://cloud-images-archive.ubuntu.com/releases/noble/release-20240423/ubuntu-24.04-server-cloudimg-amd64.img

# 2. (optional) describe it with a vmimage.json sidecar
cat > vmimage.json <<'JSON'
{ "firmware": "uefi", "disk_bus": "virtio", "net_model": "virtio",
  "name": "ubuntu", "version": "24.04", "arch": "x86_64" }
JSON

# 3. push to your registry
boxman image push registry.example.com/boxman/ubuntu-24.04:latest \
  --qcow2 ./disk.qcow2 \
  --metadata ./vmimage.json

# 4. confirm what's there (manifest fetch only — no full download)
boxman image inspect oci://registry.example.com/boxman/ubuntu-24.04:latest
```

Then point `templates.template1.image.uri` in [`conf.yml`](conf.yml) at the
reference you pushed.

## Bringing the box up

```bash
cd boxes/tiny-libvirt-ubuntu-24.04-oci
boxman up
```

On first run boxman builds the template: it `oras pull`s the qcow2 into
`~/.cache/boxman/images`, imports it as the template VM
`ubuntu-24.04-oci-base-template-cloudinit`, then clones `boxman01` from it.
Subsequent runs reuse the cached image.

```bash
boxman ssh boxman01      # log into the VM
```

## Alternative: OCI image directly as `base_image`

Skip the `templates:` block and reference the registry straight from the
cluster:

```yaml
clusters:
  cluster_1:
    base_image: oci://registry.example.com/boxman/ubuntu-24.04:latest
```

boxman synthesizes an implicit template (named `boxman-oci-<…>`), pulls the
image and clones from it. The implicit template applies boxman's **default**
cloud-init (default user + networking), so point it at a cloud-init-enabled
cloud image. Use the template `image.uri` form above when you need custom
cloud-init.

## Tearing down

```bash
boxman destroy
```

This removes `boxman01` and the project's networks. The pulled base image stays
cached under `~/.cache/boxman/images`; delete it there to force a re-pull.
