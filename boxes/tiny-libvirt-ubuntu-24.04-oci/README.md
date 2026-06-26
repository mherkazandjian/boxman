# tiny-libvirt-ubuntu-24.04-oci

A single Ubuntu 24.04 VM whose template **base image is pulled from an OCI
registry** with [`oras`](https://oras.land), instead of being downloaded over
http(s). It is otherwise identical to
[`tiny-libvirt-ubuntu-24.04-cloudinit`](../tiny-libvirt-ubuntu-24.04-cloudinit):
same cloud-init, networks (`nat1` + `isolated1`) and single VM `boxman01`.

By default it pulls a **public Docker Hub containerDisk**
(`oci://docker.io/tedezed/ubuntu-container-disk:24.04`) — a container image with
an Ubuntu 24.04 qcow2 embedded at `/disk/` — so the box runs with nothing more
than libvirt + `oras`. No registry account or push step is required.

This demonstrates boxman's OCI image support:

- **Consume an OCI base image** via `image.uri: oci://…` on a template (used
  here) or `base_image: oci://…` directly on a cluster/VM. Boxman handles two
  layouts automatically: its own titled-`disk.qcow2` artifacts *and*
  KubeVirt-style **containerDisk** images (qcow2 embedded at `/disk/`).
- `boxman image inspect oci://…` — view a reference's `kind` + manifest without
  downloading the disk.
- `boxman image push` — publish your own qcow2 (+ optional metadata) as an OCI
  artifact, if you'd rather host the base image yourself (see below).

## Prerequisites

- libvirt/KVM working (`virsh -c qemu:///system list`).
- The `oras` CLI installed and on `PATH`.
- Network access to Docker Hub (the default image is public — no auth needed).

## Bringing the box up

```bash
cd boxes/tiny-libvirt-ubuntu-24.04-oci
boxman up
```

On first run boxman builds the template: it pulls
`docker.io/tedezed/ubuntu-container-disk:24.04`, extracts the VM disk embedded
under `/disk/` (here `disk/ubuntu.img`) into `~/.cache/boxman/images`, imports it
as the template VM `ubuntu-24.04-oci-base-template-cloudinit`, then clones
`boxman01` from it. Subsequent runs reuse the cached image.

```bash
boxman image inspect oci://docker.io/tedezed/ubuntu-container-disk:24.04  # kind: image
boxman ssh boxman01                                                       # log into the VM
```

> The default containerDisk is single-arch (amd64) and is a third-party
> community image on Docker Hub (its `:24.04` tag is mutable). Pulling it extracts
> the disk image and needs transient scratch space of a few GB in
> `~/.cache/boxman/images` (the compressed layer plus the inflated disk). To pin
> or self-host the base, publish your own artifact (see below).

## Alternative: publish and use your own base image

Prefer to host the base yourself? Push a qcow2 as a boxman OCI artifact and
point the template at it:

```bash
# 1. obtain a qcow2 (e.g. the upstream Ubuntu 24.04 cloud image)
curl -L -o disk.qcow2 \
  https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img

# 2. (optional) describe it with a vmimage.json sidecar
cat > vmimage.json <<'JSON'
{ "firmware": "uefi", "disk_bus": "virtio", "net_model": "virtio",
  "name": "ubuntu", "version": "24.04", "arch": "x86_64" }
JSON

# 3. push to a registry you control
#    (auth via `oras login`, ORAS_USERNAME / ORAS_PASSWORD, or ~/.oras/config.json)
boxman image push registry.example.com/boxman/ubuntu-24.04:latest \
  --qcow2 ./disk.qcow2 --metadata ./vmimage.json

# 4. confirm what's there (manifest fetch only — no full download)
boxman image inspect oci://registry.example.com/boxman/ubuntu-24.04:latest
```

Then set `templates.template1.image.uri` in [`conf.yml`](conf.yml) to your
reference. boxman looks for a `disk.qcow2` layer, falling back to any `*.qcow2`.

## Alternative: OCI image directly as `base_image`

Skip the `templates:` block and reference the image straight from the cluster:

```yaml
clusters:
  cluster_1:
    base_image: oci://docker.io/tedezed/ubuntu-container-disk:24.04
```

boxman synthesizes an implicit template (named `boxman-oci-<…>`), pulls the
image and clones from it. The implicit template applies boxman's **default**
cloud-init (default user + networking). Use the template `image.uri` form above
when you need custom cloud-init.

## Tearing down

```bash
boxman destroy
```

This removes `boxman01` and the project's networks. The pulled base image stays
cached under `~/.cache/boxman/images`; delete it there to force a re-pull.
