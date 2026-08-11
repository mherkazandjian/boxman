# Docker-Compose Provider — Phased Implementation Plan

Epic: [#42](https://github.com/mherkazandjian/boxman/issues/42) · Design docs: this directory · Status: completed (the docker-compose provider is fully implemented; this plan is kept for historical reference)

## Context

Issue #42 asks for a master plan to add docker-compose as a provider with mixed
libvirt/container topologies. The design documents in this directory describe the target
architecture. This plan grounds those designs against the current `main` and splits the work
into 10 phases, each with a GitHub tracking issue (#48–#57).

## Grounding: what `main` already has (the design predates some of it)

| Already built | Where |
|---|---|
| `Provider`/`ProviderSession` runtime-checkable protocols | `src/boxman/abstract/providers.py`, pinned by `tests/test_provider_protocol.py` |
| Shared bridges (host side) incl. `disable_netfilter` → `bridge-nf-call-iptables=0` | `src/boxman/netlab/shared_bridges.py` (`ensure/is_shared_bridge/resolve_bridge`), hook `manager.ensure_shared_bridges()`, `shared_networks:` conf key, `tests/test_netlab_shared_bridges.py` |
| `docker-compose` provider_type recognized (stub) | `src/boxman/scripts/app.py` → `NotImplementedError` |
| Container-integration pattern to mirror | `src/boxman/netlab/containerlab.py` (`ContainerlabManager`: preflight/render/deploy/destroy/inspect/ssh_command + manager hooks) |
| Project cache, tasks, ssh-config/inventory rails | `src/boxman/config_cache.py`, `task_runner.py`, `write_ssh_config`, `_render_inventory` |

**Gaps the original design missed** (folded into the phase issues):

- Name collision: *runtime* `docker-compose` (`runtime/docker_compose.py` = libvirt-in-container)
  vs *provider* `docker-compose` → the provider initially requires `runtime: local`, hard error
  otherwise.
- Single-provider assumptions in `app.py` (`list(config['provider'].keys())[0]`) and
  `manager.provider` typed to `LibVirtSession`.
- `'vms'` appears ~40× in `manager.py` → normalize `boxes→vms` at config load, no rename sweep.
- Cache registration, ansible inventory (`community.docker` connection), `boxman run`/tasks,
  `control` verb mapping for containers.
- **Security (decided)**: the `shared_networks` default `disable_netfilter: true` flips the
  **host-global** `bridge-nf-call-iptables=0`, never restored — weakening docker/k8s bridge
  filtering on the very host the dc-provider targets, while a docker/kubelet restart silently
  flips it back and breaks the lab. Decision: replace with **per-bridge scoped accept rules**
  (physdev / `DOCKER-USER`), flip the default to `disable_netfilter: false`, keep the global
  disable as explicit opt-in with a loud warning. Spiked in Phase 0 (#48), implemented in
  Phase 4 (#52).

## Phases & tracking issues

| # | Issue | Phase | Depends on | Risk | Size |
|---|---|---|---|---|---|
| 0 | [#48](https://github.com/mherkazandjian/boxman/issues/48) | Design closure — open questions, netfilter spike, per-box ADR | — | Low | S–M |
| 1 | [#49](https://github.com/mherkazandjian/boxman/issues/49) | Provider registry & per-cluster dispatch (no behavior change) | #48 | **High** | L |
| 2 | [#50](https://github.com/mherkazandjian/boxman/issues/50) | Config schema v2.0 — version key, `boxes:`, per-cluster `provider:` | #49 | Med | M |
| 3 | [#51](https://github.com/mherkazandjian/boxman/issues/51) | `DockerComposeSession` core lifecycle | #50 | Med–High | L |
| 4 | [#52](https://github.com/mherkazandjian/boxman/issues/52) | Networking — macvlan attach to shared bridges (VM↔container L2) | #51 | Med | M |
| 5 | [#53](https://github.com/mherkazandjian/boxman/issues/53) | Volumes — named/bind/workdir mapping & lifecycle | #51 (∥ 4) | Low–Med | S–M |
| 6 | [#54](https://github.com/mherkazandjian/boxman/issues/54) | CLI/UX parity — list, connect-info, ssh/exec, inventory, tasks | #51 (parts ∥ 4/5) | Med | M |
| 7 | [#55](https://github.com/mherkazandjian/boxman/issues/55) | Container snapshot & state semantics | #51 | Low–Med | S–M |
| 8 | [#56](https://github.com/mherkazandjian/boxman/issues/56) | Hardening — e2e tests, hybrid example box, docs & migration | #52–#55 | Low | M |
| 9 | [#57](https://github.com/mherkazandjian/boxman/issues/57) | Release | #56 | Low | S |

Dependency chain: `0 → 1 → 2 → 3 → {4 ∥ 5, 6, 7} → 8 → 9`

Full task checklists and acceptance criteria live in the tracking issues (canonical);
per-phase acceptance criteria AC1–AC18 are cross-referenced from `design.md`.

## Working agreements

- One branch + PR per phase; the PR references its tracking issue; **tests land in the same
  PR** (repo convention: per-module `tests/test_*.py`, `unit`/`integration` markers). Phase 8
  is e2e + example + docs only — it is not a substitute for per-phase tests.
- `design.md` is a living document — update it in whichever phase diverges from it.
- #42 is the epic; its checklist comment links all phase issues.
