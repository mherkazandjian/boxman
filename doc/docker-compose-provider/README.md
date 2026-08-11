# Docker-Compose Provider

Design *and* user documentation for docker-compose as a first-class provider in
boxman, enabling mixed topologies where libvirt VMs and docker containers
coexist with L2 connectivity.

## Using it (start here)

- **[user-guide.md](user-guide.md)** — the practical path: prerequisites, a
  minimal project, lifecycle, volumes, networking, `exec`, ansible, snapshots,
  scoping in mixed projects, troubleshooting.
- **[config-schema.md](config-schema.md)** — every key and its exact semantics.
- **[migration-v1-to-v2.md](migration-v1-to-v2.md)** — moving an existing
  project to config v2.0 (spoiler: only needed if you want containers).
- **[demo-runbook.md](demo-runbook.md)** — a rehearsed live walkthrough: one
  config with a VM and a container, `up`, the unified `ps`, an ARP-level proof
  that the shared bridge really is layer 2, and teardown. Doubles as a
  copy-pasteable smoke test of the whole epic.
- Example boxes:
  [`docker-compose-standalone`](../../boxes/docker-compose-standalone)
  (containers only — volumes, `exec`, inventory, snapshots; no KVM needed) and
  [`hybrid-libvirt-docker-compose`](../../boxes/hybrid-libvirt-docker-compose)
  (a VM and a container sharing an L2 domain).

