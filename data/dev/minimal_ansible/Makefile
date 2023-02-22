SHELL=bash

INVENTORY ?= configs/full/inventory
SSH_CONFIG ?=
GATEWAYHOST ?= mgmt01

CURRENT_ENV_EXISTS := $(shell test -f .current_env && printf yes)


ifeq (${CURRENT_ENV_EXISTS},yes)
include .current_env
else
$(info )
$(info WARNING:)
$(info |    .current_env does not exist or points to a dead symlink)
$(info |    set an environemt by pointing to a certain environment file)
$(info )
$(info examples:)
$(info )
$(info |    - make set-current-env env=~/workspaces/full_hpccluster/env.sh)
$(info |    - make set-default-env env=~/workspaces/full_hpccluster/env.sh)
endif

ANSIBLE_FLAGS = -i ${INVENTORY} --ssh-extra-args '-F ${SSH_CONFIG}'

.PHONY: nothing
nothing:

set-default-env:
	@ln -sf ${env} .default_env

set-current-env:
	@ln -sf ${env} .current_env

set-current-env-as-default:
	@ln -sf .current_env .default_env

use-default-env-as-current:
	@ln -sf .default_env .current_env

env:
	@[[ -f .current_env ]] && echo current_env: || true
	@[[ -f .current_env ]] && ls -l .current_env || true
	@[[ -f .default_env ]] && echo default_env || true
	@[[ -f .default_env ]] && ls -l .default_env || true
	@echo "current environment"
	@echo "-------------------"
	@echo "env vars"
	@echo "    INVENTORY=${INVENTORY}"
	@echo "    SSH_CONFIG=${SSH_CONFIG}"
	@echo "    GATEWAYHOST=${GATEWAYHOST}"
	@echo "    ANSIBLE_CONFIG=${ANSIBLE_CONFIG}"
	@echo "make vars"
	@echo "    ANSIBLE_FLAGS=${ANSIBLE_FLAGS}"

boxman-provision:
	$(info todo: pass the config/{infra} if infra is set)
	exit -1
	cd configs/${infra} && boxman provision

boxman-snapshot:
	$(info todo: pass the config/{infra} if infra is set)
	exit -1
	cd configs/${infra} && boxman snapshot take --name ${name} -m "snapshot from make" --no-live

boxman-restore:
	$(info todo: pass the config/{infra} if infra is set)
	exit -1
	cd configs/${infra} && boxman snapshot restore --name ${name}

ping:
	ansible ${ANSIBLE_FLAGS}  all -o -m ping

cmd:
	ansible ${ANSIBLE_FLAGS} all -o -a "cat /etc/os-release"

playbooks-syntax:
	ansible-playbook ${ANSIBLE_FLAGS} --become ansible/site.yml --syntax-check

playbooks: playbooks-syntax
	ansible-playbook  ${ANSIBLE_FLAGS} --become ansible/site.yml ${TAGS} ${ARGS}

play: playbooks

bootstrap:
	ansible-playbook ${ANSIBLE_FLAGS} ansible/site.yml --tags=bootstrap

ssh-control:
	ssh -F ${SSH_CONFIG} -t ${GATEWAYHOST} "sudo su - root"
ssh-root: ssh-control
ssh:
	ssh -F ${SSH_CONFIG} -t ${GATEWAYHOST}
ssh-to:
	ssh -F ${SSH_CONFIG} -t ${host}

helpenv:
	@echo "to change the environment variables needed to execute ansible playbook"
	@echo ""
	@echo "  export INVENTORY=..."
	@echo "  export SSH_CONFIG=..."
	@echo "  export GATEWAYHOST=..."

help: env
	@echo ""
	@echo "available targets"
	@echo "   help"
	@echo "     show this message"
	@echo "   env"
	@echo "     set-default-env env=/path/to/bashrc"
	@echo "     set-current-env env=/path/to/bashrc"
	@echo "     set-current-env-as-default"
	@echo "     use-default-env-as-current"
	@echo "   ansible"
	@echo "     make bootstrap                                  # bootstrap all the hosts"
	@echo "     make ping                                       # ping all hosts"
	@echo "     make cmd                                        # execute 'cmd' on all hosts"
	@echo "     make playbooks-syntax                           # check the syntax of all playbooks"
	@echo "     make playbooks                                  # execute all playbooks"
	@echo "   ssh"
	@echo "     make ssh"
	@echo "     make ssh-root"
	@echo "     make ssh-to host=my_host"
	@echo "   rsync"
	@echo "     rsyncf --rsync-path='sudo rsync' -e 'ssh -F ${HOME}/workspaces/testcluster/ssh_config' /path/to/local/content testcluster01:/path/to/dst"
	@echo "   boxman-provision"
	@echo "     make boxman-provision infra=local"
	@echo "     make boxman-snapshot infra=local name=foo"
	@echo "     make boxman-restore infra=local name=foo"
