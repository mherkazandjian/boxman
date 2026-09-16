# Workstation-side driver for the two-host Proxmox lab. Every target is
# "ssh <host> && run a script from this box" — the hosts do the work, this
# file only sequences it. `make help` lists the targets; `make all` runs the
# happy path end to end (~45-90 min, mostly unattended installs).
#
#   HOSTS               physical hosts (ssh aliases) — host1 must be first: it
#                       runs the DHCP/NAT side and the orchestration scripts
#   LAB                 lab directory on the hosts
#   PVE_ROOT_PASSWORD   root password baked into the ISO (hashed)
#   TARGET / BACK       migration target node, and whether to migrate back
SHELL := /bin/bash
HOSTS ?= host1 host2
ORCH ?= host1
# The sites are named host1/host2 throughout the box. `ssh host1` still has to
# resolve, so each site maps to an ssh alias -- defaulting to this lab's, and
# Which machine is `host1`/`host2`. Unset means the site name is used as the
# ssh host, which is also what scripts/ssh-site does on its own -- the two must
# agree, or `make` and a copy-pasted command from the README reach DIFFERENT
# machines. Defaulting these to particular hostnames here would re-introduce
# exactly what naming the sites host1/host2 was meant to remove.
#   export SSH_ALIAS_host1=myhost   (or: make all SSH_ALIAS_host1=myhost)
SSH_ALIAS_host1 ?=
SSH_ALIAS_host2 ?=
# exported, because the lookup below happens in the recipe's shell (the site is
# a shell loop variable there, so make cannot resolve it at expansion time)
export SSH_ALIAS_host1
export SSH_ALIAS_host2
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
# Resolves host1/host2 to their ssh aliases (see scripts/ssh-site); every
# recipe that reaches a host goes through it, so the mapping lives in one
# place rather than at a dozen call sites.
SSH := $(BOX_DIR)/scripts/ssh-site
# Being git-ignored does not make rsync skip a file: without these the API
# token, the Terraform state that owns the running VMs, and terraform.tfvars
# were all copied to both physical hosts (#171 B14).
RSYNC_EXCLUDES := --exclude .git --exclude .venv --exclude .boxman --exclude __pycache__ \
	--exclude containers/docker/data --exclude dist --exclude .pytest_cache \
	--exclude .claude --exclude '*.rendered.yml' \
	--exclude '$(BOX_REL)/terraform/.env' \
	--exclude '$(BOX_REL)/terraform/.terraform' \
	--exclude '$(BOX_REL)/terraform/*.tfstate' \
	--exclude '$(BOX_REL)/terraform/*.tfstate.*' \
	--exclude '$(BOX_REL)/terraform/*.tfplan' \
	--exclude '$(BOX_REL)/terraform/terraform.tfvars'

# $(call run,<host>,<script>[ args])
# Double-quoted, like the boxman macro below and for the same reason: a site
# used from a shell loop must be expanded here, not handed to the remote shell
# as a literal `$h`.
#
# BOXMAN_SITE is passed because lib.sh otherwise falls back to the short
# hostname, which worked only while the machines happened to be named exactly
# like the sites. PVE_LAB_DIR is forwarded because lib.sh defaults LAB to
# ~/pve-lab on its own; without it a LAB= override changed only the
# workstation's idea of the paths (#171 B16).
define run
$(SSH) $(1) "BOXMAN_SITE=$(1) PVE_LAB_DIR=$(LAB) bash $(SCRIPTS)/$(2)"
endef
# $(call boxman,<host>,<verb and flags>) — double quotes so a loop variable
# used as <host> is expanded by the local shell, not handed to the remote one
define boxman
$(SSH) $(1) "cd $(REMOTE_BOX) && BOXMAN_SITE=$(1) PVE_LAB_DIR=$(LAB) $(BOXMAN) $(BOXMAN_FLAGS) $(2)"
endef

# `all`'s phases are sibling prerequisites with no dependency chain between
# them, so `make -j` -- or a parallel MAKEFLAGS inherited from a caller --
# would happily build the ISO before the sync, or boot before an install
# finished. Every target here drives ssh sequences, so nothing is gained by
# running them in parallel anyway (#171 B17).
.NOTPARALLEL:

.PHONY: help keys answer sync host-prep iso up wait-installed boot wait-first-boot \
	cluster ceph demo migrate mtu-check status ps down destroy host-clean all FORCE

