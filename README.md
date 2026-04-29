# boxman

Boxman (**box** **man**ager) is a package that can be used to manage
infrastructure using configuration files (yaml). It is
inspired by ``Docker Compose`` and ``vagrant``.
The main goal is to avoid having many dependencies and to
keep it simple and customizable.


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
    - oras (for OCI image push/pull operations)

## Sample configuration

  https://github.com/mherkazandjian/boxman/blob/main/data/conf.yml

## Usage

### Manage VM images (OCI Registry)

#### Pull a VM image from an OCI registry

````bash
  # Inspect an OCI image reference to see what will be used
  boxman image inspect oci://registry.example.com/my-vms/ubuntu:latest
  
  # Use an OCI image as base_image in your cluster configuration
  # (the image will be automatically pulled on first provision)
  # conf.yml:
  #   clusters:
  #     my-cluster:
  #       base_image: oci://registry.example.com/my-vms/ubuntu:latest
````

#### Push a VM image to an OCI registry

````bash
  # Push a local qcow2 disk image to an OCI registry
  boxman image push registry.example.com/my-vms/ubuntu:latest \
    --qcow2 /path/to/disk.qcow2
  
  # Push with optional metadata (vmimage.json)
  boxman image push registry.example.com/my-vms/ubuntu:latest \
    --qcow2 /path/to/disk.qcow2 \
    --metadata /path/to/vmimage.json
````

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
