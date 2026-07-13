# Netfilter spike — findings

Phase 0 deliverable — issue [#48](https://github.com/mherkazandjian/boxman/issues/48) (epic #42).
Produced by [`poc.sh`](poc.sh); run it on a docker lab host and record results here.

## Question

Can boxman keep the host-global `bridge-nf-call-iptables=1` and still pass
VM↔container L2 traffic on a shared bridge, using **per-bridge scoped iptables
rules** instead of the global disable currently performed by
`src/boxman/netlab/shared_bridges.py`?

## Environment

<!-- paste the "=== environment ===" block from the poc.sh output -->

```
(pending — run: sudo bash poc.sh --with-docker-restart --emulate-docker-policy)
```

## Results

<!-- paste the "=== summary ===" table from the poc.sh output -->

| scenario | expected | actual | verdict |
|---|---|---|---|
| 1-baseline-nf1 | blocked | *(pending)* | |
| 2-global-disable | works | *(pending)* | |
| 3-scoped-forward | works | *(pending)* | |
| 4-docker-user | works | *(pending)* | |
| 4b-du-survives-restart | works | *(pending)* | |
| 5-restart-flips-nf | (info) | *(pending)* | |
| 6a–6e macvlan legs | see script | *(pending)* | |
| 7-idempotent-insert | works | *(pending)* | |

## Pre-registered recommendation (confirm or amend after the run)

Phase 4 ([#52](https://github.com/mherkazandjian/boxman/issues/52)) changes
`shared_bridges.ensure()` to:

1. Keep `bridge-nf-call-iptables` **untouched** by default
   (`disable_netfilter` default flips to `false`).
2. Per shared bridge, insert an idempotent scoped accept rule
   (`iptables -C` before `-I`):

   ```
   iptables -I FORWARD 1 -i <bridge> -o <bridge> -m physdev --physdev-is-bridged -j ACCEPT
   ```

   On docker hosts, prefer the same rule in the `DOCKER-USER` chain if
   scenario 4/4b confirms it works and survives docker restarts.
3. Keep the global `bridge-nf-call-iptables=0` path as an **explicit opt-in**
   (`disable_netfilter: true`) with a loud warning about its host-global,
   docker/k8s-weakening, restart-fragile nature (scenario 5 evidence).

## Notes per scenario

*(fill in anything surprising — especially 6a/6b if macvlan-via-bridge-parent
frames turn out not to traverse FORWARD the way veth-bridged frames do, and
6d/6e for the host↔macvlan shim demo)*
