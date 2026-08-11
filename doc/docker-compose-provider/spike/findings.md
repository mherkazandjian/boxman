# Netfilter spike — findings

Phase 0 deliverable — issue [#48](https://github.com/mherkazandjian/boxman/issues/48) (epic #42).
Produced by [`poc.sh`](poc.sh), executed 2026-07-13 inside the epic's staging VM
([`boxes/dc-provider-staging`](../../../boxes/dc-provider-staging/) — itself provisioned by boxman).

## Question

Can boxman keep the host-global `bridge-nf-call-iptables=1` and still pass
VM↔container L2 traffic on a shared bridge, using **per-bridge scoped iptables
rules** instead of the global disable currently performed by
`src/boxman/netlab/shared_bridges.py`?

**Answer: yes — confirmed on all counts.**

## Environment

```
6.8.0-31-generic
iptables v1.8.10 (nf_tables)
Docker version 29.1.3, build 29.1.3-0ubuntu3~24.04.2
PRETTY_NAME="Ubuntu 24.04 LTS"
initial bridge-nf-call-iptables=1, FORWARD policy=ACCEPT (docker policy DROP emulated)
```

Invocation: `sudo bash poc.sh --with-docker-restart --emulate-docker-policy`

## Results

| scenario | expected | actual | verdict |
|---|---|---|---|
| 1-baseline-nf1 | blocked | blocked | as-expected |
| 2-global-disable | works | works | as-expected |
| 3-scoped-forward | works | works | as-expected |
| 4-docker-user | works | works | as-expected |
| 4b-du-survives-restart | works | works | as-expected |
| 5-restart-flips-nf | (info) | **nf=0 — did NOT flip back** | info |
| 6a-mv-ct→vm | works | works | as-expected |
| 6b-vm→mv-ct | works | works | as-expected |
| 6c-arp-resolves | works | works | as-expected |
| 6d-host→mv (caveat) | blocked | blocked | as-expected |
| 6e-host→mv via shim | works | works | as-expected |
| 7-idempotent-insert | works | works | as-expected |

## Notes per scenario

- **1** proves the problem is real: with `bridge-nf-call-iptables=1` and a
  docker-style `FORWARD` DROP policy, bridged VM↔container frames are dropped.
- **3 / 4 / 4b**: the scoped rule works in both `FORWARD` and `DOCKER-USER`,
  and the `DOCKER-USER` copy **survives a docker restart** (docker creates but
  does not flush that chain).
- **5 — fragility claim narrowed**: on this stack (docker 29.1.3, Ubuntu
  24.04) a docker daemon restart does **not** reset `bridge-nf-call-iptables`
  to 1 — the original "docker restart silently reverts it" claim does not
  reproduce with modern docker. The global-disable approach is still fragile,
  just via different vectors: **any reboot reverts it** (the `br_netfilter`
  module defaults the sysctl to 1 on load), and **kubelet hard-sets it to 1**
  on kubernetes hosts. The host-wide-weakening objection is unaffected.
- **6a–6c**: full L2 adjacency between a macvlan-attached container
  (`parent=<bridge>`) and a bridge-attached "VM" netns under the target state
  (`nf=1` + scoped rule): ping both directions, ARP neighbor entries resolve.
- **6d/6e**: the documented macvlan limitation — the host cannot reach a
  macvlan child through the parent directly; a `macvlan mode=bridge` shim
  interface restores host↔container reachability. Boxman does not need the
  shim for VM↔container traffic; document it for users who want host access.
- **7**: the `iptables -C`-before-`-I` pattern is idempotent — safe for
  repeated `shared_bridges.ensure()` calls.

## Recommendation (confirmed)

Phase 4 ([#52](https://github.com/mherkazandjian/boxman/issues/52)) changes
`shared_bridges.ensure()` to:

1. Keep `bridge-nf-call-iptables` **untouched** by default
   (`disable_netfilter` default flips to `false`).
2. Per shared bridge, insert an idempotent scoped accept rule
   (`iptables -C` before `-I`):

   ```
   iptables -I FORWARD 1 -i <bridge> -o <bridge> -m physdev --physdev-is-bridged -j ACCEPT
   ```

   Both `FORWARD` and `DOCKER-USER` placements are validated; `FORWARD` is the
   default (works without docker), `DOCKER-USER` is a validated alternative on
   docker hosts (survives docker restarts).
3. Keep the global `bridge-nf-call-iptables=0` path as an **explicit opt-in**
   (`disable_netfilter: true`) with a loud warning: host-wide weakening of
   docker/k8s bridge filtering, reverted by any reboot (module-load default)
   and by kubelet.
