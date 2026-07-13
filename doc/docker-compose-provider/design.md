# Docker-Compose Provider Design

## Overview

This document describes the design for adding docker-compose as a first-class provider in boxman, enabling mixed topologies where libvirt VMs and docker containers coexist in the same project with L2 connectivity via shared bridges.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Provider Dispatch Model](#provider-dispatch-model)
3. [Configuration Schema v2.0](#configuration-schema-v20)
4. [Network Integration](#network-integration)
5. [Volume Management](#volume-management)
6. [Implementation Phases](#implementation-phases)
7. [Breaking Changes & Versioning](#breaking-changes--versioning)
8. [Decisions (Phase 0)](#decisions-phase-0)

---

## Architecture Overview

### Top-Down Architecture

```mermaid
graph TB
    subgraph CLI["boxman CLI (app.py)"]
        UP[boxman up]
        DESTROY[boxman destroy]
        PROVISION[boxman provision]
        SNAPSHOT[boxman snapshot]
    end

    subgraph Manager["BoxmanManager (manager.py)"]
        direction TB
        subgraph Registry["Provider Registry (NEW)"]
            direction LR
            LV["libvirt<br/>LibVirtSession"]
            DC["docker-compose<br/>DockerComposeSession (NEW)"]
        end
        
        subgraph Clusters["Clusters"]
            direction LR
            CA["Cluster A (libvirt)<br/>boxes: vm1, vm2"]
            CB["Cluster B (docker-compose)<br/>boxes: web, db, redis"]
        end
        
        SB["Shared Bridges<br/>(shared_bridges.py)<br/>host-level Linux bridges"]
        
        subgraph Existing["Existing Systems"]
            direction LR
            RT["Runtime<br/>local | docker"]
            CL["Containerlab<br/>peer system"]
        end
    end

    CLI --> Manager
    Registry --> Clusters
    Clusters --> SB
    Manager --> Existing
```

### Component Interaction

```mermaid
sequenceDiagram
    participant User
    participant CLI
    participant Manager
    participant Registry
    participant LibVirt
    participant DockerCompose
    participant SharedBridges

    User->>CLI: boxman up
    CLI->>Manager: provision()
    Manager->>Registry: get_provider("libvirt")
    Registry-->>Manager: LibVirtSession
    Manager->>Registry: get_provider("docker-compose")
    Registry-->>Manager: DockerComposeSession
    
    Manager->>SharedBridges: ensure(shared_networks)
    SharedBridges-->>Manager: bridges ready
    
    par Libvirt Cluster
        Manager->>LibVirt: define_networks(cluster_a)
        Manager->>LibVirt: clone_vms(cluster_a)
        Manager->>LibVirt: configure_and_start(cluster_a)
    and Docker-Compose Cluster
        Manager->>DockerCompose: generate_compose(cluster_b)
        Manager->>DockerCompose: docker_compose_up(cluster_b)
        Manager->>DockerCompose: wait_healthy(cluster_b)
    end
    
    Manager-->>CLI: infrastructure ready
    CLI-->>User: connection info
```

---

## Provider Dispatch Model

### Current Model (Single Provider)

```mermaid
graph LR
    A[app.py] --> B{provider_type}
    B -->|libvirt| C[LibVirtSession]
    B -->|virtualbox| D[Virtualbox]
    C --> E[manager.provider = session]
    D --> E
    E --> F[All clusters use same provider]
```

### Proposed Model (Per-Cluster Provider)

```mermaid
graph TB
    A[app.py] --> B[BoxmanManager]
    B --> C[Provider Registry]
    
    C --> D{cluster.provider}
    D -->|libvirt| E[LibVirtSession]
    D -->|docker-compose| F[DockerComposeSession]
    
    E --> G[Cluster A: VMs]
    F --> H[Cluster B: Containers]
    
    subgraph Dispatch["Per-Cluster Dispatch"]
        I[define_networks]
        J[create_boxes]
        K[configure_and_start]
    end
    
    G --> Dispatch
    H --> Dispatch
```

### Provider Protocol Extension

```mermaid
classDiagram
    class ProviderSession {
        <<protocol>>
        +provider_config: dict
        +uri: str
        +use_sudo: bool
        +update_provider_config()
        +start_vm(vm_name)
        +destroy_vm(name, force)
        +clone_vm(new, src, info, workdir)
        +define_network(name, info, workdir)
        +destroy_network(name, info)
        +remove_network(name, info)
        +snapshot_take(...)
        +snapshot_restore(...)
        +snapshot_delete(...)
        +snapshot_list(...)
    }
    
    class ProviderSessionExtended {
        <<protocol>>
        +create_box(name, info, workdir)
        +destroy_box(name)
        +start_box(name)
        +stop_box(name)
        +box_ip_addresses(name)
        +configure_box_networks(cluster)
        +configure_box_volumes(cluster)
    }
    
    class LibVirtSession {
        -provider_config
        -manager
        +clone_vm()
        +define_network()
        ...
    }
    
    class DockerComposeSession {
        -compose_generator
        -compose_runner
        -volume_manager
        +create_box()
        +destroy_box()
        +generate_compose_file()
        +docker_compose_up()
        ...
    }
    
    ProviderSession <|-- ProviderSessionExtended
    ProviderSession <|.. LibVirtSession
    ProviderSessionExtended <|.. DockerComposeSession
```

---

## Configuration Schema v2.0

### Version Detection

```mermaid
flowchart TD
    A[Load conf.yml] --> B{Has version key?}
    B -->|No| C[Treat as v1.0]
    B -->|Yes| D{version value}
    D -->|'1.0'| C
    D -->|'2.0'| E[Treat as v2.0]
    D -->|other| F[Error: unsupported version]
    
    C --> G[Use vms: key]
    E --> H[Use boxes: key]
    H --> I{cluster.provider?}
    I -->|libvirt| J[boxes → vms internally]
    I -->|docker-compose| K[boxes kept as-is]
```

### v2.0 Config Structure

```yaml
version: '2.0'
project: my_hybrid_lab

provider:
  libvirt:
    uri: qemu:///system
    use_sudo: false
  docker-compose:
    project_name: my_hybrid_lab

shared_networks:
  app_bridge:
    bridge: br-app
    stp: false
    disable_netfilter: false   # default — scoped per-bridge rules instead (decision D8)

clusters:
  compute:
    provider: libvirt
    workdir: ~/workspaces/my_lab/compute
    base_image: ubuntu-24.04-cloudinit
    networks:
      mgmt:
        mode: nat
        network: 192.168.10.0/24
    boxes:
      node01:
        hostname: node01
        cpus: { sockets: 1, cores: 2 }
        memory: 2048
        network_adapters:
          - name: eth0
            network_source: mgmt
          - name: eth1
            network_source: app_bridge

  services:
    provider: docker-compose
    workdir: ~/workspaces/my_lab/services
    networks:
      backend:
        driver: bridge
        subnet: 172.20.0.0/24
    boxes:
      web:
        image: nginx:latest
        ports:
          - "8080:80"
        networks:
          - backend
          - app_bridge
        depends_on:
          - api
      api:
        image: myapp/api:latest
        networks:
          - backend
      db:
        image: postgres:16
        volumes:
          - name: pg_data
            container_path: /var/lib/postgresql/data
            size: 10G
        networks:
          - backend
```

### Migration Path

```mermaid
graph LR
    A[v1.0 Config] -->|No changes needed| B[Works as-is]
    C[v2.0 Config] -->|libvirt only| D[boxes → vms internally]
    C -->|docker-compose| E[New functionality]
    C -->|mixed providers| F[Per-cluster dispatch]
    
    B --> G[Backward Compatible]
    D --> G
    E --> H[New Features]
    F --> H
```

---

## Network Integration

### L2 Connectivity via Shared Bridges

```mermaid
graph TB
    subgraph Host["HOST MACHINE"]
        subgraph Bridge["br-app (shared bridge)<br/>10.0.0.0/24"]
            direction LR
        end
        
        subgraph LibvirtCluster["Libvirt Cluster (compute)"]
            VM1["VM node01<br/>eth0: virbr1 (NAT)<br/>eth1: br-app"]
        end
        
        subgraph DockerCluster["Docker-Compose Cluster (services)"]
            WEB["web container<br/>eth0: backend (docker)<br/>eth1: br-app (macvlan)"]
            API["api container<br/>eth0: backend (docker)<br/>eth1: br-app (macvlan)"]
        end
        
        subgraph DockerNetworks["Docker Networks"]
            BACKEND["backend<br/>172.20.0.0/24<br/>docker bridge"]
        end
        
        subgraph LibvirtNetworks["Libvirt Networks"]
            VIRBR1["virbr1<br/>192.168.10.0/24<br/>NAT"]
        end
    end
    
    Bridge --> VM1
    Bridge --> WEB
    Bridge --> API
    BACKEND --> WEB
    BACKEND --> API
    VIRBR1 --> VM1
```

### Macvlan Configuration

```mermaid
graph LR
    A[conf.yml<br/>shared_networks] --> B[shared_bridges.ensure]
    B --> C[ip link add br-app type bridge]
    
    A --> D[ComposeGenerator]
    D --> E[Generate macvlan network]
    E --> F[docker-compose.yml]
    
    F --> G["networks:<br/>  app_bridge:<br/>    driver: macvlan<br/>    driver_opts:<br/>      parent: br-app"]
    
    C --> H[Host bridge ready]
    G --> I[Containers attach via macvlan]
    H --> I
```

### Network Isolation Model

```mermaid
graph TB
    subgraph SharedNetworks["Shared Networks (L2)"]
        SB1["br-app<br/>shared_bridges.py<br/>scoped physdev accept rules (D8)"]
    end
    
    subgraph LibvirtIsolation["Libvirt Cluster Networks"]
        LN1["virbr1 (NAT)<br/>iptables MASQUERADE"]
        LN2["virbr2 (route)<br/>iptables FORWARD rules"]
    end
    
    subgraph DockerIsolation["Docker Cluster Networks"]
        DN1["backend (bridge)<br/>docker network isolation"]
    end
    
    VM["VM"] --> SB1
    VM --> LN1
    Container["Container"] --> SB1
    Container --> DN1
    
    SB1 -.->|L2 adjacency| VM
    SB1 -.->|L2 adjacency| Container
    
    LN1 -.->|Isolated| VM
    DN1 -.->|Isolated| Container
```

### Netfilter Policy (decision D8)

`shared_bridges.ensure()` keeps the host-global `bridge-nf-call-iptables`
**untouched** by default. Lab frames on a shared bridge are allowed by an
idempotent per-bridge rule instead:

```
iptables -I FORWARD 1 -i <bridge> -o <bridge> -m physdev --physdev-is-bridged -j ACCEPT
```

(`FORWARD` is the default — it works without docker; the `DOCKER-USER` chain
is a spike-validated alternative on docker hosts and survives docker
restarts). Rationale: the previous global `bridge-nf-call-iptables=0` weakens
docker/k8s bridge filtering host-wide, is never restored, and silently
reverts on any reboot (the `br_netfilter` module defaults the sysctl to 1 on
load) or on kubernetes hosts (kubelet enforces `=1`) — breaking the lab.
Spike note: modern docker (29.x) itself does *not* reset it on daemon
restart. `disable_netfilter: true` remains available as an explicit opt-in
with a loud warning. Evidence: [spike/findings.md](spike/findings.md)
(executed 2026-07-13, all scenarios pass); implementation: Phase 4
([#52](https://github.com/mherkazandjian/boxman/issues/52)).

---

## Volume Management

### Volume Types

```mermaid
graph TB
    subgraph BoxConfig["Box Volume Config"]
        V1["Named Volume<br/>name: pg_data<br/>container_path: /var/lib/postgresql<br/>size: 10G"]
        V2["Bind Mount<br/>name: config<br/>host_path: ./configs<br/>container_path: /etc/app<br/>readonly: true"]
        V3["Workdir Mount<br/>name: workdir<br/>host_path: .<br/>container_path: /workspace"]
    end
    
    subgraph Translation["ComposeGenerator Translation"]
        T1["pg_data → docker volume<br/>backed by directory"]
        T2["config → bind mount<br/>./configs:/etc/app:ro"]
        T3["workdir → bind mount<br/>workdir:/workspace"]
    end
    
    subgraph Generated["Generated docker-compose.yml"]
        G1["volumes:<br/>  - pg_data:/var/lib/postgresql"]
        G2["volumes:<br/>  - ./configs:/etc/app:ro"]
        G3["volumes:<br/>  - /abs/workdir:/workspace"]
        G4["volumes:<br/>  pg_data:<br/>    driver: local"]
    end
    
    V1 --> T1 --> G1
    V2 --> T2 --> G2
    V3 --> T3 --> G3
    T1 --> G4
```

### Volume Lifecycle

```mermaid
stateDiagram-v2
    [*] --> PreCreate: boxman up
    PreCreate --> CreateDirs: mkdir -p volumes/*
    CreateDirs --> GenerateCompose: write docker-compose.yml
    GenerateCompose --> DockerUp: docker compose up
    DockerUp --> Running: containers started
    
    Running --> Stopped: boxman down
    Stopped --> Running: boxman up
    Stopped --> Destroyed: boxman destroy
    Destroyed --> [*]: rm -rf volumes/*
```

---

## Implementation Phases

### Phase Overview

```mermaid
gantt
    title Docker-Compose Provider Implementation
    dateFormat  YYYY-MM-DD
    section Phase 0
    Design & Validation           :done, p0, 2026-01-01, 7d
    
    section Phase 1
    Provider Registry             :p1, after p0, 14d
    Multi-Provider Dispatch       :p1b, after p1, 7d
    
    section Phase 2
    Config Schema v2.0            :p2, after p1b, 10d
    Version Detection             :p2b, after p2, 5d
    
    section Phase 3
    DockerComposeSession Core     :p3, after p2b, 14d
    ComposeGenerator              :p3b, after p3, 10d
    ComposeRunner                 :p3c, after p3b, 7d
    
    section Phase 4
    Network Integration           :p4, after p3c, 14d
    Macvlan Support               :p4b, after p4, 7d
    
    section Phase 5
    Volume Management             :p5, after p4b, 10d
    
    section Phase 6
    CLI Updates                   :p6, after p5, 14d
    UX Improvements               :p6b, after p6, 7d
    
    section Phase 7
    Snapshot Support              :p7, after p6b, 10d
    
    section Phase 8
    Testing                       :p8, after p7, 14d
    Documentation                 :p8b, after p8, 7d
    
    section Phase 9
    Release                       :milestone, p9, after p8b, 0d
```

### Phase Dependencies

```mermaid
graph LR
    P0[Phase 0<br/>Design] --> P1[Phase 1<br/>Provider Registry]
    P1 --> P2[Phase 2<br/>Config v2.0]
    P2 --> P3[Phase 3<br/>Core Lifecycle]
    P3 --> P4[Phase 4<br/>Networking]
    P4 --> P5[Phase 5<br/>Volumes]
    P5 --> P6[Phase 6<br/>CLI/UX]
    P6 --> P7[Phase 7<br/>Snapshots]
    P7 --> P8[Phase 8<br/>Testing]
    P8 --> P9[Phase 9<br/>Release]
    
    P1 -.->|Risk: High<br/>Core refactor| P1
    P3 -.->|Risk: Medium<br/>New provider| P3
    P4 -.->|Risk: Medium<br/>L2 complexity| P4
```

### Acceptance Criteria per Phase

```mermaid
graph TB
    subgraph Phase1["Phase 1: Provider Registry"]
        AC1["✓ Existing v1.0 libvirt projects work unchanged"]
        AC2["✓ boxman up/destroy pass all existing tests"]
        AC3["✓ Provider dispatch works for single provider"]
    end
    
    subgraph Phase2["Phase 2: Config v2.0"]
        AC4["✓ v2.0 config with libvirt-only = v1.0 behavior"]
        AC5["✓ boxes: accepted as alias for vms:"]
        AC6["✓ Per-cluster provider: key parsed correctly"]
    end
    
    subgraph Phase3["Phase 3: Core Lifecycle"]
        AC7["✓ Docker-compose cluster: boxman up works"]
        AC8["✓ Docker-compose cluster: boxman destroy works"]
        AC9["✓ Container health checks pass"]
    end
    
    subgraph Phase4["Phase 4: Networking"]
        AC10["✓ VM and container on shared bridge can ping"]
        AC11["✓ ARP works between VM and container"]
        AC12["✓ Cluster-internal networks isolated"]
    end
    
    subgraph Phase5["Phase 5: Volumes"]
        AC13["✓ Named volumes persist across down/up"]
        AC14["✓ Bind mounts work correctly"]
        AC15["✓ Volumes cleaned on destroy"]
    end
    
    subgraph Phase6["Phase 6: CLI/UX"]
        AC16["✓ boxman ps shows VMs and containers"]
        AC17["✓ boxman ssh works for containers"]
        AC18["✓ Inventory includes containers"]
    end
```

---

## Breaking Changes & Versioning

### Compatibility Matrix

```mermaid
graph TB
    subgraph v1.0["v1.0 Configs"]
        V1A[libvirt only] -->|Works| OK1[✓ No changes needed]
        V1B[virtualbox] -->|Works| OK2[✓ No changes needed]
    end
    
    subgraph v2.0["v2.0 Configs"]
        V2A[libvirt only] -->|Works| OK3[✓ boxes → vms internally]
        V2B[docker-compose only] -->|Works| OK4[✓ New feature]
        V2C[mixed providers] -->|Works| OK5[✓ New feature]
    end
    
    subgraph Deprecation["Deprecation Path"]
        D1["v1.0 schema: supported indefinitely"]
        D2["vms: in v2.0 libvirt: accepted with warning"]
        D3["vms: in v2.0 docker-compose: rejected"]
    end
```

### Version Detection Logic

```python
def load_config(path):
    config = yaml.safe_load(rendered)
    version = config.get('version', '1.0')
    
    if version == '1.0':
        return config  # no transformation
    elif version == '2.0':
        return normalize_v2_config(config)
    else:
        raise ConfigError(f"unsupported version: {version}")

def normalize_v2_config(config):
    """Convert v2.0 to internal format."""
    for cluster_name, cluster in config['clusters'].items():
        provider = cluster.get('provider', 'libvirt')
        
        if provider == 'libvirt':
            # boxes → vms for libvirt code path
            if 'boxes' in cluster:
                cluster['vms'] = cluster.pop('boxes')
        elif provider == 'docker-compose':
            # boxes kept as-is for docker-compose code path
            pass
    
    return config
```

---

## Decisions (Phase 0)

Ratified on [#48](https://github.com/mherkazandjian/boxman/issues/48). See also
[adr-001-per-cluster-provider.md](adr-001-per-cluster-provider.md) and
[spike/findings.md](spike/findings.md). These supersede the former "Open
Questions" section.

| # | Question | Decision |
|---|---|---|
| D1 | Container readiness | `docker compose up --wait` + per-cluster `readiness_timeout` (default 120s): waits for `healthy` when a healthcheck exists, `running` otherwise — no custom polling loop. |
| D2 | Container access | `boxman ssh <cluster>.<box>` transparently uses `docker exec -it` — no sshd sidecars. Ansible reaches containers via the `community.docker` connection plugin. `write_ssh_config` stays VM-only. |
| D3 | Snapshot semantics | `docker commit`-backed: `take` commits + tags `boxman/<project>_<box>:<snap>`; `restore` regenerates the compose file with snapshot tags + `up --force-recreate`; `list`/`delete` = image ls/rmi + metadata JSON in the cluster workdir. **Volumes are not snapshotted** — documented loudly. |
| D4 | `build.context` | Resolved by boxman to an absolute path (relative to the conf.yml directory) at generation time — the generated compose file lives in the cluster workdir, so passing relative paths through would silently break. |
| D5 | Compose file location | `<cluster_workdir>/docker-compose.yml` — inspectable and hand-runnable (`docker compose -f … ps`), same debuggability philosophy as the `.rendered.yml` dump. |
| D6 | Provider granularity | Per-cluster only; per-box rejected ([ADR-001](adr-001-per-cluster-provider.md)). A box of another provider is a one-box cluster. |
| D7 | Compose passthrough | `compose_extra:` escape hatch (per-box and per-cluster), deep-merged verbatim into the generated file — keeps the boxman dialect small without blocking on unsupported compose features. |
| D8 | Netfilter | Shared bridges keep host-global `bridge-nf-call-iptables` untouched by default (`disable_netfilter` defaults to `false`); per-bridge scoped physdev accept rules allow lab frames. Global disable = explicit opt-in with loud warning. See [Netfilter Policy](#netfilter-policy-decision-d8). Implemented in Phase 4 ([#52](https://github.com/mherkazandjian/boxman/issues/52)). |

---

## Next Steps

1. Run the netfilter spike ([spike/poc.sh](spike/poc.sh)) on a docker lab host and record [spike/findings.md](spike/findings.md)
2. Merge this design (PR → `main`), closing Phase 0 ([#48](https://github.com/mherkazandjian/boxman/issues/48))
3. Begin Phase 1 — provider registry ([#49](https://github.com/mherkazandjian/boxman/issues/49)); full phase map in [implementation-plan.md](implementation-plan.md)
