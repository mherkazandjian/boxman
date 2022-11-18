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
	@echo export PYTHONPATH=${PWD}/src:${PYTHONPATH}
	@echo python app.py
	@echo python app.py --conf conf.yml