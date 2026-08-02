# Demo runbook — the docker-compose provider

A live walkthrough of the epic: one config declaring a libvirt VM **and** a
docker-compose service, brought up together, shown in one view, proven to share
an L2 domain, then torn down.

Every command below was rehearsed end-to-end on the staging VM and the output is
what it actually printed — including one expected-but-ugly libvirt error in the
teardown beat, called out where it appears.

**Timings measured during rehearsal:** `boxman up` (containers) **7s** ·
teardown **13s** · hybrid cold provision **2m12s** (which is why it is done
before the talk, not during).

---

## Where this runs, and why

Inside the **stage01** staging VM, over ssh from the workstation.

The hybrid box needs libvirt, Docker and `sudo` on the *same* host. stage01 has
nested KVM (`/dev/kvm`, vmx exposed) plus Docker and compose v2 precisely so
this epic's live tests never touch the workstation's hypervisor. One sentence of
framing for the audience is enough: *"we're inside a throwaway VM that has both
libvirt and Docker, so nothing here touches my laptop."*

```bash
# from the workstation
ssh -F ~/workspaces/boxmandev/dc-provider-staging/ssh_config staging_stage01
```

Then, inside stage01, everything uses:

```bash
export PATH=/admin/.venv-boxman/bin:$PATH   # so `boxman` is on PATH
```

| Thing | Path |
|---|---|
| hybrid project (pre-provisioned) | `~/fresh-hybrid` |
| container-only project | `~/fresh-standalone` |
| hybrid workspace (ssh_config, inventory) | `~/workspaces/boxmandev/hybrid-libvirt-docker-compose` |
| wheel-install venv (closing beat) | `~/demo-venv` |

---

## Pre-flight — run this the morning of

```bash
# 1. on the WORKSTATION: is stage01 up?
virsh -c qemu:///system list | grep stage01
#   -> 2   bprj__dc_provider_staging__bprj_staging_stage01   running
#   if missing: cd ~/boxman-demo/repo/boxes/dc-provider-staging && boxman up

# 2. inside STAGE01: hybrid must be UP, standalone must be DOWN
cd ~/fresh-hybrid && boxman ps          # -> node01 running + web running
docker ps -a --format '{{.Names}}' | grep -c dc_standalone   # -> 0

# 3. the L2 precondition must be ABSENT (beat 4 assigns it live)
WS=~/workspaces/boxmandev/hybrid-libvirt-docker-compose
ssh -F $WS/ssh_config compute_node01 ip -br addr show enp7s0
#   -> enp7s0  UP  fe80::5054:ff:feaa:2/64     (link-local only, no IPv4)

# 4. port 8080 free (the standalone frontend publishes it)
ss -ltn | grep -q ':8080 ' && echo IN-USE || echo free
```

If the hybrid project is **not** up, bring it back with `cd ~/fresh-hybrid &&
boxman up` and allow **~2m15s**. Do this before the audience arrives.

---

## Beat 1 — one config, two providers

```bash
cd ~/fresh-hybrid
sed -n '/^clusters:/,$p' conf.yml | head -40
```

Point at three things:

- `clusters.compute` → `provider: libvirt`, with `boxes: node01`
- `clusters.services` → `provider: docker-compose`, with `boxes: web`
- `shared_networks.app_bridge` → `bridge: bx_app`, `subnet: 10.10.0.0/24`

**Say:** one file, one project, two providers. The provider is chosen
*per cluster*, so a project can be part VM and part container. `boxes:` is the
provider-neutral word — VMs and containers are both boxes.

Worth adding: the VM's second NIC and the container both attach to
`app_bridge`. The container joins it as a **macvlan** whose parent is that host
bridge, which is what makes them L2-adjacent rather than routed.

---

## Beat 2 — `boxman up` (live, 7 seconds)

Run this in the **container-only** project so the audience watches a real
provision finish in seconds rather than waiting out a VM boot.

```bash
cd ~/fresh-standalone
boxman up
```

Actual output:

```
 Container dc_standalone_web-cache-1  Waiting
 Container dc_standalone_web-cache-1  Healthy
 Container dc_standalone_web-frontend-1  Starting
 Container dc_standalone_web-frontend-1  Started
 Container dc_standalone_web-frontend-1  Healthy
 Container dc_standalone_web-cache-1  Healthy
```

