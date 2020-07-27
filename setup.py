# -*- coding: utf-8 -*-

# DO NOT EDIT THIS FILE!
# This file has been autogenerated by dephell <3
# https://github.com/dephell/dephell

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

readme = ''

setup(
    long_description=readme,
    name='brish',
    version='0.2.1',
    description='A bridge between zsh and Python.',
    python_requires='==3.*,>=3.7.0',
    author='NightMachinary',
    author_email='rudiwillalwaysloveyou@gmail.com',
    packages=['brish'],
    package_dir={"": "."},
    package_data={"brish": ["*.zsh"]},
    install_requires=[],
    extras_require={
        "dev": ["ipydex==0.*,>=0.10.5", "ipython>6", "pytest==5.*,>=5.2.0"]
    },
)
