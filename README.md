# boxman

Boxman (**box** **man**ager) is a package that can be used to manage infrastructure using
configuration files (yaml). It is inspired by ``Docker Compose`` and ``vagrant``.
The main goal is to avoid having many dependencies and to keep it simple and customizable.


## Features

- Declarative VM provisioning via YAML configuration
- Supports libvirt/KVM with QEMU
- Network and disk management
- Snapshot support
- Cloud-init integration
- **Cloud-init template creation**: build template VMs from cloud images with inline cloud-init config
- **Auto-creation of templates on provision**: if a cluster's `base_image` references a template defined in the `templates` section and the template VM does not yet exist, it is automatically created before provisioning proceeds
- **Image caching**: downloaded cloud base images are stored in a local cache directory so the same image is only downloaded once across multiple projects
- **Runtime environments**: execute provider commands locally or inside a Docker container
- **`boxman up`**: idempotent bring-up command — provisions if no infrastructure exists, starts/resumes VMs if they are powered off or paused
- **`boxman update`**: incrementally apply config changes to a running project — add/remove VMs, adjust CPU/memory, grow disks

## Quick Start

### Native (Host) Installation

```bash
pip install -r requirements.txt
pip install .
```

### Docker-based Libvirt Environment

Boxman includes a containerized libvirt/KVM environment for development and testing without
modifying your host system. Only requires Docker with compose v2 and `/dev/kvm` on the host.
Boxman brings the container up automatically — no manual `docker compose up` needed.

```bash
# Minimal end-to-end flow for a brand new machine
cd boxes/tiny-libvirt-ubuntu-24.04-cloudinit-docker-runtime
boxman --runtime=docker up          # provision VMs inside the docker runtime
boxman --runtime=docker destroy -y  # tear down everything, no prompt
```

What happens on first `up`:

- The libvirt container image is built (`docker compose up -d --build`) and
  started. Per-project host ports are derived from the project name so
  multiple boxman projects on the same host do not collide on `2222 / 16509 / 16514`.
- `workspace.path`, every cluster `workdir`, and the template `workdir`
  are bind-mounted into the container at the same absolute path, so
  `virsh`, `qemu-img`, and `rsync` inside the container see the same
  file layout as the host.
- Each workdir is tagged with a `.boxman-runtime` sentinel recording
  which runtime owns it. Running `boxman --runtime=docker up` against a
  workdir that was previously used with `--runtime=local` (or vice
  versa) triggers an interactive prompt suggesting a runtime-specific
  alternative path (e.g. `~/workspaces/myproj-docker-runtime`).
- In `conf.yml`, set `provider.libvirt.use_sudo: False` for the docker
  runtime — the container already executes libvirt commands as root via
  `docker exec --user root`.

See [boxes/tiny-libvirt-ubuntu-24.04-cloudinit-docker-runtime/conf.yml](boxes/tiny-libvirt-ubuntu-24.04-cloudinit-docker-runtime/conf.yml)
for a ready-to-run example, and [containers/docker/README.md](containers/docker/README.md)
for the container internals.

### Create Template VMs from Cloud Images

Define templates in your `conf.yml` with inline cloud-init configuration:

```yaml
templates:
  my_ubuntu_template:
    name: ubuntu-24.04-base-template
    image: file:///path/to/ubuntu-24.04-server-cloudimg-amd64.img
    os_variant: ubuntu24.04
    memory: 2048
    vcpus: 2
    # Resize the cloud image disk to the given size (requires qemu-img).
    # The guest filesystem is grown automatically by cloud-init's growpart module.
    disk_size: 20G
    # Connect to a bridge device directly (recommended for internet access).
    # If omitted, boxman auto-resolves the bridge from the 'network' name.
    bridge: virbr0
    # The libvirt network name (used to auto-resolve bridge if 'bridge' not set)
    network: default
    cloudinit: |
      #cloud-config
      hostname: my-template
      manage_etc_hosts: true
      ssh_pwauth: true
      chpasswd:
        expire: false
        users:
          - name: ubuntu
            password: ubuntu
      package_update: true
    # Optional: custom network config for cloud-init (default: DHCP via virtio)
    # cloudinit_network_config: |
    #   version: 2
    #   ethernets:
    #     id0:
    #       match:
    #         driver: virtio
    #       dhcp4: true
```

```bash
# Create all templates defined in conf.yml
boxman create-templates

# Create specific templates
boxman create-templates --templates my_ubuntu_template

# Force re-creation of existing templates
boxman create-templates --force
```