```bash
curl -sI localhost:8080 | head -1     # -> HTTP/1.1 200 OK
```

**Say:** boxman translated `boxes:` into a real `docker-compose.yml`, wrote it
into the cluster workdir, and ran `docker compose up -d --wait` — so the command
only returns once the healthchecks pass. That file is inspectable and
hand-runnable; boxman is not hiding compose from you:

```bash
cat .boxman/web/docker-compose.yml | head -20
```

---

## Beat 3 — one unified view

```bash
cd ~/fresh-hybrid
boxman ps
```

Actual output:

```
Id  Cluster   Name    Provider        State
--  --------  ------  --------------  -------
0   compute   node01  libvirt         running
-   services  web     docker-compose  running
```

**Say:** one table, both providers. `Id 0` is a real libvirt domain id; the `-`
is a container, which has no virsh id. Every verb works this way — `up`, `down`,
`ps`, `destroy`, `snapshot`, `control`, and the generated Ansible inventory.

> **Note:** `boxman list` is a *different* command — it lists registered
> projects (name, config path, runtime, networks), not boxes. `ps` is the
> cross-provider view. Don't reach for `list` here.

Optional extra beat — the Ansible inventory spans both providers:

```bash
cat ~/workspaces/boxmandev/hybrid-libvirt-docker-compose/inventory/01-hosts.yml
```

The VM is an ordinary ssh host; the container carries
`ansible_connection: community.docker.docker` and its real container name.

---

## Beat 4 — prove the L2 actually works

Set up a helper first (a function, not an alias — works when pasted anywhere):

```bash
WS=~/workspaces/boxmandev/hybrid-libvirt-docker-compose
vmsh() { ssh -F "$WS/ssh_config" compute_node01 "$@"; }
```

> `boxman ssh compute_node01` opens an interactive shell but takes **no**
> trailing command, so one-off commands go through the ssh_config boxman
> generated in the workspace.

### 4a. Give the VM its address on the shared bridge

```bash
vmsh sudo ip addr add 10.10.0.20/24 dev enp7s0
vmsh ip -4 -br addr show enp7s0
#   -> enp7s0   UP   10.10.0.20/24
```

**Say:** this bridge is a plain L2 domain with **no DHCP**, on purpose. The
config deliberately does not set this address — an unreachable 10.10.0.x would
otherwise be mistaken for the VM's management IP. So we assign it explicitly,
and nothing about the next step is pre-arranged.

### 4b. Ping both directions

```bash
boxman exec services.web -- ping -c3 10.10.0.20      # container -> VM
```

```
3 packets transmitted, 3 received, 0% packet loss, time 2032ms
rtt min/avg/max/mdev = 0.284/0.295/0.311/0.011 ms
```

```bash
vmsh ping -c3 10.10.0.10                             # VM -> container
```

```
3 packets transmitted, 3 received, 0% packet loss, time 2038ms
rtt min/avg/max/mdev = 0.116/0.172/0.206/0.040 ms
```

### 4c. The money shot — ARP proves it is L2, not routed

```bash
boxman exec services.web -- ip neigh show 10.10.0.20
```

```
10.10.0.20 dev eth1 lladdr 52:54:00:aa:00:02 REACHABLE
```

**Say:** that MAC is not a coincidence — `52:54:00:aa:00:02` is written into the
config as `adapter_2`'s address. The container resolved the VM's *real hardware
address* by ARP, which only happens on a shared layer-2 segment. If this were
routed through the host, you would see the gateway's MAC instead.

```bash
grep -n "52:54:00:aa:00:02" conf.yml     # show it in the config
```

### 4d. And isolation still holds

```bash
vmsh ping -c2 -W2 172.31.0.2
```

```
2 packets transmitted, 0 received, 100% packet loss, time 1014ms
```

**Say:** the container's *other* network is a cluster-internal compose bridge.
The VM cannot reach it. Sharing an L2 domain is opt-in per network, not
all-or-nothing.

---

## Beat 5 — clean teardown

```bash
cd ~/fresh-hybrid
boxman destroy          # prompts [y/N]; type y   (or use -y to skip)
```

