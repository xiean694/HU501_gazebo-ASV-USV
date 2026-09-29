from setuptools import find_packages
from setuptools import setup

setup(
    name='vrx_gz',
    version='0.0.0',
    packages=find_packages(
        include=('vrx_gz', 'vrx_gz.*')),
)
