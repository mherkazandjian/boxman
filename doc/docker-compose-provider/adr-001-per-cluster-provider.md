# ADR-001: Provider granularity is per-cluster, not per-box

- Status: **accepted** (2026-07-13, ratified on [#48](https://github.com/mherkazandjian/boxman/issues/48))
- Epic: [#42](https://github.com/mherkazandjian/boxman/issues/42)

## Context

Issue #42 asks to "explore the design of having a per-cluster provider —
and/or a per-box provider". Config schema v2.0 introduces a `provider:` key;
this ADR fixes the level at which it may appear.

## Options considered

1. **Per-cluster provider** — every box in a cluster is provisioned by the
   cluster's single provider.
2. **Per-box provider** — each box may name its own provider; a cluster can
   mix VMs and containers.
3. Both (per-cluster default, per-box override).

## Decision

**Per-cluster only** (option 1).

A cluster is boxman's lifecycle unit, and every mechanism that makes a cluster
useful is cluster-scoped:

- one docker-compose *project* / one libvirt session per cluster — up, down,
  destroy operate on the unit;
- cluster-internal networks are defined per cluster;
- ansible inventory groups, generated ssh config, and snapshot operations
  group by cluster;
- the generated `docker-compose.yml` is one file per cluster — a per-box
  provider would fragment it.

The topology per-box would buy is already expressible: *a box of another
provider is a one-box cluster*. Mixed VM/container projects work today in the
design via multiple clusters sharing `shared_networks` for L2 adjacency.

## Consequences

- `provider:` is valid only at cluster level (and as the top-level default);
  a `provider:` key inside a box definition is a config error (enforced in
  Phase 2, [#50](https://github.com/mherkazandjian/boxman/issues/50)).
- Provider dispatch in the manager resolves per cluster
  (Phase 1, [#49](https://github.com/mherkazandjian/boxman/issues/49)).
- Docs teach the one-box-cluster pattern for "just one container next to my
  VMs" use cases.

## Revisit if

A real topology demonstrates that the one-box-cluster workaround fails —
e.g. boxes of different providers that must share a *cluster-internal*
network or a single lifecycle ordering that cross-cluster dependencies
cannot express. Reopen against #42 with the concrete case.
