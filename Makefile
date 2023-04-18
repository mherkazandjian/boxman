.PHONY: nothing
nothing:

clean:
	@rm -fvr build boxman.egg-info dist
	@find . -type d -name '__pycache__' -exec rm -fvr '{}' \;
	@find . -type f -name '__pycache__' -exec rm -fv '{}' \;

build:
	@python setup.py build

install:
	@python setup.py install

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