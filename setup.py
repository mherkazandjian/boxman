import os
import sys

from setuptools import setup

sys.path.append('src')
from boxman import metadata


setup(
    name=metadata.package,
    version=metadata.version,
    description=metadata.description,
    author=metadata.authors,
    author_email=metadata.emails,
    url=metadata.url,
    packages=[
        'boxman',
        'boxman.abstract',
        'boxman.loggers',
        'boxman.scripts',
        'boxman.providers',
        'boxman.providers.libvirt',
        'boxman.utils',
        'boxman.virtualbox',
        'boxman.assets'
    ],
    package_dir={
        'boxman': os.path.join('src', 'boxman')
    },
    package_data={
        'boxman': ['assets/*', 'assets/**/*'],
    },
    include_package_data=True,
    entry_points={
        'console_scripts': [
            'boxman=boxman.scripts.app:main',
            'boxman-import-vm=boxman.scripts.import_vm:main',
        ],
    }
)
