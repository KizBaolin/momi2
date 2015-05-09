#!/usr/bin/env python

from distutils.core import setup

setup(name='momi',
      version='0.1',
      description='MOran Model for Inference',
      author='Jack Kamm, Jonathan Terhorst, Yun S. Song',
      author_email='jkamm@stat.berkeley.edu, terhorst@stat.berkeley.edu, yss@eecs.berkeley.edu',
      packages=['momi'],
      install_requires=['numpy>=1.8','networkx','autograd>=1.02'],
      keywords=['population genetics','statistics','site frequency spectrum','coalescent'],
      url='https://github.com/jackkamm/momi',
      )
