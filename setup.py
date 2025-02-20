# Copyright 2009-2015 Yelp and Contributors
# Copyright 2016-2017 Yelp
# Copyright 2018 Google Inc.
# Copyright 2019 Yelp
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import sys

import mrjob

try:
    from setuptools import setup
    setup  # quiet "redefinition of unused ..." warning from pyflakes
    # arguments that distutils doesn't understand
    setuptools_kwargs = {
        'extras_require': {
            'ujson': ['ujson'],
        },
        'install_requires': [
            'boto3>=1.4.6',
            'botocore>=1.6.0',
            'PyYAML>=3.10',
        ],
        'provides': ['mrjob'],
        'test_suite': 'tests',
        'tests_require': [
            'pyspark',
            'simplejson',
            'ujson',
            'warcio',
        ],
        'zip_safe': False,  # so that we can bootstrap mrjob
    }

    # Google libs don't install on Python 3.4. Which is fine, the only
    # reason we support Python 3.4 at all is to support earlier
    # AMIs on EMR. See #2090
    if sys.version_info[0] == 2 or sys.version_info >= (3, 5):
        setuptools_kwargs['install_requires'].extend([
            'google-cloud-dataproc>=0.3.0',
            'google-cloud-logging>=1.9.0',
            'google-cloud-storage>=1.13.1',
        ])

        # grpcio 1.11.0 and 1.12.0 seem not to compile with PyPy
        if hasattr(sys, 'pypy_version_info'):
            setuptools_kwargs['install_requires'].append('grpcio<=1.10.0')

    # rapidjson exists on Python 3 only
    if sys.version_info >= (3, 0):
        setuptools_kwargs['extras_require']['rapidjson'] = ['rapidjson']
        setuptools_kwargs['tests_require'].append('rapidjson')

except ImportError:
    from distutils.core import setup
    setuptools_kwargs = {}

with open('README.rst') as f:
    long_description = f.read()

setup(
    author='David Marin',
    author_email='dm@davidmarin.org',
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: Apache Software License',
        'Natural Language :: English',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Topic :: System :: Distributed Computing',
    ],
    description='Python MapReduce framework',
    entry_points=dict(
        console_scripts=[
            'mrjob=mrjob.cmd:main',
            'mrjob-%d=mrjob.cmd:main' % sys.version_info[:1],
            'mrjob-%d.%d=mrjob.cmd:main' % sys.version_info[:2],
        ]
    ),
    license='Apache',
    long_description=long_description,
    name='mrjob',
    packages=[
        'mrjob',
        'mrjob.examples',
        'mrjob.examples.mr_postfix_bounce',
        'mrjob.examples.mr_travelling_salesman',
        'mrjob.fs',
        'mrjob.logs',
        'mrjob.spark',
        'mrjob.tools',
        'mrjob.tools.emr',
    ],
    package_data={
        'mrjob': ['bootstrap/*.sh'],
        'mrjob.examples': ['*.txt', '*.jar', '*.rb'],
        'mrjob.examples.mr_postfix_bounce': ['*.json'],
        'mrjob.examples.mr_travelling_salesman': ['example_graphs/*.json'],
    },
    url='http://github.com/Yelp/mrjob',
    version=mrjob.__version__,
    **setuptools_kwargs
)
