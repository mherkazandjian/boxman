# hybrid-libvirt-containerlab-nokia-srlinux

Same topology as the Arista and Cisco examples, but the switch is Nokia
SR Linux running as a **native container** — no vrnetlab wrapping and
**no account required** to pull the image. The quickest of the three
examples to get running from scratch.

## Prerequisites

- libvirt + qemu-kvm
- Docker
- containerlab:
  ```bash
  bash -c "$(curl -sL https://get.containerlab.dev)"
  ```

## Obtaining the SR Linux image

SR Linux publishes a public container image to the GitHub Container
Registry. No login, no Nokia account — just `docker pull`.

```bash
docker pull ghcr.io/nokia/srlinux:24.10.1
docker images | grep srlinux
# ghcr.io/nokia/srlinux   24.10.1   <id>   <time>   ~2GB
```

Newer tags are published as Nokia ships releases; check
https://github.com/nokia/srlinux-container-image for the current list.
If you pick a different tag, update `containerlab.topology.nodes.sw1.image`
in `conf.yml`.

The SR Linux container wants a couple of sysctls relaxed — containerlab
handles this automatically at deploy time.

## Bringing the box up

```bash
cd boxes/hybrid-libvirt-containerlab-nokia-srlinux
boxman up
```

Re-running `boxman up` is safe. It reconciles state after a host reboot
or manual `docker stop`.

## Using it

```bash
boxman netlab inspect

# drop into the SR Linux CLI. default creds: admin / NokiaSrl1!
$(boxman netlab ssh sw1)
# A:sw1# info from state / system information
# A:sw1# info from state / interface ethernet-1/1
# A:sw1# enter candidate
# A:sw1# ... set / interface ethernet-1/1 description "new-description"
# A:sw1# commit now

# and the libvirt VM
boxman ssh host01
# from host01: tcpdump -i ens7 to see LLDP / traffic; the second NIC
# is connected to sw1 via the shared `clab_mgmt` bridge.
```

## Tearing down

```bash
boxman destroy
sudo ip link delete clab_mgmt    # optional, removes the shared bridge
```

## Why pick SR Linux for a lab

- Free, public image — no registration, fastest on-ramp.
- First-class containerlab support (Nokia created containerlab).
- Modern model-driven NOS: YANG, gNMI, JSON-RPC out of the box. Good
  for practising with Ansible collections that target SR Linux or with
  gNMI clients like `gnmic`.
- Lightweight: boots in seconds vs. vrnetlab-wrapped KVM NOSes that
  take 90-120s.
