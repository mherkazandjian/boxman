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
- **Runtime environments**: execute provider commands locally or inside a Docker container

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
```

```bash
# Create all templates defined in conf.yml
boxman create-templates

# Create specific templates
boxman create-templates --templates my_ubuntu_template

# Force re-creation of existing templates
boxman create-templates --force
```

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

## Runtime Environments

The `--runtime` flag (or `runtime:` in `boxman.yml`) controls *where* provider
commands are executed. The runtime is orthogonal to the provider:

| Runtime | Description |
|---|---|
| `local` (default) | Commands run directly on the host |
| `docker-compose` | Commands run inside the boxman docker-compose container via `docker exec` |

```bash
# Local (default — same as omitting --runtime)
boxman provision

# Inside docker-compose container
boxman --runtime docker-compose provision

# Set the default in ~/.config/boxman/boxman.yml:
#   runtime: docker-compose
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
