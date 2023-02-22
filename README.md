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

## Contributing

 - git clone
 - hack
 - git commit and push
 - test
 - submit pull request
