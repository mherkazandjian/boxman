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

## Quick Start

### Native (Host) Installation

```bash
pip install -r requirements.txt
pip install .
```

### Docker-based Libvirt Environment

Boxman includes a containerized libvirt/KVM environment for development and testing without
modifying your host system. Only requires Docker with compose v2 and `/dev/kvm` on the host.

See [boxman/containers/docker/README.md](boxman/containers/docker/README.md) for full documentation.

## Usage

```bash
export BOXMAN_ADMIN_PASS=$(cat ~/.onlyme/rocky-95-minimal-base-template-admin-pass)
boxman provision
```

## Requirements

- Python 3.10+
- libvirt with KVM/QEMU
- For Docker mode: Docker with compose v2, `/dev/kvm` on the host

## Installation

 - git clone
 - python setup.py install
 - For the libvirt provider it is necessary that the user executing boxman
   can run sudo virhs and other libvirt commands (see blow).
 - the boxman configuration files are by default searched in `~/.config/boxman/boxman.conf` but
   you can specify a different configuration file using the `--conf` argument.

### other pre-requisites

    - sshpass
    - ansible

## Sample configuration

  https://github.com/mherkazandjian/boxman/blob/main/data/conf.yml

## Usage

### Import vm images

````bash
  # download and extract the vm package from a give url
  curl -L http://example.com/vm-package.tar.gz | tar xv -C ~/tmp/sandbox/

  # import a vm from a disk, a directory called my-ubuntu-vm will be created in ~/myvms
  boxman import-image --uri file://~/http://example.com/vm-package.tar.gz \
    --directory ~/myvms  \
    --name my-ubuntu-vm \
    --provider libvirt
````

### Provision and manage vms

````bash
  boxman provision
  boxman snapshot --name "state before kernel upgrade"
  # ... upgrade the kernel and then end up with a kernel panic
  boxman restore --name "state before kernel upgrade"
````

## Development

 - git clone
 - hack
 - git commit and push
 - test
 - submit pull request

### run boxman in development mode

````
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