help:
	@grep -E '^[a-z-]+:.*#@' $(MAKEFILE_LIST) | sed -E 's/^([a-z-]+):.*#@ ?/  \1\t/' | expand -t 18

keys: keys/id_ed25519_pvelab   #@ generate the lab ssh keypair (once)
keys/id_ed25519_pvelab:
	mkdir -p keys && ssh-keygen -q -t ed25519 -N "" -C pve-lab -f $@

# Always re-rendered, never date-compared. The password is baked into
# answer.toml and no file timestamp records it, so a timestamp rule made
# `make answer PVE_ROOT_PASSWORD=...` a no-op and the rebuilt ISO still
# installed the documented default. Recording the value to compare against
# would mean writing the plaintext root password to disk, where .gitignore
# does not stop rsync from shipping it to both hosts -- the same trap B14 is
# about. answer.toml is a few hundred bytes and prepare-iso.sh rebuilds the
# auto ISO unconditionally, so re-rendering every time costs nothing (#171 B15).
answer: keys   #@ render answer.toml from the template (pubkey + password hash)
	@hash=$$(openssl passwd -6 '$(PVE_ROOT_PASSWORD)'); pub=$$(cat keys/id_ed25519_pvelab.pub); \
	sed -e "s|__PWHASH__|$$hash|" -e "s|__PUBKEY__|$$pub|" answer.toml.tmpl > answer.toml \
	  && echo "rendered answer.toml"

