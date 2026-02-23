.PHONY: nothing
nothing:

clean:
	@rm -fvr build boxman.egg-info dist || true
	@find . -type d -name '__pycache__' -exec rm -fvr '{}' \; || true
	@find . -type f -name '__pycache__' -exec rm -fv '{}' \; || true

uninstall:
	@pip uninstall -y boxman || true

build:
	@poetry build

install: build
	@pip install --force-reinstall dist/*.whl
#	@poetry install

cleaninstall:
	@$(MAKE) clean
	@$(MAKE) install

full-reinstall:
	@$(MAKE) clean
	@$(MAKE) uninstall
	@poetry lock
	@$(MAKE) install

devipython:
	@cd data/dev && poetry run ipython

devshell:
	@cd data/dev && poetry run bash

help:
	@echo "For development"
	@echo "   explicit steps"
	@echo "     $ cd <ROOTDIR>"
	@echo "     $ export PYTHONPATH=${PWD}/src:${PYTHONPATH}"
	@echo "     $ cd data/dev"
	@echo "     $ cd minimal"
	@echo "     $ python ../../../src/boxman/scripts/app.py --help"
	@echo "     $ python ../../../src/boxman/scripts/app.py provision"
	@echo "     $ ssh -F ~/tmp/sandbox/minimal/ssh_config boxman01"
	@echo "   using make ( .. todo:: this does not work as expected, sinc the bash env vars are not preserved)"
	@echo "   	$ make devshell"
	@echo "   	$ cd minimal"

PYTEST_FLAGS ?=
ifeq ($(verbose),1)
PYTEST_FLAGS += -v
endif
ifdef pytest
PYTEST_FLAGS += $(pytest)
endif

test: ## Run all tests (verbose=1, pytest_args="..." for extra flags)
	PYTHONPATH=src:$(PYTHONPATH) python -m pytest $(PYTEST_FLAGS) $(pytest_args) tests/

test-integration: ## Run docker-compose integration tests (verbose=1 for verbose output)
	PYTHONPATH=src:$(PYTHONPATH) python -m pytest $(PYTEST_FLAGS) $(pytest_args) -m integration tests/test_docker_compose.py

ssh-runtime: ## SSH into the docker-compose runtime container
	@CONF_DIR=$$(pwd); \
	SSH_DIR="$$CONF_DIR/.boxman/runtime/docker/data/ssh"; \
	if [ -f "$$SSH_DIR/boxman.conf" ] && [ -f "$$SSH_DIR/id_ed25519" ]; then \
		ssh -F "$$SSH_DIR/boxman.conf" -i "$$SSH_DIR/id_ed25519" boxman-default; \
	else \
		echo "ERROR: runtime SSH config not found at $$SSH_DIR"; \
		echo "       run 'boxman --runtime docker-compose provision' first"; \
		exit 1; \
	fi