> `docker-compose` names two independent things in boxman: a **runtime** (where
> libvirt commands run) and this **provider** (what a cluster is made of). See
> [the user guide](user-guide.md#first-the-name-collision).

## Design documents

### Main Design Document
- **[design.md](design.md)** - Complete design document with Mermaid diagrams covering:
  - Architecture overview
  - Provider dispatch model
  - Configuration schema v2.0
  - Network integration
  - Volume management
  - Implementation phases
  - Breaking changes & versioning

### Architecture Diagrams (Draw.io)

Open these files with [draw.io](https://app.diagrams.net/) or any compatible editor:

- **[architecture.drawio](architecture.drawio)** - Top-down architecture showing:
  - CLI layer
  - BoxmanManager with Provider Registry
  - Per-cluster dispatch (libvirt vs docker-compose)
  - Shared bridges for L2 connectivity
  - Integration with existing systems (runtime, containerlab)

- **[network-integration.drawio](network-integration.drawio)** - Network topology showing:
  - Host-level shared bridge (br-app)
  - Libvirt VMs with dual NICs (NAT + shared bridge)
  - Docker containers with macvlan attachment to shared bridge
  - Cluster-internal isolated networks
  - iptables/sysfs configuration

- **[provider-dispatch.drawio](provider-dispatch.drawio)** - Provider dispatch flow showing:
  - Config loading and version detection
  - Per-cluster provider selection
  - Libvirt vs docker-compose operation paths
  - Shared infrastructure setup

- **[config-schema.drawio](config-schema.drawio)** - Configuration schema v2.0 showing:
  - Top-level keys (version, project, provider, shared_networks, clusters)
  - Provider configurations
  - Cluster definitions with per-cluster provider
  - Box definitions for both libvirt and docker-compose

- **[implementation-phases.drawio](implementation-phases.drawio)** - Implementation timeline showing:
  - 9 phases from design to release
  - Acceptance criteria per phase
  - Risk assessment
  - Dependencies between phases

### Example Configuration

- **[example-conf.yml](example-conf.yml)** - Complete working example showing:
  - Hybrid topology with libvirt VMs and docker containers
  - Shared network for L2 connectivity
  - Libvirt cluster with application servers
  - Docker-compose cluster with supporting services (nginx, api, postgres, redis, prometheus)
  - Volume management (named volumes, bind mounts)
  - Environment variables and secrets
  - Task definitions

### Implementation Plan & Decisions

- **[implementation-plan.md](implementation-plan.md)** - Phased plan grounded against `main`; one tracking issue per phase (#48–#57)
- **[adr-001-per-cluster-provider.md](adr-001-per-cluster-provider.md)** - ADR: provider granularity is per-cluster, not per-box
- **[spike/](spike/)** - Netfilter spike: `poc.sh` scenario matrix + `findings.md` (**run 2026-07-13 — scoped per-bridge rules confirmed** vs the global `bridge-nf-call-iptables=0`)
- **[../../boxes/dc-provider-staging/](../../boxes/dc-provider-staging/)** - The epic's staging/testing VM (docker + nested libvirt pre-baked, snapshot-resettable)

## Key Design Decisions

### 1. Per-Cluster Provider Model
Each cluster declares its own provider (`libvirt` or `docker-compose`), allowing VMs and containers to coexist in the same project but in different clusters.

### 2. Terminology: "boxes" instead of "vms"
Config schema v2.0 introduces `boxes:` as a generic term covering both VMs and containers. The `vms:` key remains valid in v1.0 configs for backward compatibility.

### 3. Shared Bridges for L2 Connectivity
Containers attach to host-level Linux bridges via macvlan, providing full L2 adjacency with libvirt VMs (ARP, DHCP, 802.1Q all work).

### 4. No Breaking Changes
- v1.0 configs work unchanged
- v2.0 configs with libvirt-only produce identical behavior to v1.0
- Mixed providers are new functionality in v2.0

### 5. Scoped Netfilter Rules (no global disable)
Shared bridges keep `bridge-nf-call-iptables=1`; per-bridge physdev accept rules allow lab frames. The former global disable is an explicit, discouraged opt-in (decision D8 in [design.md](design.md), evidence in [spike/findings.md](spike/findings.md)).

## Implementation Phases

| | Phase | Issue | Status |
|---|---|---|---|
| 0 | Design & validation (incl. netfilter spike) | [#48](https://github.com/mherkazandjian/boxman/issues/48) | done |
| 1 | Provider registry & multi-provider dispatch | [#49](https://github.com/mherkazandjian/boxman/issues/49) | done |
| 2 | Config schema v2.0 & versioning | [#50](https://github.com/mherkazandjian/boxman/issues/50) | done |
| 3 | DockerComposeSession core lifecycle | [#51](https://github.com/mherkazandjian/boxman/issues/51) | done |
| 4 | Network integration (macvlan, shared bridges) | [#52](https://github.com/mherkazandjian/boxman/issues/52) | done |
| 5 | Volume / storage management | [#53](https://github.com/mherkazandjian/boxman/issues/53) | done |
| 6 | CLI & user-experience updates | [#54](https://github.com/mherkazandjian/boxman/issues/54) | done |
| 7 | Snapshot & state management | [#55](https://github.com/mherkazandjian/boxman/issues/55) | done |
| 8 | Testing & documentation | [#56](https://github.com/mherkazandjian/boxman/issues/56) | in progress |
| 9 | Release | [#57](https://github.com/mherkazandjian/boxman/issues/57) | pending |

Each phase merged into the epic branch `feat/docker-compose-provider`, which
merges into `main` after one final epic review. Every phase carries a drift
retro on its tracking issue recording where the implementation diverged from
the plan.

## Next Steps

1. Finish Phase 8 — e2e tests, example coverage, user docs, migration guide ([#56](https://github.com/mherkazandjian/boxman/issues/56))
2. Phase 9 — version bump, changelog, tag, and close the epic ([#57](https://github.com/mherkazandjian/boxman/issues/57))

## Related Issues

- Epic — GitHub Issue [#42](https://github.com/mherkazandjian/boxman/issues/42): master plan for adding docker compose as a provider
- Phase tracking issues: [#48](https://github.com/mherkazandjian/boxman/issues/48)–[#57](https://github.com/mherkazandjian/boxman/issues/57) (one per phase, see [implementation-plan.md](implementation-plan.md))
