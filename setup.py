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
        'boxman.loggers',
        'boxman.scripts',
        'boxman.virtualbox'
    ],
    package_dir={
        'boxman': os.path.join('src', 'boxman')
    },
    entry_points={
        'console_scripts': [
            'boxman=boxman.scripts.app:main',
        ],
    }
)