> ### Expect a red libvirt error here — it is benign
>
> On libvirt **10.0.0** (what stage01 runs) you will see:
>
> ```
> error: unsupported flags (0x2) in function virStorageBackendVolDeleteLocal
> error: Failed to remove storage volume 'vda'(....qcow2)
> ```
>
> The teardown still completes — note the `Wiping volume 'vda' ... Done.` just
> above it, and boxman removes the workdir itself afterwards. libvirt 10 rejects
> the flag *combination* boxman passes to `virsh undefine`. Rehearsed: VM
> undefined, containers removed, disk directory gone. If it appears, say
> *"libvirt rejects one flag combination on this version; the volume is wiped
> and boxman cleans the directory — let's verify"* and run the proof below.

Prove it, don't assert it (took 13s in rehearsal):

```bash
sudo virsh list --all | grep -c node01          # -> 0
docker ps -a --format '{{.Names}}' | grep -c hybrid   # -> 0
ls ~/workspaces/boxmandev/hybrid-libvirt-docker-compose/compute/   # -> No such file or directory
```

**Say:** VM undefined and its disk gone, containers and their networks removed,
workspace files cleaned — from the same single command that built both.

The **shared bridge is left in place by design** (other projects may be using
it), along with the two scoped netfilter rules. Removing it is explicit:

```bash
sudo iptables -D FORWARD -i bx_app -o bx_app -m physdev --physdev-is-bridged -j ACCEPT
sudo iptables -D DOCKER-USER -i bx_app -o bx_app -m physdev --physdev-is-bridged -j ACCEPT
sudo ip link del bx_app
```

Also worth saying: boxman installs **scoped per-bridge** accept rules rather
than flipping `bridge-nf-call-iptables` off host-wide. That was a deliberate
decision (D8), backed by a spike.

---

## Optional closing beat — what release testing caught

A good 30 seconds if you want to land the "this is a real release" point.

```bash
~/demo-venv/bin/boxman --version                                  # -> v1.0.1.dev0
~/demo-venv/bin/python -c 'import docker; print(docker.__version__)'   # -> 7.2.0
```

That venv was built by installing the **wheel** with
`pip install 'boxman[docker-compose]'`.

**Say:** until this phase, that command silently installed *nothing*. Poetry
resolves `extras` against `[tool.poetry.dependencies]`, and the `docker`
dependency was declared only in a dependency *group* — so the wheel advertised
`Provides-Extra: docker-compose` with no matching requirement. It failed much
later and somewhere else, as a `ModuleNotFoundError` inside Ansible's
`community.docker` plugin.

Nothing in the source tree or the 1331-test suite could catch it, because the
defect only existed in the built artifact. It surfaced because Phase 9's
acceptance criterion is *"the released artifact provisions the example on a
fresh host"* — so the test installs the wheel into a clean venv instead of
testing the checkout.

---

## If it breaks live

| Symptom | Fix |
|---|---|
| stage01 not running | `cd ~/boxman-demo/repo/boxes/dc-provider-staging && boxman up` — this *starts* the existing VM, it does not re-provision |
| hybrid project gone / not up | `cd ~/fresh-hybrid && boxman up` (~2m15s — do not attempt mid-talk; fall back to slides) |
| `bx_app` missing after a stage01 reboot | the Linux bridge is non-persistent; `boxman up` recreates it via `ensure_shared_bridges` |
| ping fails in beat 4 | check `vmsh ip -4 -br addr show enp7s0` — the address is lost on VM restart and must be re-added |
| ssh to `compute_node01` fails after a destroy+up cycle | stale host key: `ssh-keygen -R <node01 mgmt ip>` |
| port 8080 already taken | change the published port in `~/fresh-standalone/conf.yml`, or skip beat 2's `curl` |
| everything is wedged | restore the pristine stage01 snapshot in `~/workspaces/boxmandev/dc-provider-staging/staging/` |

**Never** run `boxman provision --force` against the staging project — it
deprovisions first, which would destroy stage01 itself.

---

## Reset to demo-ready (after a rehearsal)

```bash
# inside stage01
cd ~/fresh-standalone && boxman destroy -y     # standalone must be DOWN
cd ~/fresh-hybrid     && boxman up             # hybrid must be UP  (~2m15s)
cd ~/fresh-hybrid     && boxman ps             # verify both rows present
# leave enp7s0 WITHOUT an IPv4 address — beat 4 assigns it live.
# (a fresh `up` gives a new VM, so the address is absent automatically)
```

Then re-run the pre-flight checklist above.
