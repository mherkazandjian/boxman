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
