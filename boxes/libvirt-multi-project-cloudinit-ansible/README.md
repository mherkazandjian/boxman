# Multi-project setup of two os flavors managed by ansible

This is a multi-project setup of two os flavors managed by ansible. The ansible playbook will create
two projects, one for each os flavor, and will create a vm in each project. The projects are in
the `rocky9` and `centos7` directories, the configuration files are:

  - [rocky9/conf.yml](rocky9/conf.yml)
  - [centos7/conf.yml](centos7/conf.yml)

The purpose of this project to demonstrate using ansible to manage multiple similar infrastrucres
with different os flavors and manage them both with a single ansible setup.

## provisioning / deprovisioning

To provision the vms, run the following command from the root of the repository:

```bash

    boxman --conf projects/rocky9/conf.yml deprovision
    boxman --conf projects/rocky9/conf.yml provision
    boxman --conf projects/centos7/conf.yml deprovision
    boxman --conf projects/centos7/conf.yml provision
```

After the provisioning is complete, the files marked ``NEW`` below are created:

```
├── ansible
│   └── site.yml
├── ansible.cfg                         # NEW
├── env.sh                              # NEW
├── inventory                           # NEW
│   └── 01-hosts.yml                    # NEW
├── projects
│   ├── centos7
│   │   ├── conf.rendered.yml           # NEW (not intended to be committed to git or edited)
│   │   └── conf.yml
│   └── rocky9
│       ├── conf.rendered.yml           # NEW (not intended to be committed to git or edited)
│       └── conf.yml
└── README.md
```

## Running ansible playbooks

To run the ansible playbooks, run the following command from the root of the repository:

```bash

    boxman --conf projects/rocky9/conf.yml run site
```

note that this will fail for centos7 since it is does not have python3 installed.

## Development

Once the resources are provisioned, it is recommended to version control the following files once
adjusted to the needs of the user:

  - [ansible.cfg](ansible.cfg)
  - [env.sh](env.sh)
  - [inventory/01-hosts.yml](inventory/01-hosts.yml)
  - [projects/rocky9/conf.rendered.yml](projects/rocky9/conf.rendered.yml)
  - [projects/centos7/conf.rendered.yml](projects/centos7/conf.rendered.yml)

For example it is recomended to add the ansible collections path in case some role development
is needed by adding the following line to ``env.sh``:

```
ANSIBLE_COLLECTIONS_PATH=/path/to/my_ansible_code:~/.ansible/collections:/usr/share/ansible/collections
```

the directory ``/path/to/my_ansible_code`` should contain the subdir ``ansible_collections``. For
example

```
tree /path/to/my_ansible_code/ansible_collections

/path/to/my_ansible_code/ansible_collections/
├── linux_setup
│   ├── bootstrap
│   │   └── roles
│   │       ├── admin_account
│   │       │   ├── tasks
│   │       ├── fail2ban
│   │       │   ├── tasks/main.yml
│   ├── docker
│   │   └── roles
│   │       └── docker
│   │           ├── tasks/main.yml
```
