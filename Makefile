include common.mk

nothing:

################
#@group: \033[0;32mbuild\033[0m
#@help: remove build artifacts and python caches
clean:
	@rm -fvr build boxman.egg-info dist || true
	@find . -type d -name '__pycache__' -exec rm -fvr '{}' \; || true
	@find . -type f -name '__pycache__' -exec rm -fv '{}' \; || true

#@help: uninstall boxman package
uninstall:
	@pip uninstall -y boxman || true

#@help: build the package with poetry
build:
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

# .. todo:: this does not work as expected, since the bash env vars are not preserved. We need to source the env vars in the Makefile or use a wrapper script.
# .. todo:: just a placeholder to be fixed later
#@help: build the documentation (placeholder)
docs:
	@docker run -it --rm --user $(id -u):$(id -g) --workdir="/home/${USER}" \
		--volume="/etc/group:/etc/group:ro" --volume="/etc/passwd:/etc/passwd:ro" \
		--volume="/etc/shadow:/etc/shadow:ro" -v $PWD:/work texlive/texlive:latest -c "cd /work/docs/tutorial && pdflatex boxman_beamer.tex"

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

################
#@help: show this help message
help:
	@echo "Available targets:"
	@awk "$$AWK_SCRIPT" $(MAKEFILE_LIST)
