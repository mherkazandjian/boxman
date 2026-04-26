# hybrid-libvirt-containerlab-arista-ceos

One Ubuntu 24.04 libvirt VM and one Arista cEOS switch sharing a host
Linux bridge. Demonstrates boxman's `shared_networks:` + `containerlab:`
blocks.

## Prerequisites

- libvirt + qemu-kvm (you already have these if other boxman boxes work)
- Docker (`docker --version`)
- containerlab — one-line installer:
  ```bash
  bash -c "$(curl -sL https://get.containerlab.dev)"
  ```

## Obtaining the Arista cEOS image

cEOS-lab is free but gated behind an Arista account. Boxman does not
distribute it — you download it yourself and load it into Docker.

**1. Register for a free Arista account.** You only need the free tier.

  https://www.arista.com/en/user-registration

  Account approval is instant.

**2. Download the cEOS-lab tarball.**

  https://www.arista.com/en/support/software-download

  Navigate: *EOS → Active Releases → 4.32 → cEOS-lab → cEOS-lab-4.32.0F.tar.xz*

  File sizes are ~500 MB. Any recent `4.32.x` or `4.31.x` tag works; pick one.

**3. Import the tarball as a Docker image.**

  The file is a root filesystem tarball, so use `docker import` (not `docker
  load`). The image tag must exactly match what `conf.yml` references.

  ```bash
  # verify the download is intact
  sha512sum cEOS-lab-4.32.0F.tar.xz

  # create the image
  docker import cEOS-lab-4.32.0F.tar.xz ceos:4.32.0F

  # sanity check
  docker images | grep ceos
  # ceos   4.32.0F   <id>   <time>   ~1.8GB
  ```

  If you picked a different version, update `containerlab.topology.nodes.sw1.image`
  in `conf.yml` to match the tag you imported.

## Bringing the box up

Once the cEOS image is imported (step 3 above), a single command brings
everything online — the shared bridge, the libvirt VM, and the lab:

```bash
cd boxes/hybrid-libvirt-containerlab-arista-ceos
boxman up
```

Re-running `boxman up` is safe. After a host reboot or a manual
`docker stop`, it reconciles state: starts stopped lab containers,
resumes saved/shut-off VMs, re-creates the shared bridge if it's gone.

## Using it

```bash
# print current lab state as JSON (nodes, mgmt IPs, interfaces)
boxman netlab inspect

# drop into the Arista CLI (default user: admin, no password)
$(boxman netlab ssh sw1)
# sw1> enable
# sw1# show version
# sw1# show interfaces status
# sw1# show lldp neighbors

# ssh into the libvirt VM
boxman ssh host01
# from host01: lldpctl should show sw1 as a neighbor on eth1/ens7.
# sudo dhclient ens7 should pull 192.0.2.2/24 from sw1's SVI.
```

## Tearing down

```bash
boxman destroy
```

The shared `clab_mgmt` bridge is **not** removed (multiple boxman
projects may share it). To remove it:

```bash
sudo ip link delete clab_mgmt
```
