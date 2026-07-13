# Docker-Compose Provider Design Documents

This directory contains the design documents for adding docker-compose as a first-class provider in boxman, enabling mixed topologies where libvirt VMs and docker containers coexist with L2 connectivity.

## Documents

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

1. **Phase 0**: Design & Validation (this phase)
2. **Phase 1**: Provider Registry & Multi-Provider Dispatch
3. **Phase 2**: Config Schema v2.0 & Versioning
4. **Phase 3**: DockerComposeSession Core Lifecycle
5. **Phase 4**: Network Integration (macvlan, shared bridges)
6. **Phase 5**: Volume/Storage Management
7. **Phase 6**: CLI & User Experience Updates
8. **Phase 7**: Snapshot & State Management
9. **Phase 8**: Testing & Documentation
10. **Phase 9**: Release

## Next Steps

1. ~~Run the netfilter spike~~ — executed 2026-07-13 in the staging VM ([spike/findings.md](spike/findings.md))
2. Merge the Phase 0 PR into the epic feature branch `feat/docker-compose-provider` — closes Phase 0 ([#48](https://github.com/mherkazandjian/boxman/issues/48)); the feature branch accumulates all phases and merges into `main` after one final epic review
3. Begin Phase 1 — provider registry ([#49](https://github.com/mherkazandjian/boxman/issues/49))

## Related Issues

- Epic — GitHub Issue [#42](https://github.com/mherkazandjian/boxman/issues/42): master plan for adding docker compose as a provider
- Phase tracking issues: [#48](https://github.com/mherkazandjian/boxman/issues/48)–[#57](https://github.com/mherkazandjian/boxman/issues/57) (one per phase, see [implementation-plan.md](implementation-plan.md))
