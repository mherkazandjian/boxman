# hybrid-libvirt-containerlab-cisco-iosvl2

Same topology as the Arista cEOS example, but the switch is a Cisco
IOSvL2 (Catalyst-style) device running via vrnetlab. Shows how to
swap vendors in a boxman hybrid lab.

## Prerequisites

- libvirt + qemu-kvm
- Docker
- containerlab:
  ```bash
  bash -c "$(curl -sL https://get.containerlab.dev)"
  ```
- `make`, `git`, `python3` (needed for the vrnetlab build)
- A Cisco CCO / VIRL / CML / EA account to download the IOSvL2 qcow2

## Obtaining the Cisco IOSvL2 image

Unlike Arista cEOS, Cisco does **not** publish a pre-built switch
container image. The standard workflow is to download Cisco's
virtualization qcow2 (IOSvL2) and wrap it in a privileged container
with `srl-labs/vrnetlab`, which runs QEMU inside the container.

The license for the qcow2 is on you: CCO access / VIRL subscription /
Cisco Modeling Labs / Enterprise Agreement are the usual paths.

**1. Download the IOSvL2 qcow2 from Cisco.**

  Sources (pick whichever you have access to):
  - Cisco Modeling Labs (CML): the ref-platform download page.
    Look for `vios_l2-adventerprisek9-m.vmdk.SPA.<version>.qcow2`.
  - CCO / VIRL: same file, older hosting.

  Common filenames you'll see:
  - `vios_l2-adventerprisek9-m.ssa.high_iron_20190423.qcow2` (15.2.1)
  - `vios_l2-adventerprisek9-m.SSA.high_iron_20200929.qcow2` (15.2.2020T)

**2. Clone srl-labs/vrnetlab.**

  ```bash
  git clone https://github.com/srl-labs/vrnetlab.git
  cd vrnetlab/cisco/vios_l2
  ```

**3. Copy the qcow2 into the vios_l2 directory and build.**

  vrnetlab's Makefile derives the image tag from the qcow2 filename,
  so rename the file to encode the version cleanly first.

  ```bash
  # rename to make the version unambiguous
  cp /path/to/vios_l2-adventerprisek9-m.SSA.high_iron_20200929.qcow2 \
     vios_l2-15.2.2020T.qcow2

  # build the vrnetlab container (takes a few minutes; boots the qcow2
  # once to snapshot the post-init state)
  make

  # verify
  docker images | grep cisco_vios_l2
  # vrnetlab/cisco_vios_l2   15.2.2020T   <id>   <time>   ~600MB
  ```

  If your build tags the image differently (say `vrnetlab/vr-iosvl2`),
  update `containerlab.topology.nodes.sw1.image` in `conf.yml` to
  match. Containerlab's kind for this image is `cisco_vios_l2`
  regardless of tag.

**4. Smoke-test the image with containerlab alone.**

  Before wiring it into boxman, confirm containerlab can actually run
  it:

  ```bash
  cat > /tmp/iosvl2-smoke.clab.yml <<'EOF'
  name: iosvl2-smoke
  topology:
    nodes:
      sw1:
        kind: cisco_vios_l2
        image: vrnetlab/cisco_vios_l2:15.2.2020T
  EOF

  sudo containerlab deploy -t /tmp/iosvl2-smoke.clab.yml
  # wait ~90-120 seconds for IOSvL2 to boot inside QEMU
  ssh admin@clab-iosvl2-smoke-sw1   # default admin/admin
  sudo containerlab destroy -t /tmp/iosvl2-smoke.clab.yml
  ```

  Boot time is slow (full QEMU + IOSvL2 startup); this is normal for
  any vrnetlab-wrapped Cisco image.

## Bringing the box up

Once the IOSvL2 container image exists locally (step 3 above), a single
command brings everything online:

```bash
cd boxes/hybrid-libvirt-containerlab-cisco-iosvl2
boxman up
```

Re-running `boxman up` is safe. After a host reboot or manual
`docker stop`, it reconciles state idempotently: starts stopped lab
containers, resumes saved VMs, re-creates the shared bridge if needed.

## Using it

```bash
boxman netlab inspect

# drop into the IOS CLI (user: admin, pass: admin by default — see
# configs/sw1.cfg.j2 for how to override via BOXMAN_CISCO_ADMIN_PASS)
$(boxman netlab ssh sw1)
# sw1> enable
# sw1# show running-config
# sw1# show interfaces status
# sw1# show vlan brief
# sw1# show cdp neighbors

# and the libvirt VM
boxman ssh host01
```

## Tearing down

```bash
boxman destroy
sudo ip link delete clab_mgmt    # optional, removes the shared bridge
```

## Alternatives if you don't have a Cisco qcow2

- **Cisco XRd** — a native container (not vrnetlab-wrapped). Free with
  a CCO account. Change `kind: cisco_xrd`, `image: ios-xr/xrd-control-plane:<tag>`.
  Note: XRd is IOS-XR, a router OS — not a L2 switch. Good for routing labs.
- **Cisco IOL / IOL-L2** — ELF binaries, very lightweight, but the
  images are Cisco-internal and their redistribution is a grey area.
  Containerlab kind `cisco_iol`.
- **Arista cEOS** — see the sibling box `hybrid-libvirt-containerlab-arista-ceos/`.
  Free, fastest path to a working L2 switch.
