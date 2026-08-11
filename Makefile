include common.mk

.PHONY: clean dev-clean uninstall build install cleaninstall full-reinstall \
	devipython devshell test test-integration test-provision test-dc-e2e \
	test-vm-up test-vm-sync test-vm-test test-vm-destroy loc loc-detailed \
	boxes-deprovision boxes-clean help

################
#@group: \033[0;32mbuild\033[0m
#@help: remove build artifacts and python caches
clean:
	@rm -fvr build boxman.egg-info dist || true
	@find . -type d -name '__pycache__' -exec rm -fvr '{}' \; || true
	@find . -type f -name '__pycache__' -exec rm -fv '{}' \; || true

#@help: remove .boxman directories (prompts before deleting)
dev-clean:
	@dirs=$$(find . -type d -name '.boxman' 2>/dev/null); \
	if [ -z "$$dirs" ]; then \
		echo "No .boxman directories found."; \
	else \
		echo "Found .boxman directories:"; \
		echo "$$dirs"; \
		echo ""; \
		printf "Delete all? [y/N] "; \
		read ans; \
		if [ "$$ans" = "y" ] || [ "$$ans" = "Y" ]; then \
			echo "$$dirs" | xargs sudo rm -rfv; \
		else \
			echo "Aborted."; \
		fi; \
	fi

#@help: uninstall boxman package
uninstall:
	@pip uninstall -y boxman || true

