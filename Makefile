.PHONY: nothing
nothing:

clean:
	@rm -fvr build boxman.egg-info dist || true
	@find . -type d -name '__pycache__' -exec rm -fvr '{}' \; || true
	@find . -type f -name '__pycache__' -exec rm -fv '{}' \; || true

build:
	@python setup.py build

install:
	@pip install .

cleaninstall:
	@$(MAKE) clean
	@$(MAKE) install

fullinstall:
	@$(MAKE) clean
	@pip uninstall boxman
	@$(MAKE) install
devipython:
	@cd data/dev && PYTHONPATH=${PWD}/src:${PYTHONPATH} ipython

devshell:
	@cd data/dev && PYTHONPATH=${PWD}/src:${PYTHONPATH} bash

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

test: ## Run all tests
	pytest $(PYTEST_FLAGS) tests/

test-integration: ## Run docker-compose integration tests (verbose=1 for verbose output)
	pytest $(PYTEST_FLAGS) -m integration tests/test_docker_compose.py