> **Troubleshooting: Template VM has no IP address**
>
> - Ensure the libvirt network is active: `virsh net-list` — if `default` is
>   inactive, run `sudo virsh net-start default`.
> - Boxman will attempt to auto-start an inactive network, but the network
>   must already be *defined*.
> - Verify DHCP is working: `virsh net-dhcp-leases default`.
> - Check cloud-init logs inside the VM via `virt-manager` console or
>   `virsh console <vm-name>`.

### Image Caching

Boxman caches downloaded cloud base images so that the same image is only
fetched once, even when referenced from multiple projects. Caching is
configured globally in `~/.config/boxman/boxman.yml`:

```yaml
# ~/.config/boxman/boxman.yml
cache:
  enabled: true                        # set to false to disable caching
  cache_dir: ~/.cache/boxman/images    # directory where images are stored
```

Images are keyed by the filename component of their URL (e.g.
`ubuntu-24.04-server-cloudimg-amd64.img`). A cache hit skips the download
entirely; a cache miss downloads the file to `cache_dir` for future reuse.

#### Plain URL (no checksum)

```yaml
templates:
  ubuntu_base:
    image: https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img
    # ...
```

#### URL with checksum verification

Supply the image as a dict with `uri` and `checksum` keys. The checksum
format is `algorithm:hexdigest` (any algorithm supported by Python's
`hashlib`, e.g. `sha256`, `md5`):

```yaml
templates:
  ubuntu_base:
    image:
      uri: https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img
      checksum: sha256:a1b2c3d4e5f6...   # full hex digest
    # ...
```

Checksum verification behaviour:

| Situation | Behaviour |
|---|---|
| Cache hit, checksum specified | Verify cached file; **abort** on mismatch |
| Cache miss, checksum specified | Download, then verify; **abort** on mismatch |
| Cache hit, no checksum | Use cached file as-is |
| Cache miss, no checksum | Download and cache; no verification |
| Cache disabled | Download directly to working directory on each run |

> **Tip**: official Ubuntu cloud images ship a `SHA256SUMS` file alongside
> each image — use the hash from that file to ensure image integrity.

## Requirements

- Python 3.10+
- libvirt with KVM/QEMU (for local runtime)
- For Docker runtime: Docker with compose v2, `/dev/kvm` on the host
- For cloud-init templates: one of `cloud-image-utils`, `genisoimage`, `mkisofs`, or `xorrisofs`

## Installation

 - git clone
 - `pip install .` (or `pip install -e .` for development)
 - For docker-compose runtime extras: `pip install '.[docker-compose]'`
 - For the libvirt provider it is necessary that the user executing boxman
   can run sudo virsh and other libvirt commands (see below).
 - The boxman application config is searched at `~/.config/boxman/boxman.yml` by default.
   Use `--boxman-conf` to specify an alternative path.

### Other pre-requisites

    - sshpass
    - ansible

## Environment Variables in Config Files

Config files (`conf.yml`, `boxman.yml`) are rendered as Jinja2 templates before
being parsed as YAML. The following helper functions are available:

| Syntax | Description |
|---|---|
| `{{ env("VAR") }}` | Value of `$VAR`, empty string if unset |
| `{{ env("VAR", default="fallback") }}` | Value of `$VAR`, `"fallback"` if unset |
| `{{ env("VAR") \| default("fallback", true) }}` | Same, using Jinja2's built-in `default` filter |
| `{{ env_required("VAR") }}` | Value of `$VAR`, **error** if unset or empty |
| `{{ env_required("VAR", "custom error msg") }}` | Same, with a custom error message |
| `{{ env_is_set("VAR") }}` | `True` / `False` — useful in conditionals |

### Examples

```yaml
# conf.yml
clusters:
  my_cluster:
    admin_pass: {{ env_required("BOXMAN_ADMIN_PASS", "BOXMAN_ADMIN_PASS must be set") }}
    admin_user: {{ "admin" if env_is_set("BOXMAN_ADMIN_PASS") else "" }}
    optional_setting: {{ env("BOXMAN_OPTIONAL", default="default_value") }}
```

> **Legacy syntax**: The `${env:VAR}` format (resolved at runtime by
> `BoxmanManager.fetch_value()`) is still supported in `boxman.yml` fields
> like `ssh.authorized_keys` and `admin_pass`. The old `{{ env.VAR }}` dict
> access syntax has been renamed to `{{ environ.VAR }}` to avoid shadowing
> the `env()` helper function. The Jinja `{{ env("VAR") }}` syntax is
> preferred for new configurations as it supports defaults and conditionals.

## Runtime Environments

The `--runtime` flag (or `runtime:` in `boxman.yml`) controls *where* provider
commands are executed. The runtime is orthogonal to the provider:

| Runtime | Description |
|---|---|
| `local` (default) | Commands run directly on the host |
| `docker` | Commands run inside the boxman docker-compose container via `docker exec` |

