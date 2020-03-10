import re
import os
from setuptools import find_packages, setup


def get_version():
    version_file = open(os.path.join(
        os.path.dirname(__file__), 'nameko_odoo', '__init__.py')).read()
    version_match = re.search(r"^__version__ = ['\"]([^'\"]*)['\"]",
                              version_file, re.M)
    if version_match:
        return version_match.group(1)
    raise RuntimeError("Unable to find version string.")


setup(
    name='nameko-odoo',
    version=get_version(),
    description=(
        'Nameko Odoo extension'
    ),
    author='Max Lit',
    url='http://github.com/litnimax/nameko-odoo',
    packages=find_packages(exclude=['test', 'test.*']),
    install_requires=[
        "nameko>=2.8.5",
        "odoorpc>=0.7.0",
    ],
    extras_require={
        'dev': [
            "coverage",
            "flake8",
            "pylint",
            "pytest",
            "requests-mock",
        ]
    },
    dependency_links=[],
    zip_safe=True,
    license='GNU LESSER GENERAL PUBLIC LICENSE V3',
    classifiers=[
        "Programming Language :: Python",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: POSIX",
        "Programming Language :: Python :: 3.5",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Topic :: Internet",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Intended Audience :: Developers",
    ]
)
