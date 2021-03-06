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
"""Tests for Debugger V2 Plugin."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import os
import socket

import tensorflow as tf
from werkzeug import test as werkzeug_test  # pylint: disable=wrong-import-order
from werkzeug import wrappers

from tensorboard.backend import application
from tensorboard.plugins import base_plugin
from tensorboard.plugins.debugger_v2 import debug_data_multiplexer
from tensorboard.plugins.debugger_v2 import debugger_v2_plugin
from tensorboard.util import test_util


_HOST_NAME = socket.gethostname()
_CURRENT_FILE_FULL_PATH = os.path.abspath(__file__)


def _generate_tfdbg_v2_data(logdir):
    """Generate a simple dump of tfdbg v2 data by running a TF2 program.

    The run is instrumented by the enable_dump_debug_info() API.

    The instrumented program is intentionally diverse in:
      - Execution paradigm: eager + tf.function
      - Control flow (TF while loop)
      - dtype and shape
    in order to faciliate testing.

    Args:
      logdir: Logdir to write the debugger data to.
    """
    writer = tf.debugging.experimental.enable_dump_debug_info(
        logdir, circular_buffer_size=-1
    )
    try:

        @tf.function
        def unstack_and_sum(x):
            elements = tf.unstack(x)
            return elements[0] + elements[1] + elements[2] + elements[3]

        @tf.function
        def repeated_add(x, times):
            sum = tf.constant(0, dtype=x.dtype)
            i = tf.constant(0, dtype=tf.int32)
            while tf.less(i, times):
                sum += x
                i += 1
            return sum

        @tf.function
        def my_function(x):
            times = tf.constant(3, dtype=tf.int32)
            return repeated_add(unstack_and_sum(x), times)

        x = tf.constant([1, 3, 3, 7], dtype=tf.float32)
        for i in range(3):
            assert my_function(x).numpy() == 42.0
        writer.FlushNonExecutionFiles()
        writer.FlushExecutionFiles()
    finally:
        tf.debugging.experimental.disable_dump_debug_info()


_ROUTE_PREFIX = "/data/plugin/debugger-v2"


@test_util.run_v2_only("tfdbg2 is not available in r1.")
class DebuggerV2PluginTest(tf.test.TestCase):
    def setUp(self):
        super(DebuggerV2PluginTest, self).setUp()
        self.logdir = self.get_temp_dir()
        context = base_plugin.TBContext(logdir=self.logdir)
        self.plugin = debugger_v2_plugin.DebuggerV2Plugin(context)
        wsgi_app = application.TensorBoardWSGI([self.plugin])
        self.server = werkzeug_test.Client(wsgi_app, wrappers.BaseResponse)
        # The multiplexer reads data asynchronously on a separate thread, so
        # as not to block the main thread of the TensorBoard backend. During
        # unit test, we disable the asynchronous behavior, so that we can
        # load the debugger data synchronously on the main thread and get
        # determinisic behavior in the tests.
        def run_in_background_mock(target):
            target()

        self.run_in_background_patch = tf.compat.v1.test.mock.patch.object(
            debug_data_multiplexer, "run_in_background", run_in_background_mock
        )
        self.run_in_background_patch.start()

    def tearDown(self):
        self.run_in_background_patch.stop()
        super(DebuggerV2PluginTest, self).tearDown()

    def _getExactlyOneRun(self):
        """Assert there is exactly one DebuggerV2 run and get its ID."""
        run_listing = json.loads(
            self.server.get(_ROUTE_PREFIX + "/runs").get_data()
        )
        self.assertLen(run_listing, 1)
        return list(run_listing.keys())[0]

    def testPluginIsNotActiveByDefault(self):
        self.assertFalse(self.plugin.is_active())

    def testPluginIsActiveWithDataExists(self):
        _generate_tfdbg_v2_data(self.logdir)
        self.assertTrue(self.plugin.is_active())

    def testServeRunsWithoutExistingRuns(self):
        response = self.server.get(_ROUTE_PREFIX + "/runs")
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        self.assertEqual(json.loads(response.get_data()), dict())

    def testServeRunsWithExistingRuns(self):
        _generate_tfdbg_v2_data(self.logdir)
        response = self.server.get(_ROUTE_PREFIX + "/runs")
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        data = json.loads(response.get_data())
        self.assertEqual(list(data.keys()), ["__default_debugger_run__"])
        run = data["__default_debugger_run__"]
        self.assertIsInstance(run["start_time"], float)
        self.assertGreater(run["start_time"], 0)

    def testServeExecutionDigestsWithEqualBeginAndEnd(self):
        _generate_tfdbg_v2_data(self.logdir)
        run = self._getExactlyOneRun()
        response = self.server.get(
            _ROUTE_PREFIX + "/execution/digests?run=%s&begin=0&end=0" % run
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        data = json.loads(response.get_data())
        self.assertEqual(
            data,
            {"begin": 0, "end": 0, "num_digests": 3, "execution_digests": [],},
        )

    def testServeExecutionDigestsWithEndGreaterThanBeginFullRange(self):
        _generate_tfdbg_v2_data(self.logdir)
        run = self._getExactlyOneRun()
        response = self.server.get(
            _ROUTE_PREFIX + "/execution/digests?run=%s&begin=0&end=3" % run
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        data = json.loads(response.get_data())
        self.assertEqual(data["begin"], 0)
        self.assertEqual(data["end"], 3)
        self.assertEqual(data["num_digests"], 3)
        execution_digests = data["execution_digests"]
        self.assertLen(execution_digests, 3)
        prev_wall_time = 0
        for execution_digest in execution_digests:
            self.assertGreaterEqual(
                execution_digest["wall_time"], prev_wall_time
            )
            prev_wall_time = execution_digest["wall_time"]
            self.assertStartsWith(
                execution_digest["op_type"], "__inference_my_function"
            )

    def testServeExecutionDigestsWithImplicitFullRange(self):
        _generate_tfdbg_v2_data(self.logdir)
        run = self._getExactlyOneRun()
        response = self.server.get(
            _ROUTE_PREFIX + "/execution/digests?run=%s&begin=0" % run
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        data = json.loads(response.get_data())
        self.assertEqual(data["begin"], 0)
        self.assertEqual(data["end"], 3)
        self.assertEqual(data["num_digests"], 3)
        execution_digests = data["execution_digests"]
        self.assertLen(execution_digests, 3)
        prev_wall_time = 0
        for execution_digest in execution_digests:
            self.assertGreaterEqual(
                execution_digest["wall_time"], prev_wall_time
            )
            prev_wall_time = execution_digest["wall_time"]
            self.assertStartsWith(
                execution_digest["op_type"], "__inference_my_function"
            )

    def testServeExecutionDigestsWithEndGreaterThanBeginPartialRange(self):
        _generate_tfdbg_v2_data(self.logdir)
        run = self._getExactlyOneRun()
        response = self.server.get(
            _ROUTE_PREFIX + "/execution/digests?run=%s&begin=0&end=2" % run
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        data = json.loads(response.get_data())
        self.assertEqual(data["begin"], 0)
        self.assertEqual(data["end"], 2)
        self.assertEqual(data["num_digests"], 3)
        execution_digests = data["execution_digests"]
        self.assertLen(execution_digests, 2)
        prev_wall_time = 0
        for execution_digest in execution_digests:
            self.assertGreaterEqual(
                execution_digest["wall_time"], prev_wall_time
            )
            prev_wall_time = execution_digest["wall_time"]
            self.assertStartsWith(
                execution_digest["op_type"], "__inference_my_function"
            )

    def testServeExecutionDigestOutOfBoundsError(self):
        _generate_tfdbg_v2_data(self.logdir)
        run = self._getExactlyOneRun()

        # begin = 0; end = 4
        response = self.server.get(
            _ROUTE_PREFIX + "/execution/digests?run=%s&begin=0&end=4" % run
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        self.assertEqual(
            json.loads(response.get_data()),
            {"error": "end index (4) out of bounds (3)"},
        )

        # begin = -1; end = 2
        response = self.server.get(
            _ROUTE_PREFIX + "/execution/digests?run=%s&begin=-1&end=2" % run
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        self.assertEqual(
            json.loads(response.get_data()),
            {"error": "Invalid begin index (-1)"},
        )

        # begin = 2; end = 1
        response = self.server.get(
            _ROUTE_PREFIX + "/execution/digests?run=%s&begin=2&end=1" % run
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        self.assertEqual(
            json.loads(response.get_data()),
            {"error": "end index (1) is unexpected less than begin index (2)"},
        )

    def testServeExecutionDigests400ResponseIfRunParamIsNotSpecified(self):
        response = self.server.get(
            # `run` parameter is not specified here.
            _ROUTE_PREFIX
            + "/execution/digests?begin=0&end=0"
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        self.assertEqual(
            json.loads(response.get_data()),
            {"error": "run parameter is not provided"},
        )

    def testServeSourceFileListIncludesThisTestFile(self):
        _generate_tfdbg_v2_data(self.logdir)
        run = self._getExactlyOneRun()
        response = self.server.get(
            _ROUTE_PREFIX + "/source_files/list?run=%s" % run
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        source_file_list = json.loads(response.get_data())
        self.assertIsInstance(source_file_list, list)
        self.assertIn([_HOST_NAME, _CURRENT_FILE_FULL_PATH], source_file_list)

    def testServeSourceFileListWithoutRunParamErrors(self):
        # Make request without run param.
        response = self.server.get(_ROUTE_PREFIX + "/source_files/list")
        self.assertEqual(400, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        self.assertEqual(
            json.loads(response.get_data()),
            {"error": "run parameter is not provided"},
        )

    def testServeSourceFileContentOfThisTestFile(self):
        _generate_tfdbg_v2_data(self.logdir)
        run = self._getExactlyOneRun()
        # First, access the source file list, so we can get hold of the index
        # for this file. The index is required for the request to the
        # "/source_files/file" route below.
        response = self.server.get(
            _ROUTE_PREFIX + "/source_files/list?run=%s" % run
        )
        source_file_list = json.loads(response.get_data())
        index = source_file_list.index([_HOST_NAME, _CURRENT_FILE_FULL_PATH])

        response = self.server.get(
            _ROUTE_PREFIX + "/source_files/file?run=%s&index=%d" % (run, index)
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        data = json.loads(response.get_data())
        self.assertEqual(data["host_name"], _HOST_NAME)
        self.assertEqual(data["file_path"], _CURRENT_FILE_FULL_PATH)
        with open(__file__, "r") as f:
            lines = f.read().split("\n")
        self.assertEqual(data["lines"], lines)

    def testServeSourceFileWithoutRunErrors(self):
        # Make request without run param.
        response = self.server.get(_ROUTE_PREFIX + "/source_files/file")
        self.assertEqual(400, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        self.assertEqual(
            json.loads(response.get_data()),
            {"error": "run parameter is not provided"},
        )

    def testServeSourceFileWithOutOfBoundIndexErrors(self):
        _generate_tfdbg_v2_data(self.logdir)
        run = self._getExactlyOneRun()
        # First, access the source file list, so we can get hold of the index
        # for this file. The index is required for the request to the
        # "/source_files/file" route below.
        response = self.server.get(
            _ROUTE_PREFIX + "/source_files/list?run=%s" % run
        )
        source_file_list = json.loads(response.get_data())
        self.assertTrue(source_file_list)

        # Use an out-of-bound index.
        invalid_index = len(source_file_list)
        response = self.server.get(
            _ROUTE_PREFIX
            + "/source_files/file?run=%s&index=%d" % (run, invalid_index)
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual(
            "application/json", response.headers.get("content-type")
        )
        self.assertEqual(
            json.loads(response.get_data()),
            {
                "error": "There is no source-code file at index %d"
                % invalid_index
            },
        )


if __name__ == "__main__":
    tf.test.main()
