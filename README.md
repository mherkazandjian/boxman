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
- **Runtime environments**: execute provider commands locally or inside a Docker container
- **`boxman up`**: idempotent bring-up command — provisions if no infrastructure exists, starts/resumes VMs if they are powered off or paused

## Quick Start

### Native (Host) Installation

```bash
pip install -r requirements.txt
pip install .
```

### Docker-based Libvirt Environment

Boxman includes a containerized libvirt/KVM environment for development and testing without
modifying your host system. Only requires Docker with compose v2 and `/dev/kvm` on the host.

```bash
# Start the docker-compose environment
cd containers/docker && make up

# Provision using the docker-compose runtime
boxman --runtime docker-compose provision
```

See [boxman/containers/docker/README.md](boxman/containers/docker/README.md) for full documentation.

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

# Inside docker-compose container
boxman --runtime docker provision

# Set the default in ~/.config/boxman/boxman.yml:
#   runtime: docker
```

The bundled `docker-compose.yml` is shipped with the package. To use a custom
one, set `compose_file` in `runtime_config` or the `BOXMAN_COMPOSE_FILE`
environment variable.

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