#@help: build the package with poetry
build:
	@pip install poetry
	@poetry build
	@python3 scripts/repackage_wheel.py dist/*.whl

#@help: build and install the package
install: build
	@pip install --force-reinstall dist/*.whl
#	@poetry install

#@help: clean then install
cleaninstall:
	@$(MAKE) clean
	@$(MAKE) install

#@help: clean, uninstall, lock, and reinstall
full-reinstall:
	@$(MAKE) clean
	@$(MAKE) uninstall
	@poetry lock
	@$(MAKE) install

################
#@group: \033[0;32mdevelopment\033[0m
#@help: launch ipython in data/dev
devipython:
	@cd data/dev && poetry run ipython

#@help: launch a shell in data/dev
devshell:
	@cd data/dev && poetry run bash

################
#@group: \033[0;32mtesting\033[0m
PYTEST_FLAGS ?=
ifeq ($(verbose),1)
PYTEST_FLAGS += -v
endif
ifdef pytest
PYTEST_FLAGS += $(pytest)
endif

#@help: run all tests (verbose=1, pytest_args="..." for extra flags)
test:
	PYTHONPATH=src:$(PYTHONPATH) python -m pytest $(PYTEST_FLAGS) $(pytest_args) tests/

#@help: run docker-compose integration tests (verbose=1 for verbose output)
test-integration:
	PYTHONPATH=src:$(PYTHONPATH) python -m pytest $(PYTEST_FLAGS) $(pytest_args) -m integration tests/test_docker_compose.py

#@help: run box provisioning integration tests (verbose=1, pytest_args="..." for extra flags)
test-provision:
	PYTHONPATH=src:$(PYTHONPATH) python -m pytest $(PYTEST_FLAGS) $(pytest_args) -m integration tests/test_provision_boxes.py

#@help: run docker-compose *provider* e2e tests (needs docker; hybrid tier also needs /dev/kvm)
test-dc-e2e:
	PYTHONPATH=src:$(PYTHONPATH) python -m pytest $(PYTEST_FLAGS) $(pytest_args) -m integration tests/test_docker_compose_provider_e2e.py

# --- disposable test-runner VM ---------------------------------------------
# All test tiers (unit, smoke, integration) run inside a dedicated disposable
# VM (data/dev/test-runner, 2 vCPU / 16 GB) so test side effects — docker
# networks, libvirt domains, image downloads — never touch the host.
# Requires: nested KVM enabled on the host, libvirt + docker in the VM.
TEST_VM_CONF    := data/dev/test-runner/conf.yml
TEST_VM_WS      := $(HOME)/workspaces/boxmandev/test-runner
TEST_VM_DOMAIN  := bprj__boxman_dev_test_runner__bprj_cluster_1_runner01
TEST_VM_SSH     := ssh -F $(TEST_VM_WS)/ssh_config cluster_1_runner01

#@help: provision the disposable test-runner VM (2 vCPU / 16 GB, docker + nested kvm)
test-vm-up:
	PYTHONPATH=src:$(PYTHONPATH) python src/boxman/scripts/app.py --conf $(TEST_VM_CONF) provision
	# nested virtualization: boxman already emits cpu mode='host-passthrough',
	# so /dev/kvm is available inside the VM (verify: make test-vm-check)
	$(TEST_VM_SSH) 'ls /dev/kvm && docker --version'

#@help: rsync the repo into the test-runner VM and create its venv
test-vm-sync:
	$(TEST_VM_SSH) 'mkdir -p ~/boxman && python3 -m venv ~/boxman/.venv'
	rsync -a --delete -e "ssh -F $(TEST_VM_WS)/ssh_config" \
		--exclude .git --exclude .venv --exclude .boxman --exclude __pycache__ \
		--exclude containers/docker/data --exclude dist --exclude .pytest_cache \
		./ cluster_1_runner01:~/boxman/
	$(TEST_VM_SSH) 'cd ~/boxman && .venv/bin/pip install -q -e ".[docker-compose]" pytest'

#@help: run tests inside the test-runner VM (tier=integration for the Docker/KVM tier)
# NOTE: the venv must be on PATH — e2e tests shell out to bare `python3`/`boxman`.
test-vm-test:
	$(TEST_VM_SSH) 'cd ~/boxman && PATH=$$HOME/boxman/.venv/bin:$$PATH PYTHONPATH=src \
		.venv/bin/python -m pytest tests/ -q \
		$(if $(filter integration,$(tier)),-m integration -o addopts=,) $(pytest_args)'

#@help: destroy the disposable test-runner VM and its workspace
test-vm-destroy:
	PYTHONPATH=src:$(PYTHONPATH) python src/boxman/scripts/app.py --conf $(TEST_VM_CONF) destroy -y

#@help: count lines of code per category (code/tests/docs/conf/templates/boxes/shell/docker/make/claude)
loc:
	@if ! command -v docker >/dev/null 2>&1; then \
		echo "docker is required for 'make loc' (cloc runs via the aldanial/cloc image)."; \
		exit 1; \
	fi
	@docker run --rm -v "$$PWD:/work" -w /work aldanial/cloc $$(git ls-files)

#@help: count lines of code per category (custom counter: code/tests/docs/...)
loc-detailed:
	@python3 scripts/count_loc.py

################
#@group: \033[0;32mboxes\033[0m
#@help: deprovision all boxes that have a conf.yml (also cleans .boxman dirs)
boxes-deprovision:
	@for conf in boxes/*/conf.yml; do \
		dir=$$(dirname "$$conf"); \
		echo "==> Deprovisioning $$dir"; \
		boxman --conf "$$conf" deprovision || true; \
	done
	@$(MAKE) boxes-clean

#@help: remove .boxman/ directories under boxes/ (uses alpine via docker for root-owned leftovers)
boxes-clean:
	@for bdir in boxes/*/.boxman; do \
		[ -d "$$bdir" ] || continue; \
		echo "==> Cleaning $$bdir"; \
		rm -rf "$$bdir" 2>/dev/null || true; \
		if [ -d "$$bdir" ]; then \
			abs=$$(cd "$$(dirname "$$bdir")" && pwd)/.boxman; \
			echo "    root-owned leftovers — removing via docker alpine"; \
			docker run --rm -v "$$abs:/cleanup" alpine sh -c 'rm -rf /cleanup/*' || true; \
			rm -rf "$$bdir" 2>/dev/null || true; \
		fi; \
	done

################
#@help: show this help message
help:
	@echo "Available targets:"
	@awk "$$AWK_SCRIPT" $(MAKEFILE_LIST)
