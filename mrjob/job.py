# Copyright 2009-2017 Yelp and Contributors
# Copyright 2018 Yelp
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
"""Class to inherit your MapReduce jobs from. See :doc:`guides/writing-mrjobs`
for more information."""

# don't add imports here that aren't part of the standard Python library,
# since MRJobs need to run in Amazon's generic EMR environment
import codecs
import inspect
import itertools
import json
import logging
import os.path
import sys

# don't use relative imports, to allow this script to be invoked as __main__
from mrjob.cat import decompress
from mrjob.launch import MRJobLauncher
from mrjob.launch import _im_func
from mrjob.launch import _READ_ARGS_FROM_SYS_ARGV
from mrjob.options import _add_step_args
from mrjob.options import _print_help_for_steps
from mrjob.protocol import JSONProtocol
from mrjob.protocol import RawValueProtocol
from mrjob.py2 import integer_types
from mrjob.py2 import string_types
from mrjob.step import MRStep
from mrjob.step import SparkStep
from mrjob.step import _JOB_STEP_FUNC_PARAMS
from mrjob.util import expand_path
from mrjob.util import to_lines


log = logging.getLogger(__name__)


class UsageError(Exception):
    pass


class MRJob(MRJobLauncher):
    """The base class for all MapReduce jobs. See :py:meth:`__init__`
    for details."""

    # script path is whatever file our subclass of MRJob is in
    _FIRST_ARG_IS_SCRIPT_PATH = False

    def __init__(self, args=None):
        """Entry point for running your job from other Python code.

        You can pass in command-line arguments, and the job will act the same
        way it would if it were run from the command line. For example, to
        run your job on EMR::

            mr_job = MRYourJob(args=['-r', 'emr'])
            with mr_job.make_runner() as runner:
                ...

        Passing in ``None`` is the same as passing in ``[]`` (if you want
        to parse args from ``sys.argv``, call :py:meth:`MRJob.run`).

        For a full list of command-line arguments, run:
        ``python -m mrjob.job --help``
        """
        super(MRJob, self).__init__(self.mr_job_script(), args)

        self._warned_about_parse_output_line = False

    @classmethod
    def _usage(cls):
        return "%(prog)s [options] [input files]"

    ### Defining one-step streaming jobs ###

    def mapper(self, key, value):
        """Re-define this to define the mapper for a one-step job.

        Yields zero or more tuples of ``(out_key, out_value)``.

        :param key: A value parsed from input.
        :param value: A value parsed from input.

        If you don't re-define this, your job will have a mapper that simply
        yields ``(key, value)`` as-is.

        By default (if you don't mess with :ref:`job-protocols`):
         - ``key`` will be ``None``
         - ``value`` will be the raw input line, with newline stripped.
         - ``out_key`` and ``out_value`` must be JSON-encodable: numeric,
           unicode, boolean, ``None``, list, or dict whose keys are unicodes.
        """
        raise NotImplementedError

    def reducer(self, key, values):
        """Re-define this to define the reducer for a one-step job.

        Yields one or more tuples of ``(out_key, out_value)``

        :param key: A key which was yielded by the mapper
        :param value: A generator which yields all values yielded by the
                      mapper which correspond to ``key``.

        By default (if you don't mess with :ref:`job-protocols`):
         - ``out_key`` and ``out_value`` must be JSON-encodable.
         - ``key`` and ``value`` will have been decoded from JSON (so tuples
           will become lists).
        """
        raise NotImplementedError

    def combiner(self, key, values):
        """Re-define this to define the combiner for a one-step job.

        Yields one or more tuples of ``(out_key, out_value)``

        :param key: A key which was yielded by the mapper
        :param value: A generator which yields all values yielded by one mapper
                      task/node which correspond to ``key``.

        By default (if you don't mess with :ref:`job-protocols`):
         - ``out_key`` and ``out_value`` must be JSON-encodable.
         - ``key`` and ``value`` will have been decoded from JSON (so tuples
           will become lists).
        """
        raise NotImplementedError

    def mapper_init(self):
        """Re-define this to define an action to run before the mapper
        processes any input.

        One use for this function is to initialize mapper-specific helper
        structures.

        Yields one or more tuples of ``(out_key, out_value)``.

        By default, ``out_key`` and ``out_value`` must be JSON-encodable;
        re-define :py:attr:`INTERNAL_PROTOCOL` to change this.
        """
        raise NotImplementedError

    def mapper_final(self):
        """Re-define this to define an action to run after the mapper reaches
        the end of input.

        One way to use this is to store a total in an instance variable, and
        output it after reading all input data. See :py:mod:`mrjob.examples`
        for an example.

        Yields one or more tuples of ``(out_key, out_value)``.

        By default, ``out_key`` and ``out_value`` must be JSON-encodable;
        re-define :py:attr:`INTERNAL_PROTOCOL` to change this.
        """
        raise NotImplementedError

    def mapper_cmd(self):
        """Re-define this to define the mapper for a one-step job **as a shell
        command.** If you define your mapper this way, the command will be
        passed unchanged to Hadoop Streaming, with some minor exceptions. For
        important specifics, see :ref:`cmd-steps`.

        Basic example::

            def mapper_cmd(self):
                return 'cat'
        """
        raise NotImplementedError

    def mapper_pre_filter(self):
        """Re-define this to specify a shell command to filter the mapper's
        input before it gets to your job's mapper in a one-step job. For
        important specifics, see :ref:`cmd-filters`.

        Basic example::

            def mapper_pre_filter(self):
                return 'grep "ponies"'
        """
        raise NotImplementedError

    def mapper_raw(self, input_path, input_uri):
        """Re-define this to make Hadoop pass one input file to each
        mapper.

        :param input_path: a local path that the input file has been copied to
        :param input_uri: the URI of the input file on HDFS, S3, etc

        .. versionadded:: 0.6.3
        """
        raise NotImplementedError

    def reducer_init(self):
        """Re-define this to define an action to run before the reducer
        processes any input.

        One use for this function is to initialize reducer-specific helper
        structures.

        Yields one or more tuples of ``(out_key, out_value)``.

        By default, ``out_key`` and ``out_value`` must be JSON-encodable;
        re-define :py:attr:`INTERNAL_PROTOCOL` to change this.
        """
        raise NotImplementedError

    def reducer_final(self):
        """Re-define this to define an action to run after the reducer reaches
        the end of input.

        Yields one or more tuples of ``(out_key, out_value)``.

        By default, ``out_key`` and ``out_value`` must be JSON-encodable;
        re-define :py:attr:`INTERNAL_PROTOCOL` to change this.
        """
        raise NotImplementedError

    def reducer_cmd(self):
        """Re-define this to define the reducer for a one-step job **as a shell
        command.** If you define your mapper this way, the command will be
        passed unchanged to Hadoop Streaming, with some minor exceptions. For
        specifics, see :ref:`cmd-steps`.

        Basic example::

            def reducer_cmd(self):
                return 'cat'
        """
        raise NotImplementedError

    def reducer_pre_filter(self):
        """Re-define this to specify a shell command to filter the reducer's
        input before it gets to your job's reducer in a one-step job. For
        important specifics, see :ref:`cmd-filters`.

        Basic example::

            def reducer_pre_filter(self):
                return 'grep "ponies"'
        """
        raise NotImplementedError

    def combiner_init(self):
        """Re-define this to define an action to run before the combiner
        processes any input.

        One use for this function is to initialize combiner-specific helper
        structures.

        Yields one or more tuples of ``(out_key, out_value)``.

        By default, ``out_key`` and ``out_value`` must be JSON-encodable;
        re-define :py:attr:`INTERNAL_PROTOCOL` to change this.
        """
        raise NotImplementedError

    def combiner_final(self):
        """Re-define this to define an action to run after the combiner reaches
        the end of input.

        Yields one or more tuples of ``(out_key, out_value)``.

        By default, ``out_key`` and ``out_value`` must be JSON-encodable;
        re-define :py:attr:`INTERNAL_PROTOCOL` to change this.
        """
        raise NotImplementedError

    def combiner_cmd(self):
        """Re-define this to define the combiner for a one-step job **as a
        shell command.** If you define your mapper this way, the command will
        be passed unchanged to Hadoop Streaming, with some minor exceptions.
        For specifics, see :ref:`cmd-steps`.

        Basic example::

            def combiner_cmd(self):
                return 'cat'
        """
        raise NotImplementedError

    def combiner_pre_filter(self):
        """Re-define this to specify a shell command to filter the combiner's
        input before it gets to your job's combiner in a one-step job. For
        important specifics, see :ref:`cmd-filters`.

        Basic example::

            def combiner_pre_filter(self):
                return 'grep "ponies"'
        """
        raise NotImplementedError

    ### Defining one-step Spark jobs ###

    def spark(self, input_path, output_path):
        """Re-define this with Spark code to run. You can read input
        with *input_path* and output with *output_path*.

        .. warning::

           Prior to v0.6.8, to pass job methods into Spark
           (``rdd.flatMap(self.some_method)``), you first had to call
           :py:meth:`self.sandbox() <mrjob.job.MRJob.sandbox>`; otherwise
           Spark would error because *self* was not serializable.
        """
        raise NotImplementedError

    def spark_args(self):
        """Redefine this to pass custom arguments to Spark."""
        return []

    ### Defining multi-step jobs ###

    def steps(self):
        """Re-define this to make a multi-step job.

        If you don't re-define this, we'll automatically create a one-step
        job using any of :py:meth:`mapper`, :py:meth:`mapper_init`,
        :py:meth:`mapper_final`, :py:meth:`reducer_init`,
        :py:meth:`reducer_final`, and :py:meth:`reducer` that you've
        re-defined. For example::

            def steps(self):
                return [MRStep(mapper=self.transform_input,
                               reducer=self.consolidate_1),
                        MRStep(reducer_init=self.log_mapper_init,
                               reducer=self.consolidate_2)]

        :return: a list of steps constructed with
                 :py:class:`~mrjob.step.MRStep` or other classes in
                 :py:mod:`mrjob.step`.
        """
        # only include methods that have been redefined
        kwargs = dict(
            (func_name, getattr(self, func_name))
            for func_name in _JOB_STEP_FUNC_PARAMS + ('spark',)
            if (_im_func(getattr(self, func_name)) is not
                _im_func(getattr(MRJob, func_name))))

        # special case for spark()
        # TODO: support jobconf as well
        if 'spark' in kwargs:
            if sorted(kwargs) != ['spark']:
                raise ValueError(
                    "Can't mix spark() and streaming functions")
            return [SparkStep(
                spark=kwargs['spark'],
                spark_args=self.spark_args())]

        # MRStep takes commands as strings, but the user defines them in the
        # class as functions that return strings, so call the functions.
        updates = {}
        for k, v in kwargs.items():
            if k.endswith('_cmd') or k.endswith('_pre_filter'):
                updates[k] = v()

        kwargs.update(updates)

        if kwargs:
            return [MRStep(**kwargs)]
        else:
            return []

    def increment_counter(self, group, counter, amount=1):
        """Increment a counter in Hadoop streaming by printing to stderr.

        :type group: str
        :param group: counter group
        :type counter: str
        :param counter: description of the counter
        :type amount: int
        :param amount: how much to increment the counter by

        Commas in ``counter`` or ``group`` will be automatically replaced
        with semicolons (commas confuse Hadoop streaming).
        """
        # don't allow people to pass in floats
        if not isinstance(amount, integer_types):
            raise TypeError('amount must be an integer, not %r' % (amount,))

        # cast non-strings to strings (if people pass in exceptions, etc)
        if not isinstance(group, string_types):
            group = str(group)
        if not isinstance(counter, string_types):
            counter = str(counter)

        # Extra commas screw up hadoop and there's no way to escape them. So
        # replace them with the next best thing: semicolons!
        #
        # The relevant Hadoop code is incrCounter(), here:
        # http://svn.apache.org/viewvc/hadoop/mapreduce/trunk/src/contrib/streaming/src/java/org/apache/hadoop/streaming/PipeMapRed.java?view=markup  # noqa
        group = group.replace(',', ';')
        counter = counter.replace(',', ';')

        line = 'reporter:counter:%s,%s,%d\n' % (group, counter, amount)
        if not isinstance(line, bytes):
            line = line.encode('utf_8')

        self.stderr.write(line)
        self.stderr.flush()

    def set_status(self, msg):
        """Set the job status in hadoop streaming by printing to stderr.

        This is also a good way of doing a keepalive for a job that goes a
        long time between outputs; Hadoop streaming usually times out jobs
        that give no output for longer than 10 minutes.
        """
        line = 'reporter:status:%s\n' % (msg,)
        if not isinstance(line, bytes):
            line = line.encode('utf_8')

        self.stderr.write(line)
        self.stderr.flush()

    ### Running the job ###

    @classmethod
    def run(cls):
        """Entry point for running job from the command-line.

        This is also the entry point when a mapper or reducer is run
        by Hadoop Streaming.

        Does one of:

        * Print step information (:option:`--steps`). See :py:meth:`show_steps`
        * Run a mapper (:option:`--mapper`). See :py:meth:`run_mapper`
        * Run a combiner (:option:`--combiner`). See :py:meth:`run_combiner`
        * Run a reducer (:option:`--reducer`). See :py:meth:`run_reducer`
        * Run the entire job. See :py:meth:`run_job`
        """
        # load options from the command line
        mr_job = cls(args=_READ_ARGS_FROM_SYS_ARGV)
        mr_job.execute()

    def execute(self):
        # MRJob does Hadoop Streaming stuff, or defers to its superclass
        # (MRJobLauncher) if not otherwise instructed
        if self.options.show_steps:
            log_stream = codecs.getwriter('utf_8')(self.stderr)

            self.set_up_logging(quiet=self.options.quiet,
                                verbose=self.options.verbose,
                                stream=log_stream)

            self.show_steps()

        elif self.options.run_mapper:
            self.run_mapper(self.options.step_num)

        elif self.options.run_combiner:
            self.run_combiner(self.options.step_num)

        elif self.options.run_reducer:
            self.run_reducer(self.options.step_num)

        elif self.options.run_spark:
            self.run_spark(self.options.step_num)

        else:
            super(MRJob, self).execute()

    def make_runner(self):
        """Make a runner based on command-line arguments, so we can
        launch this job on EMR, on Hadoop, or locally.

        :rtype: :py:class:`mrjob.runner.MRJobRunner`
        """
        bad_words = (
            '--steps', '--mapper', '--reducer', '--combiner', '--step-num',
            '--spark')
        for w in bad_words:
            if w in sys.argv:
                raise UsageError("make_runner() was called with %s. This"
                                 " probably means you tried to use it from"
                                 " __main__, which doesn't work." % w)

        return super(MRJob, self).make_runner()

    def _runner_class(self):
        """Runner class as indicated by ``--runner``. Defaults to ``'inline'``.
        """
        if not self.options.runner or self.options.runner == 'inline':
            from mrjob.inline import InlineMRJobRunner
            return InlineMRJobRunner

        else:
            return super(MRJob, self)._runner_class()

    def _runner_kwargs(self):
        """If we're building an inline or Spark runner,
        include mrjob_cls in kwargs."""
        kwargs = super(MRJob, self)._runner_kwargs()

        if self._runner_class().alias in ('inline', 'spark'):
            kwargs = dict(mrjob_cls=self.__class__, **kwargs)

        # pass steps to runner (see #1845)
        kwargs = dict(steps=self._steps_desc(), **kwargs)

        return kwargs

    def _get_step(self, step_num, expected_type):
        """Helper for run_* methods"""
        steps = self.steps()
        if not 0 <= step_num < len(steps):
            raise ValueError('Out-of-range step: %d' % step_num)
        step = steps[step_num]
        if not isinstance(step, expected_type):
            raise TypeError('Step %d is not a %s', expected_type.__name__)
        return step

    def run_mapper(self, step_num=0):
        """Run the mapper and final mapper action for the given step.

        :type step_num: int
        :param step_num: which step to run (0-indexed)

        Called from :py:meth:`run`. You'd probably only want to call this
        directly from automated tests.
        """
        # pick input and output protocol
        read_lines, write_line = self._wrap_protocols(step_num, 'mapper')

        for k, v in self.map_pairs(read_lines(), step_num=step_num):
            write_line(k, v)

    def run_combiner(self, step_num=0):
        """Run the combiner for the given step.

        :type step_num: int
        :param step_num: which step to run (0-indexed)

        If we encounter a line that can't be decoded by our input protocol,
        or a tuple that can't be encoded by our output protocol, we'll
        increment a counter rather than raising an exception. If
        --strict-protocols is set, then an exception is raised

        Called from :py:meth:`run`. You'd probably only want to call this
        directly from automated tests.
        """
        # pick input and output protocol
        read_lines, write_line = self._wrap_protocols(step_num, 'combiner')

        for k, v in self.combine_pairs(read_lines(), step_num=step_num):
            write_line(k, v)

    def run_reducer(self, step_num=0):
        """Run the reducer for the given step.

        :type step_num: int
        :param step_num: which step to run (0-indexed)

        Called from :py:meth:`run`. You'd probably only want to call this
        directly from automated tests.
        """
        # pick input and output protocol
        read_lines, write_line = self._wrap_protocols(step_num, 'reducer')

        for k, v in self.reduce_pairs(read_lines(), step_num=step_num):
            write_line(k, v)

    def map_pairs(self, pairs, step_num=0):
        """Runs :py:meth:`mapper_init`,
        :py:meth:`mapper`/:py:meth:`mapper_raw`, and :py:meth:`mapper_final`
        for one map task in one step.

        Takes in a sequence of (key, value) pairs as input, and yields
        (key, value) pairs as output.

        :py:meth:`run_mapper` essentially wraps this method with code to handle
        reading/decoding input and writing/encoding output.

        .. versionadded:: 0.6.7
        """
        step = self._get_step(step_num, MRStep)

        mapper = step['mapper']
        mapper_raw = step['mapper_raw']
        mapper_init = step['mapper_init']
        mapper_final = step['mapper_final']

        if mapper_init:
            for k, v in mapper_init() or ():
                yield k, v

        if mapper_raw:
            if len(self.options.args) != 2:
                raise ValueError('Wrong number of args')
            input_path, input_uri = self.options.args
            for k, v in mapper_raw(input_path, input_uri) or ():
                yield k, v
        else:
            for key, value in pairs:
                for k, v in mapper(key, value) or ():
                    yield k, v

        if mapper_final:
            for k, v in mapper_final() or ():
                yield k, v

    def combine_pairs(self, pairs, step_num=0):
        """Runs :py:meth:`combiner_init`,
        :py:meth:`combiner`, and :py:meth:`combiner_final`
        for one reduce task in one step.

        Takes in a sequence of (key, value) pairs as input, and yields
        (key, value) pairs as output.

        :py:meth:`run_combiner` essentially wraps this method with code to
        handle reading/decoding input and writing/encoding output.

        .. versionadded:: 0.6.7
        """
        for k, v in self._combine_or_reduce_pairs(pairs, 'combiner', step_num):
            yield k, v

    def reduce_pairs(self, pairs, step_num=0):
        """Runs :py:meth:`reducer_init`,
        :py:meth:`reducer`, and :py:meth:`reducer_final`
        for one reduce task in one step.

        Takes in a sequence of (key, value) pairs as input, and yields
        (key, value) pairs as output.

        :py:meth:`run_reducer` essentially wraps this method with code to
        handle reading/decoding input and writing/encoding output.

        .. versionadded:: 0.6.7
        """
        for k, v in self._combine_or_reduce_pairs(pairs, 'reducer', step_num):
            yield k, v

    def _combine_or_reduce_pairs(self, pairs, mrc, step_num=0):
        """Helper for :py:meth:`combine_pairs` and :py:meth:`reduce_pairs`."""
        step = self._get_step(step_num, MRStep)

        task = step[mrc]
        task_init = step[mrc + '_init']
        task_final = step[mrc + '_final']
        if task is None:
            raise ValueError('No %s in step %d' % (mrc, step_num))

        if task_init:
            for k, v in task_init() or ():
                yield k, v

        # group all values of the same key together, and pass to the reducer
        #
        # be careful to use generators for everything, to allow for
        # very large groupings of values
        for key, pairs_for_key in itertools.groupby(pairs, lambda k_v: k_v[0]):
            values = (value for _, value in pairs_for_key)
            for k, v in task(key, values) or ():
                yield k, v

        if task_final:
            for k, v in task_final() or ():
                yield k, v

    def run_spark(self, step_num):
        """Run the Spark code for the given step.

        :type step_num: int
        :param step_num: which step to run (0-indexed)

        Called from :py:meth:`run`. You'd probably only want to call this
        directly from automated tests.
        """
        step = self._get_step(step_num, SparkStep)

        if len(self.options.args) != 2:
            raise ValueError('Wrong number of args')
        input_path, output_path = self.options.args

        spark_method = step.spark
        spark_method(input_path, output_path)

    def show_steps(self):
        """Print information about how many steps there are, and whether
        they contain a mapper or reducer. Job runners (see
        :doc:`guides/runners`) use this to determine how Hadoop should call
        this script.

        Called from :py:meth:`run`. You'd probably only want to call this
        directly from automated tests.
        """
        log.warning('--steps is deprecated and going away in v0.7.0')

        # json only uses strings, but self.stdout only accepts bytes
        steps_json = json.dumps(self._steps_desc())
        if not isinstance(steps_json, bytes):
            steps_json = steps_json.encode('utf_8')

        self.stdout.write(steps_json)
        self.stdout.write(b'\n')

    def _steps_desc(self):
        step_descs = []
        for step_num, step in enumerate(self.steps()):
            step_descs.append(step.description(step_num))
        return step_descs

    @classmethod
    def mr_job_script(cls):
        """Path of this script. This returns the file containing
        this class, or ``None`` if there isn't any (e.g. it was
        defined from the command line interface.)"""
        try:
            return inspect.getsourcefile(cls)
        except TypeError:
            return None

    ### Other useful utilities ###

    def _read_input(self):
        """Read from stdin, or one more files, or directories.
        Yield one line at time.

        - Resolve globs (``foo_*.gz``).
        - Decompress ``.gz`` and ``.bz2`` files.
        - If path is ``-``, read from STDIN.
        - Recursively read all files in a directory
        """
        paths = self.options.args or ['-']

        for path in paths:
            if path == '-':
                for line in self.stdin:
                    yield line
            else:
                with open(path, 'rb') as f:
                    for line in to_lines(decompress(f, path)):
                        yield line

    def _wrap_protocols(self, step_num, step_type):
        """Pick the protocol classes to use for reading and writing
        for the given step.

        Returns a tuple of ``(read_lines, write_line)``

        ``read_lines()`` is a function that reads lines from input, decodes
            them, and yields key, value pairs.
        ``write_line()`` is a function that takes key and value as args,
            encodes them, and writes a line to output.

        :param step_num: which step to run (e.g. 0)
        :param step_type: ``'mapper'``, ``'reducer'``, or ``'combiner'`` from
                          :py:mod:`mrjob.step`
        """
        read, write = self.pick_protocols(step_num, step_type)

        def read_lines():
            for line in self._read_input():
                key, value = read(line.rstrip(b'\r\n'))
                yield key, value

        def write_line(key, value):
            self.stdout.write(write(key, value))
            self.stdout.write(b'\n')

        return read_lines, write_line

    def _step_key(self, step_num, step_type):
        return '%d-%s' % (step_num, step_type)

    def _script_step_mapping(self, steps_desc):
        """Return a mapping of ``self._step_key(step_num, step_type)`` ->
        (place in sort order of all *script* steps), for the purposes of
        choosing which protocols to use for input and output.

        Non-script steps do not appear in the mapping.
        """
        mapping = {}
        script_step_num = 0
        for i, step in enumerate(steps_desc):

            if 'mapper' in step and step['mapper']['type'] == 'script':
                k = self._step_key(i, 'mapper')
                mapping[k] = script_step_num
                script_step_num += 1

            if 'reducer' in step and step['reducer']['type'] == 'script':
                k = self._step_key(i, 'reducer')
                mapping[k] = script_step_num
                script_step_num += 1

        return mapping

    def _mapper_output_protocol(self, step_num, step_map):
        map_key = self._step_key(step_num, 'mapper')
        if map_key in step_map:
            if step_map[map_key] >= (len(step_map) - 1):
                return self.output_protocol()
            else:
                return self.internal_protocol()
        else:
            # mapper is not a script substep, so protocols don't apply at all
            return RawValueProtocol()

    def _pick_protocol_instances(self, step_num, step_type):
        steps_desc = self._steps_desc()

        step_map = self._script_step_mapping(steps_desc)

        # pick input protocol

        if step_type == 'combiner':
            # Combiners read and write the mapper's output protocol because
            # they have to be able to run 0-inf times without changing the
            # format of the data.
            # Combiners for non-script substeps can't use protocols, so this
            # function will just give us RawValueProtocol() in that case.
            previous_mapper_output = self._mapper_output_protocol(
                step_num, step_map)
            return previous_mapper_output, previous_mapper_output
        else:
            step_key = self._step_key(step_num, step_type)

            if step_key not in step_map:
                raise ValueError(
                    "Can't pick a protocol for a non-script step")

            real_num = step_map[step_key]
            if real_num == (len(step_map) - 1):
                write = self.output_protocol()
            else:
                write = self.internal_protocol()

            if real_num == 0:
                read = self.input_protocol()
            else:
                read = self.internal_protocol()
            return read, write

    def pick_protocols(self, step_num, step_type):
        """Pick the protocol classes to use for reading and writing for the
        given step.

        :type step_num: int
        :param step_num: which step to run (e.g. ``0`` for the first step)
        :type step_type: str
        :param step_type: one of `'mapper'`, `'combiner'`, or `'reducer'`
        :return: (read_function, write_function)

        By default, we use one protocol for reading input, one
        internal protocol for communication between steps, and one
        protocol for final output (which is usually the same as the
        internal protocol). Protocols can be controlled by setting
        :py:attr:`INPUT_PROTOCOL`, :py:attr:`INTERNAL_PROTOCOL`, and
        :py:attr:`OUTPUT_PROTOCOL`.

        Re-define this if you need fine control over which protocols
        are used by which steps.
        """

        # wrapping functionality like this makes testing much simpler
        p_read, p_write = self._pick_protocol_instances(step_num, step_type)

        return p_read.read, p_write.write

    ### Command-line arguments ###

    def configure_args(self):
        """Define arguments for this script. Called from :py:meth:`__init__()`.

        Re-define to define custom command-line arguments or pass
        through existing ones::

            def configure_args(self):
                super(MRYourJob, self).configure_args()

                self.add_passthru_arg(...)
                self.add_file_arg(...)
                self.pass_arg_through(...)
                ...
        """
        super(MRJob, self).configure_args()

        _add_step_args(self.arg_parser, include_deprecated=True)

    def is_task(self):
        """True if this is a mapper, combiner, reducer, or Spark script.

        This is mostly useful inside :py:meth:`load_args`, to disable
        loading args when we aren't running inside Hadoop.
        """
        return (self.options.run_mapper or
                self.options.run_combiner or
                self.options.run_reducer or
                self.options.run_spark)

    def _print_help(self, options):
        """Implement --help --steps"""
        if options.show_steps:
            _print_help_for_steps(include_deprecated=self.options.deprecated)
        else:
            super(MRJob, self)._print_help(options)

    ### protocols ###

    def input_protocol(self):
        """Instance of the protocol to use to convert input lines to Python
        objects. Default behavior is to return an instance of
        :py:attr:`INPUT_PROTOCOL`.
        """
        if not isinstance(self.INPUT_PROTOCOL, type):
            log.warning('INPUT_PROTOCOL should be a class, not %s' %
                        self.INPUT_PROTOCOL)
        return self.INPUT_PROTOCOL()

    def internal_protocol(self):
        """Instance of the protocol to use to communicate between steps.
        Default behavior is to return an instance of
        :py:attr:`INTERNAL_PROTOCOL`.
        """
        if not isinstance(self.INTERNAL_PROTOCOL, type):
            log.warning('INTERNAL_PROTOCOL should be a class, not %s' %
                        self.INTERNAL_PROTOCOL)
        return self.INTERNAL_PROTOCOL()

    def output_protocol(self):
        """Instance of the protocol to use to convert Python objects to output
        lines. Default behavior is to return an instance of
        :py:attr:`OUTPUT_PROTOCOL`.
        """
        if not isinstance(self.OUTPUT_PROTOCOL, type):
            log.warning('OUTPUT_PROTOCOL should be a class, not %s' %
                        self.OUTPUT_PROTOCOL)
        return self.OUTPUT_PROTOCOL()

    #: Protocol for reading input to the first mapper in your job.
    #: Default: :py:class:`RawValueProtocol`.
    #:
    #: For example you know your input data were in JSON format, you could
    #: set::
    #:
    #:     INPUT_PROTOCOL = JSONValueProtocol
    #:
    #: in your class, and your initial mapper would receive decoded JSONs
    #: rather than strings.
    #:
    #: See :py:data:`mrjob.protocol` for the full list of protocols.
    INPUT_PROTOCOL = RawValueProtocol

    #: Protocol for communication between steps and final output.
    #: Default: :py:class:`JSONProtocol`.
    #:
    #: For example if your step output weren't JSON-encodable, you could set::
    #:
    #:     INTERNAL_PROTOCOL = PickleProtocol
    #:
    #: and step output would be encoded as string-escaped pickles.
    #:
    #: See :py:data:`mrjob.protocol` for the full list of protocols.
    INTERNAL_PROTOCOL = JSONProtocol

    #: Protocol to use for writing output. Default: :py:class:`JSONProtocol`.
    #:
    #: For example, if you wanted the final output in repr, you could set::
    #:
    #:     OUTPUT_PROTOCOL = ReprProtocol
    #:
    #: See :py:data:`mrjob.protocol` for the full list of protocols.
    OUTPUT_PROTOCOL = JSONProtocol

    def parse_output(self, chunks):
        """Parse the final output of this MRJob (as a stream of byte chunks)
        into a stream of ``(key, value)``.
        """
        read = self.output_protocol().read

        for line in to_lines(chunks):
            yield read(line)

    def parse_output_line(self, line):
        """
        Parse a line from the final output of this MRJob into
        ``(key, value)``.

        .. deprecated:: 0.6.0

           Use :py:meth:`parse_output` instead.
        """
        if not self._warned_about_parse_output_line:
            log.warning('parse_output_line() is deprecated and will be removed'
                        ' in v0.7.0; use parse_output() instead.')
            self._warned_about_parse_output_line = True

        return self.output_protocol().read(line)

    ### Hadoop Input/Output Formats ###

    #: Optional name of an optional Hadoop ``InputFormat`` class, e.g.
    #: ``'org.apache.hadoop.mapred.lib.NLineInputFormat'``.
    #:
    #: Passed to Hadoop with the *first* step of this job with the
    #: ``-inputformat`` option.
    #:
    #: If you require more sophisticated behavior, try
    #: :py:meth:`hadoop_input_format` or the *hadoop_input_format* argument to
    #: :py:meth:`mrjob.runner.MRJobRunner.__init__`.
    HADOOP_INPUT_FORMAT = None

    def hadoop_input_format(self):
        """Optional Hadoop ``InputFormat`` class to parse input for
        the first step of the job.

        Normally, setting :py:attr:`HADOOP_INPUT_FORMAT` is sufficient;
        redefining this method is only for when you want to get fancy.
        """
        return self.HADOOP_INPUT_FORMAT

    #: Optional name of an optional Hadoop ``OutputFormat`` class, e.g.
    #: ``'org.apache.hadoop.mapred.FileOutputFormat'``.
    #:
    #: Passed to Hadoop with the *last* step of this job with the
    #: ``-outputformat`` option.
    #:
    #: If you require more sophisticated behavior, try
    #: :py:meth:`hadoop_output_format` or the *hadoop_output_format* argument
    #: to :py:meth:`mrjob.runner.MRJobRunner.__init__`.
    HADOOP_OUTPUT_FORMAT = None

    def hadoop_output_format(self):
        """Optional Hadoop ``OutputFormat`` class to write output for
        the last step of the job.

        Normally, setting :py:attr:`HADOOP_OUTPUT_FORMAT` is sufficient;
        redefining this method is only for when you want to get fancy.
        """
        return self.HADOOP_OUTPUT_FORMAT

    ### Libjars ###

    #: Optional list of paths of jar files to run our job with using Hadoop's
    #: ``-libjars`` option.
    #:
    #: ``~`` and environment variables
    #: in paths be expanded, and relative paths will be interpreted as
    #: relative to the directory containing the script (not the current
    #: working directory).
    #:
    #: If you require more sophisticated behavior, try overriding
    #: :py:meth:`libjars`.
    #:
    #: .. versionadded:: 0.5.3
    LIBJARS = []

    def libjars(self):
        """Optional list of paths of jar files to run our job with using
        Hadoop's ``-libjars`` option. Normally setting :py:attr:`LIBJARS`
        is sufficient. Paths from :py:attr:`LIBJARS` are interpreted as
        relative to the the directory containing the script (paths from the
        command-line are relative to the current working directory).

        Note that ``~`` and environment variables in paths will always be
        expanded by the job runner (see :mrjob-opt:`libjars`).

        .. versionadded:: 0.5.3

        .. versionchanged:: 0.6.6

           re-defining this no longer clobbers the command-line
           ``--libjars`` option
        """
        script_dir = os.path.dirname(self.mr_job_script())

        paths = []

        # libjar paths will eventually be combined with combine_path_lists,
        # which will expand environment variables. We don't want to assume
        # a path like $MY_DIR/some.jar is always relative ($MY_DIR could start
        # with /), but we also don't want to expand environment variables
        # prematurely.
        for path in self.LIBJARS or []:
            if os.path.isabs(expand_path(path)):
                paths.append(path)
            else:
                paths.append(os.path.join(script_dir, path))

        return paths

    ### Partitioning ###

    #: Optional Hadoop partitioner class to use to determine how mapper
    #: output should be sorted and distributed to reducers. For example:
    #: ``'org.apache.hadoop.mapred.lib.HashPartitioner'``.
    #:
    #: If you require more sophisticated behavior, try :py:meth:`partitioner`.
    PARTITIONER = None

    def partitioner(self):
        """Optional Hadoop partitioner class to use to determine how mapper
        output should be sorted and distributed to reducers.

        By default, returns :py:attr:`PARTITIONER`.

        You probably don't need to re-define this; it's just here for
        completeness.
        """
        return self.PARTITIONER

    ### Uploading support files ###

    #: Optional list of archives to upload and unpack in the job's working
    #: directory. These can be URIs or paths on the local filesystem.
    #:
    #: Relative paths will be interpreted as relative to the directory
    #: containing the script (not the current working directory).
    #
    #: Environment variables and ``~`` in paths will be expanded.
    #:
    #: By default, the directory will have the same name as the archive
    #: (e.g. ``foo.tar.gz/``). To change the directory's name, append
    #: ``#<name>``::
    #:
    #:     ARCHIVES = ['data/foo.tar.gz#foo']
    #:
    #: If you need to dynamically generate a list of files, override
    #: :py:meth:`archives` instead.
    #:
    #: .. versionadded:: 0.6.4
    ARCHIVES = []

    #: Optional list of directories to upload to the job's working directory.
    #: These can be URIs or paths on the local filesystem.
    #:
    #: Relative paths will be interpreted as relative to the directory
    #: containing the script (not the current working directory).
    #
    #: Environment variables and ``~`` in paths will be expanded.
    #:
    #: If you want a directory to be copied with a name other than it's own,
    #: append ``#<name>`` (e.g. ``data/foo#bar``).
    #:
    #: If you need to dynamically generate a list of files, override
    #: :py:meth:`dirs` instead.
    #:
    #: .. versionadded:: 0.6.4
    DIRS = []

    #: Optional list of files to upload to the job's working directory.
    #: These can be URIs or paths on the local filesystem.
    #:
    #: Relative paths will be interpreted as relative to the directory
    #: containing the script (not the current working directory).
    #
    #: Environment variables and ``~`` in paths will be expanded.
    #:
    #: If you want a file to be uploaded to a filename other than it's own,
    #: append ``#<name>`` (e.g. ``data/foo.json#bar.json``).
    #:
    #: If you need to dynamically generate a list of files, override
    #: :py:meth:`files` instead.
    #:
    #: .. versionadded:: 0.6.4
    FILES = []

    def archives(self):
        """Like :py:attr:`ARCHIVES`, except that it can return a dynamically
        generated list of archives to upload and unpack. Overriding
        this method disables :py:attr:`ARCHIVES`.

        Paths returned by this method are relative to the working directory
        (not the script). Note that the job runner will *always* expand
        environment variables and ``~`` in paths returned by this method.

        You do not have to worry about inadvertently disabling ``--archives``;
        this switch is handled separately.

        .. versionadded:: 0.6.4
        """
        return self._upload_attr('ARCHIVES')

    def dirs(self):
        """Like :py:attr:`DIRS`, except that it can return a dynamically
        generated list of directories to upload. Overriding
        this method disables :py:attr:`DIRS`.

        Paths returned by this method are relative to the working directory
        (not the script). Note that the job runner will *always* expand
        environment variables and ``~`` in paths returned by this method.

        You do not have to worry about inadvertently disabling ``--dirs``;
        this switch is handled separately.

        .. versionadded:: 0.6.4
        """
        return self._upload_attr('DIRS')

    def files(self):
        """Like :py:attr:`FILES`, except that it can return a dynamically
        generated list of files to upload. Overriding
        this method disables :py:attr:`FILES`.

        Paths returned by this method are relative to the working directory
        (not the script). Note that the job runner will *always* expand
        environment variables and ``~`` in paths returned by this method.

        You do not have to worry about inadvertently disabling ``--files``;
        this switch is handled separately.

        .. versionadded:: 0.6.4
        """
        return self._upload_attr('FILES')

    def _upload_attr(self, attr_name):
        """Helper for :py:meth:`archives`, :py:meth:`dirs`, and
        :py:meth:`files`"""
        attr_value = getattr(self, attr_name)

        # catch path instead of a list of paths
        if isinstance(attr_value, string_types):
            raise TypeError('%s must be a list or other sequence.' % attr_name)

        script_dir = os.path.dirname(self.mr_job_script())
        paths = []

        for path in attr_value:
            expanded_path = expand_path(path)

            if os.path.isabs(expanded_path):
                paths.append(path)
            else:
                # relative subdirs are confusing; people will expect them
                # to appear in a subdir, not the same directory as the script,
                # but Hadoop doesn't work that way
                if os.sep in path.rstrip(os.sep) and '#' not in path:
                    log.warning(
                        '%s: %s will appear in same directory as job script,'
                        ' not a subdirectory' % (attr_name, path))

                paths.append(os.path.join(script_dir, path))

        return paths

    ### Jobconf ###

    #: Optional jobconf arguments we should always pass to Hadoop. This
    #: is a map from property name to value. e.g.:
    #:
    #: ``{'stream.num.map.output.key.fields': '4'}``
    #:
    #: It's recommended that you only use this to hard-code things that
    #: affect the semantics of your job, and leave performance tweaks to
    #: the command line or whatever you use to launch your job.
    JOBCONF = {}

    def jobconf(self):
        """``-D`` args to pass to hadoop streaming. This should be a map
        from property name to value. By default, returns :py:attr:`JOBCONF`.

        .. versionchanged:: 0.6.6

           re-defining longer clobbers command-line
           ``--jobconf`` options.
        """
        return dict(self.JOBCONF)

    ### Secondary Sort ###

    #: Set this to ``True`` if you would like reducers to receive the values
    #: associated with any key in sorted order (sorted by their *encoded*
    #: value). Also known as secondary sort.
    #:
    #: This can be useful if you expect more values than you can fit in memory
    #: to be associated with one key, but you want to apply information in
    #: a small subset of these values to information in the other values.
    #: For example, you may want to convert counts to percentages, and to do
    #: this you first need to know the total count.
    #:
    #: Even though values are sorted by their encoded value, most encodings
    #: will sort strings in order. For example, you could have values like:
    #: ``['A', <total>]``, ``['B', <count_name>, <count>]``, and the value
    #: containing the total should come first regardless of what protocol
    #: you're using.
    #:
    #: See :py:meth:`jobconf()` and :py:meth:`partitioner()` for more about
    SORT_VALUES = None

    def sort_values(self):
        """A method that by default, just returns the value of
        :py:attr:`SORT_VALUES`. Mostly exists for the sake
        of consistency, but you could override it if you wanted to make
        secondary sort configurable."""
        return self.SORT_VALUES


if __name__ == '__main__':
    MRJob.run()
