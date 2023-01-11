#!/usr/bin/env python

import re
from pathlib import Path
from setuptools import setup

HERE = Path(__file__).parent

with open(HERE / 'pleroma.py') as f:
	VERSION = re.search(r'^__version__\s*=\s*[\'"]([^\'"]*)[\'"]', f.read(), re.MULTILINE).group(1)

if not VERSION:
	raise RuntimeError('version is not set')

with open(HERE / 'README.md') as f:
	README = f.read()

setup(
	name='pleroma.py',
	author='io',
	url='https://github.com/ioistired/pleroma.py',
	version=VERSION,
	py_modules=['pleroma'],
	license='AGPL-3.0-only',
	long_description=README,
	long_description_content_type='text/markdown; variant=GFM',
	install_requires=[
		'aiohttp ~= 3.0',
		'anyio ~= 3.0',
		'python-dateutil ~= 2.8',
	],
	classifiers=[
		'Development Status :: 3 - Alpha',
		'Intended Audience :: Developers',
		'Natural Language :: English',
		'Operating System :: OS Independent',
	],
)