sync: keys   #@ rsync this repo + keys to the hosts, build the venv, install boxman
	@# The alias is resolved FIRST and every step fails immediately. It used to
	@# sit after an `&&` and be followed by `;`, so a failed mkdir on the second
	@# host skipped the assignment but not the rsyncs: `a` still held the FIRST
	@# host's alias, both transfers went there again, and sync reported success.
	@for h in $(HOSTS); do \
	  echo "== $$h"; \
	  a=$$(eval printf '%s' \"\$$SSH_ALIAS_$$h\"); a=$${a:-$$h}; \
	  $(SSH) $$h 'mkdir -p $(LAB)/src $(LAB)/keys' || exit 1; \
	  rsync -a --delete $(RSYNC_EXCLUDES) $(REPO)/ $$a:$(LAB)/src/ || exit 1; \
	  rsync -a keys/ $$a:$(LAB)/keys/ || exit 1; \
	  $(SSH) $$h 'chmod 600 $(LAB)/keys/id_ed25519_pvelab; \
	    test -x $(LAB)/venv/bin/pip || python3.12 -m venv $(LAB)/venv; \
	    $(LAB)/venv/bin/pip install -q -e $(LAB)/src && $(BOXMAN) --version' || exit 1; \
	done

host-prep:   #@ host checks + lab directories on every host
	@for h in $(HOSTS); do echo "== $$h"; $(call run,$$h,host-prep.sh) || exit 1; done

iso:   #@ download + bake the auto-install ISO on every host (parallel, ~5 min)
	@# A bare `wait` returns 0 whatever the children did, so a failed ISO build
	@# used to be reported only as three quiet lines of log tail (#171 B5).
	@pids=(); hosts=(); rc=0; \
	for h in $(HOSTS); do $(SSH) $$h 'mkdir -p $(LAB)/iso && touch $(LAB)/.iso-build-marker' || exit 1; done; \
	for h in $(HOSTS); do \
	  $(call run,$$h,prepare-iso.sh) > /tmp/pve-iso-$$h.log 2>&1 & \
	  pids+=($$!); hosts+=("$$h"); \
	done; \
	for i in $${!pids[@]}; do \
	  h=$${hosts[$$i]}; \
	  if wait $${pids[$$i]}; then echo "== $$h"; else echo "== $$h  BUILD FAILED"; rc=1; fi; \
	  tail -3 /tmp/pve-iso-$$h.log; \
	done; \
	exit $$rc
	@# ...and an existing file is not evidence of a successful rebuild: compare
	@# against a marker touched on the same host, so no clock skew is involved
	@# (host2 runs a few minutes behind host1) (#171 B6).
	@for h in $(HOSTS); do \
	  $(SSH) $$h 'test $(LAB)/iso/proxmox-ve_9.2-1-auto.iso -nt $(LAB)/.iso-build-marker' \
	    || { echo "ISO on $$h was not rebuilt; a stale one is still in place"; exit 1; }; \
	done

up:   #@ host2 vxlan -> host1 boxman up -> host1 vxlan -> host2 boxman up (installs start)
	$(call run,host2,vxlan-up.sh)
	$(call boxman,host1,up)
	$(call run,host1,vxlan-up.sh)
	$(call boxman,host2,up)

wait-installed:   #@ wait until every node powered off after its unattended install
	@# a timed-out installer wait must stop `all`, not let it advance to boot
	@pids=(); hosts=(); rc=0; \
	for h in $(HOSTS); do $(call run,$$h,wait-installed.sh) & pids+=($$!); hosts+=("$$h"); done; \
	for i in $${!pids[@]}; do \
	  wait $${pids[$$i]} || { echo "install wait failed on $${hosts[$$i]}"; rc=1; }; \
	done; \
	exit $$rc

boot:   #@ start the installed nodes from disk (boxman up #2) and re-check the vxlan
	@pids=(); hosts=(); rc=0; \
	for h in $(HOSTS); do $(call boxman,$$h,up) & pids+=($$!); hosts+=("$$h"); done; \
	for i in $${!pids[@]}; do \
	  wait $${pids[$$i]} || { echo "boxman up failed on $${hosts[$$i]}"; rc=1; }; \
	done; \
	exit $$rc
	@rc=0; for h in $(HOSTS); do $(call run,$$h,vxlan-up.sh) || rc=1; done; exit $$rc

wait-first-boot:   #@ wait for the first-boot hook (repos, MTU 1450, guest agent) on all nodes
	$(call run,$(ORCH),wait-first-boot.sh)

cluster:   #@ pvecm create on pve1, pvecm add pve2..pve4
	$(call run,$(ORCH),pve-cluster.sh)

ceph:   #@ pveceph install/init, 3 mons, 2 mgrs, 8 OSDs, pool vmpool
	$(call run,$(ORCH),pve-ceph.sh)

demo:   #@ nested Ubuntu VM demo01 on pve1, disk on vmpool, DHCP from host1
	$(call run,$(ORCH),pve-demo-vm.sh)

migrate:   #@ live-migrate demo01 to TARGET (default pve3 on host2); BACK=1 returns it to pve1
	$(call run,$(ORCH),pve-migrate.sh $(TARGET))
	@if [ -n "$(BACK)" ]; then $(call run,$(ORCH),pve-migrate.sh pve1); fi

mtu-check:   #@ verify the 1450-byte path MTU across the vxlan
	$(call run,$(ORCH),mtu-check.sh)

status:   #@ boxman ps on both hosts, then bridge/cluster/ceph/vm picture from host1
	@rc=0; for h in $(HOSTS); do echo "== $$h"; $(call boxman,$$h,ps) || rc=1; done; exit $$rc
	$(call run,$(ORCH),status.sh)

down:   #@ boxman down (save) on both hosts
	@rc=0; for h in $(HOSTS); do $(call boxman,$$h,down) || rc=1; done; exit $$rc

destroy:   #@ boxman destroy -y on both hosts (nodes, disks, host1's nat bridge); keys re-synced after
	@rc=0; for h in $(HOSTS); do $(call boxman,$$h,destroy -y) || rc=1; done; exit $$rc
	@# boxman removes the admin key as a generated artifact; put the lab key back
	@rc=0; for h in $(HOSTS); do \
	  a=$$(eval printf '%s' \"\$$SSH_ALIAS_$$h\"); a=$${a:-$$h}; \
	  rsync -a keys/ $$a:$(LAB)/keys/ && $(SSH) $$h 'chmod 600 $(LAB)/keys/id_ed25519_pvelab' || rc=1; \
	done; exit $$rc

host-clean: #@ remove the vxlan, host2's bridge and the firewall rule (after destroy)
	@rc=0; for h in $(HOSTS); do $(call run,$$h,host-clean.sh) || rc=1; done; exit $$rc

all: keys answer sync host-prep iso up wait-installed boot wait-first-boot mtu-check cluster ceph demo migrate   #@ everything, in order

# ---- Terraform on top of the cluster (runs HERE, through an ssh tunnel) ----
# The provider talks to the API only (image download by the node, import_from,
# built-in cloud-init), so no ssh access from the workstation to the nodes is
# needed. Open the tunnel first:
#   ssh -N -L 8006:10.77.0.11:8006 "${SSH_ALIAS_host1:-host1}"
TF_DIR := $(BOX_DIR)/terraform
TF := terraform -chdir=$(TF_DIR)
# VM_COUNT only overrides when given (make tf-apply VM_COUNT=8); otherwise
# terraform.tfvars (local, gitignored) or the variable default applies
TF_VARS = $(if $(VM_COUNT),-var vm_count=$(VM_COUNT),)

# pipefail makes the pipeline's status visible, but the status of the
# *assignment* was never checked either: ssh emitting a token and then exiting
# non-zero was accepted and the existing .env replaced (#171 B7). PVE_LAB_DIR is
# forwarded because this sources lib.sh directly (#171 B16).
tf-token:   #@ create the API token root@pam!terraform on pve1 and write terraform/.env
	@# Fails closed. The old recipe ended in an unconditional `echo`, which
	@# made the whole line succeed however the pipeline went, and its `exit 1`
	@# for an empty secret fired inside a pipeline subshell where nothing saw
	@# it (#171 B7). The token is root@pam with --privsep 0, so it is written
	@# under umask 077 via a temp file and renamed, which also repairs an
	@# existing 0644 .env rather than leaving its mode alone (#171 B8).
	@set -o pipefail; umask 077; \
	if ! secret=$$($(SSH) $(ORCH) 'BOXMAN_SITE=$(ORCH) PVE_LAB_DIR=$(LAB) source $(SCRIPTS)/lib.sh; pssh pve1 "pveum user token list root@pam --output-format json | grep -q \"\\\"terraform\\\"\" && pveum user token remove root@pam terraform >/dev/null; pveum user token add root@pam terraform --privsep 0 --comment lab-terraform --output-format json"' \
	  | sed -n 's/.*"value":"\([^"]*\)".*/\1/p'); then \
	  echo "token creation failed on $(ORCH); nothing written, existing credentials left alone"; \
	  exit 1; \
	fi; \
	test -n "$$secret" || { echo "no token returned by pve1; nothing written"; exit 1; }; \
	tmp=$$(mktemp $(TF_DIR)/.env.XXXXXX) || exit 1; \
	printf '# generated by make tf-token\nexport TF_VAR_pve_api_token=%s\nexport TF_VAR_pve_endpoint=%s\n' \
	  "'root@pam!terraform=$$secret'" "'https://localhost:8006/'" > "$$tmp" || { rm -f "$$tmp"; exit 1; }; \
	chmod 600 "$$tmp" && mv -f "$$tmp" $(TF_DIR)/.env || { rm -f "$$tmp"; exit 1; }; \
	echo "wrote $(TF_DIR)/.env (mode 0600)"

tf-init:   #@ terraform init
	$(TF) init -input=false

# `set -a; . .env; set +a; terraform` is one shell line: a missing or
# unreadable .env only made `.` fail, and Terraform still ran against whatever
# TF_VAR_* the ambient environment happened to hold (#171 B9).
# `test -r` proves the file can be opened, not that it parsed: an unterminated
# quote, or a file whose last command fails, left Terraform running against
# whatever TF_VAR_* the ambient environment held (#171 B9).
TF_ENV = test -r $(TF_DIR)/.env || { echo "$(TF_DIR)/.env is missing or unreadable - run 'make tf-token'"; exit 1; }; \
	set -a; . $(TF_DIR)/.env || { echo "$(TF_DIR)/.env could not be sourced; refusing to run terraform"; exit 1; }; set +a

tf-plan:   #@ terraform plan (VM_COUNT=n)
	@$(TF_ENV); $(TF) plan -input=false $(TF_VARS)

tf-apply:   #@ terraform apply: VM_COUNT Rocky 9 VMs round-robin over the nodes, disks on vmpool
	@$(TF_ENV); $(TF) apply -input=false -auto-approve $(TF_VARS)

tf-output:   #@ show the VMs' nodes and addresses
	@$(TF) output vms; $(TF) output ssh

tf-destroy:   #@ terraform destroy (VMs and the downloaded images)
	@$(TF_ENV); $(TF) destroy -input=false -auto-approve $(TF_VARS)

# ---- HA placement policy (Proxmox owns *where* the HA VMs run) ------------
NODE ?= pve4

# The placement policy comes from Terraform's *configured* placement, handed
# to the script rather than re-derived from vm ids. There is no fallback: the
# arithmetic it replaced silently disagreed with `node_overrides` (#171 D1).
ha:   #@ dynamic CRS + auto-rebalance + node-affinity rules for the HA resources (after tf-apply)
	@command -v jq >/dev/null || { echo "jq is required on the workstation for 'make ha'"; exit 1; }; \
	set -o pipefail; \
	$(TF_ENV); \
	homes=$$($(TF) output -json ha_policy_homes \
	  | jq -r 'to_entries | map("\(.key)=\(.value)") | join(",")') \
	  || { echo "could not read the ha_policy_homes output; run 'make tf-apply' first"; exit 1; }; \
	test -n "$$homes" || { echo "ha_policy_homes is empty; run 'make tf-apply' first"; exit 1; }; \
	$(SSH) $(ORCH) "BOXMAN_SITE=$(ORCH) PVE_LAB_DIR=$(LAB) PVE_HA_POLICY_HOMES='$$homes' bash $(SCRIPTS)/pve-ha.sh"

ha-status:   #@ ha-manager status, rules, crs settings
	@$(SSH) $(ORCH) 'BOXMAN_SITE=$(ORCH) PVE_LAB_DIR=$(LAB) source $(SCRIPTS)/lib.sh; pssh pve1 "ha-manager status; echo; ha-manager rules list; echo; pvesh get /cluster/options --output-format json | grep -o \"\\\"crs\\\":\\\"[^\\\"]*\\\"\""'

# The victim sids are collapsed to one space-separated line before they cross
# the remote shell: they used to arrive with their newlines intact, so every sid
# after the first was run as a command of its own (#171 B13). ha-watch.sh checks
# each sid's shape on arrival. The readiness status is captured explicitly
# because `$${restore_rc:-1}` substitutes only for unset or empty values, so a
# failure with restore_rc already 0 was swallowed (#171 B12).
ha-failover:   #@ hard-kill NODE (default pve4) on its host, watch HA recover its VMs, bring the node back with boxman
	@# The drill's own result used to be lost twice over: behind the final
	@# readiness check, and behind a `boxman up | grep` pipeline that reports
	@# grep's status rather than boxman's (#171 B12). The victim inventory is
	@# now taken BEFORE the kill, because HA starts moving guests immediately
	@# and an inventory taken afterwards can miss the ones that already
	@# relocated, then call the drill complete (#171 B13).
	@set -o pipefail; \
	site=$$($(SSH) $(ORCH) 'BOXMAN_SITE=$(ORCH) PVE_LAB_DIR=$(LAB) source $(SCRIPTS)/lib.sh; echo $${NODE_SITE[$(NODE)]}') || exit 1; \
	victims=$$($(SSH) $(ORCH) 'BOXMAN_SITE=$(ORCH) PVE_LAB_DIR=$(LAB) bash $(SCRIPTS)/ha-watch.sh $(NODE) --list' | tr '\n' ' ') || exit 1; \
	echo "== HA resources on $(NODE) before the kill: $${victims:-none}"; \
	echo "== pulling the plug on $(NODE) (host $$site)"; \
	$(SSH) $$site 'virsh -c qemu:///system destroy bprj__pvelab__bprj_pve_$(NODE)' || exit 1; \
	watch_rc=0; \
	$(SSH) $(ORCH) "BOXMAN_SITE=$(ORCH) PVE_LAB_DIR=$(LAB) bash $(SCRIPTS)/ha-watch.sh $(NODE) 600 $$victims" || watch_rc=$$?; \
	echo "== bringing $(NODE) back with boxman up on $$site"; \
	restore_rc=0; \
	$(call boxman,$$site,up) > /tmp/pve-failover-up.log 2>&1 || restore_rc=$$?; \
	grep -E "starting VM|infrastructure" /tmp/pve-failover-up.log || true; \
	ready_rc=0; \
	$(call run,$(ORCH),wait-first-boot.sh $(NODE)) || ready_rc=$$?; \
	if [ $$restore_rc -eq 0 ]; then restore_rc=$$ready_rc; fi; \
	if [ $$watch_rc -ne 0 ]; then \
	  echo "== failover drill FAILED (exit $$watch_rc); the node was restored anyway"; \
	  exit $$watch_rc; \
	fi; \
	exit $$restore_rc
