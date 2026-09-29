from setuptools import find_packages
from setuptools import setup

setup(
    name='vrx_gazebo',
    version='1.3.0',
    packages=find_packages(
        include=('vrx_gazebo', 'vrx_gazebo.*')),
)
