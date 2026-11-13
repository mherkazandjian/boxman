# Entry points for boxes/talos-unattended. A plain `boxman up` (with the
# inputs exported) brings the lab from nothing to a healthy Talos cluster:
# conf.yml runs the preflight and the install driver as its pre_up and
# post_up hooks. `make install` is that `boxman up` with an inputs file.
# `make help` lists the targets.
#
#   INPUTS   the private inputs file (default: inputs.env beside this file,
#            see inputs.env.example)
#   BOXMAN   the boxman command the driver runs (default: boxman)
#
# Every target is a script in scripts/, which also run on their own.
SHELL := /bin/bash
BOX_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
INPUTS ?= $(BOX_DIR)/inputs.env
export BOXMAN

# the stages run in order, never in parallel
.NOTPARALLEL:
.PHONY: help preflight install status nodes

help:
	@grep -E '^[a-z-]+:.*#@' $(MAKEFILE_LIST) | sed -E 's/^([a-z-]+):.*#@ ?/  \1\t/' | expand -t 14

preflight:   #@ check inputs, capacity, network and ownership; changes nothing
	$(BOX_DIR)/scripts/preflight.sh --inputs $(INPUTS)

install:   #@ boxman up with INPUTS; its hooks check, configure and bootstrap (rerunnable)
	$(BOX_DIR)/scripts/install.sh --inputs $(INPUTS)

status:   #@ VMs, identity, nodes, etcd, Kubernetes and Flux, read-only
	$(BOX_DIR)/scripts/status.sh --inputs $(INPUTS)

nodes:   #@ print the node table conf.yml defines
	@set -a; . $(INPUTS); set +a; python3 $(BOX_DIR)/scripts/nodes-from-conf.py
