# simple makefile for vuegraf

PYLIB = py_lib

# python dependencies we need
PYDEPS = pyyaml influxdb pyemvue click

all: stage

stage: build/$(PYLIB)
	@echo "*** copying files ***"
	cp src/vuegraf.py build
	cp vuegraf.json build
	chmod +x build/vuegraf.py

build/$(PYLIB):
	@echo "*** installing dependency packages ***"
	mkdir -p build
	pip3 install $(PYDEPS) -t build/$(PYLIB)

clean:
	rm -rf build

.PHONY: clean deepclean zip stage all
