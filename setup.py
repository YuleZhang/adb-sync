#!/usr/bin/env python3
"""Packaging for the independently released adb-sync tool."""

from setuptools import setup

VERSION = '1.0.0'

setup(
    name='adb-sync',
    version=VERSION,
    description='Checksum-aware file synchronization over Android Debug Bridge.',
    long_description=open('README.md', encoding='utf-8').read(),
    long_description_content_type='text/markdown',
    license='Apache-2.0',
    python_requires='>=3.7',
    scripts=['adb-sync', 'adb-channel'],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Console',
        'License :: OSI Approved :: Apache Software License',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3',
    ],
)
