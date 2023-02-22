# boxman

Boxman (box manager) is a package that can be used to manage
infrastructure using configuration files (yaml). It is
inspired by ``Docker Compose`` and ``vagrant``.
The main goal is to avoid having many dependencies and to
keep it simple and customizable.


## Installation

 - git clone
 - python setup.py install

### other pre-requisites

    - sshpass
    - ansible

## Sample configuration

  https://github.com/mherkazandjian/boxman/blob/main/data/conf.yml

## Usage

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
