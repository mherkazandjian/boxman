# Workstation-side driver for the two-host Proxmox lab. Every target is
# "ssh <host> && run a script from this box" — the hosts do the work, this
# file only sequences it. `make help` lists the targets; `make all` runs the
# happy path end to end (~45-90 min, mostly unattended installs).
#
#   HOSTS               physical hosts (ssh aliases) — hpe1 must be first: it
#                       runs the DHCP/NAT side and the orchestration scripts
#   LAB                 lab directory on the hosts
#   PVE_ROOT_PASSWORD   root password baked into the ISO (hashed)
#   TARGET / BACK       migration target node, and whether to migrate back
SHELL := /bin/bash
HOSTS ?= hpe1 hpe2
ORCH ?= hpe1
LAB ?= /home/mher/pve-lab
PVE_ROOT_PASSWORD ?= pvelab123
TARGET ?= pve3
BOXMAN_FLAGS ?= -v

BOX_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
REPO := $(abspath $(BOX_DIR)/../..)
BOX_REL := boxes/proxmox-nested-ceph-cluster
REMOTE_BOX := $(LAB)/src/$(BOX_REL)
SCRIPTS := $(REMOTE_BOX)/scripts
BOXMAN := $(LAB)/venv/bin/boxman
SSH := ssh -o BatchMode=yes
RSYNC_EXCLUDES := --exclude .git --exclude .venv --exclude .boxman --exclude __pycache__ \
	--exclude containers/docker/data --exclude dist --exclude .pytest_cache \
	--exclude .claude --exclude '*.rendered.yml'

# $(call run,<host>,<script>[ args])
define run
$(SSH) $(1) 'bash $(SCRIPTS)/$(2)'
endef
# $(call boxman,<host>,<verb and flags>) — double quotes so a loop variable
# used as <host> is expanded by the local shell, not handed to the remote one
define boxman
$(SSH) $(1) "cd $(REMOTE_BOX) && BOXMAN_SITE=$(1) $(BOXMAN) $(BOXMAN_FLAGS) $(2)"
endef

.PHONY: help keys answer sync host-prep iso up wait-installed boot wait-first-boot \
	cluster ceph demo migrate mtu-check status ps down destroy host-clean all

help:
	@grep -E '^[a-z-]+:.*#@' $(MAKEFILE_LIST) | sed -E 's/^([a-z-]+):.*#@ ?/  \1\t/' | expand -t 18

keys: keys/id_ed25519_pvelab   #@ generate the lab ssh keypair (once)
keys/id_ed25519_pvelab:
	mkdir -p keys && ssh-keygen -q -t ed25519 -N "" -C pve-lab -f $@

answer: keys answer.toml   #@ render answer.toml from the template (pubkey + password hash)
answer.toml: answer.toml.tmpl keys/id_ed25519_pvelab
	hash=$$(openssl passwd -6 '$(PVE_ROOT_PASSWORD)'); pub=$$(cat keys/id_ed25519_pvelab.pub); \
	sed -e "s|__PWHASH__|$$hash|" -e "s|__PUBKEY__|$$pub|" $< > $@ && echo "rendered $@"

sync: keys   #@ rsync this repo + keys to the hosts, build the venv, install boxman
	@for h in $(HOSTS); do \
	  echo "== $$h"; \
	  $(SSH) $$h 'mkdir -p $(LAB)/src $(LAB)/keys' && \
	  rsync -a --delete $(RSYNC_EXCLUDES) $(REPO)/ $$h:$(LAB)/src/ && \
	  rsync -a keys/ $$h:$(LAB)/keys/ && \
	  $(SSH) $$h 'chmod 600 $(LAB)/keys/id_ed25519_pvelab; \
	    test -x $(LAB)/venv/bin/pip || python3.12 -m venv $(LAB)/venv; \
	    $(LAB)/venv/bin/pip install -q -e $(LAB)/src && $(BOXMAN) --version' || exit 1; \
	done

host-prep:   #@ host checks + lab directories on every host
	@for h in $(HOSTS); do echo "== $$h"; $(call run,$$h,host-prep.sh) || exit 1; done

iso:   #@ download + bake the auto-install ISO on every host (parallel, ~5 min)
	@for h in $(HOSTS); do $(call run,$$h,prepare-iso.sh) > /tmp/pve-iso-$$h.log 2>&1 & done; wait; \
	for h in $(HOSTS); do echo "== $$h"; tail -3 /tmp/pve-iso-$$h.log; done
	@for h in $(HOSTS); do $(SSH) $$h 'test -s $(LAB)/iso/proxmox-ve_9.2-1-auto.iso' || { echo "no ISO on $$h"; exit 1; }; done

up:   #@ hpe2 vxlan -> hpe1 boxman up -> hpe1 vxlan -> hpe2 boxman up (installs start)
	$(call run,hpe2,vxlan-up.sh)
	$(call boxman,hpe1,up)
	$(call run,hpe1,vxlan-up.sh)
	$(call boxman,hpe2,up)

