# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""A wrapper around DebugDataReader used for retrieving tfdbg v2 data."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import threading


# Dummy run name for the debugger.
# Currently, the `DebuggerV2ExperimentMultiplexer` class is tied to a single
# logdir, which holds at most one DebugEvent file set in the tfdbg v2 (tfdbg2
# for short) format.
# TODO(cais): When tfdbg2 allows there to be multiple DebugEvent file sets in
# the same logdir, replace this magic string with actual run names.
DEFAULT_DEBUGGER_RUN_NAME = "__default_debugger_run__"


def _execution_digest_to_json(execution_digest):
    # TODO(cais): Use the .to_json() method when avaiable.
    return {
        "wall_time": execution_digest.wall_time,
        "op_type": execution_digest.op_type,
        "output_tensor_device_ids": list(
            execution_digest.output_tensor_device_ids
        ),
    }


def run_in_background(target):
    """Run a target task in the background.

    In the context of this module, `target` is the `update()` method of the
    underlying reader for tfdbg2-format data.
    This method is mocked by unit tests for deterministic behaviors during
    testing.

    Args:
      target: The target task to run in the background, a callable with no args.
    """
    # TODO(cais): Implement repetition with sleeping periods in between.
    # TODO(cais): Add more unit tests in debug_data_multiplexer_test.py when the
    # the behavior gets more complex.
    thread = threading.Thread(target=target)
    thread.start()


class DebuggerV2EventMultiplexer(object):
    """A class used for accessing tfdbg v2 DebugEvent data on local filesystem.

    This class is a short-term hack, mirroring the EventMultiplexer for the main
    TensorBoard plugins (e.g., scalar, histogram and graphs.) As such, it only
    implements the methods relevant to the Debugger V2 pluggin.

    TODO(cais): Integrate it with EventMultiplexer and use the integrated class
    from MultiplexerDataProvider for a single path of accessing debugger and
    non-debugger data.
    """

    def __init__(self, logdir):
        """Constructor for the `DebugEventMultiplexer`.

        Args:
          logdir: Path to the directory to load the tfdbg v2 data from.
        """
        self._logdir = logdir
        self._reader = None

    def FirstEventTimestamp(self, run):
        """Return the timestamp of the first DebugEvent of the given run.

        This may perform I/O if no events have been loaded yet for the run.

        Args:
          run: A string name of the run for which the timestamp is retrieved.
            This currently must be hardcoded as `DEFAULT_DEBUGGER_RUN_NAME`,
            as each logdir contains at most one DebugEvent file set (i.e., a
            run of a tfdbg2-instrumented TensorFlow program.)

        Returns:
            The wall_time of the first event of the run, which will be in seconds
            since the epoch as a `float`.
        """
        if self._reader is None:
            raise ValueError("No tfdbg2 runs exists.")
        if run != DEFAULT_DEBUGGER_RUN_NAME:
            raise ValueError(
                "Expected run name to be %s, but got %s"
                % (DEFAULT_DEBUGGER_RUN_NAME, run)
            )
        return self._reader.starting_wall_time()

    def PluginRunToTagToContent(self, plugin_name):
        raise NotImplementedError(
            "DebugDataMultiplexer.PluginRunToTagToContent() has not been "
            "implemented yet."
        )

    def Runs(self):
        """Return all the run names in the `EventMultiplexer`.

        The `Run()` method of this class is specialized for the tfdbg2-format
        DebugEvent files. It only returns runs

        Returns:
        If tfdbg2-format data exists in the `logdir` of this object, returns:
            ```
            {runName: { "debugger-v2": [tag1, tag2, tag3] } }
            ```
            where `runName` is the hard-coded string `DEFAULT_DEBUGGER_RUN_NAME`
            string. This is related to the fact that tfdbg2 currently contains
            at most one DebugEvent file set per directory.
        If no tfdbg2-format data exists in the `logdir`, an empty `dict`.
        """
        if self._reader is None:
            from tensorflow.python.debug.lib import debug_events_reader

            try:
                self._reader = debug_events_reader.DebugDataReader(self._logdir)
                # NOTE(cais): Currently each logdir is enforced to have only one
                # DebugEvent file set. So we add hard-coded default run name.
                run_in_background(self._reader.update)
                # TODO(cais): Start off a reading thread here, instead of being
                # called only once here.
            except AttributeError as error:
                # Gracefully fail for users without the required API changes to
                # debug_events_reader.DebugDataReader introduced in
                # TF 2.1.0.dev20200103. This should be safe to remove when
                # TF 2.2 is released.
                return {}
            except ValueError as error:
                # When no DebugEvent file set is found in the logdir, a
                # `ValueError` is thrown.
                return {}

        return {
            DEFAULT_DEBUGGER_RUN_NAME: {
                # TODO(cais): Add the semantically meaningful tag names such as
                # 'execution_digests_book', 'alerts_book'
                "debugger-v2": []
            }
        }

    def ExecutionDigests(self, run, begin, end):
        """Get ExecutionDigests.

        Args:
          run: The tfdbg2 run to get `ExecutionDigest`s from.
          begin: Beginning execution index.
          end: Ending execution index.

        Returns:
          A JSON-serializable object containing the `ExecutionDigest`s and
          related meta-information
        """
        runs = self.Runs()
        if run not in runs:
            return None
        # TODO(cais): For scalability, use begin and end kwargs when available in
        # `DebugDataReader.execution()`.`
        execution_digests = self._reader.executions(digest=True)
        if begin < 0:
            raise IndexError("Invalid begin index (%d)" % begin)
        if end > len(execution_digests):
            raise IndexError(
                "end index (%d) out of bounds (%d)"
                % (end, len(execution_digests))
            )
        if end >= 0 and end < begin:
            raise ValueError(
                "end index (%d) is unexpected less than begin index (%d)"
                % (end, begin)
            )
        if end < 0:  # This means all digests.
            end = len(execution_digests)
        return {
            "begin": begin,
            "end": end,
            "num_digests": len(execution_digests),
            "execution_digests": [
                _execution_digest_to_json(digest)
                for digest in execution_digests[begin:end]
            ],
        }

    def SourceFileList(self, run):
        runs = self.Runs()
        if run not in runs:
            return None
        # TODO(cais): Use public method `self._reader.source_files()` when available.
        # pylint: disable=protected-access
        return list(self._reader._host_name_file_path_to_offset.keys())
        # pylint: enable=protected-access

    def SourceLines(self, run, index):
        runs = self.Runs()
        if run not in runs:
            return None
        # TODO(cais): Use public method `self._reader.source_files()` when available.
        # pylint: disable=protected-access
        source_file_list = list(
            self._reader._host_name_file_path_to_offset.keys()
        )
        # pylint: enable=protected-access
        try:
            host_name, file_path = source_file_list[index]
        except IndexError:
            raise IndexError("There is no source-code file at index %d" % index)
        return {
            "host_name": host_name,
            "file_path": file_path,
            "lines": self._reader.source_lines(host_name, file_path),
        }