```bash
# Local (default — same as omitting --runtime)
boxman provision

# Inside the boxman libvirt container
boxman --runtime=docker up

# Nuke everything set up under the docker runtime
boxman --runtime=docker destroy -y

# Set the default in ~/.config/boxman/boxman.yml:
#   runtime: docker
```

The bundled `docker-compose.yml` is shipped with the package. To use a custom
one, set `compose_file` in `runtime_config` or the `BOXMAN_COMPOSE_FILE`
environment variable.

**Cleaning up leftover state**: if a previous docker run left root-owned
files under `boxes/<name>/.boxman/` (libvirt runtime data), `make
boxes-clean` removes them via a throwaway `alpine` container. The same
fallback is used automatically by `destroy` and `destroy-runtime`.

## Sample configuration

  See [`data/templates/boxman.yml`](data/templates/boxman.yml) and
  [`data/conf.yml`](https://github.com/mherkazandjian/boxman/blob/main/data/conf.yml)

## Usage

### Provision and manage VMs

````bash
export BOXMAN_ADMIN_PASS=$(cat ~/.onlyme/rocky-95-minimal-base-template-admin-pass)
boxman provision
boxman snapshot --name "state before kernel upgrade"
# ... upgrade the kernel and then end up with a kernel panic
boxman restore --name "state before kernel upgrade"
````

### SSH into VMs

```bash
# SSH into the gateway host (first VM)
boxman ssh

# SSH into a VM by name
boxman ssh cluster_1_node02

# List VMs and their state
boxman ps
# Id  Cluster    VM      State
# --  ---------  ------  -------
# 0   cluster_1  node01  running
# 1   cluster_1  node02  running

# Include provider-specific columns (virsh Id and Name for libvirt)
boxman ps -p
# Id  Cluster    VM      State    Virsh Id  Virsh Name
# --  ---------  ------  -------  --------  ---------------------------
# 0   cluster_1  node01  running  3         bprj__myproject__bprj_cluster_1_node01
# 1   cluster_1  node02  running  4         bprj__myproject__bprj_cluster_1_node02

# Output as JSON (combine with -p for full provider info)
boxman ps --json
boxman ps -p --json

# SSH into a VM by its id from boxman ps
boxman ssh 0
boxman ssh 1
```

### Import VM images

````bash
# download and extract the vm package from a given url
curl -L http://example.com/vm-package.tar.gz | tar xv -C ~/tmp/sandbox/

# import a vm from a disk, a directory called my-ubuntu-vm will be created in ~/myvms
boxman import-image --uri file://~/http://example.com/vm-package.tar.gz \
  --directory ~/myvms  \
  --name my-ubuntu-vm \
  --provider libvirt
````

## Development

### Run boxman in development mode

````bash
cd $PROJECT_ROOT
PYTHONPATH=src:$PYTHONPATH python3 src/boxman/scripts/app.py <sub-command> <cmd-line-args>

# or export the PYTHONPATH
export PYTHONPATH=$PWD/src:$PYTHONPATH
python3 src/boxman/scripts/app.py <sub-command> <cmd-line-args>

# change to the development infra dir and provision the cluster
cd data/dev/minimal_ansible
python3 ../../../src/boxman/scripts/app.py provision
make set-default-env env=~/tmp/sandbox/test_cluster/env.sh
make set-current-env env=~/tmp/sandbox/test_cluster/env.sh
make ping
````

## Contributing

 - git clone
 - hack
 - git commit and push
 - test
 - submit pull request

## License

This project is licensed under the [MIT License](../LICENSE).

## Boxman Commands

- `import-image` — import an image
- `create-templates` — create template VMs from cloud images using cloud-init
- `list` — list all registered projects
- `provision` — provision a configuration
- `up` — bring up the infrastructure (provision if not created, start if powered off)
- `down` — bring down the infrastructure (save or suspend state)
- `destroy-runtime` — destroy the docker-compose runtime and clean up .boxman
- `destroy` — full teardown (VMs + networks + files + runtime + workspace
  workdir) with a `[y/N]` prompt; use `-y`/`--auto-accept` to skip the prompt
  and `--templates` to also remove template workdirs
- `deprovision` — deprovision a configuration
- `snapshot` — manage snapshots of VMs
  - `snapshot take` — take a snapshot
  - `snapshot list` — list snapshots
  - `snapshot restore` — restore VM state from a snapshot
  - `snapshot delete` — delete a snapshot
- `control` — control the state of VMs
  - `control suspend` — suspend VMs
  - `control resume` — resume VMs
  - `control save` — save the state of VMs
  - `control start` — start VMs
- `export` — export VMs
- `update` — apply config changes to a running project (add/remove VMs, update CPU/memory/disks)
- `import` — import VMs
- `run` — run tasks with the workspace environment loaded
- `ps` — show the state of VMs in the project (`-p` adds provider-specific columns, `--json` outputs JSON)
- `ssh` — ssh into a VM

## Updating a Running Project

The `update` command applies incremental changes to an already-provisioned project.
Edit `conf.yml` and run `boxman update` to reconcile the live state with the config.

### What can be updated

- **Add VMs**: add new VM entries to a cluster's `vms:` section — they will be
  cloned from the base template, configured, and started
- **Remove VMs**: remove VM entries from the config — running VMs will be
  shut down, undefined, and their disks cleaned up
- **CPU and memory**: change `cpus` or `memory` on existing VMs — applied live
  (hot-plug) when possible, otherwise a restart is flagged
- **Add disks**: add new disk entries to a VM's `disks:` section — they will be
  created and attached
- **Grow disks**: increase the `size` of an existing disk — the disk image is
  resized in place (shrinking is not supported)

### What cannot be updated

- **Networks**: adding, removing, or modifying network definitions requires a
  full deprovision/provision cycle
- **Network adapters on existing VMs**: changing `network_adapters` on a VM that
  is already provisioned is not applied by update
- **Base image / template**: changing the `base_image` of an existing VM has no
  effect — the VM was already cloned from the original template
- **Hostname**: changing the `hostname` of an existing VM is not applied
  (cloud-init ran at first boot only)
- **Disk shrinking**: existing disks can only be grown, never shrunk

### Usage

```bash
# Preview what would change without applying
boxman update --dry-run

# Apply changes (prompts for confirmation before destroying VMs)
boxman update

# Apply changes without confirmation prompt
boxman update --yes
```

## Tasks

Tasks are named shell commands defined in the `tasks:` section of `conf.yml`.
They run inside the workspace environment (env vars from `env_file` are loaded
before execution), with the working directory set to `workspace.path`.

Placeholders like `{flags}` and `{tags}` are filled from CLI arguments:

```yaml
tasks:
  ping:
    description: "ping all hosts via ansible"
    command: ansible all {{ flags }} -m ansible.builtin.ping

  cmd:
    description: "run a shell command on all hosts"
    command: ansible all {{ flags }} -m ansible.builtin.shell -a

  site:
    description: "run the full site playbook"
    command: ansible-playbook {{ flags }} --become $ANSIBLE_SITE/site.yml {{ tags }} --

  playbook:
    description: "run a specific playbook"
    command: ansible-playbook {{ flags }} --become $ANSIBLE_SITE/{{ playbook }} {{ tags }} --

  ssh:
    description: "ssh to the gateway host"
    command: ssh -F ${SSH_CONFIG} -t ${GATEWAYHOST}
```

> **Note on shell vs Jinja2 variables**: `$ANSIBLE_SITE`, `$SSH_CONFIG`, and
> `$GATEWAYHOST` use shell variable syntax so they are resolved at task
> execution time (after `env_file` is sourced). Using `{{ env("VAR") }}`
> would resolve at config-render time, before env.sh is loaded.

### Usage examples

```bash
# Ping all hosts
boxman run ping

# Ping with ansible flags (limit to one host)
boxman run ping --flags "--limit node01"

# Run a shell command on all hosts
boxman run cmd -- hostname

# Run a multi-word shell command — everything after -- is joined into one
# argument, so ansible's -a receives the full string as a single value
boxman run cmd -- curl ifconfig.me
boxman run cmd -- uname -a

# Combine ansible flags with a multi-word shell command
boxman run cmd --flags "--limit cluster_1_control01" -- curl ifconfig.me

# Run the full site playbook
boxman run site

# Run site playbook limited to specific hosts
boxman run site --flags "--limit head01"

# Run site playbook with tags (use --tags placeholder, not --)
boxman run site --tags "--tags base,networking"

# Run site playbook limited to specific hosts and with tags
boxman run site --flags "--limit head01" --tags "--tags slurm"

# Run a specific playbook by name
boxman run playbook --playbook networking.yml

# Run a specific playbook with tags
boxman run playbook --playbook storage.yml --tags "--tags beegfs"

# SSH into the gateway host
boxman run ssh
```

> **`--` joins all tokens into one argument**: everything after `--` is
> space-joined and shell-quoted as a single string.  This is ideal for
> `cmd` where the shell command (e.g. `curl ifconfig.me`) must arrive as
> one argument to ansible's `-a`.  For ansible-playbook flags (`--limit`,
> `--tags`, etc.) use the `--flags` / `--tags` placeholders instead.