wait-installed:   #@ wait until every node powered off after its unattended install
	@for h in $(HOSTS); do $(call run,$$h,wait-installed.sh) & done; wait

boot:   #@ start the installed nodes from disk (boxman up #2) and re-check the vxlan
	@for h in $(HOSTS); do $(call boxman,$$h,up) & done; wait
	@for h in $(HOSTS); do $(call run,$$h,vxlan-up.sh); done

wait-first-boot:   #@ wait for the first-boot hook (repos, MTU 1450, guest agent) on all nodes
	$(call run,$(ORCH),wait-first-boot.sh)

cluster:   #@ pvecm create on pve1, pvecm add pve2..pve4
	$(call run,$(ORCH),pve-cluster.sh)

ceph:   #@ pveceph install/init, 3 mons, 2 mgrs, 8 OSDs, pool vmpool
	$(call run,$(ORCH),pve-ceph.sh)

demo:   #@ nested Ubuntu VM demo01 on pve1, disk on vmpool, DHCP from hpe1
	$(call run,$(ORCH),pve-demo-vm.sh)

migrate:   #@ live-migrate demo01 to TARGET (default pve3 on hpe2); BACK=1 returns it to pve1
	$(call run,$(ORCH),pve-migrate.sh $(TARGET))
	@if [ -n "$(BACK)" ]; then $(call run,$(ORCH),pve-migrate.sh pve1); fi

mtu-check:   #@ verify the 1450-byte path MTU across the vxlan
	$(call run,$(ORCH),mtu-check.sh)

status:   #@ boxman ps on both hosts, then bridge/cluster/ceph/vm picture from hpe1
	@for h in $(HOSTS); do echo "== $$h"; $(call boxman,$$h,ps); done
	$(call run,$(ORCH),status.sh)

down:   #@ boxman down (save) on both hosts
	@for h in $(HOSTS); do $(call boxman,$$h,down); done

destroy:   #@ boxman destroy -y on both hosts (nodes, disks, hpe1's nat bridge); keys re-synced after
	@for h in $(HOSTS); do $(call boxman,$$h,destroy -y); done
	@# boxman removes the admin key as a generated artifact; put the lab key back
	@for h in $(HOSTS); do rsync -a keys/ $$h:$(LAB)/keys/ && $(SSH) $$h 'chmod 600 $(LAB)/keys/id_ed25519_pvelab'; done

host-clean: #@ remove the vxlan, hpe2's bridge and the firewall rule (after destroy)
	@for h in $(HOSTS); do $(call run,$$h,host-clean.sh); done

all: keys answer sync host-prep iso up wait-installed boot wait-first-boot mtu-check cluster ceph demo migrate   #@ everything, in order

# ---- Terraform on top of the cluster (runs HERE, through an ssh tunnel) ----
# The provider talks to the API only (image download by the node, import_from,
# built-in cloud-init), so no ssh access from the workstation to the nodes is
# needed. Open the tunnel first:  ssh -N -L 8006:10.77.0.11:8006 hpe1
TF_DIR := $(BOX_DIR)/terraform
TF := terraform -chdir=$(TF_DIR)
VM_COUNT ?= 4

tf-token:   #@ create the API token root@pam!terraform on pve1 and write terraform/.env
	@$(SSH) $(ORCH) 'source $(SCRIPTS)/lib.sh; pssh pve1 "pveum user token list root@pam --output-format json | grep -q \"\\\"terraform\\\"\" && pveum user token remove root@pam terraform >/dev/null; pveum user token add root@pam terraform --privsep 0 --comment lab-terraform --output-format json"' \
	  | sed -n 's/.*"value":"\([^"]*\)".*/\1/p' | { read -r secret; test -n "$$secret" || { echo "no token returned"; exit 1; }; \
	  printf '# generated by make tf-token\nexport TF_VAR_pve_api_token=%s\nexport TF_VAR_pve_endpoint=%s\n' "'root@pam!terraform=$$secret'" "'https://localhost:8006/'" > $(TF_DIR)/.env; }; echo "wrote $(TF_DIR)/.env"

tf-init:   #@ terraform init
	$(TF) init -input=false

tf-plan:   #@ terraform plan (VM_COUNT=n)
	@set -a; . $(TF_DIR)/.env; set +a; $(TF) plan -input=false -var vm_count=$(VM_COUNT)

tf-apply:   #@ terraform apply: VM_COUNT Rocky 9 VMs round-robin over the nodes, disks on vmpool
	@set -a; . $(TF_DIR)/.env; set +a; $(TF) apply -input=false -auto-approve -var vm_count=$(VM_COUNT)

tf-output:   #@ show the VMs' nodes and addresses
	@$(TF) output vms; $(TF) output ssh

tf-destroy:   #@ terraform destroy (VMs and the downloaded images)
	@set -a; . $(TF_DIR)/.env; set +a; $(TF) destroy -input=false -auto-approve -var vm_count=$(VM_COUNT)
